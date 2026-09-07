"""工具中台（Tool Hub）— 全部工具的唯一事实源。

四类 Provider 统一注册与调用：
- native  本地 Python 工具（backend/tools/*.py，qwen_agent @register_tool）
- mcp     通用 MCP Server 客户端（自动发现 tools）
- http    HTTP / OpenAPI 远程工具
- skill   复合技能（声明式多工具编排）
"""
from .models import ToolSpec, InvokeResult, MCPServerSpec, HTTPToolSpec, SkillStep, SkillSpec
from .registry import ToolHub

__all__ = [
    "ToolSpec", "InvokeResult", "MCPServerSpec", "HTTPToolSpec", "SkillStep", "SkillSpec",
    "ToolHub",
]
