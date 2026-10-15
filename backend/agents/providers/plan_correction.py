"""计划纠正策略注册表：把 intent_node 里针对各业务主题的"确定性兑底"
（重写 / 裁剪 LLM 规划的 execution_plan）抽离为独立可扩展的 Provider。

沿用 extract_node 的 DeterministicAnswerProvider 思路，但作用于**计划**而非回答：
节点只负责按注册顺序逐个执行纠正，命中一个应用一个（后续可能被更晚的纠正覆盖，
与原节点串行覆盖语义完全一致）。
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PlanCorrectionProvider:
    """计划纠正策略基类。

    correct(user_msg, intent_result)：
      - 针对当前用户消息与（可能已被前序纠正改写的）意图结果做判断；
      - 命中返回描述如何覆盖意图结果的 dict，未命中返回 None。
    返回 dict 可用字段（均可省略，省略即不改动该项）：
      {
        "primary_intent": IntentType|None,   # 覆盖意图
        "execution_plan": [TaskStep, ...],   # 覆盖计划
        "task_context":  str,                 # 覆盖任务上下文
        "confidence":    float,               # 覆盖置信度
        "description":   str,                 # 供节点日志
      }
    注意：实现不应直接改写 intent_result，统一由 _apply 应用，保证可观测与可回滚。
    """

    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        raise NotImplementedError


CORRECTION_REGISTRY: List[PlanCorrectionProvider] = []


def register_correction(provider_cls):
    """类装饰器：实例化并注册一个计划纠正 Provider（注册顺序即执行顺序）。"""
    CORRECTION_REGISTRY.append(provider_cls())
    return provider_cls


def _apply(intent_result, correction: Dict):
    if correction.get("primary_intent") is not None:
        intent_result.primary_intent = correction["primary_intent"]
    if "execution_plan" in correction:
        intent_result.execution_plan = correction["execution_plan"]
    if correction.get("task_context") is not None:
        intent_result.task_context = correction["task_context"]
    if correction.get("confidence") is not None:
        intent_result.confidence = correction["confidence"]


def apply_corrections(user_msg: str, intent_result) -> List[str]:
    """依注册顺序对 intent_result 应用全部命中的纠正，原样保留"后纠正覆盖前纠正"语义。

    返回各命中纠正的 description，供节点写入日志。
    """
    logs: List[str] = []
    for provider in CORRECTION_REGISTRY:
        try:
            correction = provider.correct(user_msg, intent_result)
        except Exception:
            logger.exception(
                "plan correction %s failed, skipping", provider.__class__.__name__
            )
            continue
        if not correction:
            continue
        _apply(intent_result, correction)
        logs.append(
            correction.get("description") or provider.__class__.__name__
        )
    return logs