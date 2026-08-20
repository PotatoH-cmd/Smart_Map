"""
test_context_manager.py — 阶段7：上下文预算裁剪优先级与边界 + 历史压缩。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents import context_manager as cm


# ----------------------------------------------------------------------
# build_history_context：最近 N 轮 + 逐条截断
# ----------------------------------------------------------------------

def test_build_history_context_empty():
    assert cm.build_history_context(None) == ""
    assert cm.build_history_context([]) == ""


def test_build_history_context_turns_and_filter():
    history = []
    for i in range(20):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    # 混入 system 消息应被过滤（插在中间，不影响最近 3 轮计数）
    history.insert(30, {"role": "system", "content": "sys"})

    ctx = cm.build_history_context(history, max_turns=3)
    # 只保留最近 3 轮 = 6 条 user/assistant（即 u17..a19）
    lines = [l for l in ctx.splitlines() if l.startswith("- ")]
    assert len(lines) == 6
    assert "u17" in lines[0]
    assert "a19" in lines[-1]
    assert "sys" not in ctx


def test_build_history_context_per_msg_truncation():
    history = [{"role": "user", "content": "长" * 300}]
    ctx = cm.build_history_context(history, per_msg=100)
    # 100 字截断
    assert len([l for l in ctx.splitlines() if l.startswith("- user")][0]) <= len("- user: ") + 100
    assert "长" * 300 not in ctx


# ----------------------------------------------------------------------
# trim_tool_summaries：优先级裁剪（错误 > DB 数据 > 知识库 > 其余）
# ----------------------------------------------------------------------

def test_trim_within_budget_unchanged():
    summaries = ["a", "bb", "ccc"]
    assert cm.trim_tool_summaries(summaries, max_chars=100) == summaries
    assert cm.trim_tool_summaries([], max_chars=100) == []


def test_trim_priority_error_first():
    # 预算只够 1 条：错误信息优先级最高
    summaries = [
        "知识库检索：政策文档说明",
        "查询结果：共 100 条记录",
        "错误：数据库连接失败，不存在表",
    ]
    kept = cm.trim_tool_summaries(summaries, max_chars=10)
    assert len(kept) == 1
    assert kept[0].startswith("错误")


def test_trim_priority_db_over_kb():
    summaries = ["知识库：检索到政策文档", "查询结果：12 行数据"]
    kept = cm.trim_tool_summaries(summaries, max_chars=11)
    assert len(kept) == 1
    assert kept[0].startswith("查询结果")


def test_trim_stable_order_within_priority():
    # 同优先级保序（稳定排序）
    summaries = ["错误A：xxx", "错误B：yyy"]
    kept = cm.trim_tool_summaries(summaries, max_chars=8)
    assert len(kept) == 1
    assert kept[0].startswith("错误A")


def test_trim_boundary_exact_budget():
    summaries = ["abc", "def", "ghi"]
    kept = cm.trim_tool_summaries(summaries, max_chars=6)
    # 恰好容纳前两条（按原序）
    assert kept == ["abc", "def"]


def test_trim_zero_budget():
    summaries = ["abc", "def"]
    assert cm.trim_tool_summaries(summaries, max_chars=0) == []


def test_trim_total_length_within_budget():
    summaries = ["错误：连接失败", "查询：100 行", "知识库：政策", "普通文本"]
    kept = cm.trim_tool_summaries(summaries, max_chars=30)
    total = sum(len(s) for s in kept)
    assert total <= 30
    # 所有保留条目按原顺序
    assert kept == [s for s in summaries if s in kept]


# ----------------------------------------------------------------------
# load_history_from_db：读库裁剪 / 失败静默
# ----------------------------------------------------------------------

def test_load_history_from_db(tmp_path):
    db = tmp_path / "sessions.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
        for i in range(10):
            conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
                         ("s1", "user" if i % 2 == 0 else "assistant", f"m{i}"))
        conn.commit()

    history = cm.load_history_from_db(str(db), "s1", max_turns=2)
    assert [h["content"] for h in history] == ["m6", "m7", "m8", "m9"]


def test_load_history_from_db_default_session_and_bad_path():
    assert cm.load_history_from_db("/nonexistent/x.db", "default") == []
    # 读库失败静默返回空
    assert cm.load_history_from_db("/nonexistent/x.db", "s1") == []


# ----------------------------------------------------------------------
# ContextManager.compress_history：滚动摘要（llm=None 降级截断）
# ----------------------------------------------------------------------

class _FakeWorkspace:
    def __init__(self):
        self.saved = []

    def set_last_summary(self, session_id, summary):
        self.saved.append((session_id, summary))


def _make_messages(turns):
    msgs = []
    for i in range(turns):
        msgs.append({"role": "user", "content": f"用户问第{i}轮"})
        msgs.append({"role": "assistant", "content": f"助手答第{i}轮"})
    return msgs


def test_compress_history_below_threshold_noop():
    ws = _FakeWorkspace()
    mgr = cm.ContextManager(workspace=ws)
    import asyncio
    ok = asyncio.run(mgr.compress_history("s1", _make_messages(10)))
    assert ok is False
    assert ws.saved == []


def test_compress_history_truncate_fallback():
    ws = _FakeWorkspace()
    mgr = cm.ContextManager(workspace=ws)
    import asyncio
    msgs = _make_messages(15)  # 超 12 轮
    ok = asyncio.run(mgr.compress_history("s1", msgs, llm=None))
    assert ok is True
    session_id, summary = ws.saved[0]
    assert session_id == "s1"
    # 截断降级：含旧轮次内容且不超过 300 字
    assert "用户问第0轮" in summary
    assert len(summary) <= cm.LAST_SUMMARY_CHARS


def test_compress_history_llm_path():
    class _FakeLLM:
        def invoke(self, messages):
            class _Resp:
                content = "这是 LLM 生成的滚动摘要"
            return _Resp()

    ws = _FakeWorkspace()
    mgr = cm.ContextManager(workspace=ws)
    import asyncio
    ok = asyncio.run(mgr.compress_history("s1", _make_messages(13), llm=_FakeLLM()))
    assert ok is True
    assert ws.saved[0][1] == "这是 LLM 生成的滚动摘要"


def test_compress_history_llm_failure_degrades():
    class _BoomLLM:
        def invoke(self, messages):
            raise RuntimeError("LLM 不可用")

    ws = _FakeWorkspace()
    mgr = cm.ContextManager(workspace=ws)
    import asyncio
    ok = asyncio.run(mgr.compress_history("s1", _make_messages(13), llm=_BoomLLM()))
    # 失败静默降级为截断，仍成功保存
    assert ok is True
    assert "用户问第0轮" in ws.saved[0][1]


# ----------------------------------------------------------------------
# 预算常量契约（禁止魔法数字散落）
# ----------------------------------------------------------------------

def test_budget_constants():
    assert cm.BUDGET_WORKSPACE_CHARS == 800
    assert cm.BUDGET_HISTORY_TURNS == 6
    assert cm.BUDGET_HISTORY_PER_MSG == 100
    assert cm.BUDGET_TOOL_RESULTS_CHARS == 4000
    assert cm.COMPRESS_THRESHOLD_TURNS == 12
