"""
合约测试 — 验证 Agent Harness 重构后核心接口和数据结构不变。
"""
import sys
import os

# 确保 backend 在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_tool_registry_creates_all_tools():
    """验证 ToolRegistry 能正确创建所有 14 个工具（含 tile_publish_tool、qgis_mcp_tool）。"""
    from agents.tool_registry import ToolRegistry
    registry = ToolRegistry()

    expected_tools = [
        "map_tool", "location_search", "coordinate_marker", "cesium_tool",
        "postgresql_tool", "mcp_postgres_tool", "knowledge_base_tool",
        "data_visualizer_tool", "report_generator_tool", "weather_tool",
        "spatial_processing_tool", "spatial_reference_tool",
        "tile_publish_tool", "qgis_mcp_tool",
    ]
    for name in expected_tools:
        tool = registry.create(name)
        assert tool is not None, f"工具 {name} 创建失败"


def test_tool_registry_names():
    """验证 ToolRegistry.names() 返回完整列表。"""
    from agents.tool_registry import ToolRegistry
    registry = ToolRegistry()
    names = registry.names()
    assert len(names) >= 14
    assert "map_tool" in names
    assert "postgresql_tool" in names


def test_base_agent_subclasses():
    """验证所有 Agent 子类正确实现了 BaseAgent 接口。"""
    from agents.base_agent import BaseAgent
    from agents.map_agent import MapAgent
    from agents.data_agent import DataAgent
    from agents.knowledge_agent import KnowledgeAgent
    from agents.report_agent import ReportAgent
    from agents.general_agent import GeneralAgent

    for agent_cls in [MapAgent, DataAgent, KnowledgeAgent, ReportAgent, GeneralAgent]:
        agent = agent_cls()
        assert isinstance(agent.intent, str) or hasattr(agent.intent, "value")
        assert isinstance(agent.tool_names, list)
        assert len(agent.tool_names) > 0
        prompt = agent.build_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 20
        resp_prompt = agent.build_response_prompt()
        assert isinstance(resp_prompt, str)
        assert len(resp_prompt) > 5


def test_agent_harness_dispatch():
    """验证 AgentHarness 按意图正确分派。"""
    from agents.agent_harness import AgentHarness
    from agents.intent_types import IntentType, IntentResult
    from agents.map_agent import MapAgent
    from agents.data_agent import DataAgent
    from agents.general_agent import GeneralAgent

    harness = AgentHarness({"model": "qwen-plus", "model_server": "http://localhost"})

    # MAP_DISPLAY → MapAgent
    result = IntentResult(
        primary_intent=IntentType.MAP_DISPLAY,
        confidence=0.9,
        entities=[],
        task_context="test",
        execution_plan=[],
    )
    assert isinstance(harness.dispatch(result), MapAgent)

    # DATA_QUERY → DataAgent
    result.primary_intent = IntentType.DATA_QUERY
    assert isinstance(harness.dispatch(result), DataAgent)

    # UNKNOWN → GeneralAgent (fallback)
    result.primary_intent = IntentType.UNKNOWN
    assert isinstance(harness.dispatch(result), GeneralAgent)


def test_fast_route_keywords():
    """验证快速路由关键词命中。"""
    from agents.agent_harness import AgentHarness
    from agents.intent_types import IntentType

    harness = AgentHarness({"model": "qwen-plus", "model_server": "http://localhost"})

    assert harness.try_fast_classify("切换到卫星图层") == IntentType.MAP_DISPLAY
    assert harness.try_fast_classify("生成报告") == IntentType.REPORT_GENERATION
    assert harness.try_fast_classify("生成图表") == IntentType.DATA_VISUALIZATION
    assert harness.try_fast_classify("红线附近") == IntentType.SPATIAL_REFERENCE
    assert harness.try_fast_classify("坐标转换") == IntentType.SPATIAL_PROCESSING
    assert harness.try_fast_classify("缓冲区分析") == IntentType.SPATIAL_ANALYSIS
    assert harness.try_fast_classify("这是普通闲聊") is None


def test_intent_types_exports():
    """验证 intent_types 关键导出不变。"""
    from agents.intent_types import IntentType, IntentResult, TaskStep, INTENT_DESCRIPTIONS, TOOL_INTENT_MAPPING

    assert len(list(IntentType)) >= 12
    assert "map_display" in [e.value for e in IntentType]
    assert len(INTENT_DESCRIPTIONS) >= 12
    assert len(TOOL_INTENT_MAPPING) >= 12


def test_harness_build_prompts():
    """验证 Harness 能为各意图生成专用 prompt。"""
    from agents.agent_harness import AgentHarness
    from agents.intent_types import IntentType, IntentResult

    harness = AgentHarness({"model": "qwen-plus", "model_server": "http://localhost"})

    for intent in [IntentType.MAP_DISPLAY, IntentType.DATA_QUERY, IntentType.KNOWLEDGE_SEARCH,
                   IntentType.REPORT_GENERATION, IntentType.UNKNOWN]:
        result = IntentResult(
            primary_intent=intent, confidence=0.9, entities=[],
            task_context="", execution_plan=[],
        )
        sys_prompt = harness.build_system_prompt_for(result)
        assert "意图分类" in sys_prompt or "职责" in sys_prompt
        resp_prompt = harness.build_response_prompt_for(result)
        assert len(resp_prompt) > 5


if __name__ == "__main__":
    tests = [
        test_tool_registry_creates_all_tools,
        test_tool_registry_names,
        test_base_agent_subclasses,
        test_agent_harness_dispatch,
        test_fast_route_keywords,
        test_intent_types_exports,
        test_harness_build_prompts,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} 测试通过")
