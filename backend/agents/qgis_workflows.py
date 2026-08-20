"""
QGIS 工作流 Recipe 定义（声明式，纯数据，不包含业务代码）。

每个 recipe 描述一个 GIS 操作的完整执行序列：
  1. extract_params — 从用户消息提取参数（正则 + 默认值）
  2. steps — MCP 工具调用序列，支持 $变量引用前一步的输出
  3. output — 如何聚合最终结果

新增 GIS 操作只需在此文件添加一个 recipe dict，无需修改 task_executor.py。
"""

# ── 通用参数提取模式 ──
_PARAM_PATTERNS = {
    "shp_path": {
        "regex": r"(/[\w./-]+\.shp)",
        "default": "/gis_data/2026年采区.shp",
        "description": "SHP 文件路径",
    },
    "feature_name": {
        "regex": r"(?:计算|查找|查询|显示|查看|对|给|为|的)?([\u4e00-\u9fa5]{2,10}(?:砂场|采区|河道|工程|水库|河流))",
        "default": "",
        "clean": r"^(计算|查找|查询|显示|查看|对|给|为|的)",
        "description": "目标要素名称（如郝楼砂场）",
    },
    "distance": {
        "regex": r"(\d+)\s*米",
        "default": 100,
        "description": "缓冲区距离（米）",
    },
    "output_crs": {
        "regex": r"EPSG:(\d+)",
        "default": "EPSG:4326",
        "description": "输出坐标系",
    },
    "work_crs": {
        "regex": r"",
        "default": "EPSG:3857",
        "description": "工作坐标系（米制，用于精确距离计算）",
    },
}

# ── 常用步骤模板 ──
_STEP_TEMPLATES = {
    "load_shp": {
        "category": "layer",
        "action": "add_vector",
        "params": {"path": "$shp_path", "name": "source_data"},
        "capture": {"layer_id": "source_layer_id"},
    },
    "export_filtered_3857": {
        "category": "layer",
        "action": "export",
        "params": {
            "layer_id": "$source_layer_id",
            "output_path": "/output/$feature_name_$_uid_3857.geojson",
            "filter_expression": "Name LIKE '%$feature_name%'",
            "target_crs": "$work_crs",
        },
        "capture": {"output_path": "filtered_3857_path"},
    },
    "load_filtered": {
        "category": "layer",
        "action": "add_vector",
        "params": {"path": "$filtered_3857_path", "name": "$feature_name_3857"},
        "capture": {"layer_id": "filtered_layer_id"},
    },
    "export_4326": {
        "category": "layer",
        "action": "export",
        "params": {
            "layer_id": "$buffer_layer_id",
            "output_path": "/output/$feature_name_buffer_$_uid_4326.geojson",
            "target_crs": "EPSG:4326",
        },
    },
    "export_original_4326": {
        "category": "layer",
        "action": "export",
        "params": {
            "layer_id": "$filtered_layer_id",
            "output_path": "/output/$feature_name_original_$_uid_4326.geojson",
            "target_crs": "EPSG:4326",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# 工作流 Recipe 定义
# ═══════════════════════════════════════════════════════════════════

RECIPES: dict[str, dict] = {
    # ─────────────────────────────────────────────────────────
    # 缓冲区（buffer）
    # ─────────────────────────────────────────────────────────
    "buffer": {
        "description": "对指定图层的要素创建缓冲区（自动处理 CRS 转换）",
        "keywords": ["缓冲区", "buffer", "缓冲", "buffered"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
            {"param": "distance", "patterns": _PARAM_PATTERNS["distance"]},
            {"param": "work_crs", "patterns": _PARAM_PATTERNS["work_crs"]},
        ],
        "steps": [
            # Step 1: 加载源 SHP
            {
                "category": "layer",
                "action": "add_vector",
                "params": {"path": "$shp_path", "name": "source_data"},
                "capture": {"id": "source_layer_id"},
            },
            # Step 2: 筛选目标要素 → 重投影到米制 CRS
            {
                "category": "layer",
                "action": "export",
                "params": {
                    "layer_id": "$source_layer_id",
                    "output_path": "/output/${feature_name}_${uid}_3857.geojson",
                    "filter_expression": "Name LIKE '%${feature_name}%'",
                    "target_crs": "$work_crs",
                },
            },
            # Step 3: 加载筛选后的图层
            {
                "category": "layer",
                "action": "add_vector",
                "params": {"path": "/output/${feature_name}_${uid}_3857.geojson", "name": "${feature_name}_3857"},
                "capture": {"id": "filtered_layer_id"},
            },
            # Step 4: 执行缓冲区
            {
                "category": "processing",
                "action": "execute",
                "params": {
                    "algorithm": "native:buffer",
                    "parameters": {
                        "INPUT": "$filtered_layer_id",
                        "DISTANCE": "$distance",
                        "OUTPUT": "/output/${feature_name}_buffer_${uid}_3857.geojson",
                    },
                },
            },
            # Step 5: 加载缓冲区图层
            {
                "category": "layer",
                "action": "add_vector",
                "params": {"path": "/output/${feature_name}_buffer_${uid}_3857.geojson", "name": "${feature_name}_buffer_3857"},
                "capture": {"id": "buffer_layer_id"},
            },
            # Step 6: 导出缓冲区 → EPSG:4326
            {
                "category": "layer",
                "action": "export",
                "params": {
                    "layer_id": "$buffer_layer_id",
                    "output_path": "/output/${feature_name}_buffer_${uid}_4326.geojson",
                    "target_crs": "EPSG:4326",
                },
            },
            # Step 7: 导出原始边界 → EPSG:4326
            {
                "category": "layer",
                "action": "export",
                "params": {
                    "layer_id": "$filtered_layer_id",
                    "output_path": "/output/${feature_name}_original_${uid}_4326.geojson",
                    "target_crs": "EPSG:4326",
                },
            },
        ],
        # 后处理：合并原始边界 + 缓冲区为单个 GeoJSON
        "post_process": {
            "combine_geojson": {
                "inputs": [
                    "/output/${feature_name}_original_${uid}_4326.geojson",
                    "/output/${feature_name}_buffer_${uid}_4326.geojson",
                ],
                "tags": [
                    {"_type": "original", "_name": "${feature_name}范围"},
                    {"_type": "buffer", "_name": "${feature_name}${distance}米缓冲区"},
                ],
                "output": "/output/${feature_name}_combined_buffer_${uid}.geojson",
            }
        },
        "result_message": "已为${feature_name}范围创建${distance}米缓冲区，输出包含原始边界和缓冲边界共 2 个图层。",
        "render": {
            "layer_name_template": "${feature_name}-${distance}米缓冲区",
            "style": {
                "polygon": {
                    "color": "#ff6600",
                    "weight": 3,
                    "fillColor": "#ff6600",
                    "fillOpacity": 0.15
                }
            },
            "view": {
                "strategy": "fit_bounds"
            }
        },
    },

    # ─────────────────────────────────────────────────────────
    # 距离到最近红线（distance_to_redline）
    # ─────────────────────────────────────────────────────────
    "distance_to_redline": {
        "description": "计算采区到最近河道红线的距离",
        "keywords": ["距离", "红线", "最近", "最短距离", "多远"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            # Step 1: 加载 SHP
            {
                "category": "layer",
                "action": "add_vector",
                "params": {"path": "$shp_path", "name": "source_data"},
                "capture": {"id": "source_layer_id"},
            },
            # Step 2: 筛选目标采区 → 导出 EPSG:4326
            {
                "category": "layer",
                "action": "export",
                "params": {
                    "layer_id": "$source_layer_id",
                    "output_path": "/output/${feature_name}_${uid}_4326.geojson",
                    "filter_expression": "Name LIKE '%${feature_name}%'",
                    "target_crs": "EPSG:4326",
                },
            },
        ],
        "post_process": {
            "distance_to_redline": {
                "feature_geojson": "/output/${feature_name}_${uid}_4326.geojson",
                "redline_layer": "hx",
            }
        },
        "render": {
            "layer_name_template": "${feature_name}-最近红线连线",
            "style": {
                "line": {"color": "#ff0000", "weight": 3, "dashArray": "5, 5"}
            },
            "view": {
                "strategy": "fit_bounds"
            }
        },
        "result_message": "${feature_name}采区距离最近的河道管理红线约 ${distance} 米。",
    },

    # ─────────────────────────────────────────────────────────
    # 裁剪（clip）
    # ─────────────────────────────────────────────────────────
    "clip": {
        "description": "用裁剪图层裁剪目标图层",
        "keywords": ["裁剪", "clip", "切割"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            {"category": "layer", "action": "add_vector",
             "params": {"path": "$shp_path", "name": "source_data"},
             "capture": {"id": "source_layer_id"}},
            {"category": "processing", "action": "execute",
             "params": {
                 "algorithm": "native:clip",
                 "parameters": {
                     "INPUT": "$source_layer_id",
                     "OUTPUT": "/output/${feature_name}_clipped_${uid}.geojson",
                 },
             }},
        ],
        "result_message": "已对${feature_name}执行裁剪操作。",
    },

    # ─────────────────────────────────────────────────────────
    # 空间关联（spatial_join）
    # ─────────────────────────────────────────────────────────
    "spatial_join": {
        "description": "空间关联两个图层",
        "keywords": ["空间关联", "spatial join", "相交", "intersect", "叠加"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            {"category": "layer", "action": "add_vector",
             "params": {"path": "$shp_path", "name": "target"},
             "capture": {"id": "target_layer_id"}},
            {"category": "analysis", "action": "spatial_join",
             "params": {
                 "target_layer": "$target_layer_id",
                 "join_layer": "$target_layer_id",
                 "predicates": [0],
                 "output_path": "/output/${feature_name}_join_${uid}.geojson",
             }},
        ],
        "result_message": "已对${feature_name}执行空间关联分析。",
    },

    # ─────────────────────────────────────────────────────────
    # 分区统计（zonal_statistics）
    # ─────────────────────────────────────────────────────────
    "zonal_statistics": {
        "description": "按面区域统计栅格值",
        "keywords": ["分区统计", "zonal statistics", "区域统计"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            {"category": "analysis", "action": "zonal_statistics",
             "params": {
                 "polygon_layer": "$source_layer_id",
                 "raster_layer": "$source_layer_id",
                 "stats": [1, 2, 5, 6],
             }},
        ],
        "result_message": "已完成${feature_name}的分区统计。",
    },

    # ─────────────────────────────────────────────────────────
    # 面积计算
    # ─────────────────────────────────────────────────────────
    "area_calculation": {
        "description": "计算图层要素的面积",
        "keywords": ["面积", "area", "计算面积"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            {"category": "layer", "action": "add_vector",
             "params": {"path": "$shp_path", "name": "source_data"},
             "capture": {"id": "source_layer_id"}},
            {"category": "field", "action": "calculate",
             "params": {
                 "layer_id": "$source_layer_id",
                 "field_name": "area_m2",
                 "expression": "$area",
                 "field_type": "double",
             }},
            {"category": "features", "action": "get_statistics",
             "params": {"layer_id": "$source_layer_id", "field_name": "area_m2"}},
        ],
        "result_message": "已计算${feature_name}的面积。",
    },

    # ─────────────────────────────────────────────────────────
    # 几何中心点（centroid）
    # ─────────────────────────────────────────────────────────
    "centroid": {
        "description": "计算指定要素的几何中心点",
        "keywords": ["中心点", "centroid", "几何中心", "重心"],
        "intent": "spatial_analysis",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
            {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
        ],
        "steps": [
            # Step 1: 加载源 SHP
            {"category": "layer", "action": "add_vector",
             "params": {"path": "$shp_path", "name": "source_data"},
             "capture": {"id": "source_layer_id"}},
            # Step 2: 筛选目标要素 → EPSG:4326
            {"category": "layer", "action": "export",
             "params": {
                 "layer_id": "$source_layer_id",
                 "output_path": "/output/${feature_name}_${uid}_4326.geojson",
                 "filter_expression": "Name LIKE '%${feature_name}%'",
                 "target_crs": "EPSG:4326",
             }},
            # Step 3: 加载筛选后图层
            {"category": "layer", "action": "add_vector",
             "params": {"path": "/output/${feature_name}_${uid}_4326.geojson",
                         "name": "${feature_name}_4326"},
             "capture": {"id": "filtered_layer_id"}},
            # Step 4: 执行 centroids 算法
            {"category": "processing", "action": "execute",
             "params": {
                 "algorithm": "native:centroids",
                 "parameters": {
                     "INPUT": "$filtered_layer_id",
                     "OUTPUT": "/output/${feature_name}_centroid_${uid}.geojson",
                 },
             }},
            # Step 5: 加载中心点图层
            {"category": "layer", "action": "add_vector",
             "params": {"path": "/output/${feature_name}_centroid_${uid}.geojson",
                         "name": "${feature_name}_centroid"},
             "capture": {"id": "centroid_layer_id"}},
            # Step 6: 导出中心点 → EPSG:4326
            {"category": "layer", "action": "export",
             "params": {
                 "layer_id": "$centroid_layer_id",
                 "output_path": "/output/${feature_name}_centroid_${uid}_4326.geojson",
                 "target_crs": "EPSG:4326",
             }},
        ],
        "result_message": "已计算${feature_name}的几何中心点，坐标标注在地图上。",
        "render": {
            "layer_name_template": "${feature_name}-centroid",
            "style": {
                "point": {
                    "radius": 10,
                    "fillColor": "#ff0000",
                    "color": "#ffffff",
                    "weight": 3,
                    "fillOpacity": 0.9
                }
            },
            "view": {
                "strategy": "fly_to_centroid",
                "zoom": 15
            }
        },
    },

    # ─────────────────────────────────────────────────────────
    # 图层加载（passthrough）
    # ─────────────────────────────────────────────────────────
    "load_layer": {
        "description": "加载图层到 QGIS（单步透传）",
        "keywords": ["加载", "load", "add_layer", "打开"],
        "intent": "map_display",
        "extract": [
            {"param": "shp_path", "patterns": _PARAM_PATTERNS["shp_path"]},
        ],
        "steps": [
            {"category": "layer", "action": "add_vector",
             "params": {"path": "$shp_path", "name": "loaded_layer"},
             "capture": {"id": "layer_id"}},
        ],
        "result_message": "已加载图层：$shp_path。",
    },
}


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

import re as _re
from typing import Dict, List, Tuple


def match_recipe(user_message: str) -> Tuple[str, dict]:
    """根据用户消息匹配最佳 recipe。

    遍历所有 recipe 的 keywords，返回匹配分数最高的 recipe。
    分数 = 匹配到的关键词字符数之和。
    """
    best_name = ""
    best_recipe = None
    best_score = 0

    for name, recipe in RECIPES.items():
        keywords = recipe.get("keywords", [])
        score = 0
        for kw in keywords:
            # 用正则匹配确保不部分匹配到无关词
            if _re.search(kw, user_message, _re.IGNORECASE):
                score += len(kw)
        if score > best_score:
            best_score = score
            best_name = name
            best_recipe = recipe

    return best_name, best_recipe


def extract_params(user_message: str, recipe: dict) -> Dict[str, any]:
    """从用户消息中提取 recipe 所需的参数。"""
    params = {}
    for entry in recipe.get("extract", []):
        param_name = entry["param"]
        patterns = entry.get("patterns", {})
        regex = patterns.get("regex", "")
        default = patterns.get("default")
        clean_regex = patterns.get("clean", "")

        value = default
        if regex:
            m = _re.search(regex, user_message)
            if m:
                value = m.group(1) if m.groups() else m.group(0)

        # 清理前缀（如去掉"对/给/为"）
        if isinstance(value, str) and clean_regex:
            value = _re.sub(clean_regex, "", value)

        params[param_name] = value

    return params


def substitute_vars(text: str, variables: dict) -> str:
    """替换字符串中的 $variable 和 ${variable} 占位符。"""
    def _replace(match):
        var_name = match.group(1) or match.group(2)
        return str(variables.get(var_name, f"${var_name}"))

    result = _re.sub(r"\$\{(\w+)\}|\$(\w+)", _replace, text)
    return result


def substitute_params(params: dict, variables: dict) -> dict:
    """递归替换 params dict 中的所有字符串变量。"""
    if isinstance(params, str):
        return substitute_vars(params, variables)
    elif isinstance(params, dict):
        return {k: substitute_params(v, variables) for k, v in params.items()}
    elif isinstance(params, list):
        return [substitute_params(v, variables) for v in params]
    return params
