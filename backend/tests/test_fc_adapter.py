"""
test_fc_adapter.py — Phase 2：原生 tool_call 转换层 + IntentAgent FC 灰度模式。

覆盖（纯内存，不调真实 LLM）：
1. build_openai_tools：ToolSpec / dict 元数据 → OpenAI tools 定义
2. tool_calls_to_steps：LangChain 形态 / OpenAI 原始形态 / 非法 JSON / 多调用顺序 / tool_call_id
3. results_to_tool_messages：tool_call_id 对应 / 错误内容 / 无 id 跳过
4. IntentAgent：_infer_intent_from_tool / _select_fc_tool_names / analyze 的 FC 路径与回退
5. schema 注入片段：_build_tool_snippet（自动 Schema + 手写约束）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.fc_adapter import build_openai_tools, results_to_tool_messages, tool_calls_to_steps
from agents.intent_types import IntentResult, IntentType
from core.toolhub.models import ToolSpec


# ----------------------------------------------------------------------
# build_openai_tools
# ----------------------------------------------------------------------

def test_build_openai_tools_from_spec_and_dict():
    spec = ToolSpec(
        name="weather_tool", description="天气查询",
        parameters=[{"name": "city", "type": "string", "description": "城市", "required": True}],
    )
    tools = build_openai_tools([spec, {"name": "kb", "description": "知识库",
                                       "parameters": [{"name": "query", "type": "string"}]},
                                {"description": "无 name，跳过"}])
    assert [t["function"]["name"] for t in tools] == ["weather_tool", "kb"]
    fn = tools[0]["function"]
    assert fn["parameters"]["required"] == ["city"]
    assert fn["parameters"]["properties"]["city"]["type"] == "string"
    assert tools[1]["function"]["parameters"]["properties"]["query"]["type"] == "string"


# ----------------------------------------------------------------------
# tool_calls_to_steps
# ----------------------------------------------------------------------

def test_tool_calls_to_steps_langchain_form():
    calls = [
        {"name": "postgresql_tool", "args": {"operation": "query"}, "id": "call_1", "type": "tool_call"},
        {"name": "data_visualizer_tool", "args": {"demand": "柱状图"}, "id": "call_2"},
    ]
    steps = tool_calls_to_steps(calls, start_id=3)
    assert [s.step_id for s in steps] == [3, 4]
    assert [s.tool for s in steps] == ["postgresql_tool", "data_visualizer_tool"]
    assert steps[0].tool_call_id == "call_1"
    assert steps[0].params == {"operation": "query"}


def test_tool_calls_to_steps_openai_raw_form_and_bad_json():
    calls = [
        {"id": "call_x", "type": "function",
         "function": {"name": "knowledge_base_tool", "arguments": json.dumps({"query": "政策"})}},
        {"id": "call_y", "type": "function",
         "function": {"name": "web_search_tool", "arguments": "{bad json"}},
        {"id": "call_z", "type": "function", "function": {"arguments": "{}"}},  # 无 name → 跳过
    ]
    steps = tool_calls_to_steps(calls)
    assert [s.tool for s in steps] == ["knowledge_base_tool", "web_search_tool"]
    assert steps[0].params == {"query": "政策"}
    assert steps[1].params == {}          # 非法 JSON 降级，由缺参/校验兜底
    assert steps[0].tool_call_id == "call_x"
    assert [s.step_id for s in steps] == [1, 2]


# ----------------------------------------------------------------------
# results_to_tool_messages
# ----------------------------------------------------------------------

def test_results_to_tool_messages_correspondence():
    ordered = [
        {"step_id": 1, "tool_name": "postgresql_tool", "tool_call_id": "call_1",
         "result": {"success": True, "data": [{"count": 5}], "message": "查询完成"}},
        {"step_id": 2, "tool_name": "web_search_tool", "tool_call_id": "call_2",
         "result": {"success": False, "error": "超时"}},
        {"step_id": 3, "tool_name": "no_id_tool", "result": {"success": True}},  # 无 tool_call_id → 跳过
    ]
    msgs = results_to_tool_messages(ordered)
    assert [m["tool_call_id"] for m in msgs] == ["call_1", "call_2"]
    assert all(m["role"] == "tool" for m in msgs)
    p0 = json.loads(msgs[0]["content"])
    assert p0["success"] is True and p0["data"] == [{"count": 5}]
    p1 = json.loads(msgs[1]["content"])
    assert p1["success"] is False and p1["error"] == "超时"


# ----------------------------------------------------------------------
# IntentAgent：FC 模式（monkeypatch 注入假 LLM，不触网）
# ----------------------------------------------------------------------

class FakeMsg:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content


def _make_agent(monkeypatch):
    from agents.intent_agent import IntentAgent
    monkeypatch.setattr(IntentAgent, "_inject_schema_once", lambda self: None)
    return IntentAgent({"model": "qwen-plus", "base_url": "http://localhost/v1",
                        "api_key": "test-key"})


class FakeStructuredLLM:
    def __init__(self, result):
        self.result = result

    def invoke(self, messages):
        return self.result


def test_fc_analyze_returns_plan_with_tool_call_ids(monkeypatch):
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("INTENT_FC_MODE", "1")
    captured = {}

    def fake_invoke(messages, tools):
        captured["tools"] = tools
        return FakeMsg(tool_calls=[
            {"name": "postgresql_tool", "args": {"operation": "query", "sql": "SELECT 1"},
             "id": "call_1", "type": "tool_call"},
            {"name": "data_visualizer_tool", "args": {"demand": "柱状图"},
             "id": "call_2", "type": "tool_call"},
        ])

    agent._fc_invoke = fake_invoke
    res = agent.analyze("统计各采区数量并做柱状图", view_hint="map")
    assert res.primary_intent == IntentType.DATA_QUERY          # 首工具 postgresql_tool 反查
    assert [s.tool for s in res.execution_plan] == ["postgresql_tool", "data_visualizer_tool"]
    assert [s.tool_call_id for s in res.execution_plan] == ["call_1", "call_2"]
    assert [s.step_id for s in res.execution_plan] == [1, 2]
    # tools= 注入的是命中工具的真实 schema
    names = [t["function"]["name"] for t in captured["tools"]]
    assert "postgresql_tool" in names and "data_visualizer_tool" in names
    assert captured["tools"][0]["function"]["parameters"]["type"] == "object"


def test_fc_analyze_text_passthrough(monkeypatch):
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("INTENT_FC_MODE", "1")
    agent._fc_invoke = lambda messages, tools: FakeMsg(content="我是一个地图指挥助手。")
    res = agent.analyze("你好")
    assert res.primary_intent == IntentType.UNKNOWN
    assert res.execution_plan == []
    assert "地图指挥助手" in res.task_context


def test_fc_failure_falls_back_to_structured(monkeypatch):
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("INTENT_FC_MODE", "1")

    def boom(messages, tools):
        raise RuntimeError("fc down")

    agent._fc_invoke = boom
    canned = IntentResult(primary_intent=IntentType.MAP_DISPLAY, confidence=0.9, entities=[],
                          task_context="结构化兜底", execution_plan=[],
                          requires_confirmation=False, suggestions=[])
    agent.structured_llm = FakeStructuredLLM(canned)
    assert agent.analyze("打开地图").task_context == "结构化兜底"


def test_fc_disabled_uses_structured_path(monkeypatch):
    agent = _make_agent(monkeypatch)
    monkeypatch.delenv("INTENT_FC_MODE", raising=False)
    called = {"fc": False}
    agent._fc_invoke = lambda messages, tools: called.__setitem__("fc", True)
    canned = IntentResult(primary_intent=IntentType.DATA_QUERY, confidence=0.9, entities=[],
                          task_context="结构化默认", execution_plan=[],
                          requires_confirmation=False, suggestions=[])
    agent.structured_llm = FakeStructuredLLM(canned)
    assert agent.analyze("查数据").task_context == "结构化默认"
    assert called["fc"] is False


def test_infer_intent_and_tool_selection(monkeypatch):
    agent = _make_agent(monkeypatch)
    from agents.intent_agent import IntentAgent
    assert IntentAgent._infer_intent_from_tool("postgresql_tool") == IntentType.DATA_QUERY
    assert IntentAgent._infer_intent_from_tool("cesium_tool") == IntentType.MAP_DISPLAY
    assert IntentAgent._infer_intent_from_tool("no_such_tool") == IntentType.UNKNOWN
    # 无关键词命中 → 兜底核心集（视图绑定）
    assert agent._select_fc_tool_names("你好", None) == [
        "knowledge_base_tool", "postgresql_tool", "web_search_tool", "map_tool"]
    assert agent._select_fc_tool_names("你好", "cesium")[-1] == "cesium_tool"
    # 关键词命中（含排除：caisha 命中后排除 report_generator）→ caisha 出现在命中集
    names = agent._select_fc_tool_names("生成郝楼砂场的采砂监测报告", "map")
    assert "caisha_report_tool" in names and "report_generator_tool" not in names


def test_build_tool_snippet_schema_plus_handwritten(monkeypatch):
    agent = _make_agent(monkeypatch)
    snippet = agent._build_tool_snippet("weather_tool")
    assert "weather_tool 参数 Schema（自动注入）" in snippet
    assert "city" in snippet
    # 手写约束层保留（语义覆盖）
    assert "forecast_days" in snippet
