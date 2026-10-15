"""react_loop.py — 公共 ReAct 观察循环：工具结果 → LLM 观察 → 追加步骤或 finish。

抽取自 run_engine._react_observe（行为逐字保留），两条执行路径共用：
  - 流式：RunEngine._react_observe 薄包装（allowed_tools=None → 全局只读安全集）
  - 非流式：图 observe_node（allowed_tools = 领域 Agent tool_names ∩ 只读安全集）

行为基线（与抽取前逐条一致）：
  1. 短路省调用：无知识库参与、全部结果实质成功 → finish（不调 LLM）
  2. 观察输入：用户问题（≤600 字）+ 最近 8 条结果摘要（每条 ≤300 字）
  3. LLM：dashscope 原生调用，REACT_OBSERVE_MODEL（默认 qwen-flash），timeout 10s，单行 JSON
  4. 白名单 + 参数校验，非法决策 → finish（记 warning）
  5. 任何异常 → finish（降级原流程，主流程不受影响）
  6. obs_generation 埋点（仅传入 run_id 时）

环境变量（调用时读取，便于测试 monkeypatch）：
  REACT_LOOP_ENABLED / REACT_MAX_ROUNDS / REACT_OBSERVE_MODEL / REACT_ALLOWED_TOOLS
"""
import json
import logging
import os
from typing import Callable, Dict, List, Optional, Set

from .intent_types import TaskStep
from core.obs import obs_generation

logger = logging.getLogger(__name__)

# 全局只读安全集：ReAct 允许追加的工具（只读查询类，防止任意操作）
DEFAULT_READONLY_TOOLS: Set[str] = {
    "knowledge_base_tool",
    "postgresql_tool",
    "web_search_tool",
}


class ReActObserver:
    """领域无关的 ReAct 观察器（单轮决策），领域差异经构造参数注入。

    - summarize_fn：工具结果摘要函数，签名 (tool_name, result_dict) -> str
    - allowed_tools：允许追加的工具集合；None → REACT_ALLOWED_TOOLS env → 全局安全集
    - max_rounds / model：None → 读环境变量（REACT_MAX_ROUNDS / REACT_OBSERVE_MODEL）
    - prompt_extra：追加到观察提示词的领域附加约束（默认空，提示词与基线逐字一致）
    """

    def __init__(
        self,
        summarize_fn: Callable[[str, Dict], str],
        allowed_tools: Optional[Set[str]] = None,
        max_rounds: Optional[int] = None,
        model: Optional[str] = None,
        prompt_extra: str = "",
    ):
        self._summarize_fn = summarize_fn
        self._allowed_tools = allowed_tools
        self._max_rounds = max_rounds
        self._model = model
        self._prompt_extra = prompt_extra

    # ── 配置读取（调用时求值，不固化；便于测试 monkeypatch 环境变量）──

    @property
    def enabled(self) -> bool:
        return os.environ.get("REACT_LOOP_ENABLED", "1") == "1"

    @property
    def fc_enabled(self) -> bool:
        """Phase 3：原生 tool_call 观测模式（REACT_FC_MODE=1；失败自动回退 JSON 决策）。"""
        return os.environ.get("REACT_FC_MODE", "0") == "1"

    @property
    def max_rounds(self) -> int:
        if self._max_rounds is not None:
            return self._max_rounds
        try:
            return int(os.environ.get("REACT_MAX_ROUNDS", "3"))
        except (TypeError, ValueError):
            return 3

    @property
    def model(self) -> str:
        return self._model or os.environ.get("REACT_OBSERVE_MODEL", "qwen-flash")

    @property
    def allowed_tools(self) -> Set[str]:
        if self._allowed_tools is not None:
            return set(self._allowed_tools)
        env = os.environ.get("REACT_ALLOWED_TOOLS", "")
        if env.strip():
            return {t.strip() for t in env.split(",") if t.strip()}
        return set(DEFAULT_READONLY_TOOLS)

    # ── 单轮观察（同步：dashscope 原生调用；与抽取前语义一致）──

    def observe(
        self,
        *,
        user_message: str,
        ordered_results: List,
        round_i: int,
        run_id: Optional[str] = None,
    ) -> Optional[TaskStep]:
        """评估已执行结果：返回待追加的 TaskStep，或 None（finish/降级/违规）。"""
        try:
            # 短路：全成功 + 有实质数据 + 无知识检索参与 → 无需观察
            has_kb = any(
                isinstance(r, dict) and r.get("tool_name") == "knowledge_base_tool"
                for r in ordered_results
            )
            if not has_kb and ordered_results:
                if all(self._substantive(r) for r in ordered_results if isinstance(r, dict)):
                    return None

            # 构造观察输入：用户问题 + 各步骤结果摘要（截断防爆 prompt）
            summaries = []
            for r in ordered_results[-8:]:
                if not isinstance(r, dict):
                    continue
                tool = r.get("tool_name", "unknown")
                res = r.get("result", {})
                summary = self._summarize_fn(tool, res if isinstance(res, dict) else {})
                summaries.append(f"步骤{r.get('step_id', '?')} [{tool}]: {str(summary)[:300]}")
            results_text = "\n".join(summaries) or "（暂无结果）"

            # Phase 3：原生 tool_call 观测（白名单经 tools= 天然约束；失败回退 JSON 决策）
            if self.fc_enabled:
                try:
                    return self._observe_fc(user_message, ordered_results, results_text,
                                            round_i, run_id)
                except Exception as e:
                    logger.warning(f"[ReAct] FC 观测失败，回退 JSON 决策: {e}")

            prompt = (
                "你是智能体执行观察员。根据用户问题和已执行的工具结果，判断现有结果是否足以完整回答。\n"
                "只输出一行 JSON，不要解释：\n"
                '- 结果充分：{"decision": "finish", "reasoning": "一句话原因"}\n'
                '- 需要补充查询（检索未覆盖/空结果/换个问法）：{"decision": "add_step", "tool": "工具名", "params": {参数}, "reasoning": "一句话原因"}\n'
                "可追加的工具及参数格式：\n"
                '- knowledge_base_tool（知识库检索）：{"operation": "search", "query": "改写后的检索词", "top_k": 8}\n'
                '- postgresql_tool（数据库查询）：{"operation": "query", "sql": "SELECT ...", "params": []}\n'
                '- web_search_tool（联网搜索）：{"query": "完整中文搜索句"}\n'
                "注意：仅当现有结果确实无法回答时才 add_step；禁止编造工具名和参数。\n"
                "若某工具步骤执行失败（参数错误/校验失败/数据缺失），属于执行错误而非信息不足，"
                "不要追加任何搜索步骤，直接 {\"decision\": \"finish\"}。\n"
                "若工具已成功执行并产出地图动作（load_vector_layer/图层/标记）、报告下载链接或图表，"
                "说明任务已完成，不要追加搜索步骤，直接 {\"decision\": \"finish\"}。\n"
                f"{self._prompt_extra}"
                f"\n\n【用户问题】{user_message[:600]}\n\n【已执行结果】\n{results_text}"
            )

            import dashscope
            resp = dashscope.Generation.call(
                model=self.model,
                api_key=os.environ.get("DASHSCOPE_API_KEY"),
                messages=[{"role": "user", "content": prompt}],
                result_format="message",
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"[ReAct] observe LLM 失败: {getattr(resp, 'message', resp)}")
                return None
            raw = resp.output.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").lstrip("json").strip()
            decision = json.loads(raw)
            # 可观测性：ReAct 决策上报（仅流式路径传入 run_id；失败不影响主流程）
            if run_id:
                try:
                    obs_generation(
                        run_id=run_id, name=f"react_observe_r{round_i}",
                        model=self.model,
                        prompt=prompt, completion=decision, usage=None,
                    )
                except Exception:
                    pass

            if decision.get("decision") != "add_step":
                return None
            tool = decision.get("tool", "")
            params = decision.get("params", {})
            reasoning = str(decision.get("reasoning", ""))[:200]
            if tool not in self.allowed_tools or not isinstance(params, dict) or not params:
                logger.warning(f"[ReAct] 非法决策（白名单/参数校验失败）: {decision}")
                return None
            next_id = max(
                (r.get("step_id", 0) for r in ordered_results if isinstance(r, dict)),
                default=0,
            ) + 1
            logger.info(f"[ReAct] r{round_i} 追加步骤: {tool} params={params}")
            return TaskStep(
                step_id=next_id,
                action=reasoning or f"ReAct 第 {round_i} 轮追加 {tool}",
                tool=tool, params=params,
                reasoning=reasoning, expected_output="",
            )
        except Exception as e:
            logger.warning(f"[ReAct] observe 失败（降级原流程）: {e}")
            return None

    # ── Phase 3：原生 tool_call 观测（REACT_FC_MODE=1 灰度）──────────────────

    def _observe_fc(self, user_message: str, ordered_results: List, results_text: str,
                    round_i: int, run_id: Optional[str] = None) -> Optional[TaskStep]:
        """bind_tools → tool_calls → TaskStep（单步契约：仅取首个追加调用）。

        - 白名单从"事后校验"变为"API 只暴露白名单工具"（仅 bind allowed_tools）
        - 既往带 tool_call_id 的结果以 assistant.tool_calls + role:"tool" 协议回填
        - 模型无工具调用 → None（finish）
        """
        from core.toolhub.param_validate import local_tool_meta
        from .fc_adapter import build_openai_tools, tool_calls_to_steps

        metas = [m for m in (local_tool_meta(t) for t in sorted(self.allowed_tools)) if m]
        if not metas:
            return None
        tools_defs = build_openai_tools(metas)
        messages = self._fc_messages(user_message, ordered_results, results_text)
        msg = self._fc_invoke(messages, tools_defs)
        tool_calls = list(getattr(msg, "tool_calls", None) or [])
        if not tool_calls:
            return None
        next_id = max(
            (r.get("step_id", 0) for r in ordered_results if isinstance(r, dict)),
            default=0,
        ) + 1
        steps = tool_calls_to_steps(tool_calls[:1], start_id=next_id)
        if not steps:
            return None
        step = steps[0]
        if step.tool not in self.allowed_tools:
            logger.warning(f"[ReAct] FC 决策越出白名单: {step.tool}")
            return None
        if len(tool_calls) > 1:
            logger.info(f"[ReAct] FC 观测返回 {len(tool_calls)} 个调用，按单步契约仅执行首个")
        logger.info(f"[ReAct] FC r{round_i} 追加步骤: {step.tool} params={step.params}")
        if run_id:
            try:
                obs_generation(
                    run_id=run_id, name=f"react_observe_fc_r{round_i}",
                    model=self.model, prompt=results_text,
                    completion={"tool_calls": [
                        {"name": str(tc.get("name")), "args": tc.get("args")}
                        for tc in tool_calls if isinstance(tc, dict)
                    ]},
                    usage=None,
                )
            except Exception:
                pass
        return step

    def _fc_messages(self, user_message: str, ordered_results: List, results_text: str) -> List:
        """构造观测对话：系统提示 + 用户问题/结果摘要 + 原生 tool_call 结果协议回填。"""
        from langchain_core.messages import AIMessage, ToolMessage
        from .fc_adapter import results_to_tool_messages

        system = (
            "你是智能体执行观察员。若现有结果足以完整回答用户问题，"
            "直接输出一句简短结论，不要调用工具；"
            "仅当确实信息不足（检索未覆盖/结果为空）时，调用一个补充查询工具（一次一个）。\n"
            "若工具步骤是执行错误（参数错误/校验失败/数据缺失），属于执行问题而非信息不足，不要追加查询。\n"
            "若结果已产出地图动作（load_vector_layer/图层/标记）、报告下载链接或图表，说明任务已完成，不要追加查询。"
            f"{self._prompt_extra}"
        )
        user_block = f"【用户问题】{user_message[:600]}\n\n【已执行结果】\n{results_text}"
        messages: List = [{"role": "system", "content": system},
                          {"role": "user", "content": user_block}]
        fc_items = [r for r in (ordered_results or [])
                    if isinstance(r, dict) and r.get("tool_call_id")]
        if fc_items:
            messages.append(AIMessage(content="", tool_calls=[
                {"name": str(r.get("tool_name") or "unknown"),
                 "args": r.get("params") if isinstance(r.get("params"), dict) else {},
                 "id": str(r["tool_call_id"]),
                 "type": "tool_call"}
                for r in fc_items
            ]))
            for m in results_to_tool_messages(fc_items):
                messages.append(ToolMessage(content=m["content"], tool_call_id=m["tool_call_id"]))
        return messages

    def _fc_invoke(self, messages: List, tools: List[Dict]):
        """原生 tool_call 观测调用（独立方法，便于测试注入/替换）。"""
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=self.model,
            base_url=os.environ.get(
                "REACT_OBSERVE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            temperature=0,
            timeout=10,
        )
        return llm.bind_tools(tools, tool_choice="auto").invoke(messages)

    @staticmethod
    def _substantive(r: Dict) -> bool:
        """结果是否实质成功（成功且有数据/命令/产物）。"""
        res = r.get("result")
        if not isinstance(res, dict) or res.get("success") is not True:
            return False
        data = res.get("data")
        if data not in (None, [], {}, ""):
            return True
        # map_tool 返回 map_command（单数），需与复数一并检查：
        # 只查 map_commands 会导致地图图层已成功加载仍走 LLM 观察并追加知识库搜索
        return bool(res.get("map_commands") or res.get("map_command")
                    or res.get("charts") or res.get("report_url"))
