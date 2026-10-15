"""确定性/规则化策略包。

把散落在各节点里的业务主题特化逻辑抽为独立 Provider，经"每机制一个注册表"统一调度：
- DeterministicAnswerProvider（providers/registry.py）：extract 侧确定性回答。
- PlanCorrectionProvider（providers/plan_correction.py）：intent 侧计划纠正。
- KbFallbackProvider（providers/kb_fallback.py）：tool 侧知识库回退定制。

各 Provider 实现模块顶部导入即自动注册；节点只消费各注册表的调度入口。
"""
from .registry import DeterministicAnswerProvider, register, resolve
from .plan_correction import PlanCorrectionProvider, register_correction, apply_corrections
from .kb_fallback import KbFallbackProvider, register_kb_fallback, resolve_kb_fallback
from . import sandfield  # noqa: F401  导入以触发注册
from . import intent_corrections  # noqa: F401
from . import sandfield_kb  # noqa: F401

__all__ = [
    "DeterministicAnswerProvider", "register", "resolve",
    "PlanCorrectionProvider", "register_correction", "apply_corrections",
    "KbFallbackProvider", "register_kb_fallback", "resolve_kb_fallback",
    "sandfield", "intent_corrections", "sandfield_kb",
]