"""
test_run_engine.py — 阶段7：RunEngine 状态机全流转。

用假工具 / 假 executor 注入，覆盖：
  RUNNING → COMPLETED（工具执行 + final）
  RUNNING → AWAITING_CONFIRMATION → resume(confirm) → COMPLETED
  RUNNING → AWAITING_INPUT → resume(supplied) → COMPLETED（参数合并进剩余步骤）
  RUNNING → CANCELLED（中断点停止 + cancelled 事件）
  RUNNING → FAILED（超时兜底 / 异常兜底）
  直答路径（无工具步骤）
  P1 流式评审闭环：critic 打回 → critic_revise 事件 → 携反馈重生成 → critic_done → final
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.run_store import (
    RunStore,
    RUNNING, AWAITING_CONFIRMATION, AWAITING_INPUT,
    COMPLETED, FAILED, CANCELLED,
)
from agents.event_bus import EventBus
from agents.run_engine import RunEngine
from agents.intent_types import IntentType, IntentResult, TaskStep


@pytest.fixture(autouse=True)
def _disable_react_loop(monkeypatch):
    """旧用例保持改造前行为（ReAct 循环在 test_react_loop.py 专项覆盖，避免真实 LLM 调用）。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")


# ----------------------------------------------------------------------
# 假件注入
# ----------------------------------------------------------------------

class FakeAdapter:
    """假工具适配器：可注入延迟 / 结果 / 错误。"""

    def __init__(self, delay=0.0, result=None, error=None):
        self.delay = delay
        self.result = result
        self.error = error
        self.calls = []

    def invoke(self, params):
        self.calls.append(dict(params or {}))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            return {"result": {"success": False, "error": self.error}}
        return {"result": self.result or {"success": True, "data": [{"count": 5}]}}


class FakeIntentAgent:
    def get_required_tools(self, intent_result):
        seen, tools = set(), []
        for step in intent_result.execution_plan:
            if step.tool and step.tool not in seen:
                seen.add(step.tool)
                tools.append(step.tool)
        return tools


class FakeExecutor:
    """只实现 RunEngine 依赖面的最小 executor。"""

    def __init__(self, intent_result, adapter=None):
        self.intent_result = intent_result
        self.adapter = adapter or FakeAdapter()
        self.intent_agent = FakeIntentAgent()
        self.workspace = None

    def _intent_node(self, state, workspace_summary=""):
        state["intent_result"] = self.intent_result
        return state

    def _format_plan_step(self, s):
        return {"step_id": s.step_id, "action": s.action,
                "tool_label": s.tool, "details": []}

    def _serialize_intent_info(self, intent):
        return {"primary_intent": str(intent.primary_intent), "plan": []}

    def _humanize_tool_name(self, tool):
        return tool

    def _summarize_tool_result(self, tool_name, result_data):
        return f"{tool_name} 完成"

    def _extract_tool_result_details(self, tool_name, result_data):
        return []

    def _get_tool_adapter(self, tool):
        return self.adapter

    def _normalize_tool_params(self, tool, params, user_message, step):
        return dict(params or {})

    async def _invoke_single_step(self, state, step, extra_params=None, params_override=None):
        """公共执行内核（与 TaskExecutor._invoke_single_step 同形，A 档 A-1 收敛后 RunEngine 依赖）。"""
        params = params_override if params_override is not None else (step.params or {})
        if extra_params:
            params.update(extra_params)
        adapter = self._get_tool_adapter(step.tool)
        if adapter is None:
            return {"tool_name": step.tool, "result": {"success": False, "error": f"工具 {step.tool} 不可用"}}
        return await asyncio.get_event_loop().run_in_executor(None, adapter.invoke, params)

    async def _fallback_knowledge_search(self, ordered_results, user_message, invoke_bare):
        return ordered_results

    def _check_data_sufficiency(self, indexed_results):
        return True, ""

    def _build_report_variables(self, indexed_results, user_message, intent):
        return {}

    def _extract_node(self, state):
        return state

    def _summarize_node(self, state):
        state["response"] = "这是最终回复"
        return state

    def _build_stream_result(self, state):
        return {
            "success": True, "response": state.get("response", ""),
            "messages": [], "map_commands": state.get("map_commands", []),
            "cesium_commands": [], "charts": [], "report_url": None,
            "intent_info": None, "requires_confirmation": False,
        }


class FakeCritic:
    """可控评审假件（流式路径）：verdicts 依次弹出；reviewable=False 时零开销直通。"""

    def __init__(self, verdicts=None, reviewable=True, max_rounds=1):
        self.verdicts = list(verdicts or [])
        self.reviewable = reviewable
        self.max_rounds = max_rounds
        self.review_calls = 0

    def should_review(self, intent_result, tool_results, response_text):
        return self.reviewable

    def review(self, **kwargs):
        self.review_calls += 1
        if not self.verdicts:
            return None
        return self.verdicts.pop(0)


class FakeCriticExecutor(FakeExecutor):
    """带 critic 的最小 executor：summarize 模拟真实反馈消费契约（生成后清空反馈）。"""

    def __init__(self, intent_result, critic, adapter=None):
        super().__init__(intent_result, adapter=adapter)
        self.critic = critic
        self.summarize_calls = 0

    def _summarize_node(self, state):
        self.summarize_calls += 1
        if state.get("_critic_feedback"):
            state["response"] = "修正后的回复"
            state["_critic_feedback"] = None
        else:
            state["response"] = "这是最终回复"
        return state


def _make_intent(confirmation=False, plan=None, intent_type=IntentType.DATA_QUERY):
    return IntentResult(
        primary_intent=intent_type,
        confidence=0.9,
        entities=[],
        task_context="测试任务",
        execution_plan=plan or [],
        requires_confirmation=confirmation,
        suggestions=[],
    )


def _make_engine(tmp_path, intent, adapter=None, timeout=600):
    store = RunStore(db_path=str(tmp_path / "r.db"))
    exe = FakeExecutor(intent, adapter=adapter)
    engine = RunEngine(exe, store=store, bus=EventBus(store), gateway=None, timeout=timeout)
    return engine, store, exe


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 正常完成：工具步骤 + final + checkpoint 清理
# ----------------------------------------------------------------------

def test_full_flow_completed(tmp_path):
    plan = [TaskStep(step_id=1, action="查询", tool="postgresql_tool",
                     params={"operation": "query"}, reasoning="", expected_output="")]
    engine, store, exe = _make_engine(tmp_path, _make_intent(plan=plan))

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    events = store.get_events("r1")
    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert "intent" in types and "plan" in types
    assert "tool_start" in types and "tool_result" in types
    assert types[-1] == "final"
    # seq 严格递增（断线补拉契约）
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # final 带 result 载荷
    final = events[-1]
    assert final["result"]["success"] is True
    assert final["result"]["response"] == "这是最终回复"
    # 完成后 checkpoint 清理
    assert store.load_checkpoint("r1") is None
    # 工具收到参数
    assert exe.adapter.calls == [{"operation": "query"}]


def test_direct_reply_no_tools(tmp_path):
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=[]))

    async def main():
        task = await engine.start("r1", "s1", "你好")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    types = [e["type"] for e in store.get_events("r1")]
    assert "tool_start" not in types and "tool_result" not in types
    assert types[-1] == "final"


# ----------------------------------------------------------------------
# pending：确认（confirm）→ resume → 完成
# ----------------------------------------------------------------------

def test_pending_confirm_and_resume(tmp_path):
    engine, store, _ = _make_engine(tmp_path, _make_intent(confirmation=True))

    async def main():
        task = await engine.start("r1", "s1", "执行计划")
        await task
    _run(main())

    # 第一阶段：AWAITING_CONFIRMATION + checkpoint + pending 事件
    assert store.get_run("r1")["status"] == AWAITING_CONFIRMATION
    pending = store.get_pending_by_session("s1")
    assert pending["run_id"] == "r1"
    assert pending["pending"]["pending_type"] == "confirm"
    assert store.load_checkpoint("r1") is not None
    first_events = store.get_events("r1")
    assert first_events[-2]["type"] == "pending"
    assert first_events[-1]["type"] == "final"
    assert first_events[-1]["result"]["pending_type"] == "confirm"
    assert first_events[-1]["result"]["requires_confirmation"] is True

    # 第二阶段：resume(confirm=True) → 从断点继续 → COMPLETED
    async def main2():
        task = await engine.resume("r1", confirm=True)
        await task
    _run(main2())

    run = store.get_run("r1")
    assert run["status"] == COMPLETED
    assert run["pending_json"] is None
    assert store.load_checkpoint("r1") is None
    assert store.get_pending_by_session("s1") is None
    # resume 生命周期的新 final 不带 pending_type
    new_events = store.get_events("r1")
    assert new_events[-1]["type"] == "final"
    assert new_events[-1]["result"].get("pending_type") is None


# ----------------------------------------------------------------------
# pending：缺参（input）→ resume 补参 → 完成
# ----------------------------------------------------------------------

def test_pending_input_and_resume_merges_params(tmp_path):
    plan = [TaskStep(step_id=1, action="加载图层", tool="map_tool",
                     params={"action": "load_vector_layer"}, reasoning="", expected_output="")]
    engine, store, exe = _make_engine(tmp_path, _make_intent(plan=plan))

    async def main():
        task = await engine.start("r1", "s1", "加载数据")
        await task
    _run(main())

    # 缺 table_name → AWAITING_INPUT
    assert store.get_run("r1")["status"] == AWAITING_INPUT
    pending = store.get_pending_by_session("s1")
    missing_params = [m["param"] for m in pending["pending"]["missing"]]
    assert "table_name" in missing_params

    # resume 补参：合并进剩余步骤 params → 执行 → 完成
    async def main2():
        task = await engine.resume("r1", user_supplied={"table_name": "ceshen"})
        await task
    _run(main2())

    assert store.get_run("r1")["status"] == COMPLETED
    assert exe.adapter.calls == [{"action": "load_vector_layer", "table_name": "ceshen"}]


def test_pending_input_when_llm_omits_params(tmp_path):
    """LLM 漏填 params（如'把图层加载到地图'）→ 缺参 pending 兜底。"""
    plan = [TaskStep(step_id=1, action="加载图层到地图", tool="map_tool",
                     params={}, reasoning="", expected_output="")]
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=plan))

    async def main():
        task = await engine.start("r1", "s1", "把图层加载到地图")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == AWAITING_INPUT
    pending = store.get_pending_by_session("s1")
    missing_params = [m["param"] for m in pending["pending"]["missing"]]
    assert "table_name" in missing_params


def test_map_tool_normalize_defaults_action():
    """补参 resume 只带 table_name 时，规范化默认视为 load_vector_layer。"""
    from agents.task_executor import TaskExecutor
    te = TaskExecutor.__new__(TaskExecutor)
    # 只有 table_name：默认补 action
    assert te._normalize_tool_params(
        "map_tool", {"table_name": "ceshen"}, "msg", None
    ) == {"table_name": "ceshen", "action": "load_vector_layer"}
    # 已有 action 不覆盖
    assert te._normalize_tool_params(
        "map_tool", {"table_name": "ceshen", "action": "add_marker"}, "msg", None
    ) == {"table_name": "ceshen", "action": "add_marker"}


# ----------------------------------------------------------------------
# 取消：中断点停止 + cancelled 事件
# ----------------------------------------------------------------------

def test_cancel_between_steps(tmp_path):
    plan = [TaskStep(step_id=1, action="慢查询", tool="postgresql_tool",
                     params={}, reasoning="", expected_output="")]
    adapter = FakeAdapter(delay=0.4)
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=plan), adapter=adapter)

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await asyncio.sleep(0.05)
        store.set_cancelled("r1")
        results = await asyncio.gather(task, return_exceptions=True)
        return results
    results = _run(main())

    assert store.get_run("r1")["status"] == CANCELLED
    types = [e["type"] for e in store.get_events("r1")]
    assert "cancelled" in types


def test_cancel_idempotent_terminal(tmp_path):
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=[]))

    async def main():
        task = await engine.start("r1", "s1", "你好")
        await task
    _run(main())
    assert store.get_run("r1")["status"] == COMPLETED
    # 终态后取消标记（幂等：不抛异常）
    store.set_cancelled("r1")
    assert store.get_run("r1")["status"] == CANCELLED


# ----------------------------------------------------------------------
# 超时兜底：强制 FAILED
# ----------------------------------------------------------------------

def test_timeout_failed(tmp_path):
    plan = [TaskStep(step_id=1, action="慢查询", tool="postgresql_tool",
                     params={}, reasoning="", expected_output="")]
    adapter = FakeAdapter(delay=0.4)
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=plan),
                                    adapter=adapter, timeout=0.05)

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == FAILED
    types = [e["type"] for e in store.get_events("r1")]
    assert "error" in types
    final = store.get_events("r1")[-1]
    assert final["type"] == "final"
    assert final["result"]["success"] is False
    assert "超" in final["result"]["response"] or "超时" in final["result"]["response"]


# ----------------------------------------------------------------------
# 异常兜底：FAILED + error 事件
# ----------------------------------------------------------------------

def test_exception_failed(tmp_path):
    engine, store, exe = _make_engine(tmp_path, _make_intent(plan=[]))

    def boom(state, workspace_summary=""):
        raise ValueError("模拟意图分析崩溃")
    exe._intent_node = boom

    async def main():
        task = await engine.start("r1", "s1", "你好")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == FAILED
    types = [e["type"] for e in store.get_events("r1")]
    assert "error" in types and types[-1] == "final"


# ----------------------------------------------------------------------
# resume 边界：无 checkpoint 时不恢复
# ----------------------------------------------------------------------

def test_resume_without_checkpoint(tmp_path):
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan=[]))

    async def main():
        return await engine.resume("no_such_run", confirm=True)
    task = _run(main())
    assert task is None
    assert store.get_run("no_such_run") is None


# ----------------------------------------------------------------------
# P1：流式评审闭环（critic / critic_revise / critic_done 事件 + 修正重生成）
# ----------------------------------------------------------------------

def _make_critic_engine(tmp_path, intent, critic, adapter=None):
    store = RunStore(db_path=str(tmp_path / "r.db"))
    exe = FakeCriticExecutor(intent, critic, adapter=adapter)
    engine = RunEngine(exe, store=store, bus=EventBus(store), gateway=None, timeout=600)
    return engine, store, exe


def test_stream_critic_reject_revises_before_final(tmp_path):
    """打回：critic → critic_revise → 携反馈重生成 → critic_done → done → final（修正版）。"""
    plan = [TaskStep(step_id=1, action="查询", tool="postgresql_tool",
                     params={"operation": "query"}, reasoning="", expected_output="")]
    critic = FakeCritic(verdicts=[{
        "passed": False, "severity": "major",
        "issues": ["数字与工具结果不一致"], "feedback": "改为工具结果中的数字",
    }])
    engine, store, exe = _make_critic_engine(tmp_path, _make_intent(plan=plan), critic)

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    events = store.get_events("r1")
    stages = [e["stage"] for e in events if e["type"] == "status"]
    # 事件顺序：critic → critic_revise → critic_done → done（均在 final 之前）
    assert stages.index("critic") < stages.index("critic_revise") < stages.index("critic_done")
    assert stages.index("critic_done") < stages.index("done")
    assert events[-1]["type"] == "final"
    assert events[-1]["result"]["response"] == "修正后的回复"
    # critic_revise 事件携带问题清单
    revise = [e for e in events if e["stage"] == "critic_revise"][0]
    assert revise["details"] == ["数字与工具结果不一致"]
    # 一次评审、两次生成（初版 + 修正）
    assert critic.review_calls == 1
    assert exe.summarize_calls == 2


def test_stream_critic_not_triggered_zero_overhead(tmp_path):
    """触发判定不命中 → 不调评审、无 critic 事件、不重生成。"""
    plan = [TaskStep(step_id=1, action="查询", tool="postgresql_tool",
                     params={}, reasoning="", expected_output="")]
    critic = FakeCritic(reviewable=False)
    engine, store, exe = _make_critic_engine(tmp_path, _make_intent(plan=plan), critic)

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await task
    _run(main())

    events = store.get_events("r1")
    stages = [e["stage"] for e in events if e["type"] == "status"]
    assert "critic" not in stages and "critic_revise" not in stages
    assert critic.review_calls == 0
    assert exe.summarize_calls == 1
    assert events[-1]["result"]["response"] == "这是最终回复"


def test_stream_critic_degrades_to_pass(tmp_path):
    """评审返回 None（异常降级）→ 已发起评审但不修正，原稿直出。"""
    plan = [TaskStep(step_id=1, action="查询", tool="postgresql_tool",
                     params={}, reasoning="", expected_output="")]
    critic = FakeCritic(verdicts=[None])
    engine, store, exe = _make_critic_engine(tmp_path, _make_intent(plan=plan), critic)

    async def main():
        task = await engine.start("r1", "s1", "查数据")
        await task
    _run(main())

    events = store.get_events("r1")
    stages = [e["stage"] for e in events if e["type"] == "status"]
    assert "critic" in stages  # 已发起评审
    assert "critic_revise" not in stages and "critic_done" not in stages
    assert exe.summarize_calls == 1
    assert events[-1]["result"]["response"] == "这是最终回复"


# ----------------------------------------------------------------------
# Phase 1：缺参检测 schema 基线（声明驱动，机器参数不反问）
# ----------------------------------------------------------------------

def test_detect_missing_params_schema_baseline(tmp_path):
    plan = [TaskStep(step_id=1, action="生成报告", tool="caisha_report_tool",
                     params={}, reasoning="", expected_output="")]
    intent = _make_intent(plan=plan)
    engine, store, exe = _make_engine(tmp_path, intent)
    exe._get_tool_parameters = lambda tool: [
        {"name": "site_name", "type": "string", "description": "砂场名称", "required": True},
        {"name": "operation", "type": "string", "required": True},  # 机器参数：不反问
    ]
    missing = engine._detect_missing_params(intent, "生成郝楼砂场的报告", None)
    assert {"param": "site_name", "label": "砂场名称", "tool": "caisha_report_tool"} in missing
    assert all(m["param"] != "operation" for m in missing)
    # 参数已提供 → 不反问
    plan[0].params["site_name"] = "郝楼砂场"
    assert engine._detect_missing_params(intent, "生成郝楼砂场的报告", None) == []


def test_detect_missing_params_without_schema_support(tmp_path):
    """无 _get_tool_parameters 的旧 executor 不因 schema 基线崩溃（向后兼容）。"""
    plan = [TaskStep(step_id=1, action="生成报告", tool="caisha_report_tool",
                     params={}, reasoning="", expected_output="")]
    intent = _make_intent(plan=plan)
    engine, store, exe = _make_engine(tmp_path, intent)
    assert engine._detect_missing_params(intent, "msg", None) == []
