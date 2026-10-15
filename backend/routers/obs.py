"""可观测性查询 API：KPI 概览 · run 列表 · 工具性能 · LangFuse 状态。

数据源：
  - 本地 SQLite（runs/run_events，run_store 聚合）— 不依赖 LangFuse，始终可用
  - LangFuse 状态/深链 — 未配置时 enabled=false，前端隐藏入口
"""
import logging
import os

from fastapi import APIRouter, Query

from agents.run_store import get_run_store
from core.obs import is_enabled

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/obs/overview")
async def obs_overview(days: int = Query(default=7, ge=1, le=90)):
    """KPI 概览：状态分布 / 成功率 / 平均与 P95 耗时 / 今日量 / 意图分布。"""
    store = get_run_store()
    return {"success": True, "data": store.get_overview(days)}


@router.get("/api/obs/runs")
async def obs_runs(
    limit: int = Query(default=50, ge=1, le=500),
    status: str = Query(default=None),
    days: int = Query(default=None, ge=1, le=90),
):
    """run 列表（倒序，含意图/耗时/事件数/LangFuse 深链）。"""
    store = get_run_store()
    runs = store.list_runs(limit=limit, status=status, days=days)
    # 补 LangFuse 深链（活跃 run 在 obs 模块内存中可查；历史 run 落库时已写 langfuse_trace_id）
    host = os.environ.get("LANGFUSE_HOST", "")
    for r in runs:
        trace_id = r.get("langfuse_trace_id") or ""
        if trace_id and host:
            r["langfuse_url"] = f"{host}/project/{_project_slug()}/traces/{trace_id}"
        else:
            r["langfuse_url"] = ""
    return {"success": True, "data": runs}


@router.get("/api/obs/tools")
async def obs_tools(days: int = Query(default=7, ge=1, le=90)):
    """工具性能榜：调用数 / 成功率 / 平均与 P95 耗时。"""
    store = get_run_store()
    return {"success": True, "data": store.get_tool_stats(days)}


@router.get("/api/obs/langfuse/status")
async def obs_langfuse_status():
    """LangFuse 集成状态（供前端指示灯与深链前缀）。"""
    enabled = is_enabled()
    return {
        "success": True,
        "data": {
            "enabled": enabled,
            "host": os.environ.get("LANGFUSE_HOST", "") if enabled else "",
            "hint": "backend/.env 配置 LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST 后自动启用" if not enabled else "",
        },
    }


def _project_slug() -> str:
    """LangFuse trace 深链用的 project id（惰性查询并缓存）。"""
    global _PROJECT_SLUG
    if _PROJECT_SLUG:
        return _PROJECT_SLUG
    import urllib.request
    import base64
    try:
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
        req = urllib.request.Request(f"{host}/api/public/projects")
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(f"{pk}:{sk}".encode()).decode())
        import json as _json
        with urllib.request.urlopen(req, timeout=5) as resp:
            projects = _json.loads(resp.read()).get("data") or []
        if projects:
            _PROJECT_SLUG = projects[0]["id"]
    except Exception as e:
        logger.warning(f"[obs] langfuse project slug fetch failed: {e}")
        _PROJECT_SLUG = ""
    return _PROJECT_SLUG


_PROJECT_SLUG = ""
