"""会话 CRUD：列表/新建/删除/重命名/历史消息（自 routers/chat.py 拆分，行为不变）。

数据落在 backend/core/db.py 管理的 SQLite（sessions/messages 两张表）。
"""
import contextlib
import logging
import uuid
from typing import Optional

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from core.db import get_db, now_iso

logger = logging.getLogger(__name__)

router = APIRouter()


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


@router.get("/api/sessions")
async def list_sessions():
    try:
        with contextlib.closing(get_db()) as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.error(f"List sessions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sessions")
async def create_session(body: SessionCreate):
    try:
        sid = str(uuid.uuid4())
        now = now_iso()
        title = (body.title or "新对话")[:50]
        with contextlib.closing(get_db()) as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (sid, title, now, now)
            )
            conn.commit()
        return {"id": sid, "title": title, "created_at": now, "updated_at": now}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Create session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    try:
        with contextlib.closing(get_db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            conn.commit()
        return {"success": True}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Delete session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, body: SessionRename):
    try:
        title = body.title[:50]
        with contextlib.closing(get_db()) as conn:
            conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, now_iso(), session_id)
            )
            conn.commit()
        return {"success": True, "title": title}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Rename session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    try:
        with contextlib.closing(get_db()) as conn:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id ASC",
                (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.error(f"Get session messages error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
