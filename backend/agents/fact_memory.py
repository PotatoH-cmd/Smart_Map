"""
fact_memory.py — 用户事实记忆（阶段D）。

跨会话长期记忆：对话成功结束后由 LLM 异步抽取稳定事实（项目偏好、常用区域、
数据习惯、单位约定等），存入 sessions.db 的 user_facts 表；后续会话在意图
分析阶段注入（build_facts_context），实现"越用越懂用户"。

约束（对齐优化方案）：
- 无用户账号体系 → 事实全局共享
- 每轮最多抽取 3 条新事实；全局上限 200 条（超出淘汰 hits 最低）
- LLM 失败静默跳过，绝不阻断主流程
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 与 main.DB_PATH / run_store.DEFAULT_DB_PATH 一致，避免循环导入故独立解析
DB_PATH = os.environ.get(
    "MAPASSIST_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db"),
)

ENABLED = os.environ.get("FACT_MEMORY_ENABLED", "1") == "1"
MAX_FACTS = 200            # 全局事实上限
MAX_NEW_PER_TURN = 3       # 每轮最多新增条数
CONTEXT_CHARS = 500        # 注入上下文长度上限
EXTRACT_USER_CHARS = 600   # 抽取时 user 消息截断
EXTRACT_ASST_CHARS = 600   # 抽取时 assistant 消息截断

# 防止并发抽取风暴（不同会话同时收尾）
_extract_lock = threading.Semaphore(2)

_DDL = """
CREATE TABLE IF NOT EXISTS user_facts (
  id TEXT PRIMARY KEY, content TEXT NOT NULL, category TEXT,
  evidence TEXT, source_session TEXT, hits INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT, last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_facts_hits ON user_facts(hits);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


# ─────────────────────────────────────────────
# 抽取 + 落库
# ─────────────────────────────────────────────

def extract_and_store_async(session_id: str, user_text: str, assistant_text: str, llm=None) -> None:
    """fire-and-forget 入口：有事件循环则 create_task，否则起 daemon 线程。"""
    if not ENABLED or not llm:
        return
    if not session_id or session_id == "default":
        return
    if not user_text or not assistant_text:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_run_extract, session_id, user_text, assistant_text, llm))
    except RuntimeError:
        threading.Thread(
            target=_run_extract, args=(session_id, user_text, assistant_text, llm), daemon=True
        ).start()


def _run_extract(session_id: str, user_text: str, assistant_text: str, llm) -> None:
    try:
        with _extract_lock:
            facts = _extract_facts(user_text, assistant_text, llm)
        if facts:
            applied = apply_facts(facts, source_session=session_id)
            if applied:
                logger.info(f"[fact-memory] 会话 {session_id[:8]} 抽取并落库 {applied} 条事实")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fact-memory] 抽取失败（静默跳过）: {e}")


def _extract_facts(user_text: str, assistant_text: str, llm) -> List[Dict[str, Any]]:
    """调用 LLM 从本轮对话抽取事实，返回 [{action, id?, content, category?, evidence?}]。"""
    existing = _format_existing_for_prompt()
    prompt = f"""请从下面这轮对话中提取值得跨会话记住的用户稳定事实。

## 提取规则
- 只提取用户明确表达、或可从回答确认的稳定信息：项目偏好、常用区域/河流、数据习惯、单位约定、重复性需求等
- 不要提取一次性任务细节（本轮具体操作步骤、临时坐标、执行过程）
- 与已有事实语义重复时输出 update（带其 id，content 为合并后的表述）；否则 new
- 每轮最多 {MAX_NEW_PER_TURN} 条新事实；没有可提取内容输出 []
- 只输出 JSON 数组，不要其他文字：
[{{"action": "new|update", "id": "仅update需要", "content": "事实表述(≤80字)", "category": "偏好|区域|数据|约定|其他", "evidence": "依据(≤40字)"}}]

## 本轮对话
[user] {user_text[:EXTRACT_USER_CHARS]}
[assistant] {assistant_text[:EXTRACT_ASST_CHARS]}

## 已有事实清单（id: 内容）
{existing if existing else "（空）"}

请输出 JSON 数组："""

    from langchain_core.messages import HumanMessage
    resp = llm.invoke([HumanMessage(content=prompt)])
    text = str(getattr(resp, "content", resp) or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)  # 剥离 markdown 围栏
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        return []
    arr = json.loads(m.group(0))
    if not isinstance(arr, list):
        return []
    cleaned = []
    for item in arr[: MAX_NEW_PER_TURN + 3]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        cleaned.append({
            "action": item.get("action", "new"),
            "id": str(item.get("id", "") or ""),
            "content": content[:120],
            "category": str(item.get("category", "") or "")[:20],
            "evidence": str(item.get("evidence", "") or "")[:60],
        })
    return cleaned[: MAX_NEW_PER_TURN]


def apply_facts(facts: List[Dict[str, Any]], source_session: str = "") -> int:
    """将抽取结果落库（new 插入 / update 合并），并做容量淘汰。返回实际落库条数。"""
    if not facts:
        return 0
    now = _now_iso()
    applied = 0
    with contextlib_closing(_connect()) as conn:
        _ensure_table(conn)
        # 已有事实索引（id -> row）
        rows = {r["id"]: r for r in conn.execute("SELECT id, content FROM user_facts").fetchall()}
        for f in facts:
            content = f.get("content", "").strip()
            if not content:
                continue
            fid = f.get("id", "")
            if f.get("action") == "update" and fid in rows:
                conn.execute(
                    "UPDATE user_facts SET content=?, category=?, evidence=?, source_session=?, updated_at=? WHERE id=?",
                    (content, f.get("category") or None, f.get("evidence") or None, source_session or None, now, fid),
                )
            else:
                # 语义级去重：内容完全相同则跳过（LLM 层已做语义去重，这里是兜底）
                if any(content == r["content"] for r in rows.values()):
                    continue
                fid = uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO user_facts (id, content, category, evidence, source_session, hits, created_at, updated_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,0,?,?,?)",
                    (fid, content, f.get("category") or None, f.get("evidence") or None, source_session or None, now, now, now),
                )
                rows[fid] = {"id": fid, "content": content}
            applied += 1
        # 容量淘汰：超出 MAX_FACTS 删 hits 最低（同 hits 按 updated_at 最旧）
        count = conn.execute("SELECT COUNT(*) FROM user_facts").fetchone()[0]
        overflow = count - MAX_FACTS
        if overflow > 0:
            victims = conn.execute(
                "SELECT id FROM user_facts ORDER BY hits ASC, updated_at ASC LIMIT ?", (overflow,)
            ).fetchall()
            for v in victims:
                conn.execute("DELETE FROM user_facts WHERE id=?", (v["id"],))
        conn.commit()
    return applied


def contextlib_closing(conn):
    """轻量包装，避免顶部额外 import 影响可读性。"""
    import contextlib
    return contextlib.closing(conn)


# ─────────────────────────────────────────────
# 注入 / 查询 / 管理
# ─────────────────────────────────────────────

def build_facts_context(max_chars: int = CONTEXT_CHARS) -> str:
    """构建「用户长期记忆」注入段（按 hits 优先，注入即视为命中更新 hits/last_seen_at）。"""
    if not ENABLED:
        return ""
    try:
        with contextlib_closing(_connect()) as conn:
            _ensure_table(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content, category FROM user_facts ORDER BY hits DESC, updated_at DESC LIMIT 20"
            ).fetchall()
        if not rows:
            return ""
        lines, used, touched = [], 0, []
        for r in rows:
            cat = f"（{r['category']}）" if r["category"] else ""
            line = f"- {r['content']}{cat}"
            if used + len(line) > max_chars or len(lines) >= 10:
                break
            lines.append(line)
            used += len(line)
            touched.append(r["id"])
        if touched and lines:
            _touch_facts(touched)
            return "## 用户长期记忆\n" + "\n".join(lines)
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fact-memory] 上下文构建失败: {e}")
        return ""


def _touch_facts(ids: List[str]) -> None:
    try:
        with contextlib_closing(_connect()) as conn:
            marks = ",".join("?" * len(ids))
            now = _now_iso()
            conn.execute(
                f"UPDATE user_facts SET hits=hits+1, last_seen_at=? WHERE id IN ({marks})",
                (now, *ids),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[fact-memory] touch 失败: {e}")


def list_facts(q: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    with contextlib_closing(_connect()) as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        if q:
            rows = conn.execute(
                "SELECT * FROM user_facts WHERE content LIKE ? OR category LIKE ? ORDER BY hits DESC, updated_at DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_facts ORDER BY hits DESC, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def delete_fact(fact_id: str) -> bool:
    with contextlib_closing(_connect()) as conn:
        _ensure_table(conn)
        cur = conn.execute("DELETE FROM user_facts WHERE id=?", (fact_id,))
        conn.commit()
        return cur.rowcount > 0


def _format_existing_for_prompt(limit: int = 30) -> str:
    try:
        with contextlib_closing(_connect()) as conn:
            _ensure_table(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content FROM user_facts ORDER BY hits DESC, updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return "\n".join(f"{r['id']}: {r['content']}" for r in rows)
    except Exception:  # noqa: BLE001
        return ""
