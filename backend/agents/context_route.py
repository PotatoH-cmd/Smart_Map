"""
context_route.py — 上下文路由协议（ContextRoute）—— 阶段8：生命周期协议契约。

把「来源 → 路由 → 预算 → 持久化级别 → 续传重建」收敛成唯一契约。
活流程（RunEngine 主路径）与续传（resume）共用同一套 `route_for(stage, ctx)`，
预算/优先级调整只改这张路由表，两边行为天然一致。

核心概念：
  ContextBlock   结构化上下文块：kind / source / durability / budget_tokens /
                 priority / payload / ttl
  durability     持久化级别：EPHEMERAL（活进程内现取）| SESSION（DB 可重建）
                 | CHECKPOINT（随 run_checkpoints 落盘，供续传重建）
  route_for      按路由表产出一 stage 需要的块（含预算裁剪、优先级排序）
  durable_blocks 该 stage 必须写入 run_checkpoints 的块（durability != EPHEMERAL）
  idempotency_key 工具步骤副作用幂等 key（run_id+step_id+params 哈希）

不改变 context_manager 公共签名（仅复用其工具：build_history_context /
trim_tool_summaries / BUDGET_* 常量）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .context_manager import (
    BUDGET_TOOL_RESULTS_CHARS,
    build_history_context,
    trim_tool_summaries,
)

# ----------------------------------------------------------------------
# 持久化级别
# ----------------------------------------------------------------------
EPHEMERAL = "ephemeral"    # 活进程内存现取，不入 checkpoint
SESSION = "session"        # 可由 sessions.db / 内存重建，仅供续传时拉取
CHECKPOINT = "checkpoint"  # 必须随 run_checkpoints 落盘，续传依赖
DURABILITIES = (EPHEMERAL, SESSION, CHECKPOINT)

# ----------------------------------------------------------------------
# 阶段（对齐 RunEngine 主路径与图节点）
# ----------------------------------------------------------------------
STAGE_INTENT = "intent"
STAGE_TOOL = "tool"
STAGE_OBSERVE = "observe"
STAGE_EXTRACT = "extract"
STAGE_SUMMARIZE = "summarize"
STAGE_CONFIRMATION = "confirmation"
STAGES = (
    STAGE_INTENT, STAGE_TOOL, STAGE_OBSERVE, STAGE_EXTRACT,
    STAGE_SUMMARIZE, STAGE_CONFIRMATION,
)

# ----------------------------------------------------------------------
# 块类型（kind）
# ----------------------------------------------------------------------
KIND_SYSTEM = "system"
KIND_HISTORY = "history"
KIND_FACTS = "facts"
KIND_WORKSPACE = "workspace"
KIND_TOOL_RESULTS = "tool_results"
KIND_TOOL_SUMMARIES = "tool_summaries"
KIND_CRITIC_FEEDBACK = "critic_feedback"
KIND_PENDING = "pending"
KIND_MISSING_PARAMS = "missing_params"
KIND_PREVIOUS = "previous"  # tool/observe 阶段上一节点（tool_results 或 LLM 决策）产物

# 默认令牌预算（token 估算见 estimate_tokens；裁剪时按此限额）
BUDGET_TOKENS_INTENT_HISTORY = 700
BUDGET_TOKENS_INTENT_FACTS = 300
BUDGET_TOKENS_WORKSPACE = 400
BUDGET_TOKENS_TOOL_RESULTS = 2100
BUDGET_TOKENS_SUMMARIZE = 2100
BUDGET_TOKENS_TOOL_SUMMARIES = 2100
BUDGET_TOKENS_CRITIC = 160

# 默认优先级（复用 context_manager 的算法思想：错误 > 数据 > 知识库 > 其余）
_PRIORITY_DEFAULT = 0


def estimate_tokens(text: Any) -> int:
    """确定性 token 估算：中文约 2 字符/token，英文约 4 字符/token（近似即可）。"""
    s = str(text or "")
    return max(1, len(s.encode("utf-8", errors="ignore")) // 3)


@dataclass
class ContextBlock:
    """结构化上下文块。payload 由 route_for 根据 ctx 物化（或由调用方自行填充）。"""
    kind: str
    source: str
    durability: str
    budget_tokens: int
    priority: int = _PRIORITY_DEFAULT
    payload: Any = None
    ttl: Optional[int] = None  # 秒；None 表示随 run 生命周期

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "durability": self.durability,
            "budget_tokens": self.budget_tokens,
            "priority": self.priority,
            "payload": self.payload,
            "ttl": self.ttl,
        }


@dataclass
class RoutingCtx:
    """route_for 的输入上下文（缺省字段均可为 None / 空）。"""
    session_id: str = ""
    user_message: str = ""
    chat_history: Optional[List[Dict]] = None
    tool_results: Optional[List[Dict]] = None
    tool_summaries: Optional[List[str]] = None
    workspace_summary: str = ""
    facts_context: str = ""
    critic_feedback: str = ""
    pending: Optional[Dict[str, Any]] = None
    missing_params: Optional[List[Dict[str, Any]]] = None


# ----------------------------------------------------------------------
# 路由表（唯一真相契约）
#  stage -> List[(kind, source, durability, budget_tokens, priority, builder_fn)]
# ----------------------------------------------------------------------

def _build_system_payload(ctx: RoutingCtx) -> str:
    return ctx.user_message or ""


def _build_history_payload(ctx: RoutingCtx) -> str:
    # 复用 context_manager 的历史构建与逐条截断
    return build_history_context(ctx.chat_history)


def _build_facts_payload(ctx: RoutingCtx) -> str:
    return ctx.facts_context or ""


def _build_workspace_payload(ctx: RoutingCtx) -> str:
    return ctx.workspace_summary or ""


def _build_tool_results_payload(ctx: RoutingCtx) -> List[str]:
    if not ctx.tool_results:
        return []
    out = []
    for r in ctx.tool_results:
        if not isinstance(r, dict):
            continue
        tool = r.get("tool_name") or "unknown"
        res = r.get("result")
        try:
            summary = json.dumps(res, ensure_ascii=False)[:600]
        except (TypeError, ValueError):
            summary = str(res or "")[:600]
        out.append(f"[{tool}] {summary}")
    return out


def _build_tool_summaries_payload(ctx: RoutingCtx) -> List[str]:
    # 复用 context_manager 的优先级裁剪（错误 > 数据 > 知识库 > 其余）
    return trim_tool_summaries(ctx.tool_summaries or [], BUDGET_TOOL_RESULTS_CHARS)


def _build_critic_feedback_payload(ctx: RoutingCtx) -> str:
    return ctx.critic_feedback or ""


def _build_pending_payload(ctx: RoutingCtx) -> Dict[str, Any]:
    return ctx.pending or {}


def _build_missing_params_payload(ctx: RoutingCtx) -> List[Dict[str, Any]]:
    return ctx.missing_params or []


# 路由模板：stage -> block 定义（build 字段为调用 shape，durable 由 durability 推导）
_ROUTE_TABLE: Dict[str, List[Dict[str, Any]]] = {
    STAGE_INTENT: [
        {"kind": KIND_SYSTEM, "source": "static", "durability": SESSION,
         "budget_tokens": 200, "priority": 4, "build": _build_system_payload},
        {"kind": KIND_HISTORY, "source": "sessions.db", "durability": SESSION,
         "budget_tokens": BUDGET_TOKENS_INTENT_HISTORY, "priority": 3,
         "build": _build_history_payload},
        {"kind": KIND_FACTS, "source": "fact_memory", "durability": SESSION,
         "budget_tokens": BUDGET_TOKENS_INTENT_FACTS, "priority": 2,
         "build": _build_facts_payload},
        {"kind": KIND_WORKSPACE, "source": "workspace_state", "durability": SESSION,
         "budget_tokens": BUDGET_TOKENS_WORKSPACE, "priority": 2,
         "build": _build_workspace_payload},
    ],
    STAGE_TOOL: [
        {"kind": KIND_PREVIOUS, "source": "context_manager", "durability": CHECKPOINT,
         "budget_tokens": 200, "priority": 3, "build": lambda ctx: ctx.user_message},
        {"kind": KIND_TOOL_RESULTS, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": BUDGET_TOKENS_TOOL_RESULTS, "priority": 2,
         "build": _build_tool_results_payload},
    ],
    STAGE_OBSERVE: [
        {"kind": KIND_TOOL_RESULTS, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": BUDGET_TOKENS_TOOL_RESULTS, "priority": 2,
         "build": _build_tool_results_payload},
    ],
    STAGE_EXTRACT: [
        {"kind": KIND_TOOL_RESULTS, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": BUDGET_TOKENS_TOOL_RESULTS, "priority": 2,
         "build": _build_tool_results_payload},
    ],
    STAGE_SUMMARIZE: [
        {"kind": KIND_SYSTEM, "source": "static", "durability": SESSION,
         "budget_tokens": 200, "priority": 4, "build": _build_system_payload},
        {"kind": KIND_HISTORY, "source": "sessions.db", "durability": SESSION,
         "budget_tokens": BUDGET_TOKENS_INTENT_HISTORY, "priority": 3,
         "build": _build_history_payload},
        {"kind": KIND_TOOL_SUMMARIES, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": BUDGET_TOKENS_TOOL_SUMMARIES, "priority": 2,
         "build": _build_tool_summaries_payload},
        {"kind": KIND_CRITIC_FEEDBACK, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": BUDGET_TOKENS_CRITIC, "priority": 1,
         "build": _build_critic_feedback_payload},
    ],
    STAGE_CONFIRMATION: [
        {"kind": KIND_PENDING, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": 200, "priority": 2, "build": _build_pending_payload},
        {"kind": KIND_MISSING_PARAMS, "source": "run_checkpoints", "durability": CHECKPOINT,
         "budget_tokens": 200, "priority": 1, "build": _build_missing_params_payload},
    ],
}


def _materialize(builder: Any, ctx: RoutingCtx) -> Any:
    """把块定义物化为 payload。构建失败静默返回 None（不阻断路由）。"""
    try:
        return builder(ctx) if ctx is not None else None
    except Exception:
        return None


def route_for(stage: str, ctx: Optional[RoutingCtx] = None) -> List[ContextBlock]:
    """按路由表产出某 stage 需要的上下文块（含预算裁剪与优先级排序）。

    返回块按路由表定义顺序（已含人工设定的优先级），同一块内超预算文本
    由 build 侧复用 context_manager 完成截断（trim_tool_summaries /
    build_history_context）。
    """
    if stage not in _ROUTE_TABLE:
        return []
    blocks: List[ContextBlock] = []
    for tpl in _ROUTE_TABLE[stage]:
        payload = _materialize(tpl["build"], ctx)
        blocks.append(ContextBlock(
            kind=tpl["kind"],
            source=tpl["source"],
            durability=tpl["durability"],
            budget_tokens=tpl["budget_tokens"],
            priority=tpl.get("priority", _PRIORITY_DEFAULT),
            payload=payload,
        ))
    return blocks


def durable_blocks(stage: str) -> List[ContextBlock]:
    """返回该 stage 需要写入 run_checkpoints 的块（durability != EPHEMERAL）。"""
    return [b for b in route_for(stage, None) if b.durability != EPHEMERAL]


def route_meta(stage: str) -> Dict[str, Any]:
    """返回路由表的元信息（stage → 各块的 kind/source/durability/budget），
    供 checkpoint 的 route_meta 快照使用。"""
    return {
        "version": 1,
        "stage": stage,
        "blocks": [
            {
                "kind": b.kind,
                "source": b.source,
                "durability": b.durability,
                "budget_tokens": b.budget_tokens,
                "priority": b.priority,
            }
            for b in route_for(stage, None)
        ],
    }


def idempotency_key(run_id: str, step_id: int, params: Optional[Dict[str, Any]]) -> str:
    """工具步骤副作用幂等 key。

    稳定契约：同 (run_id, step_id, params) 必然得到相同 key（供 resume 去重）；
    参数序列化做 key 排序与 json 归一，保证同义 dict 稳定。返回 hex 摘要。
    """
    try:
        params_norm = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        params_norm = repr(params or {})
    raw = f"{run_id}::{step_id}::{params_norm}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


# ----------------------------------------------------------------------
# route_meta 版本化 & 兼容旧 state_json
# ----------------------------------------------------------------------

ROUTE_META_VERSION = 1


def read_route_meta(state_json: Dict[str, Any]) -> Dict[str, Any]:
    """把 checkpoint 的 state_json 规范化为 route_meta（兼容旧格式）。

    旧 checkpoint（无 version 字段）按旧语义解析：stage_cursor 取 last_node
    或空串，route 为空 dict，其余字段原样保留。返回新增 `version` 字段的
    规范化 dict（不改动原始 dict）。
    """
    if isinstance(state_json, dict) and "version" in state_json:
        meta = dict(state_json)
        meta["_legacy"] = False
        return meta
    if isinstance(state_json, dict):
        meta = dict(state_json)
        meta["version"] = 0
        meta["_legacy"] = True
        meta.setdefault("stage_cursor", meta.get("last_node") or "")
        meta.setdefault("route", {})
        return meta
    return {"version": 0, "stage_cursor": "", "route": {}, "_legacy": True}