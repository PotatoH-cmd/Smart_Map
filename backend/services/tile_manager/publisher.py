"""切片发布桥接服务 — GIS 工具输出 → 切片管理发布。

将服务器文件系统上已有的影像/矢量数据一键发布为地图切片图层：
- publish_raster_async：GeoTIFF → MBTiles（复用 builders._run_drone_build_with_progress，带进度）
- publish_vector_async：GeoJSON → tippecanoe 矢量切片（PBF）

两者均提交到 _TILE_BUILD_JOBS 后台任务体系，返回 job_id，
前端通过 /api/tile_manager/build_status/{job_id} 轮询进度。
供 gis_tool_router（GIS 工具）与 tile_publish_tool（agent 工具）复用。
"""
import json
import logging
import os
import shutil
import types
from datetime import datetime as dt
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .builders import _run_drone_build_with_progress, _run_tippecanoe
from .registry import (
    _CUSTOM_TILE_DATA_DIR,
    _TILE_LAYER_META,
    _invalidate_tile_stats,
    _load_tile_registry,
    _parse_style,
    _sanitize_layer_key,
    _save_tile_registry,
)
from .tasks import _submit_tile_build_job

logger = logging.getLogger(__name__)


def _update_job(job: Dict[str, Any], **kw) -> None:
    job.update(kw)
    job["updated_at"] = dt.now().isoformat(timespec="seconds")


def _run_raster_publish_job(job: Dict[str, Any], req: Any) -> None:
    """后台执行栅格发布：GeoTIFF → MBTiles（复用带进度的构建生成器）。"""
    try:
        for chunk in _run_drone_build_with_progress(req):
            if not chunk.startswith("data: "):
                continue
            try:
                payload = json.loads(chunk[6:].strip())
            except Exception:
                continue
            job.update(payload)
            job["updated_at"] = dt.now().isoformat(timespec="seconds")
            if payload.get("stage") in ("done", "error"):
                job["done"] = True
                job["success"] = payload.get("stage") == "done"
                if payload.get("layer"):
                    job["layer"] = payload["layer"]
    except HTTPException as e:
        _update_job(job, stage="error", percent=0, message=str(e.detail), success=False, done=True)
    except Exception as e:
        logger.exception("raster publish job failed")
        _update_job(job, stage="error", percent=0, message=str(e)[:200], success=False, done=True)


def _run_vector_publish_job(
    job: Dict[str, Any],
    key: str,
    source_path: str,
    meta: Dict[str, Any],
    min_zoom: int,
    max_zoom: int,
) -> None:
    """后台执行矢量发布：GeoJSON → tippecanoe 矢量切片 + 注册表写入。"""
    try:
        _update_job(job, stage="tippecanoe", percent=5, message="开始生成矢量切片（tippecanoe）...")
        stats = _run_tippecanoe(key, source_path, min_zoom, max_zoom)
        _invalidate_tile_stats()
        _update_job(
            job, stage="done", percent=100, message="矢量切片发布完成",
            success=True, done=True, layer=key, stats=stats,
        )
    except HTTPException as e:
        _update_job(job, stage="error", percent=0, message=str(e.detail), success=False, done=True)
    except Exception as e:
        logger.exception("vector publish job failed: %s", key)
        _update_job(job, stage="error", percent=0, message=str(e)[:200], success=False, done=True)


def publish_raster_async(
    source_path: str,
    layer_key: str = "",
    name: str = "",
    area_key: str = "",
    year: Optional[int] = None,
    min_zoom: int = 0,
    max_zoom: int = 22,
    opacity: float = 0.9,
    tile_format: str = "PNG",
    quality: int = 85,
    overwrite: bool = True,
    source_srs: str = "",
) -> str:
    """提交栅格切片发布任务（GeoTIFF → MBTiles），返回 job_id。

    构建完成后自动注册到无人机影像注册表，地图组件可直接加载。
    """
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"GeoTIFF 文件不存在: {source_path}")
    key = _sanitize_layer_key(layer_key or os.path.splitext(os.path.basename(source_path))[0])
    req = types.SimpleNamespace(
        source_path=source_path,
        layer_key=key,
        name=name,
        area_key=area_key,
        year=year,
        min_zoom=max(0, min(22, int(min_zoom))),
        max_zoom=max(0, min(22, int(max_zoom))),
        max_native_zoom=max(0, min(22, int(max_zoom))),
        opacity=max(0.0, min(1.0, float(opacity))),
        tile_format=tile_format,
        quality=quality,
        overwrite=overwrite,
        source_srs=(source_srs or "").strip(),
    )
    job_id = _submit_tile_build_job({}, lambda job: _run_raster_publish_job(job, req))
    logger.info("raster publish submitted: key=%s source=%s job=%s", key, source_path, job_id)
    return job_id


def publish_vector_async(
    source_path: str,
    layer_key: str = "",
    name: str = "",
    min_zoom: int = 0,
    max_zoom: int = 18,
    style: Optional[Dict[str, Any]] = None,
    overwrite: bool = True,
) -> str:
    """提交矢量切片发布任务（GeoJSON → tippecanoe PBF），返回 job_id。

    源 GeoJSON 会复制到 uploaded_tile_data 目录并写入 tile_layers.json 注册表。
    """
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"GeoJSON 文件不存在: {source_path}")
    key = _sanitize_layer_key(layer_key or os.path.splitext(os.path.basename(source_path))[0])
    if key in _TILE_LAYER_META:
        raise HTTPException(status_code=400, detail=f"内置图层 key 不能覆盖: {key}")
    # 校验 GeoJSON 合法性
    try:
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"GeoJSON 文件无效: {e}")
    if not isinstance(data, dict) or data.get("type") not in ("FeatureCollection", "Feature"):
        raise HTTPException(status_code=400, detail="仅支持 GeoJSON FeatureCollection / Feature")
    # 复制源文件到托管目录（源文件已在托管目录则跳过）
    os.makedirs(_CUSTOM_TILE_DATA_DIR, exist_ok=True)
    target_path = os.path.join(_CUSTOM_TILE_DATA_DIR, f"{key}.geojson")
    if os.path.abspath(source_path) != os.path.abspath(target_path):
        try:
            if overwrite or not os.path.exists(target_path):
                shutil.copyfile(source_path, target_path)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"复制 GeoJSON 失败: {e}")
    style = style or _parse_style("#2773d7", "#2773d7", 0.18, 2)
    meta = {
        "label": (name or "").strip() or key,
        "color": style["stroke"],
        "source": target_path,
        "style": style,
        "min_zoom": max(0, min(22, int(min_zoom))),
        "max_zoom": max(0, min(22, int(max_zoom))),
        "build_type": "vector",
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    registry = _load_tile_registry()
    registry[key] = meta
    _save_tile_registry(registry)
    job_id = _submit_tile_build_job(
        {}, lambda job: _run_vector_publish_job(job, key, target_path, meta, min_zoom, max_zoom),
    )
    logger.info("vector publish submitted: key=%s source=%s job=%s", key, source_path, job_id)
    return job_id
