"""
ReportAgent — 报告生成专职 Agent。
覆盖：REPORT_GENERATION
"""
from typing import List

from .intent_types import IntentType
from .base_agent import BaseAgent


class ReportAgent(BaseAgent):
    """报告生成 Agent：生成正式报告、导出文档。"""

    @property
    def intent(self) -> IntentType:
        return IntentType.REPORT_GENERATION

    @property
    def tool_names(self) -> List[str]:
        # 报告生成依赖前置的数据查询/可视化工具
        return [
            "report_generator_tool",
            "caisha_report_tool",
            "postgresql_tool",
            "data_visualizer_tool",
        ]

    def build_system_prompt(self, schema_text: str = "") -> str:
        prompt = """## 报告生成领域规则

### 何时调用
- 仅当用户明确要求"生成报告/出具报告/形成文档"时才调用 report_generator_tool
- 用户要求生成"采砂场监测报告/采砂监测分析报告"（提到砂场名或采砂许可证号）时，直接调用 caisha_report_tool（内部自动完成知识库检索、影像解译、高程评估），无需再调用其他工具
- 若会话中包含系统提示"[地图截图已保存]"，必须将其中的服务器路径作为 map_image_path 传入

### 报告生成流程
1. 先通过 postgresql_tool 或 data_visualizer_tool 获取数据
2. 检查数据充分性（数据为空则告知用户原因，跳过报告生成）
3. 调用 report_generator_tool 生成正式报告

### 工具参数
- variables 必须包含：report_title、summary、details、conclusion
- generated_date 如未传入，工具会自动填充当日日期
- 若消息中有地图截图路径，作为 map_image_path 参数传入
"""
        if schema_text:
            prompt += f"\n\n### 数据库详细结构\n{schema_text}"
        return prompt

    def build_response_prompt(self) -> str:
        return (
            "你是报告生成助手。如果报告已成功生成，请告知用户并说明主要内容。"
            "如果因数据不足导致报告未生成，请简洁告知用户原因和建议（如补充数据后重试），"
            "不要输出空数据的分析描述。"
        )
