"""
快速路由表加载器。从 JSON 配置文件读取关键词→意图映射，支持热重载。

设计要点：
- 首次加载及每次调用 load_fast_routes() 时检查文件 mtime
- 若 JSON 文件不存在或格式错误，回退到内联默认值（与 agent_harness.FAST_ROUTE_KEYWORDS 保持同步）
- 默认值始终与 JSON 文件内容同步（JSON 为权威来源）

注意：为避免触发 agents/__init__.py 的级联导入，IntentType 在函数内部延迟导入。
"""
import json
import logging
import os
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)


def _get_intent_type():
    """延迟导入 IntentType，避免模块级 import 触发 agents/__init__.py 级联加载。"""
    from agents.intent_types import IntentType
    return IntentType


# 内联默认路由表（与 agent_harness.FAST_ROUTE_KEYWORDS 保持同步）
# 注意：使用字符串而非 IntentType 枚举值，避免模块级导入触发 agents/__init__.py 级联加载
_DEFAULT_ROUTES: List[Tuple[str, str]] = [
    ("切换卫星", "map_display"),
    ("卫星图层", "map_display"),
    ("卫星底图", "map_display"),
    ("卫星影像", "map_display"),
    ("高分影像", "map_display"),
    ("街道图", "map_display"),
    ("osm", "map_display"),
    ("切换底图", "map_display"),
    ("加载矢量", "map_display"),
    ("加载采区", "map_display"),
    ("加载图层", "map_display"),
    ("清除地图", "map_display"),
    ("清除标记", "map_display"),
    ("飞到", "map_display"),
    ("飞往", "map_display"),
    ("flyto", "map_display"),
    ("查找位置", "location_search"),
    ("搜索位置", "location_search"),
    ("定位", "location_search"),
    ("坐标转换", "spatial_processing"),
    ("投影坐标", "spatial_processing"),
    ("xy相反", "spatial_processing"),
    ("生成矢量", "spatial_processing"),
    ("生成面", "spatial_processing"),
    ("带号", "spatial_processing"),
    ("cgcs2000", "spatial_processing"),
    ("红线", "spatial_reference"),
    ("河道红线", "spatial_reference"),
    ("管理红线", "spatial_reference"),
    ("红线附近", "spatial_reference"),
    ("红线范围内", "spatial_reference"),
    ("生成报告", "report_generation"),
    ("出具报告", "report_generation"),
    ("导出报告", "report_generation"),
    ("生成图表", "data_visualization"),
    ("画个图", "data_visualization"),
    ("柱状图", "data_visualization"),
    ("饼图", "data_visualization"),
    ("折线图", "data_visualization"),
    ("政策", "knowledge_search"),
    ("规范", "knowledge_search"),
    ("管理规定", "knowledge_search"),
    ("技术标准", "knowledge_search"),
]

# JSON 配置文件路径（相对于本文件）
_JSON_PATH = os.path.join(os.path.dirname(__file__), "fast_routes.json")

# 热重载缓存：记录上次加载时的 mtime，避免每次查询都重新读取文件
_cached_mtime: float = 0.0
_cached_routes: List[Tuple[str, Any]] = []


def _resolve_default_routes() -> List[Tuple[str, Any]]:
    """将字符串默认路由表转换为 (keyword, IntentType) 列表。"""
    IntentType = _get_intent_type()
    result: List[Tuple[str, Any]] = []
    for keyword, intent_str in _DEFAULT_ROUTES:
        try:
            result.append((keyword, IntentType(intent_str)))
        except ValueError:
            logger.warning(f"[fast_route_loader] 默认路由表含未知意图: '{intent_str}'")
    return result


def _parse_routes(data: dict) -> List[Tuple[str, Any]]:
    """将 JSON 数据解析为 (keyword, IntentType) 列表。"""
    IntentType = _get_intent_type()
    routes: List[Tuple[str, Any]] = []
    for item in data.get("routes", []):
        keyword = item.get("keyword", "").strip()
        intent_str = item.get("intent", "").strip()
        if not keyword or not intent_str:
            continue
        try:
            intent = IntentType(intent_str)
            routes.append((keyword, intent))
        except ValueError:
            logger.warning(f"[fast_route_loader] 未知意图类型，已跳过: keyword='{keyword}', intent='{intent_str}'")
    return routes


def load_fast_routes(force_reload: bool = False) -> List[Tuple[str, Any]]:
    """加载快速路由映射表。

    默认使用 mtime 缓存：仅在 JSON 文件发生变更时重新读取。
    传 force_reload=True 可强制立即重新加载。

    Returns:
        List[Tuple[str, IntentType]]: (关键词, 意图) 映射列表。
        若 JSON 文件不可用，回退到内联默认值。
    """
    global _cached_mtime, _cached_routes

    if not os.path.exists(_JSON_PATH):
        if _cached_routes:
            # 之前加载过，文件被删除了 — 继续使用缓存
            logger.warning("[fast_route_loader] JSON 文件不存在，使用内存缓存")
            return _cached_routes
        logger.warning(
            f"[fast_route_loader] JSON 文件不存在 ({_JSON_PATH})，回退到硬编码默认值"
        )
        return _resolve_default_routes()

    try:
        current_mtime = os.path.getmtime(_JSON_PATH)
    except OSError:
        logger.warning("[fast_route_loader] 无法读取 JSON 文件 mtime，使用硬编码默认值")
        return _resolve_default_routes()

    if not force_reload and current_mtime <= _cached_mtime and _cached_routes:
        return _cached_routes

    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[fast_route_loader] JSON 解析失败: {e}，使用硬编码默认值")
        return _resolve_default_routes()

    routes = _parse_routes(data)
    if not routes:
        logger.warning("[fast_route_loader] JSON 路由表为空，回退到硬编码默认值")
        return _resolve_default_routes()

    _cached_mtime = current_mtime
    _cached_routes = routes
    logger.info(f"[fast_route_loader] 已加载 {len(routes)} 条快速路由规则")
    return routes
