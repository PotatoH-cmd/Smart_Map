"""
context_manager.py — 上下文预算与压缩（阶段4）。

集中预算常量并实施分层上下文：
  system prompt（静态，由 prompts.py 提供）
  + workspace 摘要（≤800 字，workspace_state.build_summary）
  + 最近 6 轮历史（100 字/条，逻辑自 intent_agent._build_analysis_prompt 迁入）
  + 工具结果（总预算 4000 字，优先级裁剪：错误信息 > DB 数据 > 知识库 > 其余）

历史压缩：超过 12 轮时旧轮次由 LLM 异步生成滚动摘要存入 workspace.last_summary
（失败静默降级为截断），对齐文章 context budget / step context pack 方向。
"""
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 预算常量（集中管理，禁止散落魔法数字；支持环境变量覆盖）──
BUDGET_WORKSPACE_CHARS = 800       # workspace 摘要上限
BUDGET_HISTORY_TURNS = int(os.environ.get("CONTEXT_HISTORY_TURNS", "8"))       # 最近历史轮数（每轮 = 1 user + 1 assistant）
BUDGET_HISTORY_PER_MSG = int(os.environ.get("CONTEXT_HISTORY_PER_MSG", "160"))  # 每条历史消息上限（字）
BUDGET_TOOL_RESULTS_CHARS = 4000   # 工具结果总预算（字）
COMPRESS_THRESHOLD_TURNS = 12      # 超过该轮数触发滚动摘要压缩
LAST_SUMMARY_CHARS = 300           # 滚动摘要长度
COMPRESS_DEBOUNCE_SECONDS = 60     # 同会话两次压缩的最小间隔（去抖）

# 工具结果裁剪优先级（分值越高越优先保留）
_TOOL_PRIORITY_RULES = (
    (3, ("error", "错误", "失败", "异常", "不存在", "不可用")),          # 错误信息
    (2, ("data", "查询", "结果", "行", "条记录", "SQL")),                # DB 数据
    (1, ("知识库", "检索", "政策", "文档")),                              # 知识库
)


def _priority_of(summary: str) -> int:
    for score, keywords in _TOOL_PRIORITY_RULES:
        if any(k in summary for k in keywords):
            return score
    return 0


def build_history_context(
    chat_history: Optional[List[Dict]],
    max_turns: int = BUDGET_HISTORY_TURNS,
    per_msg: int = BUDGET_HISTORY_PER_MSG,
) -> str:
    """构建最近 N 轮对话历史上下文（确定性截断，逻辑自 intent_agent 迁入）。"""
    if not chat_history:
        return ""
    recent_msgs = chat_history[-max_turns * 2:]
    return "\n\n## 最近对话历史\n" + "\n".join(
        f"- {msg.get('role', 'unknown')}: {str(msg.get('content', ''))[:per_msg]}"
        for msg in recent_msgs
        if msg.get("role") in ["user", "assistant"]
    )


def trim_tool_summaries(
    tool_summaries: List[str],
    max_chars: int = BUDGET_TOOL_RESULTS_CHARS,
) -> List[str]:
    """确定性裁剪工具摘要：优先级（错误 > DB 数据 > 知识库 > 其余），同优先级保序。"""
    if not tool_summaries:
        return []
    total = sum(len(s) for s in tool_summaries)
    if total <= max_chars:
        return list(tool_summaries)
    # (优先级 desc, 原序 asc) 稳定排序
    ranked = sorted(
        enumerate(tool_summaries),
        key=lambda pair: (-_priority_of(pair[1]), pair[0]),
    )
    kept: List[tuple] = []  # (原序索引, 保留文本)
    used = 0
    for idx, summary in ranked:
        remaining = max_chars - used
        if remaining <= 0:
            break
        text = summary
        if len(text) > remaining:
            text = text[: max(remaining - 1, 0)] + "…"
            if len(text) <= 1:
                continue
        kept.append((idx, text))
        used += len(text)
    kept.sort(key=lambda pair: pair[0])
    return [text for _, text in kept]


def load_history_from_db(db_path: str, session_id: str, max_turns: int = COMPRESS_THRESHOLD_TURNS) -> List[Dict[str, str]]:
    """契约改造：从 sessions.db 读取最近 max_turns 轮历史（前端不再全量回传）。

    返回 [{"role": ..., "content": ...}, ...]（升序）。读库失败静默返回空。
    """
    import sqlite3
    if not session_id or session_id == "default":
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, max_turns * 2),
            ).fetchall()
    except Exception as e:
        logger.warning(f"[ContextManager] load history from db failed: {e}")
        return []
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return history


class ContextManager:
    """上下文预算与压缩。workspace 为 WorkspaceState 实例（阶段3）。"""

    def __init__(self, workspace=None):
        self.workspace = workspace
        self._compress_inflight: set = set()     # 正在压缩的 session（去抖）
        self._last_compress_at: Dict[str, float] = {}  # session -> 上次压缩时刻

    # ------------------------------------------------------------------
    # 实例级入口（供 TaskExecutor / intent_agent 调用）
    # ------------------------------------------------------------------

    @staticmethod
    def build_history_context(
        chat_history: Optional[List[Dict]],
        max_turns: int = BUDGET_HISTORY_TURNS,
        per_msg: int = BUDGET_HISTORY_PER_MSG,
    ) -> str:
        """最近 N 轮历史上下文（逻辑自 intent_agent._build_analysis_prompt 迁入）。"""
        return build_history_context(chat_history, max_turns, per_msg)

    @staticmethod
    def trim_tool_summaries(
        tool_summaries: List[str],
        max_chars: int = BUDGET_TOOL_RESULTS_CHARS,
    ) -> List[str]:
        """工具结果预算裁剪。"""
        return trim_tool_summaries(tool_summaries, max_chars)

    # ------------------------------------------------------------------
    # 分层上下文组装（供 intent / summarize 使用）
    # ------------------------------------------------------------------

    def build_layered_context(
        self,
        session_id: str,
        user_message: str,
        chat_history: Optional[List[Dict]] = None,
        tool_summaries: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """返回分层上下文：workspace 摘要 / 用户长期记忆 / 历史上下文 / 裁剪后的工具结果。"""
        ws_summary = ""
        if self.workspace is not None:
            try:
                ws_summary = self.workspace.build_summary(session_id)
            except Exception as e:
                logger.warning(f"[ContextManager] workspace summary failed: {e}")
        history_ctx = build_history_context(chat_history)
        trimmed = trim_tool_summaries(tool_summaries or [])
        # 阶段D：用户事实记忆注入（失败静默为空，不阻断）
        try:
            from .fact_memory import build_facts_context
            facts_ctx = build_facts_context()
        except Exception as e:
            logger.warning(f"[ContextManager] facts context failed: {e}")
            facts_ctx = ""
        return {
            "workspace_summary": ws_summary,
            "facts_context": facts_ctx,
            "history_context": history_ctx,
            "tool_summaries": trimmed,
        }

    # ------------------------------------------------------------------
    # 历史压缩（异步滚动摘要）
    # ------------------------------------------------------------------

    async def compress_history(
        self,
        session_id: str,
        messages: List[Dict],
        llm=None,
    ) -> bool:
        """超过 12 轮时，旧轮次由 LLM 生成滚动摘要存入 workspace.last_summary。

        失败静默降级为截断（不抛异常）。llm 为 LangChain ChatOpenAI 兼容对象，
        为 None 时退化为简单截断拼接。
        去抖：同会话压缩进行中或 COMPRESS_DEBOUNCE_SECONDS 内已压缩过则跳过。
        """
        if not session_id or session_id == "default" or not messages:
            return False
        now = time.monotonic()
        if (session_id in self._compress_inflight
                or now - self._last_compress_at.get(session_id, 0.0) < COMPRESS_DEBOUNCE_SECONDS):
            return False
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if len(user_msgs) <= COMPRESS_THRESHOLD_TURNS:
            return False

        self._compress_inflight.add(session_id)
        try:
            return await self._do_compress_history(session_id, messages, llm)
        finally:
            self._compress_inflight.discard(session_id)
            self._last_compress_at[session_id] = time.monotonic()

    async def _do_compress_history(
        self,
        session_id: str,
        messages: List[Dict],
        llm=None,
    ) -> bool:
        if not session_id or session_id == "default" or not messages:
            return False
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if len(user_msgs) <= COMPRESS_THRESHOLD_TURNS:
            return False

        # 旧轮次 = 除最近 COMPRESS_THRESHOLD_TURNS 轮之外的部分
        old_turns = messages[: -COMPRESS_THRESHOLD_TURNS * 2]
        if not old_turns:
            return False
        old_text = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:200]}"
            for m in old_turns
            if m.get("role") in ("user", "assistant")
        )
        if not old_text.strip():
            return False

        summary = ""
        if llm is not None:
            try:
                from langchain_core.messages import HumanMessage, SystemMessage
                ai = llm.invoke([
                    SystemMessage(content=(
                        "你是对话历史压缩器。请用不超过 300 字的中文概括以下历史对话的"
                        "关键信息（涉及的地名、数据结论、用户目标），保留后续对话需要的事实。"
                    )),
                    HumanMessage(content=old_text),
                ])
                summary = (ai.content if hasattr(ai, "content") else str(ai)).strip()
            except Exception as e:
                logger.warning(f"[ContextManager] LLM compress failed (fallback truncate): {e}")
                summary = ""
        if not summary:
            # 静默降级：截断拼接
            summary = old_text[: LAST_SUMMARY_CHARS - 1] + "…"

        if self.workspace is not None:
            try:
                self.workspace.set_last_summary(session_id, summary)
                logger.info(
                    f"[ContextManager] history compressed for session={session_id} "
                    f"({len(old_turns)} msgs → {len(summary)} chars)"
                )
                return True
            except Exception as e:
                logger.warning(f"[ContextManager] save rolling summary failed: {e}")
        return False
