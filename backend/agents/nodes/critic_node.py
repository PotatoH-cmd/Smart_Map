"""评审节点：summarize ⇄ critic 闭环（打回 → 清空 response → summarize 修正重生成）。

设计见《ReAct子图与Critic评审设计方案.md》§5.4：
  - should_review 否 / 评审异常 / 通过 → return {}（路由去 END，零状态变更）；
  - 打回时清空 response 并写 _critic_feedback，由 summarize_node 消费后清空（防死循环）；
  - 循环安全三层保护：①入口轮数守卫（_critic_round > CRITIC_MAX_ROUNDS → 放行）；
    ②打回前修正配额检查（rounds+1 > max → 保守放行，保留最新版回答）；
    ③summarize_node 生成后必清空 _critic_feedback。
    默认 CRITIC_MAX_ROUNDS=1 ⇒ 最多"生成 → 评审 → 修正 → 评审 → 结束"两次评审、一次修正。
"""
import asyncio
import logging
from typing import Any, Dict

from langgraph.graph import END

from ..state import AgentState

logger = logging.getLogger(__name__)


class CriticNodeMixin:
    async def _critic_node(self, state: AgentState) -> Dict[str, Any]:
        """图内质量评审（单轮）：放行返回空更新；打回清空 response 并携带修正反馈。"""
        critic = self.critic
        response_text = state.get("response", "")
        if not critic.should_review(
            state.get("intent_result"), state.get("tool_results"), response_text
        ):
            return {}

        rounds = int(state.get("_critic_round") or 0)
        if rounds > critic.max_rounds:
            logger.warning(f"[Critic] 评审轮数超限（{rounds}>{critic.max_rounds}），放行")
            return {}

        # 评审在最终阶段，评测调用放线程池执行，不阻塞事件循环
        loop = asyncio.get_event_loop()
        verdict = await loop.run_in_executor(
            None,
            lambda: critic.review(
                user_message=state.get("user_message", ""),
                intent_result=state.get("intent_result"),
                response_text=response_text,
                tool_results=state.get("tool_results"),
            ),
        )
        if verdict is None or verdict.get("passed"):
            return {}

        # 打回前检查修正配额：已用满 → 保守放行（保留最新版回答，防死循环）
        if rounds + 1 > critic.max_rounds:
            logger.warning(f"[Critic] 已达最大修正轮数（{critic.max_rounds}），放行最新版回答")
            return {}

        logger.info(f"[Critic] 评审不通过 issues={verdict.get('issues')}，打回修正")
        return {
            "response": "",  # 清空，触发 summarize_node 重新生成
            "_critic_feedback": verdict.get("feedback") or "",
            "_critic_issues": verdict.get("issues") or [],
            "_critic_round": rounds + 1,
        }

    def _route_after_critic(self, state: AgentState) -> str:
        """打回（带待消费反馈）→ 回 summarize 修正；否则结束本轮图执行。"""
        if state.get("_critic_feedback"):
            return "summarize_node"
        return END
