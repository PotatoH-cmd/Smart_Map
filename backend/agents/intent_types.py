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
    SPATIAL_PROCESSING = "spatial_processing"  # 空间数据处理（坐标转换、矢量生成）
    SPATIAL_REFERENCE = "spatial_reference"      # 空间参考数据查询（红线/采区等）
    SPATIAL_ANALYSIS = "spatial_analysis"         # 空间分析（缓冲区、裁剪、叠加等）
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
    IntentType.WEATHER_QUERY: "天气查询任务：查询城市天气实况、预报、空气质量",
    IntentType.DATA_VISUALIZATION: "数据可视化任务：生成图表、统计可视化",
    IntentType.REPORT_GENERATION: "报告生成任务：生成正式报告、导出文档",
    IntentType.LOCATION_SEARCH: "位置搜索任务：查找地点坐标、地址搜索",
    IntentType.COORDINATE_MARKER: "坐标标注任务：在地图上标注特定坐标点",
    IntentType.SPATIAL_PROCESSING: "空间数据处理任务：坐标投影转换、XY交换、矢量GeoJSON生成与地图加载",
    IntentType.SPATIAL_REFERENCE: "空间参考数据查询：获取河道红线、采区边界等空间参考数据",
    IntentType.SPATIAL_ANALYSIS: "空间分析任务：缓冲区、裁剪、叠加分析、面积计算、分区统计等",
    IntentType.CROSS_INTENT: "跨意图复合任务：涉及多种操作类型的复杂任务",
    IntentType.UNKNOWN: "未知意图：无法明确分类的任务",
}


TOOL_INTENT_MAPPING = {
    "map_tool": [IntentType.MAP_DISPLAY, IntentType.LOCATION_SEARCH, IntentType.COORDINATE_MARKER],
    "cesium_tool": [IntentType.MAP_DISPLAY],  # 3D地图操作
    "location_search": [IntentType.LOCATION_SEARCH],
    "coordinate_marker": [IntentType.COORDINATE_MARKER],
    "postgresql_tool": [IntentType.DATA_QUERY],
    "knowledge_base_tool": [IntentType.KNOWLEDGE_SEARCH],
    "data_visualizer_tool": [IntentType.DATA_VISUALIZATION],
    "report_generator_tool": [IntentType.REPORT_GENERATION],
    "caisha_report_tool": [IntentType.REPORT_GENERATION],  # 采砂监测报告（一步完成）
    "weather_tool": [IntentType.WEATHER_QUERY],
    "web_search_tool": [IntentType.KNOWLEDGE_SEARCH, IntentType.UNKNOWN, IntentType.CROSS_INTENT],  # 联网搜索（实时信息）
    "spatial_processing_tool": [IntentType.SPATIAL_PROCESSING],
    "spatial_reference_tool": [IntentType.SPATIAL_REFERENCE, IntentType.DATA_QUERY, IntentType.MAP_DISPLAY, IntentType.CROSS_INTENT],
    "qgis_mcp_tool": [IntentType.SPATIAL_ANALYSIS, IntentType.SPATIAL_PROCESSING, IntentType.SPATIAL_REFERENCE],
}
