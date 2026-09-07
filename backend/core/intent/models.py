"""意图服务数据模型（泛化版：intent 为 str，无业务枚举依赖）。"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class GenericTaskStep(BaseModel):
    step_id: int = Field(..., description="步骤序号")
    action: str = Field(..., description="执行动作")
    tool: Optional[str] = Field(None, description="需要调用的工具（工具中台注册名）")
    params: dict = Field(default_factory=dict, description="工具参数")
    reasoning: str = Field(..., description="执行该步骤的推理过程")
    expected_output: str = Field(..., description="期望的输出结果")


class GenericIntentResult(BaseModel):
    primary_intent: str = Field(..., description="主要意图（意图目录中的 key）")
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度")
    entities: List[str] = Field(default_factory=list, description="提取的实体列表")
    task_context: str = Field(default="", description="任务上下文摘要")
    execution_plan: List[GenericTaskStep] = Field(default_factory=list, description="执行计划")
    requires_confirmation: bool = Field(False, description="是否需要用户确认")
    suggestions: List[str] = Field(default_factory=list, description="补充建议")


class AnalyzeContext(BaseModel):
    view: Optional[str] = Field(None, description="前端当前视图（'map'|'cesium'），地图类约束二选一注入")
    extra: Dict[str, Any] = Field(default_factory=dict,
                                  description="调用方注入的额外上下文：db_schema / facts_context 等")


class AnalyzeRequest(BaseModel):
    message: str = Field(..., description="用户消息（可含调用方预拼的上下文前缀）")
    history: List[Dict[str, Any]] = Field(default_factory=list, description="对话历史 [{role,content}]")
    context: AnalyzeContext = Field(default_factory=AnalyzeContext)
