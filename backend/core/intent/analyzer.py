"""通用意图分析器：关键词路由（数据化）+ LLM 结构化输出。

从 agents/intent_agent.py 泛化而来，差异：
- 意图目录 / 工具清单 / 约束段 / 关键词路由 全部来自目录数据（无业务硬编码）
- 输出 GenericIntentResult（intent 为 str）
"""
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from .models import GenericIntentResult

logger = logging.getLogger(__name__)

_EXTRA_INTENT_RULES = """
## 规划要求
1. 优先根据"工具与意图的对应关系"选择工具并在 execution_plan 中给出具体步骤
2. 每个步骤必须给出可直接执行的 tool 与 params
3. 不确定时选择最接近的意图，置信度如实给出
4. 置信度低于 0.6 或无法分类时 primary_intent 使用 unknown
"""


class GenericIntentAnalyzer:
    def __init__(self, llm_cfg: Dict[str, Any], catalog):
        self.catalog = catalog
        base_url = llm_cfg.get("model_server") or llm_cfg.get("base_url")
        self.structured_llm = ChatOpenAI(
            model=llm_cfg.get("model", "qwen-plus"),
            base_url=base_url,
            api_key=llm_cfg.get("api_key", ""),
            temperature=0.1,
        ).with_structured_output(GenericIntentResult)

    # ------------------------------------------------------------------
    def analyze(self, message: str, history: Optional[List[Dict]] = None,
                view: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> GenericIntentResult:
        self.catalog.refresh()
        extra = extra or {}

        prompt = self._build_system_prompt(extra)
        snippets = self._select_constraint_snippets(message, view)
        if snippets:
            prompt += ("\n\n## 相关工具参数约束（按需注入，仅本轮有效）\n"
                       + "\n".join(snippets))

        analysis_prompt = self._build_analysis_prompt(message, history, extra)
        try:
            result = self.structured_llm.invoke([
                SystemMessage(content=prompt),
                HumanMessage(content=analysis_prompt),
            ])
            if isinstance(result, GenericIntentResult):
                return result
            return self._unknown("结构化输出类型异常")
        except Exception as e:
            logger.error(f"[intent] analyze error: {e}")
            return self._unknown(str(e))

    # ------------------------------------------------------------------
    def _build_system_prompt(self, extra: Dict[str, Any]) -> str:
        intent_list = self.catalog.intent_list_text()
        tool_list = self.catalog.tool_intent_text()
        db_schema = (extra.get("db_schema") or "").strip()
        schema_block = f"\n## 数据库详细结构\n{db_schema}\n" if db_schema else ""
        return f"""你是一个专业的意图分类与任务规划专家。

## 职责
1. 准确分析用户输入的意图
2. 提取关键实体（地名、数据表名、时间等）
3. 制定清晰的任务执行计划

## 意图分类体系
{intent_list}

## 工具与意图的对应关系
{tool_list}
{schema_block}
## 输出要求
必须返回 JSON 格式，包含字段：
- `primary_intent`：主要意图（上述意图之一）
- `confidence`：置信度（0.0-1.0）
- `entities`：提取的实体列表
- `task_context`：一句话任务上下文
- `execution_plan`：包含 step_id, action, tool, params, reasoning, expected_output 的步骤列表
- `requires_confirmation`：是否需要用户确认
- `suggestions`：补充建议列表

{_EXTRA_INTENT_RULES}"""

    def _select_constraint_snippets(self, message: str, view: Optional[str]) -> List[str]:
        """关键词预筛：命中即选中该工具的约束段，最多注入 2 段。

        - view_bound 工具与视图绑定（map_tool 2D / cesium_tool 3D 二选一）
        - excludes 互斥：先命中的工具排除后续工具
        """
        text = message or ""
        selected: List[str] = []
        selected_names: set = set()
        for route in self.catalog.keyword_routes():
            tool = route.get("name")
            keywords = route.get("keywords") or []
            if not any(k in text for k in keywords):
                continue
            if route.get("view_bound"):
                if tool == "map_tool" and view == "cesium":
                    continue
                if tool == "cesium_tool" and view != "cesium":
                    continue
            excluded = False
            for prev in selected_names:
                if tool in (self._spec_of(prev) or {}).get("excludes", []):
                    excluded = True
                    break
            if excluded:
                continue
            snippet = route.get("constraint") or ""
            if snippet:
                selected.append(snippet)
                selected_names.add(tool)
                if len(selected) >= 2:
                    break
        return selected

    def _spec_of(self, tool_name: str) -> Optional[Dict[str, Any]]:
        for t in self.catalog.tools:
            if t.get("name") == tool_name:
                return t
        return None

    def _build_analysis_prompt(self, message: str, history: Optional[List[Dict]],
                               extra: Dict[str, Any]) -> str:
        history_block = ""
        if history:
            recent = history[-6:]
            lines = [f"- {h.get('role', 'user')}: {str(h.get('content', ''))[:100]}"
                     for h in recent if h.get("role") in ("user", "assistant")]
            if lines:
                history_block = "\n## 最近对话历史\n" + "\n".join(lines)
        facts = (extra.get("facts_context") or "").strip()
        facts_block = f"\n{facts}" if facts else ""
        return f"""## 用户当前输入
{message}
{facts_block}
{history_block}

请分析上述用户输入，返回结构化的意图分析结果。"""

    def _unknown(self, reason: str) -> GenericIntentResult:
        return GenericIntentResult(
            primary_intent="unknown",
            confidence=0.0,
            entities=[],
            task_context=f"无法分析意图: {reason}",
            execution_plan=[],
            requires_confirmation=True,
            suggestions=["请尝试重新描述您的需求"],
        )
