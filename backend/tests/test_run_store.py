"""
test_run_store.py — 阶段7：RunStore 三表 CRUD + 事件契约 + checkpoint 序列化往返。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.run_store import (
    RunStore, RunEvent, DEFAULT_DB_PATH,
    RUNNING, AWAITING_CONFIRMATION, AWAITING_INPUT, COMPLETED, FAILED, CANCELLED,
    INTERRUPTED, RESUMING,
)


@pytest.fixture()
def store(tmp_path):
    """临时库隔离，不触碰真实 sessions.db。"""
    return RunStore(db_path=str(tmp_path / "test_runs.db"))


# ----------------------------------------------------------------------
# run 主记录 CRUD
# ----------------------------------------------------------------------

def test_create_and_get_run(store):
    store.create_run("r1", "s1", "帮我查数据")
    run = store.get_run("r1")
    assert run is not None
    assert run["status"] == RUNNING
    assert run["session_id"] == "s1"
    assert run["user_message"] == "帮我查数据"
    assert run["pending_json"] is None


def test_get_run_missing(store):
    assert store.get_run("no_such_run") is None


def test_update_status_validation(store):
    store.create_run("r1", "s1", "msg")
    store.update_status("r1", COMPLETED)
    assert store.get_run("r1")["status"] == COMPLETED
    with pytest.raises(ValueError):
        store.update_status("r1", "not_a_status")


def test_pending_roundtrip(store):
    store.create_run("r1", "s1", "msg")
    pending = {"pending_type": "input", "missing": [{"param": "distance"}]}
    store.set_pending("r1", json.dumps(pending, ensure_ascii=False), AWAITING_INPUT)
    run = store.get_run("r1")
    assert run["status"] == AWAITING_INPUT
    assert json.loads(run["pending_json"])["pending_type"] == "input"

    store.clear_pending("r1")
    assert store.get_run("r1")["pending_json"] is None


def test_get_pending_by_session(store):
    store.create_run("r1", "s1", "msg1")
    store.create_run("r2", "s1", "msg2")
    # 仅 r2 处于 pending：无论时间戳粒度如何都只应返回 r2
    store.set_pending("r2", '{"pending_type": "input"}', AWAITING_INPUT)

    got = store.get_pending_by_session("s1")
    assert got["run_id"] == "r2"
    assert got["pending"]["pending_type"] == "input"

    # 其他会话无 pending
    assert store.get_pending_by_session("s9") is None


def test_cancel_flow(store):
    store.create_run("r1", "s1", "msg")
    assert not store.is_cancelled("r1")
    store.set_cancelled("r1")
    assert store.is_cancelled("r1")
    assert store.get_run("r1")["status"] == CANCELLED


# ----------------------------------------------------------------------
# 事件流（seq 补拉）
# ----------------------------------------------------------------------

def test_append_and_get_events_since(store):
    store.create_run("r1", "s1", "msg")
    for i in range(5):
        store.append_event("r1", {"type": "status", "stage": f"s{i}", "seq": i + 1}, i + 1)

    all_events = store.get_events("r1")
    assert len(all_events) == 5
    assert all_events[0]["stage"] == "s0"

    # 断线补拉：since=2 → 只剩 seq 3/4/5
    tail = store.get_events("r1", since_seq=2)
    assert [e["seq"] for e in tail] == [3, 4, 5]

    # since 超过最大 seq → 空
    assert store.get_events("r1", since_seq=99) == []


def test_run_event_contract(store):
    """RunEvent 契约：payload 展开进顶层（前端 event.result 兼容）。"""
    ev = RunEvent(
        type="pending", stage="pending", title="等待补充信息",
        message="缺参数", details=["distance"],
        payload={"pending_type": "input", "missing": [{"param": "distance", "label": "距离"}]},
    )
    d = ev.to_dict()
    assert d["type"] == "pending"
    assert d["pending_type"] == "input"      # payload 展开
    assert d["missing"][0]["param"] == "distance"
    assert d["stage"] == "pending"

    # 往返：from_dict 把非保留字段归位到 payload
    ev2 = RunEvent.from_dict(d)
    assert ev2.type == "pending"
    assert ev2.payload["pending_type"] == "input"

    # 最终事件带 result（旧协议兼容：result 在顶层）
    final = RunEvent(type="final", stage="final", payload={"result": {"success": True}})
    fd = final.to_dict()
    assert fd["result"]["success"] is True


# ----------------------------------------------------------------------
# checkpoint 序列化往返（IntentResult / TaskStep 用 model_dump/model_validate）
# ----------------------------------------------------------------------

def test_checkpoint_roundtrip_with_intent(store):
    from agents.intent_types import IntentType, IntentResult, TaskStep

    intent = IntentResult(
        primary_intent=IntentType.DATA_QUERY,
        confidence=0.9,
        entities=["固始县"],
        task_context="查询固始县采砂场数量",
        execution_plan=[TaskStep(
            step_id=1, action="查询", tool="postgresql_tool",
            params={"operation": "query"}, reasoning="r", expected_output="o",
        )],
        requires_confirmation=False,
        suggestions=[],
    )
    state = {
        "user_message": "帮我查数据",
        "chat_history": [{"role": "user", "content": "hi"}],
        "intent_result": intent.model_dump(),
        "remaining_steps": [s.model_dump() for s in intent.execution_plan],
        "done_steps": [],
        "pending": None,
        "session_id": "s1",
    }
    store.create_run("r1", "s1", "帮我查数据")
    store.save_checkpoint("r1", state)

    loaded = store.load_checkpoint("r1")
    assert loaded is not None
    # 反序列化往返：json → IntentResult / TaskStep
    intent2 = IntentResult.model_validate(loaded["intent_result"])
    assert intent2.primary_intent == IntentType.DATA_QUERY
    assert intent2.confidence == 0.9
    steps2 = [TaskStep.model_validate(s) for s in loaded["remaining_steps"]]
    assert steps2[0].tool == "postgresql_tool"
    assert loaded["chat_history"][0]["role"] == "user"


def test_checkpoint_missing_and_delete(store):
    assert store.load_checkpoint("no_run") is None
    store.save_checkpoint("r1", {"a": 1})
    assert store.load_checkpoint("r1") == {"a": 1}
    store.delete_checkpoint("r1")
    assert store.load_checkpoint("r1") is None


def test_checkpoint_bad_json(store):
    """损坏的 checkpoint JSON 静默返回 None。"""
    store.save_checkpoint("r1", {"a": 1})
    import sqlite3
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE run_checkpoints SET state_json='{broken' WHERE run_id='r1'"
        )
        conn.commit()
    assert store.load_checkpoint("r1") is None


# ----------------------------------------------------------------------
# 状态常量契约
# ----------------------------------------------------------------------

def test_status_constants():
    assert set([RUNNING, AWAITING_CONFIRMATION, AWAITING_INPUT,
                COMPLETED, FAILED, CANCELLED]) == {
        "running", "awaiting_confirmation", "awaiting_input",
        "completed", "failed", "cancelled",
    }
    assert DEFAULT_DB_PATH.endswith("sessions.db")


# ----------------------------------------------------------------------
# 阶段8：宕机 watchdog（heartbeat / last_node / 老化判定）
# ----------------------------------------------------------------------

def test_heartbeat_and_last_node_columns(store):
    store.create_run("r1", "s1", "msg")
    store.update_heartbeat("r1", "tool:pg:1")
    run = store.get_run("r1")
    assert run["heartbeat_at"] is not None
    assert run["last_node"] == "tool:pg:1"
    # 再次更新覆盖
    store.update_heartbeat("r1", "summarize")
    assert store.get_run("r1")["last_node"] == "summarize"


def test_is_stale_updated_at(store):
    from datetime import datetime, timedelta, timezone
    fresh = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    old = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=10))
    old_iso = old.isoformat(timespec="seconds")
    assert not store.is_stale_updated_at(fresh, max_age_seconds=300)   # 未超时
    assert store.is_stale_updated_at(old_iso, max_age_seconds=300)     # 超时
    assert store.is_stale_updated_at(None, 300)                        # 缺失视为过期
    assert store.is_stale_updated_at("not-a-date", 300)                # 解析失败视为过期


def test_find_stale_runs_and_mark_interrupted(store):
    from datetime import datetime, timedelta, timezone
    store.create_run("r1", "s1", "msg1")
    store.create_run("r2", "s1", "msg2")
    old = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=10))
    with __import__("sqlite3").connect(store.db_path) as conn:
        conn.execute("UPDATE runs SET updated_at=? WHERE run_id='r1'", (old.isoformat(timespec="seconds"),))
        conn.commit()
    stale = store.find_stale_runs(300)
    assert {r["run_id"] for r in stale} == {"r1"}   # r2 仍新鲜
    store.mark_interrupted("r1")
    assert store.get_run("r1")["status"] == INTERRUPTED
    store.mark_resuming("r1")
    assert store.get_run("r1")["status"] == RESUMING


def test_get_last_run_by_session(store):
    from datetime import datetime, timedelta, timezone
    store.create_run("r1", "s1", "m1")
    store.create_run("r2", "s1", "m2")
    store.create_run("r3", "s9", "m3")
    # r2 建立后手动把 updated_at 推后，确保排序确定性（同秒插入有平局）
    later = (datetime.now(timezone.utc).astimezone() + timedelta(seconds=5))
    with __import__("sqlite3").connect(store.db_path) as conn:
        conn.execute("UPDATE runs SET updated_at=? WHERE run_id='r2'",
                     (later.isoformat(timespec="seconds"),))
        conn.commit()
    last = store.get_last_run_by_session("s1")
    assert last["run_id"] == "r2"   # 最近一次（按 updated_at 倒序）
    assert store.get_last_run_by_session("no_such") is None


# ----------------------------------------------------------------------
# 阶段8：工具副作用幂等 key
# ----------------------------------------------------------------------

def test_idempotency_record_check(store):
    store.record_idempotency_key("r1", 1, "hash-a")
    store.record_idempotency_key("r1", 2, "hash-b")
    assert store.check_idempotency_key("r1", 1) is True
    assert store.check_idempotency_key("r1", 2) is True
    assert store.check_idempotency_key("r1", 3) is False


def test_idempotency_dedup_records_ignore_duplicate(store):
    store.record_idempotency_key("r1", 1, "hash-a")
    store.record_idempotency_key("r1", 1, "hash-a")   # 重复插入安全
    store.record_idempotency_key("r1", 1, "hash-zzz")  # 同 (run,step) 不覆盖（INSERT OR IGNORE）
    with __import__("sqlite3").connect(store.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys WHERE run_id='r1' AND step_id=1"
        ).fetchone()[0]
    assert n == 1
    assert store.check_idempotency_key("r1", 1) is True


def test_idempotency_cleanup(store):
    store.record_idempotency_key("r1", 1, "a")
    store.create_run("r1", "s1", "msg")  # 终态？
    assert store.check_idempotency_key("r1", 1) is True
    store.delete_idempotency_key("r1", 1)
    assert store.check_idempotency_key("r1", 1) is False


# ----------------------------------------------------------------------
# 阶段8：route_meta 版本化（兼容旧 state_json）
# ----------------------------------------------------------------------

def test_route_meta_new_version_roundtrip(store):
    from agents.context_route import read_route_meta, route_meta
    state = {
        "version": 1,
        "stage_cursor": "tool:pg:1",
        "route": {"tool:pg:1": route_meta("tool")},
        "user_message": "查数据",
        "tool_results": [{"tool_name": "postgresql_tool", "result": {"success": True}}],
        "done_steps": [],
        "remaining_steps": [],
    }
    store.create_run("r1", "s1", "查数据")
    store.save_checkpoint("r1", state)
    loaded = store.load_checkpoint("r1")
    meta = read_route_meta(loaded)
    assert meta["version"] == 1
    assert meta["_legacy"] is False
    assert meta["stage_cursor"] == "tool:pg:1"


def test_route_meta_read_legacy_state_json(store):
    """旧 checkpoint（无 version 字段）按旧语义解析，不报错。"""
    from agents.context_route import read_route_meta
    legacy = {
        "user_message": "查数据",
        "tool_results": [{"tool_name": "pg", "result": {"success": True}}],
        "remaining_steps": [],
        "done_steps": [1],
        "pending": None,
        "session_id": "s1",
    }
    store.create_run("r1", "s1", "查数据")
    store.save_checkpoint("r1", legacy)
    loaded = store.load_checkpoint("r1")
    assert loaded == legacy  # 原样存取
    meta = read_route_meta(loaded)
    assert meta["version"] == 0
    assert meta["_legacy"] is True
    assert meta["stage_cursor"] == ""
    assert meta["route"] == {}
