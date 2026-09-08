"""SQLite 会话/记忆持久化（core 层，与 config.py 同级）。

从 main.py 收敛而来，作为会话存储的唯一归属：
- DB_PATH 单一来源：core.config.db.path（环境变量 MAPASSIST_DB_PATH 可覆盖）
- init_db()：建 sessions/messages/user_facts 三张表 + WAL
- get_db() / now_iso()：供 routers（chat.py）与 lifespan 使用
"""
import contextlib
import os
import shutil
import sqlite3
from datetime import datetime as dt

from core.config import db as _cfg_db

DB_PATH = _cfg_db.path

# 一次性迁移：旧机器硬编码路径存在而新路径不存在时复制过来
_LEGACY_DB_PATH = "/home/server/python/map_assistant_v1/backend/sessions.db"
if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB_PATH):
    try:
        shutil.copy2(_LEGACY_DB_PATH, DB_PATH)
        print(f"[init_db] 已从旧路径迁移 sessions.db: {_LEGACY_DB_PATH} -> {DB_PATH}")
    except Exception:  # noqa: BLE001
        print(f"[init_db] 旧 sessions.db 迁移失败（继续用新路径）: {_LEGACY_DB_PATH}")


def init_db():
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
        )
        # 阶段D：用户事实记忆（跨会话长期记忆，全局共享）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT,
                evidence TEXT,
                source_session TEXT,
                hits INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                last_seen_at TEXT
            )
        """)
        conn.commit()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
