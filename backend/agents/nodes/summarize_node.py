"""汇总节点：响应生成与系统提示词（自 task_executor.py 机械搬移，方法体逐字保留）。
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
from ..agent_harness import AgentHarness
from ..state import AgentState, QwenToolAdapter

logger = logging.getLogger(__name__)


class SummarizeNodeMixin:
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
