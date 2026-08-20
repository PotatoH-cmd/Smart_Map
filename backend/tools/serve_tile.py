#!/usr/bin/env python3
"""
独立瓦片 HTTP 服务 —— 从大型 GeoTIFF 按需渲染 XYZ 栅格瓦片（PNG 256×256）

用法:
    python serve_tile.py <tif_path> [选项]

选项:
    --port PORT         HTTP 端口 (默认: 8080)
    --host HOST         绑定地址 (默认: 0.0.0.0)
    --cache-size N      LRU 缓存瓦片数 (默认: 512, 约50MB)
    --tile-size N       瓦片像素 (默认: 256)
    --max-zoom N        最大缩放级别 (默认: 18)

前端使用 (Leaflet):
    L.tileLayer('http://localhost:8080/tiles/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '2026年Q1高分影像'
    }).addTo(map);

特性:
  - 启动即服务，无需预生成 MBTiles
  - 线程安全的 LRU 内存缓存
  - 自动处理 CGCS2000 (EPSG:4490) ↔ WGS84 (EPSG:4326) 坐标转换
  - 利用 GeoTIFF 内部金字塔加速高缩放级别访问
  - CORS 支持
  - 健康检查端点 /health
  - 元数据端点 /metadata
"""

import sys
import os
import io
import math
import json
import threading
import logging
import argparse
import time
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple, Dict, Any

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from PIL import Image

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('serve_tile')

# ---------------------------------------------------------------------------
# 全局变量（线程安全初始化）
# ---------------------------------------------------------------------------
TIF_PATH = ""
TILE_SIZE = 256
MAX_ZOOM = 18
CACHE_SIZE = 512

_dataset: Optional[rasterio.io.DatasetReader] = None
_ds_lock = threading.Lock()
_overview_factors: list = []
_src_crs = None
_ds_bounds: Optional[Tuple[float, float, float, float]] = None
_stats: Dict[str, Any] = {
    "requests": 0, "hits": 0, "misses": 0, "errors": 0,
    "start_time": time.time(),
}

# 坐标转换器缓存 (EPSG:4326 → 源CRS)
_transformer = None


def get_dataset() -> rasterio.io.DatasetReader:
    """线程安全的懒加载 rasterio Dataset（利用已有内部金字塔）"""
    global _dataset, _overview_factors, _src_crs, _ds_bounds, _transformer

    if _dataset is None:
        with _ds_lock:
            if _dataset is None:
                if not TIF_PATH or not os.path.isfile(TIF_PATH):
                    raise FileNotFoundError(f"TIF 文件不存在: {TIF_PATH}")

                logger.info(f"⏳ 正在打开 TIF: {TIF_PATH}")
                t0 = time.time()
                _dataset = rasterio.open(TIF_PATH)
                _src_crs = _dataset.crs
                _ds_bounds = (
                    _dataset.bounds.left,
                    _dataset.bounds.bottom,
                    _dataset.bounds.right,
                    _dataset.bounds.top,
                )

                # 收集 overview 缩放因子
                _overview_factors = []
                for i in range(_dataset.count):
                    ovs = _dataset.overviews(i + 1)
                    if ovs:
                        _overview_factors = list(ovs)
                        break

                # 预建坐标转换器
                if _src_crs and str(_src_crs) != "EPSG:4326":
                    from pyproj import Transformer
                    _transformer = Transformer.from_crs(
                        "EPSG:4326", _src_crs, always_xy=True
                    )

                elapsed = time.time() - t0
                logger.info(
                    f"✅ 已打开: {_dataset.width}×{_dataset.height} px, "
                    f"波段={_dataset.count}, "
                    f"CRS={_src_crs}, "
                    f"数据类型={_dataset.dtypes[0]}, "
                    f"范围={_ds_bounds}, "
                    f"金字塔={_overview_factors} ({len(_overview_factors)}级), "
                    f"耗时 {elapsed:.1f}s"
                )

    return _dataset


# ---------------------------------------------------------------------------
# XYZ 瓦片数学 (EPSG:4326)
# ---------------------------------------------------------------------------
def tile_bounds_4326(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """返回 XYZ 瓦片的 EPSG:4326 范围 (west, south, east, north)"""
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
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


# ---------------------------------------------------------------------------
# 瓦片渲染核心
# ---------------------------------------------------------------------------
def _render_tile_core(z: int, x: int, y: int) -> Optional[bytes]:
    """从 GeoTIFF 读取瓦片范围的像素并输出为 PNG bytes。

    优化策略:
      1. 利用 GeoTIFF 内置金字塔（overviews）：高缩放级别时自动选取合适的 overview
      2. 窗口读取：只读取瓦片覆盖区域
      3. 提前裁剪到 TIF 范围，避免无效数据读取
    """
    ds = get_dataset()

    # 1. 计算瓦片在 EPSG:4326 下的地理范围
    west, south, east, north = tile_bounds_4326(z, x, y)

    # 2. 转换到 TIF 的源坐标系（如 EPSG:4490 CGCS2000）
    if _transformer is not None:
        t_west, t_south = _transformer.transform(west, south)
        t_east, t_north = _transformer.transform(east, north)
    else:
        t_west, t_south, t_east, t_north = west, south, east, north

    # 3. 检查是否与 TIF 范围有交集
    bl, bb, br, bt = _ds_bounds
    if t_east <= bl or t_west >= br or t_north <= bb or t_south >= bt:
        return None  # 瓦片完全在影像范围外

    # 4. 裁剪到 TIF 范围
    t_west = max(t_west, bl)
    t_south = max(t_south, bb)
    t_east = min(t_east, br)
    t_north = min(t_north, bt)

    # 5. 窗口读取（rasterio 自动选择最合适的 overview）
    try:
        window = from_bounds(t_west, t_south, t_east, t_north, ds.transform)
        data = ds.read(
            [1, 2, 3],
            window=window,
            out_shape=(3, TILE_SIZE, TILE_SIZE),
            resampling=rasterio.enums.Resampling.bilinear,
        )
    except Exception as e:
        logger.debug(f"瓦片读取失败 z={z} x={x} y={y}: {e}")
        return None

    # 6. 检查是否全空（黑色/NoData 区域）
    if data.max() == 0:
        return None

    # 7. (3, 256, 256) → (256, 256, 3) → PNG
    rgb = np.transpose(data, (1, 2, 0))

    # 数据类型归一化：确保 uint8
    if rgb.dtype != np.uint8:
        if rgb.dtype in (np.uint16, np.int16):
            rgb = (rgb / 256).astype(np.uint8)
        elif rgb.dtype in (np.float32, np.float64):
            rgb = np.clip(rgb * 255, 0, 255).astype(np.uint8)
        else:
            rgb = rgb.astype(np.uint8)

    img = Image.fromarray(rgb, "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ── 手动 LRU 缓存（线程安全，大小可动态调整）──
_tile_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_tile_cache_maxsize = 512


def _get_from_cache(key: Tuple[int, int, int]) -> Optional[bytes]:
    with _cache_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
            _stats["hits"] += 1
            return _tile_cache[key]
    return None


def _put_to_cache(key: Tuple[int, int, int], value: bytes):
    with _cache_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
        else:
            _tile_cache[key] = value
            while len(_tile_cache) > _tile_cache_maxsize:
                _tile_cache.popitem(last=False)


def _clear_cache():
    with _cache_lock:
        _tile_cache.clear()


def get_tile_png(z: int, x: int, y: int) -> Optional[bytes]:
    """公开接口：获取瓦片 PNG bytes，不存在返回 None"""
    global _tile_cache_maxsize
    _stats["requests"] += 1
    if z < 0 or z > MAX_ZOOM:
        return None
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        return None

    key = (z, x, y)

    # 先查缓存
    cached = _get_from_cache(key)
    if cached is not None:
        return cached

    # 缓存未命中 → 渲染
    _stats["misses"] += 1
    try:
        result = _render_tile_core(z, x, y)
    except Exception as e:
        logger.error(f"渲染异常 z={z} x={x} y={y}: {e}")
        _stats["errors"] += 1
        return None

    if result is not None:
        _put_to_cache(key, result)
    return result


def set_cache_size(size: int):
    """动态调整 LRU 缓存大小"""
    global _tile_cache_maxsize
    _tile_cache_maxsize = size
    _clear_cache()


# ---------------------------------------------------------------------------
# HTTP 请求处理器
# ---------------------------------------------------------------------------
class TileRequestHandler(BaseHTTPRequestHandler):
    """处理瓦片请求的 HTTP Handler"""

    # 禁用 DNS 反查，加速响应
    # 注意：BaseHTTPRequestHandler 默认会做 DNS lookup

    def log_message(self, format, *args):
        """重写日志格式，减少噪音"""
        if args[0].startswith("GET /health"):
            return  # 健康检查不打印日志
        if self.path.startswith("/tiles/") or self.path.startswith("/api/satellite_tile"):
            # 只在 4xx/5xx 时打印
            if args[1] in ("200", "204", "304"):
                return
        logger.info(f"{self.client_address[0]} - {format % args}")

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _send_error_response(self, code: int, message: str):
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def do_OPTIONS(self):
        """CORS 预检请求"""
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]  # 去除 query string

        # ── 健康检查 ──
        if path == "/health":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            uptime = time.time() - _stats["start_time"]
            self.wfile.write(json.dumps({
                "status": "ok",
                "uptime_seconds": round(uptime, 1),
                "tif_path": TIF_PATH,
                "cache": {
                    "hits": _stats.get("hits", 0),
                    "misses": _stats.get("misses", 0),
                    "currsize": len(_tile_cache),
                    "maxsize": _tile_cache_maxsize,
                },
                "stats": _stats,
            }).encode())
            return

        # ── 元数据 ──
        if path == "/metadata":
            try:
                ds = get_dataset()
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "width": ds.width,
                    "height": ds.height,
                    "bands": ds.count,
                    "crs": str(ds.crs),
                    "bounds": {
                        "left": ds.bounds.left,
                        "bottom": ds.bounds.bottom,
                        "right": ds.bounds.right,
                        "top": ds.bounds.top,
                    },
                    "dtype": str(ds.dtypes[0]),
                    "overviews": _overview_factors,
                    "tile_size": TILE_SIZE,
                    "max_zoom": MAX_ZOOM,
                }).encode())
                return
            except Exception as e:
                self._send_error_response(503, str(e))
                return

        # ── 瓦片请求 /tiles/{z}/{x}/{y}.png ──
        # 也兼容 /api/satellite_tile/{z}/{x}/{y}.png
        tile_match = None
        for prefix in ("/tiles/", "/api/satellite_tile/"):
            if path.startswith(prefix):
                rest = path[len(prefix):]
                if rest.endswith(".png"):
                    rest = rest[:-4]
                parts = rest.strip("/").split("/")
                if len(parts) == 3:
                    try:
                        z, x, y = int(parts[0]), int(parts[1]), int(parts[2])
                        tile_match = (z, x, y)
                    except ValueError:
                        pass
                break

        if tile_match:
            z, x, y = tile_match
            try:
                png = get_tile_png(z, x, y)
            except FileNotFoundError as e:
                self._send_error_response(503, f"TIF 未就绪: {e}")
                return
            except Exception as e:
                logger.error(f"瓦片处理异常 z={z} x={x} y={y}: {e}")
                self._send_error_response(500, "瓦片渲染失败")
                return

            if png is None:
                # 204 No Content: 表示该区域无影像数据
                self.send_response(204)
                self._send_cors_headers()
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                return

            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(png)
            return

        # ── 首页 / ──
        if path == "/" or path == "":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>高分影像瓦片服务</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  body{{margin:0;padding:0;}} #map{{width:100vw;height:100vh;}}
  .info{{position:absolute;top:10px;right:10px;z-index:1000;
         background:rgba(0,0,0,.7);color:white;padding:8px 14px;
         border-radius:6px;font-size:13px;font-family:monospace;}}
</style>
</head>
<body>
<div id="map"></div>
<div class="info" id="info">加载中…</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var map = L.map('map').setView([33.88, 113.5], 7);
L.tileLayer('/tiles/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: {MAX_ZOOM},
    attribution: '2026年Q1河南省高分影像 (亚米级)',
    errorTileUrl: ''
}}).addTo(map);
fetch('/metadata').then(r=>r.json()).then(m=>{{
  document.getElementById('info').innerHTML =
    '🗺 尺寸: ' + (m.width/10000).toFixed(0) + '万×' + (m.height/10000).toFixed(0) + '万 px' +
    '<br>📍 CRS: ' + m.crs +
    '<br>🔍 金字塔: ' + (m.overviews||[]).length + ' 级' +
    '<br>🏷 2026年Q1河南省高分影像 (亚米级)';
}});
</script>
</body>
</html>""")
            return

        # ── 404 ──
        self._send_error_response(404, f"未知路径: {path}")


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="从大型 GeoTIFF 按需渲染 XYZ 瓦片的独立 HTTP 服务",
        epilog="示例: python serve_tile.py /path/to/image.tif --port 8080",
    )
    parser.add_argument(
        "tif_path",
        nargs="?",
        default=os.environ.get(
            "SATELLITE_TIF_PATH",
            "",
        ),
        help="GeoTIFF 文件路径 (也可通过环境变量 SATELLITE_TIF_PATH 设置)",
    )
    parser.add_argument("--port", type=int, default=8080, help="HTTP 端口 (默认: 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认: 0.0.0.0)")
    parser.add_argument("--cache-size", type=int, default=512, help="LRU 缓存瓦片数 (默认: 512)")
    parser.add_argument("--tile-size", type=int, default=256, help="瓦片像素 (默认: 256)")
    parser.add_argument("--max-zoom", type=int, default=18, help="最大缩放级别 (默认: 18)")
    return parser.parse_args()


def main():
    global TIF_PATH, TILE_SIZE, MAX_ZOOM, CACHE_SIZE

    args = parse_args()

    if not args.tif_path:
        print("错误: 必须指定 TIF 文件路径", file=sys.stderr)
        print("用法: python serve_tile.py <tif_path> [--port 8080]", file=sys.stderr)
        sys.exit(1)

    TIF_PATH = os.path.abspath(args.tif_path)
    TILE_SIZE = args.tile_size
    MAX_ZOOM = args.max_zoom
    CACHE_SIZE = args.cache_size

    if not os.path.isfile(TIF_PATH):
        print(f"错误: TIF 文件不存在: {TIF_PATH}", file=sys.stderr)
        sys.exit(1)

    # 预加载 dataset（验证 TIF 可读）
    logger.info(f"=" * 60)
    logger.info(f"🌐 高分影像瓦片服务启动")
    logger.info(f"   文件: {TIF_PATH} ({os.path.getsize(TIF_PATH)/1024**3:.1f} GB)")
    logger.info(f"   端口: {args.port}")
    logger.info(f"   缓存: {CACHE_SIZE} 张瓦片 (~{CACHE_SIZE * TILE_SIZE * TILE_SIZE * 3 / 1024**2:.0f} MB)")
    logger.info(f"   瓦片: {TILE_SIZE}×{TILE_SIZE} px")
    logger.info(f"   级别: 0-{MAX_ZOOM}")
    logger.info(f"=" * 60)

    try:
        get_dataset()  # 预加载
    except Exception as e:
        logger.error(f"无法打开 TIF: {e}")
        sys.exit(1)

    # 设置缓存大小
    set_cache_size(CACHE_SIZE)

    # 启动 HTTP 服务器
    server = HTTPServer((args.host, args.port), TileRequestHandler)
    # 设置更短的超时，避免闲置连接占用线程
    server.timeout = 30

    logger.info(f"🚀 服务已启动: http://{args.host}:{args.port}")
    logger.info(f"   瓦片端点: http://localhost:{args.port}/tiles/{{z}}/{{x}}/{{y}}.png")
    logger.info(f"   预览页面: http://localhost:{args.port}/")
    logger.info(f"   健康检查: http://localhost:{args.port}/health")
    logger.info(f"   元数据:   http://localhost:{args.port}/metadata")
    logger.info(f"   按 Ctrl+C 停止服务")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("\n⏹ 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
