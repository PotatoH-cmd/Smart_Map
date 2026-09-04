import asyncio
import json
import logging
import operator
import os
import re
from typing import Annotated, List, Dict, Any, Optional, TypedDict, AsyncGenerator
from urllib.parse import quote

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from .intent_types import IntentType, IntentResult, TaskStep
from .intent_agent import IntentAgent
from .tool_registry import ToolRegistry
from .agent_harness import AgentHarness
from .qgis_workflows import match_recipe, extract_params, substitute_params, RECIPES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangGraph 状态定义
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    user_message: str
    chat_history: List[Dict]
    intent_result: Optional[IntentResult]
    tool_results:     Annotated[List[Dict], operator.add]   # 并行写入自动合并
    tool_summaries:   List[str]                              # extract_node → summarize_node 传递
    response: str
    map_commands:     Annotated[List[Dict], operator.add]
    cesium_commands:  Annotated[List[Dict], operator.add]
    charts:           Annotated[List[Dict], operator.add]
    report_url: Optional[str]
    error: Optional[str]


# ---------------------------------------------------------------------------
# QwenTool 适配器：将 BaseTool 实例包装为可直接调用的函数
# ---------------------------------------------------------------------------

class QwenToolAdapter:
    """将 qwen_agent BaseTool 实例包装，提供统一的 invoke(params: dict) 接口。"""

    def __init__(self, tool_instance, tool_name: str):
        self.tool = tool_instance
        self.name = tool_name

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.tool.call(params)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = {"success": True, "content": result}
            return {"tool_name": self.name, "result": result}
        except Exception as e:
            logger.error(f"Tool {self.name} invoke error: {e}")
            return {"tool_name": self.name, "result": {"success": False, "error": str(e)}}


# ---------------------------------------------------------------------------
# TaskExecutor：LangGraph 编排
# ---------------------------------------------------------------------------

class TaskExecutor:
    def __init__(self, llm_cfg: Dict[str, Any]):
        self.llm_cfg = llm_cfg
        self.intent_agent = IntentAgent(llm_cfg)
        self.tool_registry = ToolRegistry()
        self.harness = AgentHarness(llm_cfg)       # 调度中枢
        self._tool_instances: Dict[str, QwenToolAdapter] = {}
        self._memory = MemorySaver()          # 进程内存储，内存操作 <1ms
        self._memory_max_threads = 200        # MemorySaver 线程数上限（超出按最近活跃淘汰）

        # 构建 LangGraph 图
        self._graph = self._build_graph(llm_cfg)

        # 阶段1/2/3/4：RunEngine + RulesGateway（验证链）+ WorkspaceState（会话状态树）
        # + ContextManager（上下文预算）；main.py 按 RUN_ENGINE 灰度开关启用
        from .run_engine import RunEngine  # 延迟导入，避免包加载顺序问题
        from .rules_gateway import get_rules_gateway
        from .workspace_state import get_workspace_state
        from .context_manager import ContextManager
        self.rules_gateway = get_rules_gateway()
        self.workspace = get_workspace_state()
        self.context_manager = ContextManager(workspace=self.workspace)
        self.run_engine = RunEngine(self, gateway=self.rules_gateway)
        self._thread_last_seen: Dict[str, float] = {}   # thread_id -> 最近活跃时刻（MemorySaver 淘汰依据）

    def _prune_checkpoints(self, thread_id: str) -> None:
        """记录 thread 活跃并淘汰 MemorySaver 中最久未活跃的 thread，防止进程内存无限增长。"""
        import time
        self._thread_last_seen[thread_id] = time.monotonic()
        storage = getattr(self._memory, "storage", None)
        if storage is None:
            return
        # 仅统计仍存在于 storage 的 thread
        self._thread_last_seen = {t: ts for t, ts in self._thread_last_seen.items() if t in storage}
        overflow = len(self._thread_last_seen) - self._memory_max_threads
        if overflow <= 0:
            return
        for t, _ in sorted(self._thread_last_seen.items(), key=lambda kv: kv[1])[:overflow]:
            storage.pop(t, None)
            self._thread_last_seen.pop(t, None)

    # ------------------------------------------------------------------
    # 工具实例管理（延迟初始化）
    # ------------------------------------------------------------------

    def _get_tool_adapter(self, tool_name: str) -> Optional[QwenToolAdapter]:
        if tool_name not in self._tool_instances:
            instance = self._create_tool(tool_name)
            if instance is None:
                return None
            self._tool_instances[tool_name] = QwenToolAdapter(instance, tool_name)
        return self._tool_instances[tool_name]

    def _create_tool(self, tool_name: str):
        """通过 ToolRegistry 创建工具实例。"""
        return self.tool_registry.create(tool_name)

    # ------------------------------------------------------------------
    # LangGraph 图构建
    # ------------------------------------------------------------------

    def _build_graph(self, llm_cfg: Dict[str, Any]) -> Any:
        base_url = llm_cfg.get("model_server") or llm_cfg.get("base_url")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "qwen-plus")

        self._llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0.3,
        )

        graph = StateGraph(AgentState)
        graph.add_node("intent_node", self._intent_node)
        graph.add_node("tool_node", self._tool_node)
        graph.add_node("extract_node", self._extract_node)
        graph.add_node("summarize_node", self._summarize_node)
        graph.add_node("confirmation_node", self._confirmation_node)

        graph.set_entry_point("intent_node")
        graph.add_conditional_edges(
            "intent_node",
            self._route_after_intent,
            {
                "tool_node": "tool_node",
                "summarize_node": "summarize_node",
                "confirmation_node": "confirmation_node",
                END: END,
            },
        )
        graph.add_edge("tool_node", "extract_node")
        graph.add_edge("extract_node", "summarize_node")
        graph.add_edge("summarize_node", END)
        graph.add_edge("confirmation_node", END)

        return graph.compile(checkpointer=self._memory)

    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------

    def _intent_node(self, state: AgentState, workspace_summary: str = "") -> AgentState:
        """节点1：意图识别。支持快速路由（关键词匹配绕过 LLM）。

        workspace_summary：会话状态树摘要（阶段3），仅注入 LLM 分析输入，
        不影响快速路由关键词匹配。
        """
        user_msg = state["user_message"]

        # 尝试快速路由
        # 注意：以下意图不能走快速路由——快速路由的 execution_plan 为空，
        # 会导致不调用工具就直达 summarize_node，让 LLM 凭空回答"已完成"。
        # 这些意图必须由 LLM 规划出具体工具步骤。
        fast_intent = self.harness.try_fast_classify(user_msg)
        NO_FAST_ROUTE_INTENTS = {
            IntentType.REPORT_GENERATION,  # 报告生成
            IntentType.SPATIAL_ANALYSIS,   # 空间分析（缓冲/裁剪/叠加/面积等）
            IntentType.SPATIAL_PROCESSING, # 空间数据处理（坐标转换等）
            IntentType.SPATIAL_REFERENCE,  # 空间参考（需 LLM 规划 spatial_reference_tool+postgresql_tool 步骤，避免空计划直达总结编造答案）
        }
        if fast_intent in NO_FAST_ROUTE_INTENTS:
            fast_intent = None
        if fast_intent is not None:
            # 构建轻量 IntentResult 跳过 LLM 调用
            intent_result = IntentResult(
                primary_intent=fast_intent,
                confidence=0.95,
                entities=[],
                task_context=f"快速路由：{fast_intent.value}",
                execution_plan=[],
                requires_confirmation=False,
                suggestions=[],
            )
            state["intent_result"] = intent_result
            logger.info(
                f"[intent_node] Fast route: intent={fast_intent.value}, confidence=0.95"
            )
            return state

        # 未命中快速路由，走 LLM 意图分析
        analysis_input = user_msg
        if workspace_summary:
            analysis_input = f"{workspace_summary}\n\n【用户本轮输入】\n{user_msg}"
        # 阶段6：view_hint 用于地图类工具约束的按需注入（2D/3D 二选一）
        intent_result = self.intent_agent.analyze(
            analysis_input,
            state.get("chat_history"),
            view_hint=state.get("view_hint"),
        )

        # ── 修复：LLM 有时漏填 tool 字段，导致 _route_after_intent 跳过 tool_node ──
        # 对于空间分析/处理类意图，若 execution_plan 有步骤但未指定 tool，自动补全
        _AUTO_TOOL_FOR_INTENT = {
            IntentType.SPATIAL_ANALYSIS: "qgis_mcp_tool",
            IntentType.SPATIAL_PROCESSING: "spatial_processing_tool",
            IntentType.DATA_QUERY: "postgresql_tool",
        }
        auto_tool = _AUTO_TOOL_FOR_INTENT.get(intent_result.primary_intent)
        if auto_tool:
            for step in intent_result.execution_plan:
                if not step.tool:
                    step.tool = auto_tool
                    logger.info(f"[intent_node] Auto-assigned tool={auto_tool} for step {step.step_id}")

        # ── 规则化兑底：数值查询误判为 spatial_reference 时改判 data_query ──
        # 例："史河毕店可采区的2025年控制开采高程范围是多少"含"可采区"，
        # LLM 可能受空间参考规则误导判为 spatial_reference；此时按模板构造
        # postgresql_tool 查询步骤查 ceshen 表，用真实数据回答，避免凭空编造数值。
        fallback_step = None
        if intent_result.primary_intent == IntentType.SPATIAL_REFERENCE:
            numeric_hit = any(k in user_msg for k in ("高程", "深度", "超深", "多少", "几个", "数量"))
            spatial_hit = any(k in user_msg for k in ("范围内", "附近", "周边", "离", "距"))
            if numeric_hit and not spatial_hit:
                fallback_step = self._build_numeric_fallback_step(user_msg)
        if fallback_step is not None:
            intent_result.primary_intent = IntentType.DATA_QUERY
            intent_result.execution_plan = [fallback_step]
            intent_result.task_context = f"数值查询兑底：{user_msg}"
            intent_result.confidence = 0.98
            logger.info("[intent_node] Numeric-query fallback: spatial_reference → data_query")

        logger.info(
            f"[intent_node] LLM route: intent={intent_result.primary_intent}, "
            f"confidence={intent_result.confidence}, "
            f"steps={len(intent_result.execution_plan)}"
        )
        state["intent_result"] = intent_result
        return state

    def _build_numeric_fallback_step(self, user_msg: str) -> Optional[TaskStep]:
        """数值查询兑底：构造 postgresql_tool 步骤查 ceshen 表。

        适用：LLM 把"XX可采区/砂场的（控制开采）高程/深度/数量是多少"误判为
        spatial_reference 等非数据查询意图时，用确定性模板 SQL 查询真实数据，
        避免 LLM 在无数据依据时凭空编造数值。
        """
        site_m = re.search(r"([\u4e00-\u9fa5]{2,20}?(?:可采区|砂场|采区))", user_msg or "")
        if not site_m:
            return None
        site = site_m.group(1).replace("的", "").strip()
        year_m = re.search(r"(20\d{2})", user_msg or "")

        cols: List[str]
        if "控制" in user_msg and "高程" in user_msg:
            cols = ['MIN("Control_Elevation") AS min_ctrl', 'MAX("Control_Elevation") AS max_ctrl']
        elif "实测" in user_msg and ("高程" in user_msg or "深度" in user_msg):
            cols = ['MIN("Measured_Depth") AS min_measured', 'MAX("Measured_Depth") AS max_measured']
        elif "超深" in user_msg:
            cols = ['ROUND(AVG("Control_Elevation" - "Measured_Depth")::numeric, 3) AS avg_diff_m']
        elif "高程" in user_msg or "深度" in user_msg:
            cols = [
                'MIN("Measured_Depth") AS min_measured', 'MAX("Measured_Depth") AS max_measured',
                'MIN("Control_Elevation") AS min_ctrl', 'MAX("Control_Elevation") AS max_ctrl',
            ]
        elif "多少" in user_msg or "几个" in user_msg or "数量" in user_msg:
            cols = ['COUNT(DISTINCT "Mineable_Area_Name") AS site_count', 'COUNT(*) AS point_count']
        else:
            return None

        where = f'"Mineable_Area_Name" LIKE \'%{site}%\''
        if year_m:
            where += f' AND "Year" = {year_m.group(1)}'
        sql = f"SELECT {', '.join(cols)} FROM ceshen WHERE {where}"
        return TaskStep(
            step_id=1,
            action=f"查询 {site} 的数值信息",
            tool="postgresql_tool",
            params={"operation": "query", "sql": sql, "params": []},
            reasoning="数值查询兑底：从 ceshen 表查询真实数据",
            expected_output="返回查询结果",
        )

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
            adapter = self._get_tool_adapter(step.tool)
            if adapter is None:
                logger.warning(f"[tool_node] Tool not available: {step.tool}")
                return {"tool_name": step.tool, "result": {"success": False, "error": f"工具 {step.tool} 不可用"}}
            params = step.params or {}
            if extra_params:
                params.update(extra_params)
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

    def _extract_node(self, state: AgentState) -> AgentState:
        """节点3a：从 tool_results 中提取结构化输出（map_commands / cesium_commands / charts / report_url），
        并构建 tool_summaries 供后续 summarize_node 使用。

        本节点不调用 LLM，仅做纯数据提取和整理。
        """
        intent_result: IntentResult = state["intent_result"]
        tool_results: List[Dict] = state.get("tool_results", [])

        # 从工具结果中提取各类命令
        map_commands: List[Dict] = []
        cesium_commands: List[Dict] = []
        charts: List[Dict] = []
        report_url: Optional[str] = None
        tool_summaries: List[str] = []

        for item in tool_results:
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})

            # map_command
            if result.get("map_command"):
                map_commands.append(result["map_command"])

            # cesium_command
            if result.get("cesium_command"):
                cesium_commands.append(result["cesium_command"])

            # 图表
            if tool_name == "data_visualizer_tool" and result.get("success"):
                charts.append({
                    "chart_type": result.get("chart_type"),
                    "config": result.get("config"),
                    "summary": result.get("content"),
                })

            # 报告（通用报告工具 / 采砂监测报告工具）
            if tool_name in ("report_generator_tool", "caisha_report_tool") and result.get("success"):
                report_url = result.get("report_url") or result.get("download_url")
                logger.info(f"[extract_node] Report generated: report_url={report_url} (from tool_name={tool_name}, result keys={list(result.keys())})")

            # 收集摘要，供 LLM 生成文本回答
            content = result.get("content") or result.get("message") or result.get("error", "")
            data = result.get("data")

            # 知识库检索工具：将 data（chunks 列表）转为可读摘要
            if tool_name == "knowledge_base_tool" and result.get("success"):
                kb_summary_parts = []
                kb_count = result.get("count", 0)
                if isinstance(data, list) and data:
                    kb_summary_parts.append(f"知识库检索到 {kb_count} 条相关内容：")
                    for i, chunk in enumerate(data[:5]):
                        chunk_title = chunk.get("title", "")
                        chunk_content = (chunk.get("content") or "")[:300]
                        chunk_relevance = chunk.get("relevance", 0)
                        kb_summary_parts.append(
                            f"  [{i+1}] {chunk_title} (相关度:{chunk_relevance:.2f})\n     {chunk_content}"
                        )
                    summary = "\n".join(kb_summary_parts)
                elif content:
                    summary = f"[{tool_name}]: {str(content)[:500]}"
                else:
                    summary = f"[{tool_name}]: 检索完成，但未找到相关内容"
                tool_summaries.append(summary)
            elif tool_name == "weather_tool" and result.get("success"):
                # 天气查询：qweather 走 current/forecasts/aqi；web-search 降级走 answer+来源
                w_parts = [f"城市：{result.get('city', '未知')}"]
                answer = str(result.get("answer") or "").strip()
                if answer:
                    w_parts.append(f"天气实况（联网搜索）：{answer[:1200]}")
                    for i, sr in enumerate((result.get("search_results") or [])[:5]):
                        w_parts.append(f"  [{i+1}] {sr.get('title') or sr.get('url', '')}")
                else:
                    current = result.get("current") or {}
                    if current:
                        w_parts.append(
                            f"当前天气：{current.get('weather', '未知')}，气温 {current.get('temp', '?')}℃"
                            f"（体感 {current.get('feels_like', '?')}℃），湿度 {current.get('humidity', '?')}%，"
                            f"{current.get('wind_direction', '')}风 {current.get('wind_scale', '')} 级"
                        )
                    forecasts = result.get("forecasts") or []
                    if forecasts:
                        w_parts.append("未来预报：")
                        for d in forecasts[:7]:
                            w_parts.append(
                                f"  {d.get('date', '')}: {d.get('text_day', '')}转{d.get('text_night', '')}，"
                                f"{d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃，降水 {d.get('precip', '?')}mm"
                            )
                    aqi = result.get("aqi")
                    if isinstance(aqi, dict) and aqi:
                        w_parts.append(f"空气质量：AQI {aqi.get('aqi', '?')}（{aqi.get('category', '?')}），PM2.5 {aqi.get('pm2p5', '?')}")
                    if result.get("source") == "mock":
                        w_parts.append("（注意：以上为模拟数据，真实天气服务不可用）")
                tool_summaries.append(f"[{tool_name}]:\n" + "\n".join(w_parts))
            elif tool_name == "web_search_tool" and result.get("success"):
                # 联网搜索：answer 已含引用标记的总结，附上来源列表供标注出处
                ws_parts = [f"搜索问题：{result.get('query', '')}"]
                answer = str(result.get("answer") or "").strip()
                if answer:
                    ws_parts.append(f"搜索总结：{answer[:1500]}")
                for i, sr in enumerate((result.get("search_results") or [])[:5]):
                    ws_parts.append(f"  [{i+1}] {sr.get('title', '')} ({sr.get('url', '')})")
                tool_summaries.append(f"[{tool_name}]:\n" + "\n".join(ws_parts))
            elif content or (isinstance(data, list) and data):
                summary = f"[{tool_name}]: {str(content)[:500]}"
                # 对数据库查询，把 data 的 JSON 摘要也加进来
                if tool_name in ("postgresql_tool", "mcp_postgres_tool") and result.get("success"):
                    if data is not None:
                        data_text = json.dumps(data[:10], ensure_ascii=False, default=str)
                        summary += f"\n数据结果（前 10 条）: {data_text[:800]}"
                tool_summaries.append(summary)

        # 当地图操作意图但无工具被调用（execution_plan 中 tool 为空），不应让 LLM 凭空回答
        if not tool_summaries and intent_result.primary_intent in (
            IntentType.MAP_DISPLAY, IntentType.LOCATION_SEARCH, IntentType.COORDINATE_MARKER
        ):
            all_steps = intent_result.execution_plan
            if all_steps:
                logger.warning(
                    f"[extract_node] Map intent '{intent_result.primary_intent}' "
                    f"has {len(all_steps)} steps but no tool results. "
                    f"Possible cause: execution_plan steps have tool=None."
                )
                # 用实际生成的地图命令构造回复，避免所有地图操作都返回同一句模板
                # （前端按内容去重，固定模板会被当成重复消息丢弃，导致用户看不到任何回复）
                if map_commands:
                    loaded = "、".join(
                        f"「{cmd.get('name') or cmd.get('type', '图层')}」" for cmd in map_commands
                    )
                    state["response"] = f"地图指令已执行，已加载图层 {loaded}。"
                else:
                    state["response"] = "地图操作指令已解析，正在执行中。"
            else:
                state["response"] = "已接收到地图操作请求，但未生成执行步骤，请重新描述需求。"
            state["map_commands"] = map_commands
            state["cesium_commands"] = cesium_commands
            state["charts"] = charts
            state["report_url"] = report_url
            state["tool_summaries"] = tool_summaries
            return state

        # ── 知识库回退检测 + 补偿检索（响应阶段兜底）──
        has_kb_results = any(
            item.get("tool_name") == "knowledge_base_tool"
            and isinstance(item.get("result"), dict)
            and item["result"].get("success")
            and item["result"].get("count", 0) > 0
            for item in tool_results
        )
        db_tool_names = ("postgresql_tool", "mcp_postgres_tool")
        has_db = any(item.get("tool_name") in db_tool_names for item in tool_results)

        if has_db and not has_kb_results:
            need_fallback = False
            for item in tool_results:
                if item.get("tool_name") not in db_tool_names:
                    continue
                r = item.get("result", {})
                if isinstance(r, dict):
                    data = r.get("data")
                    if data is None or (isinstance(data, list) and len(data) == 0):
                        need_fallback = True
                        break
                    if data is not None and (isinstance(data, list) and data):
                        if self._is_empty_aggregate(data):
                            need_fallback = True
                            break
                        for row in data:
                            if isinstance(row, dict):
                                for v in row.values():
                                    if isinstance(v, (int, float)) and v > 100:
                                        logger.warning(
                                            f"[extract_node] Suspicious large count {v}, "
                                            f"likely COUNT(*) instead of COUNT(DISTINCT)"
                                        )
                                        need_fallback = True
                                        break
                            if need_fallback:
                                break
            if need_fallback:
                logger.warning("[extract_node] DB result unreliable — running sync fallback")
                kb_summary = self._sync_kb_search(state["user_message"])
                if kb_summary:
                    tool_summaries = [
                        s for s in tool_summaries
                        if not s.startswith("[postgresql_tool]") and not s.startswith("[mcp_postgres_tool]")
                    ]
                    tool_summaries.insert(0, kb_summary)

        elif has_db and has_kb_results:
            tool_summaries = [
                s for s in tool_summaries
                if not s.startswith("[postgresql_tool]") and not s.startswith("[mcp_postgres_tool]")
            ]
            tool_summaries.insert(0, (
                "【重要】数据库中没有该地区的砂场数据（数据库仅覆盖固始县），"
                "请完全依据以下知识库检索结果回答用户问题，不要提及数据库结果。"
            ))

        state["map_commands"] = map_commands
        state["cesium_commands"] = cesium_commands
        state["charts"] = charts
        state["report_url"] = report_url
        state["tool_summaries"] = tool_summaries
        return state

    def _summarize_node(self, state: AgentState) -> AgentState:
        """节点3b：基于 extract_node 提取的结果生成自然语言回复。

        优先检查工具是否已产出高质量摘要（跳过 LLM），否则调用 LLM 汇总。
        若 extract_node 提前写入了 response（如地图意图无工具场景），则直接透传。
        """
        # 如果 extract_node 已经直接写入了 response（地图意图无工具等场景），直接返回
        if state.get("response"):
            return state

        intent_result: IntentResult = state["intent_result"]
        tool_results: List[Dict] = state.get("tool_results", [])
        tool_summaries: List[str] = state.get("tool_summaries", [])

        # 优化：工具已返回高质量摘要时，直接透传，跳过 LLM 汇总
        rich_content = self._try_extract_rich_response(tool_results)
        if rich_content:
            logger.info("[summarize_node] Skipping LLM response generation — using rich tool content directly")
            response_text = rich_content
        else:
            response_text = self._generate_response(
                user_message=state["user_message"],
                intent_result=intent_result,
                tool_summaries=tool_summaries,
            )

        # 来源标注：回答引用了知识库时，确保末尾带有来源文档名（LLM 忘标则兜底追加）
        response_text = self._append_kb_sources(response_text, tool_results)
        # 联网搜索来源标注兜底
        response_text = self._append_web_sources(response_text, tool_results)

        state["response"] = response_text
        logger.info(
            f"[summarize_node] Final state: report_url={state.get('report_url')}, "
            f"charts={len(state.get('charts', []))}, "
            f"map_cmds={len(state.get('map_commands', []))}, "
            f"response_len={len(response_text)}"
        )
        return state

    def _append_kb_sources(self, response_text: str, tool_results: List[Dict]) -> str:
        """知识库回答的来源标注兜底：提取 KB 结果中的文档名，去重后追加到回复末尾。

        LLM 已在正文中标注（出现任一文档名）则不重复追加；最多列 3 个，超出折叠计数。
        """
        titles: List[str] = []
        for item in tool_results:
            if item.get("tool_name") != "knowledge_base_tool":
                continue
            result = item.get("result", {})
            if not isinstance(result, dict) or not result.get("success"):
                continue
            data = result.get("data")
            if not isinstance(data, list):
                continue
            for chunk in data:
                if not isinstance(chunk, dict):
                    continue
                t = str(chunk.get("title") or "").strip()
                # 清洗内部文件名：去掉 section_5_0007_ 前缀与 .txt 等扩展名
                t = re.sub(r"^section_\d+_", "", t)
                t = re.sub(r"\.(txt|md|docx?|pdf)$", "", t, flags=re.IGNORECASE)
                if t and t != "未命名文档" and t not in titles:
                    titles.append(t)
        if not titles:
            return response_text
        if any(t in response_text for t in titles):
            return response_text
        shown = "、".join(f"《{t}》" for t in titles[:3])
        if len(titles) > 3:
            shown += f" 等 {len(titles)} 篇文档"
        return f"{response_text}\n\n📎 来源：{shown}"

    def _append_web_sources(self, response_text: str, tool_results: List[Dict]) -> str:
        """联网搜索回答的来源标注兜底：提取搜索结果标题+域名，去重后追加到回复末尾。

        LLM 已在正文中标注任一来源标题/域名则不重复追加；最多列 3 条。
        """
        sources: List[str] = []
        for item in tool_results:
            is_web = item.get("tool_name") == "web_search_tool" or (
                item.get("tool_name") == "weather_tool"
                and isinstance(item.get("result"), dict)
                and item["result"].get("provider") == "web-search"
            )
            if not is_web:
                continue
            result = item.get("result", {})
            if not isinstance(result, dict) or not result.get("success"):
                continue
            for sr in result.get("search_results") or []:
                if not isinstance(sr, dict):
                    continue
                title = str(sr.get("title") or "").strip()
                url = str(sr.get("url") or "").strip()
                if not title and not url:
                    continue
                domain = url.split("//", 1)[-1].split("/", 1)[0] if url else ""
                if title and domain:
                    label = f"{title}（{domain}）"
                else:
                    label = title or domain
                if label and label not in sources:
                    sources.append(label)
        if not sources:
            return response_text
        # 正文已标注任一来源（标题或域名出现）则不重复追加
        plain = [s.split("（")[0] for s in sources] + [
            s.split("（")[1].rstrip("）") for s in sources if "（" in s
        ]
        if any(p in response_text for p in plain if p):
            return response_text
        shown = "；".join(sources[:3])
        if len(sources) > 3:
            shown += f" 等 {len(sources)} 条来源"
        return f"{response_text}\n\n📎 网络来源：{shown}"

    def _try_extract_rich_response(self, tool_results: List[Dict]) -> Optional[str]:
        """如果工具已产出高质量摘要，返回可直接用作 response 的文本，否则返回 None。"""
        parts: List[str] = []
        for item in tool_results:
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            # data_visualizer_tool 返回的 content 通常是完整的 Markdown 摘要（含表格+分析）
            if tool_name == "data_visualizer_tool" and result.get("success"):
                content = result.get("content") or ""
                if len(content) > 50:
                    parts.append(content)
            # report_generator_tool 成功时简单告知
            if tool_name == "report_generator_tool" and result.get("success"):
                url = result.get("report_url") or result.get("download_url") or ""
                msg = result.get("message") or "报告已生成"
                parts.append(f"{msg}。" + (f"下载地址：{url}" if url else ""))
            # spatial_processing_tool 成功时直接使用其 message
            if tool_name == "spatial_processing_tool" and result.get("success"):
                msg = result.get("message") or ""
                if msg:
                    parts.append(msg)
            # qgis_mcp_tool 缓冲区工作流成功时直接使用其 message
            if tool_name == "qgis_mcp_tool" and result.get("success"):
                msg = result.get("message") or ""
                if msg:
                    parts.append(msg)
        # 当 data_visualizer_tool 已提供完整分析时，postgresql_tool 的结果也视为已覆盖
        # （因为 viz tool 的 SQL 查询结果已包含数据和分析）
        covered_tools = {"data_visualizer_tool", "report_generator_tool", "spatial_processing_tool", "qgis_mcp_tool"}
        has_viz = any(
            item.get("tool_name") == "data_visualizer_tool"
            and isinstance(item.get("result"), dict)
            and item["result"].get("success")
            for item in tool_results
        )
        if has_viz:
            covered_tools.add("postgresql_tool")
        uncovered = [
            item for item in tool_results
            if item.get("tool_name") not in covered_tools
            and isinstance(item.get("result"), dict)
            and item["result"].get("success") is not False
            and (item["result"].get("content") or item["result"].get("message"))
        ]
        if parts and not uncovered:
            return "\n\n".join(parts)
        return None

    def _generate_response(
        self,
        user_message: str,
        intent_result: IntentResult,
        tool_summaries: List[str],
    ) -> str:
        """调用 LLM 将工具结果汇总为自然语言回答。

        即使无工具结果，也会调用 LLM，让其基于系统上下文回答常识性问题。
        """
        system_prompt = self._get_response_system_prompt(intent_result.primary_intent)

        # 注入系统上下文（当前时间等），确保 LLM 能回答时间相关问题
        system_context = AgentHarness.get_system_context()
        system_prompt = system_context + "\n\n" + system_prompt

        if tool_summaries:
            # 阶段4：上下文预算——工具结果按优先级确定性裁剪（错误 > DB > 知识库 > 其余）
            trimmed = self.context_manager.trim_tool_summaries(tool_summaries)
            tools_context = "\n".join(trimmed)
            user_content = f"""用户问题：{user_message}

工具执行结果：
{tools_context}

请根据上述工具执行结果，用简洁专业的语言回答用户的问题。"""
        else:
            user_content = f"""用户问题：{user_message}

请根据上述系统信息，用简洁专业的语言回答用户的问题。"""

        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_content),
            ]
            ai_msg = self._llm.invoke(messages)
            return ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)
        except Exception as e:
            logger.error(f"[summarize_node] LLM generate response error: {e}")
            return "\n".join(tool_summaries) or "命令已执行。"

    def _get_response_system_prompt(self, intent: IntentType) -> str:
        """获取当前意图对应的 response 提示词（委托给 Harness）。"""
        # 尝试通过 harness 获取专用 prompt
        try:
            # 构造一个轻量 IntentResult 用于 dispatch
            intent_result = IntentResult(
                primary_intent=intent,
                confidence=1.0,
                entities=[],
                task_context="",
                execution_plan=[],
            )
            return self.harness.build_response_prompt_for(intent_result)
        except Exception:
            pass

        # 兜底：保留原有硬编码映射
        prompts = {
            IntentType.MAP_DISPLAY: "你是地图分析助手，正在汇总地图操作结果，回答要简洁明了。",
            IntentType.DATA_QUERY: (
                "你是数据分析助手，正在汇总数据库查询结果。"
                "数据表为 'ceshen'，业务规则：超深度开采 = AVG(Control_Elevation - Measured_Depth) > 2。"
                "回答要包含具体数据。"
                "重要：当工具执行结果中包含'数据结果（前 10 条）'时，请优先使用该 JSON 数据中的具体数值来回答，"
                "不要仅依据'返回 N 条记录'这一行数描述。聚合查询（如 COUNT）只返回 1 行是正常现象，"
                "实际统计值在该行的字段中。"
            ),
            IntentType.KNOWLEDGE_SEARCH: (
                "你是知识库检索助手，请将检索结果整理成清晰易读的回答。"
                "回答内容必须基于检索结果，禁止编造。"
                "关键结论后需用括号标注来源文档名，如（来源：《2023年度罗山县采砂区监测评估意见》）。"
                "若检索结果不足以回答，如实告知并建议用户换个问法或补充文档。"
            ),
            IntentType.DATA_VISUALIZATION: "你是数据可视化助手，请简要说明图表内容和数据洞察。",
            IntentType.REPORT_GENERATION: "你是报告生成助手。如果报告已成功生成，请告知用户并说明主要内容。如果因数据不足导致报告未生成，请简洁告知用户原因和建议（如补充数据后重试），不要输出空数据的分析描述。",
            IntentType.WEATHER_QUERY: "你是天气查询助手，请将天气信息整理成简洁易读的格式。",
            IntentType.LOCATION_SEARCH: "你是位置搜索助手，请告知位置搜索结果。",
        }
        return prompts.get(intent, "你是智能助手，请根据工具执行结果回答用户问题，保持简洁专业。")

    # ------------------------------------------------------------------
    # 意图路由
    # ------------------------------------------------------------------

    def _route_after_intent(self, state: AgentState) -> str:
        intent_result: IntentResult = state.get("intent_result")
        if intent_result is None:
            return END

        if intent_result.confidence < 0.3:
            state["response"] = "抱歉，我无法理解您的意图。请尝试更详细地描述您的需求。"
            state["map_commands"] = []
            state["cesium_commands"] = []
            state["charts"] = []
            state["tool_results"] = []
            return END

        if intent_result.requires_confirmation:
            state["response"] = self._generate_confirmation_message(intent_result)
            state["map_commands"] = []
            state["cesium_commands"] = []
            state["charts"] = []
            state["tool_results"] = []
            return "confirmation_node"

        required_tools = self.intent_agent.get_required_tools(intent_result)
        # ── 调试：日志记录路由决策关键信息 ──
        steps_tools = [(s.step_id, s.tool) for s in (intent_result.execution_plan or [])]
        logger.info(
            f"[route_after_intent] intent={intent_result.primary_intent}, "
            f"confidence={intent_result.confidence}, "
            f"requires_confirmation={intent_result.requires_confirmation}, "
            f"plan_steps_tools={steps_tools}, "
            f"required_tools={required_tools}"
        )
        if not required_tools:
            # 无工具需求，直接进入 summarize_node 生成回答
            state["tool_results"] = []
            return "summarize_node"

        return "tool_node"

    def _generate_confirmation_message(self, intent_result: IntentResult) -> str:
        context = intent_result.task_context
        plan = "\n".join(
            f"{i + 1}. {step.action} (使用 {step.tool})"
            for i, step in enumerate(intent_result.execution_plan)
        )
        return f"""我理解您的需求是：{context}

我的执行计划是：
{plan}

请确认是否按此计划执行？"""

    def _confirmation_node(self, state: AgentState) -> AgentState:
        """确认节点：直接透传 _route_after_intent 中写入的确认文案，不经 LLM 二次生成。

        设计目的：解决原实现中确认文案被 summarize_node 覆盖的问题。
        确认路径不走 summarize_node，直接从本节点走向 END。
        """
        # response 已在 _route_after_intent 中写入，此处无需修改
        return state

    def _create_initial_state(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> AgentState:
        return {
            "user_message": user_message,
            "chat_history": chat_history or [],
            "intent_result": None,
            "tool_results": [],
            "tool_summaries": [],
            "response": "",
            "map_commands": [],
            "cesium_commands": [],
            "charts": [],
            "report_url": None,
            "error": None,
        }

    def _serialize_intent_info(self, intent_result: Optional[IntentResult]) -> Optional[Dict[str, Any]]:
        if not intent_result:
            return None

        primary_intent = intent_result.primary_intent
        if hasattr(primary_intent, "value"):
            primary_intent = primary_intent.value

        return {
            "primary_intent": primary_intent,
            "confidence": intent_result.confidence,
            "task_context": intent_result.task_context,
            "entities": intent_result.entities,
            "execution_plan": [
                {
                    "step_id": step.step_id,
                    "action": step.action,
                    "tool": step.tool,
                    "reasoning": step.reasoning,
                    "expected_output": step.expected_output,
                }
                for step in intent_result.execution_plan
            ],
            "requires_confirmation": intent_result.requires_confirmation,
            "suggestions": intent_result.suggestions,
        }

    def _humanize_tool_name(self, tool_name: str) -> str:
        tool_names = {
            "postgresql_tool": "数据库查询工具",
            "mcp_postgres_tool": "数据库查询工具",
            "data_visualizer_tool": "数据可视化工具",
            "report_generator_tool": "报告生成工具",
            "knowledge_base_tool": "知识库检索工具",
            "map_tool": "二维地图工具",
            "location_search": "位置搜索工具",
            "coordinate_marker": "坐标标注工具",
            "cesium_tool": "三维地图工具",
            "weather_tool": "天气查询工具",
            "web_search_tool": "联网搜索",
            "spatial_reference_tool": "空间参考数据工具",
        }
        return tool_names.get(tool_name, tool_name)

    def _truncate_text(self, value: Any, limit: int = 120) -> str:
        text = str(value or "").strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _preview_tool_params(self, tool_name: str, params: Optional[Dict[str, Any]]) -> List[str]:
        if not isinstance(params, dict) or not params:
            return []

        details: List[str] = []
        if tool_name in {"postgresql_tool", "mcp_postgres_tool"}:
            operation = params.get("operation")
            sql = params.get("sql")
            if operation:
                details.append(f"数据库操作：{operation}")
            if sql:
                details.append(f"SQL：{self._truncate_text(sql, 220)}")
            sql_params = params.get("params") or []
            if sql_params:
                details.append(f"SQL 参数数量：{len(sql_params)}")
            return details

        if tool_name == "data_visualizer_tool":
            demand = params.get("demand")
            if demand:
                details.append(f"制图需求：{self._truncate_text(demand, 160)}")
            return details

        if tool_name == "report_generator_tool":
            template_name = params.get("template_name")
            if template_name:
                details.append(f"模板：{template_name}")
            variables = params.get("variables") or {}
            if variables:
                details.append(f"报告变量：{', '.join(list(variables.keys())[:6])}")
            if params.get("map_image_path"):
                details.append("已携带地图截图路径")
            return details

        if tool_name == "map_tool":
            action = params.get("action")
            if action:
                details.append(f"地图动作：{action}")
            if params.get("table_name"):
                details.append(f"数据表：{params.get('table_name')}")
            if params.get("filter"):
                details.append(f"过滤条件：{self._truncate_text(params.get('filter'), 160)}")
            return details

        if tool_name == "cesium_tool":
            action = params.get("action") or params.get("type")
            if action:
                details.append(f"三维动作：{action}")
            if params.get("lat") is not None and params.get("lng") is not None:
                details.append(f"目标坐标：{params.get('lat')}, {params.get('lng')}")
            return details

        if tool_name in {"location_search", "knowledge_base_tool", "weather_tool", "coordinate_marker"}:
            for key in ["query", "keyword", "location", "address", "lat", "lng"]:
                if key in params and params.get(key) not in [None, ""]:
                    details.append(f"{key}：{self._truncate_text(params.get(key), 120)}")
            return details[:4]

        return [f"参数：{self._truncate_text(params, 180)}"]

    def _format_plan_step(self, step: Any) -> Dict[str, Any]:
        tool_label = self._humanize_tool_name(step.tool) if getattr(step, "tool", None) else "无需外部工具"
        details = [f"工具：{tool_label}"]
        if getattr(step, "reasoning", None):
            details.append(f"原因：{self._truncate_text(step.reasoning, 160)}")
        if getattr(step, "expected_output", None):
            details.append(f"预期输出：{self._truncate_text(step.expected_output, 160)}")
        details.extend(self._preview_tool_params(getattr(step, "tool", ""), getattr(step, "params", None)))
        return {
            "step_id": step.step_id,
            "action": step.action,
            "tool_name": step.tool,
            "tool_label": tool_label,
            "details": details,
        }

    def _extract_tool_result_details(self, tool_name: str, result: Dict[str, Any]) -> List[str]:
        if not isinstance(result, dict):
            return ["工具返回了非结构化结果。"]

        details: List[str] = []
        if result.get("success") is False:
            error = result.get("error") or result.get("message") or "未知错误"
            return [f"执行失败：{self._truncate_text(error, 180)}"]

        data = result.get("data")
        if isinstance(data, list):
            details.append(f"返回记录数：{len(data)}")
            if data and isinstance(data[0], dict):
                fields = ", ".join(list(data[0].keys())[:6])
                if fields:
                    details.append(f"字段预览：{fields}")
        elif isinstance(data, dict):
            if "rows_affected" in data:
                details.append(f"影响行数：{data['rows_affected']}")
            elif data:
                details.append(f"结果字段：{', '.join(list(data.keys())[:6])}")

        if tool_name == "data_visualizer_tool":
            chart_type = result.get("chart_type")
            chart_title = ((result.get("config") or {}).get("title") or {}).get("text")
            if chart_type:
                details.append(f"图表类型：{chart_type}")
            if chart_title:
                details.append(f"图表标题：{chart_title}")
            config = result.get("config") or {}
            series = config.get("series") or []
            if isinstance(series, list) and series:
                first_series = series[0] or {}
                series_data = first_series.get("data") or []
                details.append(f"图表数据点数：{len(series_data)}")

        if tool_name in ("report_generator_tool", "caisha_report_tool"):
            report_url = result.get("report_url") or result.get("download_url")
            if report_url:
                details.append(f"报告地址：{report_url}")

        if result.get("map_command"):
            map_command = result["map_command"]
            details.append(f"地图动作：{map_command.get('type', 'unknown')}")
            if map_command.get("name"):
                details.append(f"图层名称：{map_command.get('name')}")

        if result.get("cesium_command"):
            cesium_command = result["cesium_command"]
            action = cesium_command.get("action") or cesium_command.get("type") or "unknown"
            details.append(f"三维动作：{action}")

        content = result.get("message") or result.get("content")
        if content:
            details.append(f"结果摘要：{self._truncate_text(content, 180)}")

        return details[:6] or [f"{self._humanize_tool_name(tool_name)}已完成。"]

    def _summarize_tool_result(self, tool_name: str, result: Dict[str, Any]) -> str:
        readable_tool_name = self._humanize_tool_name(tool_name)

        if not isinstance(result, dict):
            return f"{readable_tool_name}已返回结果。"

        if result.get("success") is False:
            error = result.get("error") or result.get("message") or "未知错误"
            return f"{readable_tool_name}执行失败：{error}"

        if tool_name == "data_visualizer_tool":
            chart_type = result.get("chart_type") or "图表"
            return f"{readable_tool_name}已完成，生成结果类型：{chart_type}。"

        if tool_name == "report_generator_tool":
            return f"{readable_tool_name}已完成，报告文件已生成。"

        if result.get("map_command"):
            cmd_type = result["map_command"].get("type", "地图命令")
            return f"{readable_tool_name}已完成，生成地图动作：{cmd_type}。"

        if result.get("cesium_command"):
            cmd_type = result["cesium_command"].get("action") or result["cesium_command"].get("type", "三维命令")
            return f"{readable_tool_name}已完成，生成三维动作：{cmd_type}。"

        content = result.get("content") or result.get("message")
        if content:
            content = str(content).strip().replace("\n", " ")
            return f"{readable_tool_name}已完成：{content[:80]}"

        return f"{readable_tool_name}已完成。"

    # 天气关键词停用词：从用户消息中剥离后剩余部分视为城市名
    _WEATHER_CITY_STRIP_RE = re.compile(
        r"查询|查一下|查下|看看|帮我|麻烦|请问|今天|明天|后天|现在|当前|这几天|未来三天|未来七天"
        r"|的|天气|气温|温度|降雨|下雨|下雪|风力|风速|湿度|空气质量|AQI|雾霾"
        r"|怎么样|如何|预报|几度|带伞|紫外线|情况|冷吗|热吗|有雨吗"
    )

    @classmethod
    def _extract_city_from_message(cls, user_message: str) -> str:
        """从用户消息中兜底抽取城市名：剥离天气相关词后剩余中文片段。"""
        if not user_message:
            return ""
        candidate = cls._WEATHER_CITY_STRIP_RE.sub("", user_message)
        candidate = re.sub(r"[？?！!。，,．\s_\-—~]|吗$|呢$", "", candidate)
        return candidate.strip()

    def _normalize_tool_params(self, tool_name: str, params: Dict[str, Any], user_message: str, step: Any) -> Dict[str, Any]:
        """根据工具类型规范化参数，确保必需参数存在且格式正确。"""
        if not isinstance(params, dict):
            return {}

        normalized = dict(params)

        # postgresql_tool / mcp_postgres_tool：确保包含 operation
        if tool_name in {"postgresql_tool", "mcp_postgres_tool"}:
            if "operation" not in normalized:
                sql = normalized.get("sql", "")
                if sql:
                    normalized["operation"] = "query"
            if "params" not in normalized:
                normalized["params"] = []
            return normalized

        # map_tool：补参 resume 只带 table_name 时，默认视为加载矢量图层
        if tool_name == "map_tool":
            if "table_name" in normalized and "action" not in normalized:
                normalized["action"] = "load_vector_layer"
            return normalized

        # data_visualizer_tool：确保包含 demand
        if tool_name == "data_visualizer_tool":
            if "demand" not in normalized or not normalized["demand"]:
                normalized["demand"] = user_message
            return normalized

        # weather_tool：city 缺失时从用户消息兜底抽取（LLM 漏填 params 时避免反问死循环）
        if tool_name == "weather_tool":
            if not normalized.get("city"):
                extracted = self._extract_city_from_message(user_message)
                if extracted:
                    normalized["city"] = extracted
                    logger.info(f"[tool_node] weather_tool city fallback: '{extracted}'")
            return normalized

        # report_generator_tool：确保 variables 是字典
        if tool_name == "report_generator_tool":
            if "variables" not in normalized:
                normalized["variables"] = {}
            if not isinstance(normalized["variables"], dict):
                normalized["variables"] = {}
            return normalized

        # cesium_tool：标准化 action/type 字段
        if tool_name == "cesium_tool":
            if "action" not in normalized and "type" not in normalized:
                normalized["action"] = "unknown"
            return normalized

        return normalized

    def _check_data_sufficiency(self, prior_results: List[tuple]) -> tuple:
        """检查前置工具结果中是否有足够的有效数据来生成报告/图表。"""
        from tools.report_builder import check_data_sufficiency
        return check_data_sufficiency(prior_results)

    def _build_report_variables(
        self,
        prior_results: List[tuple],
        user_message: str,
        intent_result,
    ) -> Dict[str, Any]:
        """委托给 tools.report_builder 构建报告变量（含数据预处理 + LLM 生成）。"""
        from tools.report_builder import build_report_variables
        task_context = getattr(intent_result, "task_context", user_message) if intent_result else user_message
        return build_report_variables(
            prior_results=prior_results,
            user_message=user_message,
            task_context=task_context,
            llm=self._llm,
        )

    def _build_stream_result(self, final_state: AgentState) -> Dict[str, Any]:
        intent_result = final_state.get("intent_result")
        low_confidence = intent_result is not None and intent_result.confidence < 0.3
        report_url = final_state.get("report_url")
        logger.info(f"[_build_stream_result] report_url in state: {report_url}, state keys: {list(final_state.keys())}")
        return {
            "success": not low_confidence,
            "response": final_state.get("response", "命令已执行。"),
            "messages": [],
            "map_commands": final_state.get("map_commands", []),
            "cesium_commands": final_state.get("cesium_commands", []),
            "charts": final_state.get("charts", []),
            "report_url": final_state.get("report_url"),
            "intent_info": self._serialize_intent_info(intent_result),
            "requires_confirmation": intent_result.requires_confirmation if intent_result else False,
        }

    async def execute_stream(self, user_message: str, chat_history: Optional[List[Dict]] = None, thread_id: str = "default") -> AsyncGenerator[Dict[str, Any], None]:
        self._prune_checkpoints(thread_id)
        state = self._create_initial_state(user_message, chat_history)

        try:
            yield {
                "type": "status",
                "stage": "start",
                "title": "已接收请求",
                "message": "开始分析用户问题与上下文。",
                "details": [
                    f"用户输入：{self._truncate_text(user_message, 140)}",
                    f"历史消息数：{len(chat_history or [])}",
                ],
            }
            yield {
                "type": "status",
                "stage": "intent_start",
                "title": "意图分析中",
                "message": "正在调用意图分析模型，识别任务类型并规划步骤。",
                "details": ["此阶段会判断是地图、查询、制图还是报告任务。"],
            }

            state = self._intent_node(state)
            intent_result = state.get("intent_result")
            required_tools = self.intent_agent.get_required_tools(intent_result) if intent_result else []

            if intent_result:
                primary_intent = intent_result.primary_intent.value if hasattr(intent_result.primary_intent, "value") else str(intent_result.primary_intent)
                plan_steps = [self._format_plan_step(step) for step in intent_result.execution_plan]
                summary = f"已识别主要意图：{primary_intent}，置信度 {intent_result.confidence:.2f}。"
                if required_tools:
                    summary += f" 计划调用 {len(required_tools)} 个工具。"
                yield {
                    "type": "intent",
                    "stage": "intent",
                    "title": f"意图识别完成：{primary_intent}",
                    "message": summary,
                    "details": [
                        f"任务理解：{self._truncate_text(intent_result.task_context, 160)}",
                        f"识别实体：{', '.join(intent_result.entities[:6]) if intent_result.entities else '未识别到显式实体'}",
                        f"是否需要确认：{'是' if intent_result.requires_confirmation else '否'}",
                    ],
                    "plan_steps": plan_steps,
                    "intent_info": self._serialize_intent_info(intent_result),
                }
                if plan_steps:
                    yield {
                        "type": "plan",
                        "stage": "plan",
                        "title": f"执行计划已生成（{len(plan_steps)} 步）",
                        "message": "下面开始按计划执行。",
                        "details": [f"步骤 {step['step_id']}：{step['action']}" for step in plan_steps],
                        "plan_steps": plan_steps,
                    }

            route = self._route_after_intent(state)
            if route == END:
                yield {
                    "type": "status",
                    "stage": "done",
                    "title": "流程结束",
                    "message": state.get("response") or "处理结束。",
                    "details": ["本轮没有进入工具执行阶段。"],
                }
                yield {
                    "type": "final",
                    "result": self._build_stream_result(state),
                }
                return

            if route == "confirmation_node":
                state = self._confirmation_node(state)
                yield {
                    "type": "status",
                    "stage": "confirmation",
                    "title": "等待用户确认",
                    "message": "任务存在执行确认需求，正在整理确认说明。",
                    "details": [f"任务摘要：{self._truncate_text(intent_result.task_context, 160)}"],
                }
            elif route == "summarize_node":
                yield {
                    "type": "status",
                    "stage": "response",
                    "title": "直接生成回复",
                    "message": "本轮无需调用外部工具，直接整理答案。",
                    "details": ["原因：执行计划中没有必须调用的外部工具。"],
                }

            if route == "tool_node" and intent_result:
                steps = [step for step in intent_result.execution_plan if step.tool]
                total_steps = len(steps)
                step_lookup = {step.step_id: step for step in steps}
                yield {
                    "type": "status",
                    "stage": "tool_plan",
                    "title": "进入工具执行阶段",
                    "message": f"已生成执行计划，开始执行 {total_steps} 个工具步骤。",
                    "details": [f"将按计划调用：{', '.join(self._humanize_tool_name(step.tool) for step in steps)}"],
                    "total_steps": total_steps,
                }

                async def _invoke_stream_step(step, extra_params: Dict = None):
                    # ── qgis_mcp_tool 步骤走通用工作流引擎 ──
                    if step.tool == "qgis_mcp_tool":
                        try:
                            result = await self._execute_qgis_workflow(state, step)
                            return step.step_id, {"tool_name": "qgis_mcp_tool", "result": result}
                        except Exception as e:
                            logger.error(f"[execute_stream] QGIS workflow failed: {e}", exc_info=True)
                            return step.step_id, {
                                "tool_name": "qgis_mcp_tool",
                                "result": {"success": False, "error": str(e)}
                            }

                    adapter = self._get_tool_adapter(step.tool)
                    if adapter is None:
                        return step.step_id, {
                            "tool_name": step.tool,
                            "result": {"success": False, "error": f"工具 {step.tool} 不可用"},
                        }
                    params = self._normalize_tool_params(step.tool, step.params or {}, state["user_message"], step)
                    if extra_params:
                        params.update(extra_params)
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, adapter.invoke, params)
                    return step.step_id, result

                # 分离报告工具（需要等前置工具结果填充 variables）
                pre_steps = [s for s in steps if s.tool != "report_generator_tool"]
                report_steps = [s for s in steps if s.tool == "report_generator_tool"]

                tasks = []
                for step in pre_steps:
                    step_info = self._format_plan_step(step)
                    yield {
                        "type": "tool_start",
                        "stage": "tool_start",
                        "tool_name": step.tool,
                        "tool_label": step_info["tool_label"],
                        "step_id": step.step_id,
                        "title": f"步骤 {step.step_id} 开始：{step.action}",
                        "message": f"开始执行步骤 {step.step_id}：{step.action}",
                        "details": step_info["details"],
                    }
                    tasks.append(asyncio.create_task(_invoke_stream_step(step)))

                ordered_results = []
                completed_steps = 0
                for finished_task in asyncio.as_completed(tasks):
                    step_id, result = await finished_task
                    ordered_results.append((step_id, result))
                    completed_steps += 1
                    tool_name = result.get("tool_name", "unknown_tool")
                    step = step_lookup.get(step_id)
                    result_data = result.get("result", {})
                    yield {
                        "type": "tool_result",
                        "stage": "tool_result",
                        "tool_name": tool_name,
                        "tool_label": self._humanize_tool_name(tool_name),
                        "step_id": step_id,
                        "completed_steps": completed_steps,
                        "total_steps": total_steps,
                        "title": f"步骤 {step_id} 完成：{step.action if step else tool_name}",
                        "message": self._summarize_tool_result(tool_name, result_data),
                        "details": self._extract_tool_result_details(tool_name, result_data),
                    }

                # ── 数据库空结果降级：自动回退到知识库检索 ──
                pre_results_only = [r for _, r in ordered_results]

                async def _invoke_step_only(step, extra_params=None):
                    _, result = await _invoke_stream_step(step, extra_params)
                    return result

                kb_fallback = await self._fallback_knowledge_search(
                    pre_results_only, state["user_message"], _invoke_step_only
                )
                # 如果回退产生了新结果，追加到 ordered_results 并通知前端
                if len(kb_fallback) > len(pre_results_only):
                    new_items = kb_fallback[len(pre_results_only):]
                    for kb_item in new_items:
                        completed_steps += 1
                        # 用虚拟 step_id 标记为知识库回退结果
                        fake_step_id = 9999
                        ordered_results.append((fake_step_id, kb_item))
                        kb_result_data = kb_item.get("result", {})
                        yield {
                            "type": "tool_result",
                            "stage": "tool_result",
                            "tool_name": "knowledge_base_tool",
                            "tool_label": self._humanize_tool_name("knowledge_base_tool"),
                            "step_id": fake_step_id,
                            "completed_steps": completed_steps,
                            "total_steps": total_steps,
                            "title": "知识库补充检索完成（数据库无结果，自动回退）",
                            "message": self._summarize_tool_result("knowledge_base_tool", kb_result_data),
                            "details": self._extract_tool_result_details("knowledge_base_tool", kb_result_data),
                        }

                # 执行报告工具（串行，等前置工具全部完成后，用其结果填充 variables）
                # 先检查数据充分性，不足则跳过报告生成
                data_sufficient, skip_reason = self._check_data_sufficiency(ordered_results)
                if not data_sufficient and report_steps:
                    logger.warning(f"[tool_node] 数据不足，跳过报告生成: {skip_reason}")
                    for report_step in report_steps:
                        completed_steps += 1
                        ordered_results.append((report_step.step_id, {
                            "tool_name": "report_generator_tool",
                            "result": {"success": False, "error": skip_reason},
                        }))
                        yield {
                            "type": "tool_result",
                            "stage": "tool_result",
                            "tool_name": "report_generator_tool",
                            "tool_label": self._humanize_tool_name("report_generator_tool"),
                            "step_id": report_step.step_id,
                            "completed_steps": completed_steps,
                            "total_steps": total_steps,
                            "title": f"步骤 {report_step.step_id} 已跳过：数据不足",
                            "message": skip_reason,
                            "details": [skip_reason],
                        }
                else:
                    for report_step in report_steps:
                        step_info = self._format_plan_step(report_step)
                        yield {
                            "type": "tool_start",
                            "stage": "tool_start",
                            "tool_name": report_step.tool,
                            "tool_label": step_info["tool_label"],
                            "step_id": report_step.step_id,
                            "title": f"步骤 {report_step.step_id} 开始：{report_step.action}",
                            "message": "正在整理数据并生成报告文档…",
                            "details": step_info["details"],
                        }
                        # 从前置工具结果中提取数据，自动填充 variables
                        auto_variables = self._build_report_variables(
                            ordered_results, state["user_message"], intent_result
                        )
                        step_id, result = await _invoke_stream_step(
                            report_step, extra_params={"variables": auto_variables}
                        )
                        ordered_results.append((step_id, result))
                        completed_steps += 1
                        result_data = result.get("result", {})
                        yield {
                            "type": "tool_result",
                            "stage": "tool_result",
                            "tool_name": "report_generator_tool",
                            "tool_label": self._humanize_tool_name("report_generator_tool"),
                            "step_id": step_id,
                            "completed_steps": completed_steps,
                            "total_steps": total_steps,
                            "title": f"步骤 {step_id} 完成：报告已生成",
                            "message": self._summarize_tool_result("report_generator_tool", result_data),
                            "details": self._extract_tool_result_details("report_generator_tool", result_data),
                        }

                ordered_results.sort(key=lambda item: item[0])
                state["tool_results"] = [result for _, result in ordered_results]
                yield {
                    "type": "status",
                    "stage": "response",
                    "title": "开始汇总结果",
                    "message": "工具执行完成，正在整理最终结果。",
                    "details": [f"已完成 {completed_steps} / {total_steps} 个步骤，接下来生成最终答复。"],
                }


            if route == "summarize_node":
                # 直接路径（无需工具）：直接生成回复
                state = self._summarize_node(state)
                yield {
                    "type": "status",
                    "stage": "done",
                    "title": "最终回复已生成",
                    "message": "已完成结果整理，准备回传前端。",
                    "details": [
                        f"地图命令数：{len(state.get('map_commands', []))}",
                        f"三维命令数：{len(state.get('cesium_commands', []))}",
                        f"图表数：{len(state.get('charts', []))}",
                        f"报告地址：{state.get('report_url') or '无'}",
                    ],
                }
            elif route == "tool_node":
                # 工具路径：先提取结构化输出，再生成回复
                state = self._extract_node(state)
                state = self._summarize_node(state)
                yield {
                    "type": "status",
                    "stage": "done",
                    "title": "最终回复已生成",
                    "message": "已完成结果整理，准备回传前端。",
                    "details": [
                        f"地图命令数：{len(state.get('map_commands', []))}",
                        f"三维命令数：{len(state.get('cesium_commands', []))}",
                        f"图表数：{len(state.get('charts', []))}",
                        f"报告地址：{state.get('report_url') or '无'}",
                    ],
                }
            yield {
                "type": "final",
                "result": self._build_stream_result(state),
            }
        except Exception as e:
            logger.error(f"[TaskExecutor] Stream execution error: {e}")
            yield {
                "type": "error",
                "stage": "error",
                "title": "执行异常",
                "message": f"系统执行出现错误：{str(e)}",
                "details": ["请检查后端日志定位具体原因。"],
            }
            yield {
                "type": "final",
                "result": {
                    "success": False,
                    "response": f"系统执行出现错误：{str(e)}",
                    "messages": [],
                    "map_commands": [],
                    "cesium_commands": [],
                    "charts": [],
                    "report_url": None,
                    "intent_info": None,
                    "requires_confirmation": False,
                },
            }

    # ------------------------------------------------------------------
    # 对外接口（保持与原版完全兼容）
    # ------------------------------------------------------------------

    async def execute(self, user_message: str, chat_history: Optional[List[Dict]] = None, thread_id: str = "default") -> Dict[str, Any]:
        """执行任务，返回与原版完全兼容的字典格式。"""
        self._prune_checkpoints(thread_id)
        initial_state: AgentState = {
            "user_message": user_message,
            "chat_history": chat_history or [],
            "intent_result": None,
            "tool_results": [],
            "response": "",
            "map_commands": [],
            "cesium_commands": [],
            "charts": [],
            "report_url": None,
            "error": None,
        }
        config = {"configurable": {"thread_id": thread_id}}

        try:
            final_state = await self._graph.ainvoke(initial_state, config=config)
        except Exception as e:
            logger.error(f"[TaskExecutor] Graph execution error: {e}")
            return {
                "success": False,
                "response": f"系统执行出现错误：{str(e)}",
                "map_commands": [],
                "cesium_commands": [],
                "charts": [],
                "requires_confirmation": False,
                "intent_result": None,
            }

        intent_result = final_state.get("intent_result")
        low_confidence = (
            intent_result is not None and intent_result.confidence < 0.3
        )

        return {
            "success": not low_confidence,
            "intent_result": intent_result,
            "response": final_state.get("response", "命令已执行。"),
            "messages": [],  # LangGraph 模式下不返回原始 messages 列表
            "map_commands": final_state.get("map_commands", []),
            "cesium_commands": final_state.get("cesium_commands", []),
            "charts": final_state.get("charts", []),
            "report_url": final_state.get("report_url"),
            "requires_confirmation": (
                intent_result.requires_confirmation if intent_result else False
            ),
        }
