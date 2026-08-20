"""
test_rules_gateway.py — 阶段7：RulesGateway 校验器与 fail-fast 语义。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agents.rules_gateway import (
    RulesGateway, Check, VerificationReport,
    RISK_NONE, RISK_WARNING, RISK_CRITICAL,
)


# ----------------------------------------------------------------------
# db_validators（空结果 / 聚合全零 / 知识库空）
# ----------------------------------------------------------------------

def test_check_empty_result_critical():
    from agents.validators.db_validators import check_empty_result
    checks = check_empty_result({"success": True, "data": []}, {})
    assert len(checks) == 1
    assert checks[0].code == "db_empty"
    assert checks[0].severity == RISK_CRITICAL
    assert not checks[0].passed


def test_check_empty_result_pass():
    from agents.validators.db_validators import check_empty_result
    assert check_empty_result({"success": True, "data": [{"a": 1}]}, {}) == []
    assert check_empty_result({"success": False, "error": "x"}, {}) == []


def test_check_aggregate_zero_warning():
    from agents.validators.db_validators import check_aggregate_zero
    checks = check_aggregate_zero({"success": True, "data": [{"count": 0}, {"total": "0"}]}, {})
    assert len(checks) == 1
    assert checks[0].code == "agg_zero"
    assert checks[0].severity == RISK_WARNING


def test_check_aggregate_zero_pass_with_value():
    from agents.validators.db_validators import check_aggregate_zero
    assert check_aggregate_zero({"success": True, "data": [{"count": 5}]}, {}) == []


def test_check_aggregate_zero_skips_non_dict_rows():
    from agents.validators.db_validators import check_aggregate_zero
    # 非字典行不做判断（避免误判）
    assert check_aggregate_zero({"success": True, "data": [["a", 0]]}, {}) == []


def test_check_kb_content_empty():
    from agents.validators.db_validators import check_kb_content_empty
    checks = check_kb_content_empty({"success": True, "content": "  "}, {})
    assert checks[0].code == "kb_empty"
    assert checks[0].severity == RISK_CRITICAL
    assert check_kb_content_empty({"success": True, "content": "有效内容"}, {}) == []


# ----------------------------------------------------------------------
# gis_validators（CRS 米制 / 工作流步骤 / GeoJSON 要素数）
# ----------------------------------------------------------------------

def test_check_crs_metric_degree_critical():
    from agents.validators.gis_validators import check_crs_metric
    checks = check_crs_metric({"work_crs": "EPSG:4326"}, {})
    assert len(checks) == 1
    assert checks[0].code == "crs_degree"
    assert checks[0].severity == RISK_CRITICAL


def test_check_crs_metric_nested_and_metric():
    from agents.validators.gis_validators import check_crs_metric
    # 嵌套参数中的度单位 CRS 同样命中
    checks = check_crs_metric({"params": {"output_crs": "EPSG:4490"}}, {})
    assert len(checks) == 1
    # 米制 CRS 通过
    assert check_crs_metric({"work_crs": "EPSG:3857"}, {}) == []
    assert check_crs_metric({"work_crs": "EPSG:4526"}, {}) == []


def test_check_workflow_steps_failed():
    from agents.validators.gis_validators import check_workflow_steps_failed
    # 全部失败且无产物 → critical
    result = {"steps": [{"ok": False}, {"ok": False}], "combined_geojson": None}
    checks = check_workflow_steps_failed(result, {})
    assert checks[0].code == "workflow_failed"
    assert checks[0].severity == RISK_CRITICAL
    # 部分失败 → warning
    partial = {"steps": [{"ok": False}, {"ok": True}]}
    checks = check_workflow_steps_failed(partial, {})
    assert checks[0].code == "workflow_partial"
    assert checks[0].severity == RISK_WARNING
    # 全成功 → 通过
    assert check_workflow_steps_failed({"steps": [{"ok": True}]}, {}) == []


def test_check_geojson_features(tmp_path, monkeypatch):
    from agents.validators import gis_validators
    monkeypatch.setattr(gis_validators, "GEOJSON_DIR", str(tmp_path))

    # 文件缺失 → critical
    checks = gis_validators.check_geojson_features(
        {"success": True, "combined_geojson": "http://x/none.geojson"}, {})
    assert checks[0].code == "geojson_missing"
    assert checks[0].severity == RISK_CRITICAL

    # 要素为空 → critical
    empty_f = tmp_path / "empty.geojson"
    empty_f.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    checks = gis_validators.check_geojson_features(
        {"success": True, "combined_geojson": "http://x/empty.geojson"}, {})
    assert checks[0].code == "geojson_empty"

    # 有要素 → 通过
    ok_f = tmp_path / "ok.geojson"
    ok_f.write_text(json.dumps({"type": "FeatureCollection", "features": [{"id": 1}]}))
    assert gis_validators.check_geojson_features(
        {"success": True, "combined_geojson": "http://x/ok.geojson"}, {}) == []

    # 无 geojson 输出 → 跳过
    assert gis_validators.check_geojson_features({"success": True}, {}) == []


# ----------------------------------------------------------------------
# report_validators（输出冲突 / 文件存在 / 图表 series）
# ----------------------------------------------------------------------

def test_check_output_conflict(tmp_path, monkeypatch):
    from agents.validators import report_validators
    monkeypatch.setattr(report_validators, "REPORT_DIR", str(tmp_path))
    (tmp_path / "a.docx").write_text("x")

    checks = report_validators.check_output_conflict({"filename": "a.docx"}, {})
    assert checks[0].code == "output_conflict"
    assert checks[0].severity == RISK_WARNING
    assert report_validators.check_output_conflict({"filename": "b.docx"}, {}) == []
    assert report_validators.check_output_conflict({}, {}) == []


def test_check_file_exists(tmp_path):
    from agents.validators.report_validators import check_file_exists
    f = tmp_path / "r.docx"
    f.write_text("x")

    checks = check_file_exists({"success": True, "file_path": str(tmp_path / "nope.docx")}, {})
    assert checks[0].code == "report_missing"
    assert checks[0].severity == RISK_CRITICAL

    assert check_file_exists({"success": True, "file_path": str(f)}, {}) == []
    # 旧版只回 download_url 不强判
    assert check_file_exists({"success": True, "download_url": "http://x"}, {}) == []


def test_check_chart_series_nonempty():
    from agents.validators.report_validators import check_chart_series_nonempty
    checks = check_chart_series_nonempty({"success": True, "config": {"series": []}}, {})
    assert checks[0].code == "chart_empty"
    assert checks[0].severity == RISK_CRITICAL
    ok = {"success": True, "config": {"series": [{"data": [1, 2]}]}}
    assert check_chart_series_nonempty(ok, {}) == []


# ----------------------------------------------------------------------
# VerificationReport 聚合语义（fail-fast 依据）
# ----------------------------------------------------------------------

def test_report_risk_aggregation():
    report = VerificationReport(tool="t", phase="postflight", checks=[
        Check("a", "通过", True),
        Check("b", "警告", False, detail="警告细节", severity=RISK_WARNING),
    ])
    assert not report.passed
    assert report.risk == RISK_WARNING

    report.checks.append(Check("c", "致命", False, detail="致命错误", severity=RISK_CRITICAL))
    assert report.risk == RISK_CRITICAL

    d = report.to_dict()
    assert d["errors"] == ["致命错误"]   # 仅 critical 进 errors
    assert len(d["details"]) == 2       # 所有未通过进 details


# ----------------------------------------------------------------------
# RulesGateway 集成（注册 / 执行 / 无规则返回 None / 装饰器）
# ----------------------------------------------------------------------

def test_gateway_postflight_aggregation():
    gw = RulesGateway()
    # DB 空结果：critical → 触发知识库回退（fail-fast 语义的数据来源）
    report = gw.run_postflight("postgresql_tool", {"success": True, "data": []}, {})
    assert report is not None
    assert report["risk"] == RISK_CRITICAL
    assert any(c["code"] == "db_empty" for c in report["checks"])

    # 聚合全零：warning（合法结果，不阻断）
    report = gw.run_postflight("postgresql_tool", {"success": True, "data": [{"count": 0}]}, {})
    assert report["risk"] == RISK_WARNING

    # 正常数据：全部通过
    report = gw.run_postflight("postgresql_tool", {"success": True, "data": [{"count": 5}]}, {})
    assert report["passed"] is True
    assert report["risk"] == RISK_NONE


def test_gateway_preflight_crs():
    gw = RulesGateway()
    report = gw.run_preflight("qgis_mcp_tool", {"work_crs": "EPSG:4326"}, {})
    assert report["risk"] == RISK_CRITICAL
    report = gw.run_preflight("qgis_mcp_tool", {"work_crs": "EPSG:3857"}, {})
    assert report["passed"] is True


def test_gateway_unregistered_tool_returns_none():
    gw = RulesGateway()
    assert gw.run_postflight("weather_tool", {"success": True}, {}) is None
    assert gw.run_preflight("weather_tool", {}, {}) is None


def test_gateway_decorator_registration():
    gw = RulesGateway()

    @gw.postflight("fake_tool")
    def fake_check(result, state):
        return [Check("fake", "假校验", False, severity=RISK_CRITICAL)]

    report = gw.run_postflight("fake_tool", {"success": True}, {})
    assert report["risk"] == RISK_CRITICAL
    assert report["checks"][0]["code"] == "fake"


def test_gateway_rule_error_degrades_to_warning():
    gw = RulesGateway()

    @gw.postflight("boom_tool")
    def boom(result, state):
        raise RuntimeError("校验器自身崩溃")

    report = gw.run_postflight("boom_tool", {}, {})
    # 规则异常不阻断：降级为 warning 级未通过检查
    assert report["risk"] == RISK_WARNING
    assert any(c["code"] == "rule_error" for c in report["checks"])
