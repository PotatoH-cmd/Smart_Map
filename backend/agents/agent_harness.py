"""
AgentHarness — 意图分派与提示词工厂（自 main 拆分收敛后的瘦身版）。

职责（生产实际使用面）：
- try_fast_classify / build_response_prompt_for / get_system_context（TaskExecutor 节点调用）
- dispatch 按意图懒加载领域 Agent（Map/Data/Knowledge/Report/General）

P1 收敛说明：不再在 __init__ 重复构造 IntentAgent/ToolRegistry
（这两个由 TaskExecutor 统一持有）；快速路由表统一走 config/fast_route_loader
（JSON 权威，内置默认兜底），本模块不再维护第二份关键词表。
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .intent_types import IntentType, IntentResult
from .base_agent import BaseAgent
from config.fast_route_loader import load_fast_routes

logger = logging.getLogger(__name__)


class AgentHarness:
    """调度中枢：意图分派 + 响应提示词工厂。"""

    def __init__(self, llm_cfg: Dict):
        self.llm_cfg = llm_cfg
        # 快速路由表（JSON 配置优先，不可用时回退 loader 内置默认）
        self._fast_routes: List[Tuple[str, IntentType]] = load_fast_routes()

        # 懒加载 Agent 实例
        self._agents: Dict[IntentType, BaseAgent] = {}
        self._fallback: Optional[BaseAgent] = None

    # ------------------------------------------------------------------
    # Agent 管理
    # ------------------------------------------------------------------

    def _init_agents(self):
        """懒加载所有 Agent 实例。"""
        if self._agents:
            return

        from .map_agent import MapAgent
        from .data_agent import DataAgent
        from .knowledge_agent import KnowledgeAgent
        from .report_agent import ReportAgent
        from .general_agent import GeneralAgent

        map_agent = MapAgent()
        data_agent = DataAgent()

        self._agents = {
            IntentType.MAP_DISPLAY: map_agent,
            IntentType.LOCATION_SEARCH: map_agent,
            IntentType.COORDINATE_MARKER: map_agent,
            IntentType.SPATIAL_PROCESSING: map_agent,
            IntentType.SPATIAL_REFERENCE: map_agent,
            IntentType.SPATIAL_ANALYSIS: map_agent,
            IntentType.DATA_QUERY: data_agent,
            IntentType.DATA_VISUALIZATION: data_agent,
            IntentType.KNOWLEDGE_SEARCH: KnowledgeAgent(),
            IntentType.REPORT_GENERATION: ReportAgent(),
        }
        self._fallback = GeneralAgent()

    def dispatch(self, intent_result: IntentResult) -> BaseAgent:
        """按意图分派到对应 Agent。"""
        self._init_agents()
        intent = intent_result.primary_intent
        # 处理可能的 str 类型（Pydantic use_enum_values=True 时）
        if isinstance(intent, str):
            try:
                intent = IntentType(intent)
            except ValueError:
                return self._fallback
        return self._agents.get(intent, self._fallback)

    def get_tool_names(self, intent_result: IntentResult) -> List[str]:
        """获取当前意图需要的工具列表（去重保序）。"""
        agent = self.dispatch(intent_result)
        return list(dict.fromkeys(agent.tool_names))  # 去重保序

    # ------------------------------------------------------------------
    # 快速路由（关键词 → IntentType，JSON 权威 + loader 内置默认兜底）
    # ------------------------------------------------------------------

    def try_fast_classify(self, user_message: str) -> Optional[IntentType]:
        """快速关键词匹配。命中返回 IntentType，未命中返回 None。"""
        routes = self._fast_routes
        msg_lower = user_message.lower().replace(" ", "")
        for keyword, intent in routes:
            if keyword.lower().replace(" ", "") in msg_lower:
                logger.info(
                    f"[AgentHarness] Fast route hit: keyword='{keyword}' → intent={intent.value}"
                )
                return intent
        return None

    # ------------------------------------------------------------------
    # 提示词组装
    # ------------------------------------------------------------------

    def build_system_prompt_for(self, intent_result: IntentResult, schema_text: str = "") -> str:
        """为当前意图组装专用的 system prompt。"""
        agent = self.dispatch(intent_result)
        domain_prompt = agent.build_system_prompt(schema_text)

        # 通用前缀
        preamble = "你是一个专业的意图分类与任务规划专家。\n\n## 你的职责\n1. 准确分析用户输入的意图\n2. 提取关键实体（地名、数据表名、时间等）\n3. 制定清晰的任务执行计划\n"

        # execution_plan 通用规范
        common = """\n## execution_plan 填写规范\n- execution_plan 中每一步的 tool 字段**必须**填写对应工具名称字符串，**绝对不能**留空或为 null\n- execution_plan 中每个步骤的 step_id 从 1 开始递增\n- 如果涉及多个意图，按执行顺序规划任务步骤\n- 除非工具真实支持，否则不要臆造参数名\n"""

        return preamble + domain_prompt + common

    def build_response_prompt_for(self, intent_result: IntentResult) -> str:
        """为当前意图组装专用的 response prompt。"""
        agent = self.dispatch(intent_result)
        return agent.build_response_prompt()

    # ------------------------------------------------------------------
    # 系统上下文（时间等）
    # ------------------------------------------------------------------

    @staticmethod
    def get_system_context() -> str:
        """获取当前系统上下文信息，注入到 LLM 提示词中。"""
        now = datetime.now()
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[now.weekday()]
        return (
            f"【系统信息】当前时间：{now.strftime('%Y年%m月%d日')} {weekday} "
            f"{now.strftime('%H:%M:%S')}（北京时间）"
        )
