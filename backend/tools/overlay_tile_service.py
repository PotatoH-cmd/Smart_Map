"""
Overlay Tile Service
====================
按需将 GeoJSON 渲染为 XYZ PNG 栅格瓦片（Web Mercator / EPSG:3857）。
特点：
- 启动时一次性加载 GeoJSON 到内存，构建 STRtree 空间索引；
- 每个瓦片请求只渲染落在 tile bbox 内的要素；
- 进程内 LRU 缓存最近瓦片，避免重复绘制。

兼容 Cesium UrlTemplateImageryProvider：URL 形如 /api/overlay_tile/{layer}/{z}/{x}/{y}.png
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import shape, box, Point
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTML description 解析（采区 GeoJSON 的属性嵌在 HTML 表格中）
# ---------------------------------------------------------------------------

class _TableExtractor(HTMLParser):
    """从简单 HTML 表格中提取 td 文本。"""
    def __init__(self):
        super().__init__()
        self._in_td = False
        self._cells: List[str] = []
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self._in_td = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._cells.append(self._buf.strip())
            self._in_td = False

    def handle_data(self, data):
        if self._in_td:
            self._buf += data


def _parse_html_description(html_str: str) -> Dict[str, str]:
    """从 KML 风格的 HTML description 中提取键值对。"""
    if not html_str or "<" not in html_str:
        return {}
    try:
        parser = _TableExtractor()
        parser.feed(html_str)
        cells = parser._cells
        # 内层表格的 cells 是成对的 key/value
        # 跳过第一个 cell（通常是标题行里的 Name 值）
        # 找第一对像 key/value 的位置
        props: Dict[str, str] = {}
        skip_keys = {"SHAPE", "面", "线", "点"}
        i = 0
        while i < len(cells) - 1:
            k = cells[i].strip()
            v = cells[i + 1].strip()
            if k and k not in skip_keys and not k.startswith("<"):
                props[k] = v
                i += 2
            else:
                i += 1
        return props
    except Exception:
        return {}


# Web Mercator 常量
_MERC_HALF = 20037508.342789244

# WGS84 -> Web Mercator
_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform

TILE_SIZE = 256


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

class OverlayLayer:
    def __init__(self, key: str, path: str, style: Dict):
        self.key = key
        self.path = path
        self.style = style
        self._loaded = False
        self._lock = threading.Lock()
        self.tree: Optional[STRtree] = None
        self.geoms: List = []
        self.geom_types: List[str] = []
        self.properties: List[Dict[str, Any]] = []

    def load(self):
        with self._lock:
            if self._loaded:
                return
            if not os.path.isfile(self.path):
                logger.warning("[overlay_tile] 文件不存在: %s", self.path)
                self._loaded = True
                return
            logger.info("[overlay_tile] 加载 %s -> %s", self.key, self.path)
            with open(self.path, "r", encoding="utf-8") as f:
                gj = json.load(f)
            features = gj.get("features", []) if isinstance(gj, dict) else []
            for feat in features:
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = shape(geom)
                    if g.is_empty:
                        continue
                    g = shp_transform(_to_3857, g)
                    self.geoms.append(g)
                    self.geom_types.append(g.geom_type)
                    raw_props = feat.get("properties") or {}
                    # 采区等 KML 转出的 GeoJSON: 真实属性藏在 description HTML 里
                    desc_html = raw_props.get("description", "")
                    if isinstance(desc_html, str) and "<table" in desc_html.lower():
                        parsed = _parse_html_description(desc_html)
                        if parsed:
                            clean = dict(parsed)
                            # 保留 Name 如果有意义
                            raw_name = raw_props.get("Name", "")
                            if raw_name and not re.match(r"^[\d.eE\-+]+$", raw_name):
                                clean.setdefault("名称", raw_name)
                            raw_props = clean
                        else:
                            # 去掉 HTML 字段和无用 KML 字段
                            raw_props = {k: v for k, v in raw_props.items()
                                         if k not in ("description", "timestamp", "begin", "end",
                                                       "altitudeMode", "tessellate", "extrude",
                                                       "visibility", "drawOrder", "icon")
                                         and v is not None}
                    self.properties.append(raw_props)
                except Exception as e:
                    logger.debug("[overlay_tile] 跳过要素: %s", e)
            if self.geoms:
                self.tree = STRtree(self.geoms)
            self._loaded = True
            logger.info(
                "[overlay_tile] %s 加载完成: %d 个要素", self.key, len(self.geoms)
            )

    def query(self, minx: float, miny: float, maxx: float, maxy: float) -> List:
        self.load()
        if not self.tree:
            return []
        bbox_geom = box(minx, miny, maxx, maxy)
        # shapely 2.x: STRtree.query 返回索引数组
        idxs = self.tree.query(bbox_geom)
        return [(self.geoms[i], self.geom_types[i]) for i in idxs]

    def query_feature(self, lng: float, lat: float, tolerance_m: float = 120.0) -> Optional[Dict[str, Any]]:
        self.load()
        if not self.tree:
            return None
        px, py = _to_3857(lng, lat)
        point = Point(px, py)
        tol = max(1.0, float(tolerance_m))
        idxs = self.tree.query(box(px - tol, py - tol, px + tol, py + tol))
        best = None
        best_dist = None
        for i in idxs:
            geom = self.geoms[i]
            dist = geom.distance(point)
            if dist <= tol and (best_dist is None or dist < best_dist):
                best = i
                best_dist = dist
        if best is None:
            return None
        return {
            "layer": self.key,
            "geometry_type": self.geom_types[best],
            "properties": self.properties[best],
            "distance_m": best_dist,
        }


# ---------------------------------------------------------------------------
# Tile 数学
# ---------------------------------------------------------------------------

def tile_bounds_3857(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """返回 (minx, miny, maxx, maxy) Web Mercator 米"""
    n = 2 ** z
    tile_size = (2 * _MERC_HALF) / n
    minx = -_MERC_HALF + x * tile_size
    maxx = -_MERC_HALF + (x + 1) * tile_size
    maxy = _MERC_HALF - y * tile_size
    miny = _MERC_HALF - (y + 1) * tile_size
    return minx, miny, maxx, maxy


def world_to_pixel(
    px_origin_x: float, px_origin_y: float, scale: float, x: float, y: float
) -> Tuple[float, float]:
    return (x - px_origin_x) * scale, (px_origin_y - y) * scale


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

_EMPTY_PNG: Optional[bytes] = None


def _empty_tile_png() -> bytes:
    global _EMPTY_PNG
    if _EMPTY_PNG is None:
        img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _EMPTY_PNG = buf.getvalue()
    return _EMPTY_PNG


def _hex_to_rgba(hex_color: str, alpha: int) -> Tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return (r, g, b, alpha)


def _line_width(z: int, base: float) -> int:
    # 远视图细一点，近视图粗一点
    if z <= 6:
        return 1
    if z <= 10:
        return max(1, int(base - 1))
    return max(1, int(base))


def _draw_geom(draw: ImageDraw.ImageDraw, geom_type: str, geom, project_xy, style, z):
    stroke_rgba = _hex_to_rgba(style["stroke"], 230)
    fill_rgba = _hex_to_rgba(
        style.get("fill", style["stroke"]),
        int(255 * float(style.get("fillAlpha", 0.18))),
    )
    base_w = float(style.get("strokeWidth", 2))
    line_w = _line_width(z, base_w)

    def to_px(coords):
        # 兼容 2D / 3D / M 坐标
        return [project_xy(c[0], c[1]) for c in coords]

    if geom_type == "Point":
        x, y = project_xy(geom.x, geom.y)
        r = max(1, line_w + 1)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=stroke_rgba)
    elif geom_type == "MultiPoint":
        for p in geom.geoms:
            x, y = project_xy(p.x, p.y)
            r = max(1, line_w + 1)
            draw.ellipse((x - r, y - r, x + r, y + r), fill=stroke_rgba)
    elif geom_type == "LineString":
        pts = to_px(geom.coords)
        if len(pts) >= 2:
            draw.line(pts, fill=stroke_rgba, width=line_w, joint="curve")
    elif geom_type == "MultiLineString":
        for line in geom.geoms:
            pts = to_px(line.coords)
            if len(pts) >= 2:
                draw.line(pts, fill=stroke_rgba, width=line_w, joint="curve")
    elif geom_type == "Polygon":
        ext = to_px(geom.exterior.coords)
        if len(ext) >= 3:
            draw.polygon(ext, fill=fill_rgba, outline=stroke_rgba)
            # outline 宽度增强
            if line_w > 1:
                draw.line(ext + [ext[0]], fill=stroke_rgba, width=line_w)
    elif geom_type == "MultiPolygon":
        for poly in geom.geoms:
            ext = to_px(poly.exterior.coords)
            if len(ext) >= 3:
                draw.polygon(ext, fill=fill_rgba, outline=stroke_rgba)
                if line_w > 1:
                    draw.line(ext + [ext[0]], fill=stroke_rgba, width=line_w)
    elif geom_type == "GeometryCollection":
        for sub in geom.geoms:
            _draw_geom(draw, sub.geom_type, sub, project_xy, style, z)


def render_tile(layer: OverlayLayer, z: int, x: int, y: int) -> bytes:
    minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)
    candidates = layer.query(minx, miny, maxx, maxy)
    if not candidates:
        return _empty_tile_png()

    img = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    scale = TILE_SIZE / (maxx - minx)

    def project_xy(gx: float, gy: float) -> Tuple[float, float]:
        return ((gx - minx) * scale, (maxy - gy) * scale)

    for geom, gtype in candidates:
        _draw_geom(draw, gtype, geom, project_xy, layer.style, z)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 全局注册
# ---------------------------------------------------------------------------

_LAYERS: Dict[str, OverlayLayer] = {}


def register_layer(key: str, path: str, style: Dict):
    _LAYERS[key] = OverlayLayer(key, path, style)


def unregister_layer(key: str) -> bool:
    """从内存中移除图层注册，并清除其瓦片缓存。"""
    if key not in _LAYERS:
        return False
    del _LAYERS[key]
    # 清除该图层在 LRU 缓存中的瓦片
    try:
        _cached_tile.cache_clear()
    except Exception:
        pass
    return True


def get_layer(key: str) -> Optional[OverlayLayer]:
    return _LAYERS.get(key)


# 注册默认图层
_FRONTEND_DATA_DIR = os.environ.get(
    "MAP_OVERLAY_DATA_DIR",
    "/home/server/python/map_assistant_v1/frontend/public/data",
)

register_layer(
    "hx",
    os.path.join(_FRONTEND_DATA_DIR, "hx.geojson"),
    {"stroke": "#ef4444", "fill": "#ef4444", "fillAlpha": 0.05, "strokeWidth": 3},
)
register_layer(
    "caiqu",
    os.path.join(_FRONTEND_DATA_DIR, "caiqu.geojson"),
    {"stroke": "#38bdf8", "fill": "#38bdf8", "fillAlpha": 0.18, "strokeWidth": 2},
)


# ---------------------------------------------------------------------------
# 缓存：基于 (key, z, x, y)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def _cached_tile(key: str, z: int, x: int, y: int) -> bytes:
    layer = get_layer(key)
    if not layer:
        return _empty_tile_png()
    return render_tile(layer, z, x, y)


def get_tile_png(key: str, z: int, x: int, y: int) -> bytes:
    return _cached_tile(key, z, x, y)


def query_feature(key: str, lng: float, lat: float, tolerance_m: float = 120.0) -> Optional[Dict[str, Any]]:
    layer = get_layer(key)
    if not layer:
        return None
    return layer.query_feature(lng, lat, tolerance_m)


def list_layers() -> List[str]:
    return list(_LAYERS.keys())
