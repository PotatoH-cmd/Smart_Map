"""
report_validators.py — 产出物校验（report_generator_tool / data_visualizer_tool）。

- check_output_conflict（preflight）：输出文件名已存在 → warning（将被覆盖）
- check_file_exists（postflight）：success=True 但报告文件缺失 → critical
- check_chart_series_nonempty（postflight）：图表 config 的 series 为空 → critical
"""
import os
from typing import Any, Dict, List

from ..rules_gateway import RISK_CRITICAL, RISK_WARNING, Check

REPORT_DIR = "/home/server/python/map_assistant_v1/backend/static/reports"


def check_output_conflict(params: Dict, state: Dict) -> List[Check]:
    """preflight：报告输出文件名已存在时提示覆盖风险。"""
    if not isinstance(params, dict):
        return []
    filename = params.get("filename") or params.get("output_filename") or ""
    if not filename:
        return []
    if os.path.exists(os.path.join(REPORT_DIR, filename)):
        return [Check(
            code="output_conflict", name="输出文件已存在", passed=False,
            detail=f"同名报告已存在，本次生成将覆盖：{filename}",
            severity=RISK_WARNING,
        )]
    return []


def check_file_exists(result: Dict, state: Dict) -> List[Check]:
    """postflight：报告声称成功但文件不存在 → critical（前端将无法下载）。"""
    if not isinstance(result, dict) or not result.get("success"):
        return []
    file_path = result.get("file_path") or ""
    if not file_path:
        # 旧版工具可能只回 download_url，不强判
        return []
    if not os.path.exists(file_path):
        return [Check(
            code="report_missing", name="报告文件缺失", passed=False,
            detail=f"报告声称生成成功但文件不存在：{os.path.basename(file_path)}",
            severity=RISK_CRITICAL,
        )]
    return []


def check_chart_series_nonempty(result: Dict, state: Dict) -> List[Check]:
    """postflight：图表 config 的 series 为空 → critical（前端渲染不出图）。"""
    if not isinstance(result, dict) or not result.get("success"):
        return []
    config = result.get("config") or {}
    if not isinstance(config, dict):
        return []
    series = config.get("series")
    if isinstance(series, list) and not series:
        return [Check(
            code="chart_empty", name="图表数据为空", passed=False,
            detail="图表 series 为空，前端无法渲染",
            severity=RISK_CRITICAL,
        )]
    return []
