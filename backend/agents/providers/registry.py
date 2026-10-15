"""确定性回答策略注册表：把"工具结果足以给出权威回答时绕过 LLM"的领域特化逻辑
从通用节点（如 extract_node）中抽离，收敛为一个个独立可扩展的策略 Provider。

用法：子类继承 DeterministicAnswerProvider 并用 @register 注册，
extract_node 只需调用 resolve() 遍历所有 Provider，命中即返回确定性回答。
"""
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class DeterministicAnswerProvider:
    """确定性回答策略基类。

    子类实现 get_answer：
      - 输入 agent_state 与 tool_results；
      - 当该领域能给出确定/权威回答时返回回答文本，否则返回 None 交由 LLM 常规汇总。
    """

    def get_answer(self, state: Dict, tool_results: List[Dict]) -> Optional[str]:
        raise NotImplementedError


REGISTRY: List[DeterministicAnswerProvider] = []


def register(provider_cls):
    """类装饰器：实例化并注册一个确定性回答 Provider（按注册顺序遍历，先命中先返回）。"""
    REGISTRY.append(provider_cls())
    return provider_cls


def resolve(state: Dict, tool_results: List[Dict]) -> Optional[str]:
    """遍历注册表，返回第一个可用的确定性回答，全部不命中返回 None。"""
    for provider in REGISTRY:
        try:
            answer = provider.get_answer(state, tool_results)
        except Exception:
            logger.exception(
                "deterministic provider %s failed, falling back to LLM",
                provider.__class__.__name__,
            )
            continue
        if answer:
            return answer
    return None