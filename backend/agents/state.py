"""LangGraph 状态与 QwenTool 适配器（自 task_executor.py 机械搬移）。"""
import logging
import operator
import json
from typing import Annotated, List, Dict, Any, Optional, TypedDict

from .intent_types import IntentResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LangGraph 状态定义
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    user_message: str
    chat_history: List[Dict]
    intent_result: Optional[IntentResult]
    tool_results:     Annotated[List[Dict], operator.add]   # 并行写入自动合并
    tool_summaries:   List[str]                              # extract_node → summarize_node 传递
    response: str
    map_commands:     Annotated[List[Dict], operator.add]
    cesium_commands:  Annotated[List[Dict], operator.add]
    charts:           Annotated[List[Dict], operator.add]
    report_url: Optional[str]
    error: Optional[str]


# ---------------------------------------------------------------------------
# QwenTool 适配器：将 BaseTool 实例包装为可直接调用的函数
# ---------------------------------------------------------------------------

class QwenToolAdapter:
    """将 qwen_agent BaseTool 实例包装，提供统一的 invoke(params: dict) 接口。"""

    def __init__(self, tool_instance, tool_name: str):
        self.tool = tool_instance
        self.name = tool_name

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.tool.call(params)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = {"success": True, "content": result}
            return {"tool_name": self.name, "result": result}
        except Exception as e:
            logger.error(f"Tool {self.name} invoke error: {e}")
            return {"tool_name": self.name, "result": {"success": False, "error": str(e)}}

