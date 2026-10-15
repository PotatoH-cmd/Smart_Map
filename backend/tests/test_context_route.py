"""
test_context_route.py — 阶段8：上下文路由协议（ContextRoute）。

覆盖：route_for 各 stage 返回块顺序/预算、durable_blocks 与路由表一致、
idempotency_key 稳定性、route_meta 版本化兼容旧 state_json。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents import context_route as cr


# ----------------------------------------------------------------------
# route_for：顺序 / 预算 / 来源
# ----------------------------------------------------------------------

def test_route_for_intent_blocks_order():
    blocks = cr.route_for(cr.STAGE_INTENT)
    kinds = [b.kind for b in blocks]
    # intent 契约：system + history + facts + workspace
    assert kinds[:4] == [cr.KIND_SYSTEM, cr.KIND_HISTORY, cr.KIND_FACTS, cr.KIND_WORKSPACE]
    # 全部持久化级别为 SESSION（活进程内现取，续传由 sessions.db 重建）
    assert all(b.durability == cr.SESSION for b in blocks)


def test_route_for_stage_routes():
    # tool/observe/extract 都要 tool_results（CHECKPOINT）
    assert any(b.durability == cr.CHECKPOINT for b in cr.route_for(cr.STAGE_TOOL))
    assert [b.kind for b in cr.route_for(cr.STAGE_OBSERVE)] == [cr.KIND_TOOL_RESULTS]
    assert [b.kind for b in cr.route_for(cr.STAGE_EXTRACT)] == [cr.KIND_TOOL_RESULTS]
    # summarize：system + history + tool_summaries + critic_feedback
    kinds = [b.kind for b in cr.route_for(cr.STAGE_SUMMARIZE)]
    assert cr.KIND_TOOL_SUMMARIES in kinds
    assert cr.KIND_CRITIC_FEEDBACK in kinds
    # confirmation：pending + missing_params（均为 CHECKPOINT）
    kinds = [b.kind for b in cr.route_for(cr.STAGE_CONFIRMATION)]
    assert kinds == [cr.KIND_PENDING, cr.KIND_MISSING_PARAMS]
    assert all(b.durability == cr.CHECKPOINT for b in cr.route_for(cr.STAGE_CONFIRMATION))


def test_route_for_unknown_stage_empty():
    assert cr.route_for("no_such_stage") == []


def test_route_for_payload_from_ctx():
    ctx = cr.RoutingCtx(
        chat_history=[{"role": "user", "content": "你好"}],
        tool_summaries=["错误：数据库连接失败", "查询结果：100 行数据"],
    )
    intent_blocks = cr.route_for(cr.STAGE_INTENT, ctx)
    hist = next(b for b in intent_blocks if b.kind == cr.KIND_HISTORY)
    assert "你好" in hist.payload

    summ = {b.kind: b.payload for b in cr.route_for(cr.STAGE_SUMMARIZE, ctx)}
    # 复用 context_manager 裁剪（错误优先级 > 数据）
    assert summ[cr.KIND_TOOL_SUMMARIES][0].startswith("错误")


# ----------------------------------------------------------------------
# durable_blocks：仅非 EPHEMERAL 落 checkpoint
# ----------------------------------------------------------------------

def test_durable_blocks_ignores_ephemeral():
    # 无块声明 EPHEMERAL（本路由表 SESSION/CHECKPOINT），durable 应等于全部块
    for stage in cr.STAGES:
        durable = cr.durable_blocks(stage)
        assert durable == cr.route_for(stage, None)
        assert all(b.durability != cr.EPHEMERAL for b in durable)


def test_durable_blocks_valid_durability():
    for stage in cr.STAGES:
        for b in cr.route_for(stage, None):
            assert b.durability in cr.DURABILITIES


# ----------------------------------------------------------------------
# idempotency_key：同参数稳定 / 异参数不同 / 与 run_id 绑定
# ----------------------------------------------------------------------

def test_idempotency_key_stable():
    params = {"query": "砂场", "limit": 5}
    k1 = cr.idempotency_key("run1", 3, params)
    k2 = cr.idempotency_key("run1", 3, params)
    assert k1 == k2
    assert isinstance(k1, str) and len(k1) > 0


def test_idempotency_key_different_on_params_or_step():
    assert cr.idempotency_key("r", 1, {"q": 1}) != cr.idempotency_key("r", 1, {"q": 2})
    assert cr.idempotency_key("r", 1, {"q": 1}) != cr.idempotency_key("r", 2, {"q": 1})
    assert cr.idempotency_key("r", 1, {}) != cr.idempotency_key("s", 1, {})


def test_idempotency_key_dict_order_same_semantics():
    # 同义 dict（键序不同）应得到相同 key（sort_keys 归一）
    assert cr.idempotency_key("r", 1, {"a": 1, "b": 2}) == \
        cr.idempotency_key("r", 1, {"b": 2, "a": 1})


# ----------------------------------------------------------------------
# route_meta 版本化：兼容旧 state_json
# ----------------------------------------------------------------------

def test_read_route_meta_new_version():
    data = {"version": 1, "stage_cursor": "tool:pg:1", "route": {}, "tool_results": []}
    meta = cr.read_route_meta(data)
    assert meta["version"] == 1
    assert meta["_legacy"] is False


def test_read_route_meta_legacy():
    old = {"tool_results": [1], "last_node": "tool:pg:1"}
    meta = cr.read_route_meta(old)
    # 旧格式补版本号 + 从 last_node 推断 stage_cursor
    assert meta["version"] == 0
    assert meta["_legacy"] is True
    assert meta["stage_cursor"] == "tool:pg:1"
    assert meta["route"] == {}
    # 原 dict 不被改动
    assert "version" not in old


def test_read_route_meta_non_dict():
    meta = cr.read_route_meta(None)
    assert meta["_legacy"] is True
    assert meta["version"] == 0


def test_route_meta_snapshot():
    snap = cr.route_meta(cr.STAGE_SUMMARIZE)
    assert snap["version"] == 1
    assert snap["stage"] == cr.STAGE_SUMMARIZE
    kinds = [b["kind"] for b in snap["blocks"]]
    assert cr.KIND_TOOL_SUMMARIES in kinds


# ----------------------------------------------------------------------
# estimate_tokens
# ----------------------------------------------------------------------

def test_estimate_tokens():
    assert cr.estimate_tokens("") >= 1
    assert cr.estimate_tokens("测试文本") > 1
    assert cr.estimate_tokens(123) >= 1