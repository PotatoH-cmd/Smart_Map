"""
test_param_schema.py — Phase 1：工具参数 Schema（转换 / 修复 / 校验 / 消费）。

覆盖（纯内存，不发网络请求）：
1. parameters → JSON Schema 转换（list / JSON-Schema 风格 dict / enum / required）
2. coerce 确定性修复（数值字符串→数字、标量→数组、None 剔除）
3. validate：required/type/enum 为 blocking（error），未声明参数为 warning
4. ToolSpec.json_schema() 与 missing_required_for_prompt（机器参数不反问）
5. Registry：enforce 阻断（工具未派发）/ warn 仅记录不阻断
6. ToolNodeMixin._check_tool_params：真实工具类属性（weather_tool）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.toolhub.models import ToolSpec
from core.toolhub.param_validate import (
    MODE_ENFORCE,
    blocking_issues,
    coerce_params,
    missing_required_for_prompt,
    normalize_parameters,
    params_to_json_schema,
    validate_against,
    validate_params,
    verification_report,
)
from core.toolhub.registry import ToolHub


PARAMS = [
    {"name": "operation", "type": "string", "description": "操作",
     "enum": ["query", "execute"], "required": True},
    {"name": "limit", "type": "integer", "description": "条数", "required": False},
    {"name": "tags", "type": "array", "description": "标签", "required": False},
]


# ----------------------------------------------------------------------
# 转换
# ----------------------------------------------------------------------

def test_params_to_json_schema_conversion():
    schema = params_to_json_schema(PARAMS)
    assert schema == {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "description": "操作", "enum": ["query", "execute"]},
            "limit": {"type": "integer", "description": "条数"},
            "tags": {"type": "array", "description": "标签", "items": {}},
        },
        "required": ["operation"],
    }
    # JSON-Schema 风格 dict 原样返回（同一对象）
    jss = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    assert params_to_json_schema(jss) is jss
    # JSON-Schema 风格 dict 归一化为参数列表（required 对齐）
    norm = normalize_parameters({"properties": {"a": {"type": "number"}}, "required": ["a"]})
    assert norm == [{"name": "a", "type": "number", "required": True}]
    # 空/异常输入安全降级
    assert params_to_json_schema(None) == {"type": "object", "properties": {}, "required": []}
    assert normalize_parameters("oops") == []
    assert normalize_parameters([{"type": "string"}]) == []  # 无 name 的条目丢弃


# ----------------------------------------------------------------------
# 修复
# ----------------------------------------------------------------------

def test_coerce_params():
    schema = params_to_json_schema(PARAMS)
    out = coerce_params({"operation": "query", "limit": "5", "tags": "a", "junk": None}, schema)
    # "5"→5；"a"→["a"]；None 剔除（等价未提供）
    assert out == {"operation": "query", "limit": 5, "tags": ["a"]}
    # 数字→字符串（schema 声明 string 时）
    assert coerce_params({"operation": 123}, schema) == {"operation": "123"}
    # 非 dict 输入
    assert coerce_params("not-dict", schema) == {}
    # 无法修复的保持原样（交给 validate 报错）
    assert coerce_params({"limit": "abc"}, schema) == {"limit": "abc"}


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------

def test_validate_blocking_and_warning():
    schema = params_to_json_schema(PARAMS)
    assert validate_params({"operation": "query", "limit": 5}, schema) == []
    # 缺必填 → blocking
    issues = validate_params({"limit": 5}, schema)
    assert blocking_issues(issues)[0]["code"] == "required"
    assert "operation" in blocking_issues(issues)[0]["message"]
    # 类型错 → blocking
    issues = validate_params({"operation": "query", "limit": "5"}, schema)
    assert blocking_issues(issues)[0]["code"] == "type"
    # 非法枚举 → blocking
    issues = validate_params({"operation": "drop"}, schema)
    assert blocking_issues(issues)[0]["code"] == "enum"
    # 未声明参数 → warning（不阻断）
    issues = validate_params({"operation": "query", "x_extra": 1}, schema)
    assert blocking_issues(issues) == []
    assert issues[0]["code"] == "unknown" and issues[0]["level"] == "warning"


def test_validate_against_skips_undeclared_tools():
    params, issues = validate_against([], {"anything": 1})
    assert params == {"anything": 1} and issues == []


def test_toolspec_json_schema():
    spec = ToolSpec(name="t", parameters=PARAMS)
    assert spec.json_schema()["properties"]["limit"]["type"] == "integer"
    assert ToolSpec(name="empty").json_schema()["properties"] == {}


def test_missing_required_for_prompt():
    declared = [
        {"name": "site_name", "type": "string", "description": "砂场名称", "required": True},
        {"name": "operation", "type": "string", "required": True},   # 机器参数：跳过
        {"name": "note", "type": "string", "required": False},
    ]
    miss = missing_required_for_prompt(declared, {})
    assert [m["param"] for m in miss] == ["site_name"]
    assert miss[0]["label"] == "砂场名称"
    # 已提供不反问；空串视为缺失
    assert missing_required_for_prompt(declared, {"site_name": "郝楼砂场"}) == []
    assert [m["param"] for m in missing_required_for_prompt(declared, {"site_name": ""})] == ["site_name"]


def test_verification_report_shape():
    issues = validate_params({"operation": "drop", "z_extra": 1}, params_to_json_schema(PARAMS))
    rep = verification_report("t", issues)
    assert rep["phase"] == "param_schema" and rep["risk"] == "critical"
    assert rep["passed"] is False
    codes = {c["code"] for c in rep["checks"]}
    assert {"enum", "unknown"} <= codes
    assert rep["errors"]  # blocking 文案存在


# ----------------------------------------------------------------------
# Registry：enforce 阻断 / warn 放行
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def registry():
    return ToolHub()


def _fake_spec():
    return ToolSpec(
        name="_fake_param_tool", provider="native",
        parameters=[{"name": "count", "type": "integer", "description": "数量", "required": True}],
    )


def test_registry_enforce_blocks_before_dispatch(registry, monkeypatch):
    monkeypatch.setenv("PARAM_VALIDATE_MODE", MODE_ENFORCE)
    registry._specs["_fake_param_tool"] = _fake_spec()
    try:
        out = registry.invoke("_fake_param_tool", {"count": "abc"})
        assert out["success"] is False
        assert "参数校验失败" in out["error"]
        assert out["_verification"]["risk"] == "critical"
        # 可修复参数（"3"→3）→ 通过校验进入派发（native 类不存在 → 报错与校验无关）
        out2 = registry.invoke("_fake_param_tool", {"count": "3"})
        assert "参数校验失败" not in (out2.get("error") or "")
    finally:
        registry._specs.pop("_fake_param_tool", None)


def test_registry_warn_mode_does_not_block(registry, monkeypatch):
    monkeypatch.delenv("PARAM_VALIDATE_MODE", raising=False)  # 默认 warn
    registry._specs["_fake_param_tool"] = _fake_spec()
    try:
        out = registry.invoke("_fake_param_tool", {"count": "abc"})
        assert "参数校验失败" not in (out.get("error") or "")  # 仅告警，继续派发
    finally:
        registry._specs.pop("_fake_param_tool", None)


# ----------------------------------------------------------------------
# ToolNodeMixin：真实工具类属性校验（weather_tool）
# ----------------------------------------------------------------------

def test_mixin_check_tool_params_real_tool(monkeypatch):
    from agents.nodes.tool_node import ToolNodeMixin

    class Dummy(ToolNodeMixin):
        pass

    d = Dummy()
    monkeypatch.setenv("PARAM_VALIDATE_MODE", MODE_ENFORCE)
    # 可修复参数：字符串数字 → 整型，校验通过
    coerced, failure = d._check_tool_params("weather_tool", {"city": "郑州", "forecast_days": "3"})
    assert failure is None and coerced["forecast_days"] == 3
    # 缺必填 city → enforce 阻断，附同构报告
    _, failure2 = d._check_tool_params("weather_tool", {"forecast_days": 3})
    assert failure2 is not None and "city" in failure2["error"]
    assert failure2["_verification"]["risk"] == "critical"
    # warn（默认）不阻断
    monkeypatch.delenv("PARAM_VALIDATE_MODE", raising=False)
    _, failure3 = d._check_tool_params("weather_tool", {"forecast_days": 3})
    assert failure3 is None
