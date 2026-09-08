"""GeoLibre 实景地球：项目注入 · 瓦片叠加 · 矢量瓦片（自 main.py 机械搬移，行为不变）。"""
import json
import math
import logging
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from services.tile_manager import _3DTILES_DATA_DIR, _3DTILES_REGISTRY_PATH, _CUSTOM_TILE_DATA_DIR, _DRONE_BUILD_JOBS, _DRONE_IMAGERY_DIR, _DRONE_MBTILES_DIR, _DRONE_REGISTRY_PATH, _DRONE_WORK_DIR, _MAX_3DTILES_FILES, _MAX_3DTILES_UNZIP_BYTES, _MAX_3DTILES_ZIP_BYTES, _OVERLAY_DATA_DIR, _TILE_BUILD_JOBS, _TILE_LAYER_META, _TILE_REGISTRY_PATH, _VT_DIR, _3dtiles_layer_to_row, _auto_register_existing_3dtiles_async, _copy_upload_limited, _count_3dtiles, _dir_stats, _drone_layer_to_row, _extract_zip_safely, _invalidate_tile_stats, _load_3dtiles_registry, _load_drone_registry, _load_tile_registry, _locate_tileset_root, _mbtiles_metadata, _media_type_for_tile_format, _merged_tile_meta, _parse_bounds, _parse_style, _read_3dtiles_meta, _register_drone_imagery, _run_drone_build_with_progress, _run_drone_mbtiles_build, _run_tippecanoe, _sanitize_layer_key, _save_3dtiles_registry, _save_drone_registry, _save_tile_registry, _submit_tile_build_job, _ttl_cache, _write_3dtiles_meta
import os
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
from tools.overlay_tile_service import get_tile_png as _overlay_get_tile_png, list_layers as _overlay_list_layers, query_feature as _overlay_query_feature, register_layer as _overlay_register_layer, unregister_layer as _overlay_unregister_layer
import re
import logging
# GeoLibre 图层工作台默认样式（与 GeoLibre 项目 schema 对齐）
_GEOLIBRE_STYLE = {
    "minZoom": 0, "maxZoom": 24,
    "fillColor": "#1d4ed8", "strokeColor": "#1e3a8a", "strokeWidth": 2, "fillOpacity": 0.25,
    "circleRadius": 6, "textColor": "#111827", "textHaloColor": "#ffffff",
    "textHaloWidth": 2, "textSize": 16,
    "extrusionEnabled": False, "extrusionColor": "#3b82f6", "extrusionOpacity": 0.8,
    "extrusionHeightProperty": "height", "extrusionHeightScale": 1, "extrusionBase": 0,
    "extrusionAdvancedStyleEnabled": False, "extrusionColorExpression": "",
    "extrusionHeightExpression": "", "vectorStyleMode": "single", "vectorStyleProperty": "",
    "vectorStyleClassCount": 5, "vectorStyleColorRamp": "viridis",
    "vectorStyleClassificationScheme": "equal-interval",
    "vectorStyleStops": [{"value": 0, "color": "#dbeafe"}, {"value": 1, "color": "#2563eb"}],
    "vectorStyleExpression": "", "pointRenderer": "single",
    "heatmapRadius": 30, "heatmapIntensity": 1, "clusterRadius": 50, "clusterMaxZoom": 14,
    "rasterBrightnessMin": 0, "rasterBrightnessMax": 1, "rasterSaturation": 0,
    "rasterContrast": 0, "rasterHueRotate": 0,
}


logger = logging.getLogger(__name__)

router = APIRouter()


def _compute_3dtiles_ground_offset(tileset_path: str) -> float:
    """估算 3D Tiles 在 GeoLibre（地表为 0m 椭球面）中的贴地 altitudeOffset（米，负值=下移）。

    maplibre-gl-3d-tiles 以根节点包围体中心为锚点放置模型，因此
    偏移量 = -(包围体中心海拔 - 包围体垂直半高)，将包围体底面压到 0m 地表。
    """
    try:
        from pyproj import Transformer

        with open(tileset_path, "r", encoding="utf-8") as fh:
            t = json.load(fh)
        root = t.get("root") or {}
        tr = root.get("transform")
        if not tr or len(tr) < 12:
            return 0.0
        tx, ty, tz = tr[12], tr[13], tr[14]
        n = math.sqrt(tx * tx + ty * ty + tz * tz) or 1.0
        up = (tx / n, ty / n, tz / n)
        bv = root.get("boundingVolume") or {}
        if "box" in bv and len(bv["box"]) == 12:
            b = bv["box"]
            # 包围盒世界中心 = R @ local_center + t（transform 为 column-major）
            cx = tr[0] * b[0] + tr[4] * b[1] + tr[8] * b[2] + tx
            cy = tr[1] * b[0] + tr[5] * b[1] + tr[9] * b[2] + ty
            cz = tr[2] * b[0] + tr[6] * b[1] + tr[10] * b[2] + tz
            # 垂直半高 = 三个半轴在 up 方向投影绝对值之和
            vhalf = 0.0
            for i, half in enumerate((b[3], b[7], b[11])):
                col = (tr[4 * i], tr[4 * i + 1], tr[4 * i + 2])
                ln = math.sqrt(col[0] ** 2 + col[1] ** 2 + col[2] ** 2) or 1.0
                vhalf += abs(half) * abs(col[0] * up[0] + col[1] * up[1] + col[2] * up[2]) / ln
        elif "sphere" in bv and len(bv["sphere"]) == 4:
            s = bv["sphere"]
            cx = tr[0] * s[0] + tr[4] * s[1] + tr[8] * s[2] + tx
            cy = tr[1] * s[0] + tr[5] * s[1] + tr[9] * s[2] + ty
            cz = tr[2] * s[0] + tr[6] * s[1] + tr[10] * s[2] + tz
            vhalf = abs(s[3])
        else:
            cx, cy, cz = tx, ty, tz
            vhalf = 0.0
        _lon, _lat, alt = Transformer.from_crs(
            "EPSG:4978", "EPSG:4979", always_xy=True
        ).transform(cx, cy, cz)
        bottom = alt - vhalf
        return round(-bottom) if bottom > 0 else 0.0
    except Exception as exc:
        logging.warning("计算 3D Tiles 贴地偏移失败 %s: %s", tileset_path, exc)
        return 0.0
@router.get("/api/geolibre/project")
async def geolibre_project(request: Request):
    """为 GeoLibre 图层工作台动态生成 .geolibre.json 项目。

    预置四个图层：2023年高分影像、河道红线、2026年采区边界、北汝河实景三维。
    host 依据请求动态拼接，保证 GeoLibre(8090) 内各图层 URL 指向当前服务器。
    """
    base = f"{request.url.scheme}://{request.url.netloc}"

    # 读取 2026 年采区边界（geojson 内嵌；数据量小，直接嵌入项目）
    overlay_data_dir = os.environ.get(
        "MAP_OVERLAY_DATA_DIR",
        "/home/server/python/map_assistant_v1/frontend/public/data",
    )
    caiqu2026_path = os.path.join(overlay_data_dir, "caiqu2026.geojson")
    caiqu2026 = {"type": "FeatureCollection", "features": []}
    if os.path.isfile(caiqu2026_path):
        try:
            with open(caiqu2026_path, "r", encoding="utf-8") as fh:
                caiqu2026 = json.load(fh)
        except Exception as exc:
            logging.warning(f"读取 caiqu2026.geojson 失败: {exc}")

    # 北汝河 3D Tiles 贴地偏移：GeoLibre 地表为 0m 椭球面，按包围盒底面下移
    _beiruhe_meta = _load_3dtiles_registry().get("jiaxian-beiruhe") or {}
    _beiruhe_dir = _beiruhe_meta.get("directory") or os.path.join(_3DTILES_DATA_DIR, "jiaxian-beiruhe")
    beiruhe_offset = _compute_3dtiles_ground_offset(os.path.join(_beiruhe_dir, "tileset.json"))

    layers = [
        {
            "id": "geolibre-gf-2023",
            "name": "2023年高分影像",
            "type": "xyz",
            "visible": True,
            "opacity": 1,
            "style": _GEOLIBRE_STYLE,
            "metadata": {"sourceKind": "xyz-url"},
            "source": {
                "type": "raster",
                "tiles": [f"{base}/proxy/gf2023-tiles/{{z}}/{{y}}/{{x}}"],
                "tileSize": 256,
                "url": f"{base}/proxy/gf2023-tiles/{{z}}/{{y}}/{{x}}",
                "attribution": "2023年高分影像",
                "maxzoom": 18,
            },
        },
        {
            "id": "geolibre-hx",
            "name": "河道红线",
            "type": "xyz",
            "visible": True,
            "opacity": 1,
            "style": _GEOLIBRE_STYLE,
            "metadata": {"sourceKind": "xyz-url"},
            "source": {
                "type": "raster",
                "tiles": [f"{base}/api/overlay_tile/hx/{{z}}/{{x}}/{{y}}.png"],
                "tileSize": 256,
                "url": f"{base}/api/overlay_tile/hx/{{z}}/{{x}}/{{y}}.png",
                "attribution": "河道红线",
                "maxzoom": 18,
            },
        },
        {
            "id": "geolibre-caiqu-2026",
            "name": "2026年采区边界",
            "type": "geojson",
            "visible": True,
            "opacity": 1,
            "style": {
                **_GEOLIBRE_STYLE,
                "fillColor": "#1d4ed8",
                "strokeColor": "#1e3a8a",
                "fillOpacity": 0.3,
                "strokeWidth": 2,
            },
            "metadata": {},
            "source": {"type": "geojson"},
            "geojson": caiqu2026,
        },
        {
            "id": "geolibre-beiruhe-3dtiles",
            "name": "北汝河实景三维",
            "type": "3d-tiles",
            "visible": True,
            "opacity": 1,
            "style": _GEOLIBRE_STYLE,
            "metadata": {
                "sourceKind": "3d-tiles-url",
                "externalNativeLayer": True,
                "customLayerType": "3d-tiles",
                "identifiable": False,
            },
            "source": {
                "type": "3d-tiles",
                "url": f"{base}/api/3dtiles/jiaxian-beiruhe/tileset.json",
                "altitudeOffset": beiruhe_offset,
            },
            "sourcePath": f"{base}/api/3dtiles/jiaxian-beiruhe/tileset.json",
        },
    ]

    return {
        "version": "0.1.0",
        "name": "豫水智能一张图 - 图层工作台",
        "mapView": {"center": [114.0, 32.1], "zoom": 11, "bearing": 0, "pitch": 0},
        "basemapStyleUrl": "https://tiles.openfreemap.org/styles/liberty",
        "basemapVisible": True,
        "basemapOpacity": 1,
        "layers": layers,
        "styles": {},
        "preferences": {
            "map": {
                "restrictBounds": False,
                "bounds": [-180, -85, 180, 85],
                "minZoom": 0,
                "maxZoom": 24,
                "maxPitch": 85,
                "renderWorldCopies": True,
            },
            "environmentVariables": [],
        },
        "metadata": {"generated_by": "yushui_map_assistant"},
    }
@router.get("/api/overlay_tile/{layer}/{z}/{x}/{y}.png")
async def overlay_tile(layer: str, z: int, x: int, y: int):
    """矢量图层的栅格瓦片接口（hx / caiqu 等）。"""
    if layer not in _overlay_list_layers():
        raise HTTPException(status_code=404, detail=f"unknown overlay layer: {layer}")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    try:
        png_bytes = _overlay_get_tile_png(layer, z, x, y)
    except Exception as e:
        logger.exception("overlay tile render failed: %s/%s/%s/%s", layer, z, x, y)
        raise HTTPException(status_code=500, detail=str(e))
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )
@router.get("/api/vector_tile/{layer}/{z}/{x}/{y}.pbf")
async def vector_tile(layer: str, z: int, x: int, y: int):
    """预生成的矢量切片接口（tippecanoe .pbf），供 Leaflet 2D 地图使用。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=404, detail=f"unknown vector tile layer: {layer}")
    pbf_path = os.path.join(_VT_DIR, layer, str(z), str(x), f"{y}.pbf")
    if not os.path.abspath(pbf_path).startswith(os.path.abspath(_VT_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="invalid tile path")
    if not os.path.isfile(pbf_path):
        return Response(status_code=204)
    return FileResponse(
        pbf_path,
        media_type="application/x-protobuf",
        headers={
            "Cache-Control": "public, max-age=604800",
            "Access-Control-Allow-Origin": "*",
        },
    )
@router.get("/api/overlay_feature/{layer}")
async def overlay_feature(layer: str, lng: float, lat: float, tolerance_m: float = 120.0):
    if layer not in _overlay_list_layers():
        raise HTTPException(status_code=404, detail=f"unknown overlay layer: {layer}")
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="invalid coordinate")
    try:
        feature = _overlay_query_feature(layer, lng, lat, tolerance_m)
    except Exception as e:
        logger.exception("overlay feature query failed: %s/%s/%s", layer, lng, lat)
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(
        content={"found": feature is not None, "feature": feature},
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )
