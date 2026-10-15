"""
test_critic.py — 公共评审组件 ResponseCritic 单元测试（对齐《ReAct子图与Critic评审设计方案.md》§7.1）。

覆盖：
  - should_review 触发判定：报告/知识意图命中；操作性意图不触发；工具命中（交叉意图场景）；
    超长响应跳过；开关关闭跳过；空响应跳过；工具失败不触发
  - review 语义评审：通过放行；打回（issues+feedback 透传）；异常/HTTP 错误/非法 JSON → None（保守放行）；
    打回但 feedback 为空 → 保守放行为通过
  - prompt 回归：固定四评审维度 + 工具结果摘要 + 草拟回答注入
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.critic import ResponseCritic
from agents.intent_types import IntentType, IntentResult


class FakeResp:
    """dashscope.Generation.call 的假响应。"""

    def __init__(self, content, status_code=200):
        self.status_code = status_code
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        self.output = type("O", (), {"choices": [choice]})()


def _intent(intent_type):
    return IntentResult(
        primary_intent=intent_type, confidence=0.9, entities=[],
        task_context="测试", execution_plan=[],
        requires_confirmation=False, suggestions=[],
    )


def _results(tool="knowledge_base_tool", success=True):
    return [{"step_id": 1, "tool_name": tool,
             "result": {"success": success, "content": "检索内容"}}]


def _patch_llm(monkeypatch, content=None, status_code=200, raises=None):
    import dashscope
    if raises is not None:
        def call(*a, **kw):
            raise raises
    else:
        def call(*a, **kw):
            return FakeResp(content, status_code=status_code)
    monkeypatch.setattr(dashscope.Generation, "call", call)


# ----------------------------------------------------------------------
# should_review：触发判定（确定性，不调 LLM）
# ----------------------------------------------------------------------

def test_should_review_report_and_knowledge_intents(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    critic = ResponseCritic()
    assert critic.should_review(_intent(IntentType.REPORT_GENERATION), [], "回答") is True
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "回答") is True


def test_should_review_operational_intents_not_triggered(monkeypatch):
    """操作性意图（地图/数据/天气）无需语义评审，零开销直通。"""
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    critic = ResponseCritic()
    for it in (IntentType.MAP_DISPLAY, IntentType.DATA_QUERY, IntentType.WEATHER_QUERY):
        assert critic.should_review(_intent(it), [], "回答") is False
    # 操作类工具（postgresql）成功产出也不触发（不在评审工具集内）
    assert critic.should_review(
        _intent(IntentType.DATA_QUERY), _results(tool="postgresql_tool"), "回答") is False


def test_should_review_tool_hit_cross_intent(monkeypatch):
    """交叉意图/未知意图：含成功报告或知识工具产出 → 触发。"""
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    critic = ResponseCritic()
    assert critic.should_review(
        _intent(IntentType.CROSS_INTENT), _results(tool="report_generator_tool"), "回答") is True
    assert critic.should_review(
        _intent(IntentType.UNKNOWN), _results(tool="knowledge_base_tool"), "回答") is True
    # 工具失败（success=False）不触发
    assert critic.should_review(
        _intent(IntentType.CROSS_INTENT),
        _results(tool="report_generator_tool", success=False), "回答") is False


def test_should_review_skips_long_response(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    monkeypatch.setenv("CRITIC_MAX_RESPONSE_CHARS", "100")
    critic = ResponseCritic()
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "x" * 101) is False
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "x" * 100) is True


def test_should_review_disabled_and_empty(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "0")
    critic = ResponseCritic()
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "回答") is False
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "") is False
    assert critic.should_review(_intent(IntentType.KNOWLEDGE_SEARCH), [], "   ") is False


# ----------------------------------------------------------------------
# review：语义评审（异常降级 → 保守放行）
# ----------------------------------------------------------------------

def test_review_pass(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    _patch_llm(monkeypatch, json.dumps(
        {"passed": True, "severity": "ok", "issues": [], "feedback": ""}))
    critic = ResponseCritic(summarize_fn=lambda tool, res: "摘要")
    verdict = critic.review(
        user_message="q", intent_result=_intent(IntentType.KNOWLEDGE_SEARCH),
        response_text="回答", tool_results=_results())
    assert verdict["passed"] is True
    assert verdict["severity"] == "ok"
    assert verdict["issues"] == []


def test_review_reject_with_feedback(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    _patch_llm(monkeypatch, json.dumps({
        "passed": False, "severity": "major",
        "issues": ["引用许可号未在工具结果中出现"],
        "feedback": "删除或更正该许可号，仅保留工具结果中出现的原文",
    }))
    critic = ResponseCritic()
    verdict = critic.review(
        user_message="q", intent_result=_intent(IntentType.REPORT_GENERATION),
        response_text="回答", tool_results=_results())
    assert verdict["passed"] is False
    assert verdict["issues"] == ["引用许可号未在工具结果中出现"]
    assert "删除或更正该许可号" in verdict["feedback"]


def test_review_reject_without_feedback_clamped_to_pass(monkeypatch):
    """打回但无修正指令 → 保守放行（无法修正的打回无意义）。"""
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    _patch_llm(monkeypatch, json.dumps(
        {"passed": False, "severity": "major", "issues": ["x"], "feedback": ""}))
    critic = ResponseCritic()
    verdict = critic.review(
        user_message="q", intent_result=_intent(IntentType.KNOWLEDGE_SEARCH),
        response_text="回答", tool_results=_results())
    assert verdict["passed"] is True


def test_review_degrades_to_none(monkeypatch):
    """异常/超时/HTTP 错误/非法 JSON → None（调用方视为放行）。"""
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    critic = ResponseCritic()
    # LLM 异常
    _patch_llm(monkeypatch, raises=RuntimeError("boom"))
    assert critic.review(user_message="q", intent_result=None,
                         response_text="回答", tool_results=[]) is None
    # HTTP 错误
    _patch_llm(monkeypatch, "err", status_code=500)
    assert critic.review(user_message="q", intent_result=None,
                         response_text="回答", tool_results=[]) is None
    # 非法 JSON
    _patch_llm(monkeypatch, "not-json")
    assert critic.review(user_message="q", intent_result=None,
                         response_text="回答", tool_results=[]) is None


def test_review_prompt_contains_dimensions_and_draft(monkeypatch):
    """评审 prompt 固定四维度，且注入工具结果摘要与草拟回答（防回退性变更）。"""
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    captured = {}
    import dashscope

    def call(*a, **kw):
        captured["prompt"] = kw["messages"][0]["content"]
        return FakeResp(json.dumps({"passed": True}))
    monkeypatch.setattr(dashscope.Generation, "call", call)

    critic = ResponseCritic(summarize_fn=lambda tool, res: "KB摘要")
    critic.review(
        user_message="采砂监管要求", intent_result=_intent(IntentType.KNOWLEDGE_SEARCH),
        response_text="草拟内容ABC", tool_results=_results())
    prompt = captured["prompt"]
    for dimension in ("事实一致性", "来源标注", "完整性", "无过度承诺"):
        assert dimension in prompt
    assert "草拟内容ABC" in prompt
    assert "KB摘要" in prompt
