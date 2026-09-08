"""
test_service_fallback.py — P2：微服务回退链回归。

覆盖：
1. 意图服务不可达（INTENT_SERVICE_URL 指向死端口 / httpx 异常）→ _remote_intent_analyze
   返回 None，由调用方回退进程内 IntentAgent（意图识别永不中断）。
2. 意图服务正常响应 → 正确解析 IntentResult（含 execution_plan 反序列化）。
3. 未配置 INTENT_SERVICE_URL → 直接走本地（返回 None 且不发 HTTP）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest

from agents.task_executor import TaskExecutor
from agents.intent_types import IntentResult


@pytest.fixture()
def bare_executor(monkeypatch):
    """绕过 __init__ 的重资源装配，仅挂回退所需的属性。"""
    from unittest.mock import MagicMock

    ex = object.__new__(TaskExecutor)
    ex.intent_agent = MagicMock()
    ex.intent_agent.analyze.return_value = IntentResult(
        primary_intent="data_query", confidence=0.8, task_context="",
    )
    yield ex


DEAD_URL = "http://127.0.0.1:1"  # 保留端口，必然连接失败


def test_remote_intent_fallback_on_dead_port(bare_executor, monkeypatch):
    """意图服务不可达 → 返回 None（调用方回退本地 IntentAgent）。"""
    monkeypatch.setenv("INTENT_SERVICE_URL", DEAD_URL)
    out = bare_executor._remote_intent_analyze("郑州天气", [], None)
    assert out is None


def test_remote_intent_fallback_on_http_error(bare_executor, monkeypatch):
    """HTTP 层异常 → 返回 None 并打回退日志，不向上抛。"""
    monkeypatch.setenv("INTENT_SERVICE_URL", "http://127.0.0.1:8010")

    def boom(*a, **kw):
        raise httpx.ConnectError("simulated outage")

    monkeypatch.setattr(httpx, "post", boom)
    out = bare_executor._remote_intent_analyze("查一下实测数据", [], None)
    assert out is None


def test_remote_intent_parses_plan(bare_executor, monkeypatch):
    """远程正常响应 → IntentResult 反序列化含 execution_plan。"""
    monkeypatch.setenv("INTENT_SERVICE_URL", "http://127.0.0.1:8010")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "primary_intent": "weather_query",
                "confidence": 0.95,
                "entities": ["郑州"],
                "task_context": "天气查询",
                "execution_plan": [
                    {"step_id": 1, "tool": "weather_tool", "action": "query",
                     "params": {"city": "郑州"}, "reasoning": "获取实时天气",
                     "expected_output": "郑州实时天气"},
                ],
                "requires_confirmation": False,
                "suggestions": [],
            }

    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = bare_executor._remote_intent_analyze("郑州天气", [], None)
    assert out is not None and out.primary_intent == "weather_query"
    assert out.execution_plan and out.execution_plan[0].tool == "weather_tool"
    assert captured["url"].endswith("/v1/intent/analyze")
    # 业务上下文必须注入（意图服务自身无 DB schema 知识）
    assert "db_schema" in captured["payload"]["context"]["extra"]


def test_remote_intent_skipped_without_url(bare_executor, monkeypatch):
    """未配置 INTENT_SERVICE_URL → 不发 HTTP 直接返回 None。"""
    monkeypatch.delenv("INTENT_SERVICE_URL", raising=False)

    def fail_post(*a, **kw):
        raise AssertionError("未配置 URL 时不应发起 HTTP 请求")

    monkeypatch.setattr(httpx, "post", fail_post)
    assert bare_executor._remote_intent_analyze("任意", [], None) is None
