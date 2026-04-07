import json
import logging
from typing import List, Dict, Any, Optional
from qwen_agent.agents import Assistant
from .intent_types import (
    IntentType,
    IntentResult,
    TaskStep,
    INTENT_DESCRIPTIONS,
    TOOL_INTENT_MAPPING,
)

logger = logging.getLogger(__name__)


class IntentAgent(Assistant):
    def __init__(self, llm_cfg: Dict[str, Any]):
        self.llm_cfg = llm_cfg
        self.system_prompt = self._build_system_prompt()
        super().__init__(
            llm=llm_cfg,
            function_list=[],
            name="Intent Classifier",
            description="专门分析用户意图、提取实体并规划执行计划的智能体",
        )

    def _build_system_prompt(self) -> str:
        intent_list = "\n".join(
            f"- {intent.value}: {desc}"
            for intent, desc in INTENT_DESCRIPTIONS.items()
        )
        tool_list = "\n".join(
            f"- {tool}: {', '.join(intents)}"
            for tool, intents in TOOL_INTENT_MAPPING.items()
        )
        return f"""你是一个专业的意图分类与任务规划专家。

## 你的职责
1. 准确分析用户输入的意图
2. 提取关键实体（地名、数据表名、时间等）
3. 制定清晰的任务执行计划

## 意图分类体系
{intent_list}

## 工具与意图的对应关系
{tool_list}

## 业务背景
- 这是一个地图与数据分析助手系统
- 业务数据存储在 PostgreSQL 的 'ceshen' 表中
- 包含字段：Mineable_Area_Name（可采区名称）, Measured_Depth（实测高程）, Control_Elevation（控制高程）
- 核心业务规则：超深度开采 = AVG(Control_Elevation - Measured_Depth) > 2

## 输出要求
必须返回 JSON 格式的结构化结果，包含以下字段：
- primary_intent: 主要意图（必须是上述意图枚举值之一）
- confidence: 置信度（0.0-1.0）
- entities: 提取的实体列表（如地点名称、数据表名等）
- task_context: 用一句话总结任务上下文
- execution_plan: 执行计划，包含 step_id, action, tool, params, reasoning, expected_output
- requires_confirmation: 是否需要用户确认
- suggestions: 补充建议列表

## 注意事项
1. 优先判断是否为纯地图操作意图（map_display, location_search）
2. 数据查询必须使用 postgresql_tool
3. 知识库检索必须使用 knowledge_base_tool
4. 图表生成必须使用 data_visualizer_tool
5. 报告生成必须使用 report_generator_tool
6. 如果涉及多个意图，按执行顺序规划任务步骤"""

    def analyze(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> IntentResult:
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.append({"role": "user", "content": self._build_analysis_prompt(user_message, chat_history)})

        try:
            for response in self.run(messages=messages):
                if response and len(response) > 0:
                    last_msg = response[-1]
                    if last_msg.get("role") == "assistant" and last_msg.get("content"):
                        content = last_msg["content"]
                        parsed = self._parse_json_response(content)
                        if parsed:
                            return self._convert_to_intent_result(parsed)

            return self._create_unknown_result("LLM 返回为空")
        except Exception as e:
            logger.error(f"Intent analysis error: {e}")
            return self._create_unknown_result(str(e))

    def _build_analysis_prompt(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> str:
        history_context = ""
        if chat_history:
            recent_msgs = chat_history[-6:]
            history_context = "\n\n## 最近对话历史\n" + "\n".join(
                f"- {msg.get('role', 'unknown')}: {msg.get('content', '')[:100]}"
                for msg in recent_msgs
                if msg.get("role") in ["user", "assistant"]
            )

        return f"""## 用户当前输入
{user_message}
{history_context}

请分析上述用户输入，返回 JSON 格式的意图分析结果。"""

    def _parse_json_response(self, content: str) -> Optional[Dict]:
        content = content.strip()
        content = content.replace("```json", "").replace("```", "").strip()

        start_idx = content.find("{")
        end_idx = content.rfind("}")

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = content[start_idx : end_idx + 1]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.warning(f"JSON parse error: {e}, content: {json_str[:200]}")
                return None
        return None

    def _convert_to_intent_result(self, parsed: Dict) -> IntentResult:
        try:
            primary_intent_str = parsed.get("primary_intent", "unknown")
            try:
                primary_intent = IntentType(primary_intent_str)
            except ValueError:
                primary_intent = IntentType.UNKNOWN

            execution_plan = []
            for step in parsed.get("execution_plan", []):
                if isinstance(step, dict):
                    execution_plan.append(
                        TaskStep(
                            step_id=step.get("step_id", 0),
                            action=step.get("action", ""),
                            tool=step.get("tool"),
                            params=step.get("params", {}),
                            reasoning=step.get("reasoning", ""),
                            expected_output=step.get("expected_output", ""),
                        )
                    )

            return IntentResult(
                primary_intent=primary_intent,
                confidence=parsed.get("confidence", 0.5),
                entities=parsed.get("entities", []),
                task_context=parsed.get("task_context", ""),
                execution_plan=execution_plan,
                requires_confirmation=parsed.get("requires_confirmation", False),
                suggestions=parsed.get("suggestions", []),
            )
        except Exception as e:
            logger.error(f"Convert to IntentResult error: {e}")
            return self._create_unknown_result(str(e))

    def _create_unknown_result(self, reason: str) -> IntentResult:
        return IntentResult(
            primary_intent=IntentType.UNKNOWN,
            confidence=0.0,
            entities=[],
            task_context=f"无法分析意图: {reason}",
            execution_plan=[],
            requires_confirmation=True,
            suggestions=["请尝试重新描述您的需求"],
        )

    def get_required_tools(self, intent_result: IntentResult) -> List[str]:
        tool_set = set()
        for step in intent_result.execution_plan:
            if step.tool:
                tool_set.add(step.tool)
        return list(tool_set)
