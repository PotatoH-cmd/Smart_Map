"""
gis_validators.py — qgis_mcp_tool 校验（CRS 米制 / 工作流步骤 / GeoJSON 要素数）。

- check_crs_metric（preflight）：参数中的 CRS 若为度单位 → critical（米制距离运算会出错）
- check_workflow_steps_failed（postflight）：recipe 步骤全部失败且无产物 → critical
- check_geojson_features（postflight）：产出 GeoJSON 要素数为 0 或文件缺失 → critical
"""
import json
import os
from typing import Any, Dict, List

from ..rules_gateway import RISK_CRITICAL, RISK_WARNING, Check

GEOJSON_DIR = "/home/server/python/map_assistant_v1/backend/static/geojson"

# 度单位 CRS（地理坐标系）：米制距离运算（buffer 等）不可直接使用
DEGREE_CRS = {
    "EPSG:4326", "EPSG:4490", "EPSG:4610", "EPSG:4269", "EPSG:4258",
    "CRS:84", "OGC:CRS84",
}

# 参数中可能出现 CRS 值的键
CRS_PARAM_KEYS = ("work_crs", "target_crs", "output_crs", "crs", "source_crs")


def _find_crs_values(params: Dict) -> List[str]:
    """从参数（含嵌套）中提取 CRS 值。"""
    found: List[str] = []

    def walk(obj: Any, key: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in CRS_PARAM_KEYS and isinstance(v, str):
                    found.append(v)
                walk(v, k)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, key)

    walk(params)
    return found


def check_crs_metric(params: Dict, state: Dict) -> List[Check]:
    """preflight：工作 CRS 为度单位时，缓冲区等米制运算结果不可信。"""
    checks: List[Check] = []
    for crs in _find_crs_values(params or {}):
        if crs in DEGREE_CRS:
            checks.append(Check(
                code="crs_degree", name="CRS 非米制", passed=False,
                detail=f"坐标系 {crs} 为度单位，米制距离运算（缓冲区/距离）结果不可信，"
                       f"应使用投影坐标系（如 EPSG:3857 / EPSG:4526）",
                severity=RISK_CRITICAL,
            ))
    return checks


def check_workflow_steps_failed(result: Dict, state: Dict) -> List[Check]:
    """postflight：recipe 步骤全部失败 → critical；部分失败 → warning。"""
    if not isinstance(result, dict):
        return []
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        return []
    failed = [s for s in steps if isinstance(s, dict) and not s.get("ok")]
    if not failed:
        return []
    has_output = bool(result.get("combined_geojson") or result.get("map_command"))
    if len(failed) == len(steps) and not has_output:
        return [Check(
            code="workflow_failed", name="工作流步骤全部失败", passed=False,
            detail=f"QGIS 工作流 {len(failed)}/{len(steps)} 个步骤全部失败且无产物",
            severity=RISK_CRITICAL,
        )]
    return [Check(
        code="workflow_partial", name="部分步骤失败", passed=False,
        detail=f"QGIS 工作流 {len(failed)}/{len(steps)} 个步骤失败（结果可能不完整）",
        severity=RISK_WARNING,
    )]


def check_geojson_features(result: Dict, state: Dict) -> List[Check]:
    """postflight：产出 GeoJSON 要素数为 0 或文件缺失 → critical。"""
    if not isinstance(result, dict) or not result.get("success", True):
        return []
    geo_url = result.get("combined_geojson") or ""
    if not geo_url:
        return []
    fname = os.path.basename(geo_url)
    fpath = os.path.join(GEOJSON_DIR, fname)
    if not os.path.exists(fpath):
        return [Check(
            code="geojson_missing", name="GeoJSON 文件缺失", passed=False,
            detail=f"产出文件不存在：{fname}",
            severity=RISK_CRITICAL,
        )]
    try:
        with open(fpath) as f:
            gj = json.load(f)
        features = gj.get("features", []) if isinstance(gj, dict) else []
    except (json.JSONDecodeError, OSError):
        return [Check(
            code="geojson_invalid", name="GeoJSON 解析失败", passed=False,
            detail=f"产出文件无法解析：{fname}",
            severity=RISK_CRITICAL,
        )]
    if not features:
        return [Check(
            code="geojson_empty", name="GeoJSON 要素为空", passed=False,
            detail=f"产出 GeoJSON 要素数为 0：{fname}",
            severity=RISK_CRITICAL,
        )]
    return []
