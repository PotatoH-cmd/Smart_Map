import asyncio
import httpx
import json
import logging
import operator
import os
import re
from typing import Annotated, List, Dict, Any, Optional, TypedDict
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
from .state import AgentState, QwenToolAdapter  # re-export：外部引用兼容

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskExecutor：LangGraph 编排
# ---------------------------------------------------------------------------

from .nodes.intent_node import IntentNodeMixin
from .nodes.tool_node import ToolNodeMixin
from .nodes.extract_node import ExtractNodeMixin
from .nodes.summarize_node import SummarizeNodeMixin
from .nodes.serialize import OutputSerializationMixin


class TaskExecutor(IntentNodeMixin, ToolNodeMixin, ExtractNodeMixin, SummarizeNodeMixin, OutputSerializationMixin):
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