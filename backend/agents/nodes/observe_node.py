"""观察节点：非流式图内 ReAct 循环（工具结果 → LLM 观察 → 追加单步或 finish）。

设计要点（见《ReAct子图与Critic评审设计方案.md》§4）：
  - 自环不回 tool_node：tool_node 每次进入都会重跑全量计划（无幂等保护），
    本节点只执行"自己追加的单步"，由 _route_after_observe 依据 react_last_added
    决定是否再来一轮（轮数由 react_rounds 计数，上限 REACT_MAX_ROUNDS）；
  - 领域白名单动态化：领域 Agent tool_names ∩ DEFAULT_READONLY_TOOLS，
    空集直接 finish（地图类等意图零额外开销）；
  - reducer 安全：tool_results 是 Annotated[List, operator.add]，本节点只返回
    本轮新增的那一条结果（部分更新），严禁返回全量列表（会翻倍）。
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from ..intent_types import IntentResult
from ..react_loop import ReActObserver, DEFAULT_READONLY_TOOLS
from ..state import AgentState

logger = logging.getLogger(__name__)


class ObserveNodeMixin:
    async def _observe_node(self, state: AgentState) -> Dict[str, Any]:
        """ReAct 观察（单轮）：满足条件则 LLM 决策追加一步并执行，否则交由路由转出。

        返回部分更新 dict（reducer 安全）；非 reducer 字段按覆盖语义更新。
        """
        intent_result: Optional[IntentResult] = state.get("intent_result")
        if intent_result is None:
            return {"react_last_added": False}

        observer = self._observer_for_intent(intent_result)
        if observer is None or not observer.enabled:
            return {"react_last_added": False}

        rounds = int(state.get("react_rounds") or 0)
        if rounds >= observer.max_rounds:
            return {"react_last_added": False}

        results = list(state.get("tool_results") or [])
        user_message = state.get("user_message", "")

        # LLM 观察（dashscope 同步调用 → executor 内执行，避免阻塞事件循环）
        loop = asyncio.get_event_loop()
        new_step = await loop.run_in_executor(
            None,
            lambda: observer.observe(
                user_message=user_message,
                ordered_results=results,
                round_i=rounds + 1,
            ),
        )
        if new_step is None:
            return {"react_last_added": False}

        logger.info(
            f"[observe_node] r{rounds + 1} 追加步骤: {new_step.tool} params={new_step.params}"
        )
        try:
            result = await self._invoke_single_step(state, new_step)
        except Exception as e:
            # 追加步骤执行异常：记录失败结果并终止观察循环（主流程继续走 extract）
            logger.warning(f"[observe_node] 追加步骤执行失败（停止观察循环）: {e}")
            return {
                "tool_results": [{"step_id": new_step.step_id, "tool_name": new_step.tool,
                                  "result": {"success": False, "error": str(e)}}],
                "react_rounds": rounds + 1,
                "react_last_added": False,
            }

        # reducer 安全：只携带本轮新增的一条结果，由 operator.add 合并进总列表
        item = {"step_id": new_step.step_id, **result}
        if getattr(new_step, "tool_call_id", None):
            item["tool_call_id"] = new_step.tool_call_id
        return {
            "tool_results": [item],
            "react_rounds": rounds + 1,
            "react_last_added": True,
        }

    def _route_after_observe(self, state: AgentState) -> str:
        """观察后路由：刚追加了步骤 → 自环再来一轮；否则进入结果抽取。"""
        if state.get("react_last_added"):
            return "observe_node"
        return "extract_node"

    def _observer_for_intent(self, intent_result: IntentResult) -> Optional[ReActObserver]:
        """按领域白名单构建观察器：领域 Agent tool_names ∩ 只读安全集。

        空集返回 None（observe 直接 finish）——地图类等意图零额外开销。
        """
        try:
            domain_tools = set(self.harness.get_tool_names(intent_result))
        except Exception as e:
            logger.warning(f"[observe_node] 领域工具解析失败，跳过观察: {e}")
            return None
        allowed = domain_tools & DEFAULT_READONLY_TOOLS
        if not allowed:
            return None
        return ReActObserver(
            summarize_fn=self._summarize_tool_result,
            allowed_tools=allowed,
        )
