"""
run_store.py — Run 生命周期持久化层（阶段0：存储与契约）。

三张表（复用 sessions.db，CREATE TABLE IF NOT EXISTS，不触碰既有表）：
  runs            — run 主记录（状态机 + pending 载荷）
  run_events      — 事件流（seq 递增，支持断线补拉）
  run_checkpoints — 执行现场（plan 进度 + 已完成结果，用于 resume）

统一事件契约 RunEvent：
  {seq, run_id, type, stage, title, message, details, payload}
  兼容现有前端认识的事件类型，新增 verification / pending / cancelled。
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# 会话库路径单一来源（core/db → core/config）
from core.db import DB_PATH as DEFAULT_DB_PATH  # noqa: E402

# 状态机（对齐文章 Run 生命周期）
RUNNING = "running"
AWAITING_CONFIRMATION = "awaiting_confirmation"
AWAITING_INPUT = "awaiting_input"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
# 宕机断点续传（阶段8）：RUNNING 超时 → INTERRUPTED → resume 时置 RESUMING
INTERRUPTED = "interrupted"
RESUMING = "resuming"

ALL_STATUSES = (
    RUNNING, AWAITING_CONFIRMATION, AWAITING_INPUT, COMPLETED, FAILED, CANCELLED,
    INTERRUPTED, RESUMING,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _percentile(values: List[int], q: float) -> Optional[int]:
    """简单百分位（最近邻插值）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    idx = min(int(round(q * (len(s) - 1))), len(s) - 1)
    return int(s[idx])


@dataclass
class RunEvent:
    """统一事件契约。前端认识的字段全部保留，新增字段为可选。"""
    type: str                                    # status/intent/plan/tool_start/tool_result/verification/pending/final/error/cancelled
    run_id: str = ""
    seq: int = 0
    stage: str = ""                              # start/intent/plan/tool_*/response/done/confirmation/...
    title: str = ""
    message: str = ""
    details: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "type": self.type,
            "stage": self.stage or self.type,
            "title": self.title,
            "message": self.message,
            "details": self.details or [],
        }
        if self.run_id:
            d["run_id"] = self.run_id
        if self.seq:
            d["seq"] = self.seq
        if self.payload:
            d.update(self.payload)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunEvent":
        reserved = {"type", "stage", "title", "message", "details", "run_id", "seq"}
        payload = {k: v for k, v in d.items() if k not in reserved}
        return cls(
            type=d.get("type", "status"),
            run_id=d.get("run_id", ""),
            seq=d.get("seq", 0),
            stage=d.get("stage", ""),
            title=d.get("title", ""),
            message=d.get("message", ""),
            details=d.get("details", []),
            payload=payload,
        )


class RunStore:
    """SQLite 持久化：每次操作用独立短连接（与 main.get_db 相同模式），线程安全。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.ensure_tables()

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------

    def ensure_tables(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    status TEXT NOT NULL,
                    user_message TEXT,
                    intent_json TEXT,
                    pending_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    payload TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_run_events_run
                ON run_events(run_id, seq)
            """)
            # 可观测性：runs 表增量列（幂等迁移，老库自动补列）
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "duration_ms" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN duration_ms INTEGER")
            if "intent_name" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN intent_name TEXT")
            if "langfuse_trace_id" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN langfuse_trace_id TEXT")
            # 宕机断点续传（阶段8）：heartbeat 存活探针 + 断点定位
            if "heartbeat_at" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
            if "last_node" not in existing_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN last_node TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # 工具副作用幂等：PRIMARY KEY(run_id, step_id)，命中即跳过重跑复用结果
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    run_id TEXT NOT NULL,
                    step_id INTEGER NOT NULL,
                    params_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_id)
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # run 主记录
    # ------------------------------------------------------------------

    # 终态（可清理）
    TERMINAL_STATUSES = (COMPLETED, FAILED, CANCELLED)

    def cleanup(self, keep_days: int = 7) -> int:
        """清理超过 keep_days 的终态 run 及其事件/检查点；running/awaiting 不动。返回删除的 run 数。"""
        cutoff = (datetime.now(timezone.utc).astimezone() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT run_id FROM runs WHERE status IN (?,?,?) AND created_at < ?",
                (*self.TERMINAL_STATUSES, cutoff),
            )
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM run_events WHERE run_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM run_checkpoints WHERE run_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM idempotency_keys WHERE run_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM runs WHERE run_id IN ({marks})", ids)
            conn.commit()
        return len(ids)

    def create_run(self, run_id: str, session_id: str, user_message: str,
                   intent_json: Optional[str] = None) -> None:
        now = _now_iso()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id, session_id, status, user_message, intent_json, pending_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, session_id, RUNNING, user_message, intent_json, None, now, now),
            )
            conn.commit()

    def update_status(self, run_id: str, status: str) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid run status: {status}")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (status, _now_iso(), run_id),
            )
            conn.commit()

    def set_intent(self, run_id: str, intent_json: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET intent_json=?, updated_at=? WHERE run_id=?",
                (intent_json, _now_iso(), run_id),
            )
            conn.commit()

    def set_pending(self, run_id: str, pending_json: str, status: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET pending_json=?, status=?, updated_at=? WHERE run_id=?",
                (pending_json, status, _now_iso(), run_id),
            )
            conn.commit()

    def clear_pending(self, run_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET pending_json=NULL, updated_at=? WHERE run_id=?",
                (_now_iso(), run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_pending_by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """返回该会话最近一个 pending 状态的 run（用于规则化 resume 判定）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runs WHERE session_id=? "
                "AND status IN (?,?) ORDER BY updated_at DESC LIMIT 1",
                (session_id, AWAITING_CONFIRMATION, AWAITING_INPUT),
            ).fetchone()
        if not row:
            return None
        run = dict(row)
        try:
            run["pending"] = json.loads(run.get("pending_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            run["pending"] = {}
        return run

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def set_cancelled(self, run_id: str) -> None:
        """置取消标记：状态改为 cancelled；引擎在各步骤中断点读取状态响应取消。"""
        self.update_status(run_id, CANCELLED)

    def is_cancelled(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        return bool(run) and run["status"] == CANCELLED

    # ------------------------------------------------------------------
    # 宕机 watchdog（阶段8）：heartbeat / 老化判定 / 定位
    # ------------------------------------------------------------------

    def update_heartbeat(self, run_id: str, last_node: str = "") -> None:
        """运行心跳：工具步骤/节点落盘时更新 heartbeat_at + last_node（断点定位）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET heartbeat_at=?, last_node=?, updated_at=? WHERE run_id=?",
                (_now_iso(), last_node, _now_iso(), run_id),
            )
            conn.commit()

    def mark_interrupted(self, run_id: str) -> None:
        """RUNNING 且失活超时 → INTERRUPTED（可恢复）。"""
        self.update_status(run_id, INTERRUPTED)

    def mark_resuming(self, run_id: str) -> None:
        """启动恢复前置 RESUMING，避免并发重复 resume。"""
        self.update_status(run_id, RESUMING)

    def get_last_run_by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """返回该会话最近一次 run（按 updated_at 倒序）。无则 None。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runs WHERE session_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def is_stale_updated_at(updated_at: Optional[str], max_age_seconds: int) -> bool:
        """判断 updated_at 是否已超过 max_age_seconds 未更新。解析失败视为过期。"""
        if not updated_at:
            return True
        try:
            ts = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        now = datetime.now(timezone.utc).astimezone()
        return (now - ts).total_seconds() > max_age_seconds

    def find_stale_runs(self, max_age_seconds: int,
                        statuses=("running",)) -> List[Dict[str, Any]]:
        """返回指定状态中 updated_at 失活超过阈值的 run（未变更状态，供 watchdog 标记）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            marks = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({marks})",
                list(statuses),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if self.is_stale_updated_at(d.get("updated_at"), max_age_seconds):
                out.append(d)
        return out

    # ------------------------------------------------------------------
    # 工具副作用幂等（阶段8）：命中即跳过重跑复用结果
    # ------------------------------------------------------------------

    def record_idempotency_key(self, run_id: str, step_id: int, params_hash: str) -> None:
        """记录工具步骤幂等 key（INSERT OR IGNORE，重复插入安全）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys (run_id, step_id, params_hash, created_at) "
                "VALUES (?,?,?,?)",
                (run_id, step_id, params_hash, _now_iso()),
            )
            conn.commit()

    def check_idempotency_key(self, run_id: str, step_id: int) -> bool:
        """该 (run_id, step_id) 是否已执行过（存过 key）。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM idempotency_keys WHERE run_id=? AND step_id=?",
                (run_id, step_id),
            ).fetchone()
        return row is not None

    def delete_idempotency_key(self, run_id: str, step_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM idempotency_keys WHERE run_id=? AND step_id=?",
                (run_id, step_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------

    def append_event(self, run_id: str, event: Dict[str, Any], seq: int) -> None:
        """持久化事件。payload 存完整 JSON（含 title/message/details），供补拉还原。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_type, stage, payload, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    event.get("type", "status"),
                    event.get("stage", ""),
                    json.dumps(event, ensure_ascii=False),
                    _now_iso(),
                ),
            )
            conn.commit()

    def get_events(self, run_id: str, since_seq: int = 0) -> List[Dict[str, Any]]:
        """断线补拉：返回 seq > since_seq 的事件（升序）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT payload FROM run_events WHERE run_id=? AND seq>? ORDER BY seq ASC",
                (run_id, since_seq),
            ).fetchall()
        events = []
        for row in rows:
            try:
                events.append(json.loads(row["payload"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return events

    # ------------------------------------------------------------------
    # checkpoint（resume 现场）
    # ------------------------------------------------------------------

    def save_checkpoint(self, run_id: str, state: Dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_checkpoints (run_id, state_json, updated_at) VALUES (?,?,?)",
                (run_id, json.dumps(state, ensure_ascii=False, default=str), _now_iso()),
            )
            conn.commit()

    def load_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state_json FROM run_checkpoints WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def delete_checkpoint(self, run_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM run_checkpoints WHERE run_id=?", (run_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # 可观测性：指标写入 + 聚合查询（供 routers/obs.py）
    # ------------------------------------------------------------------

    def set_run_metrics(self, run_id: str, duration_ms: Optional[int],
                        intent_name: str = "", langfuse_trace_id: str = "") -> None:
        """run 终态时写入观测指标列（部分列缺省时保留旧值）。"""
        sets, params = [], []
        if duration_ms is not None:
            sets.append("duration_ms=?"); params.append(int(duration_ms))
        if intent_name:
            sets.append("intent_name=?"); params.append(intent_name)
        if langfuse_trace_id:
            sets.append("langfuse_trace_id=?"); params.append(langfuse_trace_id)
        if not sets:
            return
        params += [_now_iso(), run_id]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)}, updated_at=? WHERE run_id=?", params)
            conn.commit()

    def list_runs(self, limit: int = 50, status: Optional[str] = None,
                  days: Optional[int] = None) -> List[Dict[str, Any]]:
        """run 列表（倒序）。可选按状态过滤、按天数过滤。"""
        sql = ("SELECT run_id, session_id, status, user_message, intent_name, duration_ms, "
               "langfuse_trace_id, created_at, updated_at FROM runs")
        conds, params = [], []
        if status:
            conds.append("status=?"); params.append(status)
        if days:
            cutoff = (datetime.now(timezone.utc).astimezone()
                      - timedelta(days=days)).isoformat(timespec="seconds")
            conds.append("created_at>=?"); params.append(cutoff)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["user_message"] = (d.get("user_message") or "")[:120]
            d["event_count"] = self._count_events(d["run_id"])
            out.append(d)
        return out

    def _count_events(self, run_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def get_overview(self, days: int = 7) -> Dict[str, Any]:
        """KPI 聚合：状态分布 / 成功率 / 平均耗时 / 今日量 / 意图分布。"""
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=days)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT status, intent_name, duration_ms, created_at FROM runs "
                "WHERE created_at>=?", (cutoff,),
            ).fetchall()
        total = len(rows)
        by_status: Dict[str, int] = {}
        by_intent: Dict[str, int] = {}
        durations = []
        today_prefix = _now_iso()[:10]
        today = 0
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            intent = r["intent_name"] or "(未知)"
            by_intent[intent] = by_intent.get(intent, 0) + 1
            if r["duration_ms"] is not None:
                durations.append(int(r["duration_ms"]))
            if (r["created_at"] or "").startswith(today_prefix):
                today += 1
        completed = by_status.get(COMPLETED, 0)
        failed = by_status.get(FAILED, 0)
        finished = completed + failed
        top_intents = sorted(by_intent.items(), key=lambda kv: -kv[1])[:8]
        return {
            "days": days,
            "total": total,
            "today": today,
            "by_status": by_status,
            "success_rate": round(completed / finished, 4) if finished else None,
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            "p95_duration_ms": _percentile(durations, 0.95),
            "top_intents": [{"intent": k, "count": v} for k, v in top_intents],
        }

    def get_tool_stats(self, days: int = 7) -> List[Dict[str, Any]]:
        """工具性能聚合：从 run_events 的 tool_result payload 提取（调用数/成功率/平均/P95 耗时）。"""
        cutoff = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=days)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT payload FROM run_events WHERE event_type='tool_result' AND created_at>=?",
                (cutoff,),
            ).fetchall()
        agg: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                ev = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            tool = ev.get("tool_name") or "unknown"
            slot = agg.setdefault(tool, {"calls": 0, "ok": 0, "durations": []})
            slot["calls"] += 1
            if ev.get("success") is True:
                slot["ok"] += 1
            ms = ev.get("elapsed_ms")
            if isinstance(ms, (int, float)) and ms >= 0:
                slot["durations"].append(int(ms))
        out = []
        for tool, s in agg.items():
            ds = s["durations"]
            out.append({
                "tool": tool,
                "calls": s["calls"],
                "success_rate": round(s["ok"] / s["calls"], 4) if s["calls"] else None,
                "avg_ms": int(sum(ds) / len(ds)) if ds else None,
                "p95_ms": _percentile(ds, 0.95),
            })
        out.sort(key=lambda x: -x["calls"])
        return out


# 进程级单例（main.py 启动时可通过 RunStore(db_path=...) 重建覆盖）
_store_singleton: Optional[RunStore] = None


def get_run_store(db_path: str = DEFAULT_DB_PATH) -> RunStore:
    """获取全局 RunStore 单例（惰性初始化 + 惰性建表）。"""
    global _store_singleton
    if _store_singleton is None or _store_singleton.db_path != db_path:
        _store_singleton = RunStore(db_path)
    return _store_singleton
