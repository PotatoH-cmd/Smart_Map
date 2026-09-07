"""意图节点：意图识别（远程优先/本地回退）+ 路由决策（自 task_executor.py 机械搬移，方法体逐字保留）。
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
from langgraph.graph import END

from ..intent_types import IntentType, IntentResult, TaskStep
from ..qgis_workflows import match_recipe, extract_params, substitute_params, RECIPES
from ..state import AgentState, QwenToolAdapter

logger = logging.getLogger(__name__)


class IntentNodeMixin:
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

        # 服务化路径：INTENT_SERVICE_URL 配置时优先走意图服务，失败回退进程内 IntentAgent
        intent_result = self._remote_intent_analyze(
            analysis_input, state.get("chat_history"), state.get("view_hint"))
        if intent_result is not None:
            logger.info("[intent_node] Remote intent service used")
        else:
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
