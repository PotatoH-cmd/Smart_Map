"""
Satellite Tile Service
======================
从本地 GeoTIFF 按需生成 XYZ 栅格瓦片（Web Mercator / EPSG:3857 → PNG 256×256）。
利用 rasterio 的窗口读取 + COG 式 overview，实现高效按需切片。
无需预生成 MBTiles，启动即服务。

支持多数据源（参数化）：每个数据源以 key 注册（key → GeoTIFF 路径），
瓦片接口通过 key 选择数据源。注册表持久化到 JSON，重启后自动恢复。

用法:
    from tools.satellite_tile_service import (
        register_source, unregister_source, list_sources,
        get_satellite_tile, get_tile_bounds_4326,
    )
    register_source("henan2026", "/path/to/henan.tif")
    png_bytes = get_satellite_tile(z, x, y, key="henan2026")   # → bytes (PNG) 或 None
    bounds     = get_tile_bounds_4326(z, x, y)                 # → (west, south, east, north)
"""

from __future__ import annotations

import io
import json
import math
import logging
import os
import re
import threading
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from PIL import Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_DEFAULT_TIF_PATH = os.environ.get(
    "SATELLITE_TIF_PATH",
    "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/河南2026年高分第一季度影像.tif",
)
_TILE_SIZE = 256
_CACHE_SIZE = 512           # 每个数据源 LRU 缓存最多 512 张瓦片 (~50MB)
_REGISTRY_PATH = os.environ.get(
    "SATELLITE_REGISTRY_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "satellite_sources.json",
    ),
)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# 数据源注册表：key -> {"path": str, "label": str, "dataset": Optional[Dataset], "cache": Callable}
_SOURCES: Dict[str, Dict] = {}
_LOCK = threading.RLock()


def _sanitize_key(key: str) -> str:
    key = (key or "").strip()
    if not key or not _KEY_PATTERN.match(key):
        raise ValueError(f"非法的数据源 key: {key!r}（仅允许字母/数字/_/-）")
    return key


# ---------------------------------------------------------------------------
# 注册表持久化
# ---------------------------------------------------------------------------
def _load_registry() -> Dict[str, dict]:
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("satellite registry load failed: %s", e)
        return {}


def _save_registry() -> None:
    try:
        os.makedirs(os.path.dirname(_REGISTRY_PATH), exist_ok=True)
        with open(_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {k: {"path": v["path"], "label": v.get("label", "")} for k, v in _SOURCES.items()},
                f, ensure_ascii=False, indent=2,
            )
    except Exception as e:
        logger.warning("satellite registry save failed: %s", e)


# ---------------------------------------------------------------------------
# 数据源管理
# ---------------------------------------------------------------------------
def _make_source_entry(tif_path: str, label: str = "") -> Dict:
    entry: Dict = {"path": tif_path, "label": label, "dataset": None}

    @lru_cache(maxsize=_CACHE_SIZE)
    def _cached(z: int, x: int, y: int) -> Optional[bytes]:
        return _render_tile(z, x, y, entry)

    entry["cache"] = _cached
    return entry


def register_source(key: str, tif_path: str, label: str = "") -> Dict:
    """注册（或替换）一个 GeoTIFF 数据源。返回源信息 dict。"""
    key = _sanitize_key(key)
    tif_path = (tif_path or "").strip()
    if not os.path.isfile(tif_path):
        raise FileNotFoundError(f"GeoTIFF 文件不存在: {tif_path}")
    with _LOCK:
        old = _SOURCES.get(key)
        if old and old.get("dataset") is not None:
            try:
                old["dataset"].close()
            except Exception:
                pass
        _SOURCES[key] = _make_source_entry(tif_path, label)
        _save_registry()
    logger.info("satellite source registered: %s -> %s", key, tif_path)
    return {"key": key, "path": tif_path, "label": label}


def unregister_source(key: str) -> bool:
    """注销数据源。key 为 "default" 时不允许注销。返回是否成功。"""
    key = _sanitize_key(key)
    with _LOCK:
        if key == "default":
            raise ValueError("内置默认数据源不允许注销（可重新注册覆盖）")
        entry = _SOURCES.pop(key, None)
        if entry is None:
            return False
        if entry.get("dataset") is not None:
            try:
                entry["dataset"].close()
            except Exception:
                pass
        _save_registry()
    logger.info("satellite source unregistered: %s", key)
    return True


def list_sources() -> List[Dict]:
    """列出所有已注册数据源。"""
    with _LOCK:
        return [
            {
                "key": k,
                "path": v["path"],
                "label": v.get("label", ""),
                "exists": os.path.isfile(v["path"]),
                "builtin": k == "default",
            }
            for k, v in sorted(_SOURCES.items())
        ]


def _get_source(key: str) -> Dict:
    with _LOCK:
        entry = _SOURCES.get(key)
        if entry is None:
            raise KeyError(f"未注册的数据源: {key}")
        return entry


def _get_dataset(entry: Dict):
    """懒加载 rasterio Dataset，线程安全"""
    ds = entry.get("dataset")
    if ds is None:
        with _LOCK:
            ds = entry.get("dataset")
            if ds is None:
                ds = rasterio.open(entry["path"])
                entry["dataset"] = ds
                logger.info("satellite tile: opened %s (%dx%d, crs=%s)",
                            entry["path"], ds.width, ds.height, ds.crs)
    return ds


# ---------------------------------------------------------------------------
# Web Mercator 瓦片数学
# ---------------------------------------------------------------------------
def tile_bounds_3857(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """返回 XYZ 瓦片的 EPSG:3857 bounds (minx, miny, maxx, maxy)"""
    n = 2 ** z
    origin = 20037508.342789244
    tile_size = 2 * origin / n
    minx = -origin + x * tile_size
    maxx = minx + tile_size
    maxy = origin - y * tile_size
    miny = maxy - tile_size
    return minx, miny, maxx, maxy


def get_tile_bounds_4326(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """返回 XYZ 瓦片的 EPSG:4326 bounds (west, south, east, north)"""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    lat_rad_n = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat_rad_s = math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n)))
    north = math.degrees(lat_rad_n)
    south = math.degrees(lat_rad_s)
    return west, south, east, north


def lng_lat_to_tile(lng: float, lat: float, z: int) -> Tuple[int, int]:
    """经纬度 → XYZ 瓦片号"""
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tiles_in_bounds(west: float, south: float, east: float, north: float, z: int):
    """返回覆盖 bounds 的所有瓦片 (z, x, y) 列表"""
    x_min, y_min = lng_lat_to_tile(west, north, z)   # north → 小 y
    x_max, y_max = lng_lat_to_tile(east, south, z)    # south → 大 y
    tiles = []
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            tiles.append((z, tx, ty))
    return tiles


# ---------------------------------------------------------------------------
# 瓦片渲染
# ---------------------------------------------------------------------------
def _render_tile(z: int, x: int, y: int, entry: Dict) -> Optional[bytes]:
    """从数据源 GeoTIFF 读取瓦片范围的像素并输出为 256×256 PNG"""
    ds = _get_dataset(entry)
    src_crs = ds.crs

    # 瓦片地理范围 (4326)
    west, south, east, north = get_tile_bounds_4326(z, x, y)

    # 转换到 TIF 的坐标系（用 rasterio/GDAL transform，避免 pyproj proj.db 版本冲突）
    if src_crs and str(src_crs) != "EPSG:4326":
        from rasterio.warp import transform
        t_west, t_south = transform("EPSG:4326", src_crs, [west], [south], )
        t_east, t_north = transform("EPSG:4326", src_crs, [east], [north], )
        t_west, t_south, t_east, t_north = t_west[0], t_south[0], t_east[0], t_north[0]
    else:
        t_west, t_south, t_east, t_north = west, south, east, north

    # 检查是否在 TIF 范围内
    tb = ds.bounds
    if t_east <= tb.left or t_west >= tb.right or t_north <= tb.bottom or t_south >= tb.top:
        return None

    # 裁剪到 TIF 范围
    t_west = max(t_west, tb.left)
    t_south = max(t_south, tb.bottom)
    t_east = min(t_east, tb.right)
    t_north = min(t_north, tb.top)

    try:
        window = from_bounds(t_west, t_south, t_east, t_north, ds.transform)
        # 直接以目标分辨率读取（rasterio 自动选最近的 overview）
        data = ds.read(
            [1, 2, 3],
            window=window,
            out_shape=(3, _TILE_SIZE, _TILE_SIZE),
            resampling=rasterio.enums.Resampling.bilinear,
        )
    except Exception as e:
        logger.debug("satellite tile read error z=%d x=%d y=%d: %s", z, x, y, e)
        return None

    # (3, 256, 256) → (256, 256, 3)
    rgb = np.transpose(data, (1, 2, 0))

    # 检查是否全空
    if rgb.max() == 0:
        return None

    img = Image.fromarray(rgb.astype(np.uint8), "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def get_satellite_tile(z: int, x: int, y: int, key: str = "default") -> Optional[bytes]:
    """公开接口：获取指定数据源的瓦片 PNG bytes，不存在返回 None"""
    if z < 0 or z > 22:
        return None
    try:
        entry = _get_source(key)
    except KeyError:
        return None
    return entry["cache"](z, x, y)


def get_satellite_tile_base64(z: int, x: int, y: int, key: str = "default") -> Optional[str]:
    """获取瓦片的 base64 编码（供 Ollama 直接使用）"""
    import base64
    png = get_satellite_tile(z, x, y, key=key)
    if png is None:
        return None
    return base64.b64encode(png).decode("utf-8")


# ---------------------------------------------------------------------------
# 启动时恢复注册表 + 默认数据源（向后兼容）
# ---------------------------------------------------------------------------
def _init_sources() -> None:
    # 1) 从 JSON 注册表恢复（含 default 的历史路径）
    for key, meta in _load_registry().items():
        if not isinstance(meta, dict):
            continue
        path = (meta.get("path") or "").strip()
        if not path or not os.path.isfile(path):
            logger.warning("satellite source skip missing file: %s -> %s", key, path)
            continue
        try:
            _SOURCES[key] = _make_source_entry(path, meta.get("label", ""))
        except Exception as e:
            logger.warning("satellite source init failed %s: %s", key, e)
    # 2) 保证默认源存在（无注册表或文件缺失时回退 env/内置路径）
    if "default" not in _SOURCES:
        default_path = _DEFAULT_TIF_PATH
        if os.path.isfile(default_path):
            _SOURCES["default"] = _make_source_entry(default_path, "默认遥感影像")
        else:
            logger.warning("satellite default source file missing: %s", default_path)
    logger.info("satellite sources ready: %s", list(_SOURCES.keys()))


_init_sources()
