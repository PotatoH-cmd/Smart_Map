"""
test_react_loop.py — RunEngine ReAct 观察循环单元 + 集成测试。

覆盖：
  - _react_observe 短路（全成功且有实质数据、无知识检索 → 不调 LLM）
  - _react_observe add_step 决策（白名单工具 → TaskStep）
  - _react_observe 非法决策拒绝（非白名单 / 空 params / LLM 输出非 JSON）
  - _run_plan 集成：REACT_LOOP_ENABLED=1 追加步骤 + react_round 事件
  - _run_plan 集成：REACT_LOOP_ENABLED=0 行为与基线一致（无 react_round、不调 LLM）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.run_store import RunStore, COMPLETED
from agents.event_bus import EventBus
from agents.run_engine import RunEngine
from agents.intent_types import IntentType, IntentResult, TaskStep


# ----------------------------------------------------------------------
# 假件注入（对齐 test_run_engine.py 的 Fake 模式）
# ----------------------------------------------------------------------

class FakeAdapter:
    def __init__(self, results=None):
        self.results = results or []  # 依次返回；耗尽后返回兜底成功
        self.calls = []

    def invoke(self, params):
        self.calls.append(dict(params or {}))
        if self.results:
            r = self.results.pop(0)
            return {"result": r}
        return {"result": {"success": True, "data": [{"count": 5}]}}


class FakeIntentAgent:
    def get_required_tools(self, intent_result):
        seen, tools = set(), []
        for step in intent_result.execution_plan:
            if step.tool and step.tool not in seen:
                seen.add(step.tool)
                tools.append(step.tool)
        return tools


class FakeExecutor:
    def __init__(self, intent_result, adapter=None):
        self.intent_result = intent_result
        self.adapter = adapter or FakeAdapter()
        self.intent_agent = FakeIntentAgent()
        self.workspace = None

    def _intent_node(self, state, workspace_summary=""):
        state["intent_result"] = self.intent_result
        return state

    def _format_plan_step(self, s):
        return {"step_id": s.step_id, "action": s.action, "tool_label": s.tool, "details": []}

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
        state["response"] = "最终回复"
        return state

    def _build_stream_result(self, state):
        return {"success": True, "response": state.get("response", ""), "messages": [],
                "map_commands": [], "cesium_commands": [], "charts": [],
                "report_url": None, "intent_info": None, "requires_confirmation": False}


class FakeResp:
    """dashscope.Generation.call 的假响应。"""

    def __init__(self, content, status_code=200):
        self.status_code = status_code
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        self.output = type("O", (), {"choices": [choice]})()


def _make_intent(plan, intent_type=IntentType.KNOWLEDGE_SEARCH):
    return IntentResult(
        primary_intent=intent_type, confidence=0.9, entities=[],
        task_context="测试", execution_plan=plan,
        requires_confirmation=False, suggestions=[],
    )


def _make_engine(tmp_path, intent, adapter=None):
    store = RunStore(db_path=str(tmp_path / "r.db"))
    exe = FakeExecutor(intent, adapter=adapter)
    engine = RunEngine(exe, store=store, bus=EventBus(store), gateway=None, timeout=600)
    return engine, store, exe


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# _react_observe 单元测试
# ----------------------------------------------------------------------

@pytest.fixture()
def engine_skeleton(tmp_path):
    plan = [TaskStep(step_id=1, action="检索", tool="knowledge_base_tool",
                     params={"operation": "search", "query": "q"}, reasoning="", expected_output="")]
    engine, _, _ = _make_engine(tmp_path, _make_intent(plan))
    return engine


def test_react_observe_shortcircuit_no_llm(engine_skeleton, monkeypatch):
    import dashscope
    called = {"n": 0}

    def spy(*a, **kw):
        called["n"] += 1
        raise AssertionError("短路场景不应调用 LLM")
    monkeypatch.setattr(dashscope.Generation, "call", spy)

    results = [{"step_id": 1, "tool_name": "postgresql_tool",
                "result": {"success": True, "data": [{"count": 5}]}}]
    state = {"user_message": "统计砂场数量"}
    step = _run(engine_skeleton._react_observe("r1", state, results, 1))
    assert step is None
    assert called["n"] == 0


def test_react_observe_add_step_decision(engine_skeleton, monkeypatch):
    import dashscope
    decision = ('{"decision": "add_step", "tool": "knowledge_base_tool", '
                '"params": {"operation": "search", "query": "采砂审批主管部门 重查"}, '
                '"reasoning": "检索结果未覆盖主管部门"}')
    monkeypatch.setattr(dashscope.Generation, "call",
                        lambda *a, **kw: FakeResp(decision))

    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    state = {"user_message": "河道采砂归哪个部门管"}
    step = _run(engine_skeleton._react_observe("r1", state, results, 1))
    assert step is not None
    assert step.step_id == 2  # max(step_id)+1
    assert step.tool == "knowledge_base_tool"
    assert step.params["query"] == "采砂审批主管部门 重查"


def test_react_observe_rejects_non_whitelist(engine_skeleton, monkeypatch):
    import dashscope
    decision = ('{"decision": "add_step", "tool": "dangerous_tool", '
                '"params": {"cmd": "rm -rf"}, "reasoning": "x"}')
    monkeypatch.setattr(dashscope.Generation, "call", lambda *a, **kw: FakeResp(decision))

    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    state = {"user_message": "q"}
    assert _run(engine_skeleton._react_observe("r1", state, results, 1)) is None


def test_react_observe_rejects_empty_params(engine_skeleton, monkeypatch):
    import dashscope
    decision = ('{"decision": "add_step", "tool": "knowledge_base_tool", '
                '"params": {}, "reasoning": "x"}')
    monkeypatch.setattr(dashscope.Generation, "call", lambda *a, **kw: FakeResp(decision))

    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    state = {"user_message": "q"}
    assert _run(engine_skeleton._react_observe("r1", state, results, 1)) is None


def test_react_observe_llm_garbage_returns_none(engine_skeleton, monkeypatch):
    import dashscope
    monkeypatch.setattr(dashscope.Generation, "call",
                        lambda *a, **kw: FakeResp("这不是JSON"))
    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    state = {"user_message": "q"}
    assert _run(engine_skeleton._react_observe("r1", state, results, 1)) is None


# ----------------------------------------------------------------------
# _run_plan 集成：ReAct 循环开关
# ----------------------------------------------------------------------

def test_run_plan_react_appends_step(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.setenv("REACT_MAX_ROUNDS", "3")

    import dashscope
    decisions = [
        # 轮1：追加改写重查
        FakeResp('{"decision": "add_step", "tool": "knowledge_base_tool", '
                 '"params": {"operation": "search", "query": "采砂主管部门 重查"}, '
                 '"reasoning": "首轮检索为空"}'),
        # 轮2：结果充分，结束
        FakeResp('{"decision": "finish", "reasoning": "补查结果已覆盖"}'),
    ]
    monkeypatch.setattr(dashscope.Generation, "call", lambda *a, **kw: decisions.pop(0))

    plan = [TaskStep(step_id=1, action="知识检索", tool="knowledge_base_tool",
                     params={"operation": "search", "query": "河道采砂归哪个部门管"},
                     reasoning="", expected_output="")]
    adapter = FakeAdapter(results=[
        {"success": True, "data": []},                       # 计划步骤：空结果
        {"success": True, "data": [{"title": "采砂管理办法"}]},  # ReAct 追加步骤：有数据
    ])
    engine, store, exe = _make_engine(tmp_path, _make_intent(plan), adapter=adapter)

    async def main():
        task = await engine.start("r1", "s1", "河道采砂归哪个部门管")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    events = store.get_events("r1")
    stages = [e.get("stage", "") for e in events]
    # react_round 走 type=status 通道（前端 ProgressTimeline 直接渲染），用 stage 区分
    assert "react_round" in stages
    # 计划步骤 + ReAct 追加步骤都被执行，参数正确
    assert adapter.calls == [
        {"operation": "search", "query": "河道采砂归哪个部门管"},
        {"operation": "search", "query": "采砂主管部门 重查"},
    ]
    # final 正常
    assert events[-1]["type"] == "final"
    assert events[-1]["result"]["success"] is True


def test_run_plan_react_disabled_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")

    import dashscope
    called = {"n": 0}

    def spy(*a, **kw):
        called["n"] += 1
        return FakeResp('{"decision": "finish"}')
    monkeypatch.setattr(dashscope.Generation, "call", spy)

    plan = [TaskStep(step_id=1, action="知识检索", tool="knowledge_base_tool",
                     params={"operation": "search", "query": "q"}, reasoning="", expected_output="")]
    adapter = FakeAdapter(results=[{"success": True, "data": []}])
    engine, store, _ = _make_engine(tmp_path, _make_intent(plan), adapter=adapter)

    async def main():
        task = await engine.start("r1", "s1", "q")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    stages = [e.get("stage", "") for e in store.get_events("r1")]
    assert "react_round" not in stages
    assert called["n"] == 0
    # 计划步骤 1 次 + 知识库空结果触发「自动联网搜索」兜底 1 次（run_engine 既定行为）
    assert adapter.calls == [
        {"operation": "search", "query": "q"},
        {"query": "q"},
    ]


# ----------------------------------------------------------------------
# Phase 3：FC 观测模式（REACT_FC_MODE）
# ----------------------------------------------------------------------

class FakeToolCallMsg:
    """LangChain AIMessage 最小假件：tool_calls + content。"""

    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content


def _fc_step_msg():
    return FakeToolCallMsg(tool_calls=[
        {"name": "knowledge_base_tool",
         "args": {"operation": "search", "query": "采砂主管部门 重查"},
         "id": "call_9", "type": "tool_call"},
    ])


def test_react_fc_observe_add_step(engine_skeleton, monkeypatch):
    monkeypatch.setenv("REACT_FC_MODE", "1")

    import dashscope
    monkeypatch.setattr(dashscope.Generation, "call",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("FC 模式不应走 JSON 路径")))

    observer = engine_skeleton._react_observer
    captured = {}

    def fake_invoke(messages, tools):
        captured["messages"] = messages
        captured["tools"] = tools
        return _fc_step_msg()

    observer._fc_invoke = fake_invoke
    results = [{"step_id": 1, "tool_name": "knowledge_base_tool", "tool_call_id": "call_1",
                "result": {"success": True, "data": []}}]
    state = {"user_message": "河道采砂归哪个部门管"}
    step = _run(engine_skeleton._react_observe("r1", state, results, 1))
    assert step is not None
    assert step.step_id == 2                     # max(step_id)+1
    assert step.tool == "knowledge_base_tool"
    assert step.params["query"] == "采砂主管部门 重查"
    assert step.tool_call_id == "call_9"         # 原生 id 保留
    # 白名单经 tools= 暴露（API 层天然约束）
    names = [t["function"]["name"] for t in captured["tools"]]
    assert set(names) <= set(observer.allowed_tools)
    # tool_call_id 结果按 assistant.tool_calls + role:tool 协议回填
    msgs = captured["messages"]
    assert any(getattr(m, "tool_calls", None) for m in msgs)
    tool_msgs = [m for m in msgs if getattr(m, "tool_call_id", None)]
    assert tool_msgs and tool_msgs[0].tool_call_id == "call_1"


def test_react_fc_observe_finish_when_no_calls(engine_skeleton, monkeypatch):
    monkeypatch.setenv("REACT_FC_MODE", "1")
    observer = engine_skeleton._react_observer
    captured = {}

    def fake_invoke(messages, tools):
        captured["messages"] = messages
        return FakeToolCallMsg(content="结果已足够")

    observer._fc_invoke = fake_invoke
    # 无 tool_call_id 的旧结果：仅文本摘要，不构造协议消息
    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": [{"x": 1}]}}]
    step = _run(engine_skeleton._react_observe("r1", {"user_message": "q"}, results, 1))
    assert step is None
    assert not any(getattr(m, "tool_call_id", None) for m in captured["messages"])


def test_react_fc_failure_falls_back_to_json(engine_skeleton, monkeypatch):
    monkeypatch.setenv("REACT_FC_MODE", "1")

    import dashscope
    decision = ('{"decision": "add_step", "tool": "knowledge_base_tool", '
                '"params": {"operation": "search", "query": "json 回退"}, "reasoning": "r"}')
    monkeypatch.setattr(dashscope.Generation, "call", lambda *a, **kw: FakeResp(decision))

    observer = engine_skeleton._react_observer
    observer._fc_invoke = lambda messages, tools: (_ for _ in ()).throw(RuntimeError("fc down"))
    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    step = _run(engine_skeleton._react_observe("r1", {"user_message": "q"}, results, 1))
    assert step is not None
    assert step.params["query"] == "json 回退"


def test_react_fc_disabled_uses_json_path(engine_skeleton, monkeypatch):
    monkeypatch.delenv("REACT_FC_MODE", raising=False)
    observer = engine_skeleton._react_observer
    called = {"fc": False}

    def fc_spy(messages, tools):
        called["fc"] = True
        raise AssertionError("FC 关闭时不应调用")

    observer._fc_invoke = fc_spy

    import dashscope
    monkeypatch.setattr(dashscope.Generation, "call",
                        lambda *a, **kw: FakeResp('{"decision": "finish", "reasoning": "ok"}'))
    results = [{"step_id": 1, "tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": []}}]
    assert _run(engine_skeleton._react_observe("r1", {"user_message": "q"}, results, 1)) is None
    assert called["fc"] is False


def test_run_plan_react_fc_appends_step(tmp_path, monkeypatch):
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.setenv("REACT_MAX_ROUNDS", "3")
    monkeypatch.setenv("REACT_FC_MODE", "1")

    import dashscope
    monkeypatch.setattr(dashscope.Generation, "call",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("FC 模式不应走 JSON 路径")))

    from agents.react_loop import ReActObserver
    responses = [
        _fc_step_msg(),                       # 轮1：追加改写重查（原生 tool_call）
        FakeToolCallMsg(content="已足够"),     # 轮2：finish（无调用）
    ]
    monkeypatch.setattr(ReActObserver, "_fc_invoke",
                        lambda self, messages, tools: responses.pop(0))

    plan = [TaskStep(step_id=1, action="知识检索", tool="knowledge_base_tool",
                     params={"operation": "search", "query": "河道采砂归哪个部门管"},
                     reasoning="", expected_output="")]
    adapter = FakeAdapter(results=[
        {"success": True, "data": []},                        # 计划步骤：空结果
        {"success": True, "data": [{"title": "采砂管理办法"}]},  # ReAct 追加步骤：有数据
    ])
    engine, store, exe = _make_engine(tmp_path, _make_intent(plan), adapter=adapter)

    async def main():
        task = await engine.start("r1", "s1", "河道采砂归哪个部门管")
        await task
    _run(main())

    assert store.get_run("r1")["status"] == COMPLETED
    stages = [e.get("stage", "") for e in store.get_events("r1")]
    assert "react_round" in stages
    assert adapter.calls == [
        {"operation": "search", "query": "河道采砂归哪个部门管"},
        {"operation": "search", "query": "采砂主管部门 重查"},
    ]
