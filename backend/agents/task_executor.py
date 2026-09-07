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
