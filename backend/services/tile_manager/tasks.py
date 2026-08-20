"""后台构建任务管理。

- _TILE_BUILD_JOBS / _DRONE_BUILD_JOBS：内存任务状态（重启即丢，可接受）
- _submit_tile_build_job：提交任务到守护线程执行，返回 job_id
"""
import logging
import threading
import uuid
from datetime import datetime as dt
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_TILE_BUILD_JOBS: Dict[str, Dict[str, Any]] = {}
_DRONE_BUILD_JOBS: Dict[str, Dict[str, Any]] = {}


def _submit_tile_build_job(job: Dict[str, Any], fn: Callable[[Dict[str, Any]], None]) -> str:
    """提交后台构建任务，返回 job_id。任务状态存内存，重启后丢失（可接受）。"""
    job_id = uuid.uuid4().hex
    job.setdefault("job_id", job_id)
    job.setdefault("stage", "queued")
    job.setdefault("percent", 0)
    job.setdefault("message", "任务已提交，等待执行...")
    job.setdefault("done", False)
    job.setdefault("success", None)
    job.setdefault("created_at", dt.now().isoformat(timespec="seconds"))
    job.setdefault("updated_at", dt.now().isoformat(timespec="seconds"))
    _TILE_BUILD_JOBS[job_id] = job

    def _worker() -> None:
        try:
            fn(job)
        except Exception as e:
            logger.exception("tile build job failed: %s", job_id)
            job.update({
                "stage": "error",
                "percent": 0,
                "message": f"任务异常: {str(e)[:200]}",
                "done": True,
                "success": False,
            })
        finally:
            job["done"] = True
            job["updated_at"] = dt.now().isoformat(timespec="seconds")

    threading.Thread(target=_worker, daemon=True).start()
    return job_id
