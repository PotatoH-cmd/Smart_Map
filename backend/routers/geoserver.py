"""GeoServer 发布管理：状态/图层/发布/切片（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from services.tile_manager import _3DTILES_DATA_DIR, _3DTILES_REGISTRY_PATH, _CUSTOM_TILE_DATA_DIR, _DRONE_BUILD_JOBS, _DRONE_IMAGERY_DIR, _DRONE_MBTILES_DIR, _DRONE_REGISTRY_PATH, _DRONE_WORK_DIR, _MAX_3DTILES_FILES, _MAX_3DTILES_UNZIP_BYTES, _MAX_3DTILES_ZIP_BYTES, _OVERLAY_DATA_DIR, _TILE_BUILD_JOBS, _TILE_LAYER_META, _TILE_REGISTRY_PATH, _VT_DIR, _3dtiles_layer_to_row, _auto_register_existing_3dtiles_async, _copy_upload_limited, _count_3dtiles, _dir_stats, _drone_layer_to_row, _extract_zip_safely, _invalidate_tile_stats, _load_3dtiles_registry, _load_drone_registry, _load_tile_registry, _locate_tileset_root, _mbtiles_metadata, _media_type_for_tile_format, _merged_tile_meta, _parse_bounds, _parse_style, _read_3dtiles_meta, _register_drone_imagery, _run_drone_build_with_progress, _run_drone_mbtiles_build, _run_tippecanoe, _sanitize_layer_key, _save_3dtiles_registry, _save_drone_registry, _save_tile_registry, _submit_tile_build_job, _ttl_cache, _write_3dtiles_meta
import os
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
from tools import geoserver_client as _gs_client
from tools.geoserver_client import GeoServerUnavailable as _GeoServerUnavailable
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
import re
import logging


logger = logging.getLogger(__name__)

router = APIRouter()


class GeoServerPublishRequest(BaseModel):
    layer: str
class GeoServerSeedRequest(BaseModel):
    layer: str
    bounds: Optional[List[float]] = None  # [minx, miny, maxx, maxy] in EPSG:4326
    min_zoom: int = 0
    max_zoom: int = 14
    format: str = "image/png"
    threads: int = 1
def _find_tile_layer_meta(layer_key: str) -> Optional[Dict[str, Any]]:
    """从 /api/tile_manager/layers 同源逻辑里查 meta，避免重复请求。"""
    # 内置 + 自定义矢量/栅格
    merged = _merged_tile_meta()
    if layer_key in merged:
        meta = merged[layer_key]
        return {
            "type": "vector" if meta.get("build_type", "both") in ("vector", "both") else "raster",
            "label": meta.get("label", layer_key),
            "source_path": meta.get("source"),
            "style": meta.get("style") or {},
        }
    # 无人机
    drone = _load_drone_registry()
    if layer_key in drone:
        meta = drone[layer_key]
        # 优先使用 _3857.tif（保留时）；否则回退 source_path
        source_path = meta.get("source_path")
        warped = os.path.join(_DRONE_WORK_DIR, f"{layer_key}_3857.tif")
        if os.path.isfile(warped):
            source_path = warped
        return {
            "type": "drone",
            "label": meta.get("name", layer_key),
            "source_path": source_path,
        }
    if layer_key == "ceshen":
        return {"type": "vector", "label": "ceshen", "source_path": ""}
    return None
@router.get("/api/geoserver/status")
async def geoserver_status():
    try:
        return JSONResponse(content=_gs_client.health_status())
    except Exception as e:  # 兜底，避免任何意外影响主流程
        logger.warning("geoserver_status error: %s", e)
        return JSONResponse(content={"available": False, "reason": str(e)})
@router.get("/api/geoserver/layers")
async def geoserver_layers():
    try:
        items = _gs_client.list_layers()
        return JSONResponse(content={"available": True, "layers": items})
    except _GeoServerUnavailable as e:
        return JSONResponse(content={"available": False, "reason": str(e), "layers": []})
    except Exception as e:
        logger.warning("geoserver_layers error: %s", e)
        return JSONResponse(content={"available": False, "reason": str(e), "layers": []})
@router.get("/api/geoserver/capabilities")
async def geoserver_capabilities():
    return JSONResponse(content={
        "url": _gs_client.get_config()["url"],
        "workspace": _gs_client.get_config()["workspace"],
        "capabilities": _gs_client.capabilities_urls(),
    })
@router.get("/api/geoserver/preview/{layer}.png")
async def geoserver_preview(layer: str, bbox: Optional[str] = None, width: int = 520, height: int = 280):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        parsed_bbox = None
        if bbox:
            parts = [float(v) for v in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("bbox must contain 4 numbers")
            parsed_bbox = parts
        content, content_type = _gs_client.preview_image(layer, bbox=parsed_bbox, width=width, height=height)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 预览失败: {e}")
@router.post("/api/geoserver/publish")
async def geoserver_publish(req: GeoServerPublishRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    meta = _find_tile_layer_meta(req.layer)
    if not meta:
        raise HTTPException(status_code=404, detail=f"unknown layer: {req.layer}")
    try:
        result = _gs_client.publish_by_tm_key(req.layer, meta)
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 发布失败: {e}")
@router.post("/api/geoserver/unpublish")
async def geoserver_unpublish(req: GeoServerPublishRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.unpublish_layer(req.layer, recurse=True)
        return JSONResponse(content={"success": True, "layer": req.layer, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 取消发布失败: {e}")
@router.post("/api/geoserver/seed")
async def geoserver_seed(req: GeoServerSeedRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.gwc_seed(
            req.layer,
            bounds=req.bounds,
            min_zoom=req.min_zoom,
            max_zoom=req.max_zoom,
            fmt=req.format,
            threads=req.threads,
        )
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC seed 失败: {e}")
@router.post("/api/geoserver/truncate")
async def geoserver_truncate(req: GeoServerSeedRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.gwc_truncate(
            req.layer,
            bounds=req.bounds,
            min_zoom=req.min_zoom,
            max_zoom=req.max_zoom,
            fmt=req.format,
        )
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC truncate 失败: {e}")
@router.get("/api/geoserver/seed/{layer}")
async def geoserver_seed_status(layer: str):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        return JSONResponse(content=_gs_client.gwc_seed_status(layer))
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC 状态查询失败: {e}")
