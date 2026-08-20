"""
test_run_engine.py — 阶段7：RunEngine 状态机全流转。

用假工具 / 假 executor 注入，覆盖：
  RUNNING → COMPLETED（工具执行 + final）
  RUNNING → AWAITING_CONFIRMATION → resume(confirm) → COMPLETED
  RUNNING → AWAITING_INPUT → resume(supplied) → COMPLETED（参数合并进剩余步骤）
  RUNNING → CANCELLED（中断点停止 + cancelled 事件）
  RUNNING → FAILED（超时兜底 / 异常兜底）
  直答路径（无工具步骤）
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
