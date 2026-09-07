"""意图服务（Intent Service）— 通用意图识别。

不含任何业务枚举：意图目录来自 config/intents.yaml + 工具中台的 ToolSpec 元数据。
"""
from .models import AnalyzeRequest, AnalyzeContext, GenericIntentResult, GenericTaskStep

__all__ = ["AnalyzeRequest", "AnalyzeContext", "GenericIntentResult", "GenericTaskStep"]
