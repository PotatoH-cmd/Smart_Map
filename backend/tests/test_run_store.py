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
