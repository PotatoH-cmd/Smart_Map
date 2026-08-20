"""
DataAgent — 数据查询与可视化专职 Agent。
覆盖：DATA_QUERY, DATA_VISUALIZATION
"""
from typing import List

from .intent_types import IntentType
from .base_agent import BaseAgent


class DataAgent(BaseAgent):
    """数据分析 Agent：数据库查询、统计分析、图表生成。"""

    @property
    def intent(self) -> IntentType:
        return IntentType.DATA_QUERY

    @property
    def tool_names(self) -> List[str]:
        return [
            "postgresql_tool",
            "mcp_postgres_tool",
            "data_visualizer_tool",
        ]

    def build_system_prompt(self, schema_text: str = "") -> str:
        prompt = f"""## 数据查询领域规则

### 业务背景
- 业务数据存储在 PostgreSQL 的 'ceshen' 表中
- 包含字段：Mineable_Area_Name（可采区名称）, Measured_Depth（实测高程）, Control_Elevation（控制高程）
- 核心业务规则：超深度开采 = AVG(Control_Elevation - Measured_Depth) > 2
- **术语规范**：Measured_Depth 是"实测高程"（海拔高度），**严禁**称为"实测深度"

### 工具参数约束
1. `postgresql_tool` 的参数只能使用：`operation`、`sql`、`params`
   - 查询语句必须写成：{{"operation": "query", "sql": "SELECT ...", "params": []}}
   - 获取表结构必须写成：{{"operation": "get_db_schema"}}
   - 绝对不要输出 {{"query": "SELECT ..."}} 这种错误格式
   - **计数统计规则（极其重要）**：ceshen 表每行是一个测量点，不是采区！
     当用户问"多少个XX"（如砂场、采区、批复采区）时，必须使用 COUNT(DISTINCT "Mineable_Area_Name")
     而不是 COUNT(*)！COUNT(*) 会计数所有测量点位，给出错误的大数值
   - 字段名含大写字母必须用双引号包裹，如 "Mineable_Area_Name"、"Measured_Depth"
   - 完整可采区/砂场名称（含"可采区"或"砂场"）：filter 必须用等值匹配，禁止 LIKE

2. `data_visualizer_tool` 的参数支持：{{"demand": "完整制图需求", "sql": "SELECT ...", "chart_type": "bar"}}
   - `demand`（必填）：包含地区、指标、图表类型、筛选条件等完整要求
   - `sql`（可选，强烈推荐）：针对需求生成的 PostgreSQL 查询语句
   - `chart_type`（可选）：图表类型，可选 'bar'、'line'、'pie'、'scatter'
   - SQL 生成规则：字段名含大写字母必须用双引号包裹，表名通常为 'ceshen'
   - 除非用户明确要求 TOP-N，否则不要在 SQL 中加 LIMIT

### 超深度开采判定
- 整体判定公式：AVG("Control_Elevation" - "Measured_Depth") > 2（米）
- 当且仅当整个区域的**平均差值超过 2m** 才定义为超深度开采
- 若平均差值 ≤ 2m，即使个别点位超深，也**不得**定性为超深度开采
- 差值 > 2m：回复应为"{{区域}}存在超深度开采。平均实测高程比控制高程低 {{diff}}m，超过 2m 允许范围。"
- 差值 ≤ 2m：回复应为"{{区域}}未构成超深度开采。整体平均实测高程符合控制要求。"
"""
        if schema_text:
            prompt += f"\n\n### 数据库详细结构\n{schema_text}"
        return prompt

    def build_response_prompt(self) -> str:
        return (
            "你是数据分析助手，正在汇总数据库查询结果。"
            "数据表为 'ceshen'，业务规则：超深度开采 = AVG(Control_Elevation - Measured_Depth) > 2。"
            "回答要包含具体数据。"
            "重要：当工具执行结果中包含'数据结果（前 10 条）'时，请优先使用该 JSON 数据中的具体数值来回答，"
            "不要仅依据'返回 N 条记录'这一行数描述。聚合查询（如 COUNT）只返回 1 行是正常现象，"
            "实际统计值在该行的字段中。"
        )
