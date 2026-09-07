"""工具节点：工具执行 · QGIS 工作流 · 知识库兜底检索（自 task_executor.py 机械搬移，方法体逐字保留）。
P1 拆分：以 Mixin 形式挂载到 TaskExecutor，self 语义与运行时行为完全不变。
"""
import asyncio
import httpx
import json
import logging
import operator
import os
import re
from typing import Annotated, List, Dict, Any, Optional, TypedDict, AsyncGenerator
from urllib.parse import quote

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from ..intent_types import IntentType, IntentResult, TaskStep
from ..qgis_workflows import match_recipe, extract_params, substitute_params, RECIPES
from ..state import AgentState, QwenToolAdapter

logger = logging.getLogger(__name__)


class ToolNodeMixin:
    async def _tool_node(self, state: AgentState) -> AgentState:
        """节点2：执行 execution_plan 中的工具步骤。

        qgis_mcp_tool 步骤自动匹配 recipe 并通过通用工作流引擎执行。
        """
        intent_result: IntentResult = state["intent_result"]
        steps = [s for s in intent_result.execution_plan if s.tool]

        # ── 分离：qgis_mcp_tool 步骤走通用工作流引擎 ──
        qgis_steps, normal_steps = [], []
        for s in steps:
            if s.tool == "qgis_mcp_tool":
                qgis_steps.append(s)
            else:
                normal_steps.append(s)

        async def _invoke_step(step, extra_params: Dict = None) -> Dict:
            params = step.params or {}
            if extra_params:
                params.update(extra_params)
            # 服务化路径：TOOL_HUB_URL 配置时统一走工具中台，失败回退进程内实例
            hub_url = os.environ.get("TOOL_HUB_URL")
            if hub_url:
                try:
                    resp = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: httpx.post(
                            f"{hub_url.rstrip('/')}/v1/tools/{step.tool}/invoke",
                            json={"params": params}, timeout=600.0),
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    logger.info(f"[tool_node] Via tool-hub: {step.tool}, "
                                f"success={result.get('success')}, {result.get('elapsed_ms', 0)}ms")
                    return {"tool_name": step.tool, "result": result}
                except Exception as e:
                    logger.warning(f"[tool_node] tool-hub invoke failed for {step.tool}, "
                                   f"fallback local: {e}")
            adapter = self._get_tool_adapter(step.tool)
            if adapter is None:
                logger.warning(f"[tool_node] Tool not available: {step.tool}")
                return {"tool_name": step.tool, "result": {"success": False, "error": f"工具 {step.tool} 不可用"}}
            logger.info(f"[tool_node] Calling tool={step.tool}, params={params}")
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, adapter.invoke, params)

        # ── 执行 qgis_mcp_tool 步骤（通过通用工作流引擎） ──
        qgis_results = []
        for gs in qgis_steps:
            try:
                result = await self._execute_qgis_workflow(state, gs)
                qgis_results.append({"tool_name": "qgis_mcp_tool", "result": result})
            except Exception as e:
                logger.error(f"[tool_node] QGIS workflow failed: {e}", exc_info=True)
                qgis_results.append({"tool_name": "qgis_mcp_tool", "result": {"success": False, "error": str(e)}})

        # ── 执行普通步骤 ──
        pre_steps = [s for s in normal_steps if s.tool != "report_generator_tool"]
        report_steps = [s for s in normal_steps if s.tool == "report_generator_tool"]
        pre_results = list(await asyncio.gather(*[_invoke_step(s) for s in pre_steps]))
        pre_results = await self._fallback_knowledge_search(pre_results, state["user_message"], _invoke_step)

        report_results = []
        if report_steps:
            ordered_pre = list(zip(range(len(pre_results)), pre_results))
            data_sufficient, skip_reason = self._check_data_sufficiency(ordered_pre)
            if not data_sufficient:
                logger.warning(f"[tool_node] 数据不足，跳过报告生成: {skip_reason}")
                for rs in report_steps:
                    report_results.append({"tool_name": "report_generator_tool", "result": {"success": False, "error": skip_reason}})
            else:
                for rs in report_steps:
                    auto_vars = self._build_report_variables(ordered_pre, state["user_message"], intent_result)
                    r = await _invoke_step(rs, extra_params={"variables": auto_vars})
                    report_results.append(r)

        state["tool_results"] = qgis_results + pre_results + report_results
        return state

    async def _execute_qgis_workflow(self, state: AgentState, step) -> Dict[str, Any]:
        """通用 QGIS 工作流引擎。

        根据用户消息匹配 recipe → 提取参数 → 逐步执行 MCP 调用 → 后处理。
        新增 GIS 操作只需在 qgis_workflows.py 添加 recipe 定义。
        """
        import re as _re, hashlib, time, os as _os

        user_msg = state.get("user_message", "")
        adapter = self._get_tool_adapter("qgis_mcp_tool")
        if adapter is None:
            return {"success": False, "error": "qgis_mcp_tool 不可用"}

        # 1. 匹配 recipe
        recipe_name, recipe = match_recipe(user_msg)
        if not recipe:
            # 无匹配 recipe → 透传给 MCP（LLM 已指定 category/action）
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, adapter.invoke, step.params or {})
            return raw.get("result", raw) if isinstance(raw, dict) else {"success": True, "data": raw}

        logger.info(f"[qgis_workflow] Matched: {recipe_name}")

        # 2. 提取参数
        variables = extract_params(user_msg, recipe)
        variables["uid"] = hashlib.md5(f"{recipe_name}{time.time()}".encode()).hexdigest()[:8]
        variables["user_msg"] = user_msg
        for entry in recipe.get("extract", []):
            pname = entry["param"]
            if pname not in variables:
                d = entry.get("patterns", {}).get("default")
                if d is not None:
                    variables[pname] = d
        logger.info(f"[qgis_workflow] Vars: {json.dumps({k:v for k,v in variables.items() if k!='user_msg'}, ensure_ascii=False)}")

        # 3. MCP 调用辅助
        def _call_mcp(params: Dict) -> Dict:
            raw = adapter.invoke(params)
            inner = raw.get("result", raw) if isinstance(raw, dict) else {}
            return inner

        def _extract_layer_id(result: Dict) -> str:
            if not result.get("success"):
                return ""
            data = result.get("data", {})
            if not isinstance(data, dict):
                return ""
            for src in [data.get("content", []), data.get("structuredContent", {}).get("result", [])]:
                if isinstance(src, list) and src:
                    t = src[0].get("text", "") if isinstance(src[0], dict) else ""
                    if t:
                        try:
                            lid = json.loads(t).get("id", "")
                            if lid:
                                return lid
                        except (json.JSONDecodeError, TypeError):
                            pass
            try:
                m = _re.search(r'"id":\s*"([^"]{20,})"', json.dumps(data))
                return m.group(1) if m else ""
            except Exception:
                return ""

        # 4. 逐步执行 recipe
        results = {"steps": [], "success": True}
        for i, step_def in enumerate(recipe.get("steps", [])):
            resolved = substitute_params(step_def.get("params", {}), variables)
            sr = _call_mcp({"category": step_def["category"], "action": step_def["action"], "params": resolved})
            results["steps"].append({"i": i, "action": step_def["action"], "ok": sr.get("success", False)})
            # 捕获变量
            for cap_key, var_name in step_def.get("capture", {}).items():
                if cap_key == "id":
                    variables[var_name] = _extract_layer_id(sr)
                elif cap_key == "output_path":
                    variables[var_name] = resolved.get("output_path", "")

        # 5. 后处理：合并 GeoJSON
        geojson_dir = "/home/server/python/map_assistant_v1/backend/static/geojson"
        combine_cfg = recipe.get("post_process", {}).get("combine_geojson")
        if combine_cfg:
            try:
                features = []
                for idx, tpl in enumerate(combine_cfg.get("inputs", [])):
                    fpath = _os.path.join(geojson_dir, _os.path.basename(substitute_params(tpl, variables)))
                    if _os.path.exists(fpath):
                        with open(fpath) as f:
                            gj = json.load(f)
                        tag = combine_cfg.get("tags", [{}])[min(idx, len(combine_cfg.get("tags", [])) - 1)]
                        for feat in gj.get("features", []):
                            feat["properties"].update(substitute_params(tag, variables))
                            features.append(feat)
                if features:
                    out_fname = _os.path.basename(substitute_params(combine_cfg["output"], variables))
                    out_full = _os.path.join(geojson_dir, out_fname)
                    with open(out_full, 'w') as f:
                        json.dump({"type": "FeatureCollection", "features": features}, f)
                    results["combined_geojson"] = f"/static/geojson/{out_fname}"
                    logger.info(f"[qgis_workflow] Combined: {out_full} ({len(features)} features)")
            except Exception as e:
                logger.warning(f"[qgis_workflow] Combine failed: {e}")

        # 5b. 后处理：距离到红线（shapely 计算，无需 QGIS）
        dist_cfg = recipe.get("post_process", {}).get("distance_to_redline")
        if dist_cfg:
            try:
                from pyproj import Transformer
                from shapely.geometry import shape, LineString, mapping
                from shapely.ops import nearest_points, transform as shp_transform
                from tools.overlay_tile_service import get_layer

                _to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
                _to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform

                # 1) 读取导出的采区 GeoJSON
                feat_fname = substitute_params(dist_cfg["feature_geojson"], variables)
                feat_path = _os.path.join(geojson_dir, _os.path.basename(feat_fname))
                with open(feat_path) as f:
                    feat_gj = json.load(f)

                # 2) 从 overlay_tile_service 获取红线 shapely 几何（EPSG:3857）
                hx_layer = get_layer("hx")
                if hx_layer is not None:
                    hx_layer.load()  # 确保图层已加载
                if not hx_layer or not hx_layer.geoms:
                    raise ValueError("红线图层未加载或为空")

                # 3) 解析采区几何（EPSG:4326 → EPSG:3857 以匹配红线坐标系）
                feature_geom_4326 = shape(feat_gj["features"][0]["geometry"])
                feature_geom_3857 = shp_transform(_to_3857, feature_geom_4326)

                # 4) 计算到每条红线的最短距离
                min_dist = float("inf")
                best_pair = None
                for hx_geom in hx_layer.geoms:
                    d = feature_geom_3857.distance(hx_geom)
                    if d < min_dist:
                        min_dist = d
                        best_pair = nearest_points(feature_geom_3857, hx_geom)

                # 5) 构建最短连线 GeoJSON（EPSG:4326）
                p1_4326 = shp_transform(_to_4326, best_pair[0])
                p2_4326 = shp_transform(_to_4326, best_pair[1])
                conn_line = LineString([p1_4326, p2_4326])

                out_fname = substitute_params("${feature_name}_redline_dist_${uid}.geojson", variables)
                out_full = _os.path.join(geojson_dir, out_fname)
                with open(out_full, "w") as f:
                    json.dump({
                        "type": "FeatureCollection",
                        "features": [{
                            "type": "Feature",
                            "geometry": mapping(conn_line),
                            "properties": {"distance_m": round(min_dist, 1)}
                        }]
                    }, f)

                results["combined_geojson"] = f"/static/geojson/{out_fname}"
                variables["distance"] = str(round(min_dist, 1))
                logger.info(f"[qgis_workflow] 最近红线距离: {min_dist:.1f}m, 连线: {out_fname}")
            except Exception as e:
                logger.warning(f"[qgis_workflow] distance_to_redline 失败: {e}")

        # 6. 结果
        msg = substitute_params(recipe.get("result_message", "操作完成。"), variables)
        results["message"] = msg
        results["data"] = {k: v for k, v in variables.items() if k != "user_msg"}

        # ── 生成 map_command（由 recipe 的 render 配置驱动，通用解耦）──
        geo = results.get("combined_geojson", "")
        if not geo:
            # 无 combine_geojson → 从最后一步的 output_path 推导
            last_step = recipe.get("steps", [{}])[-1]
            last_params = last_step.get("params", {})
            out_tpl = last_params.get("output_path", "")
            if out_tpl:
                resolved = substitute_params(out_tpl, variables)
                fname = _os.path.basename(resolved)
                full = _os.path.join(geojson_dir, fname)
                if _os.path.exists(full):
                    geo = f"/static/geojson/{fname}"
        if geo:
            results["map_command"] = self._build_render_command(recipe, variables, geo)
        return results

    def _build_render_command(self, recipe: Dict, variables: Dict, geo_path: str) -> Dict:
        """从 recipe 的 render 配置生成前端 map_command。

        新增空间分析功能时只需在 recipe 中配置 render 字段，无需改此方法。
        render 结构：
        {
            "layer_name_template": "${feature_name}-centroid",
            "style": { "point": {...} } | { "polygon": {...} },
            "view": { "strategy": "fly_to_centroid", "zoom": 15 } | null
        }
        """
        render_cfg = recipe.get("render", {})

        # 图层名：模板 + variable 替换
        name_tpl = render_cfg.get("layer_name_template", recipe.get("description", "result"))
        layer_name = substitute_params(name_tpl, variables)

        # URL-encode 文件名中的中文字符
        encoded_url = "/".join(quote(part, safe='/._-') for part in geo_path.split("/"))

        cmd: Dict[str, Any] = {
            "type": "load_vector_layer",
            "url": encoded_url,
            "name": layer_name,
        }

        # 有 render 配置时，把 style/view 透传给前端做样式驱动
        if render_cfg:
            if render_cfg.get("style"):
                cmd["style"] = render_cfg["style"]
            if render_cfg.get("view"):
                cmd["view"] = render_cfg["view"]

        return cmd

    async def _fallback_knowledge_search(
        self, pre_results: List[Dict], user_message: str, _invoke_step
    ) -> List[Dict]:
        """数据库查询结果为空时，自动回退到知识库检索。

        检查 pre_results 中是否有 postgresql_tool / mcp_postgres_tool 返回了空数据
        （success=True 但 data 为空或 content/message 指示无结果）。
        如果是，则调用 knowledge_base_tool 补充检索。
        """
        db_tool_names = {"postgresql_tool", "mcp_postgres_tool"}
        has_db_query = any(r.get("tool_name") in db_tool_names for r in pre_results)
        if not has_db_query:
            return pre_results

        db_data_is_empty = False
        for r in pre_results:
            if r.get("tool_name") not in db_tool_names:
                continue
            result = r.get("result", {})
            if not isinstance(result, dict):
                continue
            if result.get("success") is not True:
                # DB 查询失败（网络/语法错误等），也应回退
                db_data_is_empty = True
                break
            data = result.get("data")
            if data is None:
                db_data_is_empty = True
                break
            if isinstance(data, list):
                if len(data) == 0:
                    db_data_is_empty = True
                    break
                # 聚合查询（如 COUNT）返回 [{"count": 0}] 也视为空
                if self._is_empty_aggregate(data):
                    db_data_is_empty = True
                    break
            # 也检测 content/message 中是否包含"无结果"提示
            content = result.get("content") or result.get("message") or ""
            if content and any(kw in str(content) for kw in ["0 rows", "0 条记录", "无记录", "no rows", "empty"]):
                db_data_is_empty = True
                break

        if not db_data_is_empty:
            return pre_results

        logger.info(
            f"[tool_node] 数据库查询无结果，自动回退到知识库检索，query={user_message[:80]}"
        )

        # 构造 knowledge_base_tool 的假 step 用于调用
        from dataclasses import dataclass

        @dataclass
        class _FakeStep:
            tool: str = "knowledge_base_tool"
            params: dict = None
            step_id: int = 9999

        fake_step = _FakeStep(
            tool="knowledge_base_tool",
            params={"operation": "search", "query": user_message},
        )
        try:
            kb_result = await _invoke_step(fake_step)
            logger.info(
                f"[tool_node] 知识库回退检索完成: success={kb_result.get('result', {}).get('success')}, "
                f"count={kb_result.get('result', {}).get('count', 0)}"
            )
            pre_results.append(kb_result)
        except Exception as e:
            logger.warning(f"[tool_node] 知识库回退检索失败: {e}")

        return pre_results

    @staticmethod
    def _is_empty_aggregate(data: List) -> bool:
        """
        检测聚合查询（如 COUNT/SUM）返回的 data 是否全部为零/空值。
        例如 [{"count": 0}], [{"total": "0"}], [{"cnt": None}] 等。
        """
        if not isinstance(data, list) or not data:
            return False
        for row in data:
            if not isinstance(row, dict):
                return False  # 非字典行不加判断，避免误判
            for v in row.values():
                if v is not None and v != 0 and v != "0" and v != "" and v is not False:
                    return False  # 存在非零非空的实际值
        return True

    def _sync_kb_search(self, query: str) -> Optional[str]:
        """同步调用知识库检索（通过 KnowledgeBaseTool 适配器），返回格式化的摘要字符串。

        作为 _fallback_knowledge_search（异步）未生效时的补偿兜底。
        后端无关：通过统一的 QwenToolAdapter 接口调用，支持 RagFlow / LlamaIndex 双后端。
        """
        try:
            adapter = self._get_tool_adapter("knowledge_base_tool")
            if adapter is None:
                logger.warning("[sync_kb] knowledge_base_tool 适配器不可用")
                return None
            result = adapter.invoke({"operation": "search", "query": query, "top_k": 5})
            data = result.get("result", {}).get("data", [])
            if not data:
                return None
            lines = [f"【知识库补充检索】找到 {len(data)} 条相关内容："]
            for i, item in enumerate(data[:5]):
                title = item.get("title", "未知文档")
                text = (item.get("content", "") or "")[:400]
                lines.append(f"  [{i+1}] {title}\n     {text}")
            logger.info(f"[sync_kb] Got {len(data)} KB results for query: {query[:60]}")
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[sync_kb] Failed: {e}")
            return None
