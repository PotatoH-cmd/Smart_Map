"""瓦片与图层管理：tile_manager · drone_imagery · 3dtiles · 卫星瓦片代理 · MVT（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from services.tile_manager import _3DTILES_DATA_DIR, _3DTILES_REGISTRY_PATH, _CUSTOM_TILE_DATA_DIR, _DRONE_BUILD_JOBS, _DRONE_IMAGERY_DIR, _DRONE_MBTILES_DIR, _DRONE_REGISTRY_PATH, _DRONE_WORK_DIR, _MAX_3DTILES_FILES, _MAX_3DTILES_UNZIP_BYTES, _MAX_3DTILES_ZIP_BYTES, _OVERLAY_DATA_DIR, _TILE_BUILD_JOBS, _TILE_LAYER_META, _TILE_REGISTRY_PATH, _VT_DIR, _3dtiles_layer_to_row, _auto_register_existing_3dtiles_async, _copy_upload_limited, _count_3dtiles, _dir_stats, _drone_layer_to_row, _extract_zip_safely, _invalidate_tile_stats, _load_3dtiles_registry, _load_drone_registry, _load_tile_registry, _locate_tileset_root, _mbtiles_metadata, _media_type_for_tile_format, _merged_tile_meta, _parse_bounds, _parse_style, _read_3dtiles_meta, _register_drone_imagery, _run_drone_build_with_progress, _run_drone_mbtiles_build, _run_tippecanoe, _sanitize_layer_key, _save_3dtiles_registry, _save_drone_registry, _save_tile_registry, _submit_tile_build_job, _ttl_cache, _write_3dtiles_meta
from tools.overlay_tile_service import get_tile_png as _overlay_get_tile_png, list_layers as _overlay_list_layers, query_feature as _overlay_query_feature, register_layer as _overlay_register_layer, unregister_layer as _overlay_unregister_layer
import os
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from tools.geoserver_client import GeoServerUnavailable as _GeoServerUnavailable
from datetime import datetime as dt
from tools import geoserver_client as _gs_client
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
import re
import psycopg2
from tools.postgresql_tool import PostgreSQLTool  # /api/vector-data 动态矢量加载
import shutil
import json
import contextlib
import asyncio
import uuid
import threading
import sqlite3
import tempfile
import httpx
import math
import base64
import logging
from core.config import postgis as _cfg_postgis

_PG_CONN = _cfg_postgis.as_dict()


logger = logging.getLogger(__name__)

router = APIRouter()


class TileRegenerateRequest(BaseModel):
    layer: str
class DroneImageryRegisterRequest(BaseModel):
    layer_key: str
    name: str = ""
    path: str
    area_key: str = ""
    year: Optional[int] = None
    min_zoom: int = 0
    max_zoom: int = 22
    max_native_zoom: Optional[int] = None
    bounds: Optional[List[float]] = None
    opacity: float = 0.9
    scheme: str = "tms"
class DroneImageryBuildRequest(BaseModel):
    source_path: str
    layer_key: str = ""
    name: str = ""
    area_key: str = ""
    year: Optional[int] = None
    min_zoom: int = 0
    max_zoom: int = 22
    opacity: float = 0.9
    tile_format: str = "PNG"
    quality: int = 85
    overwrite: bool = True
    source_srs: str = ""  # 手动指定源坐标系，如 EPSG:4547；为空则自动检测
class ThreeDTilesRegisterRequest(BaseModel):
    directory: str
    key: str = ""
    name: str = ""
    label: str = ""
    alt_offset: float = 0.0
    auto_ground_clamp: bool = True
    description: str = ""
class ThreeDTilesDeleteRequest(BaseModel):
    key: str
    delete_files: bool = False
def _register_custom_raster_layers() -> None:
    for key, meta in _load_tile_registry().items():
        if meta.get("build_type", "both") not in ("raster", "both"):
            continue
        source = meta.get("source")
        style = meta.get("style") or {}
        if source and os.path.isfile(source):
            try:
                _overlay_register_layer(key, source, style)
            except Exception as e:
                logger.warning("register custom raster layer failed %s: %s", key, e)
def _run_geojson_build_job(
    job: Dict[str, Any],
    safe_key: str,
    source_path: str,
    meta: Dict[str, Any],
    build_type: str,
    min_zoom: int,
    max_zoom: int,
    style: Dict[str, Any],
    auto_publish: bool,
) -> None:
    """后台执行 GeoJSON 构建：tippecanoe + 可选 GeoServer 发布。"""
    def _update(**kw):
        job.update(kw)
        job["updated_at"] = dt.now().isoformat(timespec="seconds")

    try:
        stats = {"tile_count": 0, "size_bytes": 0}
        if build_type in ("vector", "both"):
            _update(stage="tippecanoe", percent=5, message="开始生成矢量切片（tippecanoe）...")
            stats = _run_tippecanoe(safe_key, source_path, min_zoom, max_zoom)
            _update(stage="tippecanoe", percent=60, message="矢量切片生成完成")
        geoserver_result = None
        if auto_publish:
            _update(stage="publish", percent=70, message="正在发布到 GeoServer...")
            try:
                import_result = _gs_client.import_geojson_to_postgis(safe_key, source_path)
                geoserver_result = _gs_client.publish_by_tm_key(safe_key, {
                    "type": "vector",
                    "label": meta["label"],
                    "source_path": source_path,
                    "style": style,
                })
                geoserver_result["import"] = import_result
                _update(percent=90, message="GeoServer 发布完成")
            except _GeoServerUnavailable as e:
                logger.warning("GeoServer 自动发布失败: %s", e)
                geoserver_result = {"error": str(e)}
                _update(percent=95, message=f"GeoServer 发布失败（已跳过）: {str(e)[:100]}")
        _invalidate_tile_stats()
        _update(
            stage="done", percent=100, message="构建完成",
            success=True, done=True, stats=stats, geoserver=geoserver_result, layer=safe_key,
        )
    except HTTPException as e:
        _update(stage="error", percent=0, message=str(e.detail), success=False, done=True)
    except Exception as e:
        logger.exception("geojson build job failed: %s", safe_key)
        _update(stage="error", percent=0, message=str(e)[:200], success=False, done=True)
@router.get("/api/tile_manager/layers")
async def tile_manager_layers():
    layers = []
    for key, meta in _merged_tile_meta().items():
        source_path = meta["source"]
        style = meta.get("style") or {}
        min_zoom = int(meta.get("min_zoom", 0))
        max_zoom = int(meta.get("max_zoom", 18))
        build_type = meta.get("build_type", "both")
        vector_dir = os.path.join(_VT_DIR, key)
        stats = _dir_stats(vector_dir)
        vector_ready = build_type in ("vector", "both") and stats["tile_count"] > 0
        raster_ready = build_type in ("raster", "both") and os.path.isfile(source_path)
        color = style.get("stroke") or meta.get("color") or "#2773d7"
        layers.append({
            "key": key,
            "label": meta["label"],
            "type": "vector",
            "status": "ready" if vector_ready else "missing",
            "color": color,
            "tile_count": stats["tile_count"],
            "size_bytes": stats["size_bytes"],
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "api_url": f"/api/vector_tile/{key}/{{z}}/{{x}}/{{y}}.pbf",
            "directory": vector_dir,
            "source_path": source_path,
            "source_name": os.path.basename(source_path),
            "style": style,
            "custom": key not in _TILE_LAYER_META,
        })
        layers.append({
            "key": key,
            "label": meta["label"],
            "type": "raster",
            "status": "ready" if raster_ready else "missing",
            "color": color,
            "tile_count": None,
            "size_bytes": os.path.getsize(source_path) if os.path.isfile(source_path) else 0,
            "min_zoom": 0,
            "max_zoom": 22,
            "api_url": f"/api/overlay_tile/{key}/{{z}}/{{x}}/{{y}}.png",
            "directory": "后端实时渲染 + LRU 缓存",
            "source_path": source_path,
            "source_name": os.path.basename(source_path),
            "style": style,
            "custom": key not in _TILE_LAYER_META,
        })
    for key, meta in _load_drone_registry().items():
        if isinstance(meta, dict):
            layers.append(_drone_layer_to_row(key, meta))
    for key, meta in _load_3dtiles_registry().items():
        if isinstance(meta, dict):
            layers.append(_3dtiles_layer_to_row(key, meta))
    return JSONResponse(content={"success": True, "layers": layers})
@router.post("/api/tile_manager/regenerate")
async def tile_manager_regenerate(req: TileRegenerateRequest):
    key = req.layer
    meta_map = _merged_tile_meta()
    if key not in meta_map:
        raise HTTPException(status_code=404, detail=f"unknown tile layer: {key}")
    source_path = meta_map[key]["source"]
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"source geojson not found: {source_path}")
    min_zoom = int(meta_map[key].get("min_zoom", 0))
    max_zoom = int(meta_map[key].get("max_zoom", 18))

    def _run(job: Dict[str, Any]) -> None:
        job["stage"] = "tippecanoe"
        job["percent"] = 5
        job["message"] = "开始重新生成矢量切片..."
        job["updated_at"] = dt.now().isoformat(timespec="seconds")
        try:
            stats = _run_tippecanoe(key, source_path, min_zoom, max_zoom)
            _invalidate_tile_stats()
            job.update({
                "stage": "done", "percent": 100, "message": "重新生成完成",
                "success": True, "done": True, "layer": key, **stats,
            })
        except HTTPException as e:
            job.update({
                "stage": "error", "percent": 0, "message": str(e.detail),
                "success": False, "done": True,
            })
        except Exception as e:
            job.update({
                "stage": "error", "percent": 0, "message": str(e)[:200],
                "success": False, "done": True,
            })

    job_id = _submit_tile_build_job({}, _run)
    return JSONResponse(content={"success": True, "layer": key, "async": True, "job_id": job_id})
@router.get("/api/tile_manager/build_status/{job_id}")
async def tile_manager_build_status(job_id: str):
    """查询后台切片构建任务状态。"""
    job = _TILE_BUILD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="构建任务不存在或已过期")
    return JSONResponse(content={"success": True, "job": job})
@router.delete("/api/tile_manager/{layer_key}")
async def tile_manager_delete(layer_key: str, delete_files: bool = False):
    """删除自定义矢量/栅格图层（内置图层 hx/caiqu 不允许删除）。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer_key):
        raise HTTPException(status_code=400, detail=f"非法的图层 key: {layer_key}")
    if layer_key in _TILE_LAYER_META:
        raise HTTPException(status_code=403, detail=f"内置图层不允许删除: {layer_key}")

    registry = _load_tile_registry()
    if layer_key not in registry:
        # 检查是否是无人机图层
        drone_registry = _load_drone_registry()
        if layer_key in drone_registry:
            meta = drone_registry.pop(layer_key)
            _save_drone_registry(drone_registry)
            return JSONResponse(content={"success": True, "layer": layer_key, "type": "drone", "deleted_files": []})

        # 图层不在任何注册表中 —— 尝试从 GeoServer 取消发布 + 可选删除 PostGIS 表
        gs_removed = False
        try:
            gs_result = _gs_client.unpublish_layer(layer_key, recurse=True)
            gs_removed = gs_result.get("removed", False)
        except _GeoServerUnavailable:
            pass
        except Exception as e:
            logger.warning("GeoServer unpublish failed for virtual layer %s: %s", layer_key, e)

        pg_dropped = False
        if delete_files:
            # 尝试删除 PostGIS 表
            try:
                conn = psycopg2.connect(**_PG_CONN)
                try:
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute(f'DROP TABLE IF EXISTS public."{layer_key}" CASCADE')
                        pg_dropped = True
                    logger.info("PostGIS table dropped: %s", layer_key)
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("Drop PostGIS table failed for %s: %s", layer_key, e)

        # 即使 GeoServer 和 PostGIS 都没操作，也返回成功（前端会从列表中移除）
        logger.info("虚拟图层已删除: key=%s, gs_removed=%s, pg_dropped=%s", layer_key, gs_removed, pg_dropped)
        return JSONResponse(content={
            "success": True,
            "layer": layer_key,
            "type": "virtual",
            "gs_removed": gs_removed,
            "pg_dropped": pg_dropped,
            "deleted_files": [],
        })

    meta = registry.pop(layer_key)
    _save_tile_registry(registry)

    # 从内存栅格服务中注销
    _overlay_unregister_layer(layer_key)

    deleted_files: list = []
    if delete_files:
        # 删除矢量切片目录
        vt_dir = os.path.join(_VT_DIR, layer_key)
        if os.path.isdir(vt_dir):
            import shutil
            try:
                shutil.rmtree(vt_dir)
                deleted_files.append(vt_dir)
            except OSError as e:
                logger.warning("删除矢量切片目录失败: %s %s", vt_dir, e)
        # 删除源 GeoJSON（仅自定义图层）
        source_path = meta.get("source") or ""
        if source_path and os.path.isfile(source_path) and not source_path.startswith(_OVERLAY_DATA_DIR):
            try:
                os.remove(source_path)
                deleted_files.append(source_path)
            except OSError as e:
                logger.warning("删除源文件失败: %s %s", source_path, e)

    logger.info("图层已删除: key=%s, delete_files=%s, removed=%s", layer_key, delete_files, deleted_files)
    _invalidate_tile_stats()
    return JSONResponse(content={
        "success": True,
        "layer": layer_key,
        "deleted_files": deleted_files,
    })
@router.post("/api/tile_manager/build")
async def tile_manager_build(
    file: UploadFile = File(...),
    layer_key: str = Form(""),
    label: str = Form(""),
    build_type: str = Form("both"),
    stroke: str = Form("#2773d7"),
    fill: str = Form("#2773d7"),
    fill_alpha: float = Form(0.18),
    stroke_width: float = Form(2),
    point_size: float = Form(8),
    min_zoom: int = Form(0),
    max_zoom: int = Form(18),
    auto_publish: bool = Form(False),
):
    safe_key = _sanitize_layer_key(layer_key or os.path.splitext(file.filename or "")[0])
    if safe_key in _TILE_LAYER_META:
        raise HTTPException(status_code=400, detail="内置图层 key 不能覆盖")
    build_type = build_type if build_type in ("vector", "raster", "both") else "both"
    min_zoom = max(0, min(22, int(min_zoom)))
    max_zoom = max(min_zoom, min(22, int(max_zoom)))
    style = _parse_style(stroke, fill, fill_alpha, stroke_width, point_size)
    os.makedirs(_CUSTOM_TILE_DATA_DIR, exist_ok=True)
    source_path = os.path.join(_CUSTOM_TILE_DATA_DIR, f"{safe_key}.geojson")
    try:
        with open(source_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("type") not in ("FeatureCollection", "Feature"):
            raise ValueError("仅支持 GeoJSON FeatureCollection / Feature")
    except Exception as e:
        with contextlib.suppress(Exception):
            os.remove(source_path)
        raise HTTPException(status_code=400, detail=f"GeoJSON 文件无效: {e}")
    meta = {
        "label": label.strip() or safe_key,
        "color": style["stroke"],
        "source": source_path,
        "style": style,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "build_type": build_type,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    registry = _load_tile_registry()
    registry[safe_key] = meta
    _save_tile_registry(registry)
    if build_type in ("raster", "both"):
        _overlay_register_layer(safe_key, source_path, style)
    # 矢量切片生成与 GeoServer 发布放入后台任务，避免阻塞其他请求
    job_id = _submit_tile_build_job({}, lambda job: _run_geojson_build_job(
        job, safe_key, source_path, meta, build_type, min_zoom, max_zoom, style, auto_publish,
    ))
    return JSONResponse(content={
        "success": True,
        "layer": safe_key,
        "meta": meta,
        "async": True,
        "job_id": job_id,
    })
@router.get("/api/drone_imagery/layers")
async def drone_imagery_layers():
    layers = [
        _drone_layer_to_row(key, meta)
        for key, meta in _load_drone_registry().items()
        if isinstance(meta, dict)
    ]
    return JSONResponse(content={"success": True, "layers": layers})
@router.post("/api/drone_imagery/register")
async def drone_imagery_register(req: DroneImageryRegisterRequest):
    try:
        row = _register_drone_imagery(req)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(content={"success": True, "layer": row["key"], "item": row})
@router.delete("/api/drone_imagery/{layer_key}")
async def drone_imagery_delete(layer_key: str, delete_files: bool = False):
    """删除无人机影像图层：从注册表中移除，并可选删除 MBTiles / 工作文件。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer_key):
        raise HTTPException(status_code=400, detail=f"非法的图层 key: {layer_key}")
    registry = _load_drone_registry()
    if layer_key not in registry:
        raise HTTPException(status_code=404, detail=f"无人机影像图层不存在: {layer_key}")
    meta = registry.pop(layer_key)
    _save_drone_registry(registry)

    deleted_files = []
    if delete_files and isinstance(meta, dict):
        # 删除 MBTiles 文件
        mbtiles_path = meta.get("path") or ""
        if mbtiles_path and os.path.isfile(mbtiles_path):
            try:
                os.remove(mbtiles_path)
                deleted_files.append(mbtiles_path)
            except OSError as e:
                logger.warning("删除 MBTiles 文件失败: %s %s", mbtiles_path, e)
        # 删除工作目录中的临时文件（如 _3857.tif）
        for suffix in ("_3857.tif", ".mbtiles"):
            work_file = os.path.join(_DRONE_WORK_DIR, f"{layer_key}{suffix}")
            if os.path.isfile(work_file):
                try:
                    os.remove(work_file)
                    deleted_files.append(work_file)
                except OSError as e:
                    logger.warning("删除工作文件失败: %s %s", work_file, e)

    logger.info("无人机影像图层已删除: key=%s, delete_files=%s, removed=%s", layer_key, delete_files, deleted_files)
    _invalidate_tile_stats()
    return JSONResponse(content={
        "success": True,
        "layer": layer_key,
        "deleted_files": deleted_files,
    })
@router.get("/api/file_browser")
async def file_browser(path: str = "/mnt", extensions: str = ".tif,.tiff"):
    """浏览服务器目录，返回子目录和文件列表（异步+超时保护）"""
    import pathlib, asyncio, concurrent.futures

    def _list_dir(dir_path: str, exts: str):
        target = pathlib.Path(dir_path).resolve()
        if not target.exists():
            return {"error": f"路径不存在: {dir_path}", "code": 404}
        if not target.is_dir():
            return {"error": f"不是目录: {dir_path}", "code": 400}
        ext_set = set(e.strip().lower() for e in exts.split(",") if e.strip())
        dirs_list, files_list = [], []
        try:
            for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith('.'):
                    continue
                if item.is_dir():
                    dirs_list.append({"name": item.name, "type": "dir"})
                elif item.is_file():
                    if ext_set and item.suffix.lower() not in ext_set:
                        continue
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                    files_list.append({"name": item.name, "type": "file", "size": size})
        except PermissionError:
            return {"error": f"无权访问: {dir_path}", "code": 403}
        parent = str(target.parent) if str(target) != "/" else None
        return {"path": str(target), "parent": parent, "dirs": dirs_list, "files": files_list}

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _list_dir, path, extensions),
            timeout=10
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"读取目录超时（10秒），网络共享可能响应缓慢: {path}")
    if "error" in result:
        raise HTTPException(status_code=result["code"], detail=result["error"])
    return JSONResponse(content=result)
@router.post("/api/drone_imagery/build")
async def drone_imagery_build(req: DroneImageryBuildRequest):
    row = _run_drone_mbtiles_build(req)
    return JSONResponse(content={"success": True, "layer": row["key"], "item": row})
@router.post("/api/drone_imagery/build_stream")
async def drone_imagery_build_stream(req: DroneImageryBuildRequest):
    import asyncio, queue, threading

    def _producer(q: queue.Queue):
        try:
            for chunk in _run_drone_build_with_progress(req):
                q.put(chunk)
        except Exception as e:
            q.put(f"data: {json.dumps({'stage':'error','percent':0,'message':str(e)}, ensure_ascii=False)}\n\n")
        finally:
            q.put(None)

    async def _async_gen():
        q = queue.Queue()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _producer, q)
        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(_async_gen(), media_type="text/event-stream")
@router.post("/api/drone_imagery/build_async")
async def drone_imagery_build_async(req: DroneImageryBuildRequest):
    import threading
    job_id = uuid.uuid4().hex
    _DRONE_BUILD_JOBS[job_id] = {
        "job_id": job_id,
        "stage": "queued",
        "percent": 0,
        "message": "任务已提交，等待构建...",
        "done": False,
        "success": None,
        "created_at": dt.now().isoformat(timespec="seconds"),
        "updated_at": dt.now().isoformat(timespec="seconds"),
    }

    def _worker():
        try:
            chunks = _run_drone_build_with_progress(req)
            for chunk in chunks:
                if not chunk.startswith("data: "):
                    continue
                try:
                    payload = json.loads(chunk[6:].strip())
                except Exception:
                    continue
                job = _DRONE_BUILD_JOBS.get(job_id, {})
                job.update(payload)
                job["updated_at"] = dt.now().isoformat(timespec="seconds")
                if payload.get("stage") in ("done", "error"):
                    job["done"] = True
                    job["success"] = payload.get("stage") == "done"
                    job["finished_at"] = dt.now().isoformat(timespec="seconds")
                _DRONE_BUILD_JOBS[job_id] = job
        except Exception as e:
            payload = {"stage": "error", "percent": 0, "message": str(e)}
            job = _DRONE_BUILD_JOBS.get(job_id, {})
            job.update(payload)
            job["updated_at"] = dt.now().isoformat(timespec="seconds")
            job["done"] = True
            job["success"] = False
            job["finished_at"] = dt.now().isoformat(timespec="seconds")
            _DRONE_BUILD_JOBS[job_id] = job

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse(content={"success": True, "job_id": job_id})
@router.get("/api/drone_imagery/build_status/{job_id}")
async def drone_imagery_build_status(job_id: str):
    job = _DRONE_BUILD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="构建任务不存在或已过期")
    return JSONResponse(content={"success": True, "job": job})
@router.get("/api/drone_imagery/tile/{layer}/{z}/{x}/{y}.png")
async def drone_imagery_tile(layer: str, z: int, x: int, y: int):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=404, detail=f"unknown drone imagery layer: {layer}")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    registry = _load_drone_registry()
    meta = registry.get(layer)
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail=f"unknown drone imagery layer: {layer}")
    path = os.path.abspath(meta.get("path") or "")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"MBTiles 文件不存在: {path}")
    tile_row = (n - 1 - y) if meta.get("scheme", "tms") == "tms" else y
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tile_row),
            ).fetchone()
    except Exception as e:
        logger.exception("read drone imagery tile failed: %s/%s/%s/%s", layer, z, x, y)
        raise HTTPException(status_code=500, detail=str(e))
    if not row:
        return Response(status_code=204)
    media_type = _media_type_for_tile_format(str(meta.get("tile_format") or "png"))
    return Response(
        content=row[0],
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=604800",
            "Access-Control-Allow-Origin": "*",
        },
    )
@router.get("/api/tile_manager/3dtiles")
async def tile_manager_3dtiles_list():
    """列出所有已注册的 3D Tiles 数据集。"""
    registry = _load_3dtiles_registry()
    datasets = []
    for key, meta in registry.items():
        if isinstance(meta, dict):
            datasets.append(_3dtiles_layer_to_row(key, meta))
    return JSONResponse(content={"success": True, "datasets": datasets})
@router.post("/api/tile_manager/3dtiles/upload")
async def tile_manager_3dtiles_upload(
    file: UploadFile = File(...),
    key: str = Form(""),
    name: str = Form(""),
    label: str = Form(""),
    alt_offset: float = Form(0.0),
    auto_ground_clamp: bool = Form(True),
    description: str = Form(""),
):
    """上传 3D Tiles zip 包，自动解压并注册。"""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请选择 .zip 文件")
    safe_key = _sanitize_layer_key(key or os.path.splitext(file.filename)[0])
    target_dir = os.path.join(_3DTILES_DATA_DIR, safe_key)
    if os.path.exists(target_dir):
        raise HTTPException(status_code=409, detail=f"数据集 key 已存在: {safe_key}")
    os.makedirs(_3DTILES_DATA_DIR, exist_ok=True)
    tmp_path = ""
    try:
        import tempfile, zipfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            _copy_upload_limited(file.file, tmp, _MAX_3DTILES_ZIP_BYTES)
            tmp_path = tmp.name
        os.makedirs(target_dir, exist_ok=True)
        _extract_zip_safely(tmp_path, target_dir)
        if tmp_path:
            os.unlink(tmp_path)
            tmp_path = ""
    except zipfile.BadZipFile:
        with contextlib.suppress(Exception):
            if tmp_path:
                os.unlink(tmp_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="无效的 zip 文件")
    except ValueError as e:
        with contextlib.suppress(Exception):
            if tmp_path:
                os.unlink(tmp_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        with contextlib.suppress(Exception):
            if tmp_path:
                os.unlink(tmp_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except Exception as e:
        with contextlib.suppress(Exception):
            if tmp_path:
                os.unlink(tmp_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"解压失败: {str(e)}")
    # 兼容嵌套目录：zip 内唯一子目录含 tileset.json 时，将内容提升到数据集根目录
    tileset_root = _locate_tileset_root(target_dir)
    if tileset_root is None:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="zip 包中未找到 tileset.json")
    if tileset_root != target_dir:
        try:
            for entry in os.listdir(tileset_root):
                src = os.path.join(tileset_root, entry)
                dst = os.path.join(target_dir, entry)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
            shutil.rmtree(tileset_root, ignore_errors=True)
        except OSError as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"zip 目录结构处理失败: {e}")
    tileset_path = os.path.join(target_dir, "tileset.json")
    if not os.path.isfile(tileset_path):
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="zip 包中未找到 tileset.json")
    meta_info = _read_3dtiles_meta(target_dir)
    _write_3dtiles_meta(target_dir, {
        "name": name.strip() or meta_info.get("name") or safe_key,
        "label": label.strip() or meta_info.get("label") or name.strip() or safe_key,
        "altOffset": float(alt_offset),
        "autoGroundClamp": bool(auto_ground_clamp),
        "description": description.strip() or meta_info.get("description") or "",
        "center": meta_info.get("center"),
    })
    stats = _count_3dtiles(target_dir)
    registry = _load_3dtiles_registry()
    registry[safe_key] = {
        "directory": target_dir,
        "label": label.strip() or name.strip() or safe_key,
        "name": name.strip() or safe_key,
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "alt_offset": float(alt_offset),
        "auto_ground_clamp": bool(auto_ground_clamp),
        "center": meta_info.get("center"),
        "description": description.strip() or "",
        "meta": meta_info,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    _save_3dtiles_registry(registry)
    logger.info("3dtiles uploaded: key=%s, tiles=%s", safe_key, stats["tile_count"])
    return JSONResponse(content={
        "success": True,
        "layer": safe_key,
        "item": _3dtiles_layer_to_row(safe_key, registry[safe_key]),
    })
@router.post("/api/tile_manager/3dtiles/register")
async def tile_manager_3dtiles_register(req: ThreeDTilesRegisterRequest):
    """注册服务器上已有的 3D Tiles 目录。"""
    directory = os.path.abspath(req.directory)
    if not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail=f"目录不存在: {directory}")
    tileset_path = os.path.join(directory, "tileset.json")
    if not os.path.isfile(tileset_path):
        raise HTTPException(status_code=400, detail="目录中未找到 tileset.json")
    safe_key = _sanitize_layer_key(req.key or os.path.basename(directory))
    registry = _load_3dtiles_registry()
    if safe_key in registry:
        raise HTTPException(status_code=409, detail=f"数据集 key 已存在: {safe_key}")
    meta_info = _read_3dtiles_meta(directory)
    _write_3dtiles_meta(directory, {
        "name": req.name.strip() or meta_info.get("name") or safe_key,
        "label": req.label.strip() or meta_info.get("label") or req.name.strip() or safe_key,
        "altOffset": float(req.alt_offset),
        "autoGroundClamp": bool(req.auto_ground_clamp),
        "description": req.description.strip() or meta_info.get("description") or "",
        "center": meta_info.get("center"),
    })
    stats = _count_3dtiles(directory)
    registry[safe_key] = {
        "directory": directory,
        "label": req.label.strip() or req.name.strip() or safe_key,
        "name": req.name.strip() or safe_key,
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "alt_offset": float(req.alt_offset),
        "auto_ground_clamp": bool(req.auto_ground_clamp),
        "center": meta_info.get("center"),
        "description": req.description.strip() or "",
        "meta": meta_info,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    _save_3dtiles_registry(registry)
    logger.info("3dtiles registered: key=%s, directory=%s", safe_key, directory)
    return JSONResponse(content={
        "success": True,
        "layer": safe_key,
        "item": _3dtiles_layer_to_row(safe_key, registry[safe_key]),
    })
@router.post("/api/tile_manager/3dtiles/restats/{key}")
async def tile_manager_3dtiles_restats(key: str):
    """重新统计 3D Tiles 数据集的切片数与磁盘占用，并更新注册表缓存。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    registry = _load_3dtiles_registry()
    if key not in registry:
        raise HTTPException(status_code=404, detail=f"数据集不存在: {key}")
    meta = registry[key]
    directory = meta.get("directory") or ""
    if not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail=f"数据集目录不存在: {directory}")
    stats = _count_3dtiles(directory)
    meta["tile_count"] = stats["tile_count"]
    meta["size_bytes"] = stats["size_bytes"]
    meta["updated_at"] = dt.now().isoformat(timespec="seconds")
    _save_3dtiles_registry(registry)
    logger.info("3dtiles restats: key=%s, tiles=%s", key, stats["tile_count"])
    return JSONResponse(content={
        "success": True,
        "item": _3dtiles_layer_to_row(key, meta),
    })
@router.delete("/api/tile_manager/3dtiles/{key}")
async def tile_manager_3dtiles_delete(key: str, delete_files: bool = False):
    """删除已注册的 3D Tiles 数据集。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    registry = _load_3dtiles_registry()
    if key not in registry:
        raise HTTPException(status_code=404, detail=f"数据集不存在: {key}")
    meta = registry.pop(key)
    _save_3dtiles_registry(registry)
    deleted_files = []
    if delete_files:
        directory = meta.get("directory") or ""
        if directory and os.path.isdir(directory):
            try:
                shutil.rmtree(directory)
                deleted_files.append(directory)
            except OSError as e:
                logger.warning("删除 3D Tiles 目录失败: %s %s", directory, e)
    logger.info("3dtiles deleted: key=%s, delete_files=%s, removed=%s", key, delete_files, deleted_files)
    _invalidate_tile_stats()
    return JSONResponse(content={
        "success": True,
        "layer": key,
        "deleted_files": deleted_files,
    })
@router.get("/api/3dtiles/{key}/tileset.json")
async def serve_3dtiles_tileset(key: str):
    """提供 3D Tiles 的 tileset.json 给 Cesium 加载。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    registry = _load_3dtiles_registry()
    meta = registry.get(key)
    directory = (meta.get("directory") if isinstance(meta, dict) else None) or os.path.join(_3DTILES_DATA_DIR, key)
    tileset_path = os.path.join(directory, "tileset.json")
    if not os.path.isfile(tileset_path):
        raise HTTPException(status_code=404, detail="tileset.json 不存在")
    return FileResponse(
        tileset_path,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
    )
@router.get("/api/3dtiles/{key}/{file_path:path}")
async def serve_3dtiles_file(key: str, file_path: str):
    """提供 3D Tiles 数据集内的任意文件（子瓦片、b3dm 等）给 Cesium 加载。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    # 防止路径穿越
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    registry = _load_3dtiles_registry()
    meta = registry.get(key)
    directory = (meta.get("directory") if isinstance(meta, dict) else None) or os.path.join(_3DTILES_DATA_DIR, key)
    full_path = os.path.join(directory, file_path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    # 根据扩展名设置 MIME 类型
    ext = os.path.splitext(file_path)[1].lower()
    media_type_map = {
        ".json": "application/json",
        ".b3dm": "application/octet-stream",
        ".i3dm": "application/octet-stream",
        ".pnts": "application/octet-stream",
        ".cmpt": "application/octet-stream",
        ".gltf": "model/gltf+json",
        ".glb": "model/gltf-binary",
        ".bin": "application/octet-stream",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    media_type = media_type_map.get(ext, "application/octet-stream")
    return FileResponse(
        full_path,
        media_type=media_type,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
    )
# ---------- 卫星影像动态切片（从本地 GeoTIFF 按需渲染，自 main.py 迁入） ----------
try:
    from tools.satellite_tile_service import get_satellite_tile, get_tile_bounds_4326, tiles_in_bounds
    _satellite_tile_available = True
    logger.info("satellite tile service loaded")
except Exception as _e:
    _satellite_tile_available = False
    logger.warning("satellite tile service NOT available: %s", _e)


@router.get("/api/satellite_tile/{z}/{x}/{y}.png")
async def satellite_tile(z: int, x: int, y: int):
    if not _satellite_tile_available:
        raise HTTPException(status_code=503, detail="satellite tile service not available")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    png = get_satellite_tile(z, x, y)
    if png is None:
        return Response(status_code=204)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )
@router.get("/proxy/gf2023-tiles/{z}/{y}/{x}")
async def proxy_gf2023_tiles(z: int, y: int, x: int):
    """代理 2023年高分影像 GF_202308_cache 瓦片 (ArcGIS MapServer)"""
    url = f"http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202308_cache/MapServer/tile/{z}/{y}/{x}"
    return await _proxy_arcgis_tile(url)
@router.get("/proxy/gf2026-tiles/{z}/{y}/{x}")
async def proxy_gf2026_tiles(z: int, y: int, x: int):
    """代理 2026年Q1 本地 GeoTIFF 高分影像瓦片 → serve_tile.py (port 8090)"""
    url = f"http://127.0.0.1:8090/tiles/{z}/{x}/{y}.png"
    return await _proxy_local_tile(url)
async def _proxy_local_tile(url: str):
    """通用本地瓦片代理（serve_tile.py）"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
        if r.status_code == 204:
            return Response(status_code=204)
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
            "Content-Type": r.headers.get("Content-Type", "image/png"),
        }
        return Response(content=r.content, headers=resp_headers, status_code=r.status_code)
    except Exception as e:
        logger.error(f"代理本地瓦片失败: {url} - {e}")
        raise HTTPException(status_code=502, detail="本地瓦片服务不可达")
@router.get("/proxy/gf-tiles/{z}/{y}/{x}")
async def proxy_gf_tiles(z: int, y: int, x: int):
    """代理 GF_2024_YM 高分影像瓦片 (HTTP, /arcgis/ 路径)"""
    url = f"http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile/{z}/{y}/{x}"
    return await _proxy_arcgis_tile(url)
@router.get("/proxy/gf2025-tiles/{z}/{y}/{x}")
async def proxy_gf2025_tiles(z: int, y: int, x: int):
    """代理 GF_202509_cache 高分影像瓦片"""
    url = f"http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202509_cache/MapServer/tile/{z}/{y}/{x}"
    return await _proxy_arcgis_tile(url)
async def _proxy_arcgis_tile(url: str):
    """通用 ArcGIS 瓦片代理"""
    headers = {
        "User-Agent": "MapAssistant/1.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.get(url, headers=headers)
        content_type = r.headers.get("Content-Type", "image/png")
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=600",
            "Content-Type": content_type,
        }
        return Response(content=r.content, headers=resp_headers, status_code=r.status_code)
    except Exception as e:
        logger.error(f"代理 ArcGIS 瓦片失败: {url} - {e}")
        raise HTTPException(status_code=502, detail="代理上游不可达")
@router.get("/api/mvt/{z}/{x}/{y}")
async def get_mvt_tile(
    z: int,
    x: int,
    y: int,
    table_name: str = "ceshen",
    geom_col: str = "geom",
    properties: str = None,
    filter: str = None
):
    try:
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s]+$', table_name):
            raise HTTPException(status_code=400, detail="无效的表名格式")
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s]+$', geom_col):
            raise HTTPException(status_code=400, detail="无效的几何列格式")

        n = 2 ** z
        lon_min = x / n * 360.0 - 180.0
        lon_max = (x + 1) / n * 360.0 - 180.0
        import math
        def tile2lat(ty, tz):
            n_ = math.pi - 2.0 * math.pi * ty / (2.0 ** tz)
            return math.degrees(math.atan(math.sinh(n_)))
        lat_max = tile2lat(y, z)
        lat_min = tile2lat(y + 1, z)

        props_select = ""
        if properties:
            fields = [p.strip() for p in properties.split(",") if p.strip()]
            cleaned = []
            for f in fields:
                if f.startswith('"') and f.endswith('"'):
                    cleaned.append(f)
                else:
                    cleaned.append(f'"{f}"')
            for f in cleaned:
                props_select += f", t.{f}"

        safe_table = table_name if table_name.startswith('"') else f'"{table_name}"'
        env_sql = f"ST_Transform(ST_MakeEnvelope({lon_min}, {lat_min}, {lon_max}, {lat_max}, 4326), 3857)"
        where_extra = ""
        if filter:
            where_extra = f" AND ({filter})"

        sql = f"""
        SELECT encode(ST_AsMVT(q, '{table_name}', 4096, 'geom'), 'base64') AS tile
        FROM (
            SELECT
                ST_AsMVTGeom(
                    ST_Transform(t.{geom_col}, 3857),
                    b.env,
                    4096,
                    256,
                    true
                ) AS geom
                {props_select}
            FROM {safe_table} t
            JOIN (SELECT {env_sql} AS env) b ON TRUE
            WHERE t.{geom_col} IS NOT NULL
              AND ST_IsValid(t.{geom_col})
              AND ST_Intersects(ST_Transform(t.{geom_col}, 3857), b.env)
              {where_extra}
        ) AS q;
        """

        conn = psycopg2.connect(**_PG_CONN)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                if not row or not row[0]:
                    return Response(content=b"", media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=300"})
                import base64
                tile_bytes = base64.b64decode(row[0])
                return Response(content=tile_bytes, media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=300"})
        finally:
            try:
                conn.close()
            except:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"MVT error: {e}")
        raise HTTPException(status_code=500, detail="生成矢量瓦片失败")



def _resolve_vector_filter(pg_tool, safe_table_name: str, filter_text: str,
                           table_cols_lower: dict) -> Optional[str]:
    """校验 filter 中引用的字段是否存在于目标表。

    - 字段是目标表真实列 → 保留原样；
    - 目标表是 jsonb 属性表（如 caiqu/hx）且 properties 中存在该键
      → 改写为 (properties->>'字段')；
    - 字段既不是列也不在 properties 中 → 丢弃整个 filter（加载全部要素），
      避免"字段不存在"导致整表查询失败。
    """
    refs = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', filter_text or "")
    resolved = filter_text
    for field in refs:
        if field.lower() in table_cols_lower:
            continue  # 真实列，保留原样
        if "properties" in table_cols_lower:
            try:
                probe = pg_tool.call({
                    "operation": "query",
                    "sql": f"SELECT properties ? %s AS has_key FROM {safe_table_name} LIMIT 1",
                    "params": [field],
                })
                row = (probe.get("data") or [{}])[0] if probe.get("success") else {}
                if row.get("has_key"):
                    resolved = resolved.replace(
                        f'"{field}"', f"(properties->>'{field}')"
                    )
                    continue
            except Exception as e:
                logger.warning(f"Vector API properties probe failed for '{safe_table_name}': {e}")
        logger.warning(
            f"Vector API dropping filter for table '{safe_table_name}': "
            f"field '{field}' not found in columns or properties"
        )
        return None
    return resolved



@router.get("/api/vector-data")
async def get_vector_data(
    table_name: str,
    geom_col: str = 'geom',
    properties: str = None,
    filter: str = None,
    color_expression: str = None,
    debug: bool = False
):
    """
    动态获取指定表的 GeoJSON 数据
    :param table_name: 数据库表名
    :param geom_col: 几何列名，默认为 'geom'
    :param properties: 需要包含在 properties 中的字段名，逗号分隔。
    :param filter: SQL 过滤条件 (WHERE 后的内容，如 "name='xxx'")
    :param color_expression: SQL 颜色表达式，例如 "CASE WHEN depth < 10 THEN 'red' ELSE 'blue' END"
    """
    # 兼容性处理：如果请求的是旧表名 mineable_areas，自动映射到新表 ceshen
    target_table = table_name.strip().lower()
    if target_table == 'mineable_areas' or target_table == '"mineable_areas"':
        logger.info(f"Redirecting table_name from '{table_name}' to 'ceshen'")
        table_name = 'ceshen'

    try:
        logger.info(f"Vector API request: table_name={table_name}, geom_col={geom_col}, properties={properties}, filter={filter}, color_expression={color_expression}, debug={debug}")
        # 安全性校验：允许字母、数字、下划线、双引号、单引号、等号、空格和中文字符
        # 注意：此处 filter 校验需要比较宽松，但也需防止恶意 SQL 注入
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s\'\.\(\)\=\!\<\>\-\+]+$', table_name):
            raise HTTPException(status_code=400, detail="无效的表名格式")

        pg_tool = PostgreSQLTool(cfg={
            'host': '172.136.16.52',
            'port': 5432,
            'database': 'postgres',
            'user': 'postgres',
        })

        # 修复 color_expression 中的字段引用，增加表别名 t. 以避免字段不存在报错
        safe_color_expression = color_expression
        if color_expression:
            # 匹配双引号中的字段名，例如 "Measured_Depth" -> "t"."Measured_Depth"
            safe_color_expression = re.sub(r'("([a-zA-Z0-9_]+)")', r'"t".\1', color_expression)

        # 构建属性 JSON 对象
        if properties:
            props_list = [p.strip() for p in properties.split(',')]
            # 确保关键字段始终包含在内，用于前端 Popup 显示（仅限目标表实际存在的字段）
            essential_fields = [
                '"Mineable_Area_Name"', '"Measured_Depth"', '"Control_Elevation"',
                '"Lon_4326"', '"Lat_4326"', '"Year"', '"Mineable_Area_ID"', '"County_District"'
            ]
            for field in essential_fields:
                clean_field = field.replace('"', '')
                if clean_field.lower() in table_cols_lower and clean_field not in props_list:
                    # 用表中实际列名（兼容大小写）追加，避免引用不存在的字段导致查询失败
                    props_list.append(table_cols_lower[clean_field.lower()])
            
            # 修复：避免在 f-string 表达式中使用反斜杠
            formatted_props = []
            for p in props_list:
                if not p.startswith('"'):
                    formatted_props.append(f"'{p}', \"t\".\"{p}\"")
                else:
                    clean_p = p.replace('"', '')
                    formatted_props.append(f"'{clean_p}', \"t\".{p}")
            
            props_json = ", ".join(formatted_props)
            if safe_color_expression:
                props_json += f", '_style_color', {safe_color_expression}"
            props_sql = f"json_build_object({props_json})"
        else:
            if safe_color_expression:
                props_sql = f"(row_to_json(t)::jsonb - '{geom_col}' || jsonb_build_object('_style_color', {safe_color_expression}))::json"
            else:
                props_sql = f"(row_to_json(t)::jsonb - '{geom_col}')::json"

        safe_table_name = table_name if table_name.startswith('"') else f'"{table_name}"'

        # 查询目标表实际列名，动态决定是否启用经纬度回退（caiqu/hx 等 jsonb 表无 Lon_4326/Lat_4326 列）
        table_cols_lower = {}
        try:
            col_res = pg_tool.call({
                'operation': 'query',
                'sql': """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND LOWER(table_name) = LOWER(%s)
                """,
                'params': [table_name.strip('"')]
            })
            if col_res.get('success'):
                table_cols_lower = {str(r.get('column_name')).lower(): str(r.get('column_name'))
                                    for r in (col_res.get('data') or []) if r.get('column_name')}
        except Exception as e:
            logger.warning(f"Vector API failed to fetch columns for '{table_name}': {e}")
        has_lonlat = 'lon_4326' in table_cols_lower and 'lat_4326' in table_cols_lower
        lon_col = table_cols_lower.get('lon_4326') if has_lonlat else None
        lat_col = table_cols_lower.get('lat_4326') if has_lonlat else None
        lonlat_case = ""
        if has_lonlat:
            lonlat_case = (
                f'WHEN "{lon_col}" IS NOT NULL AND "{lat_col}" IS NOT NULL THEN\n'
                f'                                    ST_SetSRID(ST_MakePoint("{lon_col}", "{lat_col}"), 4326)\n'
            )

        # 处理过滤条件（包含几何或经纬度回退；经纬度回退仅对含经纬度列的表生效）
        where_geom_valid = f"({geom_col} IS NOT NULL)"
        if has_lonlat:
            where_lonlat_valid = f"(\"{lon_col}\" IS NOT NULL AND \"{lat_col}\" IS NOT NULL)"
            where_clause = f"WHERE ({where_geom_valid} OR {where_lonlat_valid})"
        else:
            where_clause = f"WHERE {where_geom_valid}"
        # 校验 filter 引用的字段是否存在于目标表；不存在时改写到 jsonb properties
        # 或直接丢弃 filter（加载全部要素），避免"字段不存在"导致整表查询失败。
        if filter:
            filter = _resolve_vector_filter(pg_tool, safe_table_name, filter, table_cols_lower)
        if filter:
            where_clause += f" AND ({filter})"

        def build_empty_meta(count_filter: str):
            count_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE {count_filter};"
            geom_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL);"
            geom_valid_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL AND ST_IsValid({geom_col}));"
            lonlat_res = {'success': False, 'error': None}
            if has_lonlat:
                lonlat_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND (\"{lon_col}\" IS NOT NULL AND \"{lat_col}\" IS NOT NULL);"
                lonlat_res = pg_tool.call({'operation': 'query', 'sql': lonlat_sql, 'params': []})
            count_res = pg_tool.call({'operation': 'query', 'sql': count_sql, 'params': []})
            geom_res = pg_tool.call({'operation': 'query', 'sql': geom_sql, 'params': []})
            geom_valid_res = pg_tool.call({'operation': 'query', 'sql': geom_valid_sql, 'params': []})
            return {
                "matched_total": (count_res.get("data") or [{}])[0].get("cnt") if count_res.get("success") else None,
                "geom_total": (geom_res.get("data") or [{}])[0].get("cnt") if geom_res.get("success") else None,
                "geom_valid_total": (geom_valid_res.get("data") or [{}])[0].get("cnt") if geom_valid_res.get("success") else None,
                "lonlat_total": (lonlat_res.get("data") or [{}])[0].get("cnt") if lonlat_res.get("success") else None,
                "matched_total_error": None if count_res.get("success") else count_res.get("error"),
                "geom_total_error": None if geom_res.get("success") else geom_res.get("error"),
                "geom_valid_total_error": None if geom_valid_res.get("success") else geom_valid_res.get("error"),
                "lonlat_total_error": None if lonlat_res.get("success") else lonlat_res.get("error"),
                "where_clause": where_clause,
            }

        sql = f"""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(
                json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(
                            CASE 
                                WHEN {geom_col} IS NOT NULL THEN 
                                    CASE 
                                        WHEN ST_SRID({geom_col}) = 0 THEN ST_SetSRID(ST_MakeValid({geom_col}), 4326)
                                        ELSE ST_MakeValid({geom_col})
                                    END
                                {lonlat_case}                                ELSE NULL
                            END, 6
                        )::json,
                        'properties', {props_sql}
                    )
                ), 
                '[]'::json
            )
        ) AS geojson
        FROM {safe_table_name} AS t
        {where_clause};
        """

        res = pg_tool.call({'operation': 'query', 'sql': sql, 'params': []})
        if not res.get('success'):
            error_msg = res.get('error', '数据库查询失败')
            logger.warning(f"Vector API query failed for table '{table_name}': {error_msg}")
            # 优雅降级：返回空 FeatureCollection 而非 500，让前端正常处理
            return JSONResponse(
                content={
                    "type": "FeatureCollection",
                    "features": [],
                    "meta": {
                        "status": "error",
                        "message": f"数据表 '{table_name}' 查询失败: {error_msg}",
                        "table_name": table_name,
                        "applied_filter": filter,
                    }
                },
                headers={"Cache-Control": "public, max-age=60"}
            )
        rows = res.get('data') or []

        # 保留失败容错：若两次查询均异常，返回空集合

        if not rows or len(rows) == 0:
            sql2 = f"""
            SELECT 
                ST_AsGeoJSON(
                    CASE 
                        WHEN {geom_col} IS NOT NULL THEN 
                            CASE 
                                WHEN ST_SRID({geom_col}) = 0 THEN ST_SetSRID(ST_MakeValid({geom_col}), 4326)
                                ELSE ST_MakeValid({geom_col})
                            END
                        {lonlat_case}                        ELSE NULL
                    END, 6
                ) AS geom_json,
                (row_to_json(t)::jsonb - '{geom_col}')::json AS props
            FROM {safe_table_name} AS t
            {where_clause};
            """
            res2 = pg_tool.call({'operation': 'query', 'sql': sql2, 'params': []})
            rows2 = res2.get('data') or []
            features2 = []
            for r in rows2:
                gj = r.get("geom_json")
                if not gj:
                    continue
                try:
                    geom = json.loads(gj)
                except:
                    geom = None
                props = r.get("props") or {}
                if geom:
                    features2.append({"type": "Feature", "geometry": geom, "properties": props})
            if features2:
                fc = {"type": "FeatureCollection", "features": features2, "meta": {"feature_count": len(features2), "table_name": table_name, "applied_filter": filter}}
                if debug:
                    fc["_debug"] = {"sql": sql2}
                return JSONResponse(content=fc, headers={"Cache-Control": "public, max-age=60"})
            count_filter = f"({filter})" if filter else "TRUE"
            meta = build_empty_meta(count_filter)
            logger.info(f"Vector query returned no rows: table={table_name}, filter={filter}, meta={meta}")
            content = {
                "type": "FeatureCollection",
                "features": [],
                "meta": {
                    "status": "empty",
                    "message": "查询成功但无可用要素",
                    "applied_filter": filter,
                    "table_name": table_name,
                    **meta
                }
            }
            if debug:
                content["_debug"] = {"sql": sql, **meta}
            return JSONResponse(content=content, headers={"Cache-Control": "public, max-age=60"})

        geojson = rows[0].get('geojson')
        if isinstance(geojson, str):
            try:
                geojson = json.loads(geojson)
            except Exception as e:
                logger.error(f"Vector API returned invalid JSON string: {e}")
                geojson = {"type": "FeatureCollection", "features": [], "meta": {"status": "invalid", "message": "后端返回数据格式异常"}}
        if not isinstance(geojson, dict):
            logger.error(f"Vector API returned non-dict geojson: {type(geojson)}")
            geojson = {"type": "FeatureCollection", "features": [], "meta": {"status": "invalid", "message": "后端返回数据格式异常"}}
        features = geojson.get("features")
        if not isinstance(features, list):
            features = []
            geojson["features"] = features
        feature_count = len(features)
        meta = geojson.get("meta") if isinstance(geojson.get("meta"), dict) else {}
        meta.update({"feature_count": feature_count, "table_name": table_name, "applied_filter": filter})
        geojson["meta"] = meta
        if feature_count == 0:
            count_filter = f"({filter})" if filter else "TRUE"
            empty_meta = build_empty_meta(count_filter)
            meta.update({"status": "empty", "message": "查询成功但无可用要素", **empty_meta})
            logger.info(f"Vector query returned empty features: table={table_name}, filter={filter}, meta={empty_meta}")
            if debug:
                geojson["_debug"] = {"sql": sql, **empty_meta}
        elif debug:
            geojson["_debug"] = {"sql": sql, "where_clause": where_clause}
        return JSONResponse(
            content=geojson,
            headers={"Cache-Control": "public, max-age=60"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Vector API error for table {table_name}")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")
