"""Run 生命周期查询：状态 · 事件补拉 · 取消（自 routers/chat.py 拆分，行为不变）。

SSE 断线重连 / 前端轮询由这些端点支撑；取消为协作式标记，
引擎在下一个步骤中断点响应并停止。
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Query

from agents.run_store import get_run_store
import main as _main  # 仅运行期访问 lifespan 装配的单例（task_executor.run_engine.bus）

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/run/{run_id}")
async def get_run_status(run_id: str):
    """查询 run 状态与 pending 载荷（断线重连 / 前端轮询）。"""
    store = get_run_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")
    out = {k: run[k] for k in ("run_id", "session_id", "status", "user_message", "created_at", "updated_at")}
    pending_json = run.get("pending_json")
    out["pending"] = json.loads(pending_json) if pending_json else None
    return out


@router.get("/api/run/{run_id}/events")
async def get_run_events(run_id: str, since: int = Query(default=0, ge=0)):
    """断线补拉：返回 seq > since 的事件（升序）。内存缓冲优先，回退 DB。"""
    store = get_run_store()
    if not store.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")
    bus = None
    if _main.task_executor is not None and _main.task_executor.run_engine is not None:
        bus = _main.task_executor.run_engine.bus
    if bus is not None:
        events = await bus.get_history(run_id, since)
    else:
        events = store.get_events(run_id, since)
    latest_seq = max([e.get("seq", 0) for e in events] or [since])
    return {"run_id": run_id, "events": events, "latest_seq": latest_seq}


@router.post("/api/run/{run_id}/cancel")
async def cancel_run(run_id: str):
    """取消 run：置取消标记，引擎在下一个步骤中断点响应并停止（结果丢弃不写 workspace）。"""
    store = get_run_store()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"run {run_id} 不存在")
    if run["status"] in ("completed", "failed", "cancelled"):
        return {"run_id": run_id, "status": run["status"], "cancelled": False,
                "message": "run 已处于终态，无需取消"}
    store.set_cancelled(run_id)
    logger.info(f"[run] cancel requested: {run_id}")
    return {"run_id": run_id, "status": "cancelled", "cancelled": True,
            "message": "取消标记已设置，run 将在当前步骤完成后停止"}


@router.get("/suggestions")
async def get_suggestions():
    return {
        "suggestions": [
            "清除所有地图标记",
            "切换到卫星图层",
            "加载潢河郝楼可采区的矢量数据",
            "统计数据库中的点数量",
            "显示所有采样点并标注高程"
        ]
    }
