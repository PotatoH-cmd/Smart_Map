"""FastAPI backend for Map Assistant"""
import os
import json
import logging
import asyncio
import math
import traceback
import re
import base64
import sqlite3
import uuid
import contextlib
import subprocess
import shutil
import tempfile
import threading
import time
import functools
from datetime import datetime as dt

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

# 在导入 torch 或 transformer 之前检查显卡设置
cuda_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "Not Set")
logging.info(f"Current CUDA_VISIBLE_DEVICES: {cuda_devices}")
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
from starlette.types import Scope, Receive, Send
import httpx
from pydantic import BaseModel
from decimal import Decimal


class SelectiveGZipMiddleware(GZipMiddleware):
    """SSE 端点禁用 GZip，避免流式响应被缓冲后一次性返回。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/chat/stream":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
# Configure logging
LOG_FILE = "/home/server/python/map_assistant_v1/backend/backend.log"
# 确保文件存在且可写
with open(LOG_FILE, "a") as f:
    f.write(f"\n--- Service Restart at {dt.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

# 定义统一的格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 创建文件处理器
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(formatter)

# 创建控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

# 特别确保 qwen_agent, tools 和 uvicorn 日志能写入文件
for name in ["qwen_agent", "qwen_agent_logger", "tools", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx"]:
    l = logging.getLogger(name)
    if name in ["httpx", "qwen_agent", "qwen_agent_logger"]:
        l.setLevel(logging.DEBUG)  # 开启 DEBUG 级别以查看详细过程
    else:
        l.setLevel(logging.INFO)
    l.addHandler(file_handler)
    # l.propagate = False # 允许日志传播到控制台，方便调试工具调用
    l.setLevel(logging.INFO) if name not in ["httpx", "qwen_agent", "qwen_agent_logger"] else l.setLevel(logging.DEBUG)

# Import tools
from qwen_agent.agents import Assistant
from tools.map_tool import MapTool, LocationSearchTool
from tools.postgresql_tool import PostgreSQLTool
import psycopg2
# 知识库后端选择（环境变量 KNOWLEDGE_BACKEND: ragflow|llamaindex，默认 ragflow）
_kb_backend = os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
if _kb_backend == "llamaindex":
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from tools.ragflow_knowledge_tool import KnowledgeBaseTool
from tools.knowledge_qa_agent import KnowledgeQAAgent
from tools.knowledge_graph_tool import get_kg  # 知识图谱工具
from tools.data_visualizer_tool import DataVisualizerTool
from tools.report_generator_tool import ReportGeneratorTool
from tools.weather_tool import WeatherTool
from tools.web_search_tool import WebSearchTool  # noqa: F401 — import 触发 qwen_agent 注册
from tools.cesium_tool import CesiumTool  # Cesium 3D 地图工具
from tools.gis_tool_router import router as gis_tool_router  # GIS 处理工具
from tools.spatial_reference_tool import SpatialReferenceTool  # 空间参考工具（红线/采区），触发注册

# 切片管理服务包：注册表/统计缓存/构建/后台任务（原 main.py 中切片逻辑拆分）
from services.tile_manager import (
    _3DTILES_DATA_DIR,
    _3DTILES_REGISTRY_PATH,
    _CUSTOM_TILE_DATA_DIR,
    _DRONE_BUILD_JOBS,
    _DRONE_IMAGERY_DIR,
    _DRONE_MBTILES_DIR,
    _DRONE_REGISTRY_PATH,
    _DRONE_WORK_DIR,
    _MAX_3DTILES_FILES,
    _MAX_3DTILES_UNZIP_BYTES,
    _MAX_3DTILES_ZIP_BYTES,
    _OVERLAY_DATA_DIR,
    _TILE_BUILD_JOBS,
    _TILE_LAYER_META,
    _TILE_REGISTRY_PATH,
    _VT_DIR,
    _3dtiles_layer_to_row,
    _auto_register_existing_3dtiles_async,
    _copy_upload_limited,
    _count_3dtiles,
    _dir_stats,
    _drone_layer_to_row,
    _extract_zip_safely,
    _invalidate_tile_stats,
    _load_3dtiles_registry,
    _load_drone_registry,
    _load_tile_registry,
    _locate_tileset_root,
    _mbtiles_metadata,
    _media_type_for_tile_format,
    _merged_tile_meta,
    _parse_bounds,
    _parse_style,
    _read_3dtiles_meta,
    _register_drone_imagery,
    _run_drone_build_with_progress,
    _run_drone_mbtiles_build,
    _run_tippecanoe,
    _sanitize_layer_key,
    _save_3dtiles_registry,
    _save_drone_registry,
    _save_tile_registry,
    _submit_tile_build_job,
    _ttl_cache,
    _write_3dtiles_meta,
)

from agents import TaskExecutor
# 阶段1：RunEngine（可中断执行引擎）接入
from agents.run_engine import is_confirm_message, parse_supplied_from_message
from agents.run_store import get_run_store
# 阶段4：上下文预算（历史读库裁剪 / 滚动摘要压缩）
from agents.context_manager import load_history_from_db, COMPRESS_THRESHOLD_TURNS
# 阶段4：视图提示词 SSoT（2D/3D 唯一权威版本）
from prompts import build_view_system_message, build_elevation_injection
from cesium_bridge_server import cesium_ws_endpoint, get_cesium_client_count

from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# SQLite 会话持久化
# ---------------------------------------------------------------------------
# 路径策略：优先环境变量 MAPASSIST_DB_PATH，默认项目 backend/sessions.db（不再硬编码旧机器路径）
# P1.5：env 读取统一收敛至 core/config.py，此处仅保留原常量名 re-export（routers 引用兼容）
# ---------------------------------------------------------------------------
from core.config import db as _cfg_db, falcon as _cfg_falcon, postgis as _cfg_postgis  # noqa: E402

DB_PATH = _cfg_db.path
_LEGACY_DB_PATH = "/home/server/python/map_assistant_v1/backend/sessions.db"
if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB_PATH):
    # 一次性迁移：旧机器硬编码路径存在而新路径不存在时复制过来
    try:
        shutil.copy2(_LEGACY_DB_PATH, DB_PATH)
        print(f"[init_db] 已从旧路径迁移 sessions.db: {_LEGACY_DB_PATH} -> {DB_PATH}")
    except Exception as e:  # noqa: BLE001
        print(f"[init_db] 旧 sessions.db 迁移失败（继续用新路径）: {e}")

# Falcon 目标识别：检测脚本 + 常驻推理服务
# 脚本（falcon_detect.py）负责影像获取/瓦片/融合/GeoJSON，模型推理走常驻服务
# falcon_service.py（FALCON_SERVICE_URL），避免每请求冷加载模型。
_FALCON_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "falcon_detect.py")
_FALCON_PYTHON_BIN = _cfg_falcon.python_bin
FALCON_SERVICE_URL = _cfg_falcon.service_url

# PostgreSQL 连接配置（环境变量注入，见 .env GEOSERVER_PG_*）
_PG_CONN = _cfg_postgis.as_dict()

def init_db():
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
        )
        # 阶段D：用户事实记忆（跨会话长期记忆，全局共享）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT,
                evidence TEXT,
                source_session TEXT,
                hits INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                last_seen_at TEXT
            )
        """)
        conn.commit()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def now_iso():
    return dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


async def _daily_run_cleanup():
    """每日清理过期 run 数据（终态且超 7 天）。"""
    while True:
        await asyncio.sleep(86400)
        try:
            removed = get_run_store().cleanup(keep_days=7)
            if removed:
                logger.info(f"[run-cleanup] 已清理 {removed} 个过期 run 及其事件/检查点")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[run-cleanup] 清理失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global task_executor, bot
    init_db()
    # 启动时清理过期 run 数据，并启动每日定时清理
    try:
        removed = get_run_store().cleanup(keep_days=7)
        if removed:
            logger.info(f"[lifespan] 启动清理：移除 {removed} 个过期 run")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lifespan] run 清理失败: {e}")
    app.state.run_cleanup_task = asyncio.create_task(_daily_run_cleanup())
    task_executor = init_task_executor()
    bot = init_agent()
    yield

app = FastAPI(title="Map Assistant API", version="1.0.0", lifespan=lifespan)

# Mount static files for reports
app.mount("/static", StaticFiles(directory="/home/server/python/map_assistant_v1/backend/static"), name="static")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SelectiveGZipMiddleware, minimum_size=500)

# 注册 Cesium WebSocket 端点
app.add_api_websocket_route("/ws/cesium", cesium_ws_endpoint)

# 注册 GIS 处理工具路由
app.include_router(gis_tool_router)

# 注册服务化反向代理（前端控制台 → tool-hub:8011 / intent-service:8010）
from services.service_proxy import router as service_proxy_router
app.include_router(service_proxy_router)

bot = None
task_executor = None
LLM_CFG = None











# ============================================================
# 图片 OCR 预处理：使用 qwen-vl-ocr 提取文字后注入到对话
# ============================================================
OCR_MODEL = 'qwen-vl-ocr-2025-11-20'
OCR_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
OCR_API_KEY = 'sk-e4990da94bfb4037be1f755fa586d048'


UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static")







def init_task_executor():
    global LLM_CFG
    LLM_CFG = {
        'model': 'qwen-flash-2025-07-28',
        'model_server': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'api_key': 'sk-e4990da94bfb4037be1f755fa586d048',
        'generate_cfg': {
            'extra_body': {
                'enable_thinking': False,
            },
        },
    }
    return TaskExecutor(LLM_CFG)


def init_agent():
    global LLM_CFG
    LLM_CFG = {
        'model': 'qwen-flash-2025-07-28',
        'model_server': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'api_key': 'sk-e4990da94bfb4037be1f755fa586d048',
        'generate_cfg': {
            'extra_body': {
                'enable_thinking': False,
            },
        },
    }
    
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
        llm=LLM_CFG,
        function_list=tools,
        name='Qwen3 地图助手',
        description="""你是一个专业的地图与数据分析助手，负责完成地图展示、数据库分析、政策查询和数据可视化任务。

------------------------------------------------------------
一、核心规则（最高优先级）
------------------------------------------------------------

1. 地图展示类任务
当用户提出以下需求时：
- "上图"
- "加载数据"
- "查看位置"
- "展示某砂场"

**根据当前视图选择工具（会话开头系统消息会指明）：**
- 2D 地图视图：调用 map_tool(action='load_vector_layer')
- 3D Cesium 视图：调用 cesium_tool(action='addGeoJsonLayer')
- 3D Cesium 视图且用户要求“测深风险/超深风险/风险柱/三维柱状展示”时：调用 cesium_tool(action='addDepthColumns')

如果是矢量图层加载，严格要求：
- 2D 模式必须使用 table_name='ceshen'
- 必须根据用户提到的名称设置 filter
- 严禁调用 location_search 或 add_marker
- 严禁在文本中返回经纬度

示例：

map_tool(
action='load_vector_layer',
table_name='ceshen',
filter="\"Mineable_Area_Name\"='种子场可采区'",
layer_name='种子场可采区'
)

严禁错误：
用户说 **种子场** → 加载 **潘庄砂场**

------------------------------------------------------------

2. 数据查询类任务

所有业务数据均来自 PostgreSQL 表：

ceshen

查询规则：

① 必须先调用

postgresql_tool(operation='get_db_schema')

② 字段必须加双引号，例如：

"Mineable_Area_Name"
"Measured_Depth"
"Control_Elevation"

③ 查询示例：

SELECT
AVG("Measured_Depth")
FROM ceshen

------------------------------------------------------------

3. 数据可视化任务

当用户提出：

- 生成图表
- 数据可视化
- 对比分析
- 统计并展示为柱状图/折线图/饼图

必须调用：

data_visualizer_tool

禁止行为：

- 禁止生成 Markdown 图片链接
- 禁止编写 Python 代码画图
- 禁止返回 Matplotlib 图

------------------------------------------------------------

4. 报告生成

只有用户明确提出：

- 生成报告
- 出具报告
- 形成文档

才允许调用：

report_generator_tool

报告生成流程（必须严格遵循）：

① 调用 knowledge_base_tool(operation='search', query='报告主题相关的政策、规范') 检索知识库
② 调用 postgresql_tool 获取业务数据（只使用真实查询结果，严禁虚构任何数据）
③ 调用 report_generator_tool 传入变量

【变量内容规范——必须严格遵守】

summary（摘要）：
- 简要说明报告背景、数据来源、整体结论
- 引用知识库中的相关政策或规范名称（不要复制原文，用自己的话概括）
- 只能使用数据库查询到的真实数据，不得捏造数字

details（详细数据）：
- 只填写真实的数据库查询结果
- 格式示例："查询返回 XX 条记录，其中 AA 字段均值为 BB，最大值为 CC..."
- 严禁虚构任何测量值、数量、地点或技术参数

knowledge_content（由工具自动填充）：
- 工具会自动从知识库检索并填充
- 无需手动传入，但如需传入，必须是经过归纳的文字，不得直接复制原始切片

conclusion（结论）：
- 结合知识库政策规范 + 数据库实际数据，得出综合结论
- 必须有依据，禁止主观推断或捏造

示例：
用户：生成固始县砂场超深度开采分析报告

正确流程：
1. knowledge_base_tool(operation='search', query='超深度开采判定规则 固始县')
2. postgresql_tool(operation='query', sql='SELECT "Mineable_Area_Name", AVG("Control_Elevation"-"Measured_Depth") as avg_depth FROM ceshen WHERE "County_District"=\'固始县\' GROUP BY "Mineable_Area_Name"')
3. report_generator_tool(variables={
     'report_title': '固始县砂场超深度开采分析报告',
     'summary': '本报告依据信阳市智慧巡河监管要求，对固始县XX个砂场进行超深度开采核查。（引用政策规范名称，不照搬原文）',
     'details': '数据库查询结果：固始县共有XX个砂场，平均超深XX米，最大超深砂场为XX（仅填真实查询值）',
     'conclusion': '综合以上数据与政策标准，建议...'
   })

------------------------------------------------------------

二、工具调用优先级（必须严格遵循）

工具调用顺序：

知识库 → 数据库 → 地图

规则：

① 政策 / 流程 / 操作文档问题

必须先调用

knowledge_base_tool(operation='search')

示例：

疏浚工程申请延期如何修改结束日期？

不得查询数据库。

② 数据统计问题

调用

postgresql_tool

③ 地图展示问题

根据当前视图选择工具：
- 2D 地图视图：调用 map_tool
- 3D Cesium 视图：调用 cesium_tool

如果会话开头有系统消息指明当前视图，必须严格按照指示选择工具。



------------------------------------------------------------

三、需求分析机制（必须执行）

在执行任务前必须进行 **需求分析**。

输出：

需求分析：
- 问题类型
- 目标输出
- 关键约束

示例：

需求分析

问题类型：数据分析  
目标输出：统计结果  
关键约束：采区名称

------------------------------------------------------------

四、任务拆解

根据需求分析生成执行计划：

子任务 | 工具 | 输出
---|---|---
查询数据库 | postgresql_tool | 数据表
生成图表 | data_visualizer_tool | 可视化图

------------------------------------------------------------

五、核心业务逻辑：实测高程 vs 控制高程

**警告：严禁将"实测高程"称为"实测深度"！两者物理意义相反！**

1. 字段物理意义：
   - Measured_Depth：代表"实测高程"（海拔高度）。
   - Control_Elevation：代表"控制高程"（准许挖掘的最低海拔）。

2. 判定逻辑：
   - 整体超深度开采定义：整个砂场的（实测高程-控制高程）测深点的 **平均差值超过 2米**，即定义为超深度开采。
     即：AVG(Control_Elevation - Measured_Depth) > 2。
   - 否则：视为整体合规（未构成超深度开采）。
   - 注意：如果平均差值未超过 2米，即使存在个别点位差值较大，也不能将该砂场定性为"超深度开采"。

3. 潘庄砂场专项修正：
   - 实测高程 (~30.5m) 远高于 控制高程 (17.1m)。
   - **结论：潘庄砂场完全不超深！** 它是未挖到控制深度，属于安全/合规状态。

4. 强制回答规范：
   - 若 平均差值 > 2m，回答格式："{区域}存在超深度开采。平均实测高程比控制高程低 {diff}m，超过 2m 允许范围。"
   - 否则，回答格式："{区域}未构成超深度开采。整体平均实测高程符合控制要求（平均偏差在 2m 以内）。"

------------------------------------------------------------

六、输出格式规范

回答必须结构化。

结构：

结论
概述
要点
建议

要求：

- 使用短句
- 使用 4–6 条要点
- 禁止长段落
- 先结论再解释

------------------------------------------------------------

七、禁止行为

以下行为绝对禁止：

1. 政策问题查询数据库
2. 地图加载调用 knowledge_base_tool
3. 未经请求主动上图
4. 生成 Python 图表
5. 返回 Markdown 图片

------------------------------------------------------------

八、交互确认

地图加载成功后，回复语必须与用户请求 **完全一致**。

示例：

"种子场可采区数据已成功加载到地图。"

------------------------------------------------------------

九、空间参考数据（红线/采区边界等）

系统内置了空间参考图层，可通过 spatial_reference_tool 查询：

| 图层 key | 名称 | 说明 |
|----------|------|------|
| hx | 河道管理红线 | 河道管理范围法定边界 |
| caiqu | 2025年可采区边界 | 许可采砂区域空间范围 |

**自动触发规则**：
- 用户提及"红线""河道红线""红线范围" → 自动关联 hx 图层
- 用户提及"采区""可采区""采砂区范围" → 自动关联 caiqu 图层

**使用流程**（当用户问题涉及空间参考数据时）：
1. 调用 spatial_reference_tool(action='get_geometry', layer='hx'或'caiqu') 获取参考几何
2. 将几何作为 WKT 传给 postgresql_tool(operation='spatial_query') 做空间筛选
3. 或传给 map_tool 将参考数据叠加到地图显示

示例：用户问"红线附近的采砂场有哪些"
步骤① spatial_reference_tool(action='get_geometry', layer='hx')
步骤② postgresql_tool(operation='spatial_query', spatial_table='ceshen', spatial_op='within', spatial_geom_wkt='<步骤①返回的几何WKT>')
        """
    )

# 工具实例缓存（延迟初始化，第一次调用时创建）
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
            from tools.spatial_reference_tool import SpatialReferenceTool
            _TOOL_INSTANCES[name] = SpatialReferenceTool()
        else:
            return name  # 未知工具回退字符串
    return _TOOL_INSTANCES[name]

def build_assistant_with_tools(function_list):
    # 优先使用工具实例，避免字符串名称查找失败
    resolved = [_get_tool_instance(name) for name in function_list]
    return Assistant(
        llm=LLM_CFG,
        function_list=resolved,
        name='Qwen3 地图助手',
        description="""地图数据加载任务，仅使用地图与数据库工具。"""
    )

@app.get("/")
async def root():
    return {"message": "Map Assistant API is running"}

# ==============================
# ✅ 优化：动态加载矢量数据接口
# ==============================
def _resolve_vector_filter(pg_tool, safe_table_name: str, filter_text: str,
                           table_cols_lower: dict) -> Optional[str]:
    """校验 filter 中引用的字段是否存在于目标表。

    - 字段是目标表真实列 → 保留原样；
    - 目标表是 jsonb 属性表（如 caiqu/hx）且 properties 中存在该键
      → 改写为 (properties->>'字段')；
    - 字段既不是列也不在 properties 中 → 丢弃整个 filter（加载全部要素），
      避免"字段不存在"导致整表查询失败。
    """
    refs = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', filter_text or "")
    resolved = filter_text
    for field in refs:
        if field.lower() in table_cols_lower:
            continue  # 真实列，保留原样
        if "properties" in table_cols_lower:
            try:
                probe = pg_tool.call({
                    "operation": "query",
                    "sql": f"SELECT properties ? %s AS has_key FROM {safe_table_name} LIMIT 1",
                    "params": [field],
                })
                row = (probe.get("data") or [{}])[0] if probe.get("success") else {}
                if row.get("has_key"):
                    resolved = resolved.replace(
                        f'"{field}"', f"(properties->>'{field}')"
                    )
                    continue
            except Exception as e:
                logger.warning(f"Vector API properties probe failed for '{safe_table_name}': {e}")
        logger.warning(
            f"Vector API dropping filter for table '{safe_table_name}': "
            f"field '{field}' not found in columns or properties"
        )
        return None
    return resolved


@app.get("/api/vector-data")
async def get_vector_data(
    table_name: str,
    geom_col: str = 'geom',
    properties: str = None,
    filter: str = None,
    color_expression: str = None,
    debug: bool = False
):
    """
    动态获取指定表的 GeoJSON 数据
    :param table_name: 数据库表名
    :param geom_col: 几何列名，默认为 'geom'
    :param properties: 需要包含在 properties 中的字段名，逗号分隔。
    :param filter: SQL 过滤条件 (WHERE 后的内容，如 "name='xxx'")
    :param color_expression: SQL 颜色表达式，例如 "CASE WHEN depth < 10 THEN 'red' ELSE 'blue' END"
    """
    # 兼容性处理：如果请求的是旧表名 mineable_areas，自动映射到新表 ceshen
    target_table = table_name.strip().lower()
    if target_table == 'mineable_areas' or target_table == '"mineable_areas"':
        logger.info(f"Redirecting table_name from '{table_name}' to 'ceshen'")
        table_name = 'ceshen'

    try:
        logger.info(f"Vector API request: table_name={table_name}, geom_col={geom_col}, properties={properties}, filter={filter}, color_expression={color_expression}, debug={debug}")
        # 安全性校验：允许字母、数字、下划线、双引号、单引号、等号、空格和中文字符
        # 注意：此处 filter 校验需要比较宽松，但也需防止恶意 SQL 注入
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s\'\.\(\)\=\!\<\>\-\+]+$', table_name):
            raise HTTPException(status_code=400, detail="无效的表名格式")

        pg_tool = PostgreSQLTool(cfg={
            'host': '172.136.16.52',
            'port': 5432,
            'database': 'postgres',
            'user': 'postgres',
        })

        # 修复 color_expression 中的字段引用，增加表别名 t. 以避免字段不存在报错
        safe_color_expression = color_expression
        if color_expression:
            # 匹配双引号中的字段名，例如 "Measured_Depth" -> "t"."Measured_Depth"
            safe_color_expression = re.sub(r'("([a-zA-Z0-9_]+)")', r'"t".\1', color_expression)

        # 构建属性 JSON 对象
        if properties:
            props_list = [p.strip() for p in properties.split(',')]
            # 确保关键字段始终包含在内，用于前端 Popup 显示（仅限目标表实际存在的字段）
            essential_fields = [
                '"Mineable_Area_Name"', '"Measured_Depth"', '"Control_Elevation"',
                '"Lon_4326"', '"Lat_4326"', '"Year"', '"Mineable_Area_ID"', '"County_District"'
            ]
            for field in essential_fields:
                clean_field = field.replace('"', '')
                if clean_field.lower() in table_cols_lower and clean_field not in props_list:
                    # 用表中实际列名（兼容大小写）追加，避免引用不存在的字段导致查询失败
                    props_list.append(table_cols_lower[clean_field.lower()])
            
            # 修复：避免在 f-string 表达式中使用反斜杠
            formatted_props = []
            for p in props_list:
                if not p.startswith('"'):
                    formatted_props.append(f"'{p}', \"t\".\"{p}\"")
                else:
                    clean_p = p.replace('"', '')
                    formatted_props.append(f"'{clean_p}', \"t\".{p}")
            
            props_json = ", ".join(formatted_props)
            if safe_color_expression:
                props_json += f", '_style_color', {safe_color_expression}"
            props_sql = f"json_build_object({props_json})"
        else:
            if safe_color_expression:
                props_sql = f"(row_to_json(t)::jsonb - '{geom_col}' || jsonb_build_object('_style_color', {safe_color_expression}))::json"
            else:
                props_sql = f"(row_to_json(t)::jsonb - '{geom_col}')::json"

        safe_table_name = table_name if table_name.startswith('"') else f'"{table_name}"'

        # 查询目标表实际列名，动态决定是否启用经纬度回退（caiqu/hx 等 jsonb 表无 Lon_4326/Lat_4326 列）
        table_cols_lower = {}
        try:
            col_res = pg_tool.call({
                'operation': 'query',
                'sql': """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND LOWER(table_name) = LOWER(%s)
                """,
                'params': [table_name.strip('"')]
            })
            if col_res.get('success'):
                table_cols_lower = {str(r.get('column_name')).lower(): str(r.get('column_name'))
                                    for r in (col_res.get('data') or []) if r.get('column_name')}
        except Exception as e:
            logger.warning(f"Vector API failed to fetch columns for '{table_name}': {e}")
        has_lonlat = 'lon_4326' in table_cols_lower and 'lat_4326' in table_cols_lower
        lon_col = table_cols_lower.get('lon_4326') if has_lonlat else None
        lat_col = table_cols_lower.get('lat_4326') if has_lonlat else None
        lonlat_case = ""
        if has_lonlat:
            lonlat_case = (
                f'WHEN "{lon_col}" IS NOT NULL AND "{lat_col}" IS NOT NULL THEN\n'
                f'                                    ST_SetSRID(ST_MakePoint("{lon_col}", "{lat_col}"), 4326)\n'
            )

        # 处理过滤条件（包含几何或经纬度回退；经纬度回退仅对含经纬度列的表生效）
        where_geom_valid = f"({geom_col} IS NOT NULL)"
        if has_lonlat:
            where_lonlat_valid = f"(\"{lon_col}\" IS NOT NULL AND \"{lat_col}\" IS NOT NULL)"
            where_clause = f"WHERE ({where_geom_valid} OR {where_lonlat_valid})"
        else:
            where_clause = f"WHERE {where_geom_valid}"
        # 校验 filter 引用的字段是否存在于目标表；不存在时改写到 jsonb properties
        # 或直接丢弃 filter（加载全部要素），避免"字段不存在"导致整表查询失败。
        if filter:
            filter = _resolve_vector_filter(pg_tool, safe_table_name, filter, table_cols_lower)
        if filter:
            where_clause += f" AND ({filter})"

        def build_empty_meta(count_filter: str):
            count_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE {count_filter};"
            geom_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL);"
            geom_valid_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL AND ST_IsValid({geom_col}));"
            lonlat_res = {'success': False, 'error': None}
            if has_lonlat:
                lonlat_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND (\"{lon_col}\" IS NOT NULL AND \"{lat_col}\" IS NOT NULL);"
                lonlat_res = pg_tool.call({'operation': 'query', 'sql': lonlat_sql, 'params': []})
            count_res = pg_tool.call({'operation': 'query', 'sql': count_sql, 'params': []})
            geom_res = pg_tool.call({'operation': 'query', 'sql': geom_sql, 'params': []})
            geom_valid_res = pg_tool.call({'operation': 'query', 'sql': geom_valid_sql, 'params': []})
            return {
                "matched_total": (count_res.get("data") or [{}])[0].get("cnt") if count_res.get("success") else None,
                "geom_total": (geom_res.get("data") or [{}])[0].get("cnt") if geom_res.get("success") else None,
                "geom_valid_total": (geom_valid_res.get("data") or [{}])[0].get("cnt") if geom_valid_res.get("success") else None,
                "lonlat_total": (lonlat_res.get("data") or [{}])[0].get("cnt") if lonlat_res.get("success") else None,
                "matched_total_error": None if count_res.get("success") else count_res.get("error"),
                "geom_total_error": None if geom_res.get("success") else geom_res.get("error"),
                "geom_valid_total_error": None if geom_valid_res.get("success") else geom_valid_res.get("error"),
                "lonlat_total_error": None if lonlat_res.get("success") else lonlat_res.get("error"),
                "where_clause": where_clause,
            }

        sql = f"""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(
                json_agg(
                    json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(
                            CASE 
                                WHEN {geom_col} IS NOT NULL THEN 
                                    CASE 
                                        WHEN ST_SRID({geom_col}) = 0 THEN ST_SetSRID(ST_MakeValid({geom_col}), 4326)
                                        ELSE ST_MakeValid({geom_col})
                                    END
                                {lonlat_case}                                ELSE NULL
                            END, 6
                        )::json,
                        'properties', {props_sql}
                    )
                ), 
                '[]'::json
            )
        ) AS geojson
        FROM {safe_table_name} AS t
        {where_clause};
        """

        res = pg_tool.call({'operation': 'query', 'sql': sql, 'params': []})
        if not res.get('success'):
            error_msg = res.get('error', '数据库查询失败')
            logger.warning(f"Vector API query failed for table '{table_name}': {error_msg}")
            # 优雅降级：返回空 FeatureCollection 而非 500，让前端正常处理
            return JSONResponse(
                content={
                    "type": "FeatureCollection",
                    "features": [],
                    "meta": {
                        "status": "error",
                        "message": f"数据表 '{table_name}' 查询失败: {error_msg}",
                        "table_name": table_name,
                        "applied_filter": filter,
                    }
                },
                headers={"Cache-Control": "public, max-age=60"}
            )
        rows = res.get('data') or []

        # 保留失败容错：若两次查询均异常，返回空集合

        if not rows or len(rows) == 0:
            sql2 = f"""
            SELECT 
                ST_AsGeoJSON(
                    CASE 
                        WHEN {geom_col} IS NOT NULL THEN 
                            CASE 
                                WHEN ST_SRID({geom_col}) = 0 THEN ST_SetSRID(ST_MakeValid({geom_col}), 4326)
                                ELSE ST_MakeValid({geom_col})
                            END
                        {lonlat_case}                        ELSE NULL
                    END, 6
                ) AS geom_json,
                (row_to_json(t)::jsonb - '{geom_col}')::json AS props
            FROM {safe_table_name} AS t
            {where_clause};
            """
            res2 = pg_tool.call({'operation': 'query', 'sql': sql2, 'params': []})
            rows2 = res2.get('data') or []
            features2 = []
            for r in rows2:
                gj = r.get("geom_json")
                if not gj:
                    continue
                try:
                    geom = json.loads(gj)
                except:
                    geom = None
                props = r.get("props") or {}
                if geom:
                    features2.append({"type": "Feature", "geometry": geom, "properties": props})
            if features2:
                fc = {"type": "FeatureCollection", "features": features2, "meta": {"feature_count": len(features2), "table_name": table_name, "applied_filter": filter}}
                if debug:
                    fc["_debug"] = {"sql": sql2}
                return JSONResponse(content=fc, headers={"Cache-Control": "public, max-age=60"})
            count_filter = f"({filter})" if filter else "TRUE"
            meta = build_empty_meta(count_filter)
            logger.info(f"Vector query returned no rows: table={table_name}, filter={filter}, meta={meta}")
            content = {
                "type": "FeatureCollection",
                "features": [],
                "meta": {
                    "status": "empty",
                    "message": "查询成功但无可用要素",
                    "applied_filter": filter,
                    "table_name": table_name,
                    **meta
                }
            }
            if debug:
                content["_debug"] = {"sql": sql, **meta}
            return JSONResponse(content=content, headers={"Cache-Control": "public, max-age=60"})

        geojson = rows[0].get('geojson')
        if isinstance(geojson, str):
            try:
                geojson = json.loads(geojson)
            except Exception as e:
                logger.error(f"Vector API returned invalid JSON string: {e}")
                geojson = {"type": "FeatureCollection", "features": [], "meta": {"status": "invalid", "message": "后端返回数据格式异常"}}
        if not isinstance(geojson, dict):
            logger.error(f"Vector API returned non-dict geojson: {type(geojson)}")
            geojson = {"type": "FeatureCollection", "features": [], "meta": {"status": "invalid", "message": "后端返回数据格式异常"}}
        features = geojson.get("features")
        if not isinstance(features, list):
            features = []
            geojson["features"] = features
        feature_count = len(features)
        meta = geojson.get("meta") if isinstance(geojson.get("meta"), dict) else {}
        meta.update({"feature_count": feature_count, "table_name": table_name, "applied_filter": filter})
        geojson["meta"] = meta
        if feature_count == 0:
            count_filter = f"({filter})" if filter else "TRUE"
            empty_meta = build_empty_meta(count_filter)
            meta.update({"status": "empty", "message": "查询成功但无可用要素", **empty_meta})
            logger.info(f"Vector query returned empty features: table={table_name}, filter={filter}, meta={empty_meta}")
            if debug:
                geojson["_debug"] = {"sql": sql, **empty_meta}
        elif debug:
            geojson["_debug"] = {"sql": sql, "where_clause": where_clause}
        return JSONResponse(
            content=geojson,
            headers={"Cache-Control": "public, max-age=60"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Vector API error for table {table_name}")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")



# ==============================
# 报告文件强制下载接口
# ==============================


# ==============================
# 报告下载接口（无 .docx 后缀，防止迅雷等下载管理器拦截）
# ==============================


# ==============================
# 报告在线预览接口（docx 转 HTML）
# ==============================


# ==============================
# 图片上传接口（用于聊天中的多模态图片上传）
# ==============================
UPLOAD_IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_IMAGES_DIR, exist_ok=True)



# ==============================
# SHP 文件上传接口（ZIP 包，解压后供 QGIS MCP 空间分析使用）
# ==============================
SHP_UPLOAD_DIR = "/home/server/python/GIS/uploads"
os.makedirs(SHP_UPLOAD_DIR, exist_ok=True)

REQUIRED_SHP_EXTENSIONS = {".shp", ".dbf", ".shx"}



# ==============================
# GeoJSON 文件读取接口（供前端矢量图层加载使用）
# ==============================
GEOJSON_DIR = os.path.join(os.path.dirname(__file__), "static", "geojson")



# ==============================
# 原有聊天接口（保持不变）
# ==============================

# run SSE 订阅空闲超时：超过该时长无事件则检查 run 状态（防哨兵丢失导致挂起）
RUN_SSE_IDLE_TIMEOUT = 120




# ---------------------------------------------------------------------------
# 阶段1：Run 生命周期端点（查询 / 断线补拉 / 取消）
# ---------------------------------------------------------------------------






from tools.overlay_tile_service import get_tile_png as _overlay_get_tile_png, list_layers as _overlay_list_layers, query_feature as _overlay_query_feature, register_layer as _overlay_register_layer, unregister_layer as _overlay_unregister_layer
from tools import geoserver_client as _gs_client
from tools.geoserver_client import GeoServerUnavailable as _GeoServerUnavailable

# GeoLibre 图层工作台默认样式（与 GeoLibre 项目 schema 对齐）
_GEOLIBRE_STYLE = {
    "minZoom": 0, "maxZoom": 24,
    "fillColor": "#1d4ed8", "strokeColor": "#1e3a8a", "strokeWidth": 2, "fillOpacity": 0.25,
    "circleRadius": 6, "textColor": "#111827", "textHaloColor": "#ffffff",
    "textHaloWidth": 2, "textSize": 16,
    "extrusionEnabled": False, "extrusionColor": "#3b82f6", "extrusionOpacity": 0.8,
    "extrusionHeightProperty": "height", "extrusionHeightScale": 1, "extrusionBase": 0,
    "extrusionAdvancedStyleEnabled": False, "extrusionColorExpression": "",
    "extrusionHeightExpression": "", "vectorStyleMode": "single", "vectorStyleProperty": "",
    "vectorStyleClassCount": 5, "vectorStyleColorRamp": "viridis",
    "vectorStyleClassificationScheme": "equal-interval",
    "vectorStyleStops": [{"value": 0, "color": "#dbeafe"}, {"value": 1, "color": "#2563eb"}],
    "vectorStyleExpression": "", "pointRenderer": "single",
    "heatmapRadius": 30, "heatmapIntensity": 1, "clusterRadius": 50, "clusterMaxZoom": 14,
    "rasterBrightnessMin": 0, "rasterBrightnessMax": 1, "rasterSaturation": 0,
    "rasterContrast": 0, "rasterHueRotate": 0,
}








_VT_DIR = os.path.join(os.path.dirname(__file__), "vector_tiles")











# ---------- 3D Tiles 管理 ----------

# 模块加载时自动注册（_auto_register_existing_3dtiles_async / _register_custom_raster_layers）
# 移至文件尾 routers 导入之后执行（函数已随 P0 拆分迁入 routers/tiles.py）


# ---------------------------------------------------------------------------
# 后台构建任务（tippecanoe / GeoServer 发布等耗时操作放入线程，避免阻塞服务）
# ---------------------------------------------------------------------------































# ---------- 3D Tiles 管理 API ----------















# 卫星影像动态切片的模块级 try 导入块已随 P0 拆分迁至 routers/tiles.py




# ==============================
# ✅ GeoServer 集成代理接口
# ==============================




































# ==============================
# ✅ 新增：会话管理接口
# ==============================








# ==============================
# 知识库管理接口 (RagFlow)
# ==============================
























def _is_explicit_map_request(user_content: str) -> bool:
    if not user_content:
        return False
    map_terms = [
        "地图", "上图", "加载", "图层", "定位", "跳转", "飞到", "显示到地图",
        "加载到地图", "标记", "标注", "打点", "落点", "经纬度", "卫星图", "底图",
        "切换", "卫星", "清除",
        # qgis_mcp_tool 产出结果也需要地图展示
        "中心点", "缓冲区", "buffer", "裁剪", "clip",
        # 空间分析类结果也需要地图展示
        "距离", "连线", "最近", "最短", "多远",
    ]
    return any(term in user_content for term in map_terms)


def _is_explicit_marker_request(user_content: str) -> bool:
    if not user_content:
        return False
    marker_terms = ["标记", "标注", "打点", "落点", "经纬度", "坐标点"]
    return any(term in user_content for term in marker_terms)


def _stable_json_key(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(payload)







# ==============================
# Falcon 目标识别接口
# ==============================




# ==============================
# 变化检测接口 — 已移除（统一为 Falcon 语义分割路径）
# ==============================


# ==============================
# 阈值配置接口 — 已移除（VLM 相关功能已删除）
# 检测路径统一为 Falcon-Perception 自由文本 query
# ==============================










# ==============================
# Falcon Requery 精修 API
# ==============================






# ---------------------------------------------------------------------------
# P0 重构：分域路由（chat/files/geolibre/tiles/geoserver/knowledge/memory/falcon）
# ---------------------------------------------------------------------------
# python main.py 直跑时模块名为 __main__，routers 内 `from main import ...`
# 会二次加载 main 造成循环导入；将 __main__ 别名注册为 'main'，
# 使 routers 引用当前已完成初始化的同一模块（uvicorn 以 main:app 导入时本就存在）。
import sys as _sys
if __name__ == "__main__":
    _sys.modules.setdefault("main", _sys.modules["__main__"])

from routers.chat import router as chat_router
from routers.files import router as files_router
from routers.geolibre import router as geolibre_router
from routers.tiles import router as tiles_router
from routers.geoserver import router as geoserver_router
from routers.knowledge import router as knowledge_router
from routers.memory import router as memory_router
from routers.falcon import router as falcon_router

app.include_router(chat_router)
app.include_router(files_router)
app.include_router(geolibre_router)
app.include_router(tiles_router)
app.include_router(geoserver_router)
app.include_router(knowledge_router)
app.include_router(memory_router)
app.include_router(falcon_router)

# 模块加载时自动注册已有数据集与自定义栅格图层（原模块级调用，随 P0 拆分移至此处：
# routers 导入后、uvicorn 启动前，仍在 lifespan 之前执行）
from services.tile_manager import _auto_register_existing_3dtiles_async  # noqa: E402
from routers.tiles import _register_custom_raster_layers  # noqa: E402
_auto_register_existing_3dtiles_async()
_register_custom_raster_layers()


if __name__ == "__main__":
    import uvicorn
    import socket
    from core.config import server
    _port = server.port
    # 创建带 SO_REUSEADDR 的 socket，避免 PM2 重启时端口抢占导致启动失败
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", _port))
    sock.listen(2048)
    uvicorn.run(app, fd=sock.fileno())
