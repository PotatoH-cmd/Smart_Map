import logging
from typing import List, Dict, Any, Optional, Tuple
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from .intent_types import (
    IntentType,
    IntentResult,
    INTENT_DESCRIPTIONS,
    TOOL_INTENT_MAPPING,
)
from tools.schema_manager import SchemaManager
from prompts import build_intent_classifier_prompt, TOOL_CONSTRAINT_SNIPPETS

logger = logging.getLogger(__name__)

# 阶段6：关键词预筛路由（按优先级排列，命中即选中该工具约束段，最多注入 2 段）。
# 顺序很重要：更具体的工具排在前面（如"采砂监测报告"必须先于通用"报告"）。
TOOL_KEYWORD_ROUTES: List[Tuple[str, Tuple[str, ...]]] = [
    ("caisha_report_tool", ("采砂监测报告", "监测分析报告", "采砂场监测报告", "采砂报告")),
    ("qgis_mcp_tool", ("缓冲区", "buffer", "裁剪", "clip", "叠加", "相交", "面积", "中心点", "centroid", "分区统计", "空间关联")),
    ("spatial_processing_tool", ("坐标转换", "投影", "带号", "CGCS2000", "EPSG", "矢量范围", "XY相反", "生成矢量")),
    ("spatial_reference_tool", ("红线", "河道", "可采区", "采区", "许可范围")),
    ("weather_tool", ("天气", "气温", "温度", "降雨", "下雨", "下雪", "风力", "风速", "湿度", "空气质量", "AQI", "雾霾", "预报", "带伞", "紫外线")),
    ("web_search_tool", ("新闻", "热点", "实时", "最新", "今天", "现在", "目前", "行情")),
    ("data_visualizer_tool", ("图表", "统计图", "可视化", "柱状图", "趋势", "占比", "分布图", "饼图")),
    ("postgresql_tool", ("查询", "统计", "多少个", "多少", "平均", "超深", "高程", "数量", "count")),
    ("report_generator_tool", ("报告", "出具", "文档")),
    ("knowledge_base_tool", ("政策", "规定", "流程", "制度", "怎么办", "如何办理", "手续")),
    ("cesium_tool", ("三维", "3D", "飞行", "测深风险", "风险柱")),
    ("map_tool", ("加载", "上图", "图层", "标记", "标点", "底图", "切换", "跳转", "定位", "位置", "经纬度", "清除")),
]
# 地图类工具约束与视图绑定：命中地图关键词时按视图二选一注入
_VIEW_BOUND_ROUTES = {"map_tool", "cesium_tool"}
# 互斥对：前一个工具命中后，不再注入后一个工具的约束
# （caisha_report_tool 内部自动完成报告生成，约束中明确禁止再规划 report_generator_tool）
_EXCLUSIVE_ROUTES = {("caisha_report_tool", "report_generator_tool")}


class IntentAgent:
    """
    意图识别 Agent。
    使用 LangChain ChatOpenAI + with_structured_output 实现结构化意图分析，
    替代原 qwen_agent.agents.Assistant 继承方式。
    """

    def __init__(self, llm_cfg: Dict[str, Any]):
        self.llm_cfg = llm_cfg

        # 兼容 qwen-agent 格式的 LLM_CFG（model_server / base_url 二选一）
        base_url = llm_cfg.get("model_server") or llm_cfg.get("base_url")
        api_key = llm_cfg.get("api_key", "")
        model = llm_cfg.get("model", "qwen-plus")

        llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0.1,
        )
        # 结构化输出：直接输出 IntentResult Pydantic 模型
        self.structured_llm = llm.with_structured_output(IntentResult)
        self._schema_injected = False
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """构建意图分类器 system prompt。所有规则统一从 prompts.py 获取。"""
        intent_list = "\n".join(
            f"- {intent.value}: {desc}"
            for intent, desc in INTENT_DESCRIPTIONS.items()
        )
        tool_list = "\n".join(
            f"- {tool}: {', '.join(str(i) if isinstance(i, str) else i.value for i in intents)}"
            for tool, intents in TOOL_INTENT_MAPPING.items()
        )
        return build_intent_classifier_prompt(intent_list, tool_list)

    def _inject_schema_once(self):
        """首次调用时将真实 DB schema 注入 system prompt。"""
        if self._schema_injected:
            return
        try:
            sm = SchemaManager.instance()
            schema_text = sm.get_formatted_schema()
            if schema_text:
                self.system_prompt = self.system_prompt.replace(
                    "{DB_SCHEMA_PLACEHOLDER}",
                    f"## 数据库详细结构\n{schema_text}"
                )
            else:
                self.system_prompt = self.system_prompt.replace("{DB_SCHEMA_PLACEHOLDER}", "")
        except Exception as e:
            logger.warning(f"Failed to inject DB schema into IntentAgent prompt: {e}")
            self.system_prompt = self.system_prompt.replace("{DB_SCHEMA_PLACEHOLDER}", "")
        self._schema_injected = True

    def analyze(self, user_message: str, chat_history: Optional[List[Dict]] = None,
                system_prompt: Optional[str] = None, view_hint: Optional[str] = None) -> IntentResult:
        """分析用户消息，返回结构化 IntentResult。

        Args:
            user_message: 用户输入文本
            chat_history: 可选的对话历史
            system_prompt: 可选外部 system prompt，为 None 时使用默认完整 prompt。
            view_hint: 前端当前视图（'map' | 'cesium'），用于地图类约束的按需注入。
        """
        self._inject_schema_once()
        try:
            analysis_prompt = self._build_analysis_prompt(user_message, chat_history)
            prompt = system_prompt if system_prompt else self.system_prompt
            # 阶段6：按需注入 1-2 个工具约束段（PineFlow Skills 简化落地）
            snippets = self._select_constraint_snippets(user_message, view_hint)
            if snippets:
                prompt = (
                    prompt
                    + "\n\n## 相关工具参数约束（按需注入，仅本轮有效）\n"
                    + "\n".join(snippets)
                )
                logger.info(
                    f"[IntentAgent] constraint snippets injected: {len(snippets)} (view={view_hint})"
                )
            messages = [
                SystemMessage(content=prompt),
                HumanMessage(content=analysis_prompt),
            ]
            result = self.structured_llm.invoke(messages)
            if isinstance(result, IntentResult):
                return result
            return self._create_unknown_result("结构化输出类型异常")
        except Exception as e:
            logger.error(f"Intent analysis error: {e}")
            return self._create_unknown_result(str(e))

    def _select_constraint_snippets(self, user_message: str, view_hint: Optional[str] = None) -> List[str]:
        """关键词预筛：命中即选中该工具的约束段，最多注入 2 段。

        地图类约束（map_tool / cesium_tool）与视图绑定：
        - view_hint='cesium' 时注入 cesium_tool 约束，跳过 map_tool；
        - 其他情况注入 map_tool 约束，跳过 cesium_tool。
        """
        text = user_message or ""
        selected: List[str] = []
        selected_names: set = set()
        for tool, keywords in TOOL_KEYWORD_ROUTES:
            if not any(k in text for k in keywords):
                continue
            if tool in _VIEW_BOUND_ROUTES:
                if tool == "map_tool" and view_hint == "cesium":
                    continue
                if tool == "cesium_tool" and view_hint != "cesium":
                    continue
            if any(first in selected_names and second == tool
                   for first, second in _EXCLUSIVE_ROUTES):
                continue
            snippet = TOOL_CONSTRAINT_SNIPPETS.get(tool)
            if snippet:
                selected.append(snippet)
                selected_names.add(tool)
                if len(selected) >= 2:
                    break
        return selected

    def _build_analysis_prompt(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> str:
        # 阶段4：历史上下文构建迁入 context_manager（预算：最近 6 轮 × 100 字/条）
        from .context_manager import build_history_context
        history_context = build_history_context(chat_history)

        # 阶段D：用户事实记忆（跨会话长期记忆，≤500 字；失败静默为空）
        facts_context = ""
        try:
            from .fact_memory import build_facts_context
            facts_context = build_facts_context()
        except Exception:
            pass

        return f"""## 用户当前输入
{user_message}
{facts_context}
{history_context}

请分析上述用户输入，返回结构化的意图分析结果。"""

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
        """从 execution_plan 中提取需要调用的工具列表（去重、保序）。"""
        seen = set()
        tools = []
        for step in intent_result.execution_plan:
            if step.tool and step.tool not in seen:
                seen.add(step.tool)
                tools.append(step.tool)
        return tools
