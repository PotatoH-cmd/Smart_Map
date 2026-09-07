"""输出序列化：初始状态/意图序列化/预览/计划格式化/流式结果（自 task_executor.py 机械搬移，方法体逐字保留）。
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


class OutputSerializationMixin:
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
