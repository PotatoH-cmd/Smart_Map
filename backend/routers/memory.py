"""用户事实记忆管理（自 main.py 机械搬移，行为不变）。"""
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
import logging


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/memory/facts")
async def list_user_facts(q: str = ""):
    """查看用户事实记忆（阶段D，支持 q 关键字过滤）。"""
    from agents.fact_memory import list_facts
    try:
        facts = list_facts(q=q or None)
        return {"success": True, "total": len(facts), "facts": facts}
    except Exception as e:
        logger.error(f"List facts error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@router.delete("/api/memory/facts/{fact_id}")
async def delete_user_fact(fact_id: str):
    """删除指定用户事实记忆。"""
    from agents.fact_memory import delete_fact
    try:
        if not delete_fact(fact_id):
            raise HTTPException(status_code=404, detail="事实不存在")
        return {"success": True, "id": fact_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete fact error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
