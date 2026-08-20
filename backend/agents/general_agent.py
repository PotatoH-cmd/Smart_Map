"""
GeneralAgent — 兜底 Agent。
覆盖：UNKNOWN, WEATHER_QUERY, CROSS_INTENT 等未拆分的意图。
保留完整的原始 prompt 作为兜底，确保未覆盖场景不退化。
"""
from typing import List

from .intent_types import IntentType, INTENT_DESCRIPTIONS, TOOL_INTENT_MAPPING
from .base_agent import BaseAgent


class GeneralAgent(BaseAgent):
    """兜底 Agent：使用完整的原始 165 行 prompt，保证向后兼容。"""

    @property
    def intent(self) -> IntentType:
        return IntentType.UNKNOWN

    @property
    def tool_names(self) -> List[str]:
        # 兜底 Agent 开放全部工具
        return [
            "map_tool",
            "cesium_tool",
            "location_search",
            "coordinate_marker",
            "postgresql_tool",
            "mcp_postgres_tool",
            "knowledge_base_tool",
            "data_visualizer_tool",
            "report_generator_tool",
            "weather_tool",
            "spatial_processing_tool",
            "spatial_reference_tool",
            "qgis_mcp_tool",
        ]

    def build_system_prompt(self, schema_text: str = "") -> str:
        """返回完整的原始 system prompt，与重构前完全一致。"""
        intent_list = "\n".join(
            f"- {intent.value}: {desc}"
            for intent, desc in INTENT_DESCRIPTIONS.items()
        )
        tool_list = "\n".join(
            f"- {tool}: {', '.join(str(i) if isinstance(i, str) else i.value for i in intents)}"
            for tool, intents in TOOL_INTENT_MAPPING.items()
        )
        prompt = f"""你是一个专业的意图分类与任务规划专家。

## 你的职责
1. 准确分析用户输入的意图
2. 提取关键实体（地名、数据表名、时间等）
3. 制定清晰的任务执行计划

## 意图分类体系
{intent_list}

## 工具与意图的对应关系
{tool_list}

## 业务背景
- 这是一个地图与数据分析助手系统
- 业务数据存储在 PostgreSQL 的 'ceshen' 表中
- 包含字段：Mineable_Area_Name（可采区名称）, Measured_Depth（实测高程）, Control_Elevation（控制高程）
- 核心业务规则：超深度开采 = AVG(Control_Elevation - Measured_Depth) > 2

{{DB_SCHEMA_PLACEHOLDER}}

## 注意事项
1. 优先判断是否为纯地图操作意图（map_display, location_search）
2. 数据查询必须使用 postgresql_tool
3. 知识库检索必须使用 knowledge_base_tool
4. 图表生成必须使用 data_visualizer_tool
5. 报告生成必须使用 report_generator_tool
6. 空间数据处理必须使用 spatial_processing_tool
7. 空间参考数据查询必须使用 spatial_reference_tool
8. 空间分析任务（缓冲区/裁剪/叠加/面积计算等）必须使用 qgis_mcp_tool
9. 如果涉及多个意图，按执行顺序规划任务步骤
10. execution_plan 中每个步骤的 step_id 从 1 开始递增

## 工具参数约束（必须严格遵守）
1. postgresql_tool 的参数只能使用：operation、sql、params
2. data_visualizer_tool 的参数支持：demand（必填）、sql（可选）、chart_type（可选）
3. map_tool 的 action：switch_layer、load_vector_layer、add_marker、clear_markers、set_view
4. cesium_tool 的 action：setBasemap、flyTo、addGeoJsonLayer、clearAll
5. spatial_processing_tool 支持：generate_polygon、generate_polyline、generate_points
6. qgis_mcp_tool 支持：category(必填，如analysis/processing/layer)、action(必填，如buffer/clip)、params(可选，参数对象)
7. 除非工具真实支持，否则不要臆造参数名

## 常见任务与工具选择
- 地图操作：使用 map_tool 或 cesium_tool
- 数据查询：使用 postgresql_tool
- 知识检索：使用 knowledge_base_tool
- 图表生成：使用 data_visualizer_tool
- 报告生成：使用 report_generator_tool
- 空间处理：使用 spatial_processing_tool
- 空间分析：使用 qgis_mcp_tool
- 空间参考：使用 spatial_reference_tool
"""
        if schema_text:
            prompt = prompt.replace("{DB_SCHEMA_PLACEHOLDER}", f"## 数据库详细结构\n{schema_text}")
        return prompt

    def build_response_prompt(self) -> str:
        return "你是智能助手，请根据工具执行结果回答用户问题，保持简洁专业。"
