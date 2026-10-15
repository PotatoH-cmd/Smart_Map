"""
test_graph_observe.py — 非流式图 ReAct 观察节点（observe_node）路由与语义测试。

覆盖（对齐《ReAct子图与Critic评审设计方案.md》§7.2）：
  - 自环路由：observe 追加步骤 → react_last_added=True → 回 observe_node → finish → extract_node
  - reducer 安全：tool_results 按 operator.add 合并、部分更新不翻倍
  - 领域白名单动态化：领域 tool_names ∩ 只读安全集；空集（地图类）→ 不调 LLM 直接转出
  - 白名单过滤：领域外的只读工具（知识领域下的 postgresql）被拒绝
  - 开关：REACT_LOOP_ENABLED=0 → 不调 LLM（现状行为）
  - 轮数上限：REACT_MAX_ROUNDS 用尽后不再调用 LLM
  - critic 闭环（§5.4）：打回 → response 清空 → summarize 消费反馈重生成 → 二次评审 → END；
    超限守卫保证 ≤2 次评审、≤1 次修正（防死循环）；触发不命中零开销直通

实现方式：TaskExecutor.__new__ 装配最小依赖面（不触发 __init__ 的重初始化），
使用真实 _build_graph / _observe_node / _critic_node / _route_after_observe / _route_after_critic /
_invoke_single_step，仅对图入口与出口节点打桩，验证真实图拓扑与本轮 P1 改造。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langgraph.checkpoint.memory import MemorySaver
from agents.task_executor import TaskExecutor
from agents.intent_types import IntentType, IntentResult, TaskStep


# ----------------------------------------------------------------------
# 假件注入
# ----------------------------------------------------------------------

class FakeAdapter:
    """假工具适配器：返回与真实 QwenToolAdapter 同形的结果（含 tool_name 包裹）。"""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def invoke(self, params):
        self.calls.append(dict(params or {}))
        inner = self.results.pop(0) if self.results else {"success": True, "data": [{"x": 1}]}
        return {"tool_name": "knowledge_base_tool", "result": inner}


class FakeHarness:
    """按意图返回领域工具清单（替代真实 AgentHarness.dispatch）。"""

    def __init__(self, tool_names):
        self._tool_names = list(tool_names)

    def get_tool_names(self, intent_result):
        return list(self._tool_names)


class FakeIntentAgent:
    def get_required_tools(self, intent_result):
        seen, tools = set(), []
        for step in intent_result.execution_plan:
            if step.tool and step.tool not in seen:
                seen.add(step.tool)
                tools.append(step.tool)
        return tools


class FakeCritic:
    """可控评审假件：reviewable=False（默认）零开销放行，隔离非评审用例。"""

    def __init__(self, verdicts=None, reviewable=False, max_rounds=1):
        self.verdicts = list(verdicts or [])
        self.reviewable = reviewable
        self.max_rounds = max_rounds
        self.review_calls = 0

    @property
    def enabled(self):
        return True

    def should_review(self, intent_result, tool_results, response_text):
        return self.reviewable

    def review(self, **kwargs):
        self.review_calls += 1
        if not self.verdicts:
            return None
        return self.verdicts.pop(0)


class FakeLLM:
    """假 LLM：按是否含修正指令返回初版/修正版回答，并记录 user_content。"""

    def __init__(self, initial="初版回答", corrected="修正后的回答"):
        self.initial = initial
        self.corrected = corrected
        self.calls = []

    def invoke(self, messages):
        content = messages[-1].content if messages else ""
        self.calls.append(content)
        text = self.corrected if "上一版回答存在以下问题" in content else self.initial
        return type("Msg", (), {"content": text})()


class FakeResp:
    """dashscope.Generation.call 的假响应。"""

    def __init__(self, content, status_code=200):
        self.status_code = status_code
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        self.output = type("O", (), {"choices": [choice]})()


def _make_intent(tool="knowledge_base_tool", intent_type=IntentType.KNOWLEDGE_SEARCH):
    plan = [TaskStep(step_id=1, action="检索", tool=tool,
                     params={"operation": "search", "query": "q"},
                     reasoning="", expected_output="")]
    return IntentResult(
        primary_intent=intent_type, confidence=0.9, entities=[],
        task_context="测试", execution_plan=plan,
        requires_confirmation=False, suggestions=[],
    )


def _initial_state():
    return {
        "user_message": "河道采砂归哪个部门管",
        "chat_history": [],
        "intent_result": None,
        "tool_results": [],
        "response": "",
        "map_commands": [],
        "cesium_commands": [],
        "charts": [],
        "report_url": None,
        "error": None,
        "react_rounds": 0,
        "react_last_added": False,
        "_critic_feedback": None,
        "_critic_issues": [],
        "_critic_round": 0,
    }


def _build_serviced_executor(tool_names, adapter, intent, seed_results,
                             critic=None, spy_critic=False, real_summarize=False):
    """装配最小 TaskExecutor：真实图 + 真实 observe，仅入口/出口节点打桩。

    critic：评审假件（默认零开销放行，隔离非评审用例）；
    spy_critic=True：对 critic 节点加访问打点（验证闭环路由）；
    real_summarize=True：使用真实 summarize 节点 + FakeLLM（critic 闭环用例）。
    """
    te = TaskExecutor.__new__(TaskExecutor)
    te._memory = MemorySaver()
    te.harness = FakeHarness(tool_names)
    te.intent_agent = FakeIntentAgent()
    te._summarize_tool_result = lambda tool, res: f"{tool} 摘要"
    te._get_tool_adapter = lambda name: adapter
    te.critic = critic or FakeCritic()
    te.visits = []

    real_observe = te._observe_node  # 真实节点（绑定方法），外层加访问打点

    async def _intent_node(state):
        te.visits.append("intent")
        state["intent_result"] = intent
        return state

    async def _tool_node(state):
        te.visits.append("tool")
        state["tool_results"] = list(seed_results)
        return state

    async def _observe_spy(state):
        te.visits.append("observe")
        return await real_observe(state)

    def _extract_node(state):
        te.visits.append("extract")
        return {}

    if real_summarize:
        # 真实 summarize（验证反馈消费/重生成）；依赖面打桩避开中间件与来源收集
        te._try_extract_rich_response = lambda tr: None
        te._append_kb_sources = lambda text, tr: text
        te._append_web_sources = lambda text, tr: text
        te._collect_kb_sources = lambda tr: []
        real_summarize_fn = te._summarize_node

        def _summarize_real(state):
            te.visits.append("summarize")
            return real_summarize_fn(state)

        te._summarize_node = _summarize_real
    else:
        def _summarize_node(state):
            te.visits.append("summarize")
            return {"response": "最终回复"}

        te._summarize_node = _summarize_node

    if spy_critic:
        real_critic_fn = te._critic_node

        async def _critic_spy(state):
            te.visits.append("critic")
            return await real_critic_fn(state)

        te._critic_node = _critic_spy

    te._intent_node = _intent_node
    te._tool_node = _tool_node
    te._observe_node = _observe_spy
    te._extract_node = _extract_node

    te._graph = te._build_graph({"model": "qwen-plus", "api_key": "test"})
    if real_summarize:
        te.fake_llm = FakeLLM()
        te._llm = te.fake_llm  # 覆盖 _build_graph 内构造的真实 ChatOpenAI
    return te


def _run(te, thread="t1"):
    return asyncio.run(te._graph.ainvoke(
        _initial_state(), config={"configurable": {"thread_id": thread}}))


# ----------------------------------------------------------------------
# 自环路由 + reducer 安全
# ----------------------------------------------------------------------

def test_observe_selfloop_appends_step_without_doubling(monkeypatch):
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.setenv("REACT_MAX_ROUNDS", "3")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    import dashscope
    decisions = [
        FakeResp('{"decision": "add_step", "tool": "knowledge_base_tool", '
                 '"params": {"operation": "search", "query": "采砂主管部门 重查"}, '
                 '"reasoning": "首轮检索为空"}'),
        FakeResp('{"decision": "finish", "reasoning": "补查已覆盖"}'),
    ]
    monkeypatch.setattr(dashscope.Generation, "call", lambda *a, **kw: decisions.pop(0))

    adapter = FakeAdapter(results=[{"success": True, "data": [{"title": "采砂管理办法"}]}])
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool", "web_search_tool"},
        adapter=adapter, intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": []}}],
    )
    final = _run(te)

    # 路由序列：tool → observe(追加) → observe(finish) → extract → summarize
    assert te.visits == ["intent", "tool", "observe", "observe", "extract", "summarize"]
    # reducer 安全：计划 1 条 + 观察追加 1 条 = 2 条（部分更新不翻倍）
    assert len(final["tool_results"]) == 2
    assert final["tool_results"][1]["step_id"] == 2
    assert final["tool_results"][1]["tool_name"] == "knowledge_base_tool"
    assert final["react_rounds"] == 1
    # 追加步骤经真实执行内核调用，参数为 LLM 决策参数
    assert adapter.calls == [{"operation": "search", "query": "采砂主管部门 重查"}]


# ----------------------------------------------------------------------
# 领域白名单动态化
# ----------------------------------------------------------------------

def test_observe_map_domain_empty_whitelist_skips_llm(monkeypatch):
    """MapAgent 域无只读工具（∩ 空集）→ 不进 LLM，零额外开销。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    import dashscope

    def spy(*a, **kw):
        raise AssertionError("空白名单不应调用 LLM")
    monkeypatch.setattr(dashscope.Generation, "call", spy)

    adapter = FakeAdapter()
    te = _build_serviced_executor(
        tool_names={"map_tool", "qgis_mcp_tool"},
        adapter=adapter,
        intent=_make_intent(tool="map_tool", intent_type=IntentType.MAP_DISPLAY),
        seed_results=[{"step_id": 1, "tool_name": "map_tool",
                       "result": {"success": True,
                                  "map_command": {"type": "load_vector_layer"}}}],
    )
    final = _run(te, thread="t2")

    assert te.visits == ["intent", "tool", "observe", "extract", "summarize"]
    assert len(final["tool_results"]) == 1
    assert adapter.calls == []


def test_observe_whitelist_rejects_out_of_domain_tool(monkeypatch):
    """知识领域白名单 = {kb, web}；LLM 误选 postgresql_tool（全局安全集内但领域外）被拒绝。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.setenv("REACT_MAX_ROUNDS", "3")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    import dashscope
    calls = {"n": 0}

    def decide(*a, **kw):
        calls["n"] += 1
        return FakeResp('{"decision": "add_step", "tool": "postgresql_tool", '
                        '"params": {"operation": "query", "sql": "SELECT 1"}, "reasoning": "x"}')
    monkeypatch.setattr(dashscope.Generation, "call", decide)

    adapter = FakeAdapter()
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool", "web_search_tool"},
        adapter=adapter, intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": []}}],
    )
    final = _run(te, thread="t3")

    assert calls["n"] == 1  # 仅一次观察（决策被白名单拒绝 → finish）
    assert adapter.calls == []  # 未执行任何追加步骤
    assert te.visits == ["intent", "tool", "observe", "extract", "summarize"]
    assert len(final["tool_results"]) == 1


# ----------------------------------------------------------------------
# 开关与轮数上限
# ----------------------------------------------------------------------

def test_observe_disabled_by_env(monkeypatch):
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    import dashscope

    def spy(*a, **kw):
        raise AssertionError("REACT_LOOP_ENABLED=0 不应调用 LLM")
    monkeypatch.setattr(dashscope.Generation, "call", spy)

    adapter = FakeAdapter()
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool"},
        adapter=adapter, intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": []}}],
    )
    final = _run(te, thread="t4")

    assert te.visits == ["intent", "tool", "observe", "extract", "summarize"]
    assert len(final["tool_results"]) == 1
    assert adapter.calls == []


def test_observe_max_rounds_bound(monkeypatch):
    """REACT_MAX_ROUNDS=2：恰 2 次 LLM 观察 + 2 次追加执行，第 3 次进入即被上限拦截。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "1")
    monkeypatch.setenv("REACT_MAX_ROUNDS", "2")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    import dashscope
    calls = {"n": 0}

    def always_add(*a, **kw):
        calls["n"] += 1
        return FakeResp('{"decision": "add_step", "tool": "knowledge_base_tool", '
                        '"params": {"operation": "search", "query": "q%d"}, '
                        '"reasoning": "x"}' % calls["n"])
    monkeypatch.setattr(dashscope.Generation, "call", always_add)

    adapter = FakeAdapter(results=[{"success": True, "data": []} for _ in range(3)])
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool"},
        adapter=adapter, intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": []}}],
    )
    final = _run(te, thread="t5")

    assert calls["n"] == 2
    assert final["react_rounds"] == 2
    assert te.visits.count("observe") == 3  # 2 次执行 + 1 次上限拦截
    assert len(final["tool_results"]) == 3  # 1 计划 + 2 追加
    assert adapter.calls == [
        {"operation": "search", "query": "q1"},
        {"operation": "search", "query": "q2"},
    ]


# ----------------------------------------------------------------------
# critic 评审闭环（summarize ⇄ critic）
# ----------------------------------------------------------------------

def test_critic_rejects_then_summarize_corrects_and_passes(monkeypatch):
    """回环：打回 → response 清空 → summarize 消费反馈重生成 → 二次评审通过 → END。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    critic = FakeCritic(reviewable=True, verdicts=[
        {"passed": False, "severity": "major",
         "issues": ["许可号未在工具结果中出现"], "feedback": "删除该许可号"},
        {"passed": True, "severity": "ok", "issues": [], "feedback": ""},
    ])
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool"},
        adapter=FakeAdapter(),
        intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": [{"title": "办法"}]}}],
        critic=critic, spy_critic=True, real_summarize=True,
    )
    final = _run(te, thread="tc1")

    # summarize 重入 + critic 两次
    assert te.visits == ["intent", "tool", "observe", "extract",
                         "summarize", "critic", "summarize", "critic"]
    # 修正轮消费反馈：最终为修正版、反馈不残留、修正轮数恰 1
    assert final["response"] == "修正后的回答"
    assert final["_critic_feedback"] is None
    assert final["_critic_round"] == 1
    assert critic.review_calls == 2
    # 修正指令注入 LLM user_content（含反馈原文），两次生成
    assert len(te.fake_llm.calls) == 2
    assert "上一版回答存在以下问题：删除该许可号" in te.fake_llm.calls[1]


def test_critic_max_rounds_guard_no_infinite_loop(monkeypatch):
    """防死循环：连续打回但 CRITIC_MAX_ROUNDS=1 → ≤2 次评审、≤1 次修正后放行最新版。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    reject = {"passed": False, "severity": "major",
              "issues": ["仍存在问题"], "feedback": "再改一次"}
    critic = FakeCritic(reviewable=True, verdicts=[dict(reject), dict(reject)])
    te = _build_serviced_executor(
        tool_names={"knowledge_base_tool"},
        adapter=FakeAdapter(),
        intent=_make_intent(),
        seed_results=[{"step_id": 1, "tool_name": "knowledge_base_tool",
                       "result": {"success": True, "data": [{"title": "办法"}]}}],
        critic=critic, spy_critic=True, real_summarize=True,
    )
    final = _run(te, thread="tc2")

    assert te.visits == ["intent", "tool", "observe", "extract",
                         "summarize", "critic", "summarize", "critic"]
    # 第二次打回被配额守卫拦截：保留最新版回答、无反馈残留、未进入第三次生成
    assert final["response"] == "修正后的回答"
    assert final["_critic_feedback"] is None
    assert final["_critic_round"] == 1
    assert critic.review_calls == 2
    assert len(te.fake_llm.calls) == 2


def test_critic_not_triggered_zero_overhead(monkeypatch):
    """触发判定不命中（地图类）→ 不调评审、不重生成，直通 END。"""
    monkeypatch.setenv("REACT_LOOP_ENABLED", "0")
    monkeypatch.delenv("TOOL_HUB_URL", raising=False)

    critic = FakeCritic(reviewable=False)
    te = _build_serviced_executor(
        tool_names={"map_tool"},
        adapter=FakeAdapter(),
        intent=_make_intent(tool="map_tool", intent_type=IntentType.MAP_DISPLAY),
        seed_results=[{"step_id": 1, "tool_name": "map_tool",
                       "result": {"success": True,
                                  "map_command": {"type": "load_vector_layer"}}}],
        critic=critic, spy_critic=True, real_summarize=True,
    )
    final = _run(te, thread="tc3")

    assert te.visits == ["intent", "tool", "observe", "extract", "summarize", "critic"]
    assert final["response"] == "初版回答"
    assert critic.review_calls == 0
    assert len(te.fake_llm.calls) == 1
