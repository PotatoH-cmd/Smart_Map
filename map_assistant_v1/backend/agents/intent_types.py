from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class IntentType(str, Enum):
    MAP_DISPLAY = "map_display"
    DATA_QUERY = "data_query"
    KNOWLEDGE_SEARCH = "knowledge_search"
    DATA_VISUALIZATION = "data_visualization"
    REPORT_GENERATION = "report_generation"
    CROSS_INTENT = "cross_intent"
    LOCATION_SEARCH = "location_search"
    COORDINATE_MARKER = "coordinate_marker"
    WEATHER_QUERY = "weather_query"
    UNKNOWN = "unknown"


class SubIntent(str, Enum):
    MAP_DISPLAY = "map_display"
    LOAD_VECTOR_LAYER = "load_vector_layer"
    SWITCH_LAYER = "switch_layer"
    CLEAR_MARKERS = "clear_markers"

    DB_SCHEMA_QUERY = "db_schema_query"
    DB_DATA_QUERY = "db_data_query"
    DB_STATISTICS = "db_statistics"

    KB_SEARCH = "kb_search"
    KB_ADD = "kb_add"
    KB_LIST = "kb_list"

    CHART_BAR = "chart_bar"
    CHART_LINE = "chart_line"
    CHART_PIE = "chart_pie"
    CHART_SCATTER = "chart_scatter"

    REPORT_GENERATE = "report_generate"

    LOCATION_SEARCH = "location_search"
    COORDINATE_MARKER = "coordinate_marker"


class TaskStep(BaseModel):
    step_id: int = Field(..., description="步骤序号")
    action: str = Field(..., description="执行动作")
    tool: Optional[str] = Field(None, description="需要调用的工具")
    params: dict = Field(default_factory=dict, description="工具参数")
    reasoning: str = Field(..., description="执行该步骤的推理过程")
    expected_output: str = Field(..., description="期望的输出结果")


class IntentResult(BaseModel):
    primary_intent: IntentType = Field(..., description="主要意图")
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度")
    entities: List[str] = Field(default_factory=list, description="提取的实体列表")
    task_context: str = Field(..., description="任务上下文摘要")
    execution_plan: List[TaskStep] = Field(default_factory=list, description="执行计划")
    requires_confirmation: bool = Field(False, description="是否需要用户确认")
    suggestions: List[str] = Field(default_factory=list, description="补充建议")

    class Config:
        use_enum_values = True


INTENT_DESCRIPTIONS = {
    IntentType.MAP_DISPLAY: "地图展示类任务：加载矢量数据、切换底图、添加标记等",
    IntentType.DATA_QUERY: "数据查询类任务：数据库查询、统计分析、数据对比",
    IntentType.KNOWLEDGE_SEARCH: "知识检索类任务：政策查询、流程咨询、文档查找",
    IntentType.DATA_VISUALIZATION: "数据可视化任务：生成图表、统计可视化",
    IntentType.REPORT_GENERATION: "报告生成任务：生成正式报告、导出文档",
    IntentType.LOCATION_SEARCH: "位置搜索任务：查找地点坐标、地址搜索",
    IntentType.COORDINATE_MARKER: "坐标标注任务：在地图上标注特定坐标点",
    IntentType.CROSS_INTENT: "跨意图复合任务：涉及多种操作类型的复杂任务",
    IntentType.UNKNOWN: "未知意图：无法明确分类的任务",
}


TOOL_INTENT_MAPPING = {
    "map_tool": [IntentType.MAP_DISPLAY, IntentType.LOCATION_SEARCH, IntentType.COORDINATE_MARKER],
    "location_search": [IntentType.LOCATION_SEARCH],
    "coordinate_marker": [IntentType.COORDINATE_MARKER],
    "postgresql_tool": [IntentType.DATA_QUERY],
    "knowledge_base_tool": [IntentType.KNOWLEDGE_SEARCH],
    "data_visualizer_tool": [IntentType.DATA_VISUALIZATION],
    "report_generator_tool": [IntentType.REPORT_GENERATION],
    "weather_tool": [IntentType.WEATHER_QUERY],
}
