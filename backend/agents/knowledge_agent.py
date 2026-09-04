"""
KnowledgeAgent — 知识检索专职 Agent。
覆盖：KNOWLEDGE_SEARCH
"""
from typing import List

from .intent_types import IntentType
from .base_agent import BaseAgent


class KnowledgeAgent(BaseAgent):
    """知识检索 Agent：政策查询、流程咨询、文档查找。"""

    @property
    def intent(self) -> IntentType:
        return IntentType.KNOWLEDGE_SEARCH

    @property
    def tool_names(self) -> List[str]:
        return ["knowledge_base_tool"]

    def build_system_prompt(self, schema_text: str = "") -> str:
        return """## 知识检索领域规则

- 政策/流程/操作文档/规范类问题：必须使用 knowledge_base_tool(operation='search') 检索知识库
- **禁止**对此类问题查询数据库（postgresql_tool）
- 检索到的知识库内容优先于数据库结果回答用户
"""

    def build_response_prompt(self) -> str:
        return (
            "你是知识库检索助手，请将检索结果整理成清晰易读的回答。"
            "回答内容必须基于检索结果，禁止编造。"
            "关键结论后需用括号标注来源文档名，如（来源：《2023年度罗山县采砂区监测评估意见》）。"
            "若检索结果不足以回答，如实告知并建议用户换个问法或补充文档。"
        )
