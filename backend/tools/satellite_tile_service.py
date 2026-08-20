"""
Satellite Tile Service
======================
从本地 GeoTIFF 按需生成 XYZ 栅格瓦片（Web Mercator / EPSG:3857 → PNG 256×256）。
利用 rasterio 的窗口读取 + COG 式 overview，实现高效按需切片。
无需预生成 MBTiles，启动即服务。

用法:
    from tools.satellite_tile_service import get_satellite_tile, get_tile_bounds_4326
    png_bytes = get_satellite_tile(z, x, y)       # → bytes (PNG) 或 None
    bounds     = get_tile_bounds_4326(z, x, y)     # → (west, south, east, north)
"""

from __future__ import annotations

import io
import math
import logging
import os
import threading
from functools import lru_cache
from typing import Optional, Tuple

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import transform_bounds
from PIL import Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_TIF_PATH = os.environ.get(
    "SATELLITE_TIF_PATH",
    "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/河南2026年高分第一季度影像.tif",
)
_TILE_SIZE = 256
_CACHE_SIZE = 512           # LRU 缓存最多 512 张瓦片 (~50MB)
_dataset = None
_ds_lock = threading.Lock()


def _get_dataset():
    """懒加载 rasterio Dataset，线程安全"""
    global _dataset
    if _dataset is None:
        with _ds_lock:
            if _dataset is None:
                _dataset = rasterio.open(_TIF_PATH)
                logger.info("satellite tile: opened %s (%dx%d, crs=%s)",
                            _TIF_PATH, _dataset.width, _dataset.height, _dataset.crs)
    return _dataset


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
@lru_cache(maxsize=_CACHE_SIZE)
def _cached_tile(z: int, x: int, y: int) -> Optional[bytes]:
    return _render_tile(z, x, y)


def _render_tile(z: int, x: int, y: int) -> Optional[bytes]:
    """从 GeoTIFF 读取瓦片范围的像素并输出为 256×256 PNG"""
    ds = _get_dataset()
    src_crs = ds.crs

    # 瓦片地理范围 (4326)
    west, south, east, north = get_tile_bounds_4326(z, x, y)

    # 转换到 TIF 的坐标系
    if src_crs and str(src_crs) != "EPSG:4326":
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True)
        t_west, t_south = tr.transform(west, south)
        t_east, t_north = tr.transform(east, north)
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


def get_satellite_tile(z: int, x: int, y: int) -> Optional[bytes]:
    """公开接口：获取瓦片 PNG bytes，不存在返回 None"""
    if z < 0 or z > 22:
        return None
    return _cached_tile(z, x, y)


def get_satellite_tile_base64(z: int, x: int, y: int) -> Optional[str]:
    """获取瓦片的 base64 编码（供 Ollama 直接使用）"""
    import base64
    png = get_satellite_tile(z, x, y)
    if png is None:
        return None
    return base64.b64encode(png).decode("utf-8")
