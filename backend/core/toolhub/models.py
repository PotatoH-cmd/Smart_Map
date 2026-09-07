"""工具中台数据模型。

ToolSpec 是中台对外的统一工具描述：
- native / http / skill / mcp 四类 Provider 都产出 ToolSpec
- 意图服务、主服务、前端控制台均以 ToolSpec 为唯一目录格式
"""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str = Field(..., description="工具唯一注册名")
    provider: Literal["native", "mcp", "http", "skill"] = "native"
    description: str = ""
    parameters: List[Dict[str, Any]] = Field(default_factory=list,
                                             description="与 qwen_agent 工具 parameters 同构：[{name,type,description,required,...}]")
    intents: List[str] = Field(default_factory=list, description="关联意图标签（替代 TOOL_INTENT_MAPPING）")
    keywords: List[str] = Field(default_factory=list, description="关键词路由词表（替代 TOOL_KEYWORD_ROUTES）")
    priority: int = Field(100, description="关键词路由优先级，越小越先（替代列表顺序敏感）")
    constraint: str = Field("", description="按需注入意图分析的参数约束段（替代 TOOL_CONSTRAINT_SNIPPETS）")
    view_bound: bool = Field(False, description="地图类视图绑定：2D/3D 二选一注入")
    excludes: List[str] = Field(default_factory=list, description="互斥：本工具命中后不再注入这些工具的约束段")
    config: Dict[str, Any] = Field(default_factory=dict, description="Provider 私有配置")
    enabled: bool = Field(True, description="是否启用（禁用后不进入目录与调用）")

    def brief(self) -> Dict[str, Any]:
        """目录列表用的精简视图。"""
        return {
            "name": self.name,
            "provider": self.provider,
            "description": self.description,
            "intents": self.intents,
            "keywords": self.keywords,
            "priority": self.priority,
            "enabled": self.enabled,
        }


class InvokeResult(BaseModel):
    """统一调用结果契约：{success, data, message}（与现有工具约定一致）。"""
    success: bool = True
    data: Any = None
    message: str = ""
    error: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


class MCPServerSpec(BaseModel):
    """tools.yaml 中声明的 MCP Server。"""
    name: str
    transport: Literal["http", "streamable-http", "sse", "stdio"] = "http"
    url: str = ""                                   # http / streamable-http / sse
    command: List[str] = Field(default_factory=list)  # stdio
    env: Dict[str, str] = Field(default_factory=dict)  # stdio
    timeout: float = 10.0
    expose: bool = Field(True, description="是否将自动发现的 tools 暴露进目录（false 时仅做状态探测）")
    include: List[str] = Field(default_factory=list, description="只暴露这些远端工具（空=全部）")
    exclude: List[str] = Field(default_factory=list)
    alias: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="远端工具名 → 元数据覆盖，key 可用 '本地注册名'（如 qgis_mcp_tool）实现旧名兼容")
    tools_meta: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="远端工具名 → {description,intents,keywords,...} 元数据补充")


class HTTPToolSpec(BaseModel):
    """tools.yaml 中声明的 HTTP/OpenAPI 工具。"""
    name: str
    endpoint: str
    method: Literal["GET", "POST"] = "POST"
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0
    params_in: Literal["json", "query"] = "json"


class SkillStep(BaseModel):
    tool: str
    params: Dict[str, Any] = Field(default_factory=dict)
    optional: bool = Field(False, description="失败时是否继续后续步骤")


class SkillSpec(BaseModel):
    """skills.yaml 中声明的复合技能：对外表现为一个普通工具。"""
    name: str
    description: str = ""
    parameters: List[Dict[str, Any]] = Field(default_factory=list)
    intents: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    priority: int = 10
    constraint: str = ""
    excludes: List[str] = Field(default_factory=list)
    steps: List[SkillStep] = Field(default_factory=list)
    prompt_hint: str = ""
    fail_fast: bool = Field(True, description="非 optional 步骤失败后是否终止")
