"""Agent 装配工厂（从 main.py 收敛而来，自 main.py 机械搬移，行为不变）。

职责：
- 集中全部 tools import —— import 副作用触发 qwen_agent 工具注册，
  main.py 入口不再直接导入 tools，本模块是唯一装配点。
- 构造 LangGraph 编排的 TaskExecutor（build_task_executor）与旧版
  Qwen Assistant（build_legacy_agent / build_assistant_with_tools）。
"""
# ---------------------------------------------------------------------------
# tools 全量导入（副作用：向 qwen_agent 注册工具）
# ---------------------------------------------------------------------------
from qwen_agent.agents import Assistant

from core.config import keys as _cfg_keys
from prompts import LEGACY_ASSISTANT_SYSTEM_PROMPT

from tools.map_tool import MapTool, LocationSearchTool
from tools.postgresql_tool import PostgreSQLTool
# 知识库后端选择（环境变量 KNOWLEDGE_BACKEND: ragflow|llamaindex，默认 ragflow）
import os as _os

_KB_BACKEND = _os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
if _KB_BACKEND == "llamaindex":
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from tools.ragflow_knowledge_tool import KnowledgeBaseTool
from tools.knowledge_qa_agent import KnowledgeQAAgent  # noqa: F401 — 知识问答 Agent 注册
from tools.knowledge_graph_tool import get_kg  # noqa: F401 — 知识图谱工具
from tools.data_visualizer_tool import DataVisualizerTool
from tools.report_generator_tool import ReportGeneratorTool
from tools.weather_tool import WeatherTool
from tools.web_search_tool import WebSearchTool  # noqa: F401 — import 触发 qwen_agent 注册
from tools.cesium_tool import CesiumTool  # Cesium 3D 地图工具
from tools.spatial_reference_tool import SpatialReferenceTool  # noqa: F401 — 空间参考工具（红线/采区），触发注册

from agents import TaskExecutor

# ---------------------------------------------------------------------------
# LLM 配置（LangGraph TaskExecutor 与旧版 Assistant 共用同一份）
# ---------------------------------------------------------------------------
LEGACY_LLM_CFG = {
    'model': 'qwen-flash-2025-07-28',
    'model_server': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'api_key': _cfg_keys.dashscope_api_key,
    'generate_cfg': {
        'extra_body': {
            'enable_thinking': False,
        },
    },
}


def build_task_executor():
    """LangGraph 意图编排执行器（主链路 /chat、/chat/stream 使用）。"""
    return TaskExecutor(LEGACY_LLM_CFG)


def build_legacy_agent():
    """旧版 Qwen Assistant（注册全部工具，作为直连兜底 runner）。"""
    tools = [
        'map_tool',
        'location_search',
        'coordinate_marker',
        'postgresql_tool',
        'knowledge_base_tool',
        'data_visualizer_tool',
        'report_generator_tool',
        'weather_tool',
        'web_search_tool',  # 联网搜索（实时信息兜底）
        'cesium_tool',  # Cesium 3D 地图工具
        'spatial_reference_tool',  # 空间参考数据工具（红线/采区边界等）
    ]
    return Assistant(
        llm=LEGACY_LLM_CFG,
        function_list=tools,
        name='Qwen3 地图助手',
        description=LEGACY_ASSISTANT_SYSTEM_PROMPT,
    )


# ---------------------------------------------------------------------------
# 工具实例缓存（延迟初始化，第一次调用时创建）
# ---------------------------------------------------------------------------
_TOOL_INSTANCES = {}


def _get_tool_instance(name):
    if name not in _TOOL_INSTANCES:
        if name == 'map_tool':
            _TOOL_INSTANCES[name] = MapTool()
        elif name == 'location_search':
            _TOOL_INSTANCES[name] = LocationSearchTool()
        elif name == 'postgresql_tool':
            _TOOL_INSTANCES[name] = PostgreSQLTool()
        elif name == 'knowledge_base_tool':
            _TOOL_INSTANCES[name] = KnowledgeBaseTool()
        elif name == 'data_visualizer_tool':
            _TOOL_INSTANCES[name] = DataVisualizerTool()
        elif name == 'report_generator_tool':
            _TOOL_INSTANCES[name] = ReportGeneratorTool()
        elif name == 'weather_tool':
            _TOOL_INSTANCES[name] = WeatherTool()
        elif name == 'cesium_tool':
            _TOOL_INSTANCES[name] = CesiumTool()
        elif name == 'spatial_reference_tool':
            _TOOL_INSTANCES[name] = SpatialReferenceTool()
        else:
            return name  # 未知工具回退字符串
    return _TOOL_INSTANCES[name]


def build_assistant_with_tools(function_list):
    # 优先使用工具实例，避免字符串名称查找失败
    resolved = [_get_tool_instance(name) for name in function_list]
    return Assistant(
        llm=LEGACY_LLM_CFG,
        function_list=resolved,
        name='Qwen3 地图助手',
        description="地图数据加载任务，仅使用地图与数据库工具。",
    )
