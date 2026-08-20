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
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 与 main.DB_PATH 保持一致；可在进程启动时通过 RunStore(db_path=...) 覆盖
DEFAULT_DB_PATH = "/home/server/python/map_assistant_v1/backend/sessions.db"

# 状态机（对齐文章 Run 生命周期）
RUNNING = "running"
AWAITING_CONFIRMATION = "awaiting_confirmation"
AWAITING_INPUT = "awaiting_input"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

ALL_STATUSES = (
    RUNNING, AWAITING_CONFIRMATION, AWAITING_INPUT, COMPLETED, FAILED, CANCELLED,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # run 主记录
    # ------------------------------------------------------------------

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


# 进程级单例（main.py 启动时可通过 RunStore(db_path=...) 重建覆盖）
_store_singleton: Optional[RunStore] = None


def get_run_store(db_path: str = DEFAULT_DB_PATH) -> RunStore:
    """获取全局 RunStore 单例（惰性初始化 + 惰性建表）。"""
    global _store_singleton
    if _store_singleton is None or _store_singleton.db_path != db_path:
        _store_singleton = RunStore(db_path)
    return _store_singleton
