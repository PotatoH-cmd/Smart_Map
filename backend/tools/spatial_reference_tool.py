# tools/spatial_reference_tool.py
"""
空间参考数据工具 — 将 overlay_tile_service 中已加载的空间参考图层
（如河道红线 hx、采区边界 caiqu）暴露给 Agent，使其具备空间感知能力。

Agent 可通过本工具：
- 列出所有可用的空间参考图层
- 获取指定图层的完整几何数据（GeoJSON）
- 按 bbox 查询图层中相交的要素

典型场景：
- 用户提到"红线附近的采砂场" → Agent 先获取 hx 图层几何，再用 postgresql_tool(spatial_query) 筛选
- 用户说"采区范围内有什么" → Agent 获取 caiqu 图层几何，传入 map_tool 叠加显示
"""
import json
import logging
from typing import Dict, Any, List, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 坐标转换
# ---------------------------------------------------------------------------
from pyproj import Transformer
_to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
_to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform

# ---------------------------------------------------------------------------
# 图层描述信息（可扩展：新增空间参考时在此追加一条即可）
# ---------------------------------------------------------------------------
LAYER_META: Dict[str, Dict[str, str]] = {
    "hx": {
        "name": "河道管理红线",
        "description": "河道管理范围的法定边界，用于约束采砂、建筑等活动",
        "geom_type": "LineString/MultiLineString",
    },
    "caiqu": {
        "name": "2025年可采区边界",
        "description": "许可采砂区域的空间范围，凡采砂活动须在此范围内",
        "geom_type": "Polygon/MultiPolygon",
    },
}


def _load_overlay_layer(key: str):
    """延迟加载 overlay 图层，返回 OverlayLayer 实例或 None。"""
    try:
        from .overlay_tile_service import get_layer
        layer = get_layer(key)
        if layer is not None:
            layer.load()
        return layer
    except Exception as e:
        logger.warning(f"[spatial_ref] 加载图层 {key} 失败: {e}")
        return None


def _geometries_to_geojson(layer, simplify_tolerance: float = 0.0001) -> Dict:
    """
    将 OverlayLayer 中存储的 shapely 几何对象转换为 GeoJSON FeatureCollection。
    默认对几何做 simplify，减少返回给 LLM 的数据量。
    """
    if not layer or not layer.geoms:
        return {"type": "FeatureCollection", "features": []}

    features = []
    for i, geom in enumerate(layer.geoms):
        # simplify: 减少顶点数，tolerance≈0.0001（约 10m @ 赤道）
        if simplify_tolerance and simplify_tolerance > 0:
            try:
                simplified = geom.simplify(simplify_tolerance, preserve_topology=True)
            except Exception:
                simplified = geom
        else:
            simplified = geom

        # 从 Web Mercator (EPSG:3857) 转回 WGS84 (EPSG:4326)
        from shapely.ops import transform

        try:
            geom_4326 = transform(_to_4326, simplified)
        except Exception:
            geom_4326 = simplified

        from shapely.geometry import mapping
        feat = {
            "type": "Feature",
            "geometry": mapping(geom_4326),
            "properties": layer.properties[i] if i < len(layer.properties) else {},
        }
        features.append(feat)

    return {"type": "FeatureCollection", "features": features}


@register_tool('spatial_reference_tool')
class SpatialReferenceTool(BaseTool):
    """
    空间参考数据查询工具。
    用于获取河道红线、采区边界等空间参考图层的几何数据，
    以便在其他任务中作为空间约束使用。
    """

    description = (
        "空间参考数据查询工具。用于获取系统的空间参考图层数据。\n"
        "可用图层（通过 layer 参数指定）：\n"
        "- hx：河道管理红线（河道管理法定边界）\n"
        "- caiqu：可采区边界（许可采砂区域范围）\n\n"
        "支持操作：\n"
        "- list_layers：列出所有可用图层及其描述\n"
        "- get_geometry：获取指定图层的完整 GeoJSON 几何\n"
        "- query：按 bbox 查询图层中相交的要素\n\n"
        "重要：当用户提到'红线''河道红线''采区''可采区'时，必须使用本工具。"
    )

    parameters = [
        {
            'name': 'action',
            'type': 'string',
            'description': '操作类型：list_layers（列出图层）、get_geometry（获取几何）、query（空间查询）',
            'enum': ['list_layers', 'get_geometry', 'query'],
            'required': True,
        },
        {
            'name': 'layer',
            'type': 'string',
            'description': '图层 key，如 "hx"（红线）、"caiqu"（采区）',
            'required': False,
        },
        {
            'name': 'simplify_tolerance',
            'type': 'number',
            'description': '几何简化容差（度），默认 0.0001。值越大顶点越少',
            'required': False,
        },
        {
            'name': 'bbox',
            'type': 'object',
            'description': '查询边界框，格式 {"minx": ..., "miny": ..., "maxx": ..., "maxy": ...}。仅 query 操作需要',
            'required': False,
        },
    ]

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            if isinstance(params, str):
                params = json.loads(params)
        except json.JSONDecodeError:
            return json.dumps({"success": False, "error": "参数不是有效的 JSON"}, ensure_ascii=False)

        action = params.get("action", "")
        layer_key = params.get("layer", "")
        simplify = params.get("simplify_tolerance", 0.0001)

        if action == "list_layers":
            return self._list_layers()

        if action == "get_geometry":
            if not layer_key:
                return json.dumps({"success": False, "error": "get_geometry 需要指定 layer 参数"}, ensure_ascii=False)
            return self._get_geometry(layer_key, simplify)

        if action == "query":
            if not layer_key:
                return json.dumps({"success": False, "error": "query 需要指定 layer 参数"}, ensure_ascii=False)
            bbox = params.get("bbox")
            if not bbox:
                return json.dumps({"success": False, "error": "query 需要指定 bbox 参数"}, ensure_ascii=False)
            return self._query(layer_key, bbox, simplify)

        return json.dumps({
            "success": False,
            "error": f"不支持的操作: {action}。支持: list_layers, get_geometry, query",
        }, ensure_ascii=False)

    def _list_layers(self) -> str:
        """列出所有已注册的空间参考图层。"""
        try:
            from .overlay_tile_service import list_layers as ov_list_layers
            keys = ov_list_layers()
        except Exception:
            keys = list(LAYER_META.keys())

        layers_info = {}
        for key in keys:
            meta = LAYER_META.get(key, {})
            layer = _load_overlay_layer(key)
            layers_info[key] = {
                "name": meta.get("name", key),
                "description": meta.get("description", ""),
                "geom_type": meta.get("geom_type", "unknown"),
                "feature_count": len(layer.geoms) if layer else 0,
            }
        return json.dumps({"success": True, "layers": layers_info}, ensure_ascii=False)

    def _get_geometry(self, layer_key: str, simplify: float) -> str:
        """获取指定图层的几何摘要（bounds + WKT摘要），供 LLM 理解和传递给下游工具。

        注意：不返回完整 GeoJSON（通常几十 MB），而是返回 bounds + WKT 摘要。
        下游工具（如 postgresql_tool）支持直接传入 spatial_ref_layer 参数，
        服务端自动加载几何，LLM 无需传递大几何数据。
        """
        layer = _load_overlay_layer(layer_key)
        if layer is None or not layer.geoms:
            meta = LAYER_META.get(layer_key, {})
            return json.dumps({
                "success": False,
                "error": f"图层 '{layer_key}'（{meta.get('name', '未知')}）不存在或无数据",
                "hint": "请使用 list_layers 查看可用图层",
            }, ensure_ascii=False)

        # 快速计算 bounds（遍历各要素的 bounds，O(n)，远快于 unary_union）
        bounds = None
        wkt_summary = None
        if layer.geoms:
            try:
                # 快速遍历所有要素求整体 bbox（EPSG:3857 → 4326）
                xs_min, ys_min, xs_max, ys_max = [], [], [], []
                for g in layer.geoms:
                    bx = g.bounds
                    xs_min.append(bx[0]); ys_min.append(bx[1])
                    xs_max.append(bx[2]); ys_max.append(bx[3])
                # 用 pyproj 直接转换坐标点
                min_lng, min_lat = _to_4326(min(xs_min), min(ys_min))
                max_lng, max_lat = _to_4326(max(xs_max), max(ys_max))
                bounds = {
                    "min_lng": min_lng, "min_lat": min_lat,
                    "max_lng": max_lng, "max_lat": max_lat,
                }
                # WKT 摘要：只对前 5 个要素做 simplify + WKT，供 LLM 感受几何形态
                from shapely.ops import transform
                sample_geoms = []
                for g in layer.geoms[:5]:
                    s = g.simplify(simplify if simplify > 0 else 0.001, preserve_topology=True)
                    sample_geoms.append(transform(_to_4326, s))
                if sample_geoms:
                    from shapely.geometry import GeometryCollection
                    try:
                        sample_merged = GeometryCollection(sample_geoms)
                    except Exception:
                        sample_merged = sample_geoms[0]
                    wkt_full = sample_merged.wkt
                    wkt_summary = wkt_full[:3000] if len(wkt_full) > 3000 else wkt_full
            except Exception as e:
                logger.warning(f"[spatial_ref] 计算 bbox / WKT 摘要失败: {e}")

        meta = LAYER_META.get(layer_key, {})
        return json.dumps({
            "success": True,
            "layer": layer_key,
            "layer_name": meta.get("name", layer_key),
            "feature_count": len(layer.geoms),
            "bounds": bounds,
            "wkt_summary": wkt_summary,
            "_usage": (
                "【如何使用本结果】\n"
                "1. 需要做空间筛选时，直接用 postgresql_tool(operation='spatial_query', spatial_ref_layer='"
                + layer_key + "')，无需传递几何数据\n"
                "2. 需要在底图上叠加显示时，用 map_tool 叠加，传入 bounds 定位范围"
            ),
        }, ensure_ascii=False)

    def _query(self, layer_key: str, bbox: Dict, simplify: float) -> str:
        """按 bbox 查询图层中相交的要素。"""
        layer = _load_overlay_layer(layer_key)
        if layer is None:
            return json.dumps({
                "success": False,
                "error": f"图层 '{layer_key}' 不可用",
            }, ensure_ascii=False)

        minx = bbox.get("minx", 0)
        miny = bbox.get("miny", 0)
        maxx = bbox.get("maxx", 0)
        maxy = bbox.get("maxy", 0)

        try:
            from shapely import geometry as shp_geom
            from shapely.ops import transform
            # bbox 是 WGS84，需转到 EC 3857 以匹配 overlay 层存储的坐标系
            minx_e, miny_e = _to_3857(minx, miny)
            maxx_e, maxy_e = _to_3857(maxx, maxy)
            results = layer.query(minx_e, miny_e, maxx_e, maxy_e)
        except Exception as e:
            return json.dumps({"success": False, "error": f"空间查询失败: {e}"}, ensure_ascii=False)

        features = []
        for geom, geom_type in results:
            if simplify and simplify > 0:
                try:
                    geom = geom.simplify(simplify, preserve_topology=True)
                except Exception:
                    pass
            features.append({
                "geometry_type": str(geom_type),
                "bounds": [geom.bounds[0], geom.bounds[1], geom.bounds[2], geom.bounds[3]],
            })

        return json.dumps({
            "success": True,
            "layer": layer_key,
            "matched_count": len(results),
            "features_brief": features,
        }, ensure_ascii=False)
