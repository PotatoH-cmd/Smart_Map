# tools/spatial_processing_tool.py
"""
空间数据处理器具 — 将用户提供的坐标数据转换为矢量 GeoJSON 文件并加载到地图。
支持投影坐标系转换、XY 坐标交换、面/线/点要素生成。

典型场景：
- 用户上传含坐标的图片/Excel，或手动输入坐标文本
- LLM 解析坐标后调用本工具
- 工具完成投影转换 → 生成 GeoJSON → 返回 map_command 加载到地图
"""
import os
import json
import uuid
import logging
from typing import Dict, Any, List, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)

# GeoJSON 输出目录
GEOJSON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "geojson")
os.makedirs(GEOJSON_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 投影坐标系名称 → EPSG 代码映射
# ---------------------------------------------------------------------------
CRS_NAME_MAP: Dict[str, int] = {
    # === 3度带 GK 投影（带号已嵌入东偏移，如 4526 的 false_easting=38500000） ===
    "cgcs2000 zone 38n": 4526,
    "cgcs2000 38度带": 4526,
    "cgcs2000 38带": 4526,
    "cgcs2000 38n": 4526,
    "cgcs2000 38": 4526,
    "cgcs2000_38n": 4526,
    "2000国家大地坐标系 38度带": 4526,
    "2000国家大地坐标系 38带": 4526,
    "cgcs2000 / 3-degree gauss-kruger zone 38": 4526,
    "epsg:4526": 4526,
    "4526": 4526,
    "cgcs2000 zone 39n": 4527,
    "cgcs2000 39度带": 4527,
    "cgcs2000 39带": 4527,
    "cgcs2000 39n": 4527,
    "cgcs2000 39": 4527,
    "epsg:4527": 4527,
    "4527": 4527,
    "cgcs2000 zone 37n": 4525,
    "cgcs2000 37度带": 4525,
    "cgcs2000 37带": 4525,
    "epsg:4525": 4525,
    "4525": 4525,
    "cgcs2000 zone 40n": 4528,
    "cgcs2000 40度带": 4528,
    "epsg:4528": 4528,
    "4528": 4528,
    # === 3度带 GK 投影（带号需手动去除，false_easting=500000） ===
    "cgcs2000 cm 114e": 4547,
    "epsg:4547": 4547,
    "4547": 4547,
    "cgcs2000 cm 117e": 4548,
    "epsg:4548": 4548,
    "4548": 4548,
    "cgcs2000 cm 111e": 4546,
    "epsg:4546": 4546,
    "4546": 4546,
    # === 6度带 GK 投影（⚠️ 与 3 度带不同！不要混淆） ===
    # 4497 是 6 度带第 19 带（CM 111°E），不是 3 度带第 38 带！
    "epsg:4497": 4497,
    "4497": 4497,
    "epsg:4496": 4496,
    "4496": 4496,
    "epsg:4498": 4498,
    "4498": 4498,
    # === 地理坐标系 ===
    "wgs84": 4326,
    "epsg:4326": 4326,
    "4326": 4326,
    "cgcs2000": 4490,  # 地理坐标系 CGCS2000
    "2000国家大地坐标系": 4490,
    "cgcs2000 地理坐标系": 4490,
    "epsg:4490": 4490,
    "4490": 4490,
    "web mercator": 3857,
    "epsg:3857": 3857,
    "3857": 3857,
    # === 其他常用中国坐标系 ===
    "北京54": 4214,
    "beijing 54": 4214,
    "epsg:4214": 4214,
    "4214": 4214,
    "西安80": 4610,
    "xian 80": 4610,
    "epsg:4610": 4610,
    "4610": 4610,
}


def _strip_zone_prefix(easting: float, source_crs: int) -> float:
    """
    去除中国测绘惯例中的带号前缀（仅当 CRS 的 false easting ≤ 1000000 时生效）。

    中国 3 度分带 GK 投影有两种 EPSG 编码体系：
    - EPSG:4546~4548: false_easting=500000，坐标 Y=38529158.86 → 需去带号 → 529158.86
    - EPSG:4525~4528: false_easting=38500000（带号已内置），坐标保留不动

    策略：通过 pyproj 读取 CRS 的 false easting，
    仅当 false_easting ≤ 1000000 且 easting > 20000000 时才去除带号前缀。
    """
    if easting < 20000000:
        return easting
    # 查询 CRS 的 false_easting 决定是否需要 strip
    try:
        from pyproj import CRS
        crs = CRS.from_epsg(source_crs)
        cf = crs.coordinate_operation
        if cf:
            for param in cf.params:
                if 'easting' in param.name.lower():
                    false_easting = float(param.value)
                    if false_easting <= 1000000:
                        stripped = easting % 1000000
                        logger.info(f"[spatial_tool] 检测到带号前缀（false_easting={false_easting}），East={easting} → {stripped}")
                        return stripped
                    else:
                        logger.debug(f"[spatial_tool] 坐标系 false_easting={false_easting} ≥ 1000000，保留原始 Easting={easting}")
                        return easting
    except Exception as e:
        logger.warning(f"[spatial_tool] 无法读取 CRS 参数，使用启发式判断: {e}")
    # 回退：无法确定时保守处理，直接返回
    return easting


def _detect_chinese_survey_xy(coords: List[tuple]) -> tuple:
    """智能检测坐标是否为「中国测绘惯例」顺序（第一列=Northing, 第二列=Easting）。

    中国测绘惯例中，X 为北向（3.2M~4.5M），Y 为含带号东向（18M~40M）。
    而 pyproj 标准顺序为 (easting, northing) 即 (Y, X)。

    返回 (coords, did_swap, detection_reason)
    """
    if len(coords) < 2:
        return (coords, False, "")
    
    x_vals = [c[0] for c in coords]
    y_vals = [c[1] for c in coords]
    
    # 检查第一列是否全部在 northing 范围 [3.2M, 4.5M]
    all_x_northing = all(3_200_000 <= abs(v) <= 4_500_000 for v in x_vals)
    # 检查第二列是否全部在 easting（含带号）范围 [18M, 40M]
    all_y_easting_with_zone = all(18_000_000 <= abs(v) <= 40_000_000 for v in y_vals)
    
    if all_x_northing and all_y_easting_with_zone:
        logger.info(f"[spatial_tool] 检测到中国测绘惯例顺序 (X=Northing, Y=Easting)，自动交换为 (Easting, Northing)")
        swapped = [(y, x) for x, y in coords]
        return (swapped, True, "中国测绘惯例 (X=Northing, Y=Easting)")
    
    return (coords, False, "")


def _validate_coordinate_ranges(coords: List[tuple], source_crs: int) -> Optional[str]:
    """预校验坐标值是否在源 CRS 的合理范围内。返回 None 表示通过，否则返回错误描述。"""
    x_vals = [c[0] for c in coords]
    y_vals = [c[1] for c in coords]
    
    if source_crs in (4525, 4526, 4527, 4528, 4546, 4547, 4548, 4496, 4497, 4498):
        # 投影坐标系：easting 应约 18M-40M（含带号）或 0-1M（去带号），northing 应约 3.2M-4.5M
        max_abs_x = max(abs(v) for v in x_vals)
        max_abs_y = max(abs(v) for v in y_vals)
        min_abs_x = min(abs(v) for v in x_vals)
        min_abs_y = min(abs(v) for v in y_vals)
        
        # 检查是否有值在 WGS84 经纬度范围（可能已混淆坐标系）
        all_like_lonlat = all(-180 <= v <= 180 for v in x_vals) and all(-90 <= v <= 90 for v in y_vals)
        if all_like_lonlat:
            return (
                f"检测到坐标值在经纬度范围（X=[{min(x_vals):.2f}, {max(x_vals):.2f}], "
                f"Y=[{min(y_vals):.2f}, {max(y_vals):.2f}]），但指定的源坐标为投影坐标系 EPSG:{source_crs}。"
                f"请确认坐标系是否正确（若为 WGS84 经纬度应使用 EPSG:4326）。"
            )
        
        # 检查 easting 范围
        if max_abs_x < 1_000_000:
            easting_ok = True  # 已去带号
        elif 18_000_000 <= min_abs_x <= 40_000_000:
            easting_ok = True  # 含带号
        else:
            easting_ok = False
        
        northing_ok = 3_200_000 <= min_abs_y <= 4_500_000
        
        if not easting_ok or not northing_ok:
            issues = []
            for i, (x, y) in enumerate(coords):
                if (x < 100_000 or x > 45_000_000) and (y < 100_000 or y > 45_000_000):
                    issues.append(f"  点{i+1}: ({x:.2f}, {y:.2f}) — 两值均异常")
                elif x < 100_000 or x > 45_000_000:
                    issues.append(f"  点{i+1}: X={x:.2f} 异常 (src_crs=EPSG:{source_crs}, 预期 0.5M~40M)")
                elif y < 100_000 or y > 45_000_000:
                    issues.append(f"  点{i+1}: Y={y:.2f} 异常 (src_crs=EPSG:{source_crs}, 预期 3.2M~4.5M)")
            if issues:
                return (
                    f"坐标值可能超出 EPSG:{source_crs} 合法范围：\n"
                    + "\n".join(issues[:8])
                    + f"\n\n提示：中国测绘惯例中 X=Northing Y=Easting，若顺序不一致请设置 swap_xy=true。"
                )
    
    elif source_crs == 4326:
        # WGS84 经纬度：lon [-180,180], lat [-90,90]
        bad_points = []
        for i, (x, y) in enumerate(coords):
            if abs(x) > 180 or abs(y) > 90:
                bad_points.append(f"  点{i+1}: ({x:.6f}, {y:.6f})")
        if bad_points:
            return (
                f"以下坐标超出 WGS84 (EPSG:4326) 合法范围（经度[-180,180], 纬度[-90,90]）：\n"
                + "\n".join(bad_points[:8])
                + "\n\n请确认源坐标系是否正确。"
            )
    
    return None  # 通过


def _resolve_crs(crs_input: Optional[Union[str, int]]) -> int:
    """将用户输入的 CRS 名称/代码解析为 EPSG 整数代码。"""
    if crs_input is None:
        return 4326  # 默认 WGS84
    if isinstance(crs_input, int):
        return crs_input
    key = str(crs_input).strip().lower()
    # 直接数字
    if key.isdigit():
        return int(key)
    # 按名称查找
    if key in CRS_NAME_MAP:
        return CRS_NAME_MAP[key]
    # 模糊匹配（如 "cgcs2000 zone 38" 在前面没有完全匹配时）
    for name, code in CRS_NAME_MAP.items():
        if key in name or name in key:
            return code
    logger.warning(f"[spatial_tool] 无法识别 CRS: {crs_input}, 回退到 WGS84 (4326)")
    return 4326


def _transform_coords(
    coords: List[tuple],
    source_crs: int,
    target_crs: int,
) -> List[tuple]:
    """使用 pyproj 进行坐标投影转换。"""
    try:
        from pyproj import Transformer
        transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
        transformed = [transformer.transform(x, y) for x, y in coords]
        return transformed
    except Exception as e:
        logger.error(f"[spatial_tool] 坐标转换失败: {e}")
        raise ValueError(f"坐标转换失败 (EPSG:{source_crs} → EPSG:{target_crs}): {e}")


def _generate_geojson(
    coords: List[tuple],
    feature_type: str,
    layer_name: str,
) -> Dict[str, Any]:
    """根据坐标和要素类型生成标准 GeoJSON FeatureCollection。"""
    # 构建坐标（GeoJSON 要求 [lng, lat] 即 [x, y]）
    geojson_coords = [[c[0], c[1]] for c in coords]

    if feature_type == "polygon":
        # 多边形需要闭合环
        if geojson_coords and geojson_coords[0] != geojson_coords[-1]:
            geojson_coords.append(geojson_coords[0])
        geometry = {
            "type": "Polygon",
            "coordinates": [geojson_coords],
        }
    elif feature_type == "polyline":
        geometry = {
            "type": "LineString",
            "coordinates": geojson_coords,
        }
    elif feature_type == "point":
        # 多个点 → MultiPoint
        if len(geojson_coords) > 1:
            geometry = {
                "type": "MultiPoint",
                "coordinates": geojson_coords,
            }
        else:
            geometry = {
                "type": "Point",
                "coordinates": geojson_coords[0],
            }
    else:
        raise ValueError(f"不支持的要素类型: {feature_type}，可选: polygon, polyline, point")

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": layer_name,
                    "feature_type": feature_type,
                },
                "geometry": geometry,
            }
        ],
    }


@register_tool('spatial_processing_tool')
class SpatialProcessingTool(BaseTool):
    """
    空间数据处理工具。将用户提供的坐标转换为矢量 GeoJSON 文件，支持：
    - 投影坐标系转换（CGCS2000 ↔ WGS84 等常见坐标系）
    - XY 坐标交换（适应 GIS 与普通坐标的 XY 顺序差异）
    - 生成面（polygon）、线（polyline）、点（point）要素
    - 自动加载到 2D 地图

    典型使用流程：
    1. 用户上传图片/Excel 或输入坐标文本
    2. LLM 从图片/文本中提取坐标数组
    3. LLM 调用本工具，传入坐标 + 投影信息
    4. 工具完成转换并生成 GeoJSON，自动加载到地图
    """

    description = (
        "空间数据处理工具。用于根据坐标生成矢量 GeoJSON 文件并加载到地图。\n"
        "支持功能：\n"
        "- 投影坐标系转换（CGCS2000 / WGS84 / 北京54 / 西安80 等）\n"
        "- 智能 XY 坐标检测（自动识别测绘惯例顺序并纠正）\n"
        "- 生成面（polygon）、线（polyline）、点（point）要素\n"
        "- 自动加载生成的矢量到 2D 地图\n\n"
        "坐标系名称映射：\n"
        "- CGCS2000 Zone 38N / 38度带 / 2000国家大地坐标系 38度带 → EPSG:4526（推荐）\n"
        "- CGCS2000 Zone 39N → EPSG:4527\n"
        "- CGCS2000 Zone 37N → EPSG:4525\n"
        "- CGCS2000 CM 114E（需手动去带号）→ EPSG:4547\n"
        "- WGS84 → EPSG:4326\n"
        "- Web Mercator → EPSG:3857\n\n"
        "⚠️ 重要提示：\n"
        "- 坐标顺序应为 [[easting, northing], ...] 即第一列为东向（约 38M），第二列为北向（约 3.5M）\n"
        "- 工具会自动检测测绘惯例顺序（X=北向, Y=东向）并纠正，无需手动设置 swap_xy\n"
        "- 中国测绘数据通常使用 EPSG:4526（东偏移已包含带号）\n"
        "- 必须提取**全部**坐标点，不能遗漏任何点位",
        "典型使用场景：\n"
        "1. 用户上传图片（含坐标表），OCR 已提取文字 → 解析坐标并调用本工具\n"
        "2. 用户上传 Excel 文件 → 解析表格提取坐标列，调用本工具\n"
        "3. 手输坐标（easting first）：'38529158.86,3552058.84; 38529000,3566230.52; ...  投影 CGCS2000 38度带'\n"
        "   → coordinates=[[38529158.86,3552058.84],[38529000,3566230.52],...], source_crs='4526', feature_type='polygon'\n"
        "4. 手输经纬度：'115.2,32.5; 115.3,32.6; 115.3,32.8; 115.2,32.8 生成面'\n"
        "   → coordinates=[[115.2,32.5],[115.3,32.6],...], source_crs='4326', feature_type='polygon'\n"
    )

    parameters = [
        {
            "name": "action",
            "type": "string",
            "description": "操作类型",
            "enum": [
                "generate_polygon",   # 生成面要素
                "generate_polyline",  # 生成线要素
                "generate_points",    # 生成点要素
                "transform_coords",   # 仅坐标转换，返回转换后坐标
            ],
            "required": True,
        },
        {
            "name": "coordinates",
            "type": "array",
            "description": (
                "坐标数组，格式: [[easting, northing], [easting, northing], ...]。\n"
                "easting=东向（CGCS2000 含带号约 38M，WGS84 经纬度约 114°），northing=北向（约 3.5M 或 32°）。\n"
                "工具会自动检测测绘惯例顺序（X=北向, Y=东向）并纠正，无需手动设置 swap_xy。\n"
                "从 OCR/Excel 中提取坐标时，按原文顺序传入即可。"
            ),
            "required": True,
        },
        {
            "name": "source_crs",
            "type": "string",
            "description": (
                "源投影坐标系，支持 EPSG 代码（如 '4526', '4547', '4326'）或名称（如 'CGCS2000 Zone 38N', 'WGS84'）。\n"
                "CGCS2000 Zone 38N 推荐使用 EPSG:4526（东偏移已内置带号，坐标无需手动处理）。\n"
                "也可用 EPSG:4547（需手动去除带号前缀）。\n"
                "注意：CGCS2000 Zone 38N 是「3度分带」投影系，带号 38，适用于信阳地区（约 114°E）。"
            ),
            "required": False,
        },
        {
            "name": "target_crs",
            "type": "string",
            "description": "目标坐标系。地图使用 WGS84 (EPSG:4326)，默认自动转换为 4326。",
            "required": False,
        },
        {
            "name": "swap_xy",
            "type": "boolean",
            "description": (
                "是否交换输入的 X/Y 坐标。\n"
                "当用户明确说 'XY 相反'、'Y 是 X'、'坐标顺序反了' 时设置为 true。\n"
                "默认 false（不交换）。"
            ),
            "required": False,
        },
        {
            "name": "layer_name",
            "type": "string",
            "description": "图层名称，显示在地图图例中。如未提供，自动生成。",
            "required": False,
        },
        {
            "name": "auto_load",
            "type": "boolean",
            "description": "是否自动加载到地图，默认 true。",
            "required": False,
        },
    ]

    def call(self, params: Union[str, dict], **kwargs) -> dict:
        """处理空间数据请求。"""
        # 参数解析
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {"success": False, "error": "无效的 JSON 参数"}
        if not isinstance(params, dict):
            return {"success": False, "error": f"无效的参数类型: {type(params)}"}

        action = params.get("action", "generate_polygon")
        coordinates = params.get("coordinates", [])
        source_crs_input = params.get("source_crs")
        target_crs_input = params.get("target_crs")
        swap_xy = params.get("swap_xy", False)
        layer_name = params.get("layer_name", "")
        auto_load = params.get("auto_load", True)

        # 根据 action 确定 feature_type
        feature_type_map = {
            "generate_polygon": "polygon",
            "generate_polyline": "polyline",
            "generate_points": "point",
            "transform_coords": "point",  # 转换模式默认按点处理
        }
        feature_type = feature_type_map.get(action, "polygon")

        # 校验坐标
        if not coordinates or not isinstance(coordinates, list) or len(coordinates) == 0:
            return {"success": False, "error": "坐标不能为空，请提供 [[x1,y1],[x2,y2],...] 格式的坐标数组"}

        try:
            # 解析坐标 (x, y) 元组列表
            parsed: List[tuple] = []
            for item in coordinates:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    x = float(item[0])
                    y = float(item[1])
                    parsed.append((x, y))
                else:
                    return {"success": False, "error": f"坐标格式错误: {item}，应为 [x, y]"}

            if len(parsed) == 0:
                return {"success": False, "error": "没有有效的坐标点"}

            # XY 交换（用户显式指定）
            original_parsed = parsed.copy()  # 保存原始坐标，供 Infinity 检测回退使用
            if swap_xy:
                parsed = [(y, x) for x, y in parsed]
                logger.info(f"[spatial_tool] XY 坐标已交换（用户指定），点数: {len(parsed)}")
            else:
                # 智能检测：中国测绘惯例中 X=Northing, Y=Easting，自动识别并纠正
                parsed, auto_swapped, reason = _detect_chinese_survey_xy(parsed)
                if auto_swapped:
                    swap_xy = True
                    logger.info(f"[spatial_tool] XY 已自动交换: {reason}")

            # 解析 CRS
            source_crs = _resolve_crs(source_crs_input)
            target_crs = _resolve_crs(target_crs_input) if target_crs_input else 4326

            # 去除中国测绘习惯中的带号前缀（如 38529158.86 → 529158.86）
            # _strip_zone_prefix 会自动检测 CRS false_easting，仅对 454x 系列生效
            # ⚠️ 同时对 x 和 y 做 strip——LLM 可能不知道哪个维度有带号前缀
            parsed = [(_strip_zone_prefix(x, source_crs), _strip_zone_prefix(y, source_crs)) for x, y in parsed]

            # 坐标范围预校验（在调用 pyproj 前快速检测异常值）
            range_error = _validate_coordinate_ranges(parsed, source_crs)
            if range_error and source_crs != 4326:
                logger.warning(f"[spatial_tool] 坐标范围校验未通过: {range_error[:200]}")

            # 坐标投影转换
            if source_crs != target_crs:
                transformed = _transform_coords(parsed, source_crs, target_crs)
                logger.info(
                    f"[spatial_tool] 坐标从 EPSG:{source_crs} 转换到 EPSG:{target_crs}, "
                    f"点数: {len(transformed)}"
                )

                # ═══════════════════════════════════════════════════════════
                # 安全兜底：检测转换结果是否包含 Infinity/NaN
                # 常见原因：XY 顺序错误导致 pyproj 无法收敛
                # 策略：双向尝试（交换/不交换），两次都失败则返回详细诊断
                # ═══════════════════════════════════════════════════════════
                import math
                
                def _build_inf_error(extra_info: str = "") -> dict:
                    coord_sample = original_parsed[:3] if len(original_parsed) >= 3 else original_parsed
                    msg = (
                        f"坐标转换失败：投影 EPSG:{source_crs} → EPSG:{target_crs} 结果为无穷大。\n"
                        f"可能原因：\n"
                        f"1. 从图片/文件中提取的坐标值有误，请手动核对原始坐标\n"
                        f"2. 指定的投影坐标系与实际数据不匹配\n"
                        f"3. XY 坐标顺序可能不正确\n\n"
                        f"收到的坐标（前 3 个）: {coord_sample}\n"
                        f"源坐标系: EPSG:{source_crs}，目标: EPSG:{target_crs}"
                    )
                    if range_error:
                        msg += f"\n\n【坐标范围诊断】\n{range_error}"
                    if extra_info:
                        msg += f"\n{extra_info}"
                    msg += (
                        f"\n\n建议：请手动输入坐标点的实际数值，格式为 [[x1,y1],[x2,y2],...]，"
                        f"并确认正确的投影坐标系（如 CGCS2000 Zone 38N = EPSG:4526）。"
                    )
                    return {"success": False, "error": msg}
                
                has_inf = any(
                    math.isinf(c[0]) or math.isinf(c[1]) or math.isnan(c[0]) or math.isnan(c[1])
                    for c in transformed
                )
                if has_inf and swap_xy:
                    logger.warning(
                        f"[spatial_tool] 转换结果包含 Infinity/NaN，"
                        f"可能是 XY 交换方向错误，尝试不使用 swap_xy 重新转换"
                    )
                    # 回退：用未交换的原始坐标重试
                    parsed_retry = original_parsed  # 在 swap 之前保存的原始坐标
                    parsed_retry = [(_strip_zone_prefix(x, source_crs), y) for x, y in parsed_retry]
                    transformed = _transform_coords(parsed_retry, source_crs, target_crs)
                    swap_xy = False  # 标记为未交换
                    has_inf = any(
                        math.isinf(c[0]) or math.isinf(c[1]) or math.isnan(c[0]) or math.isnan(c[1])
                        for c in transformed
                    )
                    if has_inf:
                        logger.error(f"[spatial_tool] 回退后仍为 Infinity/NaN，坐标数据可能无效")
                        return _build_inf_error("（已尝试无XY交换回退，仍失败）")
                    logger.info(f"[spatial_tool] 回退成功，XY 已自动纠正，点数: {len(transformed)}")
                elif has_inf:
                    # swap_xy=false 但结果仍是 Infinity → 尝试交换 XY 回退
                    logger.warning(
                        f"[spatial_tool] 转换结果包含 Infinity/NaN，"
                        f"尝试自动交换 XY 顺序重新转换"
                    )
                    parsed_retry = [(y, x) for x, y in original_parsed]
                    parsed_retry = [(_strip_zone_prefix(x, source_crs), _strip_zone_prefix(y, source_crs)) for x, y in parsed_retry]
                    transformed = _transform_coords(parsed_retry, source_crs, target_crs)
                    swap_xy = True  # 标记为已交换
                    has_inf = any(
                        math.isinf(c[0]) or math.isinf(c[1]) or math.isnan(c[0]) or math.isnan(c[1])
                        for c in transformed
                    )
                    if has_inf:
                        logger.error(f"[spatial_tool] XY 交换回退后仍为 Infinity/NaN")
                        return _build_inf_error("（已尝试XY交换回退，仍失败）")
                    logger.info(f"[spatial_tool] XY 交换回退成功，点数: {len(transformed)}")
            else:
                transformed = parsed
                logger.info(f"[spatial_tool] 坐标未转换（源=目标 EPSG:{source_crs}），点数: {len(transformed)}")

            # 纯转换模式：只返回转换后的坐标
            if action == "transform_coords":
                return {
                    "success": True,
                    "message": f"坐标已从 EPSG:{source_crs} 转换到 EPSG:{target_crs}",
                    "transformed_coords": [[c[0], c[1]] for c in transformed],
                    "source_crs": source_crs,
                    "target_crs": target_crs,
                    "point_count": len(transformed),
                }

            # 生成图层名称
            if not layer_name:
                layer_name = f"空间矢量_{uuid.uuid4().hex[:6]}"

            # 生成 GeoJSON
            geojson = _generate_geojson(transformed, feature_type, layer_name)

            # 保存到文件
            file_name = f"spatial_{uuid.uuid4().hex[:8]}.geojson"
            file_path = os.path.join(GEOJSON_DIR, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(geojson, f, ensure_ascii=False, indent=2)

            url = f"/api/geojson/{file_name}"
            logger.info(f"[spatial_tool] GeoJSON 已生成: {url}")

            # 构建 map_command（由 task_executor 传递给前端）
            map_command = {
                "type": "load_vector_layer",
                "url": url,
                "name": layer_name,
                "feature_type": feature_type,
            }

            result_msg = (
                f"矢量{feature_type}「{layer_name}」已生成，"
                f"包含 {len(transformed)} 个坐标点，"
                f"从 EPSG:{source_crs} 转换到 EPSG:{target_crs}"
                + ("，已交换 XY" if swap_xy else "")
                + ("，已加载到地图。" if auto_load else "。")
            )

            return {
                "success": True,
                "map_command": map_command if auto_load else None,
                "message": result_msg,
                "feature_count": len(transformed),
                "feature_type": feature_type,
                "source_crs": source_crs,
                "target_crs": target_crs,
                "url": url,
                "layer_name": layer_name,
            }

        except ValueError as e:
            logger.error(f"[spatial_tool] 参数错误: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"[spatial_tool] 处理失败: {e}", exc_info=True)
            return {"success": False, "error": f"空间处理失败: {str(e)}"}
