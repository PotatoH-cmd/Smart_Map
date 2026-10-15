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
from ..providers import resolve_kb_fallback
from ..state import AgentState, QwenToolAdapter

logger = logging.getLogger(__name__)

# 单工具步骤执行超时（秒）：本地 adapter 在进程内线程池执行时无内置超时，
# 若无此兜底，单个工具（如 caisha_report_tool 内 dashscope 无 timeout 调用）可无限阻塞，
# 整个 run 只能等外层 RUN_TIMEOUT_SECONDS(600s) 兜底，用户感知为"一直不出结果"。
# 超时后返回明确失败结果，run 始终有终态。QGIS 重工作流步骤需更多时间，单独放宽。
_TOOL_STEP_TIMEOUT = int(os.environ.get("TOOL_STEP_TIMEOUT_SECONDS", "240"))
# qgis_mcp_tool 通用工作流步骤超时（相对更长，含多步 MCP 调用）
_QGIS_WORKFLOW_TIMEOUT = int(os.environ.get("QGIS_WORKFLOW_TIMEOUT_SECONDS", "480"))

# ── 采区名称归一化（红线距离分析用）：SHP 容器路径 /gis_data 的宿主机映射 ──
_HOST_GIS_DIRS = [
    "/home/szgczx/python/GIS",
    "/home/server/python/GIS",
]

# 任务描述词：出现在要素名尾部时剥离（"郝楼采区砂场到最近河道" → "郝楼采区砂场"）
_TASK_TAILS = ("到最近河道", "到最近红线", "距最近河道", "最近河道", "最近红线", "距红线")
# 业务后缀：叠加在库名上的冗余写法（"XX采区采砂场" → "XX采区"）
_BIZ_TAILS = ("采砂场", "砂场", "可采区", "采区", "可采点", "采点", "砂站")


def _read_shp_names(shp_path: str) -> List[str]:
    """读取 SHP 图层全部 Name 字段值（容器路径 /gis_data 自动映射到宿主机目录）。"""
    if not shp_path:
        return []
    import sys
    # PROJ 数据目录兜底（conda 环境被迁移后 PROJ_LIB 可能指向旧路径）
    proj_lib = os.path.join(sys.prefix, "share", "proj")
    if os.path.isdir(proj_lib):
        os.environ.setdefault("PROJ_LIB", proj_lib)
    host_path = shp_path
    if not os.path.exists(host_path):
        for host_dir in _HOST_GIS_DIRS:
            cand = shp_path.replace("/gis_data", host_dir, 1)
            if os.path.exists(cand):
                host_path = cand
                break
    if not os.path.exists(host_path):
        return []
    try:
        from osgeo import ogr
        ogr.UseExceptions()
        ds = ogr.Open(host_path)
        if ds is None:
            return []
        lyr = ds.GetLayer(0)
        return [str(f.GetField("Name")) for f in lyr if f.GetField("Name")]
    except Exception as e:
        logger.warning(f"[qgis_workflow] 读取 SHP 名称失败: {e}")
        return []


def _resolve_feature_name(raw_name: str, names: List[str]) -> str:
    """名称归一化："郝楼采区砂场到最近河道" → "潢河郝楼村采区"。

    逐层剥离任务描述词与业务后缀生成候选，在 SHP 名称列表中做唯一包含匹配；
    多义（如"史河"命中多个名称）不猜，返回原样由调用方报明确错误。
    """
    if not raw_name or not names:
        return raw_name
    candidates: List[str] = []
    cur = raw_name
    while cur:
        if cur not in candidates:
            candidates.append(cur)
        nxt = ""
        for task in _TASK_TAILS:
            if cur.endswith(task):
                nxt = cur[:-len(task)].strip()
                break
        if not nxt:
            for tail in _BIZ_TAILS:
                if cur.endswith(tail):
                    nxt = cur[:-len(tail)].strip()
                    break
        if nxt and nxt != cur:
            cur = nxt
            continue
        break
    for cand in candidates:
        matches = [n for n in names if cand in n]
        if len(matches) == 1:
            return matches[0]
    return raw_name


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
            """薄包装：委托公共执行内核（observe_node / RunEngine._invoke_step 共用）。"""
            return await self._invoke_single_step(state, step, extra_params)

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
        # FC 步骤：tool_call_id 上浮到结果根（供观测器以 role:"tool" 协议回填；gather 顺序保留）
        for _s, _r in zip(pre_steps, pre_results):
            if getattr(_s, "tool_call_id", None) and isinstance(_r, dict):
                _r["tool_call_id"] = _s.tool_call_id
        pre_results = await self._fallback_knowledge_search(pre_results, state["user_message"], _invoke_step)

        # ── 知识库无结果 → 联网搜索回退（确定性决策，覆盖 /chat 非流式路径）──
        # 原逻辑仅 DB 空→KB 回退，KB 也空时 LLM 会回复"无可用文档"；
        # 此处补一步确定性联网搜索，与 RunEngine._decide_web_search_fallback 逻辑一致。
        pre_results = await self._fallback_web_search(pre_results, state, intent_result, _invoke_step)

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

    def _get_tool_parameters(self, tool_name: str) -> List[Dict[str, Any]]:
        """工具声明的 parameters（类属性直读，不实例化工具）。未知工具返回 []。

        供 schema 校验（_check_tool_params）与缺参检测（RunEngine._detect_missing_params）共用；
        实现收敛到 core.toolhub.param_validate.local_tool_parameters（单一来源 + 幂等扫描缓存）。
        """
        from core.toolhub.param_validate import local_tool_parameters
        return local_tool_parameters(tool_name)

    def _check_tool_params(self, tool_name: str, params: Dict[str, Any]):
        """参数 Schema 校验（Phase 1）：修复 → 校验。

        返回 (可用参数, 失败结果或 None)：enforce 模式下 blocking 错误直接失败、不调用工具；
        warn（默认）模式仅记录日志，修复结果照常生效。
        """
        from core.toolhub.param_validate import (
            MODE_ENFORCE, MODE_OFF, blocking_issues, format_issues,
            validate_against, validation_mode, verification_report,
        )
        if validation_mode() == MODE_OFF:
            return params, None
        declared = self._get_tool_parameters(tool_name)
        if not declared:
            return params, None
        coerced, issues = validate_against(declared, params, tool_name=tool_name)
        if not issues:
            return coerced, None
        blocking = blocking_issues(issues)
        if blocking:
            logger.warning(f"[tool_node] {tool_name} 参数校验未通过: {format_issues(blocking)}")
        else:
            logger.info(f"[tool_node] {tool_name} 参数校验告警: {format_issues(issues)}")
        if blocking and validation_mode() == MODE_ENFORCE:
            return coerced, {
                "success": False,
                "error": f"参数校验失败: {format_issues(blocking)}",
                "_verification": verification_report(tool_name, issues),
            }
        return coerced, None

    async def _invoke_single_step(self, state: AgentState, step, extra_params: Dict = None,
                                  params_override: Dict = None) -> Dict[str, Any]:
        """单步执行内核：TOOL_HUB_URL 外呼 → 本地 adapter 回退。

        tool_node / observe_node / RunEngine._invoke_step 共用同一实现，
        保证服务化路径（Tool Hub）与本地路径在所有执行路径中待遇一致。
        - extra_params：追加参数并合并进 step.params（原有语义，逐字保留）
        - params_override：调用方已完成参数归一化时传入（RunEngine 路径）
        """
        params = params_override if params_override is not None else (step.params or {})
        if extra_params:
            params.update(extra_params)
        # ── 参数 Schema 校验（Phase 1）：修复 → 校验；enforce 下 blocking 直接失败（不调用工具）──
        params, schema_failure = self._check_tool_params(step.tool, params)
        if schema_failure is not None:
            return {"tool_name": step.tool, "result": schema_failure}
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
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, adapter.invoke, params),
                timeout=_TOOL_STEP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"[tool_node] Tool {step.tool} timed out after {_TOOL_STEP_TIMEOUT}s")
            return {"tool_name": step.tool,
                    "result": {"success": False,
                               "error": f"工具 {step.tool} 执行超时（超过 {_TOOL_STEP_TIMEOUT}s 未返回结果，已终止）"}}

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
            # 无匹配 recipe → 透传给 MCP（LLM 已指定 category/action），带超时防阻塞
            loop = asyncio.get_event_loop()
            try:
                raw = await asyncio.wait_for(
                    loop.run_in_executor(None, adapter.invoke, step.params or {}),
                    timeout=_QGIS_WORKFLOW_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"[qgis_workflow] 透传调用超时（>{_QGIS_WORKFLOW_TIMEOUT}s）")
                return {"success": False, "error": f"GIS 工具执行超时（超过 {_QGIS_WORKFLOW_TIMEOUT}s）"}
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

        # ── 修复：feature_name 归一化提前到步骤执行前 ──
        # 用户口语名称（"郝楼砂场"）与 SHP 正式名（"潢河郝楼村采区"）常有出入，
        # export 的 Name LIKE 过滤会匹配 0 要素，导致 buffer/original 全空、
        # 前端报"无可用要素"且回复谎报成功。此处读 SHP 名称做唯一包含匹配归一化，
        # buffer / distance_to_redline 等所有 recipe 统一受益。
        if variables.get("feature_name") and variables.get("shp_path"):
            _names = _read_shp_names(variables["shp_path"])
            _norm = _resolve_feature_name(variables["feature_name"], _names)
            if _norm and _norm != variables["feature_name"]:
                logger.info(f"[qgis_workflow] 名称归一化: {variables['feature_name']!r} → {_norm!r}")
                variables["feature_name"] = _norm

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
                else:
                    # ── 修复：0 要素视为失败，禁止生成空图层指令与谎报成功 ──
                    results["success"] = False
                    results["error"] = (
                        f"SHP 中未匹配到 '{variables.get('feature_name', '')}' 的要素，"
                        "缓冲区生成失败，请确认名称后重试"
                    )
                    logger.warning(f"[qgis_workflow] Combine: 0 features，filter 未匹配任何要素")
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

                # 1b) ── 修复：导出 0 要素（filter 未匹配）时的名称归一化重试 ──
                # 意图 LLM 提取的采区名（"郝楼采区砂场"）与 SHP 正式名（"潢河郝楼村采区"）
                # 常有出入，export 的 Name LIKE 过滤匹配不到要素，原逻辑直接
                # features[0] 抛 list index out of range，导致 $distance 未渲染、
                # 前端加载空连线图层。此处从 SHP 读名称做唯一包含匹配归一化后重导出。
                if not feat_gj.get("features"):
                    norm_name = _resolve_feature_name(
                        variables.get("feature_name", ""),
                        _read_shp_names(variables.get("shp_path", "")),
                    )
                    source_layer_id = variables.get("source_layer_id", "")
                    if norm_name and norm_name != variables.get("feature_name") and source_layer_id:
                        logger.info(
                            f"[qgis_workflow] 名称归一化: {variables.get('feature_name')!r} → {norm_name!r}"
                        )
                        variables["feature_name"] = norm_name
                        _call_mcp({"category": "layer", "action": "export", "params": {
                            "layer_id": source_layer_id,
                            "output_path": feat_fname,
                            "filter_expression": f"Name LIKE '%{norm_name}%'",
                            "target_crs": "EPSG:4326",
                        }})
                        with open(feat_path) as f:
                            feat_gj = json.load(f)
                if not feat_gj.get("features"):
                    raise ValueError(
                        f"SHP 中未匹配到与 '{variables.get('feature_name', '')}' 对应的采区要素，"
                        "请确认采区名称后重试"
                    )

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
                results["success"] = False
                results["error"] = str(e)

        # 6. 结果
        if results.get("error"):
            # ── 修复：失败时输出明确错误信息，避免占位符残留与谎报成功 ──
            feat = variables.get("feature_name", "") or "该要素"
            msg = f"抱歉，对{feat}执行空间分析失败：{results['error']}。请确认名称后重试。"
        else:
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
        if geo and not results.get("error"):
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

        # ── 知识库回退策略注册表：定主题定向检索（如方案年份）──
        # 领域定制的定向检索与检索词覆盖收敛到 providers/kb_fallback 注册表，
        # 此处只负责调度：先定向检索（命中即在空库判断前提前返回），
        # 空库时再用其检索词覆盖。
        def _make_plan_llm():
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=self.llm_cfg.get("model", "qwen-plus"),
                base_url=self.llm_cfg.get("model_server") or self.llm_cfg.get("base_url"),
                api_key=self.llm_cfg.get("api_key", ""),
                temperature=0,
            )

        targeted, custom_query = await resolve_kb_fallback(
            user_message, pre_results,
            invoke_step=_invoke_step, llm_factory=_make_plan_llm,
        )
        if targeted:
            pre_results.extend(targeted)
            return pre_results

        # ── 去重守卫：计划里已含知识库步骤且成功返回时不再重复回退检索 ──
        # （否则"双工具计划"场景下同问题会被检索两次：计划内一次 + 空库回退一次）
        if any(
            r.get("tool_name") == "knowledge_base_tool"
            and isinstance(r.get("result"), dict)
            and r["result"].get("success")
            and r["result"].get("data")
            for r in pre_results
        ):
            return pre_results

        db_data_is_empty = False
        for r in pre_results:
            if r.get("tool_name") not in db_tool_names:
                continue
            result = r.get("result", {})
            if not isinstance(result, dict):
                continue
            if result.get("success") is not True:
                # ── 修复：参数/调用类执行错误如实上报，不回退知识库 ──
                # 意图 LLM 常生成缺参数的 SQL/工具调用（如 spatial_query 缺
                # spatial_table），若视为"查询无结果"回退知识库，用户会看到
                # 一堆无关文档引用和编造的失败结论。仅连接/服务类故障才回退。
                err = str(result.get("error") or result.get("message") or "")
                if any(k in err for k in ("必须指定", "不支持的操作", "缺少", "参数", "语法", "syntax")):
                    return pre_results
                # DB 查询失败（网络/连接错误等），也应回退
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

        # ── 空库回退检索词：优先使用 Provider 注册表给出的检索词覆盖 ──
        # （如砂场统计类问题改用"豫X砂许 YYYY 采砂许可"模板词，由 SandfieldKbProvider.query_for 提供）
        kb_query = custom_query or user_message

        # 构造 knowledge_base_tool 的假 step 用于调用
        from dataclasses import dataclass

        @dataclass
        class _FakeStep:
            tool: str = "knowledge_base_tool"
            params: dict = None
            step_id: int = 9999

        fake_step = _FakeStep(
            tool="knowledge_base_tool",
            params={"operation": "search", "query": kb_query, "top_k": 20},
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

    async def _fallback_web_search(
        self, pre_results: List[Dict], state: Dict[str, Any],
        intent_result: IntentResult, _invoke_step
    ) -> List[Dict]:
        """知识库检索无结果时改用联网搜索；返回补充后的结果列表。

        触发条件（全部满足）：知识类意图；计划中有 knowledge_base_tool 且全部结果为空；
        其他工具未产出实质性结果；尚未执行过联网搜索；用户原问题非空。
        与 RunEngine._decide_web_search_fallback 逻辑保持一致。
        """
        _WEB_FALLBACK_INTENTS = ("knowledge_search", "unknown", "cross_intent")
        primary = getattr(intent_result, "primary_intent", None)
        primary_val = primary.value if hasattr(primary, "value") else str(primary or "")
        if primary_val not in _WEB_FALLBACK_INTENTS:
            return pre_results

        kb_results = [
            r for r in pre_results
            if isinstance(r, dict) and r.get("tool_name") == "knowledge_base_tool"
        ]
        if not kb_results:
            return pre_results
        if any(
            isinstance(r, dict) and r.get("tool_name") == "web_search_tool"
            for r in pre_results
        ):
            return pre_results
        if any(not self._kb_result_empty(r.get("result")) for r in kb_results):
            return pre_results  # 知识库有结果 → 不需要联网
        others = [
            r for r in pre_results
            if isinstance(r, dict) and r.get("tool_name") != "knowledge_base_tool"
        ]
        if any(self._has_substantive_result(r) for r in others):
            return pre_results  # 其他工具已产出实质结果
        query = (state.get("user_message") or "").strip()
        if not query:
            return pre_results

        logger.info(f"[tool_node] 知识库无结果，自动联网搜索: query={query[:80]}")

        from dataclasses import dataclass

        @dataclass
        class _FakeWebStep:
            tool: str = "web_search_tool"
            params: dict = None
            step_id: int = 9998

        fake_step = _FakeWebStep(
            tool="web_search_tool",
            params={"query": query},
        )
        try:
            ws_result = await _invoke_step(fake_step)
            logger.info(
                f"[tool_node] 联网搜索回退完成: success={ws_result.get('result', {}).get('success')}"
            )
            pre_results.append(ws_result)
        except Exception as e:
            logger.warning(f"[tool_node] 联网搜索回退失败: {e}")

        return pre_results

    @staticmethod
    def _kb_result_empty(result) -> bool:
        """知识库检索结果是否为'空'（成功但无内容）。失败（执行错误）不算空。"""
        if not isinstance(result, dict) or result.get("success") is not True:
            return False
        data = result.get("data")
        if isinstance(data, (list, dict)) and data:
            return False
        if str(result.get("content") or "").strip():
            return False
        return True

    @staticmethod
    def _has_substantive_result(item) -> bool:
        """工具步骤是否已产出实质性结果（成功且有数据/命令/产物/答案）。"""
        if not isinstance(item, dict):
            return False
        result = item.get("result")
        if not isinstance(result, dict) or result.get("success") is not True:
            return False
        data = result.get("data")
        if data not in (None, [], {}, ""):
            return True
        return bool(
            result.get("map_commands") or result.get("map_command")
            or result.get("charts") or result.get("report_url")
            or result.get("answer") or result.get("content")
        )

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
