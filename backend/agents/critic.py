"""critic.py — 公共评审组件：触发判定 + 语义评审 + 保守放行（异常降级）。

设计见《ReAct子图与Critic评审设计方案.md》§5：
  - should_review：纯确定性规则（开关 / 长度上限 / 意图或工具命中），不调 LLM；
  - review：dashscope 同步评审，单行 JSON 契约 {"passed","severity","issues","feedback"}；
  - 保守放行（宁漏检不错杀）：信息不足 / 模型失败 / 超时 / JSON 异常 → None（调用方视为放行）。

与 RulesGateway 的分工（§5.6）：网关管"操作安全"（结构化校验），critic 管"表达质量"（语义评审），
两层正交、互不替代。

环境变量（调用时读取，便于测试 monkeypatch）：
  CRITIC_ENABLED / CRITIC_MODEL / CRITIC_MAX_ROUNDS / CRITIC_TIMEOUT / CRITIC_MAX_RESPONSE_CHARS
"""
import json
import logging
import os
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 触发评审的意图（报告/知识类需要表达质量把关；操作性意图零开销直通）
_REVIEW_INTENTS = {"report_generation", "knowledge_search"}
# 命中即评审的工具（成功产出才算，覆盖 cross_intent 生成报告等场景）
_REVIEW_TOOLS = {"report_generator_tool", "caisha_report_tool", "knowledge_base_tool"}


class ResponseCritic:
    """领域无关的回答评审器：确定性触发 + 语义评审 + 保守放行。"""

    def __init__(self, summarize_fn: Optional[Callable[[str, Dict], str]] = None):
        # summarize_fn：工具结果摘要（与 ReAct 观察同一经济口径），None 时退化为原文截断
        self._summarize_fn = summarize_fn

    # ── 配置读取（调用时求值，不固化；便于测试 monkeypatch 环境变量）──

    @property
    def enabled(self) -> bool:
        return os.environ.get("CRITIC_ENABLED", "1") == "1"

    @property
    def model(self) -> str:
        return os.environ.get("CRITIC_MODEL", "qwen-flash")

    @property
    def max_rounds(self) -> int:
        """最大修正轮数（1 = 生成→评审→修正→评审→结束）。"""
        try:
            return int(os.environ.get("CRITIC_MAX_ROUNDS", "1"))
        except (TypeError, ValueError):
            return 1

    @property
    def timeout(self) -> int:
        try:
            return int(os.environ.get("CRITIC_TIMEOUT", "12"))
        except (TypeError, ValueError):
            return 12

    @property
    def max_response_chars(self) -> int:
        """超长响应跳过评审（防 prompt 爆炸）。"""
        try:
            return int(os.environ.get("CRITIC_MAX_RESPONSE_CHARS", "6000"))
        except (TypeError, ValueError):
            return 6000

    # ── 触发判定（确定性，不调 LLM）──

    def should_review(self, intent_result, tool_results, response_text: str) -> bool:
        """同时满足才评审：开关开启 + 响应非空且不超长 + 命中触发范围（意图或工具）。"""
        if not self.enabled:
            return False
        text = response_text or ""
        if not text.strip():
            return False
        if len(text) > self.max_response_chars:
            logger.info(f"[Critic] 响应超长（{len(text)}>{self.max_response_chars} 字符），跳过评审")
            return False

        # 意图命中
        primary = getattr(intent_result, "primary_intent", None)
        primary_val = primary.value if hasattr(primary, "value") else str(primary or "")
        if primary_val in _REVIEW_INTENTS:
            return True

        # 工具命中（成功产出的报告 / 知识检索）
        for item in (tool_results or []):
            if not isinstance(item, dict):
                continue
            if item.get("tool_name") not in _REVIEW_TOOLS:
                continue
            result = item.get("result")
            if isinstance(result, dict) and result.get("success") is True:
                return True
        return False

    # ── 语义评审（同步 dashscope；异常/超时 → None 放行）──

    def review(self, *, user_message: str, intent_result, response_text: str,
               tool_results: List[Dict], run_id: Optional[str] = None) -> Optional[Dict]:
        """评审草拟回答。返回 {"passed","severity","issues","feedback"}；异常 → None（放行）。"""
        try:
            results_text = self._summarize_results(tool_results)
            prompt = (
                "你是回答质量评审员。对照工具执行结果，评审草拟回答是否可以直接发给用户。\n"
                "评审维度：\n"
                "1. 事实一致性：数字、名称、许可号、结论是否与工具结果矛盾或凭空出现；\n"
                "2. 来源标注：引用知识库具体结论时是否标注来源；\n"
                "3. 完整性：是否直接回答用户问题（报告类是否说明生成结果或跳过原因）；\n"
                "4. 无过度承诺：不虚构系统不具备的能力。\n"
                "只输出一行 JSON，不要解释：\n"
                '- 通过：{"passed": true, "severity": "ok", "issues": [], "feedback": ""}\n'
                '- 不通过：{"passed": false, "severity": "major", "issues": ["问题1"], '
                '"feedback": "给撰写者的具体修正指令"}\n'
                "若信息不足以判断，一律输出通过（宁漏检不错杀）；feedback 只给修正指令，不要提及评审过程。\n"
                f"\n\n【用户问题】{(user_message or '')[:600]}\n\n"
                f"【工具结果】\n{results_text}\n\n"
                f"【草拟回答】\n{(response_text or '')[:self.max_response_chars]}"
            )

            import dashscope
            resp = dashscope.Generation.call(
                model=self.model,
                api_key=os.environ.get("DASHSCOPE_API_KEY"),
                messages=[{"role": "user", "content": prompt}],
                result_format="message",
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.warning(f"[Critic] 评审 LLM 失败: {getattr(resp, 'message', resp)}")
                return None
            raw = resp.output.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").lstrip("json").strip()
            verdict = json.loads(raw)

            # 可观测性：评审上报（仅流式路径传入 run_id；失败不影响主流程）
            if run_id:
                try:
                    from core.obs import obs_generation
                    obs_generation(
                        run_id=run_id, name="critic_review", model=self.model,
                        prompt=prompt, completion=verdict, usage=None,
                    )
                except Exception:
                    pass

            # 契约归一化（缺字段按保守语义处理：passed 缺失 = 放行）
            passed = bool(verdict.get("passed", True))
            severity = str(verdict.get("severity") or ("ok" if passed else "major"))
            issues = [str(i) for i in (verdict.get("issues") or []) if str(i).strip()][:5]
            feedback = str(verdict.get("feedback") or "")[:1000]
            if not passed and not feedback:
                # 打回但无修正指令 → 保守放行（无法修正的打回无意义）
                logger.warning(f"[Critic] 打回但 feedback 为空，保守放行 issues={issues}")
                return {"passed": True, "severity": severity, "issues": issues, "feedback": ""}
            return {"passed": passed, "severity": severity, "issues": issues, "feedback": feedback}
        except Exception as e:
            logger.warning(f"[Critic] 评审失败（放行）: {e}")
            return None

    # ── 内部：工具结果摘要（与 ReAct 观察同一经济口径：最近 8 条、每条 ≤300 字）──

    def _summarize_results(self, tool_results: List[Dict]) -> str:
        lines = []
        for r in (tool_results or [])[-8:]:
            if not isinstance(r, dict):
                continue
            tool = r.get("tool_name", "unknown")
            res = r.get("result", {})
            if self._summarize_fn and isinstance(res, dict):
                summary = self._summarize_fn(tool, res)
            else:
                summary = str(res)
            lines.append(f"步骤{r.get('step_id', '?')} [{tool}]: {str(summary)[:300]}")
        return "\n".join(lines) or "（暂无结果）"
