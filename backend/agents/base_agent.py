"""
BaseAgent — 所有专职 Agent 的抽象基类。
每个 Agent 自持提示词、工具集、响应生成逻辑。
"""
from abc import ABC, abstractmethod
from typing import List

from .intent_types import IntentType


class BaseAgent(ABC):
    """Agent 抽象基类。

    每个子类声明：
    - intent: 该 Agent 处理的主意图
    - tool_names: 需要的工具名列表
    - build_system_prompt(): 意图识别用的 system prompt 片段
    - build_response_prompt(): 结果汇总用的 response prompt
    """

    @property
    @abstractmethod
    def intent(self) -> IntentType:
        """该 Agent 覆盖的主意图类型。"""
        ...

    @property
    @abstractmethod
    def tool_names(self) -> List[str]:
        """该 Agent 依赖的工具名列表。"""
        ...

    @abstractmethod
    def build_system_prompt(self, schema_text: str = "") -> str:
        """返回意图识别阶段注入的 system prompt 片段。

        不应包含完整的意图分类体系——只包含该领域的工具约束和业务规则。
        """
        ...

    @abstractmethod
    def build_response_prompt(self) -> str:
        """返回结果汇总阶段的 response 提示词。"""
        ...
