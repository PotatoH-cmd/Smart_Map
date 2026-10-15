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
import json
import logging
import os
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from .intent_types import IntentResult, TaskStep
from .run_store import (
    AWAITING_CONFIRMATION,
    AWAITING_INPUT,
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    RESUMING,
    RunEvent,
    RunStore,
    get_run_store,
)
from .context_route import (
    ROUTE_META_VERSION,
    RoutingCtx,
    idempotency_key,
    route_for,
    route_meta,
)
from .event_bus import EventBus
from .qgis_workflows import match_recipe, extract_params
from .react_loop import ReActObserver, DEFAULT_READONLY_TOOLS
from core.obs import obs_trace, obs_trace_update, obs_span, obs_generation, obs_get_trace_id
from core.toolhub.param_validate import missing_required_for_prompt

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 600  # 10 分钟兜底

# 宕机断点续传（阶段8）：RUNNING 失活阈值与 watchdog 扫描周期
STALE_AFTER_SECONDS = int(os.environ.get("RUN_STALE_AFTER_SECONDS", "300"))
WATCHDOG_INTERVAL_SECONDS = int(os.environ.get("RUN_WATCHDOG_INTERVAL_SECONDS", "60"))

# 确认词（规则判定，不调 LLM；要求 pending 存在且消息短）
CONFIRM_KEYWORDS = ("确认", "执行", "继续", "好的", "可以", "按计划执行")
CONFIRM_MAX_LEN = 20


# 底图取值白名单（与 tools/map_tool.py::switch_layer、tools/cesium_tool.py::setBasemap 保持一致）
MAP_LAYERS = ("arcgis", "jcdt", "gf2024", "gf2025", "gf2026")
CESIUM_BASEMAPS = ("satellite", "osm", "tianditu", "jcdt", "gf2024", "gf2025")


def is_confirm_message(text: str) -> bool:
    """规则化确认判定：消息短且含确认词，或携带 __confirm__ 标记。"""
    t = (text or "").strip()
    if "__confirm__" in t:
        return True
    return len(t) <= CONFIRM_MAX_LEN and any(k in t for k in CONFIRM_KEYWORDS)


def normalize_basemap_choice(text: str, param: str) -> Optional[str]:
    """把用户对"要切哪种底图"的自然语言回答归一化为工具接受的取值。

    param 为 "layer"（2D map_tool）或 "basemap"（3D cesium_tool），两者白名单不同。
    无法识别时返回 None（调用方不 resume，交由新一轮请求处理）。
    """
    t = (text or "").strip().lower().replace(" ", "")
    if not t:
        return None
    allowed = CESIUM_BASEMAPS if param == "basemap" else MAP_LAYERS
    for value in allowed:
        if value in t:  # 用户直接写出 arcgis/jcdt/gf2024 等取值
            return value
    for year in ("2026", "2025", "2024"):
        if year in t and f"gf{year}" in allowed:
            return f"gf{year}"
    if any(k in t for k in ("街道", "基础", "路网", "矢量底图")):
        return "jcdt"
    if "天地图" in t:
        return "tianditu" if "tianditu" in allowed else None
    if any(k in t for k in ("卫星", "影像", "高分", "正射", "航拍")):
        return "satellite" if param == "basemap" else "arcgis"
    return None


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
        elif p in ("layer", "basemap"):
            normalized = normalize_basemap_choice(user_text, p)
            if normalized:
                supplied[p] = normalized
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
        self._obs_t0: Dict[str, float] = {}  # run_id -> perf_counter 起点（本地耗时统计）
        self._watchdog_task: Optional[asyncio.Task] = None  # 宕机 watch（阶段8）
        # 公共 ReAct 观察器（react_loop 模块，行为与原 _react_observe 逐字一致）
        self._react_observer = ReActObserver(summarize_fn=executor._summarize_tool_result)

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
        self._ensure_watchdog()
        self._touch(run_id, "start")
        # 可观测性：LangFuse trace 起点 + 本地计时起点
        obs_trace(run_id, session_id, user_message)
        self._obs_t0[run_id] = time.perf_counter()
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
            "_run_id": run_id,  # 供 intent_node 等下游做可观测性埋点
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
        # 可观测性：resume 续接（同进程幂等跳过；跨进程重启则新建 resumed trace）
        obs_trace(run_id, state.get("_session_id", ""), "(resumed) 用户已确认/补参，从断点继续")
        self._obs_t0[run_id] = time.perf_counter()
        logger.info(
            f"[RunEngine] resume run={run_id} confirm={confirm} supplied={list((user_supplied or {}).keys())}"
        )
        self._touch(run_id, "resume")
        return asyncio.create_task(self._guarded(run_id, state, resumed=True))

    # ==================================================================
    # 宕机断点续传（阶段8）：watchdog 定时器 + 懒检查 + 上下文重建
    # ==================================================================

    def start_watchdog(self) -> None:
        """注册进程级 watchdog 定时器（幂等：已运行则复用）。"""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(f"[RunEngine] watchdog started (interval={WATCHDOG_INTERVAL_SECONDS}s)")

    def stop_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        """周期扫描：把 RUNNING 且失活超时 → INTERRUPTED（可恢复）。"""
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            try:
                stale = self.store.find_stale_runs(STALE_AFTER_SECONDS, statuses=(RUNNING,))
                for run in stale:
                    self.store.mark_interrupted(run["run_id"])
                    logger.warning(
                        f"[RunEngine] watchdog: run={run['run_id']} RUNNING 失活超时 → INTERRUPTED"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[RunEngine] watchdog scan failed: {e}")

    def _touch(self, run_id: str, node: str) -> None:
        """运行时心跳：更新 heartbeat_at + last_node（断点定位），失败静默。"""
        try:
            self.store.update_heartbeat(run_id, node)
        except Exception as e:
            logger.warning(f"[RunEngine] heartbeat update failed (non-fatal): {e}")

    async def recover_session(self, session_id: str) -> Optional[tuple]:
        """新请求懒检查恢复：该会话最近 run 若为 IL故障 → 标可恢复并自动 resume。

        触发条件：run 状态为 INTERRUPTED（或 RUNNING 且失活超时），且存在有效 checkpoint。
        命中 → mark_resuming + resume（从断点续跑，不重做意图分析）。返回 (task, run_id) 或 None。
        """
        self._ensure_watchdog()
        last = self.store.get_last_run_by_session(session_id)
        if not last:
            return None
        rid = last["run_id"]
        st = last["status"]
        # RUNNING 未失活 → 正在前台执行，不劫持
        if st == RUNNING and not self.store.is_stale_updated_at(
            last.get("updated_at"), STALE_AFTER_SECONDS
        ):
            return None
        if st == RUNNING:
            self.store.mark_interrupted(rid)
            st = INTERRUPTED
            logger.warning(f"[RunEngine] lazy check: run={rid} RUNNING 失活 → INTERRUPTED")
        if st != INTERRUPTED or self.store.load_checkpoint(rid) is None:
            return None
        self.store.mark_resuming(rid)
        logger.info(f"[RunEngine] recover_session: run={rid} INTERRUPTED → RESUMING，自动续传")
        task = await self.resume(rid)
        return (task, rid) if task is not None else None

    def rebuild_stage_context(self, state: Dict[str, Any], stage: str) -> List[Any]:
        """用 route_for 重建某 stage 的上下文块（续传与活流程共用同一路由契约）。

        返回 ContextBlock 列表；payload 由块承载，供下游取用。仅作路由可观测，
        不写回 state（下游仍按 state 原有字段消费，零行为变更）。
        """
        results = state.get("tool_results") or []
        summaries = state.get("tool_summaries") or []
        ctx = RoutingCtx(
            session_id=state.get("_session_id", ""),
            user_message=state.get("user_message", ""),
            chat_history=state.get("chat_history") or [],
            tool_results=results,
            tool_summaries=summaries,
            workspace_summary="",
            facts_context="",
            critic_feedback=state.get("_critic_feedback") or "",
            pending=state.get("_pending"),
        )
        return route_for(stage, ctx)

    def _ensure_watchdog(self) -> None:
        """惰性启动 watchdog（首次 start/resume 时的 try 场景兜底）。"""
        if self._watchdog_task is None or self._watchdog_task.done():
            self.start_watchdog()

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
        missing = [] if resumed else self._detect_missing_params(
            intent_result, state.get("user_message", ""), state.get("view_hint"))
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
            # 阶段8：幂等 key 命中 → 该工具步骤已执行过（跨崩溃防重放副作用），直接跳过
            try:
                if self.store.check_idempotency_key(run_id, step.step_id):
                    logger.info(f"[RunEngine] idempotency hit, skip step={step.step_id} ({step.tool})")
                    continue
            except Exception:
                pass
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

        # ── ReAct 观察循环：结果不足时 LLM 决策追加工具步骤（env 开关，异常降级原流程）──
        if self._react_observer.enabled and ordered_results:
            for round_i in range(1, self._react_observer.max_rounds + 1):
                await self._check_cancel(run_id)
                new_step = await self._react_observe(run_id, state, ordered_results, round_i)
                if new_step is None:
                    break
                total_steps += 1
                await self._emit(
                    run_id, "status", "react_round",
                    title=f"ReAct 第 {round_i} 轮：{new_step.reasoning[:60]}",
                    message=f"观察到已有结果不足以完整回答，追加调用 "
                            f"{self.executor._humanize_tool_name(new_step.tool)}。",
                    details=[f"参数：{json.dumps(new_step.params, ensure_ascii=False)[:200]}"],
                    total_steps=total_steps,
                )
                await self._emit_tool_start(run_id, new_step)
                step_id, result = await self._invoke_step(run_id, state, new_step)
                completed += 1
                ordered_results.append({"step_id": step_id, **result})
                state["_done_steps"] = list(set(state.get("_done_steps") or []) | {step_id})
                await self._emit_tool_result(run_id, new_step, result, completed, total_steps)
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
                    title="知识库检索（数据库无结果，自动回退）",
                    message=self.executor._summarize_tool_result("knowledge_base_tool", kb_item.get("result", {})),
                    details=self.executor._extract_tool_result_details("knowledge_base_tool", kb_item.get("result", {})),
                    completed_steps=completed, total_steps=total_steps,
                )

        # ── 知识库无结果 → 联网搜索回退（确定性，不依赖 ReAct LLM 决策）──
        # 知识库覆盖不到的问题此前只会回复"系统无法提供…"，这里自动补一步联网搜索。
        web_step = self._decide_web_search_fallback(intent_result, state, ordered_results)
        if web_step is not None:
            total_steps += 1
            await self._emit(
                run_id, "status", "web_fallback",
                title="知识库无结果，自动联网搜索",
                message="知识库未检索到相关内容，改用联网搜索补充回答。",
                details=[f"搜索问题：{str(web_step.params.get('query', ''))[:120]}"],
                total_steps=total_steps,
            )
            await self._emit_tool_start(run_id, web_step)
            step_id, result = await self._invoke_step(run_id, state, web_step)
            completed += 1
            ordered_results.append({"step_id": step_id, **result})
            state["_done_steps"] = list(set(state.get("_done_steps") or []) | {step_id})
            await self._emit_tool_result(run_id, web_step, result, completed, total_steps)
            await self._check_cancel(run_id)

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
        # ── P1：质量评审闭环（打回 → 携反馈重生成；不命中/异常均保守放行）──
        state = await self._critic_review_stream(run_id, state, intent_result)
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

    # ------------------------------------------------------------------
    # P1：流式评审闭环（summarize 之后、workspace.record / final 之前）
    # ------------------------------------------------------------------

    async def _critic_review_stream(self, run_id: str, state: Dict[str, Any], intent_result) -> Dict[str, Any]:
        """单轮评审闭环：不命中/异常/通过 → 原样返回；打回 → 携反馈重生成（≤1 轮）。

        设计见《ReAct子图与Critic评审设计方案.md》§5.5：
          - 评审放线程池执行，不阻塞 asyncio 事件循环；
          - 事件沿用 status 通道（critic / critic_revise / critic_done），前端零改动；
          - 保守放行：任何失败都不阻断主流程，最差回退为原稿。
        """
        critic = getattr(self.executor, "critic", None)
        if critic is None:
            return state
        draft = state.get("response", "")
        try:
            if not critic.should_review(intent_result, state.get("tool_results"), draft):
                return state
            await self._emit(
                run_id, "status", "critic", title="质量评审中",
                message="正在对生成结果进行质量检查。",
            )
            loop = asyncio.get_event_loop()
            verdict = await loop.run_in_executor(
                None,
                lambda: critic.review(
                    user_message=state.get("user_message", ""),
                    intent_result=intent_result,
                    response_text=draft,
                    tool_results=state.get("tool_results"),
                    run_id=run_id,
                ),
            )
            if not verdict or verdict.get("passed") or critic.max_rounds < 1:
                return state
            await self._emit(
                run_id, "status", "critic_revise", title="根据评审意见修正答复",
                message="质量检查发现问题，正在修正答复。",
                details=list(verdict.get("issues") or []),
            )
            state["_critic_feedback"] = verdict.get("feedback") or ""
            state["response"] = ""
            state = self.executor._summarize_node(state)
            if not (state.get("response") or "").strip():
                state["response"] = draft  # 修正生成失败兜底：回退原稿
            await self._emit(
                run_id, "status", "critic_done", title="质量评审完成",
                message="已根据评审意见完成修正。",
            )
            return state
        except Exception as e:
            # 评审环节任何异常都不影响主流程；若响应已被清空则回退原稿
            logger.warning(f"[RunEngine] critic review failed (non-fatal): {e}")
            if not (state.get("response") or "").strip():
                state["response"] = draft
            state["_critic_feedback"] = None
            return state

    # ==================================================================
    # 工具调用
    # ==================================================================

    async def _invoke_step(self, run_id: str, state: Dict[str, Any], step: TaskStep,
                           extra_params: Optional[Dict] = None):
        """执行单个工具步骤：preflight → 调用 → postflight 验证（含耗时统计与观测埋点）。"""
        t_obs = time.perf_counter()
        node = f"tool:{step.tool}:{step.step_id}"
        self._touch(run_id, node)  # 阶段8：工具步骤落盘心跳 + 断点定位
        state["_last_node"] = node
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
                # A 档 A-1：执行内核收敛——TOOL_HUB 外呼 → 本地 adapter 回退
                # （tool_node / observe_node / 本方法共用 _invoke_single_step）
                raw = await self.executor._invoke_single_step(
                    state, step, params_override=params
                )
                # 结果整形契约：兼容裸 adapter 返回（无 tool_name 包裹）
                result = {"tool_name": step.tool, "result": raw.get("result", raw) if isinstance(raw, dict) else raw}
                # 参数 Schema 校验失败（Phase 1）：工具未执行，postflight 不适用，
                # 与 preflight critical 同语义单独发 verification 事件。
                _r = result.get("result")
                schema_ver = _r.get("_verification") if isinstance(_r, dict) else None
                if isinstance(schema_ver, dict) and schema_ver.get("phase") == "param_schema":
                    if schema_ver.get("risk") == "critical":
                        await self._emit_verification(run_id, step, schema_ver)
                else:
                    # postflight（阶段2）
                    verification = self._run_postflight(step.tool, result.get("result", {}), state)
                    if verification is not None:
                        result["result"]["_verification"] = verification
                        if verification.get("risk") == "critical":
                            await self._emit_verification(run_id, step, verification)
        # 可观测性：耗时落 result（事件流/聚合共用）+ LangFuse TOOL span
        result["elapsed_ms"] = int((time.perf_counter() - t_obs) * 1000)
        _rd = result.get("result") if isinstance(result.get("result"), dict) else {}
        _out = {"success": _rd.get("success"), "elapsed_ms": result["elapsed_ms"]}
        if _rd.get("error"):
            _out["error"] = str(_rd["error"])[:500]
        obs_span(
            run_id, step.tool,
            input=step.params or {}, output=_out,
            level="ERROR" if _rd.get("success") is False else "DEFAULT",
            status_message=str(_rd.get("error", ""))[:300] or "",
            elapsed_ms=result["elapsed_ms"],
        )
        # 阶段8：工具步骤幂等 key（resume 命中→跳过重跑复用结果）。读失败不影响主流程。
        try:
            self.store.record_idempotency_key(
                run_id, step.step_id,
                idempotency_key(run_id, step.step_id, step.params or {}),
            )
        except Exception as e:
            logger.warning(f"[RunEngine] record idempotency key failed (non-fatal): {e}")
        # FC 步骤：tool_call_id 上浮到结果根（供观测器以 role:"tool" 协议回填）
        if getattr(step, "tool_call_id", None):
            result["tool_call_id"] = step.tool_call_id
        return step.step_id, result

    async def _invoke_step_bare(self, step, extra_params: Optional[Dict] = None):
        """供 executor._fallback_knowledge_search 回调使用的裸调用（无事件、无校验）。

        fallback 只会构造 knowledge_base_tool 假 step，这里按通用路径处理即可。
        A 档 A-1：委托公共执行内核（TOOL_HUB → adapter），与 tool_node 的 KB 回退待遇一致。
        """
        raw = await self.executor._invoke_single_step({}, step, extra_params=extra_params)
        return {"tool_name": step.tool, "result": raw.get("result", raw) if isinstance(raw, dict) else raw}

    # ==================================================================
    # ReAct 观察循环（Agent Search 配套：工具结果反馈 LLM 决策追加步骤）
    # ==================================================================

    # ReAct 允许追加的工具白名单（兼容别名；实际白名单见 react_loop.DEFAULT_READONLY_TOOLS）
    REACT_ALLOWED_TOOLS = DEFAULT_READONLY_TOOLS

    async def _react_observe(self, run_id: str, state: Dict[str, Any],
                             ordered_results: List, round_i: int) -> Optional[TaskStep]:
        """ReAct 观察决策（薄包装）：实现全部委托 react_loop.ReActObserver。

        - 短路 / 白名单 / 异常降级 / obs 埋点语义与原实现逐字一致
        - 返回 TaskStep 时由调用方执行并 emit react_round 事件
        """
        return self._react_observer.observe(
            user_message=state.get("user_message", ""),
            ordered_results=ordered_results,
            round_i=round_i,
            run_id=run_id,
        )

    # ==================================================================
    # 知识库无结果 → 联网搜索回退（确定性决策，不调 LLM）
    # ==================================================================

    # 允许联网回退的意图（知识类问题；数据查询/地图等有结果的场景不回退）
    _WEB_FALLBACK_INTENTS = ("knowledge_search", "unknown", "cross_intent")

    def _decide_web_search_fallback(self, intent_result, state: Dict[str, Any],
                                    ordered_results: List) -> Optional[TaskStep]:
        """知识库检索无结果时改用联网搜索；返回待执行步骤或 None。

        触发条件（全部满足）：知识类意图；计划中有 knowledge_base_tool 且全部结果为空；
        其他工具未产出实质性结果；尚未执行过联网搜索；用户原问题非空。
        """
        primary = getattr(intent_result, "primary_intent", None)
        primary_val = primary.value if hasattr(primary, "value") else str(primary or "")
        if primary_val not in self._WEB_FALLBACK_INTENTS:
            return None
        kb_results = [
            r for r in ordered_results
            if isinstance(r, dict) and r.get("tool_name") == "knowledge_base_tool"
        ]
        if not kb_results:
            return None
        if any(
            isinstance(r, dict) and r.get("tool_name") == "web_search_tool"
            for r in ordered_results
        ):
            return None
        if any(not self._kb_result_empty(r.get("result")) for r in kb_results):
            return None  # 知识库有结果 → 不需要联网
        others = [
            r for r in ordered_results
            if isinstance(r, dict) and r.get("tool_name") != "knowledge_base_tool"
        ]
        if any(self._has_substantive_result(r) for r in others):
            return None  # 其他工具已产出实质结果（数据库数据/地图命令/报告等）
        query = (state.get("user_message") or "").strip()
        if not query:
            return None
        next_id = max(
            (r.get("step_id", 0) for r in ordered_results if isinstance(r, dict)),
            default=0,
        ) + 1
        logger.info(f"[RunEngine] 知识库无结果，自动联网搜索: query={query[:80]}")
        return TaskStep(
            step_id=next_id,
            action="知识库无结果，改用联网搜索补充",
            tool="web_search_tool",
            params={"query": query},
            reasoning="知识库未检索到相关内容，自动联网搜索补充回答",
            expected_output="联网搜索答案与网络来源",
        )

    @staticmethod
    def _kb_result_empty(result) -> bool:
        """知识库检索结果是否为"空"（成功但无内容）。失败（执行错误）不算空。"""
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
        res = item.get("result")
        if not isinstance(res, dict) or res.get("success") is not True:
            return False
        if res.get("data") not in (None, [], {}, ""):
            return True
        return bool(
            res.get("map_command") or res.get("map_commands") or res.get("cesium_command")
            or res.get("charts") or res.get("report_url") or res.get("answer")
        )

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

    def _detect_missing_params(self, intent: IntentResult, user_message: str,
                               view_hint: Optional[str] = None) -> List[Dict[str, Any]]:
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
                elif action == "switch_layer" and (params.get("layer") or "") not in MAP_LAYERS:
                    # 用户只说"切换底图"未指明切哪种（LLM 会填非法值或漏填，工具默认 arcgis）：
                    # 改为反问用户，避免静默切错底图或直接报错。
                    if view_hint == "cesium":
                        # 3D 视图下底图切换由 cesium_tool 承担，纠正视图绑定，
                        # 否则 resume 后只下发 map_command，3D 界面不会有任何变化。
                        step.tool = "cesium_tool"
                        step.params = {"action": "setBasemap"}
                        missing.append({
                            "param": "basemap", "tool": step.tool,
                            "label": "要切换的3D底图（卫星影像 / 街道图 / 天地图 / OSM）",
                        })
                    else:
                        params.pop("layer", None)
                        missing.append({
                            "param": "layer", "tool": step.tool,
                            "label": "要切换的底图（卫星影像 / 2024年 / 2025年 / 2026年高分影像 / 基础底图）",
                        })
                elif not action and not params.get("table_name") and any(
                    k in (step.action or "") for k in ("加载", "图层", "显示", "上图")
                ):
                    # LLM 漏填 params（如"把图层加载到地图"）：加载类意图缺表名兜底
                    missing.append({"param": "table_name", "label": "要加载的数据表", "tool": step.tool})
            elif step.tool == "cesium_tool":
                action = params.get("action") or ""
                if action == "setBasemap" and (params.get("basemap") or "") not in CESIUM_BASEMAPS:
                    params.pop("basemap", None)
                    missing.append({
                        "param": "basemap", "tool": step.tool,
                        "label": "要切换的3D底图（卫星影像 / 街道图 / 天地图 / OSM）",
                    })
            # ── schema.required 基线（Phase 1）：声明驱动的通用必填检测（新工具自动覆盖）──
            # 先按现有规则归一化（如 weather city 从用户消息兜底抽取）再查缺失，避免误反问；
            # 机器参数（PROMPT_SKIP_PARAMS）不参与反问，其缺失由执行前 schema 校验负责。
            get_params = getattr(self.executor, "_get_tool_parameters", None)
            if get_params:
                normalize = getattr(self.executor, "_normalize_tool_params", None)
                check_params = (normalize(step.tool, params, user_message, step)
                                if normalize else params)
                for m in missing_required_for_prompt(get_params(step.tool), check_params):
                    missing.append({"param": m["param"], "label": m["label"], "tool": step.tool})
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
        """运行状态 → 可 JSON 序列化的 checkpoint（含 route_meta 版本，兼容旧读）。"""
        intent = state.get("intent_result")
        last_node = state.get("_last_node") or ""
        return {
            "version": ROUTE_META_VERSION,
            "stage_cursor": last_node,
            "route": {last_node: route_meta(last_node)} if last_node else {},
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
            "last_node": last_node,
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
            "_last_node": checkpoint.get("last_node") or checkpoint.get("stage_cursor") or "",
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
        rd = result_data if isinstance(result_data, dict) else {}
        await self._emit(
            run_id, "tool_result", "tool_result",
            tool_name=tool_name,
            tool_label=self.executor._humanize_tool_name(tool_name),
            step_id=step.step_id if step else None,
            completed_steps=completed,
            total_steps=total,
            elapsed_ms=result.get("elapsed_ms"),
            success=rd.get("success"),
            title=f"步骤 {step.step_id if step else '?'} 完成：{step.action if step else tool_name}",
            message=self.executor._summarize_tool_result(tool_name, result_data),
            details=self.executor._extract_tool_result_details(tool_name, result_data),
        )

    async def _emit_final(self, run_id: str, result: Dict[str, Any]):
        run = self.store.get_run(run_id)
        result = dict(result or {})
        result["run_id"] = run_id
        result["run_status"] = (run or {}).get("status", COMPLETED)
        # 可观测性：LangFuse trace 终态（output + status）
        obs_trace_update(
            run_id,
            output={"success": result.get("success"),
                    "response": str(result.get("response", ""))[:2000],
                    "map_commands": len(result.get("map_commands") or []),
                    "report_url": result.get("report_url")},
            status=result.get("run_status", ""),
            metadata={"duration_ms": self._obs_elapsed_ms(run_id)},
        )
        # final 事件直接带 result 载荷（与旧协议一致）
        await self._emit(
            run_id, "final", "final", title="final", message="",
            result=result,
        )

    def _obs_elapsed_ms(self, run_id: str) -> Optional[int]:
        """run 已运行时长（从最近一次 start/resume 起算，未开始返回 None）。"""
        t0 = self._obs_t0.get(run_id)
        return int((time.perf_counter() - t0) * 1000) if t0 is not None else None

    def _obs_finish(self, run_id: str, state: Dict[str, Any]):
        """run 终态收尾（_guarded finally 调用）：本地 duration/intent 落库 + trace 兜底 end。"""
        try:
            t0 = self._obs_t0.pop(run_id, None)
            run = self.store.get_run(run_id)
            if t0 is not None and run and run["status"] in (COMPLETED, FAILED, CANCELLED):
                duration_ms = int((time.perf_counter() - t0) * 1000)
                intent = state.get("intent_result")
                # primary_intent 经 pydantic 序列化为 str（无 .value），统一 str() 取值
                intent_name = str(intent.primary_intent) if intent is not None else ""
                self.store.set_run_metrics(
                    run_id, duration_ms, intent_name, obs_get_trace_id(run_id))
        except Exception as e:
            logger.warning(f"[RunEngine] obs finish metrics failed: {e}")
        # 兜底：_emit_final 未触发（如 cancelled 路径）时确保 trace 关闭（幂等）
        status = ""
        try:
            status = (self.store.get_run(run_id) or {}).get("status", "")
        except Exception:
            pass
        obs_trace_update(run_id, status=status)

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
        self._obs_finish(run_id, state)


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
