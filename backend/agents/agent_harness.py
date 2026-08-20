"""
AgentHarness — 调度中枢。
负责：意图路由、Agent 分派、工具加载协调。
不替代 LangGraph 图，而是作为 TaskExecutor 三个节点的内部实现。
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .intent_types import IntentType, IntentResult
from .intent_agent import IntentAgent
from .base_agent import BaseAgent
from .tool_registry import ToolRegistry
from config.fast_route_loader import load_fast_routes

logger = logging.getLogger(__name__)


# ── 快速关键词路由表：高频命令绕过 LLM，直接命中意图 ──

FAST_ROUTE_KEYWORDS: List[Tuple[str, IntentType]] = [
    # 地图操作
    ("切换卫星", IntentType.MAP_DISPLAY),
    ("卫星图层", IntentType.MAP_DISPLAY),
    ("卫星底图", IntentType.MAP_DISPLAY),
    ("卫星影像", IntentType.MAP_DISPLAY),
    ("高分影像", IntentType.MAP_DISPLAY),
    ("街道图", IntentType.MAP_DISPLAY),
    ("osm", IntentType.MAP_DISPLAY),
    ("切换底图", IntentType.MAP_DISPLAY),
    ("加载矢量", IntentType.MAP_DISPLAY),
    ("加载采区", IntentType.MAP_DISPLAY),
    ("加载图层", IntentType.MAP_DISPLAY),
    ("清除地图", IntentType.MAP_DISPLAY),
    ("清除标记", IntentType.MAP_DISPLAY),
    ("飞到", IntentType.MAP_DISPLAY),          # 3D 飞行
    ("飞往", IntentType.MAP_DISPLAY),
    ("flyto", IntentType.MAP_DISPLAY),
    # 位置搜索
    ("查找位置", IntentType.LOCATION_SEARCH),
    ("搜索位置", IntentType.LOCATION_SEARCH),
    ("定位", IntentType.LOCATION_SEARCH),
    # 空间处理
    ("坐标转换", IntentType.SPATIAL_PROCESSING),
    ("投影坐标", IntentType.SPATIAL_PROCESSING),
    ("xy相反", IntentType.SPATIAL_PROCESSING),
    ("生成矢量", IntentType.SPATIAL_PROCESSING),
    ("生成面", IntentType.SPATIAL_PROCESSING),
    ("带号", IntentType.SPATIAL_PROCESSING),
    ("cgcs2000", IntentType.SPATIAL_PROCESSING),
    # 红线 / 采区空间参考
    ("红线", IntentType.SPATIAL_REFERENCE),
    ("河道红线", IntentType.SPATIAL_REFERENCE),
    ("管理红线", IntentType.SPATIAL_REFERENCE),
    ("红线附近", IntentType.SPATIAL_REFERENCE),
    ("红线范围内", IntentType.SPATIAL_REFERENCE),
    # 报告生成
    ("生成报告", IntentType.REPORT_GENERATION),
    ("出具报告", IntentType.REPORT_GENERATION),
    ("导出报告", IntentType.REPORT_GENERATION),
    # 图表
    ("生成图表", IntentType.DATA_VISUALIZATION),
    ("画个图", IntentType.DATA_VISUALIZATION),
    ("柱状图", IntentType.DATA_VISUALIZATION),
    ("饼图", IntentType.DATA_VISUALIZATION),
    ("折线图", IntentType.DATA_VISUALIZATION),
    # 知识检索
    ("政策", IntentType.KNOWLEDGE_SEARCH),
    ("规范", IntentType.KNOWLEDGE_SEARCH),
    ("管理规定", IntentType.KNOWLEDGE_SEARCH),
    ("技术标准", IntentType.KNOWLEDGE_SEARCH),
    # 空间分析（QGIS MCP）
    ("缓冲区", IntentType.SPATIAL_ANALYSIS),
    ("裁剪", IntentType.SPATIAL_ANALYSIS),
    ("叠加分析", IntentType.SPATIAL_ANALYSIS),
    ("面积计算", IntentType.SPATIAL_ANALYSIS),
    ("空间分析", IntentType.SPATIAL_ANALYSIS),
    ("相交", IntentType.SPATIAL_ANALYSIS),
    ("包含", IntentType.SPATIAL_ANALYSIS),
    ("分区统计", IntentType.SPATIAL_ANALYSIS),
    ("空间关联", IntentType.SPATIAL_ANALYSIS),
    ("距离计算", IntentType.SPATIAL_ANALYSIS),
]


class AgentHarness:
    """调度中枢：管理 Agent 映射、快速路由、工具加载协调。"""

    def __init__(self, llm_cfg: Dict):
        self.llm_cfg = llm_cfg
        self.tool_registry = ToolRegistry()
        self.intent_agent = IntentAgent(llm_cfg)

        # 加载快速路由表（JSON 配置优先，回退到硬编码默认值）
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
    # 快速路由
    # ------------------------------------------------------------------

    def try_fast_classify(self, user_message: str) -> Optional[IntentType]:
        """快速关键词匹配。命中返回 IntentType，未命中返回 None。

        优先使用 JSON 配置文件加载的路由表，若配置不可用则回退到 FAST_ROUTE_KEYWORDS。
        """
        routes = self._fast_routes or FAST_ROUTE_KEYWORDS
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
