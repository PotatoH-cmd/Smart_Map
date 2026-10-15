"""知识库回退策略注册表：把 tool_node `_fallback_knowledge_search` 里针对业务主题的
定制化回退逻辑（定向检索 + 检索词覆盖）抽离为独立可扩展的 Provider。

节点只负责依注册顺序调度：先跑定向检索（命中即在空库判断前提前返回），
再在空库回退时读取检索词覆盖。两个钩子均异常隔离。
"""
import logging
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class KbFallbackProvider:
    """知识库回退策略基类。

    - targeted_search：方案/主题定向检索；需在空库判断前执行并可直接返回结果时
      返回（可追加到 tool_results 的列表），否则返回 None。注入依赖：
      invoke_step 执行工具步骤、llm_factory 创建读数用的 LLM。
    - query_for：空库回退用的 KB 检索词覆盖；无覆盖返回 None。
    """

    async def targeted_search(
        self,
        user_msg: str,
        pre_results: List[Dict],
        *,
        invoke_step: Callable,
        llm_factory: Callable,
    ) -> Optional[List[Dict]]:
        return None

    def query_for(self, user_msg: str) -> Optional[str]:
        return None


KB_FALLBACK_REGISTRY: List[KbFallbackProvider] = []


def register_kb_fallback(provider_cls):
    """类装饰器：实例化并注册一个知识库回退 Provider。"""
    KB_FALLBACK_REGISTRY.append(provider_cls())
    return provider_cls


async def resolve_kb_fallback(
    user_msg: str,
    pre_results: List[Dict],
    *,
    invoke_step: Callable,
    llm_factory: Callable,
) -> Tuple[Optional[List[Dict]], Optional[str]]:
    """依注册顺序收集：定向检索命中结果（第一个非空，命中即提前返回）与检索词覆盖。

    返回 (targeted_results|None, custom_query|None)。异常隔离：单 Provider 失败不阻断其余。
    """
    early: Optional[List[Dict]] = None
    custom_query: Optional[str] = None
    for provider in KB_FALLBACK_REGISTRY:
        try:
            targeted = await provider.targeted_search(
                user_msg, pre_results, invoke_step=invoke_step, llm_factory=llm_factory
            )
            if targeted:
                early = targeted
                break
            q = provider.query_for(user_msg)
            if q:
                custom_query = q
                break
        except Exception:
            logger.exception(
                "kb fallback provider %s failed, skipping", provider.__class__.__name__
            )
            continue
    return early, custom_query