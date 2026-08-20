"""切片管理 - 注册表与统计层。

集中管理 4 类切片资源（矢量/栅格/无人机/3D Tiles）的：
- 路径常量
- 注册表（tile_layers.json / drone registry.json / 3dtiles registry.json）CRUD
- TTL 统计缓存（目录文件数、磁盘占用、MBTiles 元数据）
- 注册表条目 -> 前端行结构转换

本模块不依赖 FastAPI 路由与 GeoServer 客户端，可独立测试。
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import functools
from datetime import datetime as dt
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
_OVERLAY_DATA_DIR = os.environ.get(
    "MAP_OVERLAY_DATA_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "public", "data")),
)

_TILE_LAYER_META = {
    "hx": {
        "label": "河道红线",
        "color": "#ef4444",
        "source": os.path.join(_OVERLAY_DATA_DIR, "hx.geojson"),
    },
    "caiqu": {
        "label": "2025年采区边界",
        "color": "#facc15",
        "source": os.path.join(_OVERLAY_DATA_DIR, "caiqu.geojson"),
    },
}

_VT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "vector_tiles")
_CUSTOM_TILE_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "uploaded_tile_data")
_TILE_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "tile_layers.json")
_DRONE_IMAGERY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "drone_imagery")
_DRONE_REGISTRY_PATH = os.path.join(_DRONE_IMAGERY_DIR, "registry.json")
_DRONE_MBTILES_DIR = os.path.join(_DRONE_IMAGERY_DIR, "mbtiles")
_DRONE_WORK_DIR = os.path.join(_DRONE_IMAGERY_DIR, "work")
_3DTILES_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "3dtiles_data")
_3DTILES_REGISTRY_PATH = os.path.join(_3DTILES_DATA_DIR, "registry.json")

# 3D Tiles 上传限制
_MAX_3DTILES_ZIP_BYTES = 2 * 1024 ** 3      # 上传压缩包 ≤ 2GB
_MAX_3DTILES_UNZIP_BYTES = 20 * 1024 ** 3   # 解压后总量 ≤ 20GB
_MAX_3DTILES_FILES = 200000                 # 解压文件数上限


# ---------------------------------------------------------------------------
# 通用 TTL 缓存（用于目录/文件统计，避免列表接口频繁全量遍历磁盘）
# ---------------------------------------------------------------------------
_STATS_CACHE_TTL = 60.0  # 秒
_STATS_CACHE_INSTANCES: List[Any] = []


def _ttl_cache(ttl: float = _STATS_CACHE_TTL):
    """带 TTL 的内存缓存装饰器。统计结果允许短暂过期，避免接口频繁全量 IO。"""
    cache: Dict[Any, tuple] = {}
    lock = threading.Lock()

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with lock:
                hit = cache.get(key)
                if hit is not None and now - hit[0] < ttl:
                    return hit[1]
            result = fn(*args, **kwargs)
            with lock:
                cache[key] = (now, result)
                if len(cache) > 1024:
                    expired = [k for k, v in cache.items() if now - v[0] >= ttl]
                    for k in expired:
                        cache.pop(k, None)
                if len(cache) > 1024:
                    oldest = sorted(cache.items(), key=lambda kv: kv[1][0])
                    for k, _ in oldest[: len(cache) // 2]:
                        cache.pop(k, None)
            return result

        def _clear():
            with lock:
                cache.clear()

        wrapper.clear = _clear
        _STATS_CACHE_INSTANCES.append(wrapper)
        return wrapper

    return deco


def _invalidate_tile_stats() -> None:
    """清除切片统计缓存（构建/删除/上传等变更操作后调用）。"""
    for w in _STATS_CACHE_INSTANCES:
        try:
            w.clear()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _sanitize_layer_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    if not key:
        key = f"layer_{uuid.uuid4().hex[:8]}"
    return key[:64]


def _parse_style(stroke: str, fill: str, fill_alpha: float, stroke_width: float, point_size: float = 8) -> Dict[str, Any]:
    hex_pattern = r"^#[0-9a-fA-F]{6}$"
    stroke = stroke if re.match(hex_pattern, stroke or "") else "#2773d7"
    fill = fill if re.match(hex_pattern, fill or "") else stroke
    return {
        "stroke": stroke,
        "fill": fill,
        "fillAlpha": max(0.0, min(1.0, float(fill_alpha))),
        "strokeWidth": max(1.0, min(20.0, float(stroke_width))),
        "pointSize": max(2.0, min(64.0, float(point_size))),
    }


def _parse_bounds(value: Any) -> Optional[List[float]]:
    if isinstance(value, list) and len(value) == 4:
        try:
            return [float(v) for v in value]
        except Exception:
            return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 4:
            try:
                return [float(p) for p in parts]
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# 注册表 CRUD
# ---------------------------------------------------------------------------
def _load_tile_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_TILE_REGISTRY_PATH):
        return {}
    try:
        with open(_TILE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load tile registry failed: %s", e)
        return {}


def _save_tile_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_TILE_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_TILE_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _TILE_REGISTRY_PATH)


def _load_drone_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_DRONE_REGISTRY_PATH):
        return {}
    try:
        with open(_DRONE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load drone imagery registry failed: %s", e)
        return {}


def _save_drone_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_DRONE_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_DRONE_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _DRONE_REGISTRY_PATH)


def _load_3dtiles_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_3DTILES_REGISTRY_PATH):
        return {}
    try:
        with open(_3DTILES_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load 3dtiles registry failed: %s", e)
        return {}


def _save_3dtiles_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_3DTILES_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_3DTILES_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _3DTILES_REGISTRY_PATH)


# ---------------------------------------------------------------------------
# 统计（TTL 缓存）
# ---------------------------------------------------------------------------
@_ttl_cache()
def _dir_stats(path: str) -> Dict[str, int]:
    """统计目录下的 .pbf 矢量切片数量与总大小。"""
    total_size = 0
    tile_count = 0
    if not os.path.isdir(path):
        return {"size_bytes": 0, "tile_count": 0}
    for root, _, files in os.walk(path):
        for name in files:
            if not name.endswith(".pbf"):
                continue
            tile_count += 1
            try:
                total_size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return {"size_bytes": total_size, "tile_count": tile_count}


@_ttl_cache()
def _count_3dtiles(directory: str) -> Dict[str, int]:
    """统计 3D Tiles 目录下的 b3dm/json 文件数量和总大小。"""
    tile_count = 0
    total_size = 0
    if not os.path.isdir(directory):
        return {"tile_count": 0, "size_bytes": 0}
    for root, _, files in os.walk(directory):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in (".b3dm", ".json", ".i3dm", ".pnts", ".cmpt"):
                tile_count += 1
                try:
                    total_size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return {"tile_count": tile_count, "size_bytes": total_size}


@_ttl_cache()
def _mbtiles_metadata(path: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not os.path.isfile(path):
        return meta
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT name, value FROM metadata").fetchall()
            meta = {str(k): v for k, v in rows}
            zoom_row = conn.execute("SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles").fetchone()
            if zoom_row:
                meta["minzoom_actual"] = zoom_row[0]
                meta["maxzoom_actual"] = zoom_row[1]
                meta["tile_count"] = zoom_row[2]
    except Exception as e:
        logger.warning("read mbtiles metadata failed %s: %s", path, e)
    return meta


# ---------------------------------------------------------------------------
# 3D Tiles meta 读写
# ---------------------------------------------------------------------------
def _read_3dtiles_meta(directory: str) -> Dict[str, Any]:
    """读取 3D Tiles 目录下的 meta.json。"""
    meta_path = os.path.join(directory, "meta.json")
    if not os.path.isfile(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_3dtiles_meta(directory: str, meta: Dict[str, Any]) -> None:
    """写入 3D Tiles meta.json。"""
    meta_path = os.path.join(directory, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 注册表条目 -> 前端行结构
# ---------------------------------------------------------------------------
def _3dtiles_layer_to_row(key: str, meta: Dict[str, Any], refresh_stats: bool = False) -> Dict[str, Any]:
    directory = meta.get("directory") or ""
    if refresh_stats:
        stats = _count_3dtiles(directory)
    else:
        # 优先使用注册表缓存统计（注册/上传/重新统计时更新），避免每次列表全量遍历目录
        stats = {
            "tile_count": int(meta.get("tile_count") or 0),
            "size_bytes": int(meta.get("size_bytes") or 0),
        }
    tileset_url = f"/api/3dtiles/{key}/tileset.json"
    has_tileset = os.path.isfile(os.path.join(directory, "tileset.json"))
    meta_info = meta.get("meta") or _read_3dtiles_meta(directory)
    center = meta.get("center") or meta_info.get("center")
    return {
        "key": key,
        "label": meta.get("label") or meta_info.get("label") or meta.get("name") or key,
        "type": "3dtiles",
        "status": "ready" if has_tileset else "missing",
        "color": "#722ed1",
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "min_zoom": 0,
        "max_zoom": 22,
        "api_url": tileset_url,
        "directory": directory,
        "source_path": directory,
        "source_name": os.path.basename(directory),
        "custom": True,
        "alt_offset": meta.get("alt_offset", 0.0),
        "auto_ground_clamp": meta.get("auto_ground_clamp", True),
        "center": center,
        "description": meta.get("description") or meta_info.get("description") or "",
        "tileset_url": tileset_url,
    }


def _drone_layer_to_row(key: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    path = meta.get("path") or ""
    mb_meta = _mbtiles_metadata(path)
    tile_count = int(mb_meta.get("tile_count") or 0)
    # 以注册表值为准，但必须被 MBTiles 实际数据范围约束，避免前端大量 204 请求
    reg_min = int(meta.get("min_zoom", 0))
    reg_max = int(meta.get("max_zoom", 22))
    mb_min = int(mb_meta.get("minzoom_actual") or mb_meta.get("minzoom") or 0)
    mb_max = int(mb_meta.get("maxzoom_actual") or mb_meta.get("maxzoom") or 22)
    min_zoom = max(reg_min if meta.get("min_zoom") is not None else mb_min, mb_min)
    max_zoom = min(reg_max if meta.get("max_zoom") is not None else mb_max, mb_max)
    max_native = int(meta.get("max_native_zoom") or max_zoom)
    if mb_max > 0:
        max_native = min(max_native, mb_max)
    fmt = str(mb_meta.get("format") or meta.get("tile_format") or "png").lower()
    return {
        "key": key,
        "label": meta.get("name") or meta.get("label") or key,
        "type": "drone",
        "status": "ready" if os.path.isfile(path) and tile_count > 0 else "missing",
        "color": "#722ed1",
        "tile_count": tile_count,
        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "max_native_zoom": max_native,
        "api_url": f"/api/drone_imagery/tile/{key}/{{z}}/{{x}}/{{y}}.png",
        "directory": path,
        "source_path": meta.get("source_path") or path,
        "source_name": os.path.basename(meta.get("source_path") or path),
        "style": {"opacity": float(meta.get("opacity", 0.9))},
        "custom": True,
        "area_key": meta.get("area_key") or "",
        "year": meta.get("year"),
        "bounds": meta.get("bounds") or _parse_bounds(mb_meta.get("bounds")),
        "tile_format": fmt,
        "storage": "mbtiles",
    }


def _media_type_for_tile_format(fmt: str) -> str:
    fmt = (fmt or "").lower()
    if fmt in ("jpg", "jpeg"):
        return "image/jpeg"
    if fmt == "webp":
        return "image/webp"
    return "image/png"


def _register_drone_imagery(req: Any) -> Dict[str, Any]:
    """注册无人机影像 MBTiles（req 为鸭子类型：DroneImageryRegisterRequest）。"""
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(req.path))[0])
    path = os.path.abspath(req.path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"MBTiles 文件不存在: {path}")
    mb_meta = _mbtiles_metadata(path)
    if not mb_meta.get("tile_count"):
        raise ValueError("MBTiles 中未找到 tiles 数据")
    min_zoom = max(0, min(22, int(req.min_zoom if req.min_zoom is not None else mb_meta.get("minzoom_actual") or 0)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom if req.max_zoom is not None else mb_meta.get("maxzoom_actual") or 22)))
    meta = {
        "name": req.name.strip() or mb_meta.get("name") or key,
        "area_key": req.area_key.strip(),
        "year": req.year,
        "path": path,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "max_native_zoom": int(req.max_native_zoom or max_zoom),
        "bounds": getattr(req, "bounds", None) or _parse_bounds(mb_meta.get("bounds")),
        "opacity": max(0.0, min(1.0, float(req.opacity))),
        "scheme": req.scheme if req.scheme in ("tms", "xyz") else "tms",
        "tile_format": str(mb_meta.get("format") or "png").lower(),
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    registry = _load_drone_registry()
    registry[key] = meta
    _save_drone_registry(registry)
    return _drone_layer_to_row(key, meta)


def _merged_tile_meta() -> Dict[str, Dict[str, Any]]:
    merged = dict(_TILE_LAYER_META)
    for key, meta in _load_tile_registry().items():
        if isinstance(meta, dict):
            merged[key] = meta
    return merged
