"""意图节点：意图识别（远程优先/本地回退）+ 路由决策（自 task_executor.py 机械搬移，方法体逐字保留）。
P1 拆分：以 Mixin 形式挂载到 TaskExecutor，self 语义与运行时行为完全不变。
"""
import asyncio
import httpx
import json
import logging
import operator
import os
import time
from typing import Annotated, List, Dict, Any, Optional, TypedDict, AsyncGenerator
from urllib.parse import quote

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.graph import END

from ..intent_types import IntentType, IntentResult, TaskStep
from ..qgis_workflows import match_recipe, extract_params, substitute_params, RECIPES
from ..state import AgentState, QwenToolAdapter
from ..providers import apply_corrections
from core.obs import obs_generation

logger = logging.getLogger(__name__)

# 豫水地图助手能力介绍（能力咨询类请求的预制回复，不走 LLM，
# 避免 LLM 编造"通用聊天助手"式自我介绍，不符合豫水地图助手业务定位）
YUSHUI_CAPABILITY_INTRO = """我是豫水地图助手，专注于水利采砂监测与河道管理业务。我可以协助您完成以下工作：

一、采砂监测报告
• 生成采砂场/可采区采砂监测分析报告（含 RTK 测点统计、无人机正射影像解译、高程与超深评估、现场监测情况，输出 Word 报告）

二、地图操作
• 切换卫星影像/街道底图，加载采区、河道管理范围线等矢量图层
• 地图定位、2D/3D 飞行浏览、标记与清除标记

三、数据查询
• RTK 测点与测深数据统计：控制开采高程、实测高程、超深度开采分析
• 采区边界、河道管理红线等空间参考数据查询

四、空间分析与处理
• 缓冲区分析、影像裁剪、叠加分析、面积计算、红线距离分析
• 坐标投影转换（CGCS2000 带号换算）、XY 坐标交换、矢量面/线/点生成

五、知识检索
• 水利政策法规、采砂管理规定、技术标准等知识库文档查询

六、数据可视化
• 柱状图、饼图、折线图等统计图表生成

示例："生成中山村采区采砂场监测报告"、"切换到卫星底图"、"查询史河毕店可采区2025年控制开采高程范围"。"""


# ── 导航类请求（飞到X / 飞往X / 定位到X）的确定性地名提取 ──
# 这些词在 fast_routes.json 中路由为 map_display/location_search，而快速路由给不出
# execution_plan：空计划 → 无工具调用 → 直达 summarize_node，LLM 凭空回答"已飞抵"，
# 前端收不到任何相机命令，视角不动。导航请求改为确定性单步 location_search 计划
# （该工具同时产出 2D set_view 与 3D flyTo 两条命令，2D/3D 视图各自消费自己那条）。
_NAV_VERBS = ("飞往", "飞到", "飞至", "fly to", "flyto", "跳转到", "导航到", "定位到")
_NAV_LEADS = ("请", "帮我", "麻烦", "我想", "我要", "把视角", "视角", "镜头", "地图")
_NAV_TAILS = ("的位置", "的坐标", "那里", "那个地方", "去看看", "看看", "查看",
              "上空", "附近", "一带", "区域", "位置", "处")


def extract_nav_location(user_msg: str) -> str:
    """从导航类请求中提取目标地名；非导航请求或地名不可用时返回空串。

    "请帮我飞到郏县北汝河处" → "郏县北汝河"
    """
    text = (user_msg or "").strip()
    lower = text.lower()
    hit = None
    for verb in _NAV_VERBS:
        idx = lower.find(verb)
        if idx >= 0 and (hit is None or idx < hit[0]):
            hit = (idx, verb)
    if hit is None:
        return ""
    idx, verb = hit
    name = text[idx + len(verb):].strip()
    for lead in _NAV_LEADS:
        if name.startswith(lead):
            name = name[len(lead):].strip()
    for tail in _NAV_TAILS:
        if name.endswith(tail):
            name = name[:-len(tail)].strip()
            break
    name = name.strip("，。！？、,.!?")
    return name if 1 <= len(name) <= 20 else ""


class IntentNodeMixin:
    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 快速路由的最小执行计划（规则声明式，杜绝空计划直达汇总节点编造结果）
    # ------------------------------------------------------------------
    # 参数占位符：$msg → 用户原文；$loc → 按导航词提取出的地名（提取失败回退原文）
    _FAST_PARAM_LOC = "$loc"

    def _resolve_fast_params(self, params: Dict[str, Any], user_msg: str) -> Dict[str, Any]:
        """解析规则参数中的占位符。"""
        resolved: Dict[str, Any] = {}
        for key, value in (params or {}).items():
            if isinstance(value, str) and value.startswith("$"):
                if value == self._FAST_PARAM_LOC:
                    resolved[key] = extract_nav_location(user_msg) or user_msg
                else:
                    resolved[key] = user_msg
            else:
                resolved[key] = value
        return resolved

    def _build_fast_route_plan(self, rule: Dict[str, Any], user_msg: str,
                               view_hint: Optional[str] = None) -> Optional[IntentResult]:
        """按规则声明的最小执行计划构造单步 IntentResult；规则未声明计划时返回 None。

        3D 视图优先使用规则声明的 tool_cesium/params_cesium（地图类工具与视图绑定）。
        """
        tool = rule.get("tool")
        params = rule.get("params") or {}
        if view_hint == "cesium" and rule.get("tool_cesium"):
            tool = rule["tool_cesium"]
            params = rule.get("params_cesium") or {}
        if not tool:
            return None
        resolved = self._resolve_fast_params(params, user_msg)
        return IntentResult(
            primary_intent=rule["intent"],
            confidence=0.95,
            entities=[],
            task_context=f"快速路由：{rule['intent'].value}（关键词「{rule['keyword']}」）",
            execution_plan=[TaskStep(
                step_id=1,
                action=f"执行「{rule['keyword']}」对应的操作",
                tool=tool,
                params=resolved,
                reasoning="快速路由命中关键词，规则已声明最小执行计划，直接调用工具执行",
                expected_output="返回工具执行结果，并生成对应的地图/相机命令",
            )],
            requires_confirmation=False,
            suggestions=[],
        )

    def _intent_node(self, state: AgentState, workspace_summary: str = "") -> AgentState:
        """节点1：意图识别。支持快速路由（关键词匹配绕过 LLM）。

        workspace_summary：会话状态树摘要（阶段3），仅注入 LLM 分析输入，
        不影响快速路由关键词匹配。
        """
        user_msg = state["user_message"]

        # ── 导航类请求：确定性单步计划（不走快速路由的空计划，也不依赖 LLM 规划）──
        nav_location = extract_nav_location(user_msg)
        if nav_location:
            intent_result = IntentResult(
                primary_intent=IntentType.LOCATION_SEARCH,
                confidence=0.95,
                entities=[nav_location],
                task_context=f"快速路由：位置导航（{nav_location}）",
                execution_plan=[TaskStep(
                    step_id=1,
                    action=f"查找「{nav_location}」的坐标并跳转",
                    tool="location_search",
                    params={"location_name": nav_location},
                    reasoning="导航类请求需先解析地名坐标，再下发相机跳转命令",
                    expected_output="返回坐标，并生成 2D set_view / 3D flyTo 命令",
                )],
                requires_confirmation=False,
                suggestions=[],
            )
            state["intent_result"] = intent_result
            logger.info(f"[intent_node] Nav route: location_search(location_name={nav_location!r})")
            return state

        # 尝试快速路由：规则自带最小执行计划则确定性执行；未声明计划的规则回退 LLM 规划。
        # 不允许用空 execution_plan 短路 —— 空计划不调用任何工具就直达 summarize_node，
        # 会让 LLM 凭空回答"已完成"（历史缺陷，见 fast_routes.json 的 _plan_comment）。
        fast_rule = self.harness.try_fast_route(user_msg)
        fast_intent = fast_rule["intent"] if fast_rule else None
        if fast_rule is not None:
            # 规则自带最小执行计划 → 构造确定性单步计划直接执行工具
            fast_plan = self._build_fast_route_plan(fast_rule, user_msg, state.get("view_hint"))
            if fast_plan is not None:
                state["intent_result"] = fast_plan
                logger.info(
                    f"[intent_node] Fast route: keyword='{fast_rule['keyword']}', "
                    f"intent={fast_intent.value}, tool={fast_plan.execution_plan[0].tool}"
                )
                return state
            # 能力咨询（"你能干什么"等，路由为 unknown）无需工具，直接写入预制介绍。
            # 若走 LLM 汇总，模型缺少豫水地图助手人设，会生成通用聊天助手自我介绍。
            # 此处直接透传预制文案（summarize_node 对已有 response 原样透传，不再调 LLM）。
            if fast_intent == IntentType.UNKNOWN:
                state["intent_result"] = IntentResult(
                    primary_intent=fast_intent,
                    confidence=0.95,
                    entities=[],
                    task_context=f"快速路由：{fast_intent.value}",
                    execution_plan=[],
                    requires_confirmation=False,
                    suggestions=[],
                )
                state["response"] = YUSHUI_CAPABILITY_INTRO
                state["map_commands"] = []
                state["cesium_commands"] = []
                state["charts"] = []
                state["tool_results"] = []
                logger.info("[intent_node] Capability intro: prebuilt response injected, skipping LLM")
                return state

            # 规则未声明最小执行计划 → 不短路，交由下方 LLM 规划出具体工具步骤
            logger.info(
                f"[intent_node] Fast route without plan: keyword='{fast_rule['keyword']}', "
                f"intent={fast_intent.value} → fallback to LLM planning"
            )

        # 未命中快速路由，走 LLM 意图分析
        analysis_input = user_msg
        if workspace_summary:
            analysis_input = f"{workspace_summary}\n\n【用户本轮输入】\n{user_msg}"

        # 服务化路径：INTENT_SERVICE_URL 配置时优先走意图服务，失败回退进程内 IntentAgent
        t_llm = time.perf_counter()
        intent_result = self._remote_intent_analyze(
            analysis_input, state.get("chat_history"), state.get("view_hint"))
        remote_used = intent_result is not None
        if remote_used:
            logger.info("[intent_node] Remote intent service used")
        else:
            # 阶段6：view_hint 用于地图类工具约束的按需注入（2D/3D 二选一）
            intent_result = self.intent_agent.analyze(
                analysis_input,
                state.get("chat_history"),
                view_hint=state.get("view_hint"),
            )
        # 可观测性：意图分析 LLM 调用（remote / local 均上报）
        try:
            _llm = getattr(self.intent_agent, "structured_llm", None)
            _model = (getattr(_llm, "model_name", None) or getattr(_llm, "model", None)
                      or ("intent-service" if remote_used else "unknown"))
            obs_generation(
                run_id=state.get("_run_id", ""),
                name="intent_analysis" + ("_remote" if remote_used else ""),
                model=str(_model),
                prompt=analysis_input,
                completion={
                    "intent": str(getattr(intent_result, "primary_intent", "")),
                    "confidence": getattr(intent_result, "confidence", None),
                    "steps": len(getattr(intent_result, "execution_plan", []) or []),
                },
                usage=None,
            )
        except Exception:
            pass  # 观测埋点失败不影响主流程

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

        # ── 确定性计划纠正 ──
        # 各业务主题（红线距离/测深加载/缓冲区/坐标转矢量/数值查询/砂场统计/报告裁剪）
        # 的"确定性兑底"收敛到 providers 注册表，此处仅依注册顺序逐个应用
        # （后纠正覆盖前纠正，语义与迁移前串行覆盖完全一致）。
        for correction_desc in apply_corrections(user_msg, intent_result):
            logger.info(f"[intent_node] {correction_desc}")

        logger.info(
            f"[intent_node] LLM route: intent={intent_result.primary_intent}, "
            f"confidence={intent_result.confidence}, "
            f"steps={len(intent_result.execution_plan)}"
        )
        state["intent_result"] = intent_result
        return state

    def _remote_intent_analyze(self, analysis_input: str,
                               chat_history: Optional[List[Dict]],
                               view_hint: Optional[str]) -> Optional[IntentResult]:
        """通过意图服务（INTENT_SERVICE_URL）做意图分析；未配置或失败返回 None。"""
        url = os.environ.get("INTENT_SERVICE_URL")
        if not url:
            return None
        # 注入业务上下文（意图服务自身无业务知识）
        db_schema = ""
        try:
            from tools.schema_manager import SchemaManager
            db_schema = SchemaManager.instance().get_formatted_schema() or ""
        except Exception:
            pass
        facts_context = ""
        try:
            from .fact_memory import build_facts_context
            facts_context = build_facts_context()
        except Exception:
            pass
        payload = {
            "message": analysis_input,
            "history": chat_history or [],
            "context": {"view": view_hint,
                        "extra": {"db_schema": db_schema, "facts_context": facts_context}},
        }
        try:
            resp = httpx.post(f"{url.rstrip('/')}/v1/intent/analyze", json=payload, timeout=90.0)
            resp.raise_for_status()
            d = resp.json()
            return IntentResult(
                primary_intent=d.get("primary_intent", "unknown"),
                confidence=float(d.get("confidence", 0.0)),
                entities=d.get("entities") or [],
                task_context=d.get("task_context", ""),
                execution_plan=[TaskStep(**s) for s in (d.get("execution_plan") or [])],
                requires_confirmation=bool(d.get("requires_confirmation", False)),
                suggestions=d.get("suggestions") or [],
            )
        except Exception as e:
            logger.warning(f"[intent_node] Intent service unavailable, fallback local: {e}")
            return None


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
