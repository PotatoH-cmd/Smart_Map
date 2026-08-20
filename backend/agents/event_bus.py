"""
event_bus.py — 进程内事件总线（阶段1：异步解耦）。

run 在独立 asyncio task 中执行，SSE 连接只是事件订阅者：
  - publish：分配 seq → 写内存环形缓冲（每 run 200 条）→ 通知订阅者 → 持久化 run_events
  - subscribe：获得 asyncio.Queue，run 结束时收到 None 哨兵
  - get_history：断线补拉（内存缓冲优先，不足回退 DB）

浏览器断线不影响 run 执行；重连走 GET /api/run/{id}/events?since=seq 补拉。
"""
import asyncio
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

from .run_store import RunEvent, get_run_store

logger = logging.getLogger(__name__)

RING_BUFFER_SIZE = 200
_SUBSCRIBE_TIMEOUT = 3600.0  # 订阅队列最长存活 1 小时（防御泄漏）


class EventBus:
    """每 run 一组订阅者 + 环形缓冲 + seq 分配器。"""

    def __init__(self, store=None):
        self._store = store or get_run_store()
        # run_id -> seq 计数器（resume 后继续递增）
        self._seq: Dict[str, int] = {}
        # run_id -> deque[dict]（内存环形缓冲，dict 含 seq）
        self._buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=RING_BUFFER_SIZE))
        # run_id -> set[asyncio.Queue]
        self._subscribers: Dict[str, set] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 订阅 / 发布
    # ------------------------------------------------------------------

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        """订阅 run 事件流。返回队列；run 结束时推送 None 哨兵。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        # 补发历史缓冲（订阅前已产生的事件）
        async with self._lock:
            history = list(self._buffer.get(run_id, ()))
        for event in history:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"[EventBus] queue full on subscribe for run {run_id}")
                break
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.discard(queue)
            if not subs:
                self._subscribers.pop(run_id, None)

    async def publish(self, run_id: str, event: RunEvent) -> int:
        """发布事件：分配 seq → 内存缓冲 → 通知订阅者 → 持久化。返回 seq。"""
        async with self._lock:
            seq = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = seq
            event.seq = seq
            event.run_id = run_id
            d = event.to_dict()
            self._buffer[run_id].append(d)
            subs = list(self._subscribers.get(run_id, ()))
        for q in subs:
            try:
                q.put_nowait(d)
            except asyncio.QueueFull:
                logger.warning(f"[EventBus] subscriber queue full for run {run_id}")
        # 持久化失败不阻塞执行（记录日志即可）
        try:
            self._store.append_event(run_id, d, seq)
        except Exception as e:
            logger.warning(f"[EventBus] persist event failed (non-fatal): {e}")
        return seq

    def mark_finished(self, run_id: str) -> None:
        """run 终态：向所有订阅者推送 None 哨兵并清理订阅集合。"""
        subs = self._subscribers.pop(run_id, set())
        for q in subs:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # 断线补拉
    # ------------------------------------------------------------------

    async def get_history(self, run_id: str, since_seq: int = 0) -> List[Dict[str, Any]]:
        """返回 seq > since_seq 的事件（内存优先，回退 DB）。"""
        events: List[Dict[str, Any]] = []
        async with self._lock:
            buffer = self._buffer.get(run_id)
            if buffer is not None:
                events = [e for e in buffer if e.get("seq", 0) > since_seq]
                buffer_min = buffer[0].get("seq", 0) if buffer else 0
                # 缓冲未覆盖全部（或为空）时回退 DB 补早期事件
                if buffer_min > since_seq + 1:
                    db_events = self._store.get_events(run_id, since_seq)
                    db_max = db_events[-1]["seq"] if db_events else 0
                    # DB 与缓冲可能重叠，取 DB 中缓冲未覆盖的部分
                    events = db_events + [
                        e for e in events if e.get("seq", 0) > db_max
                    ]
                return events
        # 无内存缓冲（进程重启 / 其他 worker）：纯 DB
        return self._store.get_events(run_id, since_seq)

    def reset_run(self, run_id: str) -> None:
        """清理 run 相关内存状态（进程内重跑同 run_id 时调用）。"""
        self._seq.pop(run_id, None)
        self._buffer.pop(run_id, None)
        self._subscribers.pop(run_id, None)

    async def current_seq(self, run_id: str) -> int:
        """当前已发布的最大 seq（订阅回放边界：seq <= 该值的事件均可能被回放）。"""
        async with self._lock:
            return self._seq.get(run_id, 0)
