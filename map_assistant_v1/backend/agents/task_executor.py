import json
import logging
from typing import List, Dict, Any, Optional
from qwen_agent.agents import Assistant
from .intent_types import IntentType, IntentResult
from .intent_agent import IntentAgent

logger = logging.getLogger(__name__)


class TaskExecutor:
    def __init__(self, llm_cfg: Dict[str, Any]):
        self.llm_cfg = llm_cfg
        self.intent_agent = IntentAgent(llm_cfg)
        self._tool_instances = {}
        self._agent_cache = {}

    def _get_tool_instance(self, tool_name: str):
        if tool_name not in self._tool_instances:
            if tool_name == "map_tool":
                from tools.map_tool import MapTool
                self._tool_instances[tool_name] = MapTool()
            elif tool_name == "location_search":
                from tools.map_tool import LocationSearchTool
                self._tool_instances[tool_name] = LocationSearchTool()
            elif tool_name == "coordinate_marker":
                from tools.map_tool import CoordinateMarkerTool
                self._tool_instances[tool_name] = CoordinateMarkerTool()
            elif tool_name == "postgresql_tool":
                from tools.postgresql_tool import PostgreSQLTool
                self._tool_instances[tool_name] = PostgreSQLTool(cfg={
                    'host': '172.136.16.52',
                    'port': 5432,
                    'database': 'postgres',
                    'user': 'postgres',
                })
            elif tool_name == "mcp_postgres_tool":
                from tools.mcp_postgres_tool import MCPPostgreSQLTool
                self._tool_instances[tool_name] = MCPPostgreSQLTool(cfg={
                    'readonly': True,
                })
            elif tool_name == "knowledge_base_tool":
                from tools.knowledge_base_tool import KnowledgeBaseTool
                self._tool_instances[tool_name] = KnowledgeBaseTool()
            elif tool_name == "data_visualizer_tool":
                from tools.data_visualizer_tool import DataVisualizerTool
                self._tool_instances[tool_name] = DataVisualizerTool()
            elif tool_name == "report_generator_tool":
                from tools.report_generator_tool import ReportGeneratorTool
                self._tool_instances[tool_name] = ReportGeneratorTool()
            elif tool_name == "weather_tool":
                from tools.weather_tool import WeatherTool
                self._tool_instances[tool_name] = WeatherTool()
        return self._tool_instances.get(tool_name)

    def _build_agent(self, tools: List[str], name: str = "Task Executor") -> Assistant:
        tool_objs = []
        for tool_name in tools:
            tool_obj = self._get_tool_instance(tool_name)
            if tool_obj:
                tool_objs.append(tool_obj)

        return Assistant(
            llm=self.llm_cfg,
            function_list=tool_objs,
            name=name,
            description="任务执行智能体",
        )

    async def execute(self, user_message: str, chat_history: Optional[List[Dict]] = None) -> Dict[str, Any]:
        intent_result = self.intent_agent.analyze(user_message, chat_history)
        logger.info(f"Intent analysis: {intent_result.primary_intent}, confidence: {intent_result.confidence}")
        logger.info(f"Execution plan: {len(intent_result.execution_plan)} steps")

        if intent_result.confidence < 0.3:
            return {
                "success": False,
                "intent_result": intent_result,
                "response": f"抱歉，我无法理解您的意图。请尝试更详细地描述您的需求。",
                "map_commands": [],
                "charts": [],
                "requires_confirmation": True,
            }

        if intent_result.requires_confirmation:
            return {
                "success": True,
                "intent_result": intent_result,
                "response": self._generate_confirmation_message(intent_result),
                "map_commands": [],
                "charts": [],
                "requires_confirmation": True,
            }

        required_tools = self.intent_agent.get_required_tools(intent_result)
        logger.info(f"Required tools: {required_tools}")

        if not required_tools:
            return {
                "success": True,
                "intent_result": intent_result,
                "response": f"已分析意图为 {intent_result.primary_intent}，但当前无需执行具体操作。",
                "map_commands": [],
                "charts": [],
                "requires_confirmation": False,
            }

        agent = self._build_agent(required_tools)
        messages = self._prepare_messages(user_message, intent_result)

        response_messages = []
        for response in agent.run(messages=messages):
            response_messages = response

        response_text = self._extract_response_text(response_messages)
        map_commands = self._extract_map_commands(response_messages)
        charts = self._extract_charts(response_messages)

        return {
            "success": True,
            "intent_result": intent_result,
            "response": response_text,
            "messages": response_messages,
            "map_commands": map_commands,
            "charts": charts,
            "requires_confirmation": False,
        }

    def _prepare_messages(self, user_message: str, intent_result: IntentResult) -> List[Dict]:
        messages = []

        if intent_result.primary_intent == IntentType.MAP_DISPLAY:
            messages.append({
                "role": "system",
                "content": self._get_map_system_prompt()
            })
        elif intent_result.primary_intent == IntentType.DATA_QUERY:
            messages.append({
                "role": "system",
                "content": self._get_db_system_prompt()
            })
        elif intent_result.primary_intent == IntentType.KNOWLEDGE_SEARCH:
            messages.append({
                "role": "system",
                "content": self._get_kb_system_prompt()
            })
        elif intent_result.primary_intent == IntentType.DATA_VISUALIZATION:
            messages.append({
                "role": "system",
                "content": self._get_viz_system_prompt()
            })
        elif intent_result.primary_intent == IntentType.WEATHER_QUERY:
            messages.append({
                "role": "system",
                "content": self._get_weather_system_prompt()
            })

        messages.append({"role": "user", "content": user_message})
        return messages

    def _get_map_system_prompt(self) -> str:
        return """本轮会话为地图操作任务。
- 禁止调用 knowledge_base_tool
- 仅允许使用 map_tool, location_search, coordinate_marker 与 postgresql_tool
- 当用户要求跳转或查找位置时，必须优先使用 location_search 查找坐标
- 当用户明确要求在特定经纬度标记点时，使用 coordinate_marker 工具
- 若用户给出了完整可采区/砂场名称（包含"可采区"或"砂场"），filter 必须使用等值匹配：\\"Mineable_Area_Name\\"='完整名称'，禁止用 LIKE"""

    def _get_db_system_prompt(self) -> str:
        return """本轮会话为数据查询任务。
- 业务数据表为 'ceshen'
- 字段名必须加双引号，如 "Mineable_Area_Name", "Measured_Depth", "Control_Elevation"
- 超深度开采判定：AVG(Control_Elevation - Measured_Depth) > 2"""

    def _get_kb_system_prompt(self) -> str:
        return """本轮会话为知识检索任务。
- 政策/流程/操作文档问题必须使用 knowledge_base_tool
- 禁止查询数据库"""

    def _get_viz_system_prompt(self) -> str:
        return """本轮会话为数据可视化任务。
- 必须使用 data_visualizer_tool 生成图表
- 禁止生成 Markdown 图片链接
- 禁止编写 Python 代码画图"""

    def _get_weather_system_prompt(self) -> str:
        return """本轮会话为天气查询任务。
- 必须使用 weather_tool 查询天气
- 支持查询当前天气和未来预报
- 城市名称建议精确到区县级别"""

    def _generate_confirmation_message(self, intent_result: IntentResult) -> str:
        context = intent_result.task_context
        plan = "\n".join(
            f"{i+1}. {step.action} (使用 {step.tool})"
            for i, step in enumerate(intent_result.execution_plan)
        )
        return f"""我理解您的需求是：{context}

我的执行计划是：
{plan}

请确认是否按此计划执行？"""

    def _extract_response_text(self, response_messages: List[Dict]) -> str:
        for msg in reversed(response_messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                content = msg["content"]
                content = content.replace("```json", "").replace("```", "").strip()
                if content.startswith("{"):
                    try:
                        parsed = json.loads(content)
                        if "formatted_answer" in parsed:
                            return parsed["formatted_answer"]
                    except:
                        pass
                return content
        return "命令已执行。"

    def _extract_map_commands(self, response_messages: List[Dict]) -> List[Dict]:
        map_commands = []
        for msg in response_messages:
            if msg.get("role") == "function" and msg.get("name") in ["map_tool", "location_search"]:
                try:
                    func_res = json.loads(msg.get("content", "{}"))
                    if func_res.get("map_command"):
                        map_commands.append(func_res["map_command"])
                except:
                    pass
        return map_commands

    def _extract_charts(self, response_messages: List[Dict]) -> List[Dict]:
        charts = []
        for msg in response_messages:
            if msg.get("role") == "function" and msg.get("name") == "data_visualizer_tool":
                try:
                    vis = json.loads(msg.get("content", "{}"))
                    if isinstance(vis, dict) and vis.get("success"):
                        charts.append({
                            "chart_type": vis.get("chart_type"),
                            "config": vis.get("config"),
                            "summary": vis.get("content"),
                        })
                except:
                    pass
        return charts
