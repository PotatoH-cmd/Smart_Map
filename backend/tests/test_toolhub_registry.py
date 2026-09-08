"""
test_toolhub_registry.py — P2：工具中台 Registry 目录语义。

覆盖（纯内存，不发网络请求）：
1. Native 工具默认启用（enabled 缺省 True，避免 hub 重启后目录失联）。
2. MCP 工具默认禁用（远端 server 未部署时不阻塞主链路）。
3. 控制台开关 set_enabled 覆盖默认值，reset 后恢复 YAML 语义。
4. 目录过滤：include_disabled=False 时隐藏禁用工具。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.toolhub.registry import ToolHub


@pytest.fixture(scope="module")
def registry():
    return ToolHub()


def _by_name(reg, name):
    for spec in reg.catalog(include_disabled=True):
        if spec.name == name:
            return spec
    return None


def test_native_enabled_by_default(registry):
    """weather_tool 是 native 扫描工具，YAML 未写 enabled → 默认启用。"""
    spec = _by_name(registry, "weather_tool")
    assert spec is not None and spec.provider == "native"
    assert spec.enabled is True
    assert registry.is_enabled("weather_tool") is True


def test_mcp_disabled_by_default(registry):
    """MCP 远端工具默认禁用（QGIS server 未部署时不报错、不进目录）。"""
    mcp_specs = [s for s in registry.catalog(include_disabled=True) if s.provider == "mcp"]
    if not mcp_specs:
        pytest.skip("当前环境无 MCP server 声明")
    assert all(s.enabled is False for s in mcp_specs)


def test_console_toggle_overrides_default(registry):
    """set_enabled 内存开关覆盖 YAML 默认，且影响目录可见性。"""
    registry.set_enabled("weather_tool", False)
    assert registry.is_enabled("weather_tool") is False
    visible = {s.name for s in registry.catalog(include_disabled=False)}
    assert "weather_tool" not in visible
    # 恢复
    registry.set_enabled("weather_tool", True)
    assert registry.is_enabled("weather_tool") is True
    visible = {s.name for s in registry.catalog(include_disabled=False)}
    assert "weather_tool" in visible


def test_disabled_tool_rejected_on_invoke(registry):
    """禁用工具的统一调用入口直接拒绝（统一调用契约）。"""
    registry.set_enabled("weather_tool", False)
    out = registry.invoke("weather_tool", {"city": "郑州"})
    assert out.get("success") is False and "禁用" in (out.get("error") or "")
    registry.set_enabled("weather_tool", True)
