import logging
from typing import List, Dict, Any, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from .intent_types import (
    IntentType,
    IntentResult,
    INTENT_DESCRIPTIONS,
    TOOL_INTENT_MAPPING,
)
from tools.schema_manager import SchemaManager

logger = logging.getLogger(__name__)


class IntentAgent:
    """
    意图识别 Agent。
    使用 LangChain ChatOpenAI + with_structured_output 实现结构化意图分析，
    替代原 qwen_agent.agents.Assistant 继承方式。
    """

    def __init__(self, llm_cfg: Dict[str, Any]):
        self.llm_cfg = llm_cfg

        # 兼容 qwen-agent 格式的 LLM_CFG（model_server / base_url 二选一）
        base_url = llm_cfg.get("model_server") or llm_cfg.get("base_url")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "qwen-plus")

        llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0.1,
        )
        # 结构化输出：直接输出 IntentResult Pydantic 模型
        self.structured_llm = llm.with_structured_output(IntentResult)
        self._schema_injected = False
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        intent_list = "\n".join(
            f"- {intent.value}: {desc}"
            for intent, desc in INTENT_DESCRIPTIONS.items()
        )
        tool_list = "\n".join(
            f"- {tool}: {', '.join(str(i) if isinstance(i, str) else i.value for i in intents)}"
            for tool, intents in TOOL_INTENT_MAPPING.items()
        )
        return f"""你是一个专业的意图分类与任务规划专家。

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
6. 空间数据处理（坐标转换、投影变换、矢量生成）必须使用 spatial_processing_tool
   - 当用户说"生成矢量范围"、"转换坐标"、"XY相反"、"投影坐标系"、"CGCS2000"、"带号"等关键词时，意图应为 spatial_processing
   - 典型场景：用户上传图片/Excel 或输入坐标，要求生成矢量并加载到地图
   - **重要**：spatial_processing_tool 会自动生成 GeoJSON 并返回 load_vector_layer 指令加载到地图，因此使用了 spatial_processing_tool 后，**不要再使用 map_tool 执行 load_vector_layer 操作**，避免重复加载。
7. 如果涉及多个意图，按执行顺序规划任务步骤
8. execution_plan 中每个步骤的 step_id 从 1 开始递增

## 工具参数约束（必须严格遵守）
1. `postgresql_tool` 的参数只能使用：`operation`、`sql`、`params`
   - 查询语句必须写成：`{{"operation": "query", "sql": "SELECT ...", "params": []}}`
   - 获取表结构必须写成：`{{"operation": "get_db_schema"}}`
   - 绝对不要输出 `{{"query": "SELECT ..."}}` 这种错误格式。
   - **计数统计规则（极其重要）**：ceshen 表每行是一个测量点（经纬度坐标），不是采区！
     当用户问"多少个XX"（如砂场、采区、批复采区）时，必须使用 `COUNT(DISTINCT "Mineable_Area_Name")`
     而不是 `COUNT(*)`！COUNT(*) 会计数所有测量点位，给出错误的大数值。
     正确示例：`SELECT COUNT(DISTINCT "Mineable_Area_Name") AS count FROM ceshen WHERE "County_District"='固始县'`
     错误示例：~~`SELECT COUNT(*) FROM ceshen WHERE "County_District"='固始县'`~~（这会返回测量点数而非采区数）
2. `data_visualizer_tool` 的参数支持：`{{"demand": "完整制图需求", "sql": "SELECT ...", "chart_type": "bar"}}`
   - `demand`（必填）：包含地区、指标、图表类型、筛选条件等完整要求。
   - `sql`（可选，强烈推荐）：针对需求生成的 PostgreSQL 查询语句。如果你能根据数据库结构生成准确的 SQL，请填写此字段，可大幅提高执行效率。
   - `chart_type`（可选）：图表类型，可选 'bar'、'line'、'pie'、'scatter'。用户明确指定或可从需求推断时填写。
   - SQL 生成规则：字段名含大写字母必须用双引号包裹，表名通常为 'ceshen'。除非用户明确要求 TOP-N，否则不要在 SQL 中加 LIMIT，应返回全部满足条件的数据。
3. `map_tool`（2D地图）的 action 类型：
   - 切换底图：action="switch_layer"，layer="satellite"（卫星影像）、"arcgis"（高分影像）或 "osm"（街道地图）
   - 加载矢量图层：action="load_vector_layer"，需提供 table_name、filter、layer_name
   - 添加标记：action="add_marker"，需提供 lat、lng
   - 清除标记：action="clear_markers"
   - 设置视图：action="set_view"，需提供 lat、lng、zoom
4. `cesium_tool`（3D地图）的 action 类型：
   - 切换底图：action="setBasemap"，basemap="satellite"（卫星）、"osm"（街道）或 "tianditu"（天地图）
   - 飞行到位置：action="flyTo"，需提供 lat、lng
   - 添加GeoJSON图层：action="addGeoJsonLayer"，需提供 table_name
   - 清除所有：action="clearAll"
5. `spatial_processing_tool`（空间数据处理）的参数：
   - 生成面：`{{"action": "generate_polygon", "coordinates": [[x1,y1],[x2,y2],...], "source_crs": "4526"}}`
   - 生成线：`{{"action": "generate_polyline", ...}}`
   - 生成点：`{{"action": "generate_points", ...}}`
   - XY交换：当用户说"XY相反"时，设置 `"swap_xy": true`
   - 坐标系对照：CGCS2000 Zone 38N = EPSG:4526（推荐），也可用 EPSG:4547；WGS84 = EPSG:4326
   - ⚠️ EPSG:4497 是 6 度带第 19 带（CM 111°E），**不是** 3 度带第 38 带，切勿混淆
   - 当用户提到"38带号"、"CGCS2000"、"2000国家大地坐标系"、"投影坐标"时，source_crs 应为 "4526"
   - ⚠️ **坐标顺序**：`coordinates` 应为 [[easting, northing], ...] 即 [[Y含带号, X], ...]，easting 值通常约 38M，northing 值约 3.5M
   - ⚠️ 从 OCR 文本/Excel 中提取坐标时：必须提取**全部**坐标点，不要遗漏任何一个角点
   - ⚠️ 如果坐标提取不完整或转换失败，**不要猜测替代方案**（如 cesium_tool），直接告知用户错误信息和建议
6. 除非工具真实支持，否则不要臆造参数名。

## ⚠️ execution_plan 填写规范（极其重要）
- execution_plan 中每一步的 `tool` 字段**必须**填写对应工具名称字符串，**绝对不能**留空或为 null。
- 只要是地图操作（切换图层、加载矢量、添加标记等），`tool` 必须填 `"map_tool"` 或 `"cesium_tool"`。
- 示例（切换卫星图层）：
  ```json
  {{
    "step_id": 1,
    "action": "切换到卫星底图",
    "tool": "map_tool",
    "params": {{"action": "switch_layer", "layer": "satellite"}},
    "reasoning": "用户要求切换到卫星图层",
    "expected_output": "地图底图切换为卫星影像"
  }}
  ```
- 示例（加载矢量图层）：
  ```json
  {{
    "step_id": 1,
    "action": "加载采区矢量数据",
    "tool": "map_tool",
    "params": {{"action": "load_vector_layer", "table_name": "ceshen", "filter": "\\\"Mineable_Area_Name\\\"='种子场可采区'", "layer_name": "种子场可采区"}},
    "reasoning": "用户要求加载采区数据到地图",
    "expected_output": "矢量图层加载到地图上"
  }}
  ```

## 常见任务与工具选择
- "切换到卫星图层"：使用 map_tool（2D）或 cesium_tool（3D）的底图切换操作，tool 字段必须填写
- "切换到街道图"：同上，layer/osm
- "加载采区数据"：使用 map_tool 或 cesium_tool 的加载矢量图层操作，tool 字段必须填写
- "查找某地位置"：使用 location_search 工具，tool 字段填 "location_search"
- "清除地图"：使用 map_tool，action="clear_markers"，tool 字段填 "map_tool"
- "XY相反/投影坐标/带号/生成矢量"：使用 spatial_processing_tool。用户上传图片/Excel/输入坐标，要求生成矢量范围时使用，tool 字段填 "spatial_processing_tool\""""

    def _inject_schema_once(self):
        """首次调用时将真实 DB schema 注入 system prompt。"""
        if self._schema_injected:
            return
        try:
            sm = SchemaManager.instance()
            schema_text = sm.get_formatted_schema()
            if schema_text:
                self.system_prompt = self.system_prompt.replace(
                    "{DB_SCHEMA_PLACEHOLDER}",
                    f"## 数据库详细结构\n{schema_text}"
                )
            else:
                self.system_prompt = self.system_prompt.replace("{DB_SCHEMA_PLACEHOLDER}", "")
        except Exception as e:
            logger.warning(f"Failed to inject DB schema into IntentAgent prompt: {e}")
            self.system_prompt = self.system_prompt.replace("{DB_SCHEMA_PLACEHOLDER}", "")
        self._schema_injected = True

    def analyze(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> IntentResult:
        """分析用户消息，返回结构化 IntentResult。"""
        self._inject_schema_once()
        try:
            analysis_prompt = self._build_analysis_prompt(user_message, chat_history)
            messages = [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=analysis_prompt),
            ]
            result = self.structured_llm.invoke(messages)
            if isinstance(result, IntentResult):
                return result
            return self._create_unknown_result("结构化输出类型异常")
        except Exception as e:
            logger.error(f"Intent analysis error: {e}")
            return self._create_unknown_result(str(e))

    def _build_analysis_prompt(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> str:
        history_context = ""
        if chat_history:
            recent_msgs = chat_history[-6:]
            history_context = "\n\n## 最近对话历史\n" + "\n".join(
                f"- {msg.get('role', 'unknown')}: {msg.get('content', '')[:100]}"
                for msg in recent_msgs
                if msg.get("role") in ["user", "assistant"]
            )

        return f"""## 用户当前输入
{user_message}
{history_context}

请分析上述用户输入，返回结构化的意图分析结果。"""

    def _create_unknown_result(self, reason: str) -> IntentResult:
        return IntentResult(
            primary_intent=IntentType.UNKNOWN,
            confidence=0.0,
            entities=[],
            task_context=f"无法分析意图: {reason}",
            execution_plan=[],
            requires_confirmation=True,
            suggestions=["请尝试重新描述您的需求"],
        )

    def get_required_tools(self, intent_result: IntentResult) -> List[str]:
        """从 execution_plan 中提取需要调用的工具列表（去重、保序）。"""
        seen = set()
        tools = []
        for step in intent_result.execution_plan:
            if step.tool and step.tool not in seen:
                seen.add(step.tool)
                tools.append(step.tool)
        return tools
