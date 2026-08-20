"""
run_engine.py — RunEngine：可中断、可恢复、可取消的执行引擎（阶段1：核心重构）。

设计（对齐文章 agent harness 理念）：
  - 一次任务 = 一个有生命周期的 run（RUNNING → AWAITING_* / COMPLETED / FAILED / CANCELLED）
  - run 在独立 asyncio task 中执行，SSE 连接只是事件订阅者（断线不影响执行）
  - 每个工具步骤之间是"中断点"：检查取消、处理缺参/确认（PendingRequired）
  - pending 时把完整执行现场（plan + 已完成结果 + 剩余步骤）序列化进 run_checkpoints，
    用户确认/补参后 resume 从断点继续，不重新做意图分析
  - 超时兜底：单次 run 超过 RUN_TIMEOUT_SECONDS 强制 FAILED（防 task 泄漏）

替换 task_executor.execute_stream 的手动编排（灰度开关 RUN_ENGINE 见 main.py）。
"""
import asyncio
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

from .intent_types import IntentResult, TaskStep
from .run_store import (
    AWAITING_CONFIRMATION,
    AWAITING_INPUT,
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    RunEvent,
    RunStore,
    get_run_store,
)
from .event_bus import EventBus
from .qgis_workflows import match_recipe, extract_params

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 600  # 10 分钟兜底

# 确认词（规则判定，不调 LLM；要求 pending 存在且消息短）
CONFIRM_KEYWORDS = ("确认", "执行", "继续", "好的", "可以", "按计划执行")
CONFIRM_MAX_LEN = 20


def is_confirm_message(text: str) -> bool:
    """规则化确认判定：消息短且含确认词，或携带 __confirm__ 标记。"""
    t = (text or "").strip()
    if "__confirm__" in t:
        return True
    return len(t) <= CONFIRM_MAX_LEN and any(k in t for k in CONFIRM_KEYWORDS)


def parse_supplied_from_message(pending: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    """从用户自然语言补充消息中提取 pending 缺失参数（纯规则，不调 LLM）。

    - feature_name：优先 recipe 提取器，否则整个消息作为名称
    - distance：优先 recipe 提取器，否则正则提取数字（如"500米"）
    - table_name / demand / coordinates：整个消息兜底
    """
    supplied: Dict[str, Any] = {}
    missing = pending.get("missing") or []
    if not missing:
        return supplied
    params = [m.get("param") for m in missing if m.get("param")]
    variables: Dict[str, Any] = {}
    try:
        _, recipe = match_recipe(user_text)
        if recipe:
            variables = extract_params(user_text, recipe) or {}
    except Exception:
        variables = {}
    for p in params:
        if p == "feature_name":
            supplied[p] = variables.get("feature_name") or user_text.strip()
        elif p == "distance":
            dist = variables.get("distance")
            if dist is None:
                m = re.search(r"(\d+(?:\.\d+)?)\s*(?:米|m|公里|km)?", user_text)
                if m:
                    dist = float(m.group(1))
            if dist is not None:
                supplied[p] = dist
        elif p in ("table_name", "demand", "coordinates"):
            supplied[p] = user_text.strip()
    return supplied


class PendingRequired(Exception):
    """执行中需要用户介入：确认计划或补充参数。

    携带结构化载荷，由引擎捕获后落 checkpoint + pending 事件。
    """

    def __init__(self, pending_type: str, payload: Dict[str, Any]):
        super().__init__(pending_type)
        self.pending_type = pending_type  # "confirm" | "input"
        self.payload = payload


class RunEngine:
    """可中断执行引擎。executor 为 TaskExecutor（复用其意图分析/工具/汇总能力）。"""

    def __init__(
        self,
        executor,
        store: Optional[RunStore] = None,
        bus: Optional[EventBus] = None,
        gateway=None,
        timeout: int = RUN_TIMEOUT_SECONDS,
    ):
        self.executor = executor
        self.store = store or get_run_store()
        self.bus = bus or EventBus(self.store)
        self.gateway = gateway  # RulesGateway（阶段2），可为 None 时跳过校验
        self.timeout = timeout

    # ==================================================================
    # 对外入口
    # ==================================================================

    async def start(
        self,
        run_id: str,
        session_id: str,
        user_message: str,
        chat_history: Optional[List[Dict]] = None,
        intent_result: Optional[IntentResult] = None,
        view_hint: Optional[str] = None,
    ) -> asyncio.Task:
        """创建 run 并启动后台执行任务。返回 task（调用方可选择 await）。"""
        self.bus.reset_run(run_id)
        self.store.create_run(run_id, session_id, user_message)
        state: Dict[str, Any] = {
            "user_message": user_message,
            "chat_history": chat_history or [],
            "intent_result": intent_result,
            "tool_results": [],
            "tool_summaries": [],
            "response": "",
            "map_commands": [],
            "cesium_commands": [],
            "charts": [],
            "report_url": None,
            "error": None,
            "view_hint": view_hint,
            # 引擎内部进度（不随 final 输出）
            "_remaining_steps": [],
            "_done_steps": [],
            "_pending": None,
            "_session_id": session_id,
        }
        return asyncio.create_task(self._guarded(run_id, state, resumed=False))

    async def resume(
        self,
        run_id: str,
        user_supplied: Optional[Dict[str, Any]] = None,
        confirm: bool = False,
    ) -> Optional[asyncio.Task]:
        """从 checkpoint 恢复执行。confirm=True 直接继续；否则合并补充参数。"""
        checkpoint = self.store.load_checkpoint(run_id)
        if not checkpoint:
            logger.warning(f"[RunEngine] resume failed: no checkpoint for run {run_id}")
            return None
        state = self._checkpoint_to_state(checkpoint)

        pending = state.get("_pending") or {}
        if confirm:
            state["_pending"] = None
        elif user_supplied:
            self._merge_supplied(state, pending, user_supplied)

        self.store.update_status(run_id, RUNNING)
        self.store.clear_pending(run_id)
        logger.info(
            f"[RunEngine] resume run={run_id} confirm={confirm} supplied={list((user_supplied or {}).keys())}"
        )
        return asyncio.create_task(self._guarded(run_id, state, resumed=True))

    # ==================================================================
    # 执行主循环（_guarded 定义见模块末尾，含超时/pending/异常兜底）
    # ==================================================================

    async def _run_plan(self, run_id: str, state: Dict[str, Any], resumed: bool):
        """主循环：意图 → 路由 → 工具步骤（中断点）→ 汇总 → final。"""
        # ── 1. 意图分析（resume 时跳过，使用 checkpoint 中的 intent）──
        if state.get("intent_result") is None:
            await self._emit(
                run_id, "status", "start", title="已接收请求",
                message="开始分析用户问题与上下文。",
                details=[f"历史消息数：{len(state.get('chat_history') or [])}"],
            )
            await self._emit(
                run_id, "status", "intent_start", title="意图分析中",
                message="正在调用意图分析模型，识别任务类型并规划步骤。",
                details=["此阶段会判断是地图、查询、制图还是报告任务。"],
            )
            # 阶段3：注入会话状态树摘要（工作区图层/产物/上轮结论），供多轮引用
            ws_summary = ""
            if getattr(self.executor, "workspace", None) is not None:
                try:
                    ws_summary = self.executor.workspace.build_summary(
                        state.get("_session_id", "")
                    )
                except Exception as e:
                    logger.warning(f"[RunEngine] workspace summary failed: {e}")
            if ws_summary:
                logger.info(f"[RunEngine] workspace summary injected ({len(ws_summary)} chars)")
            state = self.executor._intent_node(state, workspace_summary=ws_summary)
        intent_result: IntentResult = state.get("intent_result")
        if intent_result is None:
            raise ValueError("intent_result is None after intent analysis")

        # intent / plan 事件（resume 时也重新发出，保证重连端能看到上下文）
        plan_steps = [self.executor._format_plan_step(s) for s in intent_result.execution_plan]
        primary_intent = intent_result.primary_intent.value if hasattr(intent_result.primary_intent, "value") else str(intent_result.primary_intent)
        await self._emit(
            run_id, "intent", "intent", title=f"意图识别完成：{primary_intent}",
            message=f"已识别主要意图：{primary_intent}，置信度 {intent_result.confidence:.2f}。",
            details=[
                f"任务理解：{self._trunc(intent_result.task_context, 160)}",
                f"识别实体：{', '.join(intent_result.entities[:6]) if intent_result.entities else '未识别到显式实体'}",
                f"是否需要确认：{'是' if intent_result.requires_confirmation else '否'}",
            ],
            plan_steps=plan_steps,
            intent_info=self.executor._serialize_intent_info(intent_result),
        )
        if plan_steps:
            await self._emit(
                run_id, "plan", "plan", title=f"执行计划已生成（{len(plan_steps)} 步）",
                message="下面开始按计划执行。",
                details=[f"步骤 {s['step_id']}：{s['action']}" for s in plan_steps],
                plan_steps=plan_steps,
            )

        await self._check_cancel(run_id)

        # ── 2. 路由：低置信度 / 确认 / 缺参 / 直答 / 工具 ──
        # 先物化剩余步骤：pending 落 checkpoint 时需携带未执行步骤，
        # resume 时 _merge_supplied 才能把补参合并进剩余步骤 params。
        self._remaining_or_full(state, intent_result)

        if intent_result.confidence < 0.3:
            state["response"] = "抱歉，我无法理解您的意图。请尝试更详细地描述您的需求。"
            await self._emit_final(run_id, self.executor._build_stream_result(state))
            self.store.update_status(run_id, COMPLETED)
            self.store.delete_checkpoint(run_id)
            self.store.clear_pending(run_id)
            return

        # resume 时跳过确认/缺参检测：用户已介入过，从断点继续执行
        # （否则基于原始消息的检测会重复触发，形成 pending 死循环）
        # 缺参优先于确认：缺参能给出明确的缺失参数列表（input），用户补参即隐含确认；
        # 两者同时成立时（LLM 既要求确认又漏填参数），走 input 更利于 resume。
        missing = [] if resumed else self._detect_missing_params(intent_result, state.get("user_message", ""))
        if missing:
            self._raise_pending_input(state, missing)

        if intent_result.requires_confirmation and not resumed:
            self._raise_pending_confirm(state)

        required_tools = self.executor.intent_agent.get_required_tools(intent_result)
        logger.info(
            f"[RunEngine] route: intent={primary_intent}, tools={required_tools}, "
            f"resumed={resumed}"
        )
        if not required_tools:
            await self._emit(
                run_id, "status", "response", title="直接生成回复",
                message="本轮无需调用外部工具，直接整理答案。",
            )
            state = self.executor._summarize_node(state)
            await self._emit(
                run_id, "status", "done", title="最终回复已生成",
                message="已完成结果整理，准备回传前端。",
                details=self._final_details(state),
            )
            await self._emit_final(run_id, self.executor._build_stream_result(state))
            self.store.update_status(run_id, COMPLETED)
            self.store.delete_checkpoint(run_id)
            self.store.clear_pending(run_id)
            return

        # ── 3. 工具步骤执行（每步之间是中断点）──
        steps = self._remaining_or_full(state, intent_result)
        await self._check_cancel(run_id)

        pre_steps = [s for s in steps if s.tool != "report_generator_tool"]
        report_steps = [s for s in steps if s.tool == "report_generator_tool"]
        total_steps = len(steps)

        await self._emit(
            run_id, "status", "tool_plan", title="进入工具执行阶段",
            message=f"已生成执行计划，开始执行 {total_steps} 个工具步骤。",
            details=[f"将按计划调用：{', '.join(self.executor._humanize_tool_name(s.tool) for s in steps)}"],
            total_steps=total_steps,
        )

        completed = len(state.get("_done_steps") or [])
        ordered_results: List = list(state.get("tool_results") or [])
        results_by_step: Dict[int, Dict] = {
            r.get("step_id"): r for r in ordered_results if isinstance(r, dict)
        }

        # 并行执行普通步骤（保留现有 asyncio.gather 语义）
        pending_tasks = {}
        for step in pre_steps:
            if step.step_id in state.get("_done_steps", set()):
                continue
            await self._check_cancel(run_id)
            await self._emit_tool_start(run_id, step)
            pending_tasks[step.step_id] = asyncio.create_task(self._invoke_step(run_id, state, step))

        for finished in asyncio.as_completed(list(pending_tasks.values())):
            step_id, result = await finished
            completed += 1
            ordered_results.append({"step_id": step_id, **result})
            results_by_step[step_id] = result
            state["_done_steps"] = list(set(state.get("_done_steps") or []) | {step_id})
            step = next((s for s in steps if s.step_id == step_id), None)
            await self._emit_tool_result(run_id, step, result, completed, total_steps)
            await self._check_cancel(run_id)

        # 知识库回退（DB 空 → KB 补搜，保留现有编排语义）
        kb_fallback = await self.executor._fallback_knowledge_search(
            ordered_results, state["user_message"], self._invoke_step_bare
        )
        if len(kb_fallback) > len(ordered_results):
            for kb_item in kb_fallback[len(ordered_results):]:
                ordered_results.append(kb_item)
                await self._emit(
                    run_id, "tool_result", "tool_result",
                    tool_name="knowledge_base_tool",
                    tool_label=self.executor._humanize_tool_name("knowledge_base_tool"),
                    title="知识库补充检索完成（数据库无结果，自动回退）",
                    message=self.executor._summarize_tool_result("knowledge_base_tool", kb_item.get("result", {})),
                    details=self.executor._extract_tool_result_details("knowledge_base_tool", kb_item.get("result", {})),
                    completed_steps=completed, total_steps=total_steps,
                )

        # 报告工具（串行：数据充分性检查 → 生成）
        if report_steps:
            data_sufficient, skip_reason = self.executor._check_data_sufficiency(
                list(zip(range(len(ordered_results)), ordered_results))
            )
            if not data_sufficient:
                for rs in report_steps:
                    await self._emit(
                        run_id, "tool_result", "tool_result",
                        tool_name="report_generator_tool",
                        tool_label=self.executor._humanize_tool_name("report_generator_tool"),
                        step_id=rs.step_id,
                        title=f"步骤 {rs.step_id} 已跳过：数据不足",
                        message=skip_reason, details=[skip_reason],
                        completed_steps=completed, total_steps=total_steps,
                    )
                    ordered_results.append({
                        "step_id": rs.step_id,
                        "tool_name": "report_generator_tool",
                        "result": {"success": False, "error": skip_reason},
                    })
            else:
                for rs in report_steps:
                    await self._check_cancel(run_id)
                    await self._emit_tool_start(run_id, rs)
                    auto_vars = self.executor._build_report_variables(
                        list(zip(range(len(ordered_results)), ordered_results)),
                        state["user_message"], intent_result,
                    )
                    step_id, result = await self._invoke_step(
                        run_id, state, rs, extra_params={"variables": auto_vars}
                    )
                    completed += 1
                    ordered_results.append({"step_id": step_id, **result})
                    state["_done_steps"] = list(set(state.get("_done_steps") or []) | {step_id})
                    await self._emit_tool_result(run_id, rs, result, completed, total_steps)
                    await self._check_cancel(run_id)

        # ── 4. 汇总 ──
        state["tool_results"] = ordered_results
        await self._emit(
            run_id, "status", "response", title="开始汇总结果",
            message="工具执行完成，正在整理最终结果。",
            details=[f"已完成 {completed} / {total_steps} 个步骤，接下来生成最终答复。"],
        )
        state = self.executor._extract_node(state)
        state = self.executor._summarize_node(state)
        await self._emit(
            run_id, "status", "done", title="最终回复已生成",
            message="已完成结果整理，准备回传前端。",
            details=self._final_details(state),
        )

        # 工作区状态记录（阶段3：WorkspaceState）
        if hasattr(self.executor, "workspace") and self.executor.workspace is not None:
            try:
                self.executor.workspace.record(
                    session_id=state.get("_session_id", ""),
                    run_id=run_id,
                    tool_results=state.get("tool_results", []),
                    map_commands=state.get("map_commands", []),
                    report_url=state.get("report_url"),
                    response=state.get("response", ""),
                )
            except Exception as e:
                logger.warning(f"[RunEngine] workspace.record failed (non-fatal): {e}")

        await self._emit_final(run_id, self.executor._build_stream_result(state))
        self.store.update_status(run_id, COMPLETED)
        self.store.delete_checkpoint(run_id)
        self.store.clear_pending(run_id)

    # ==================================================================
    # 工具调用
    # ==================================================================

    async def _invoke_step(self, run_id: str, state: Dict[str, Any], step: TaskStep,
                           extra_params: Optional[Dict] = None):
        """执行单个工具步骤：preflight → 调用 → postflight 验证。"""
        if step.tool == "qgis_mcp_tool":
            try:
                result = await self.executor._execute_qgis_workflow(state, step)
                result = {"tool_name": "qgis_mcp_tool", "result": result}
            except Exception as e:
                logger.error(f"[RunEngine] QGIS workflow failed: {e}", exc_info=True)
                result = {"tool_name": "qgis_mcp_tool", "result": {"success": False, "error": str(e)}}
            verification = self._run_postflight(step.tool, result.get("result", {}), state)
            if verification is not None:
                result["result"]["_verification"] = verification
                if verification.get("risk") == "critical":
                    await self._emit_verification(run_id, step, verification)
        else:
            adapter = self.executor._get_tool_adapter(step.tool)
            if adapter is None:
                result = {"tool_name": step.tool, "result": {"success": False, "error": f"工具 {step.tool} 不可用"}}
            else:
                params = self.executor._normalize_tool_params(
                    step.tool, step.params or {}, state["user_message"], step
                )
                if extra_params:
                    params.update(extra_params)
                # preflight（阶段2）
                preflight_report = self._run_preflight(step.tool, params, state)
                if preflight_report is not None and preflight_report.get("risk") == "critical":
                    result = {
                        "tool_name": step.tool,
                        "result": {
                            "success": False,
                            "error": "；".join(preflight_report.get("errors", [])) or "预检未通过",
                            "_verification": preflight_report,
                        },
                    }
                else:
                    loop = asyncio.get_event_loop()
                    raw = await loop.run_in_executor(None, adapter.invoke, params)
                    result = {"tool_name": step.tool, "result": raw.get("result", raw) if isinstance(raw, dict) else raw}
                    # postflight（阶段2）
                    verification = self._run_postflight(step.tool, result.get("result", {}), state)
                    if verification is not None:
                        result["result"]["_verification"] = verification
                        if verification.get("risk") == "critical":
                            await self._emit_verification(run_id, step, verification)
        return step.step_id, result

    async def _invoke_step_bare(self, step, extra_params: Optional[Dict] = None):
        """供 executor._fallback_knowledge_search 回调使用的裸调用（无事件、无校验）。

        fallback 只会构造 knowledge_base_tool 假 step，这里按通用路径处理即可。
        """
        adapter = self.executor._get_tool_adapter(step.tool)
        if adapter is None:
            return {"tool_name": step.tool, "result": {"success": False, "error": f"工具 {step.tool} 不可用"}}
        params = step.params or {}
        if extra_params:
            params = {**params, **extra_params}
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, adapter.invoke, params)
        return {"tool_name": step.tool, "result": raw.get("result", raw) if isinstance(raw, dict) else raw}

    # ==================================================================
    # 校验接入（阶段2 RulesGateway，gateway 未注入时返回 None 跳过）
    # ==================================================================

    def _run_preflight(self, tool_name: str, params: Dict, state: Dict) -> Optional[Dict]:
        if self.gateway is None:
            return None
        try:
            return self.gateway.run_preflight(tool_name, params, state)
        except Exception as e:
            logger.warning(f"[RunEngine] preflight error for {tool_name}: {e}")
            return None

    def _run_postflight(self, tool_name: str, result: Dict, state: Dict) -> Optional[Dict]:
        if self.gateway is None:
            return None
        try:
            return self.gateway.run_postflight(tool_name, result, state)
        except Exception as e:
            logger.warning(f"[RunEngine] postflight error for {tool_name}: {e}")
            return None

    async def _emit_verification(self, run_id: str, step: TaskStep, verification: Dict):
        await self._emit(
            run_id, "verification", "verification",
            tool_name=step.tool,
            step_id=step.step_id,
            title=f"步骤 {step.step_id} 结果校验未通过",
            message="；".join(c.get("detail", "") for c in verification.get("checks", []) if not c.get("passed")),
            details=verification.get("details", []),
            risk=verification.get("risk", "warning"),
        )

    # ==================================================================
    # pending / cancel
    # ==================================================================

    def _raise_pending_confirm(self, state: Dict[str, Any]):
        intent: IntentResult = state["intent_result"]
        plan = self.executor._serialize_intent_info(intent)
        plan_text = "\n".join(
            f"{i + 1}. {s.action} (使用 {s.tool})" for i, s in enumerate(intent.execution_plan)
        )
        question = f"""我理解您的需求是：{intent.task_context}

我的执行计划是：
{plan_text}

请确认是否按此计划执行？"""
        pending = {
            "pending_type": "confirm",
            "question": question,
            "plan": plan,
            "missing": [],
        }
        state["_pending"] = pending
        raise PendingRequired("confirm", pending)

    def _raise_pending_input(self, state: Dict[str, Any], missing: List[Dict[str, Any]]):
        labels = "、".join(m.get("label", m.get("param", "")) for m in missing)
        question = f"执行前还缺少必要信息：{labels}。请补充后我将继续执行。"
        pending = {
            "pending_type": "input",
            "question": question,
            "plan": self.executor._serialize_intent_info(state["intent_result"]),
            "missing": missing,
        }
        state["_pending"] = pending
        raise PendingRequired("input", pending)

    async def _check_cancel(self, run_id: str):
        """中断点：检查取消标记。同步调用避免每步都开连接的开销放大——仅在步骤间调用。"""
        if self.store.is_cancelled(run_id):
            self.store.update_status(run_id, CANCELLED)
            await self._emit(
                run_id, "cancelled", "cancelled", title="任务已取消",
                message="任务已被用户取消。", details=["当前步骤完成后已停止执行。"],
            )
            raise asyncio.CancelledError()

    # ==================================================================
    # 缺参检测（纯规则，不调 LLM）
    # ==================================================================

    def _detect_missing_params(self, intent: IntentResult, user_message: str) -> List[Dict[str, Any]]:
        """执行型意图的必填参数规则检查。返回 missing 列表（元素含 param/label）。"""
        missing: List[Dict[str, Any]] = []
        for step in intent.execution_plan:
            if not step.tool:
                continue
            params = step.params or {}
            if step.tool == "qgis_mcp_tool":
                recipe_name, recipe = match_recipe(user_message)
                if recipe:
                    variables = extract_params(user_message, recipe)
                    feat_name = (variables.get("feature_name") or "").strip()
                    distance = variables.get("distance")
                    needs_distance = any(
                        s.get("action") == "execute" and "buffer" in str(s.get("params", {}).get("algorithm", "")).lower()
                        for s in recipe.get("steps", [])
                    ) or recipe_name == "buffer"
                    if not feat_name:
                        missing.append({"param": "feature_name", "label": "目标要素名称（如郝楼砂场）", "tool": step.tool})
                    if needs_distance and distance is None:
                        missing.append({"param": "distance", "label": "缓冲区距离（米）", "tool": step.tool})
            elif step.tool == "spatial_processing_tool":
                action = params.get("action", "")
                if action.startswith("generate") and not params.get("coordinates"):
                    missing.append({"param": "coordinates", "label": "坐标点列表", "tool": step.tool})
            elif step.tool == "data_visualizer_tool":
                if not params.get("demand"):
                    missing.append({"param": "demand", "label": "制图需求描述", "tool": step.tool})
            elif step.tool == "map_tool":
                action = params.get("action") or ""
                if action == "load_vector_layer" and not params.get("table_name"):
                    missing.append({"param": "table_name", "label": "要加载的数据表", "tool": step.tool})
                elif not action and not params.get("table_name") and any(
                    k in (step.action or "") for k in ("加载", "图层", "显示", "上图")
                ):
                    # LLM 漏填 params（如"把图层加载到地图"）：加载类意图缺表名兜底
                    missing.append({"param": "table_name", "label": "要加载的数据表", "tool": step.tool})
        # 去重
        seen, deduped = set(), []
        for m in missing:
            key = (m["tool"], m["param"])
            if key not in seen:
                seen.add(key)
                deduped.append(m)
        return deduped

    def _merge_supplied(self, state: Dict[str, Any], pending: Dict[str, Any], user_supplied: Dict[str, Any]):
        """把用户补充的参数合并进剩余步骤的 params（仅覆盖空值/缺失键）。"""
        missing_params = {m.get("param") for m in pending.get("missing", []) if m.get("param")}
        for step in state.get("_remaining_steps") or []:
            if not isinstance(step, TaskStep):
                continue
            if step.params is None:
                step.params = {}
            for key, value in user_supplied.items():
                if key in missing_params and not step.params.get(key):
                    step.params[key] = value

    # ==================================================================
    # checkpoint 序列化
    # ==================================================================

    def _checkpoint_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """运行状态 → 可 JSON 序列化的 checkpoint。"""
        intent = state.get("intent_result")
        return {
            "user_message": state.get("user_message", ""),
            "chat_history": state.get("chat_history", []),
            "intent_result": intent.model_dump() if intent else None,
            "tool_results": state.get("tool_results", []),
            "tool_summaries": state.get("tool_summaries", []),
            "response": state.get("response", ""),
            "map_commands": state.get("map_commands", []),
            "cesium_commands": state.get("cesium_commands", []),
            "charts": state.get("charts", []),
            "report_url": state.get("report_url"),
            "view_hint": state.get("view_hint"),
            "remaining_steps": [s.model_dump() for s in (state.get("_remaining_steps") or [])],
            "done_steps": list(state.get("_done_steps") or []),
            "pending": state.get("_pending"),
            "session_id": state.get("_session_id", ""),
        }

    def _checkpoint_to_state(self, checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        """checkpoint → 运行状态（反序列化 IntentResult / TaskStep）。"""
        intent_raw = checkpoint.get("intent_result")
        intent = IntentResult.model_validate(intent_raw) if intent_raw else None
        steps = [
            TaskStep.model_validate(s)
            for s in (checkpoint.get("remaining_steps") or [])
            if isinstance(s, dict)
        ]
        return {
            "user_message": checkpoint.get("user_message", ""),
            "chat_history": checkpoint.get("chat_history", []),
            "intent_result": intent,
            "tool_results": checkpoint.get("tool_results", []),
            "tool_summaries": checkpoint.get("tool_summaries", []),
            "response": checkpoint.get("response", ""),
            "map_commands": checkpoint.get("map_commands", []),
            "cesium_commands": checkpoint.get("cesium_commands", []),
            "charts": checkpoint.get("charts", []),
            "report_url": checkpoint.get("report_url"),
            "view_hint": checkpoint.get("view_hint"),
            "error": None,
            "_remaining_steps": steps,
            "_done_steps": set(checkpoint.get("done_steps") or []),
            "_pending": checkpoint.get("pending"),
            "_session_id": checkpoint.get("session_id", ""),
        }

    def _remaining_or_full(self, state: Dict[str, Any], intent: IntentResult) -> List[TaskStep]:
        """resume 时返回剩余步骤；否则返回完整 plan。"""
        remaining = state.get("_remaining_steps") or []
        if remaining:
            return remaining
        steps = [s for s in intent.execution_plan if s.tool]
        state["_remaining_steps"] = steps
        return steps

    # ==================================================================
    # 事件发射 helper
    # ==================================================================

    async def _emit(self, run_id: str, type_: str, stage: str = "", title: str = "",
                    message: str = "", details: Optional[List[str]] = None, **payload):
        ev = RunEvent(
            type=type_, stage=stage, title=title, message=message,
            details=details or [], payload=payload,
        )
        await self.bus.publish(run_id, ev)

    async def _emit_tool_start(self, run_id: str, step: TaskStep):
        info = self.executor._format_plan_step(step)
        await self._emit(
            run_id, "tool_start", "tool_start",
            tool_name=step.tool,
            tool_label=info["tool_label"],
            step_id=step.step_id,
            title=f"步骤 {step.step_id} 开始：{step.action}",
            message=f"开始执行步骤 {step.step_id}：{step.action}",
            details=info["details"],
        )

    async def _emit_tool_result(self, run_id: str, step: Optional[TaskStep],
                                result: Dict, completed: int, total: int):
        tool_name = result.get("tool_name", "unknown_tool")
        result_data = result.get("result", {})
        await self._emit(
            run_id, "tool_result", "tool_result",
            tool_name=tool_name,
            tool_label=self.executor._humanize_tool_name(tool_name),
            step_id=step.step_id if step else None,
            completed_steps=completed,
            total_steps=total,
            title=f"步骤 {step.step_id if step else '?'} 完成：{step.action if step else tool_name}",
            message=self.executor._summarize_tool_result(tool_name, result_data),
            details=self.executor._extract_tool_result_details(tool_name, result_data),
        )

    async def _emit_final(self, run_id: str, result: Dict[str, Any]):
        run = self.store.get_run(run_id)
        result = dict(result or {})
        result["run_id"] = run_id
        result["run_status"] = (run or {}).get("status", COMPLETED)
        # final 事件直接带 result 载荷（与旧协议一致）
        await self._emit(
            run_id, "final", "final", title="final", message="",
            result=result,
        )

    def _final_details(self, state: Dict[str, Any]) -> List[str]:
        return [
            f"地图命令数：{len(state.get('map_commands', []))}",
            f"三维命令数：{len(state.get('cesium_commands', []))}",
            f"图表数：{len(state.get('charts', []))}",
            f"报告地址：{state.get('report_url') or '无'}",
        ]

    @staticmethod
    def _trunc(value: Any, limit: int = 160) -> str:
        text = str(value or "").strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"


# ----------------------------------------------------------------------
# pending 捕获包装：由 _guarded 捕获 PendingRequired → checkpoint + 事件
# ----------------------------------------------------------------------

async def _guarded(self, run_id: str, state: Dict[str, Any], resumed: bool):
    """带超时、pending 捕获与异常兜底的执行入口。"""
    try:
        await asyncio.wait_for(
            self._run_plan(run_id, state, resumed),
            timeout=self.timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"[RunEngine] run {run_id} timeout after {self.timeout}s")
        self.store.update_status(run_id, FAILED)
        await self._emit(
            run_id, "error", "error", title="执行超时",
            message=f"任务超过 {self.timeout // 60} 分钟未完成，已强制终止。",
            details=["若为长任务（如报告生成），请拆分需求后重试。"],
        )
        await self._emit_final(run_id, {
            "success": False,
            "response": f"任务超过 {self.timeout // 60} 分钟未完成，已强制终止。",
            "messages": [], "map_commands": [], "cesium_commands": [],
            "charts": [], "report_url": None, "intent_info": None,
            "requires_confirmation": False,
        })
    except PendingRequired as pending_exc:
        # 保存现场 → pending 事件 → run 正常终结（不占资源）
        await self._persist_pending(run_id, state, pending_exc)
        status = (AWAITING_CONFIRMATION if pending_exc.pending_type == "confirm"
                  else AWAITING_INPUT)
        self.store.update_status(run_id, status)
        await self._emit(
            run_id, "pending", "pending",
            title="等待用户确认" if status == AWAITING_CONFIRMATION else "等待补充信息",
            message=pending_exc.payload.get("question", "需要用户介入。"),
            details=[m.get("label", m.get("param", "")) for m in pending_exc.payload.get("missing", [])],
            pending_type=pending_exc.pending_type,
            run_status=status,
        )
        await self._emit_final(run_id, {
            "success": True,
            "response": pending_exc.payload.get("question", ""),
            "messages": [], "map_commands": [], "cesium_commands": [],
            "charts": [], "report_url": None,
            "intent_info": pending_exc.payload.get("plan"),
            "requires_confirmation": pending_exc.pending_type == "confirm",
            "pending_type": pending_exc.pending_type,
            "missing": pending_exc.payload.get("missing", []),
        })
    except asyncio.CancelledError:
        # 用户取消（_check_cancel 已发 cancelled 事件并置状态）；服务关闭场景直接置 FAILED
        run = self.store.get_run(run_id)
        if run and run["status"] != CANCELLED:
            self.store.update_status(run_id, FAILED)
        raise
    except Exception as e:
        logger.error(f"[RunEngine] run {run_id} fatal error: {e}", exc_info=True)
        self.store.update_status(run_id, FAILED)
        await self._emit(
            run_id, "error", "error", title="执行异常",
            message=f"系统执行出现错误：{e}",
            details=["请检查后端日志定位具体原因。"],
        )
        await self._emit_final(run_id, {
            "success": False,
            "response": f"系统执行出现错误：{e}",
            "messages": [], "map_commands": [], "cesium_commands": [],
            "charts": [], "report_url": None, "intent_info": None,
            "requires_confirmation": False,
        })
    finally:
        self.bus.mark_finished(run_id)


async def _persist_pending(self, run_id: str, state: Dict[str, Any], pending_exc: PendingRequired):
    """pending 现场持久化：checkpoint（完整 state）+ runs.pending_json。"""
    import json
    state["_pending"] = pending_exc.payload
    checkpoint = self._checkpoint_state(state)
    self.store.save_checkpoint(run_id, checkpoint)
    self.store.set_pending(run_id, json.dumps(pending_exc.payload, ensure_ascii=False),
                           AWAITING_CONFIRMATION if pending_exc.pending_type == "confirm" else AWAITING_INPUT)


# 将 _guarded / _persist_pending 挂到 RunEngine（模块内扩展，避免重复定义）
RunEngine._guarded = _guarded
RunEngine._persist_pending = _persist_pending
