"""FastAPI backend for Map Assistant"""
import os
import json
import logging
import asyncio
import traceback
import re
import base64
import sqlite3
import uuid
import contextlib
import subprocess
import shutil
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
from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Query
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
    f.write(f"\n--- Service Restart at {re.sub(r'[^0-9-]', '', '2026-01-17')} ---\n")

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
from tools.data_visualizer_tool import DataVisualizerTool
from tools.report_generator_tool import ReportGeneratorTool
from tools.weather_tool import WeatherTool
from tools.cesium_tool import CesiumTool  # Cesium 3D 地图工具
from tools.gis_tool_router import router as gis_tool_router  # GIS 处理工具

from agents import TaskExecutor
from cesium_bridge_server import cesium_ws_endpoint, get_cesium_client_count

from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# SQLite 会话持久化
# ---------------------------------------------------------------------------
DB_PATH = "/home/server/python/map_assistant_v1/backend/sessions.db"

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
        conn.commit()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def now_iso():
    return dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global task_executor, bot
    init_db()
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

bot = None
task_executor = None
LLM_CFG = None

class ChatMessage(BaseModel):
    role: str
    content: str
    images: Optional[List[str]] = None  # 图片 URL 列表（用于多模态消息）

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    active_view: str = 'map'  # 'map'(2D) | 'cesium'(3D) | 'kb'
    session_id: Optional[str] = None  # 可选会话 ID，用于 MemorySaver thread_id 隔离

class ScreenshotRequest(BaseModel):
    image_data: str
    file_name: Optional[str] = None

class KnowledgeItem(BaseModel):
    title: str
    content: str
    tags: List[str] = []

class KnowledgeQARequest(BaseModel):
    question: str
    top_k: int = 5

class KnowledgeAddRequest(BaseModel):
    name: str
    content: str

class ChatResponse(BaseModel):
    response: str
    messages: List[Dict[str, Any]]
    map_commands: List[Dict[str, Any]] = []
    cesium_commands: List[Dict[str, Any]] = []
    charts: List[Dict[str, Any]] = []
    intent_info: Optional[Dict[str, Any]] = None


def _extract_text_content(content) -> str:
    """从消息内容中提取纯文本。若为多模态数组，取第一个 text 部分。"""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
        return ""
    return content or ""


# ============================================================
# 图片 OCR 预处理：使用 qwen-vl-ocr 提取文字后注入到对话
# ============================================================
OCR_MODEL = 'qwen-vl-ocr-2025-11-20'
OCR_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
OCR_API_KEY = 'sk-e4990da94bfb4037be1f755fa586d048'


UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static")

async def _ocr_image(image_url: str) -> str:
    """使用 qwen-vl-ocr 专用模型从图片中提取文字内容。
    
    image_url 可以是：
    - 本地路径（如 /static/uploads/xxx.png），会自动转为 base64 data URL
    - 完整 HTTP(S) URL（DashScope 需要能访问）
    """
    # 将本地路径转为 base64 data URL（DashScope 无法访问内网地址）
    if image_url.startswith('/'):
        local_path = os.path.join(os.path.dirname(__file__), image_url.lstrip('/'))
        if not os.path.isfile(local_path):
            # 尝试相对于 static 目录
            local_path = os.path.join(UPLOAD_DIR, image_url.lstrip('/'))
        if os.path.isfile(local_path):
            with open(local_path, 'rb') as f:
                img_data = f.read()
            # 探测 MIME 类型
            ext = os.path.splitext(local_path)[1].lower()
            mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp'}
            mime = mime_map.get(ext, 'image/png')
            image_url = f"data:{mime};base64,{base64.b64encode(img_data).decode()}"
            logger.info(f"[OCR] 本地图片转 base64: {local_path} ({len(img_data)} bytes)")
        else:
            logger.error(f"[OCR] 图片文件不存在: {local_path}")
            return ""
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{OCR_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OCR_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OCR_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一个精确的文字提取工具。只输出图片中的纯文字内容，不要添加任何坐标框、边框标记、位置标注或额外说明。保持原始格式、精度和顺序。"
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "提取图片中所有文字，只输出纯文本，不要任何坐标标注或边框信息。"
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            }
                        ]
                    }
                ],
                "max_tokens": 4000,
            }
        )
        if response.status_code != 200:
            logger.error(f"[OCR] API 请求失败 HTTP {response.status_code}: {response.text[:300]}")
            return ""
        result = response.json()
        text = result["choices"][0]["message"]["content"]
        logger.info(f"[OCR] 提取成功 ({len(text)} 字符): {text[:200]}...")
        return text


async def _preprocess_excel(file_url: str) -> str:
    """使用 openpyxl 解析 Excel 文件，将表格数据格式化为文本。"""
    local_path = os.path.join(os.path.dirname(__file__), file_url.lstrip('/'))
    if not os.path.isfile(local_path):
        # 尝试相对于 static 目录
        local_path = os.path.join(UPLOAD_DIR, file_url.lstrip('/'))
    if not os.path.isfile(local_path):
        logger.error(f"[Excel] 文件不存在: {file_url} -> {local_path}")
        return "[Excel 文件读取失败]"
    try:
        import openpyxl
        wb = openpyxl.load_workbook(local_path, data_only=True)
        parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            if ws.max_row == 0:
                continue
            rows = []
            for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200), values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                rows.append(" | ".join(cells))
            if rows:
                parts.append(f"【Excel 文件内容（{sheet_name}）】\n" + "\n".join(rows))
        wb.close()
        if parts:
            result = "\n\n".join(parts)
            logger.info(f"[Excel] 解析成功: {file_url} ({ws.max_row} 行)")
            return result
        return "[Excel 文件为空]"
    except Exception as e:
        logger.error(f"[Excel] 解析失败: {e}")
        return f"[Excel 文件解析失败: {e}]"


async def _preprocess_message_images(messages: list) -> list:
    """对用户消息中的附件进行预处理。
    
    - 图片：用 qwen-vl-ocr 提取文字
    - Excel：用 openpyxl 解析表格
    预处理结果注入到消息 content 中，并移除 images 字段。
    """
    for msg in messages:
        attachments = msg.get('images')
        if msg.get('role') != 'user' or not attachments or not isinstance(attachments, list):
            continue

        text_content = msg.get('content', '')
        all_results = []
        for file_url in attachments:
            is_excel = file_url.lower().endswith(('.xlsx', '.xls'))
            is_image = file_url.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'))
            
            if is_excel:
                logger.info(f"[Excel] 开始处理: {file_url}")
                try:
                    result = await _preprocess_excel(file_url)
                    if result.strip():
                        all_results.append(result)
                except Exception as e:
                    logger.error(f"[Excel] 处理异常: {e}")
                    all_results.append(f"[Excel 读取失败: {e}]")
            elif is_image:
                logger.info(f"[OCR] 开始处理图片: {file_url}")
                try:
                    ocr_text = await _ocr_image(file_url)
                    if ocr_text.strip():
                        all_results.append(ocr_text)
                except Exception as e:
                    logger.error(f"[OCR] 图片处理异常: {e}")
                    all_results.append(f"[图片 OCR 失败: {e}]")
            else:
                logger.warning(f"[预处理] 跳过不支持的文件: {file_url}")

        if all_results:
            prefix = "【以下是从上传文件中自动提取的内容】\n"
            msg['content'] = text_content + "\n\n" + prefix + "\n---\n".join(all_results)
            logger.info(f"[预处理] 已将 {len(all_results)} 个文件内容注入到用户消息")
        
        # 移除 images 字段——主模型 qwen-flash 不支持，且文件内容已注入
        msg.pop('images', None)
    
    return messages


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
        'cesium_tool',  # Cesium 3D 地图工具
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
            # 确保关键字段始终包含在内，用于前端 Popup 显示
            essential_fields = [
                '"Mineable_Area_Name"', 
                '"Measured_Depth"', 
                '"Control_Elevation"', 
                '"Lon_4326"', 
                '"Lat_4326"', 
                '"Year"',
                '"Mineable_Area_ID"',
                '"County_District"'
            ]
            for field in essential_fields:
                clean_field = field.replace('"', '')
                if clean_field not in props_list:
                    props_list.append(clean_field)
            
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
        
        # 处理过滤条件（包含几何或经纬度回退）
        where_geom_valid = f"({geom_col} IS NOT NULL)"
        where_lonlat_valid = "\"Lon_4326\" IS NOT NULL AND \"Lat_4326\" IS NOT NULL"
        where_clause = f"WHERE ({where_geom_valid} OR ({where_lonlat_valid}))"
        if filter:
            where_clause += f" AND ({filter})"

        def build_empty_meta(count_filter: str):
            count_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE {count_filter};"
            geom_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL);"
            geom_valid_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND ({geom_col} IS NOT NULL AND ST_IsValid({geom_col}));"
            lonlat_sql = f"SELECT COUNT(*)::int AS cnt FROM {safe_table_name} AS t WHERE ({count_filter}) AND (\"Lon_4326\" IS NOT NULL AND \"Lat_4326\" IS NOT NULL);"
            count_res = pg_tool.call({'operation': 'query', 'sql': count_sql, 'params': []})
            geom_res = pg_tool.call({'operation': 'query', 'sql': geom_sql, 'params': []})
            geom_valid_res = pg_tool.call({'operation': 'query', 'sql': geom_valid_sql, 'params': []})
            lonlat_res = pg_tool.call({'operation': 'query', 'sql': lonlat_sql, 'params': []})
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
                                WHEN \"Lon_4326\" IS NOT NULL AND \"Lat_4326\" IS NOT NULL THEN
                                    ST_SetSRID(ST_MakePoint(\"Lon_4326\", \"Lat_4326\"), 4326)
                                ELSE NULL
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
                        WHEN "Lon_4326" IS NOT NULL AND "Lat_4326" IS NOT NULL THEN
                            ST_SetSRID(ST_MakePoint("Lon_4326", "Lat_4326"), 4326)
                        ELSE NULL
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

@app.post("/api/save-screenshot")
async def save_screenshot(payload: ScreenshotRequest):
    if not payload.image_data:
        raise HTTPException(status_code=400, detail="截图数据不能为空")
    match = re.match(r"^data:(image/\w+);base64,", payload.image_data)
    if not match:
        raise HTTPException(status_code=400, detail="无效的图片数据格式")
    mime_type = match.group(1)
    ext = mime_type.split("/")[-1]
    try:
        image_bytes = base64.b64decode(payload.image_data.split(",", 1)[1])
    except Exception:
        raise HTTPException(status_code=400, detail="图片解码失败")
    directory = "/home/server/python/map_assistant_v1/backend/static/screenshots"
    os.makedirs(directory, exist_ok=True)
    base_name = payload.file_name or f"map_screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", base_name)
    if not safe_name.lower().endswith(f".{ext.lower()}"):
        safe_name = f"{safe_name}.{ext}"
    file_path = os.path.join(directory, safe_name)
    with open(file_path, "wb") as f:
        f.write(image_bytes)
    url = f"/static/screenshots/{safe_name}"
    return {"url": url, "filename": safe_name, "file_path": file_path}


# ==============================
# 报告文件强制下载接口
# ==============================
@app.api_route("/api/download/report/{filename}", methods=["GET", "HEAD"])
async def download_report(filename: str):
    """强制触发浏览器下载报告文件（带 Content-Disposition: attachment）"""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    return FileResponse(
        path=file_path,
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )


# ==============================
# 报告下载接口（无 .docx 后缀，防止迅雷等下载管理器拦截）
# ==============================
@app.get("/api/download/report")
async def download_report_query(filename: str = Query(..., description="报告文件名")):
    """通过 query 参数传递文件名，URL 不含 .docx 后缀，避免迅雷下载管理器拦截"""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    return FileResponse(
        path=file_path,
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )


# ==============================
# 报告在线预览接口（docx 转 HTML）
# ==============================
@app.get("/api/preview/report", response_class=HTMLResponse)
async def preview_report(filename: str = Query(..., description="报告文件名")):
    """将 docx 报告转为 HTML 在线预览"""
    import mammoth
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    try:
        with open(file_path, "rb") as f:
            result = mammoth.convert_to_html(f)
        html_body = result.value
        warnings = result.messages
        if warnings:
            logger.warning(f"[Preview] mammoth warnings: {warnings[:3]}")
    except Exception as e:
        logger.error(f"[Preview] 转换失败: {e}")
        raise HTTPException(status_code=500, detail=f"报告转换失败: {str(e)}")

    html_page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>报告预览 - {safe_name}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 860px; margin: 30px auto; padding: 20px 40px; background: #fafafa; color: #333; line-height: 1.8; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; color: #1e3a5f; }}
  h2 {{ font-size: 18px; color: #2563eb; margin-top: 24px; }}
  h3 {{ font-size: 15px; color: #475569; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 8px 12px; text-align: left; font-size: 14px; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 8px 0; }}
  p {{ margin: 8px 0; }}
  .preview-toolbar {{ position: sticky; top: 0; background: #fff; border-bottom: 1px solid #e2e8f0; padding: 10px 0; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; z-index: 10; }}
  .preview-toolbar h1 {{ font-size: 16px; margin: 0; border: none; padding: 0; flex: 1; }}
  .btn-download {{ background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 7px 16px; font-size: 13px; cursor: pointer; text-decoration: none; }}
  .btn-download:hover {{ background: #1d4ed8; }}
  .btn-print {{ background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 16px; font-size: 13px; cursor: pointer; }}
  .btn-print:hover {{ background: #e2e8f0; }}
  @media print {{ .preview-toolbar {{ display: none; }} body {{ max-width: 100%; padding: 0; }} }}
</style>
</head>
<body>
<div class="preview-toolbar">
  <h1>📄 {safe_name}</h1>
  <button class="btn-print" onclick="window.print()">🖨️ 打印</button>
  <a class="btn-download" href="/api/download/report/{safe_name}" download="{safe_name}">⬇ 下载</a>
</div>
<div class="report-content">
{html_body}
</div>
</body>
</html>"""
    return HTMLResponse(content=html_page)


# ==============================
# 图片上传接口（用于聊天中的多模态图片上传）
# ==============================
UPLOAD_IMAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_IMAGES_DIR, exist_ok=True)

@app.post("/api/upload_image")
async def upload_image(file: UploadFile = File(...)):
    """上传图片或 Excel 文件，返回访问 URL"""
    import time
    # 校验文件类型（图片 + Excel）
    allowed_types = {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/vnd.ms-excel",  # .xls
        "application/octet-stream",  # 某些浏览器对 Excel 的兼容类型
    }
    # 也通过后缀名判断，兼容浏览器发送的各种 MIME
    ext = os.path.splitext(file.filename or "image.png")[1].lower()
    is_excel = ext in {".xlsx", ".xls"}
    is_image = ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if not is_image and not is_excel:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
    
    # 生成唯一文件名
    ext = os.path.splitext(file.filename or "image.png")[1] or ".png"
    safe_ext = ext.lower() if ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".xlsx", ".xls"} else ".png"
    unique_name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{safe_ext}"
    save_path = os.path.join(UPLOAD_IMAGES_DIR, unique_name)
    
    # 保存文件
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)
    
    # 返回可访问的 URL
    url = f"/static/uploads/{unique_name}"
    logger.info(f"Image uploaded: {url} ({len(content)} bytes)")
    return JSONResponse({"url": url, "filename": unique_name, "size": len(content)})


# ==============================
# GeoJSON 文件读取接口（供前端矢量图层加载使用）
# ==============================
GEOJSON_DIR = os.path.join(os.path.dirname(__file__), "static", "geojson")

@app.get("/api/geojson/{filename}")
async def get_geojson(filename: str):
    """读取 GeoJSON 文件，用于矢量图层加载到地图"""
    import re
    # 安全校验：仅允许 .geojson 后缀，防止路径穿越
    if not re.match(r'^[a-zA-Z0-9_\-\.]+\.geojson$', filename):
        raise HTTPException(status_code=400, detail="无效的文件名")
    file_path = os.path.join(GEOJSON_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="GeoJSON 文件不存在")
    return FileResponse(file_path, media_type="application/geo+json")


# ==============================
# 原有聊天接口（保持不变）
# ==============================
@app.post("/chat")
async def chat(request: ChatRequest, x_session_id: Optional[str] = Header(default=None, alias="X-Session-ID")):
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="消息不能为空")

        # 优先使用请求头 X-Session-ID，其次 body.session_id，最后回退 "default"
        thread_id = x_session_id or request.session_id or "default"
        
        valid_roles = {'user', 'assistant', 'system', 'function'}
        for i, msg in enumerate(request.messages):
            if msg.role not in valid_roles:
                raise HTTPException(status_code=400, detail=f"无效角色: {msg.role}")
        
        messages = [msg.model_dump() for msg in request.messages]
        # 记录每条消息的角色，用于调试 400 错误
        roles = [m.get('role') for m in messages]
        logger.info(f"Processing messages with roles: {roles}")
        
        # 修复：确保消息列表以 user 开头（跳过 system）
        # 如果第一条不是 system 且第一条不是 user，或者第一条是 system 且第二条不是 user
        if messages:
            first_non_system_idx = 0
            if messages[0].get('role') == 'system':
                first_non_system_idx = 1
            
            if len(messages) > first_non_system_idx:
                if messages[first_non_system_idx].get('role') != 'user':
                    logger.warning(f"First non-system message is {messages[first_non_system_idx].get('role')}, not 'user'. Attempting to fix...")
                    # 找到第一个 user 消息并删除它之前的所有非 system 消息
                    first_user_idx = -1
                    for i in range(first_non_system_idx, len(messages)):
                        if messages[i].get('role') == 'user':
                            first_user_idx = i
                            break
                    
                    if first_user_idx != -1:
                        messages = messages[:first_non_system_idx] + messages[first_user_idx:]
                        logger.info(f"Fixed message sequence. New roles: {[m.get('role') for m in messages]}")
                    else:
                        logger.error("No user message found in history!")

        # 强力指令：彻底纠正实测高程与超深逻辑，严禁误导
        if messages and messages[-1]['role'] == 'user':
            content = messages[-1]['content']
            if any(k in content for k in ["超深", "高程", "深度", "采深", "开采深度"]):
                messages[-1]['content'] += (
                    "\n\n【高程判定业务规则更新】\n"
                    '1. 术语规范：统一使用「实测高程」代替「实测深度」。\n'
                    "2. 业务定义：\n"
                    "   - Measured_Depth（实测高程）：实地测量的海拔高度。\n"
                    "   - Control_Elevation（控制高程）：红线标准（最低许可海拔）。\n"
                    "3. 判定原则：\n"
                    '   - 整个砂场（或区域）的「实测高程-控制高程」测深点的【平均差值】超过 2米，才定义为【超深度开采】。\n'
                    "   - 只有当 AVG(Control_Elevation - Measured_Depth) > 2 时，才判定整个区域违规。\n"
                    "   - 若平均差值 <= 2米，即使有个别点位超深，也判定为【未构成超深度开采】。"
                )
                logger.info(f"Injected elevation business rules.")
            # 地图数据加载意图守卫：根据 active_view 决定使用 2D 还是 3D 工具
            map_intent_keywords = ["加载", "上图", "矢量", "图层", "地图", "位置", "显示到地图", "加载到地图", "跳转", "定位", "标记", "标点", "经纬度", "切换", "卫星", "底图", "清除"]
            if any(k in content for k in map_intent_keywords):
                active_view = request.active_view
                if active_view == 'cesium':
                    # 3D 视图：使用 cesium_tool 进行飞行和标注
                    messages.insert(0, {
                        'role': 'system',
                        'content': (
                            "当前用户处于 Cesium 3D 视图。本轮任务为 3D 地图操作。"
                            "禁止调用 map_tool、knowledge_base_tool；"
                            "使用 cesium_tool 完成以下操作："
                            "当用户要求切换底图（如卫星图、街道图）时，使用 cesium_tool(action='setBasemap', basemap='satellite'/'osm'/'tianditu')。"
                            "当用户要求跳转或查找位置时，先调用 location_search 查找坐标，"
                            "再调用 cesium_tool(action='flyTo', lat=..., lng=..., height=100000) 执行飞行动画。"
                            "当用户要求添加标注时，使用 cesium_tool(action='addMarker')。"
                            "当用户要求展示测深风险、超深风险、风险柱或三维柱状图时，使用 cesium_tool(action='addDepthColumns', table_name='ceshen')。"
                            "当用户要求加载矢量图层时，先查询数据库再用 cesium_tool(action='addGeoJsonLayer')。"
                            "当用户要求清除所有实体时，使用 cesium_tool(action='clearAll')。"
                        )
                    })
                    logger.info("3D view detected: using cesium_tool for map operations.")
                else:
                    # 2D 视图：使用 map_tool
                    messages.insert(0, {
                        'role': 'system',
                        'content': (
                            "当前用户处于 2D Leaflet 地图视图。本轮任务为 2D 地图操作。"
                            "禁止调用 cesium_tool、knowledge_base_tool；"
                            "仅允许使用 map_tool, location_search, coordinate_marker 与 postgresql_tool。"
                            '当用户要求切换底图时，使用 map_tool(action=\'switch_layer\', layer=\'satellite\'/\'arcgis\'/\'osm\')，'
                            '其中 satellite=卫星影像，arcgis=高分影像，osm=街道地图。'
                            "当用户要求跳转或查找位置时，必须优先使用 location_search 查找坐标，"
                            '然后通过 map_tool(action=\'set_view\') 进行跳转。'
                            "请生成 map_tool(action='load_vector_layer', table_name='ceshen', filter=...) 的加载命令，"
                            "其中字段名必须加双引号。"
                            '若用户给出了完整可采区/砂场名称（包含「可采区」或「砂场」），filter 必须使用等値匹配：'
                            "\\\"Mineable_Area_Name\\\"='\u5b8c整名称'，禁止用 LIKE。"
                            '当用户要求清除标记时，使用 map_tool(action=\'clear_markers\')。'
                        )
                    })
                    logger.info("2D view detected: using map_tool for map operations.")

        use_intent_agent = os.environ.get("USE_INTENT_AGENT", "true").lower() == "true"

        if use_intent_agent and task_executor is not None:
            result = await task_executor.execute(
                user_message=messages[-1]['content'],
                chat_history=messages[:-1],
                thread_id=thread_id,
            )

            intent_info = None
            if result.get("intent_result"):
                ir = result["intent_result"]
                intent_info = {
                    "primary_intent": ir.primary_intent.value if hasattr(ir.primary_intent, 'value') else str(ir.primary_intent),
                    "confidence": ir.confidence,
                    "task_context": ir.task_context,
                    "entities": ir.entities,
                    "execution_plan": [
                        {
                            "step_id": s.step_id,
                            "action": s.action,
                            "tool": s.tool,
                            "reasoning": s.reasoning
                        }
                        for s in ir.execution_plan
                    ] if ir.execution_plan else []
                }

            # 持久化 intent agent 结果
            _persist_chat(thread_id, messages[-1]['content'], result.get("response", ""))

            optimized_map_commands = _optimize_map_commands(messages[-1]['content'], result.get("map_commands", []))
            optimized_charts = _optimize_charts(result.get("charts", []))

            return ChatResponse(
                response=result.get("response", "命令已执行。"),
                messages=result.get("messages", []),
                map_commands=optimized_map_commands,
                cesium_commands=result.get("cesium_commands", []),
                charts=optimized_charts,
                intent_info=intent_info
            )

        response_messages = []
        iteration = 0
        runner = bot
        if messages and messages[-1]['role'] == 'user':
            content = messages[-1]['content']
            weather_intent_keywords = ["天气", "气温", "几度", "空气质量", "AQI", "雾霾", "降雨", "下雨", "预报", "带伞", "风力", "风速", "湿度", "紫外线"]
            if any(k in content for k in weather_intent_keywords):
                runner = bot  # 主 bot 已有 weather_tool
                logger.info("Weather intent: using main bot.")
            else:
                map_intent_keywords = ["加载", "上图", "矢量", "图层", "地图", "位置", "显示到地图", "加载到地图", "跳转", "定位", "标记", "标点", "经纬度"]
                if any(k in content for k in map_intent_keywords):
                    # 始终使用主 bot（已注册所有工具），通过系统消息指导工具选择
                    # 不使用 build_assistant_with_tools 的受限 Assistant，避免工具注册问题
                    runner = bot
                    if request.active_view == 'cesium':
                        logger.info("3D view: using main bot with cesium_tool system hint.")
                    else:
                        logger.info("2D view: using main bot with map_tool system hint.")
        for response in runner.run(messages=messages):
            iteration += 1
            response_messages = response
            # 记录每一次迭代，看是否卡在某个工具调用上
            last_msg = response_messages[-1] if response_messages else {}
            role = last_msg.get('role')
            name = last_msg.get('name', 'N/A')
            content = last_msg.get('content', '')
            
            # 记录关键信息
            if role == 'assistant' and 'call' in str(last_msg):
                logger.info(f"Bot iteration {iteration}: Assistant is calling tool: {last_msg}")
            elif role == 'function':
                logger.info(f"Bot iteration {iteration}: Function {name} returned: {str(content)[:200]}...")
            else:
                logger.info(f"Bot iteration {iteration}: role={role}, content={str(content)[:100]}...")
        
        # 获取最后一条助手回复作为 response 字段（兼容旧前端逻辑）
        response_text = ""
        formatted_answer = ""
        for msg in reversed(response_messages):
            if msg.get('role') == 'assistant' and msg.get('content'):
                response_text = clean_response_content(msg['content'])
                break
        # 如果工具返回了标准化答案（如平均超深深度），优先使用
        if not response_text:
            for msg in reversed(response_messages):
                if msg.get('role') == 'function' and msg.get('name') == 'data_visualizer_tool':
                    try:
                        func_res = json.loads(msg.get('content', '{}'))
                        fa = func_res.get('formatted_answer')
                        if fa:
                            formatted_answer = fa
                            break
                    except:
                        pass
        if formatted_answer:
            response_text = formatted_answer
        
        if not response_text:
            response_text = "命令已执行。"

        # 提取地图命令
        map_commands = []
        cesium_commands = []
        charts = []
        for msg in response_messages:
            if msg.get('role') == 'function' and msg.get('name') in ['map_tool', 'location_search']:
                try:
                    func_res = json.loads(msg.get('content', '{}'))
                    if func_res.get('map_command'):
                        map_commands.append(func_res['map_command'])
                except:
                    pass
            # 提取 Cesium 3D 命令
            if msg.get('role') == 'function' and msg.get('name') == 'cesium_tool':
                try:
                    func_res = json.loads(msg.get('content', '{}'))
                    if func_res.get('cesium_command'):
                        cesium_commands.append(func_res['cesium_command'])
                except:
                    pass
            # 提取数据可视化图表
            if msg.get('role') == 'function' and msg.get('name') == 'data_visualizer_tool':
                try:
                    vis = json.loads(msg.get('content', '{}'))
                    if isinstance(vis, dict) and vis.get('success'):
                        charts.append({
                            'chart_type': vis.get('chart_type'),
                            'config': vis.get('config'),
                            'summary': vis.get('content')
                        })
                except Exception as e:
                    logger.warning(f"Failed to parse visualization content: {e}")
        
        map_commands = _optimize_map_commands(messages[-1]['content'], map_commands)
        charts = _optimize_charts(charts)

        logger.info(f"Chat response: {response_text[:100]}... Map commands: {len(map_commands)}, Cesium commands: {len(cesium_commands)}, Charts: {len(charts)}")
        if map_commands:
            logger.info(f"First map command: {map_commands[0]}")
        if cesium_commands:
            logger.info(f"First cesium command: {cesium_commands[0]}")

        # 持久化消息到 SQLite（仅当 thread_id 是有效 UUID 会话时）
        _persist_chat(thread_id, messages[-1]['content'], response_text)
        
        return ChatResponse(
            response=response_text,
            messages=response_messages, # 返回完整的对话历史，包括工具执行结果
            map_commands=map_commands,
            cesium_commands=cesium_commands,
            charts=charts
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="内部服务器错误")

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, x_session_id: Optional[str] = Header(default=None, alias="X-Session-ID")):
    def sse_payload(payload: Dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def event_generator():
        try:
            # 先发送 SSE 预热包，尽量让浏览器和代理尽快进入流式模式
            yield ": stream-open\n\n"
            yield f": {' ' * 2048}\n\n"
            yield "retry: 1500\n\n"

            if not request.messages:
                raise HTTPException(status_code=400, detail="消息不能为空")

            thread_id = x_session_id or request.session_id or "default"
            valid_roles = {'user', 'assistant', 'system', 'function'}
            for msg in request.messages:
                if msg.role not in valid_roles:
                    raise HTTPException(status_code=400, detail=f"无效角色: {msg.role}")

            messages = [msg.model_dump() for msg in request.messages]
            roles = [m.get('role') for m in messages]
            logger.info(f"[stream] Processing messages with roles: {roles}")

            # 图片 OCR 预处理：用 qwen-vl-ocr 提取文字后注入到用户消息
            # 主模型 qwen-flash 不支持图片，所以文字提取后再移除 images 字段
            messages = await _preprocess_message_images(messages)

            if messages:
                first_non_system_idx = 0
                if messages[0].get('role') == 'system':
                    first_non_system_idx = 1

                if len(messages) > first_non_system_idx and messages[first_non_system_idx].get('role') != 'user':
                    first_user_idx = -1
                    for i in range(first_non_system_idx, len(messages)):
                        if messages[i].get('role') == 'user':
                            first_user_idx = i
                            break
                    if first_user_idx != -1:
                        messages = messages[:first_non_system_idx] + messages[first_user_idx:]

            if messages and messages[-1]['role'] == 'user':
                content = _extract_text_content(messages[-1]['content'])
                if any(k in content for k in ["超深", "高程", "深度", "采深", "开采深度"]):
                    # 多模态消息需要特殊处理：将业务规则追加到文本部分
                    extra_rules = (
                        "\n\n【高程判定业务规则更新】\n"
                        '1. 术语规范：统一使用「实测高程」代替「实测深度」。\n'
                        "2. 业务定义：\n"
                        "   - Measured_Depth（实测高程）：实地测量的海拔高度。\n"
                        "   - Control_Elevation（控制高程）：红线标准（最低许可海拔）。\n"
                        "3. 判定原则：\n"
                        '   - 整个砂场（或区域）的「实测高程-控制高程」测深点的【平均差值】超过 2米，才定义为【超深度开采】。\n'
                        "   - 只有当 AVG(Control_Elevation - Measured_Depth) > 2 时，才判定整个区域违规。\n"
                        "   - 若平均差值 <= 2米，即使有个别点位超深，也判定为【未构成超深度开采】。"
                    )
                    if isinstance(messages[-1]['content'], list):
                        # 多模态格式：追加到第一个 text 部分
                        for part in messages[-1]['content']:
                            if isinstance(part, dict) and part.get("type") == "text":
                                part["text"] = part.get("text", "") + extra_rules
                                break
                    else:
                        messages[-1]['content'] += extra_rules
                map_intent_keywords = ["加载", "上图", "矢量", "图层", "地图", "位置", "显示到地图", "加载到地图", "跳转", "定位", "标记", "标点", "经纬度", "切换", "卫星", "底图", "清除"]
                if any(k in content for k in map_intent_keywords):
                    active_view = request.active_view
                    if active_view == 'cesium':
                        messages.insert(0, {
                            'role': 'system',
                            'content': (
                                "当前用户处于 Cesium 3D 视图。本轮任务为 3D 地图操作。"
                                "禁止调用 map_tool、knowledge_base_tool；"
                                "使用 cesium_tool 完成以下操作："
                                "当用户要求切换底图（如卫星图、街道图）时，使用 cesium_tool(action='setBasemap', basemap='satellite'/'osm'/'tianditu')。"
                                "当用户要求跳转或查找位置时，先调用 location_search 查找坐标，"
                                "再调用 cesium_tool(action='flyTo', lat=..., lng=..., height=100000) 执行飞行动画。"
                                "当用户要求添加标注时，使用 cesium_tool(action='addMarker')。"
                                "当用户要求展示测深风险、超深风险、风险柱或三维柱状图时，使用 cesium_tool(action='addDepthColumns', table_name='ceshen')。"
                                "当用户要求加载矢量图层时，先查询数据库再用 cesium_tool(action='addGeoJsonLayer')。"
                                "当用户要求清除所有实体时，使用 cesium_tool(action='clearAll')。"
                            )
                        })
                    else:
                        messages.insert(0, {
                            'role': 'system',
                            'content': (
                                "当前用户处于 2D Leaflet 地图视图。本轮任务为 2D 地图操作。"
                                "禁止调用 cesium_tool、knowledge_base_tool；"
                                "仅允许使用 map_tool, location_search, coordinate_marker, postgresql_tool 与 spatial_processing_tool。"
                                '当用户要求切换底图时，使用 map_tool(action=\'switch_layer\', layer=\'satellite\'/\'arcgis\'/\'osm\')，'
                                '其中 satellite=卫星影像，arcgis=高分影像，osm=街道地图。'
                                "当用户要求跳转或查找位置时，必须优先使用 location_search 查找坐标，"
                                '然后通过 map_tool(action=\'set_view\') 进行跳转。'
                                "请生成 map_tool(action='load_vector_layer', table_name='ceshen', filter=...) 的加载命令，"
                                "其中字段名必须加双引号。"
                                '若用户给出了完整可采区/砂场名称（包含「可采区」或「砂场」），filter 必须使用等値匹配：'
                                "\\\"Mineable_Area_Name\\\"='\u5b8c整名称'，禁止用 LIKE。"
                                '当用户要求清除标记时，使用 map_tool(action=\'clear_markers\')。'
                                "当用户上传图片/Excel 或输入坐标，要求生成矢量范围、转换坐标、投影变换时，使用 spatial_processing_tool。"
                                "CGCS2000 Zone 38N (3度带, 带号38) 的推荐 EPSG 代码是 4526（东偏移已内置带号），也可使用 4547（需手动去带号前缀）。适用于信阳地区（约 114°E）。\n"
                                "⚠️ 重要：EPSG:4497 是 6 度带第 19 带（CM 111°E），**不是** 3 度带第 38 带，切勿混淆！\n"
                                "⚠️ 坐标顺序：coordinates 参数应为 [[easting, northing], ...] 格式，即 easting 在第一维（约 38M），northing 在第二维（约 3.5M）。工具会自动检测测绘惯例顺序并纠正。\n"
                                "若用户说 XY 坐标相反，调用 spatial_processing_tool 时设置 swap_xy=true。\n"
                                "如果 spatial_processing_tool 返回错误（如坐标转换失败），直接告知用户错误内容，**不要尝试用 cesium_tool 或手动构造 GeoJSON 作为替代方案**。",
                            )
                        })

            yield sse_payload({
                "type": "status",
                "stage": "queued",
                "message": "请求已提交，后端开始处理。",
            })

            use_intent_agent = os.environ.get("USE_INTENT_AGENT", "true").lower() == "true"

            if use_intent_agent and task_executor is not None:
                # 提取纯文本用于 task_executor（它目前只支持字符串 user_message）
                user_text = _extract_text_content(messages[-1].get('content', ''))
                async for event in task_executor.execute_stream(
                    user_message=user_text,
                    chat_history=messages[:-1],
                    thread_id=thread_id,
                ):
                    if event.get("type") == "final":
                        result = event.get("result", {})
                        result["map_commands"] = _optimize_map_commands(user_text, result.get("map_commands", []))
                        result["charts"] = _optimize_charts(result.get("charts", []))
                        _persist_chat(thread_id, user_text, result.get("response", ""))
                        yield sse_payload({"type": "final", "result": result})
                    else:
                        yield sse_payload(event)

                yield "data: [DONE]\n\n"
                return

            response_messages = []
            iteration = 0
            runner = bot
            if messages and messages[-1]['role'] == 'user':
                content = messages[-1]['content']
                weather_intent_keywords = ["天气", "气温", "几度", "空气质量", "AQI", "雾霾", "降雨", "下雨", "预报", "带伞", "风力", "风速", "湿度", "紫外线"]
                if any(k in content for k in weather_intent_keywords):
                    runner = bot
                else:
                    map_intent_keywords = ["加载", "上图", "矢量", "图层", "地图", "位置", "显示到地图", "加载到地图", "跳转", "定位", "标记", "标点", "经纬度"]
                    if any(k in content for k in map_intent_keywords):
                        runner = bot

            yield sse_payload({
                "type": "status",
                "stage": "model",
                "message": "模型已开始执行，正在逐步调用工具。",
            })

            for response in runner.run(messages=messages):
                iteration += 1
                response_messages = response
                last_msg = response_messages[-1] if response_messages else {}
                role = last_msg.get('role')
                name = last_msg.get('name', 'N/A')
                content = last_msg.get('content', '')

                if role == 'assistant' and 'call' in str(last_msg):
                    yield sse_payload({
                        "type": "tool_start",
                        "stage": "tool_start",
                        "tool_name": name,
                        "message": f"正在调用工具处理请求（第 {iteration} 轮）。",
                    })
                elif role == 'function':
                    try:
                        parsed = json.loads(content) if isinstance(content, str) else content
                    except Exception:
                        parsed = {"content": str(content)}
                    summary = parsed.get('content') or parsed.get('message') or parsed.get('error') or f"{name} 已返回结果"
                    yield sse_payload({
                        "type": "tool_result",
                        "stage": "tool_result",
                        "tool_name": name,
                        "message": f"{name}：{str(summary)[:100]}",
                    })
                elif role == 'assistant' and content:
                    preview = clean_response_content(str(content))[:80]
                    if preview:
                        yield sse_payload({
                            "type": "status",
                            "stage": "reasoning",
                            "message": f"正在整理回复：{preview}",
                        })

            response_text = ""
            formatted_answer = ""
            for msg in reversed(response_messages):
                if msg.get('role') == 'assistant' and msg.get('content'):
                    response_text = clean_response_content(msg['content'])
                    break
            if not response_text:
                for msg in reversed(response_messages):
                    if msg.get('role') == 'function' and msg.get('name') == 'data_visualizer_tool':
                        try:
                            func_res = json.loads(msg.get('content', '{}'))
                            fa = func_res.get('formatted_answer')
                            if fa:
                                formatted_answer = fa
                                break
                        except Exception:
                            pass
            if formatted_answer:
                response_text = formatted_answer
            if not response_text:
                response_text = "命令已执行。"

            map_commands = []
            cesium_commands = []
            charts = []
            for msg in response_messages:
                if msg.get('role') == 'function' and msg.get('name') in ['map_tool', 'location_search']:
                    try:
                        func_res = json.loads(msg.get('content', '{}'))
                        if func_res.get('map_command'):
                            map_commands.append(func_res['map_command'])
                    except Exception:
                        pass
                if msg.get('role') == 'function' and msg.get('name') == 'cesium_tool':
                    try:
                        func_res = json.loads(msg.get('content', '{}'))
                        if func_res.get('cesium_command'):
                            cesium_commands.append(func_res['cesium_command'])
                    except Exception:
                        pass
                if msg.get('role') == 'function' and msg.get('name') == 'data_visualizer_tool':
                    try:
                        vis = json.loads(msg.get('content', '{}'))
                        if isinstance(vis, dict) and vis.get('success'):
                            charts.append({
                                'chart_type': vis.get('chart_type'),
                                'config': vis.get('config'),
                                'summary': vis.get('content')
                            })
                    except Exception as e:
                        logger.warning(f"[stream] Failed to parse visualization content: {e}")

            map_commands = _optimize_map_commands(messages[-1]['content'], map_commands)
            charts = _optimize_charts(charts)
            _persist_chat(thread_id, messages[-1]['content'], response_text)

            yield sse_payload({
                "type": "final",
                "result": {
                    "response": response_text,
                    "messages": response_messages,
                    "map_commands": map_commands,
                    "cesium_commands": cesium_commands,
                    "charts": charts,
                    "intent_info": None,
                }
            })
            yield "data: [DONE]\n\n"
        except HTTPException as e:
            message = e.detail if isinstance(e.detail, str) else "请求处理失败"
            yield sse_payload({"type": "error", "stage": "error", "message": message})
            yield sse_payload({
                "type": "final",
                "result": {
                    "response": message,
                    "messages": [],
                    "map_commands": [],
                    "cesium_commands": [],
                    "charts": [],
                    "intent_info": None,
                }
            })
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Chat stream error: {e}\n{traceback.format_exc()}")
            message = f"内部服务器错误：{str(e)}"
            yield sse_payload({"type": "error", "stage": "error", "message": message})
            yield sse_payload({
                "type": "final",
                "result": {
                    "response": message,
                    "messages": [],
                    "map_commands": [],
                    "cesium_commands": [],
                    "charts": [],
                    "intent_info": None,
                }
            })
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream; charset=utf-8",
            "Content-Encoding": "identity",
        },
    )

from tools.overlay_tile_service import get_tile_png as _overlay_get_tile_png, list_layers as _overlay_list_layers, query_feature as _overlay_query_feature, register_layer as _overlay_register_layer, unregister_layer as _overlay_unregister_layer
from tools import geoserver_client as _gs_client
from tools.geoserver_client import GeoServerUnavailable as _GeoServerUnavailable

@app.get("/api/overlay_tile/{layer}/{z}/{x}/{y}.png")
async def overlay_tile(layer: str, z: int, x: int, y: int):
    """矢量图层的栅格瓦片接口（hx / caiqu 等）。"""
    if layer not in _overlay_list_layers():
        raise HTTPException(status_code=404, detail=f"unknown overlay layer: {layer}")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    try:
        png_bytes = _overlay_get_tile_png(layer, z, x, y)
    except Exception as e:
        logger.exception("overlay tile render failed: %s/%s/%s/%s", layer, z, x, y)
        raise HTTPException(status_code=500, detail=str(e))
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


_VT_DIR = os.path.join(os.path.dirname(__file__), "vector_tiles")

@app.get("/api/vector_tile/{layer}/{z}/{x}/{y}.pbf")
async def vector_tile(layer: str, z: int, x: int, y: int):
    """预生成的矢量切片接口（tippecanoe .pbf），供 Leaflet 2D 地图使用。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=404, detail=f"unknown vector tile layer: {layer}")
    pbf_path = os.path.join(_VT_DIR, layer, str(z), str(x), f"{y}.pbf")
    if not os.path.abspath(pbf_path).startswith(os.path.abspath(_VT_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="invalid tile path")
    if not os.path.isfile(pbf_path):
        return Response(status_code=204)
    return FileResponse(
        pbf_path,
        media_type="application/x-protobuf",
        headers={
            "Cache-Control": "public, max-age=604800",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/overlay_feature/{layer}")
async def overlay_feature(layer: str, lng: float, lat: float, tolerance_m: float = 120.0):
    if layer not in _overlay_list_layers():
        raise HTTPException(status_code=404, detail=f"unknown overlay layer: {layer}")
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="invalid coordinate")
    try:
        feature = _overlay_query_feature(layer, lng, lat, tolerance_m)
    except Exception as e:
        logger.exception("overlay feature query failed: %s/%s/%s", layer, lng, lat)
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(
        content={"found": feature is not None, "feature": feature},
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


class TileRegenerateRequest(BaseModel):
    layer: str


class DroneImageryRegisterRequest(BaseModel):
    layer_key: str
    name: str = ""
    path: str
    area_key: str = ""
    year: Optional[int] = None
    min_zoom: int = 0
    max_zoom: int = 22
    max_native_zoom: Optional[int] = None
    bounds: Optional[List[float]] = None
    opacity: float = 0.9
    scheme: str = "tms"


class DroneImageryBuildRequest(BaseModel):
    source_path: str
    layer_key: str = ""
    name: str = ""
    area_key: str = ""
    year: Optional[int] = None
    min_zoom: int = 0
    max_zoom: int = 22
    opacity: float = 0.9
    tile_format: str = "PNG"
    quality: int = 85
    overwrite: bool = True


# ---------- 3D Tiles 管理 ----------
class ThreeDTilesRegisterRequest(BaseModel):
    directory: str
    key: str = ""
    name: str = ""
    label: str = ""
    alt_offset: float = 0.0
    auto_ground_clamp: bool = True
    description: str = ""


class ThreeDTilesDeleteRequest(BaseModel):
    key: str
    delete_files: bool = False


_3DTILES_DATA_DIR = os.path.join(os.path.dirname(__file__), "3dtiles_data")
_3DTILES_REGISTRY_PATH = os.path.join(_3DTILES_DATA_DIR, "registry.json")


def _load_3dtiles_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_3DTILES_REGISTRY_PATH):
        return {}
    try:
        with open(_3DTILES_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load 3dtiles registry failed: %s", e)
        return {}


def _save_3dtiles_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_3DTILES_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_3DTILES_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _3DTILES_REGISTRY_PATH)


def _count_3dtiles(directory: str) -> Dict[str, int]:
    """统计 3D Tiles 目录下的 b3dm/json 文件数量和总大小。"""
    tile_count = 0
    total_size = 0
    if not os.path.isdir(directory):
        return {"tile_count": 0, "size_bytes": 0}
    for root, _, files in os.walk(directory):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in (".b3dm", ".json", ".i3dm", ".pnts", ".cmpt"):
                tile_count += 1
                try:
                    total_size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return {"tile_count": tile_count, "size_bytes": total_size}


def _read_3dtiles_meta(directory: str) -> Dict[str, Any]:
    """读取 3D Tiles 目录下的 meta.json。"""
    meta_path = os.path.join(directory, "meta.json")
    if not os.path.isfile(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_3dtiles_meta(directory: str, meta: Dict[str, Any]) -> None:
    """写入 3D Tiles meta.json。"""
    meta_path = os.path.join(directory, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _3dtiles_layer_to_row(key: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    directory = meta.get("directory") or ""
    stats = _count_3dtiles(directory)
    tileset_url = f"/api/3dtiles/{key}/tileset.json"
    has_tileset = os.path.isfile(os.path.join(directory, "tileset.json"))
    meta_info = meta.get("meta") or _read_3dtiles_meta(directory)
    center = meta.get("center") or meta_info.get("center")
    return {
        "key": key,
        "label": meta.get("label") or meta_info.get("label") or meta.get("name") or key,
        "type": "3dtiles",
        "status": "ready" if has_tileset else "missing",
        "color": "#722ed1",
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "min_zoom": 0,
        "max_zoom": 22,
        "api_url": tileset_url,
        "directory": directory,
        "source_path": directory,
        "source_name": os.path.basename(directory),
        "custom": True,
        "alt_offset": meta.get("alt_offset", 0.0),
        "auto_ground_clamp": meta.get("auto_ground_clamp", True),
        "center": center,
        "description": meta.get("description") or meta_info.get("description") or "",
        "tileset_url": tileset_url,
    }


def _auto_register_existing_3dtiles() -> None:
    """启动时自动扫描 3dtiles_data 并注册已有的数据集。"""
    registry = _load_3dtiles_registry()
    if not os.path.isdir(_3DTILES_DATA_DIR):
        return
    updated = False
    for entry in sorted(os.listdir(_3DTILES_DATA_DIR)):
        full_dir = os.path.join(_3DTILES_DATA_DIR, entry)
        if not os.path.isdir(full_dir):
            continue
        tileset_path = os.path.join(full_dir, "tileset.json")
        if not os.path.isfile(tileset_path):
            continue
        if entry in registry:
            continue
        meta_info = _read_3dtiles_meta(full_dir)
        stats = _count_3dtiles(full_dir)
        registry[entry] = {
            "directory": full_dir,
            "label": meta_info.get("label") or meta_info.get("name") or entry,
            "name": meta_info.get("name") or entry,
            "tile_count": stats["tile_count"],
            "size_bytes": stats["size_bytes"],
            "alt_offset": meta_info.get("altOffset", 0.0),
            "auto_ground_clamp": meta_info.get("autoGroundClamp", True),
            "center": meta_info.get("center"),
            "description": meta_info.get("description") or "",
            "meta": meta_info,
            "created_at": dt.now().isoformat(timespec="seconds"),
        }
        logger.info("auto-registered 3dtiles: %s", entry)
        updated = True
    if updated:
        _save_3dtiles_registry(registry)


# 在模块加载时自动注册已有数据集
_auto_register_existing_3dtiles()


_OVERLAY_DATA_DIR = os.environ.get(
    "MAP_OVERLAY_DATA_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "public", "data")),
)

_TILE_LAYER_META = {
    "hx": {
        "label": "河道红线",
        "color": "#ef4444",
        "source": os.path.join(_OVERLAY_DATA_DIR, "hx.geojson"),
    },
    "caiqu": {
        "label": "采区边界",
        "color": "#facc15",
        "source": os.path.join(_OVERLAY_DATA_DIR, "caiqu.geojson"),
    },
}

_CUSTOM_TILE_DATA_DIR = os.path.join(os.path.dirname(__file__), "uploaded_tile_data")
_TILE_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "tile_layers.json")
_DRONE_IMAGERY_DIR = os.path.join(os.path.dirname(__file__), "drone_imagery")
_DRONE_REGISTRY_PATH = os.path.join(_DRONE_IMAGERY_DIR, "registry.json")
_DRONE_MBTILES_DIR = os.path.join(_DRONE_IMAGERY_DIR, "mbtiles")
_DRONE_WORK_DIR = os.path.join(_DRONE_IMAGERY_DIR, "work")
_DRONE_BUILD_JOBS = {}


def _sanitize_layer_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    if not key:
        key = f"layer_{uuid.uuid4().hex[:8]}"
    return key[:64]


def _parse_style(stroke: str, fill: str, fill_alpha: float, stroke_width: float, point_size: float = 8) -> Dict[str, Any]:
    hex_pattern = r"^#[0-9a-fA-F]{6}$"
    stroke = stroke if re.match(hex_pattern, stroke or "") else "#2773d7"
    fill = fill if re.match(hex_pattern, fill or "") else stroke
    return {
        "stroke": stroke,
        "fill": fill,
        "fillAlpha": max(0.0, min(1.0, float(fill_alpha))),
        "strokeWidth": max(1.0, min(20.0, float(stroke_width))),
        "pointSize": max(2.0, min(64.0, float(point_size))),
    }


def _load_tile_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_TILE_REGISTRY_PATH):
        return {}
    try:
        with open(_TILE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load tile registry failed: %s", e)
        return {}


def _save_tile_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_TILE_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_TILE_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _TILE_REGISTRY_PATH)


def _load_drone_registry() -> Dict[str, Dict[str, Any]]:
    if not os.path.isfile(_DRONE_REGISTRY_PATH):
        return {}
    try:
        with open(_DRONE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("load drone imagery registry failed: %s", e)
        return {}


def _save_drone_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(_DRONE_REGISTRY_PATH), exist_ok=True)
    tmp_path = f"{_DRONE_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _DRONE_REGISTRY_PATH)


def _mbtiles_metadata(path: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not os.path.isfile(path):
        return meta
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT name, value FROM metadata").fetchall()
            meta = {str(k): v for k, v in rows}
            zoom_row = conn.execute("SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles").fetchone()
            if zoom_row:
                meta["minzoom_actual"] = zoom_row[0]
                meta["maxzoom_actual"] = zoom_row[1]
                meta["tile_count"] = zoom_row[2]
    except Exception as e:
        logger.warning("read mbtiles metadata failed %s: %s", path, e)
    return meta


def _parse_bounds(value: Any) -> Optional[List[float]]:
    if isinstance(value, list) and len(value) == 4:
        try:
            return [float(v) for v in value]
        except Exception:
            return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 4:
            try:
                return [float(p) for p in parts]
            except Exception:
                return None
    return None


def _drone_layer_to_row(key: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    path = meta.get("path") or ""
    mb_meta = _mbtiles_metadata(path)
    tile_count = int(mb_meta.get("tile_count") or 0)
    min_zoom = int(meta.get("min_zoom", mb_meta.get("minzoom_actual") or mb_meta.get("minzoom") or 0))
    max_zoom = int(meta.get("max_zoom", mb_meta.get("maxzoom_actual") or mb_meta.get("maxzoom") or 22))
    fmt = str(mb_meta.get("format") or meta.get("tile_format") or "png").lower()
    return {
        "key": key,
        "label": meta.get("name") or meta.get("label") or key,
        "type": "drone",
        "status": "ready" if os.path.isfile(path) and tile_count > 0 else "missing",
        "color": "#722ed1",
        "tile_count": tile_count,
        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "max_native_zoom": int(meta.get("max_native_zoom") or max_zoom),
        "api_url": f"/api/drone_imagery/tile/{key}/{{z}}/{{x}}/{{y}}.png",
        "directory": path,
        "source_path": meta.get("source_path") or path,
        "source_name": os.path.basename(meta.get("source_path") or path),
        "style": {"opacity": float(meta.get("opacity", 0.9))},
        "custom": True,
        "area_key": meta.get("area_key") or "",
        "year": meta.get("year"),
        "bounds": meta.get("bounds") or _parse_bounds(mb_meta.get("bounds")),
        "tile_format": fmt,
        "storage": "mbtiles",
    }


def _media_type_for_tile_format(fmt: str) -> str:
    fmt = (fmt or "").lower()
    if fmt in ("jpg", "jpeg"):
        return "image/jpeg"
    if fmt == "webp":
        return "image/webp"
    return "image/png"


def _register_drone_imagery(req: DroneImageryRegisterRequest) -> Dict[str, Any]:
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(req.path))[0])
    path = os.path.abspath(req.path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"MBTiles 文件不存在: {path}")
    mb_meta = _mbtiles_metadata(path)
    if not mb_meta.get("tile_count"):
        raise HTTPException(status_code=400, detail="MBTiles 中未找到 tiles 数据")
    min_zoom = max(0, min(22, int(req.min_zoom if req.min_zoom is not None else mb_meta.get("minzoom_actual") or 0)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom if req.max_zoom is not None else mb_meta.get("maxzoom_actual") or 22)))
    meta = {
        "name": req.name.strip() or mb_meta.get("name") or key,
        "area_key": req.area_key.strip(),
        "year": req.year,
        "path": path,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "max_native_zoom": int(req.max_native_zoom or max_zoom),
        "bounds": req.bounds or _parse_bounds(mb_meta.get("bounds")),
        "opacity": max(0.0, min(1.0, float(req.opacity))),
        "scheme": req.scheme if req.scheme in ("tms", "xyz") else "tms",
        "tile_format": str(mb_meta.get("format") or "png").lower(),
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    registry = _load_drone_registry()
    registry[key] = meta
    _save_drone_registry(registry)
    return _drone_layer_to_row(key, meta)


def _run_drone_mbtiles_build(req: DroneImageryBuildRequest) -> Dict[str, Any]:
    source_path = os.path.abspath(req.source_path)
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"GeoTIFF 文件不存在: {source_path}")
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(source_path))[0])
    min_zoom = max(0, min(22, int(req.min_zoom)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom)))
    tile_format = req.tile_format.upper()
    if tile_format not in ("PNG", "PNG8", "JPEG"):
        tile_format = "PNG"
    quality = max(1, min(100, int(req.quality)))
    os.makedirs(_DRONE_MBTILES_DIR, exist_ok=True)
    os.makedirs(_DRONE_WORK_DIR, exist_ok=True)
    warped_path = os.path.join(_DRONE_WORK_DIR, f"{key}_3857.tif")
    mbtiles_path = os.path.join(_DRONE_MBTILES_DIR, f"{key}.mbtiles")
    if os.path.exists(mbtiles_path) and not req.overwrite:
        raise HTTPException(status_code=409, detail=f"MBTiles 已存在: {mbtiles_path}")
    for path in (warped_path, mbtiles_path):
        if os.path.exists(path) and req.overwrite:
            os.remove(path)
    warp_cmd = [
        "gdalwarp",
        "-t_srs", "EPSG:3857",
        "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-r", "bilinear",
        "-of", "GTiff",
        "-co", "TILED=YES",
        "-co", "COMPRESS=DEFLATE",
        "-co", "BIGTIFF=YES",
        source_path,
        warped_path,
    ]
    translate_cmd = [
        "gdal_translate",
        "-of", "MBTILES",
        "-co", f"TILE_FORMAT={tile_format}",
        "-co", f"QUALITY={quality}",
        "-co", "RESAMPLING=BILINEAR",
        "-co", "WRITE_BOUNDS=YES",
        "-co", "WRITE_MINMAXZOOM=YES",
        warped_path,
        mbtiles_path,
    ]
    overview_count = max(0, max_zoom - min_zoom)
    overview_factors = [str(2 ** i) for i in range(1, overview_count + 1)]
    try:
        for cmd in (warp_cmd, translate_cmd):
            result = subprocess.run(
                cmd,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
            if result.returncode != 0:
                logger.error("drone imagery build failed: %s", result.stderr[-2000:])
                raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "GDAL 构建失败")
        if overview_factors:
            addo_cmd = ["gdaladdo", "-r", "average", mbtiles_path, *overview_factors]
            result = subprocess.run(
                addo_cmd,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
            if result.returncode != 0:
                logger.error("drone imagery overview build failed: %s", result.stderr[-2000:])
                raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "MBTiles 金字塔构建失败")
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"GDAL 工具未安装或不在 PATH 中: {e.filename}")
    register_req = DroneImageryRegisterRequest(
        layer_key=key,
        name=req.name or key,
        path=mbtiles_path,
        area_key=req.area_key,
        year=req.year,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        max_native_zoom=max_zoom,
        opacity=req.opacity,
        scheme="tms",
    )
    row = _register_drone_imagery(register_req)
    registry = _load_drone_registry()
    if key in registry:
        registry[key]["source_path"] = source_path
        registry[key]["build_type"] = "geotiff_to_mbtiles"
        registry[key]["updated_at"] = dt.now().isoformat(timespec="seconds")
        _save_drone_registry(registry)
        row = _drone_layer_to_row(key, registry[key])
    with contextlib.suppress(Exception):
        os.remove(warped_path)
    return row


def _merged_tile_meta() -> Dict[str, Dict[str, Any]]:
    merged = dict(_TILE_LAYER_META)
    for key, meta in _load_tile_registry().items():
        if isinstance(meta, dict):
            merged[key] = meta
    return merged


def _register_custom_raster_layers() -> None:
    for key, meta in _load_tile_registry().items():
        if meta.get("build_type", "both") not in ("raster", "both"):
            continue
        source = meta.get("source")
        style = meta.get("style") or {}
        if source and os.path.isfile(source):
            try:
                _overlay_register_layer(key, source, style)
            except Exception as e:
                logger.warning("register custom raster layer failed %s: %s", key, e)


def _run_tippecanoe(key: str, source_path: str, min_zoom: int, max_zoom: int) -> Dict[str, int]:
    target_dir = os.path.join(_VT_DIR, key)
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    cmd = [
        "tippecanoe",
        "-e", target_dir,
        f"-z{max_zoom}",
        f"-Z{min_zoom}",
        "--no-tile-compression",
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "-l", key,
        "--force",
        source_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="tippecanoe 未安装或不在 PATH 中")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="矢量切片生成超时")
    if result.returncode != 0:
        logger.error("tippecanoe failed: %s", result.stderr[-2000:])
        raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "tippecanoe failed")
    return _dir_stats(target_dir)


_register_custom_raster_layers()


def _dir_stats(path: str) -> Dict[str, int]:
    total_size = 0
    tile_count = 0
    if not os.path.isdir(path):
        return {"size_bytes": 0, "tile_count": 0}
    for root, _, files in os.walk(path):
        for name in files:
            if not name.endswith(".pbf"):
                continue
            tile_count += 1
            try:
                total_size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return {"size_bytes": total_size, "tile_count": tile_count}


@app.get("/api/tile_manager/layers")
async def tile_manager_layers():
    layers = []
    for key, meta in _merged_tile_meta().items():
        source_path = meta["source"]
        style = meta.get("style") or {}
        min_zoom = int(meta.get("min_zoom", 0))
        max_zoom = int(meta.get("max_zoom", 18))
        build_type = meta.get("build_type", "both")
        vector_dir = os.path.join(_VT_DIR, key)
        stats = _dir_stats(vector_dir)
        vector_ready = build_type in ("vector", "both") and stats["tile_count"] > 0
        raster_ready = build_type in ("raster", "both") and os.path.isfile(source_path)
        color = style.get("stroke") or meta.get("color") or "#2773d7"
        layers.append({
            "key": key,
            "label": meta["label"],
            "type": "vector",
            "status": "ready" if vector_ready else "missing",
            "color": color,
            "tile_count": stats["tile_count"],
            "size_bytes": stats["size_bytes"],
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "api_url": f"/api/vector_tile/{key}/{{z}}/{{x}}/{{y}}.pbf",
            "directory": vector_dir,
            "source_path": source_path,
            "source_name": os.path.basename(source_path),
            "style": style,
            "custom": key not in _TILE_LAYER_META,
        })
        layers.append({
            "key": key,
            "label": meta["label"],
            "type": "raster",
            "status": "ready" if raster_ready else "missing",
            "color": color,
            "tile_count": None,
            "size_bytes": os.path.getsize(source_path) if os.path.isfile(source_path) else 0,
            "min_zoom": 0,
            "max_zoom": 22,
            "api_url": f"/api/overlay_tile/{key}/{{z}}/{{x}}/{{y}}.png",
            "directory": "后端实时渲染 + LRU 缓存",
            "source_path": source_path,
            "source_name": os.path.basename(source_path),
            "style": style,
            "custom": key not in _TILE_LAYER_META,
        })
    for key, meta in _load_drone_registry().items():
        if isinstance(meta, dict):
            layers.append(_drone_layer_to_row(key, meta))
    for key, meta in _load_3dtiles_registry().items():
        if isinstance(meta, dict):
            layers.append(_3dtiles_layer_to_row(key, meta))
    return JSONResponse(content={"success": True, "layers": layers})


@app.post("/api/tile_manager/regenerate")
async def tile_manager_regenerate(req: TileRegenerateRequest):
    key = req.layer
    meta_map = _merged_tile_meta()
    if key not in meta_map:
        raise HTTPException(status_code=404, detail=f"unknown tile layer: {key}")
    source_path = meta_map[key]["source"]
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"source geojson not found: {source_path}")
    stats = _run_tippecanoe(
        key,
        source_path,
        int(meta_map[key].get("min_zoom", 0)),
        int(meta_map[key].get("max_zoom", 18)),
    )
    return JSONResponse(content={"success": True, "layer": key, **stats})


@app.delete("/api/tile_manager/{layer_key}")
async def tile_manager_delete(layer_key: str, delete_files: bool = False):
    """删除自定义矢量/栅格图层（内置图层 hx/caiqu 不允许删除）。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer_key):
        raise HTTPException(status_code=400, detail=f"非法的图层 key: {layer_key}")
    if layer_key in _TILE_LAYER_META:
        raise HTTPException(status_code=403, detail=f"内置图层不允许删除: {layer_key}")

    registry = _load_tile_registry()
    if layer_key not in registry:
        # 检查是否是无人机图层
        drone_registry = _load_drone_registry()
        if layer_key in drone_registry:
            meta = drone_registry.pop(layer_key)
            _save_drone_registry(drone_registry)
            return JSONResponse(content={"success": True, "layer": layer_key, "type": "drone", "deleted_files": []})

        # 图层不在任何注册表中 —— 尝试从 GeoServer 取消发布 + 可选删除 PostGIS 表
        gs_removed = False
        try:
            gs_result = _gs_client.unpublish_layer(layer_key, recurse=True)
            gs_removed = gs_result.get("removed", False)
        except _GeoServerUnavailable:
            pass
        except Exception as e:
            logger.warning("GeoServer unpublish failed for virtual layer %s: %s", layer_key, e)

        pg_dropped = False
        if delete_files:
            # 尝试删除 PostGIS 表
            try:
                conn = psycopg2.connect(host='172.136.16.52', port=5432, dbname='postgres', user='postgres')
                try:
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute(f'DROP TABLE IF EXISTS public."{layer_key}" CASCADE')
                        pg_dropped = True
                    logger.info("PostGIS table dropped: %s", layer_key)
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("Drop PostGIS table failed for %s: %s", layer_key, e)

        # 即使 GeoServer 和 PostGIS 都没操作，也返回成功（前端会从列表中移除）
        logger.info("虚拟图层已删除: key=%s, gs_removed=%s, pg_dropped=%s", layer_key, gs_removed, pg_dropped)
        return JSONResponse(content={
            "success": True,
            "layer": layer_key,
            "type": "virtual",
            "gs_removed": gs_removed,
            "pg_dropped": pg_dropped,
            "deleted_files": [],
        })

    meta = registry.pop(layer_key)
    _save_tile_registry(registry)

    # 从内存栅格服务中注销
    _overlay_unregister_layer(layer_key)

    deleted_files: list = []
    if delete_files:
        # 删除矢量切片目录
        vt_dir = os.path.join(_VT_DIR, layer_key)
        if os.path.isdir(vt_dir):
            import shutil
            try:
                shutil.rmtree(vt_dir)
                deleted_files.append(vt_dir)
            except OSError as e:
                logger.warning("删除矢量切片目录失败: %s %s", vt_dir, e)
        # 删除源 GeoJSON（仅自定义图层）
        source_path = meta.get("source") or ""
        if source_path and os.path.isfile(source_path) and not source_path.startswith(_OVERLAY_DATA_DIR):
            try:
                os.remove(source_path)
                deleted_files.append(source_path)
            except OSError as e:
                logger.warning("删除源文件失败: %s %s", source_path, e)

    logger.info("图层已删除: key=%s, delete_files=%s, removed=%s", layer_key, delete_files, deleted_files)
    return JSONResponse(content={
        "success": True,
        "layer": layer_key,
        "deleted_files": deleted_files,
    })


@app.post("/api/tile_manager/build")
async def tile_manager_build(
    file: UploadFile = File(...),
    layer_key: str = Form(""),
    label: str = Form(""),
    build_type: str = Form("both"),
    stroke: str = Form("#2773d7"),
    fill: str = Form("#2773d7"),
    fill_alpha: float = Form(0.18),
    stroke_width: float = Form(2),
    point_size: float = Form(8),
    min_zoom: int = Form(0),
    max_zoom: int = Form(18),
    auto_publish: bool = Form(False),
):
    safe_key = _sanitize_layer_key(layer_key or os.path.splitext(file.filename or "")[0])
    if safe_key in _TILE_LAYER_META:
        raise HTTPException(status_code=400, detail="内置图层 key 不能覆盖")
    build_type = build_type if build_type in ("vector", "raster", "both") else "both"
    min_zoom = max(0, min(22, int(min_zoom)))
    max_zoom = max(min_zoom, min(22, int(max_zoom)))
    style = _parse_style(stroke, fill, fill_alpha, stroke_width, point_size)
    os.makedirs(_CUSTOM_TILE_DATA_DIR, exist_ok=True)
    source_path = os.path.join(_CUSTOM_TILE_DATA_DIR, f"{safe_key}.geojson")
    try:
        with open(source_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("type") not in ("FeatureCollection", "Feature"):
            raise ValueError("仅支持 GeoJSON FeatureCollection / Feature")
    except Exception as e:
        with contextlib.suppress(Exception):
            os.remove(source_path)
        raise HTTPException(status_code=400, detail=f"GeoJSON 文件无效: {e}")
    meta = {
        "label": label.strip() or safe_key,
        "color": style["stroke"],
        "source": source_path,
        "style": style,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "build_type": build_type,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    registry = _load_tile_registry()
    registry[safe_key] = meta
    _save_tile_registry(registry)
    if build_type in ("raster", "both"):
        _overlay_register_layer(safe_key, source_path, style)
    stats = {"tile_count": 0, "size_bytes": 0}
    if build_type in ("vector", "both"):
        stats = _run_tippecanoe(safe_key, source_path, min_zoom, max_zoom)
    geoserver_result = None
    if auto_publish:
        try:
            import_result = _gs_client.import_geojson_to_postgis(safe_key, source_path)
            geoserver_result = _gs_client.publish_by_tm_key(
                safe_key,
                {
                    "type": "vector",
                    "label": meta["label"],
                    "source_path": source_path,
                    "style": style,
                },
            )
            geoserver_result["import"] = import_result
        except _GeoServerUnavailable as e:
            raise HTTPException(status_code=502, detail=f"GeoServer 自动发布失败: {e}")
    return JSONResponse(content={"success": True, "layer": safe_key, "meta": meta, "geoserver": geoserver_result, **stats})


@app.get("/api/drone_imagery/layers")
async def drone_imagery_layers():
    layers = [
        _drone_layer_to_row(key, meta)
        for key, meta in _load_drone_registry().items()
        if isinstance(meta, dict)
    ]
    return JSONResponse(content={"success": True, "layers": layers})


@app.post("/api/drone_imagery/register")
async def drone_imagery_register(req: DroneImageryRegisterRequest):
    row = _register_drone_imagery(req)
    return JSONResponse(content={"success": True, "layer": row["key"], "item": row})


@app.delete("/api/drone_imagery/{layer_key}")
async def drone_imagery_delete(layer_key: str, delete_files: bool = False):
    """删除无人机影像图层：从注册表中移除，并可选删除 MBTiles / 工作文件。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", layer_key):
        raise HTTPException(status_code=400, detail=f"非法的图层 key: {layer_key}")
    registry = _load_drone_registry()
    if layer_key not in registry:
        raise HTTPException(status_code=404, detail=f"无人机影像图层不存在: {layer_key}")
    meta = registry.pop(layer_key)
    _save_drone_registry(registry)

    deleted_files = []
    if delete_files and isinstance(meta, dict):
        # 删除 MBTiles 文件
        mbtiles_path = meta.get("path") or ""
        if mbtiles_path and os.path.isfile(mbtiles_path):
            try:
                os.remove(mbtiles_path)
                deleted_files.append(mbtiles_path)
            except OSError as e:
                logger.warning("删除 MBTiles 文件失败: %s %s", mbtiles_path, e)
        # 删除工作目录中的临时文件（如 _3857.tif）
        for suffix in ("_3857.tif", ".mbtiles"):
            work_file = os.path.join(_DRONE_WORK_DIR, f"{layer_key}{suffix}")
            if os.path.isfile(work_file):
                try:
                    os.remove(work_file)
                    deleted_files.append(work_file)
                except OSError as e:
                    logger.warning("删除工作文件失败: %s %s", work_file, e)

    logger.info("无人机影像图层已删除: key=%s, delete_files=%s, removed=%s", layer_key, delete_files, deleted_files)
    return JSONResponse(content={
        "success": True,
        "layer": layer_key,
        "deleted_files": deleted_files,
    })


@app.get("/api/file_browser")
async def file_browser(path: str = "/mnt", extensions: str = ".tif,.tiff"):
    """浏览服务器目录，返回子目录和文件列表（异步+超时保护）"""
    import pathlib, asyncio, concurrent.futures

    def _list_dir(dir_path: str, exts: str):
        target = pathlib.Path(dir_path).resolve()
        if not target.exists():
            return {"error": f"路径不存在: {dir_path}", "code": 404}
        if not target.is_dir():
            return {"error": f"不是目录: {dir_path}", "code": 400}
        ext_set = set(e.strip().lower() for e in exts.split(",") if e.strip())
        dirs_list, files_list = [], []
        try:
            for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith('.'):
                    continue
                if item.is_dir():
                    dirs_list.append({"name": item.name, "type": "dir"})
                elif item.is_file():
                    if ext_set and item.suffix.lower() not in ext_set:
                        continue
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                    files_list.append({"name": item.name, "type": "file", "size": size})
        except PermissionError:
            return {"error": f"无权访问: {dir_path}", "code": 403}
        parent = str(target.parent) if str(target) != "/" else None
        return {"path": str(target), "parent": parent, "dirs": dirs_list, "files": files_list}

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _list_dir, path, extensions),
            timeout=10
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"读取目录超时（10秒），网络共享可能响应缓慢: {path}")
    if "error" in result:
        raise HTTPException(status_code=result["code"], detail=result["error"])
    return JSONResponse(content=result)


@app.post("/api/drone_imagery/build")
async def drone_imagery_build(req: DroneImageryBuildRequest):
    row = _run_drone_mbtiles_build(req)
    return JSONResponse(content={"success": True, "layer": row["key"], "item": row})


def _run_drone_build_with_progress(req: DroneImageryBuildRequest):
    """Generator that yields SSE progress events during drone imagery build."""
    import time as _time

    def _send(stage, pct, msg, **extra):
        payload = {"stage": stage, "percent": pct, "message": msg, **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    source_path = os.path.abspath(req.source_path)
    if not os.path.isfile(source_path):
        yield _send("error", 0, f"GeoTIFF 文件不存在: {source_path}")
        return
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(source_path))[0])
    min_zoom = max(0, min(22, int(req.min_zoom)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom)))
    tile_format = req.tile_format.upper()
    if tile_format not in ("PNG", "PNG8", "JPEG"):
        tile_format = "PNG"
    quality = max(1, min(100, int(req.quality)))
    os.makedirs(_DRONE_MBTILES_DIR, exist_ok=True)
    os.makedirs(_DRONE_WORK_DIR, exist_ok=True)
    warped_path = os.path.join(_DRONE_WORK_DIR, f"{key}_3857.tif")
    mbtiles_path = os.path.join(_DRONE_MBTILES_DIR, f"{key}.mbtiles")
    if os.path.exists(mbtiles_path) and not req.overwrite:
        yield _send("error", 0, f"MBTiles 已存在: {mbtiles_path}")
        return
    for path in (warped_path, mbtiles_path):
        if os.path.exists(path) and req.overwrite:
            os.remove(path)

    # === CRS 检测与自动修复（处理地理坐标系标记+投影坐标数据的错误文件） ===
    yield _send("warp", 0, "检测源文件坐标系...")
    s_srs = None
    try:
        import subprocess as _sp
        gdalinfo_proc = _sp.run(
            ["gdalinfo", source_path],
            capture_output=True, text=True, timeout=30,
        )
        if gdalinfo_proc.returncode == 0:
            info_output = gdalinfo_proc.stdout + gdalinfo_proc.stderr
            has_projcrs = "PROJCRS[" in info_output
            has_geogcrs = "GEOGCRS[" in info_output

            # 提取 Origin 坐标判断是否是投影坐标（米制）
            origin_match = re.search(r'Origin\s*=\s*\(([\d.]+),\s*([\d.]+)\)', info_output)
            is_projected_coords = False
            if origin_match:
                ox, oy = float(origin_match.group(1)), float(origin_match.group(2))
                is_projected_coords = abs(ox) > 360 or abs(oy) > 90

            # 提取 EPSG 编码
            epsg_matches = re.findall(r'ID\["EPSG",(\d+)\]', info_output)
            proj_epsg_candidates = [int(m) for m in epsg_matches if 2000 <= int(m) <= 9999]

            if has_projcrs and proj_epsg_candidates:
                # 正常情况：投影坐标系（如 EPSG:4548）
                src_epsg = proj_epsg_candidates[-1]
                s_srs = f"EPSG:{src_epsg}"
                yield _send("warp", 2, f"源坐标系: EPSG:{src_epsg}")
            elif has_geogcrs and not has_projcrs and is_projected_coords:
                # 异常：WKT 标记为地理坐标系，但坐标明显是投影米制 → 自动修正
                yield _send("warp", 2, "检测到坐标系标记异常（地理CRS+投影坐标），自动修正为 EPSG:4548")
                _sp.run(
                    ["gdal_edit.py", "-a_srs", "EPSG:4548", source_path],
                    capture_output=True, text=True, timeout=30,
                )
                s_srs = "EPSG:4548"
                yield _send("warp", 4, "已修正 CRS 为 EPSG:4548 (CGCS2000 / CM 117E)")
            elif epsg_matches:
                # 纯地理坐标系（如 EPSG:4490, 4326）且坐标确实是经纬度
                s_srs = f"EPSG:{epsg_matches[-1]}"
                yield _send("warp", 2, f"源坐标系(地理): {s_srs}")
    except Exception as e:
        yield _send("warp", 2, f"CRS检测警告: {str(e)[:80]}，将继续尝试投影变换")

    # === 构建 GDAL 环境变量 ===
    gdal_env = os.environ.copy()
    # 确保 PROJ_LIB 指向系统 proj.db 所在目录
    for candidate in ("/usr/share/proj", "/usr/local/share/proj"):
        if os.path.isfile(os.path.join(candidate, "proj.db")):
            gdal_env["PROJ_LIB"] = candidate
            break

    warp_cmd = [
        "gdalwarp", "-t_srs", "EPSG:3857", "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS", "-r", "bilinear",
        "-of", "GTiff", "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE",
        "-co", "BIGTIFF=YES",
    ]
    if s_srs:
        warp_cmd.extend(["-s_srs", s_srs])  # 显式指定源 CRS
    warp_cmd.extend([source_path, warped_path])

    stages = [
        ("warp", "投影变换 (gdalwarp)", warp_cmd, 5, 45),
        ("translate", "转换 MBTiles (gdal_translate)", [
            "gdal_translate", "-of", "MBTILES",
            "-co", f"TILE_FORMAT={tile_format}", "-co", f"QUALITY={quality}",
            "-co", "RESAMPLING=BILINEAR", "-co", "WRITE_BOUNDS=YES",
            "-co", "WRITE_MINMAXZOOM=YES", warped_path, mbtiles_path,
        ], 45, 75),
    ]
    overview_count = max(0, max_zoom - min_zoom)
    overview_factors = [str(2 ** i) for i in range(1, overview_count + 1)]
    if overview_factors:
        stages.append((
            "overview", "构建瓦片金字塔 (gdaladdo)",
            ["gdaladdo", "-r", "average", mbtiles_path, *overview_factors],
            75, 95,
        ))

    try:
        for stage_key, stage_label, cmd, pct_start, pct_end in stages:
            yield _send(stage_key, pct_start, f"开始{stage_label}...")
            proc = subprocess.Popen(
                cmd, cwd=os.path.dirname(__file__),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=gdal_env,
            )
            t0 = _time.time()
            last_pct = pct_start
            last_emit_ts = t0
            # 收集输出用于错误诊断
            _stderr_lines = []
            while proc.poll() is None:
                line = ""
                try:
                    import select
                    ready, _, _ = select.select([proc.stdout], [], [], 1)
                    if ready:
                        line = proc.stdout.readline() or ""
                        if not line:
                            # 也读 stderr
                            try: _stderr_lines.append(proc.stderr.readline() or "")
                            except: pass
                except Exception:
                    line = proc.stdout.readline() or ""
                elapsed = _time.time() - t0
                progress_match = re.search(r'(\d+)%', line) if line else None
                dot_numbers = re.findall(r'(?:^|\.{3})(\d{1,3})(?=\.{3}|$)', line.strip()) if line else []
                if progress_match or dot_numbers:
                    sub_pct = int(progress_match.group(1) if progress_match else dot_numbers[-1])
                    if 0 <= sub_pct <= 100:
                        last_pct = pct_start + (pct_end - pct_start) * sub_pct / 100
                        last_emit_ts = _time.time()
                        yield _send(stage_key, int(last_pct), f"{stage_label}: {sub_pct}%")
                elif _time.time() - last_emit_ts >= 3:
                    last_pct = min(pct_end - 1, last_pct + 1)
                    last_emit_ts = _time.time()
                    yield _send(stage_key, int(last_pct), f"{stage_label} 进行中... 已运行 {int(elapsed)} 秒")
            # 读取剩余的 stderr
            try:
                remaining = proc.stderr.read()
                if remaining:
                    _stderr_lines.append(remaining)
            except Exception:
                pass
            proc.wait()
            if proc.returncode != 0:
                err_detail = "".join(_stderr_lines[-5:]).strip() or "(无详细错误输出)"
                yield _send("error", int(last_pct),
                    f"{stage_label} 失败 (退出码 {proc.returncode}): {err_detail[:200]}")
                return
            yield _send(stage_key, pct_end, f"{stage_label} 完成")
    except FileNotFoundError as e:
        yield _send("error", 0, f"GDAL 工具未安装: {e.filename}")
        return

    yield _send("register", 96, "注册图层...")
    try:
        register_req = DroneImageryRegisterRequest(
            layer_key=key, name=req.name or key, path=mbtiles_path,
            area_key=req.area_key, year=req.year,
            min_zoom=min_zoom, max_zoom=max_zoom, max_native_zoom=max_zoom,
            opacity=req.opacity, scheme="tms",
        )
        row = _register_drone_imagery(register_req)
        registry = _load_drone_registry()
        if key in registry:
            registry[key]["source_path"] = source_path
            registry[key]["build_type"] = "geotiff_to_mbtiles"
            registry[key]["updated_at"] = dt.now().isoformat(timespec="seconds")
            _save_drone_registry(registry)
            row = _drone_layer_to_row(key, registry[key])
    except Exception as e:
        yield _send("error", 96, f"注册失败: {str(e)}")
        return
    with contextlib.suppress(Exception):
        os.remove(warped_path)
    yield _send("done", 100, "构建完成！", layer=key)


@app.post("/api/drone_imagery/build_stream")
async def drone_imagery_build_stream(req: DroneImageryBuildRequest):
    import asyncio, queue, threading

    def _producer(q: queue.Queue):
        try:
            for chunk in _run_drone_build_with_progress(req):
                q.put(chunk)
        except Exception as e:
            q.put(f"data: {json.dumps({'stage':'error','percent':0,'message':str(e)}, ensure_ascii=False)}\n\n")
        finally:
            q.put(None)

    async def _async_gen():
        q = queue.Queue()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _producer, q)
        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(_async_gen(), media_type="text/event-stream")


@app.post("/api/drone_imagery/build_async")
async def drone_imagery_build_async(req: DroneImageryBuildRequest):
    import threading
    job_id = uuid.uuid4().hex
    _DRONE_BUILD_JOBS[job_id] = {
        "job_id": job_id,
        "stage": "queued",
        "percent": 0,
        "message": "任务已提交，等待构建...",
        "done": False,
        "success": None,
        "created_at": dt.now().isoformat(timespec="seconds"),
        "updated_at": dt.now().isoformat(timespec="seconds"),
    }

    def _worker():
        try:
            chunks = _run_drone_build_with_progress(req)
            for chunk in chunks:
                if not chunk.startswith("data: "):
                    continue
                try:
                    payload = json.loads(chunk[6:].strip())
                except Exception:
                    continue
                job = _DRONE_BUILD_JOBS.get(job_id, {})
                job.update(payload)
                job["updated_at"] = dt.now().isoformat(timespec="seconds")
                if payload.get("stage") in ("done", "error"):
                    job["done"] = True
                    job["success"] = payload.get("stage") == "done"
                    job["finished_at"] = dt.now().isoformat(timespec="seconds")
                _DRONE_BUILD_JOBS[job_id] = job
        except Exception as e:
            payload = {"stage": "error", "percent": 0, "message": str(e)}
            job = _DRONE_BUILD_JOBS.get(job_id, {})
            job.update(payload)
            job["updated_at"] = dt.now().isoformat(timespec="seconds")
            job["done"] = True
            job["success"] = False
            job["finished_at"] = dt.now().isoformat(timespec="seconds")
            _DRONE_BUILD_JOBS[job_id] = job

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse(content={"success": True, "job_id": job_id})


@app.get("/api/drone_imagery/build_status/{job_id}")
async def drone_imagery_build_status(job_id: str):
    job = _DRONE_BUILD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="构建任务不存在或已过期")
    return JSONResponse(content={"success": True, "job": job})


@app.get("/api/drone_imagery/tile/{layer}/{z}/{x}/{y}.png")
async def drone_imagery_tile(layer: str, z: int, x: int, y: int):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=404, detail=f"unknown drone imagery layer: {layer}")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    registry = _load_drone_registry()
    meta = registry.get(layer)
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail=f"unknown drone imagery layer: {layer}")
    path = os.path.abspath(meta.get("path") or "")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"MBTiles 文件不存在: {path}")
    tile_row = (n - 1 - y) if meta.get("scheme", "tms") == "tms" else y
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tile_row),
            ).fetchone()
    except Exception as e:
        logger.exception("read drone imagery tile failed: %s/%s/%s/%s", layer, z, x, y)
        raise HTTPException(status_code=500, detail=str(e))
    if not row:
        return Response(status_code=204)
    media_type = _media_type_for_tile_format(str(meta.get("tile_format") or "png"))
    return Response(
        content=row[0],
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=604800",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------- 3D Tiles 管理 API ----------

@app.get("/api/tile_manager/3dtiles")
async def tile_manager_3dtiles_list():
    """列出所有已注册的 3D Tiles 数据集。"""
    registry = _load_3dtiles_registry()
    datasets = []
    for key, meta in registry.items():
        if isinstance(meta, dict):
            datasets.append(_3dtiles_layer_to_row(key, meta))
    return JSONResponse(content={"success": True, "datasets": datasets})


@app.post("/api/tile_manager/3dtiles/upload")
async def tile_manager_3dtiles_upload(
    file: UploadFile = File(...),
    key: str = Form(""),
    name: str = Form(""),
    label: str = Form(""),
    alt_offset: float = Form(0.0),
    auto_ground_clamp: bool = Form(True),
    description: str = Form(""),
):
    """上传 3D Tiles zip 包，自动解压并注册。"""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请选择 .zip 文件")
    safe_key = _sanitize_layer_key(key or os.path.splitext(file.filename)[0])
    target_dir = os.path.join(_3DTILES_DATA_DIR, safe_key)
    if os.path.exists(target_dir):
        raise HTTPException(status_code=409, detail=f"数据集 key 已存在: {safe_key}")
    os.makedirs(_3DTILES_DATA_DIR, exist_ok=True)
    try:
        import tempfile, zipfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
        os.makedirs(target_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(target_dir)
        os.unlink(tmp_path)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="无效的 zip 文件")
    except Exception as e:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"解压失败: {str(e)}")
    tileset_path = os.path.join(target_dir, "tileset.json")
    if not os.path.isfile(tileset_path):
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="zip 包中未找到 tileset.json")
    meta_info = _read_3dtiles_meta(target_dir)
    _write_3dtiles_meta(target_dir, {
        "name": name.strip() or meta_info.get("name") or safe_key,
        "label": label.strip() or meta_info.get("label") or name.strip() or safe_key,
        "altOffset": float(alt_offset),
        "autoGroundClamp": bool(auto_ground_clamp),
        "description": description.strip() or meta_info.get("description") or "",
        "center": meta_info.get("center"),
    })
    stats = _count_3dtiles(target_dir)
    registry = _load_3dtiles_registry()
    registry[safe_key] = {
        "directory": target_dir,
        "label": label.strip() or name.strip() or safe_key,
        "name": name.strip() or safe_key,
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "alt_offset": float(alt_offset),
        "auto_ground_clamp": bool(auto_ground_clamp),
        "center": meta_info.get("center"),
        "description": description.strip() or "",
        "meta": meta_info,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    _save_3dtiles_registry(registry)
    logger.info("3dtiles uploaded: key=%s, tiles=%s", safe_key, stats["tile_count"])
    return JSONResponse(content={
        "success": True,
        "layer": safe_key,
        "item": _3dtiles_layer_to_row(safe_key, registry[safe_key]),
    })


@app.post("/api/tile_manager/3dtiles/register")
async def tile_manager_3dtiles_register(req: ThreeDTilesRegisterRequest):
    """注册服务器上已有的 3D Tiles 目录。"""
    directory = os.path.abspath(req.directory)
    if not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail=f"目录不存在: {directory}")
    tileset_path = os.path.join(directory, "tileset.json")
    if not os.path.isfile(tileset_path):
        raise HTTPException(status_code=400, detail="目录中未找到 tileset.json")
    safe_key = _sanitize_layer_key(req.key or os.path.basename(directory))
    registry = _load_3dtiles_registry()
    if safe_key in registry:
        raise HTTPException(status_code=409, detail=f"数据集 key 已存在: {safe_key}")
    meta_info = _read_3dtiles_meta(directory)
    _write_3dtiles_meta(directory, {
        "name": req.name.strip() or meta_info.get("name") or safe_key,
        "label": req.label.strip() or meta_info.get("label") or req.name.strip() or safe_key,
        "altOffset": float(req.alt_offset),
        "autoGroundClamp": bool(req.auto_ground_clamp),
        "description": req.description.strip() or meta_info.get("description") or "",
        "center": meta_info.get("center"),
    })
    stats = _count_3dtiles(directory)
    registry[safe_key] = {
        "directory": directory,
        "label": req.label.strip() or req.name.strip() or safe_key,
        "name": req.name.strip() or safe_key,
        "tile_count": stats["tile_count"],
        "size_bytes": stats["size_bytes"],
        "alt_offset": float(req.alt_offset),
        "auto_ground_clamp": bool(req.auto_ground_clamp),
        "center": meta_info.get("center"),
        "description": req.description.strip() or "",
        "meta": meta_info,
        "created_at": dt.now().isoformat(timespec="seconds"),
    }
    _save_3dtiles_registry(registry)
    logger.info("3dtiles registered: key=%s, directory=%s", safe_key, directory)
    return JSONResponse(content={
        "success": True,
        "layer": safe_key,
        "item": _3dtiles_layer_to_row(safe_key, registry[safe_key]),
    })


@app.delete("/api/tile_manager/3dtiles/{key}")
async def tile_manager_3dtiles_delete(key: str, delete_files: bool = False):
    """删除已注册的 3D Tiles 数据集。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    registry = _load_3dtiles_registry()
    if key not in registry:
        raise HTTPException(status_code=404, detail=f"数据集不存在: {key}")
    meta = registry.pop(key)
    _save_3dtiles_registry(registry)
    deleted_files = []
    if delete_files:
        directory = meta.get("directory") or ""
        if directory and os.path.isdir(directory):
            try:
                shutil.rmtree(directory)
                deleted_files.append(directory)
            except OSError as e:
                logger.warning("删除 3D Tiles 目录失败: %s %s", directory, e)
    logger.info("3dtiles deleted: key=%s, delete_files=%s, removed=%s", key, delete_files, deleted_files)
    return JSONResponse(content={
        "success": True,
        "layer": key,
        "deleted_files": deleted_files,
    })


@app.get("/api/3dtiles/{key}/tileset.json")
async def serve_3dtiles_tileset(key: str):
    """提供 3D Tiles 的 tileset.json 给 Cesium 加载。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    registry = _load_3dtiles_registry()
    meta = registry.get(key)
    directory = (meta.get("directory") if isinstance(meta, dict) else None) or os.path.join(_3DTILES_DATA_DIR, key)
    tileset_path = os.path.join(directory, "tileset.json")
    if not os.path.isfile(tileset_path):
        raise HTTPException(status_code=404, detail="tileset.json 不存在")
    return FileResponse(
        tileset_path,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/3dtiles/{key}/{file_path:path}")
async def serve_3dtiles_file(key: str, file_path: str):
    """提供 3D Tiles 数据集内的任意文件（子瓦片、b3dm 等）给 Cesium 加载。"""
    if not re.match(r"^[A-Za-z0-9_-]+$", key):
        raise HTTPException(status_code=400, detail=f"非法的 key: {key}")
    # 防止路径穿越
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    registry = _load_3dtiles_registry()
    meta = registry.get(key)
    directory = (meta.get("directory") if isinstance(meta, dict) else None) or os.path.join(_3DTILES_DATA_DIR, key)
    full_path = os.path.join(directory, file_path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    # 根据扩展名设置 MIME 类型
    ext = os.path.splitext(file_path)[1].lower()
    media_type_map = {
        ".json": "application/json",
        ".b3dm": "application/octet-stream",
        ".i3dm": "application/octet-stream",
        ".pnts": "application/octet-stream",
        ".cmpt": "application/octet-stream",
        ".gltf": "model/gltf+json",
        ".glb": "model/gltf-binary",
        ".bin": "application/octet-stream",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    media_type = media_type_map.get(ext, "application/octet-stream")
    return FileResponse(
        full_path,
        media_type=media_type,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
    )


# ---------- 卫星影像动态切片（从本地 GeoTIFF 按需渲染） ----------
try:
    from tools.satellite_tile_service import get_satellite_tile, get_tile_bounds_4326, tiles_in_bounds
    _satellite_tile_available = True
    logger.info("satellite tile service loaded")
except Exception as _e:
    _satellite_tile_available = False
    logger.warning("satellite tile service NOT available: %s", _e)


@app.get("/api/satellite_tile/{z}/{x}/{y}.png")
async def satellite_tile(z: int, x: int, y: int):
    if not _satellite_tile_available:
        raise HTTPException(status_code=503, detail="satellite tile service not available")
    if z < 0 or z > 22:
        raise HTTPException(status_code=400, detail="invalid zoom")
    n = 1 << z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="invalid tile xy")
    png = get_satellite_tile(z, x, y)
    if png is None:
        return Response(status_code=204)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


# ==============================
# ✅ GeoServer 集成代理接口
# ==============================
class GeoServerPublishRequest(BaseModel):
    layer: str


class GeoServerSeedRequest(BaseModel):
    layer: str
    bounds: Optional[List[float]] = None  # [minx, miny, maxx, maxy] in EPSG:4326
    min_zoom: int = 0
    max_zoom: int = 14
    format: str = "image/png"
    threads: int = 1


def _find_tile_layer_meta(layer_key: str) -> Optional[Dict[str, Any]]:
    """从 /api/tile_manager/layers 同源逻辑里查 meta，避免重复请求。"""
    # 内置 + 自定义矢量/栅格
    merged = _merged_tile_meta()
    if layer_key in merged:
        meta = merged[layer_key]
        return {
            "type": "vector" if meta.get("build_type", "both") in ("vector", "both") else "raster",
            "label": meta.get("label", layer_key),
            "source_path": meta.get("source"),
            "style": meta.get("style") or {},
        }
    # 无人机
    drone = _load_drone_registry()
    if layer_key in drone:
        meta = drone[layer_key]
        # 优先使用 _3857.tif（保留时）；否则回退 source_path
        source_path = meta.get("source_path")
        warped = os.path.join(_DRONE_WORK_DIR, f"{layer_key}_3857.tif")
        if os.path.isfile(warped):
            source_path = warped
        return {
            "type": "drone",
            "label": meta.get("name", layer_key),
            "source_path": source_path,
        }
    if layer_key == "ceshen":
        return {"type": "vector", "label": "ceshen", "source_path": ""}
    return None


@app.get("/api/geoserver/status")
async def geoserver_status():
    try:
        return JSONResponse(content=_gs_client.health_status())
    except Exception as e:  # 兜底，避免任何意外影响主流程
        logger.warning("geoserver_status error: %s", e)
        return JSONResponse(content={"available": False, "reason": str(e)})


@app.get("/api/geoserver/layers")
async def geoserver_layers():
    try:
        items = _gs_client.list_layers()
        return JSONResponse(content={"available": True, "layers": items})
    except _GeoServerUnavailable as e:
        return JSONResponse(content={"available": False, "reason": str(e), "layers": []})
    except Exception as e:
        logger.warning("geoserver_layers error: %s", e)
        return JSONResponse(content={"available": False, "reason": str(e), "layers": []})


@app.get("/api/geoserver/capabilities")
async def geoserver_capabilities():
    return JSONResponse(content={
        "url": _gs_client.get_config()["url"],
        "workspace": _gs_client.get_config()["workspace"],
        "capabilities": _gs_client.capabilities_urls(),
    })


@app.get("/api/geoserver/preview/{layer}.png")
async def geoserver_preview(layer: str, bbox: Optional[str] = None, width: int = 520, height: int = 280):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        parsed_bbox = None
        if bbox:
            parts = [float(v) for v in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("bbox must contain 4 numbers")
            parsed_bbox = parts
        content, content_type = _gs_client.preview_image(layer, bbox=parsed_bbox, width=width, height=height)
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 预览失败: {e}")


@app.post("/api/geoserver/publish")
async def geoserver_publish(req: GeoServerPublishRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    meta = _find_tile_layer_meta(req.layer)
    if not meta:
        raise HTTPException(status_code=404, detail=f"unknown layer: {req.layer}")
    try:
        result = _gs_client.publish_by_tm_key(req.layer, meta)
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 发布失败: {e}")


@app.post("/api/geoserver/unpublish")
async def geoserver_unpublish(req: GeoServerPublishRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.unpublish_layer(req.layer, recurse=True)
        return JSONResponse(content={"success": True, "layer": req.layer, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GeoServer 取消发布失败: {e}")


@app.post("/api/geoserver/seed")
async def geoserver_seed(req: GeoServerSeedRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.gwc_seed(
            req.layer,
            bounds=req.bounds,
            min_zoom=req.min_zoom,
            max_zoom=req.max_zoom,
            fmt=req.format,
            threads=req.threads,
        )
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC seed 失败: {e}")


@app.post("/api/geoserver/truncate")
async def geoserver_truncate(req: GeoServerSeedRequest):
    if not re.match(r"^[A-Za-z0-9_-]+$", req.layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        result = _gs_client.gwc_truncate(
            req.layer,
            bounds=req.bounds,
            min_zoom=req.min_zoom,
            max_zoom=req.max_zoom,
            fmt=req.format,
        )
        return JSONResponse(content={"success": True, **result})
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC truncate 失败: {e}")


@app.get("/api/geoserver/seed/{layer}")
async def geoserver_seed_status(layer: str):
    if not re.match(r"^[A-Za-z0-9_-]+$", layer):
        raise HTTPException(status_code=400, detail="invalid layer key")
    try:
        return JSONResponse(content=_gs_client.gwc_seed_status(layer))
    except _GeoServerUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GWC 状态查询失败: {e}")


@app.get("/proxy/gf-tiles/{z}/{y}/{x}")
async def proxy_gf_tiles(z: int, y: int, x: int):
    """代理 GF_2024_YM 高分影像瓦片 (HTTP, /arcgis/ 路径)"""
    url = f"http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile/{z}/{y}/{x}"
    return await _proxy_arcgis_tile(url)


@app.get("/proxy/gf2025-tiles/{z}/{y}/{x}")
async def proxy_gf2025_tiles(z: int, y: int, x: int):
    """代理 GF_202509_cache 高分影像瓦片"""
    url = f"http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202509_cache/MapServer/tile/{z}/{y}/{x}"
    return await _proxy_arcgis_tile(url)


async def _proxy_arcgis_tile(url: str):
    """通用 ArcGIS 瓦片代理"""
    headers = {
        "User-Agent": "MapAssistant/1.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.get(url, headers=headers)
        content_type = r.headers.get("Content-Type", "image/png")
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=600",
            "Content-Type": content_type,
        }
        return Response(content=r.content, headers=resp_headers, status_code=r.status_code)
    except Exception as e:
        logger.error(f"代理 ArcGIS 瓦片失败: {url} - {e}")
        raise HTTPException(status_code=502, detail="代理上游不可达")

@app.get("/suggestions")
async def get_suggestions():
    return {
        "suggestions": [
            "清除所有地图标记",
            "切换到卫星图层",
            "加载潢河郝楼可采区的矢量数据",
            "统计数据库中的点数量",
            "显示所有采样点并标注高程"
        ]
    }

# ==============================
# ✅ 新增：会话管理接口
# ==============================

class SessionCreate(BaseModel):
    title: Optional[str] = None

class SessionRename(BaseModel):
    title: str

@app.get("/api/sessions")
async def list_sessions():
    try:
        with contextlib.closing(get_db()) as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"List sessions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sessions")
async def create_session(body: SessionCreate):
    try:
        sid = str(uuid.uuid4())
        now = now_iso()
        title = (body.title or "新对话")[:50]
        with contextlib.closing(get_db()) as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (sid, title, now, now)
            )
            conn.commit()
        return {"id": sid, "title": title, "created_at": now, "updated_at": now}
    except Exception as e:
        logger.error(f"Create session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    try:
        with contextlib.closing(get_db()) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            conn.commit()
        return {"success": True}
    except Exception as e:
        logger.error(f"Delete session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, body: SessionRename):
    try:
        title = body.title[:50]
        with contextlib.closing(get_db()) as conn:
            conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, now_iso(), session_id)
            )
            conn.commit()
        return {"success": True, "title": title}
    except Exception as e:
        logger.error(f"Rename session error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    try:
        with contextlib.closing(get_db()) as conn:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id ASC",
                (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Get session messages error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# 知识库管理接口 (RagFlow)
# ==============================
@app.get("/api/knowledge")
async def list_knowledge():
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({'operation': 'list_topics'})
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"List knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/knowledge/{document_id}")
async def get_knowledge_content(document_id: str):
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'get_content',
            'document_id': document_id
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Get knowledge content error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/knowledge/qa")
async def knowledge_qa(req: KnowledgeQARequest):
    """
    智能问答：KnowledgeQAAgent 四阶段管道
    Stage 1: 查询理解（Qwen 提取实体/属性/时间）
    Stage 2: 多路检索（RagFlow 原问题+关键词+宽泛查询）
    Stage 3: 结构化提取（Qwen 从 chunk 提取 JSON）
    Stage 4: 推理生成（Qwen 合成最终答案 + 来源引用）
    """
    try:
        agent = KnowledgeQAAgent()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, agent.answer, req.question, req.top_k)
        return result
    except Exception as e:
        logger.error(f"Knowledge QA error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/knowledge/add")
async def add_knowledge(req: KnowledgeAddRequest):
    """添加文本文档到 RagFlow"""
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'add_document',
            'name': req.name,
            'content': req.content
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Add knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/knowledge/upload")
async def upload_knowledge_file(file: UploadFile = File(...)):
    """上传文件到 RagFlow"""
    import tempfile as _tmp
    tmp_path = None
    try:
        # 保存上传文件到临时目录
        suffix = os.path.splitext(file.filename or "document.txt")[1] or ".txt"
        with _tmp.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'upload_file',
            'file_path': tmp_path
        })
        
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Upload knowledge file error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

@app.get("/api/knowledge/diagnose/stats")
async def diagnose_kb_stats():
    """知识库索引诊断统计 - 返回 chunk 数、文档数、存储大小等"""
    try:
        kb_tool = KnowledgeBaseTool()
        if not kb_tool._ensure_initialized():
            return {"success": False, "error": "LlamaIndex 未初始化"}
        
        store = kb_tool._index.docstore
        all_docs = list(store.docs.items())
        real_docs = [(k, v) for k, v in all_docs if k != "placeholder"]
        
        # 按文档分组
        doc_groups = {}
        for doc_id, doc in real_docs:
            meta = doc.metadata or {}
            parent_id = meta.get("document_id", doc_id)
            doc_groups.setdefault(parent_id, []).append((doc_id, doc))
        
        # 详细文档信息
        doc_list = []
        for parent_id, chunks in doc_groups.items():
            first_chunk = chunks[0][1] if chunks else None
            name = first_chunk.metadata.get("title", parent_id[:40]) if first_chunk else parent_id[:40]
            total_chars = sum(len(c.text) for _, c in chunks)
            avg_embedding = sum(len(c.embedding) if c.embedding else 0 for _, c in chunks) / max(len(chunks), 1)
            doc_list.append({
                "id": parent_id,
                "name": name,
                "chunk_count": len(chunks),
                "total_chars": total_chars,
                "avg_embedding_dim": round(avg_embedding),
                "created_at": first_chunk.metadata.get("created_at", "") if first_chunk else ""
            })
        
        doc_list.sort(key=lambda x: x["chunk_count"], reverse=True)
        
        # 存储文件大小
        import glob
        persist_dir = kb_tool._persist_dir
        file_sizes = {}
        for f in glob.glob(os.path.join(persist_dir, "*.json")):
            fname = os.path.basename(f)
            file_sizes[fname] = os.path.getsize(f)
        
        return {
            "success": True,
            "total_chunks": len(real_docs),
            "total_documents": len(doc_groups),
            "persist_dir": persist_dir,
            "chunk_size": kb_tool._index.settings.chunk_size if hasattr(kb_tool._index, 'settings') else 512,
            "chunk_overlap": kb_tool._index.settings.chunk_overlap if hasattr(kb_tool._index, 'settings') else 64,
            "embed_model": kb_tool._embed_model_name,
            "file_sizes": file_sizes,
            "documents": doc_list[:50]  # 最多返回 50 个
        }
    except Exception as e:
        logger.error(f"Diagnose stats error: {e}")
        return {"success": False, "error": str(e)}


@app.delete("/api/knowledge/{kb_id}")
async def delete_knowledge(kb_id: str):
    """从 RagFlow 删除文档"""
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'delete_document',
            'document_id': kb_id
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Delete knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _persist_chat(session_id: str, user_content: str, assistant_content: str):
    """将本轮对话持久化到 SQLite，如果 session_id 不存在则跳过。"""
    if not session_id or session_id == "default":
        return
    try:
        with contextlib.closing(get_db()) as conn:
            row = conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return  # 不是有效会话，跳过
            now = now_iso()
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, "user", user_content, now)
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, "assistant", assistant_content, now)
            )
            # 自动命名：如果是第一条消息，用前20字作为标题
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            if msg_count <= 2:  # 刚插入的这两条
                title = user_content[:20] + ("…" if len(user_content) > 20 else "")
                conn.execute(
                    "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                    (title, now, session_id)
                )
            else:
                conn.execute(
                    "UPDATE sessions SET updated_at=? WHERE id=?",
                    (now, session_id)
                )
            conn.commit()
    except Exception as e:
        logger.warning(f"_persist_chat failed (non-fatal): {e}")


def clean_response_content(content: str) -> str:
    if not content:
        return content
    content = re.sub(r'<\w+_tool[^>]*>', '', content)
    content = re.sub(r'\n\s*\n+', '\n\n', content)
    return content.strip()


def _is_explicit_map_request(user_content: str) -> bool:
    if not user_content:
        return False
    map_terms = [
        "地图", "上图", "加载", "图层", "定位", "跳转", "飞到", "显示到地图",
        "加载到地图", "标记", "标注", "打点", "落点", "经纬度", "卫星图", "底图",
        "切换", "卫星", "清除"
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


def _optimize_map_commands(user_content: str, map_commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not map_commands:
        return []

    if not _is_explicit_map_request(user_content):
        logger.info("Suppressing map commands for non-map visualization request.")
        return []

    explicit_marker = _is_explicit_marker_request(user_content)
    seen = set()
    unique_commands = []
    for cmd in map_commands:
        key = _stable_json_key(cmd)
        if key in seen:
            continue
        seen.add(key)
        unique_commands.append(cmd)

    switch_layer_cmd = None
    set_view_cmd = None
    vector_commands = []
    marker_commands = []
    fit_markers_cmd = None
    other_commands = []

    for cmd in unique_commands:
        cmd_type = cmd.get("type")
        if cmd_type == "switch_layer":
            switch_layer_cmd = cmd
        elif cmd_type == "set_view" and set_view_cmd is None:
            set_view_cmd = cmd
        elif cmd_type == "load_vector_layer":
            vector_commands.append(cmd)
        elif cmd_type == "add_marker":
            if explicit_marker and len(marker_commands) < 3:
                marker_commands.append(cmd)
        elif cmd_type == "fit_markers":
            fit_markers_cmd = cmd
        else:
            other_commands.append(cmd)

    optimized = []
    if switch_layer_cmd:
        optimized.append(switch_layer_cmd)

    if vector_commands:
        optimized.extend(vector_commands[:2])
    else:
        if set_view_cmd:
            optimized.append(set_view_cmd)
        optimized.extend(marker_commands)
        if fit_markers_cmd and marker_commands:
            optimized.append(fit_markers_cmd)

    optimized.extend(other_commands)

    logger.info(
        "Optimized map commands from %s to %s (explicit_marker=%s)",
        len(map_commands), len(optimized), explicit_marker
    )
    return optimized


def _optimize_charts(charts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not charts:
        return []

    unique = []
    seen = set()
    for chart in charts:
        config = chart.get("config") or {}
        title = ((config.get("title") or {}).get("text") if isinstance(config.get("title"), dict) else None) or chart.get("summary") or chart.get("chart_type")
        key = f"{chart.get('chart_type', 'chart')}::{title}::{_stable_json_key(config)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(chart)

    if len(unique) <= 2:
        return unique

    selected = []
    used_types = set()
    for chart in unique:
        chart_type = chart.get("chart_type", "chart")
        if chart_type not in used_types:
            selected.append(chart)
            used_types.add(chart_type)
        if len(selected) >= 2:
            break

    if not selected:
        selected = unique[:2]

    logger.info("Optimized charts from %s to %s", len(charts), len(selected))
    return selected

@app.get("/api/mvt/{z}/{x}/{y}")
async def get_mvt_tile(
    z: int,
    x: int,
    y: int,
    table_name: str = "ceshen",
    geom_col: str = "geom",
    properties: str = None,
    filter: str = None
):
    try:
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s]+$', table_name):
            raise HTTPException(status_code=400, detail="无效的表名格式")
        if not re.match(r'^[a-zA-Z0-9_"\u4e00-\u9fa5\s]+$', geom_col):
            raise HTTPException(status_code=400, detail="无效的几何列格式")

        n = 2 ** z
        lon_min = x / n * 360.0 - 180.0
        lon_max = (x + 1) / n * 360.0 - 180.0
        import math
        def tile2lat(ty, tz):
            n_ = math.pi - 2.0 * math.pi * ty / (2.0 ** tz)
            return math.degrees(math.atan(math.sinh(n_)))
        lat_max = tile2lat(y, z)
        lat_min = tile2lat(y + 1, z)

        props_select = ""
        if properties:
            fields = [p.strip() for p in properties.split(",") if p.strip()]
            cleaned = []
            for f in fields:
                if f.startswith('"') and f.endswith('"'):
                    cleaned.append(f)
                else:
                    cleaned.append(f'"{f}"')
            for f in cleaned:
                props_select += f", t.{f}"

        safe_table = table_name if table_name.startswith('"') else f'"{table_name}"'
        env_sql = f"ST_Transform(ST_MakeEnvelope({lon_min}, {lat_min}, {lon_max}, {lat_max}, 4326), 3857)"
        where_extra = ""
        if filter:
            where_extra = f" AND ({filter})"

        sql = f"""
        SELECT encode(ST_AsMVT(q, '{table_name}', 4096, 'geom'), 'base64') AS tile
        FROM (
            SELECT
                ST_AsMVTGeom(
                    ST_Transform(t.{geom_col}, 3857),
                    b.env,
                    4096,
                    256,
                    true
                ) AS geom
                {props_select}
            FROM {safe_table} t
            JOIN (SELECT {env_sql} AS env) b ON TRUE
            WHERE t.{geom_col} IS NOT NULL
              AND ST_IsValid(t.{geom_col})
              AND ST_Intersects(ST_Transform(t.{geom_col}, 3857), b.env)
              {where_extra}
        ) AS q;
        """

        conn = psycopg2.connect(host='172.136.16.52', port=5432, dbname='postgres', user='postgres')
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                if not row or not row[0]:
                    return Response(content=b"", media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=300"})
                import base64
                tile_bytes = base64.b64decode(row[0])
                return Response(content=tile_bytes, media_type="application/x-protobuf", headers={"Cache-Control": "public, max-age=300"})
        finally:
            try:
                conn.close()
            except:
                pass
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"MVT error: {e}")
        raise HTTPException(status_code=500, detail="生成矢量瓦片失败")


# ==============================
# SAM 目标识别接口
# ==============================
class SAMDetectRequest(BaseModel):
    geometry: dict  # GeoJSON Polygon geometry
    prompt: str
    mode: str = "rectangle"  # rectangle | polygon
    fast_mode: bool = False
    quick_mode: bool = False
    demo_mode: bool = False  # 超快速演示模式
    # SAM 阈值参数（可选，不传则使用环境变量默认值）
    classify_conf_thd: float | None = None   # 分类阈值
    box_conf_thd: float | None = None         # 候选框阈值
    verify_conf_thd: float | None = None      # 二次确认阈值
    auto_verify_thd: float | None = None      # 自动放行阈值（高于此值跳过验证）
    classify_max_side: int | None = None      # 分类阶段图片最大边长


@app.post("/api/sam-detect")
async def sam_detect(req: SAMDetectRequest):
    """
    SAM 目标识别：根据绘制的 GeoJSON 区域和文本提示词，执行 SAM 推理，返回 GeoJSON 结果
    支持实时进度查询（通过 /api/sam-progress/{task_id}）
    """
    import subprocess
    import tempfile
    import shutil
    import uuid

    logger.info(f"SAM detect request: prompt={req.prompt}, mode={req.mode}, fast_mode={req.fast_mode}, quick_mode={req.quick_mode}")

    # 生成唯一任务 ID 和进度文件
    task_id = uuid.uuid4().hex[:12]
    progress_dir = "/tmp/sam_progress"
    os.makedirs(progress_dir, exist_ok=True)
    progress_file = os.path.join(progress_dir, f"{task_id}.json")
    
    # 初始化进度文件
    with open(progress_file, 'w') as f:
        json.dump({"stage": "init", "current": 0, "total": 1, "message": "任务已创建，正在启动..."}, f)
    
    logger.info(f"SAM task_id: {task_id}, progress_file: {progress_file}")

    # 创建临时输出目录
    output_dir = tempfile.mkdtemp(prefix="sam_detect_")
    try:
        # 构建推理脚本路径
        sam_script = "/home/server/python/map_assistant_v1/backend/tools/sam_predict.py"
        python_bin = "/home/server/miniconda3/envs/sam/bin/python"

        # 调用推理脚本（通过环境变量传递进度文件路径）
        geometry_json = json.dumps(req.geometry)
        cmd = [python_bin, "-u", sam_script, geometry_json, req.prompt]
        if req.demo_mode:
            cmd.append("--demo")
        elif req.fast_mode:
            cmd.append("--fast")
        if req.quick_mode:
            cmd.append("--quick")

        logger.info(f"SAM command: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ,
                 "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
                 "SAM_PROGRESS_FILE": progress_file,
                 "SAM_BACKEND": os.environ.get("SAM_BACKEND", "ollama"),
                 "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11433"),
                 "SAM_OLLAMA_MODEL": os.environ.get("SAM_OLLAMA_MODEL", "gemma4:31b"),
                 "SAM_OLLAMA_CLASSIFY_CONF_THD": str(req.classify_conf_thd) if req.classify_conf_thd is not None else os.environ.get("SAM_OLLAMA_CLASSIFY_CONF_THD", "0.45"),
                 "SAM_OLLAMA_BOX_CONF_THD": str(req.box_conf_thd) if req.box_conf_thd is not None else os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.65"),
                 "SAM_OLLAMA_VERIFY_CONF_THD": str(req.verify_conf_thd) if req.verify_conf_thd is not None else os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75"),
                 "SAM_OLLAMA_AUTO_VERIFY_THD": str(req.auto_verify_thd) if req.auto_verify_thd is not None else os.environ.get("SAM_OLLAMA_AUTO_VERIFY_THD", "0.90"),
                 "SAM_OLLAMA_CLASSIFY_MAX_SIDE": str(req.classify_max_side) if req.classify_max_side is not None else os.environ.get("SAM_OLLAMA_CLASSIFY_MAX_SIDE", "640")}
        )

        if proc.returncode != 0:
            logger.error(f"SAM inference failed: {proc.stderr}")
            raise HTTPException(status_code=500, detail=f"SAM 推理失败: {proc.stderr[:500]}")

        # 解析输出
        # sam_predict.py 会输出 JSON 到 stdout
        output_lines = proc.stdout.strip().split("\n")
        # 取最后一行 JSON 输出
        result_json = None
        for line in reversed(output_lines):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result_json = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result_json is None:
            logger.error(f"SAM output parse failed. stdout: {proc.stdout[-500:]}")
            raise HTTPException(status_code=500, detail="SAM 输出解析失败")

        logger.info(f"SAM result: {result_json.get('features', []).__len__()} features")
        # 将 task_id 附加到返回结果
        result_json["_task_id"] = task_id
        return result_json

    except subprocess.TimeoutExpired:
        logger.error("SAM inference timeout (300s)")
        raise HTTPException(status_code=504, detail="SAM 推理超时（超过 5 分钟）")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"SAM detect error: {e}")
        raise HTTPException(status_code=500, detail=f"SAM 识别异常: {str(e)}")
    finally:
        # 清理进度文件
        try:
            if os.path.exists(progress_file):
                os.remove(progress_file)
        except Exception:
            pass
        # 清理临时目录
        try:
            shutil.rmtree(output_dir, ignore_errors=True)
        except Exception:
            pass


class SAMChangeDetectRequest(BaseModel):
    """SAM 变化检测请求"""
    geometry: dict              # GeoJSON Polygon geometry
    prompt: str                 # 检测目标提示词
    year_a: int = 2024          # 基准年份 A
    year_b: int = 2025          # 对比年份 B
    fast_mode: bool = True      # 快速模式（降低分辨率加速）
    demo_mode: bool = False     # 超快速演示模式


@app.post("/api/sam-change-detect")
async def sam_change_detect(req: SAMChangeDetectRequest):
    """
    SAM 变化检测：对同一区域分别使用两个年份的影像执行 SAM 检测，
    对比结果识别新增/消失的变化图斑。
    支持实时进度查询（通过 /api/sam-progress/{task_id}）
    """
    import subprocess
    import tempfile
    import shutil
    import uuid

    if req.year_a not in (2023, 2024, 2025):
        raise HTTPException(status_code=400, detail="year_a 必须为 2023/2024/2025")
    if req.year_b not in (2023, 2024, 2025):
        raise HTTPException(status_code=400, detail="year_b 必须为 2023/2024/2025")
    if req.year_a == req.year_b:
        raise HTTPException(status_code=400, detail="year_a 与 year_b 不能相同")

    logger.info(f"SAM change detect: {req.year_a} vs {req.year_b}, prompt={req.prompt}")

    task_id = uuid.uuid4().hex[:12]
    progress_dir = "/tmp/sam_progress"
    os.makedirs(progress_dir, exist_ok=True)
    progress_file = os.path.join(progress_dir, f"{task_id}.json")

    with open(progress_file, 'w') as f:
        json.dump({"stage": "init", "current": 0, "total": 1,
                    "message": f"变化检测: {req.year_a} vs {req.year_b}"}, f)

    output_dir = tempfile.mkdtemp(prefix="sam_change_")
    try:
        sam_script = "/home/server/python/map_assistant_v1/backend/tools/sam_predict.py"
        python_bin = "/home/server/miniconda3/envs/sam/bin/python"

        geometry_json = json.dumps(req.geometry)
        cmd = [python_bin, "-u", sam_script,
               "--change-detect",
               geometry_json, req.prompt,
               str(req.year_a), str(req.year_b)]
        if req.fast_mode:
            cmd.append("--fast")
        if req.demo_mode:
            cmd.append("--demo")

        logger.info(f"SAM change-detect command: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1200,
            cwd="/home/server/python/OmniOVCD",
            env={**os.environ,
                 "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "2"),
                 "SAM_PROGRESS_FILE": progress_file,
                 "SAM_BACKEND": os.environ.get("SAM_BACKEND", "omniovcd"),
                 "OLLAMA_HOST": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11433"),
                 "SAM_OLLAMA_MODEL": os.environ.get("SAM_OLLAMA_MODEL", "gemma4:31b"),
                 "SAM_OLLAMA_IMAGE_MAX_SIDE": os.environ.get("SAM_OLLAMA_IMAGE_MAX_SIDE", "1280"),
                 "SAM_OUTPUT_DIR": output_dir,
                 "PYTHONPATH": os.environ.get("PYTHONPATH", "") + ":/home/server/python/OmniOVCD"}
        )

        if proc.returncode != 0:
            logger.error(f"SAM change detect failed: {proc.stderr[:500]}")
            raise HTTPException(status_code=500, detail=f"变化检测失败: {proc.stderr[:300]}")

        output_lines = proc.stdout.strip().split("\n")
        result_json = None
        for line in reversed(output_lines):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result_json = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result_json is None:
            raise HTTPException(status_code=500, detail="变化检测输出解析失败")

        result_json["_task_id"] = task_id

        # 检查子进程返回的错误（如瓦片下载失败）
        if result_json.get("error"):
            logger.warning(f"SAM change detect partial error: {result_json['error']}")
            # 即使有 error，也返回结果（可能包含部分 features 或 fallback 结果）

        logger.info(f"SAM change detect result: {len(result_json.get('features',[]))} features")
        return result_json

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="变化检测超时（超过 20 分钟）")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"SAM change detect error: {e}")
        raise HTTPException(status_code=500, detail=f"变化检测异常: {str(e)}")
    finally:
        try:
            if os.path.exists(progress_file):
                os.remove(progress_file)
        except Exception:
            pass
        try:
            shutil.rmtree(output_dir, ignore_errors=True)
        except Exception:
            pass


# ==============================
# SAM 阈值配置接口（前端实时调节）
# ==============================

@app.get("/api/sam-thresholds")
async def get_sam_thresholds():
    """获取当前 SAM 阈值配置"""
    return {
        "classify_conf_thd": float(os.environ.get("SAM_OLLAMA_CLASSIFY_CONF_THD", "0.45")),
        "box_conf_thd": float(os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.65")),
        "verify_conf_thd": float(os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75")),
        "auto_verify_thd": float(os.environ.get("SAM_OLLAMA_AUTO_VERIFY_THD", "0.90")),
        "classify_max_side": int(os.environ.get("SAM_OLLAMA_CLASSIFY_MAX_SIDE", "640")),
    }


class SAMThresholdUpdate(BaseModel):
    classify_conf_thd: float | None = None
    box_conf_thd: float | None = None
    verify_conf_thd: float | None = None
    auto_verify_thd: float | None = None
    classify_max_side: int | None = None


class SAMDownloadRequest(BaseModel):
    geojson: dict


@app.post("/api/sam-thresholds")
async def update_sam_thresholds(req: SAMThresholdUpdate):
    """更新 SAM 阈值（写入 os.environ，重启后恢复默认）"""
    if req.classify_conf_thd is not None:
        val = max(0.0, min(1.0, req.classify_conf_thd))
        os.environ["SAM_OLLAMA_CLASSIFY_CONF_THD"] = str(val)
        logger.info(f"SAM classify threshold updated: {val}")
    if req.box_conf_thd is not None:
        val = max(0.0, min(1.0, req.box_conf_thd))
        os.environ["SAM_OLLAMA_BOX_CONF_THD"] = str(val)
        logger.info(f"SAM box threshold updated: {val}")
    if req.verify_conf_thd is not None:
        val = max(0.0, min(1.0, req.verify_conf_thd))
        os.environ["SAM_OLLAMA_VERIFY_CONF_THD"] = str(val)
        logger.info(f"SAM verify threshold updated: {val}")
    if req.auto_verify_thd is not None:
        val = max(0.0, min(1.0, req.auto_verify_thd))
        os.environ["SAM_OLLAMA_AUTO_VERIFY_THD"] = str(val)
        logger.info(f"SAM auto-verify threshold updated: {val}")
    if req.classify_max_side is not None:
        val = max(64, min(2048, req.classify_max_side))
        os.environ["SAM_OLLAMA_CLASSIFY_MAX_SIDE"] = str(val)
        logger.info(f"SAM classify max_side updated: {val}")
    return {
        "classify_conf_thd": float(os.environ.get("SAM_OLLAMA_CLASSIFY_CONF_THD", "0.45")),
        "box_conf_thd": float(os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.65")),
        "verify_conf_thd": float(os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75")),
        "auto_verify_thd": float(os.environ.get("SAM_OLLAMA_AUTO_VERIFY_THD", "0.90")),
        "classify_max_side": int(os.environ.get("SAM_OLLAMA_CLASSIFY_MAX_SIDE", "640")),
    }


# ---------- 瓦片级 VLM 检测（Gemma4 六步流程） ----------

# 高分影像 ArcGIS 切片服务（内网）
_TILE_SOURCES = {
    "arcgis_gf": "http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202308_cache/MapServer/tile/{z}/{y}/{x}",
    "esri_world": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
}
_DEFAULT_TILE_SOURCE = "arcgis_gf"


class TileDetectRequest(BaseModel):
    z: int
    x: int
    y: int
    prompt: str
    layer: str = ""           # drone imagery layer key（优先），空则用高分影像
    tile_source: str = ""     # arcgis_gf | esri_world，空则用默认
    context_radius: int = 0
    # SAM 阈值参数（可选，不传则使用环境变量默认值）
    classify_conf_thd: float | None = None
    box_conf_thd: float | None = None
    verify_conf_thd: float | None = None


def _fetch_tile_png(z: int, x: int, y: int, layer: str = "", tile_source: str = "") -> Optional[bytes]:
    """从切片服务获取瓦片 PNG bytes"""
    import urllib.request as _ur

    # 优先使用 drone imagery MBTiles
    if layer:
        registry = _load_drone_registry()
        meta = registry.get(layer)
        if meta:
            mbtiles_path = meta.get("path", "")
            if os.path.isfile(mbtiles_path):
                n = 1 << z
                tile_row = (n - 1 - y) if meta.get("scheme", "tms") == "tms" else y
                try:
                    with sqlite3.connect(mbtiles_path) as conn:
                        row = conn.execute(
                            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                            (z, x, tile_row)).fetchone()
                    if row:
                        return row[0]
                except Exception:
                    pass
        return None

    # 使用高分影像 ArcGIS 或 Esri
    src = tile_source or _DEFAULT_TILE_SOURCE
    url_tpl = _TILE_SOURCES.get(src, _TILE_SOURCES[_DEFAULT_TILE_SOURCE])
    url = url_tpl.replace("{z}", str(z)).replace("{y}", str(y)).replace("{x}", str(x))
    try:
        req = _ur.Request(url, headers={"User-Agent": "MapAssistant/1.0"})
        with _ur.urlopen(req, timeout=10) as resp:
            data = resp.read()
            if len(data) < 100:  # 空白瓦片
                return None
            return data
    except Exception as e:
        logger.debug("fetch tile %s/%s/%s failed: %s", z, x, y, e)
        return None


def _tile_bounds_4326(z: int, x: int, y: int):
    """XYZ 瓦片 → EPSG:4326 bounds (west, south, east, north)"""
    import math
    n = 2 ** z
    west = x / n * 360 - 180
    east = (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _tile_range_bounds_4326(z: int, x0: int, y0: int, x1: int, y1: int):
    import math
    n = 2 ** z
    west = x0 / n * 360 - 180
    east = (x1 + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y0 / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y1 + 1) / n))))
    return west, south, east, north


def _stitch_context_tiles(z: int, x: int, y: int, radius: int = 1,
                          layer: str = "", tile_source: str = ""):
    from PIL import Image as _Img
    import io
    radius = max(0, min(int(radius), 2))
    if radius <= 0:
        return None
    n = 1 << z
    x0, x1 = max(0, x - radius), min(n - 1, x + radius)
    y0, y1 = max(0, y - radius), min(n - 1, y + radius)
    tile_size = 256
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    canvas = _Img.new("RGB", (cols * tile_size, rows * tile_size), (0, 0, 0))
    got = 0
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            data = _fetch_tile_png(z, tx, ty, layer=layer, tile_source=tile_source)
            if not data:
                continue
            try:
                img = _Img.open(io.BytesIO(data)).convert("RGB").resize((tile_size, tile_size))
                canvas.paste(img, ((tx - x0) * tile_size, (ty - y0) * tile_size))
                got += 1
            except Exception:
                pass
    if got == 0:
        return None
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    west, south, east, north = _tile_range_bounds_4326(z, x0, y0, x1, y1)
    cx0 = (x - x0) * tile_size
    cy0 = (y - y0) * tile_size
    center_rect = (cx0, cy0, cx0 + tile_size, cy0 + tile_size)
    return buf.getvalue(), canvas.size[0], canvas.size[1], west, south, east, north, center_rect, got


def _stitch_4_subtiles(z: int, x: int, y: int, layer: str = "", tile_source: str = "") -> Optional[bytes]:
    """第5步：获取 z+1 级的 4 张子瓦片，拼成 512×512 大图"""
    from PIL import Image as _Img
    import io
    z1 = z + 1
    positions = [(2*x, 2*y), (2*x+1, 2*y), (2*x, 2*y+1), (2*x+1, 2*y+1)]
    canvas = _Img.new("RGB", (512, 512), (0, 0, 0))
    offsets = [(0, 0), (256, 0), (0, 256), (256, 256)]
    got = 0
    for (sx, sy), (ox, oy) in zip(positions, offsets):
        png = _fetch_tile_png(z1, sx, sy, layer=layer, tile_source=tile_source)
        if png:
            try:
                tile_img = _Img.open(io.BytesIO(png)).convert("RGB").resize((256, 256))
                canvas.paste(tile_img, (ox, oy))
                got += 1
            except Exception:
                pass
    if got == 0:
        return None
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ==============================
# 目标类型视觉特征增强 — 用 LLM 动态生成目标视觉描述，减少 VLM 误判
# ==============================
_enhance_cache: dict = {}  # {prompt: enriched_description} — 避免重复 LLM 调用

def _enhance_prompt(user_prompt: str) -> str:
    """用 LLM 为任意目标名称动态生成视觉特征描述（带缓存）。
    相比字典写死方案，可处理任意目标，无需人工维护。
    首次调用 ~1s，后续从缓存直接返回。
    """
    key = user_prompt.strip()
    if key in _enhance_cache:
        return _enhance_cache[key]
    try:
        result = _ollama_chat(
            f'You are a remote sensing image analysis expert. '
            f'In one concise English sentence (max 60 words), describe what '
            f'"{key}" looks like in satellite/aerial imagery: '
            f'key visual features, typical shapes, colors, textures, and spatial layout. '
            f'Also list 3-4 common look-alike objects to EXCLUDE. '
            f'Output ONLY the description, no JSON.',
            None, num_predict=100,
            system_prompt="Reply in English only. Be precise and concise. No JSON format.",
            timeout=15)
        result = result.strip().strip('"').strip("'")
        if result:
            _enhance_cache[key] = result
            print(f"[EnhancePrompt] '{key}' -> {result[:100]}...")
            return result
    except Exception as e:
        print(f"[EnhancePrompt] LLM generation failed for '{key}': {e}, fallback to original")
    return key


def _ollama_chat(prompt_text: str, image_b64: str = None, num_predict: int = 200,
                 system_prompt: str = "", timeout: int = 60) -> str:
    """调用 Ollama /api/chat + think:false，支持 system prompt；image_b64 可选（纯文本调用时传 None）"""
    import urllib.request as _ur
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11433")
    if not host.startswith("http"):
        host = f"http://{host}"
    model = os.environ.get("SAM_OLLAMA_MODEL", "gemma4:31b")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    user_msg: dict = {"role": "user", "content": prompt_text}
    if image_b64:
        user_msg["images"] = [image_b64]
    messages.append(user_msg)
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": num_predict},
    }).encode()
    req = _ur.Request(f"{host}/api/chat", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    with _ur.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read())
    return raw.get("message", {}).get("content", "")


def _extract_json(text: str):
    """从 Ollama 响应中提取 JSON"""
    import re as _re
    cleaned = _re.sub(r'```(?:json)?\s*', '', text).strip().rstrip('`')
    for candidate in [cleaned, text]:
        try:
            return json.loads(candidate)
        except Exception:
            pass
    for ch, end in [('{', '}'), ('[', ']')]:
        idx = candidate.find(ch)
        if idx >= 0:
            ridx = candidate.rfind(end)
            if ridx > idx:
                try:
                    return json.loads(candidate[idx:ridx + 1])
                except Exception:
                    pass
    return None


def _parse_boxes(objects, img_w=256, img_h=256):
    """从 VLM 返回结果中解析 bbox 列表 → [(x1,y1,x2,y2,label,conf)]
    支持:
      - Gemma4 box_2d: [y1,x1,y2,x2] 范围 0-1000
      - 标准 bbox/box/xyxy: [x1,y1,x2,y2]
      - 归一化 0-1 或像素坐标
    """
    results = []
    if not isinstance(objects, list):
        return results
    for item in objects:
        if not isinstance(item, dict):
            continue
        # Gemma4 专用 box_2d 格式: [y_min, x_min, y_max, x_max] 范围 0-1000
        box_2d = item.get("box_2d")
        if isinstance(box_2d, (list, tuple)) and len(box_2d) >= 4:
            try:
                raw = [float(v) for v in box_2d[:4]]
            except Exception:
                continue
            # box_2d = [y1, x1, y2, x2] normalized to 0-1000
            y1_n, x1_n, y2_n, x2_n = raw
            x1 = x1_n / 1000.0 * img_w
            y1 = y1_n / 1000.0 * img_h
            x2 = x2_n / 1000.0 * img_w
            y2 = y2_n / 1000.0 * img_h
        else:
            bbox = item.get("bbox") or item.get("box") or item.get("xyxy")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
            except Exception:
                continue
            # 归一化 0-1 → 像素
            if max(x1, y1, x2, y2) <= 1.5:
                x1 *= img_w; x2 *= img_w; y1 *= img_h; y2 *= img_h
        x1, x2 = sorted([x1, x2])
        y1, y2 = sorted([y1, y2])
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        results.append((x1, y1, x2, y2, item.get("label", ""), float(item.get("confidence", 0.8))))
    return results


def _boxes_to_geo_features(boxes, west, south, east, north, img_w, img_h, prompt, tile_str):
    """第6步：像素 bbox → 经纬度 GeoJSON Feature 列表"""
    features = []
    for x1, y1, x2, y2, label, conf in boxes:
        gw = west + (x1 / img_w) * (east - west)
        ge = west + (x2 / img_w) * (east - west)
        gn = north - (y1 / img_h) * (north - south)
        gs = north - (y2 / img_h) * (north - south)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[gw, gs], [ge, gs], [ge, gn], [gw, gn], [gw, gs]]]},
            "properties": {"label": label or prompt, "confidence": round(conf, 2), "tile": tile_str},
        })
    return features


def _filter_boxes_by_center(boxes, center_rect):
    """只保留中心点落在当前瓦片区域内的 bbox，避免 3x3 上下文导致相邻瓦片重复输出"""
    cx0, cy0, cx1, cy1 = center_rect
    kept = []
    for x1, y1, x2, y2, label, conf in boxes:
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        if cx0 <= cx < cx1 and cy0 <= cy < cy1:
            kept.append((x1, y1, x2, y2, label, conf))
    return kept


@app.post("/api/sam-tile-detect")
async def sam_tile_detect(req: TileDetectRequest):
    """
    六步 VLM 瓦片检测（步骤 2+3 合并为一次调用提速）：
    1. 获取瓦片影像
    2+3. Gemma4 一次调用完成 分类+检测（无目标返回 [] ~2s，有目标 ~5s）
    4. 验证：裁剪 bbox 二次确认（所有类别统一验证）
    5. 下钻：z+1 四张子瓦片拼 512×512 精细检测（目标<=3个 + z<20）
    6. bbox → 经纬度 → GeoJSON 绘制到图层
    """
    import base64, io, time
    from PIL import Image as _Image

    t0 = time.time()
    tile_str = f"{req.z}/{req.x}/{req.y}"

    # === 步骤 1: 获取瓦片影像 ===
    context = _stitch_context_tiles(
        req.z, req.x, req.y,
        radius=req.context_radius,
        layer=req.layer,
        tile_source=req.tile_source,
    )
    if context:
        png, img_w, img_h, west, south, east, north, center_rect, context_got = context
        use_context = True
    else:
        png = _fetch_tile_png(req.z, req.x, req.y, layer=req.layer, tile_source=req.tile_source)
        if png:
            img_w, img_h = 256, 256
            west, south, east, north = _tile_bounds_4326(req.z, req.x, req.y)
            center_rect = (0, 0, 256, 256)
            context_got = 1
            use_context = False

    if png is None:
        return {"type": "FeatureCollection", "features": [],
                "_meta": {"reason": "empty_tile", "tile": tile_str, "duration": round(time.time() - t0, 2)}}

    img_b64 = base64.b64encode(png).decode("utf-8")
    t1 = time.time()

    # === 读取阈值（优先从请求参数，其次环境变量） ===
    _classify_thd = req.classify_conf_thd if req.classify_conf_thd is not None else float(os.environ.get("SAM_OLLAMA_CLASSIFY_CONF_THD", "0.45"))
    _box_thd = req.box_conf_thd if req.box_conf_thd is not None else float(os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.65"))
    _verify_thd = req.verify_conf_thd if req.verify_conf_thd is not None else float(os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75"))
    if _classify_thd >= 1.0 or _box_thd >= 1.0 or _verify_thd >= 1.0:
        logger.info("tile %s all thresholds >= 1.0, skip entirely", tile_str)

    # === 目标类型视觉特征增强（减少 VLM 误判） ===
    _enriched = _enhance_prompt(req.prompt)
    _enriched_short = _enriched.split('—')[0].strip() if '—' in _enriched else _enriched

    # === 步骤 0: VLM 分类（快速判断瓦片是否含目标） ===
    classify_text = _ollama_chat(
        f'Look at this satellite/aerial image carefully. '
        f'Does it contain any "{_enriched}"? '
        f'Reply ONLY JSON: {{"has_target":true/false,"confidence":0.0-1.0,"count_estimate":N}}',
        img_b64, num_predict=50)
    classify_json = _extract_json(classify_text)
    has_target = True
    classify_conf = 0.5
    if isinstance(classify_json, dict):
        has_target = classify_json.get("has_target", True)
        classify_conf = float(classify_json.get("confidence", 0.5))
    else:
        has_target = any(w in classify_text.lower() for w in ['yes', 'true'])
        classify_conf = 0.6 if has_target else 0.3
    logger.info("tile %s classify: has_target=%s, conf=%.2f, thd=%.2f", tile_str, has_target, classify_conf, _classify_thd)

    if not has_target or classify_conf < _classify_thd:
        return {"type": "FeatureCollection", "features": [],
                "_meta": {"stage": "classify", "has_target": has_target, "classify_conf": classify_conf,
                          "classify_thd": _classify_thd, "tile": tile_str,
                          "duration": round(time.time() - t0, 2)}}

    # === 步骤 2+3 合并: system prompt + 一次调用完成分类+检测 ===
    # system prompt 引导模型进入"检测器"角色，大幅提高检测率
    # 无目标 ~2s 快速返回 []，有目标 ~10-15s 返回 box_2d
    _SYS = (f"You are a remote sensing object detector specializing in {_enriched_short}. "
            f"Detect ALL instances of the requested object type. Be thorough - do not miss any. "
            f"Output precise bounding boxes for every instance found. "
            f"IMPORTANT: Only detect objects that truly match the description — reject roads, buildings, "
            f"parking lots, water bodies, or any other similar-looking but incorrect targets.")
    detect_text = _ollama_chat(
        f'Find ALL "{_enriched}" in this {img_w}x{img_h} satellite image. '
        f'Be thorough: detect every instance, even partially visible ones at edges. '
        f'Only return objects that clearly match the visual description above — do NOT include '
        f'roads, building rooftops, parking lots, rivers, or any look-alikes. '
        f'Return ONLY JSON array: [{{"label":"name","box_2d":[y_min,x_min,y_max,x_max],"confidence":0.9}}] '
        f'coords 0-1000. If none found, return [].',
        img_b64, num_predict=1200, system_prompt=_SYS)
    t2 = time.time()
    logger.info("tile %s detect(%.1fs): %s", tile_str, t2 - t1, detect_text[:400])
    raw_objects = _extract_json(detect_text)
    if isinstance(raw_objects, dict):
        raw_objects = raw_objects.get("objects") or raw_objects.get("detections") or []
    if not isinstance(raw_objects, list):
        raw_objects = []
    raw_boxes = _parse_boxes(raw_objects, img_w, img_h)
    raw_boxes_total = len(raw_boxes)

    # === 第二轮补检: 告诉模型已检到的区域，让它找遗漏 ===
    if raw_boxes and len(raw_boxes) >= 2:
        known_desc = ", ".join(
            f"[{int(y1/img_h*1000)},{int(x1/img_w*1000)},{int(y2/img_h*1000)},{int(x2/img_w*1000)}]"
            for x1, y1, x2, y2, _, _ in raw_boxes[:12]
        )
        pass2_text = _ollama_chat(
            f'I already found {len(raw_boxes)} "{_enriched_short}" at these box_2d locations: {known_desc}. '
            f'Look CAREFULLY at the remaining areas of this {img_w}x{img_h} image for any MISSED "{_enriched_short}". '
            f'Only return true matches — do NOT include roads, buildings, parking lots, or look-alikes. '
            f'Return ONLY JSON array of NEW detections not overlapping the above: '
            f'[{{"label":"name","box_2d":[y_min,x_min,y_max,x_max],"confidence":0.8}}] '
            f'coords 0-1000. If no more found, return [].',
            img_b64, num_predict=800, system_prompt=_SYS)
        t2b = time.time()
        logger.info("tile %s pass2(%.1fs): %s", tile_str, t2b - t2, pass2_text[:300])
        pass2_objects = _extract_json(pass2_text)
        if isinstance(pass2_objects, dict):
            pass2_objects = pass2_objects.get("objects") or pass2_objects.get("detections") or []
        if isinstance(pass2_objects, list):
            pass2_boxes = _parse_boxes(pass2_objects, img_w, img_h)
            # 去重: 新框中心不能落在已有框内
            for nb in pass2_boxes:
                nx_c = (nb[0] + nb[2]) / 2
                ny_c = (nb[1] + nb[3]) / 2
                dup = False
                for eb in raw_boxes:
                    if eb[0] <= nx_c <= eb[2] and eb[1] <= ny_c <= eb[3]:
                        dup = True
                        break
                if not dup:
                    raw_boxes.append(nb)
            logger.info("tile %s pass2 added %d new boxes (total %d)",
                        tile_str, len(raw_boxes) - raw_boxes_total, len(raw_boxes))
        raw_boxes_total = len(raw_boxes)

    if use_context:
        raw_boxes = _filter_boxes_by_center(raw_boxes, center_rect)

    # === 候选框置信度过滤 ===
    raw_boxes_before = len(raw_boxes)
    raw_boxes = [(x1, y1, x2, y2, label, conf) for x1, y1, x2, y2, label, conf in raw_boxes
                 if conf >= _box_thd]
    logger.info("tile %s box filter: %d -> %d (thd=%.2f)", tile_str, raw_boxes_before, len(raw_boxes), _box_thd)

    if not raw_boxes:
        return {"type": "FeatureCollection", "features": [],
                "_meta": {"stage": "detect", "has_target": False, "boxes": 0,
                          "raw_boxes_total": raw_boxes_total,
                          "context": use_context,
                          "context_tiles": context_got,
                          "tile": tile_str, "duration": round(time.time() - t0, 2)}}

    logger.info("tile %s raw_boxes=%d", tile_str, len(raw_boxes))

    # === 步骤 4: 验证（对低于 verify_thd 的 bbox 做二次确认） ===
    verified_boxes = []
    for x1, y1, x2, y2, label, conf in raw_boxes:
        if conf >= _verify_thd:
            verified_boxes.append((x1, y1, x2, y2, label, conf))
            continue
        try:
            img = _Image.open(io.BytesIO(png))
            crop = img.crop((max(0, int(x1)), max(0, int(y1)),
                             min(img.width, int(x2)), min(img.height, int(y2))))
            if crop.width < 8 or crop.height < 8:
                continue
            buf = io.BytesIO()
            crop.save(buf, format='JPEG', quality=85)
            crop_b64 = base64.b64encode(buf.getvalue()).decode()
            vtext = _ollama_chat(
                f'This crop was proposed as target "{_enriched}". '
                f'Verify STRICTLY whether it is truly the requested target based on its visual characteristics. '
                f'If it looks like a road, building rooftop, parking lot, construction site, river, '
                f'or any other non-target object, REJECT it. '
                f'If different, similar, or uncertain, reject. '
                f'Return ONLY JSON: {{"is_target":true/false,"confidence":0.0-1.0}}',
                crop_b64, num_predict=80)
            vjson = _extract_json(vtext)
            is_target = False
            vconf = 0.0
            if isinstance(vjson, dict):
                is_target = bool(vjson.get("is_target", vjson.get("target", vjson.get("yes", False))))
                vconf = float(vjson.get("confidence", 0.5))
            else:
                is_target = any(w in vtext.lower() for w in ['yes', 'true', '是'])
                vconf = 0.65 if is_target else 0.0
            if is_target and vconf >= _verify_thd:
                verified_boxes.append((x1, y1, x2, y2, label, vconf))
            else:
                logger.info("tile %s verify rejected (conf=%.2f, vconf=%.2f, thd=%.2f): %s",
                            tile_str, conf, vconf, _verify_thd, vtext.strip()[:50])
        except Exception:
            verified_boxes.append((x1, y1, x2, y2, label, conf))
    t3 = time.time()

    if not verified_boxes:
        return {"type": "FeatureCollection", "features": [],
                "_meta": {"stage": "verify", "raw": len(raw_boxes), "verified": 0,
                          "tile": tile_str, "duration": round(time.time() - t0, 2)}}

    # === 步骤 5: 下钻拼接（目标<=3个 + z<20） ===
    drill_features = []
    if len(verified_boxes) <= 3 and req.z < 20:
        big_jpg = _stitch_4_subtiles(req.z, req.x, req.y, layer=req.layer, tile_source=req.tile_source)
        if big_jpg:
            big_b64 = base64.b64encode(big_jpg).decode("utf-8")
            drill_text = _ollama_chat(
                f'Find all "{_enriched}" in this satellite image. '
                f'Only return objects that clearly match — do NOT include roads, buildings, or look-alikes. '
                f'Return ONLY JSON array: [{{"label":"name","box_2d":[y_min,x_min,y_max,x_max],"confidence":0.9}}] '
                f'coords 0-1000. If none, return [].',
                big_b64, num_predict=600, system_prompt=_SYS)
            drill_objects = _extract_json(drill_text)
            if isinstance(drill_objects, dict):
                drill_objects = drill_objects.get("objects") or drill_objects.get("detections") or []
            if isinstance(drill_objects, list):
                drill_parsed = _parse_boxes(drill_objects, 512, 512)
                if drill_parsed:
                    drill_features = _boxes_to_geo_features(
                        drill_parsed, west, south, east, north, 512, 512, req.prompt,
                        f"{req.z}+1/{req.x}/{req.y}")
    t4 = time.time()

    # === 步骤 6: bbox → 经纬度坐标 → GeoJSON ===
    if drill_features:
        features = drill_features
    else:
        features = _boxes_to_geo_features(
            verified_boxes, west, south, east, north, img_w, img_h, req.prompt, tile_str)

    dur = round(time.time() - t0, 2)
    logger.info("tile %s DONE: raw=%d verified=%d drilled=%d final=%d (detect=%.1fs verify=%.1fs drill=%.1fs total=%.1fs)",
                tile_str, len(raw_boxes), len(verified_boxes), len(drill_features), len(features),
                t2 - t1, t3 - t2, t4 - t3, dur)

    return {
        "type": "FeatureCollection",
        "features": features,
        "_meta": {
            "stage": "complete",
            "has_target": True,
            "raw_boxes": len(raw_boxes),
            "raw_boxes_total": raw_boxes_total,
            "verified": len(verified_boxes),
            "drilled": len(drill_features),
            "final": len(features),
            "context": use_context,
            "context_tiles": context_got,
            "image_size": [img_w, img_h],
            "tile": tile_str,
            "duration": dur,
        },
    }


@app.get("/api/sam-progress/{task_id}")
async def sam_progress(task_id: str):
    """查询 SAM 推理实时进度"""
    progress_file = f"/tmp/sam_progress/{task_id}.json"
    if not os.path.exists(progress_file):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    try:
        with open(progress_file) as f:
            return json.load(f)
    except Exception as e:
        return {"stage": "error", "current": 0, "total": 1, "message": str(e)}
@app.post("/api/sam-download")
async def sam_download(req: SAMDownloadRequest):
    """
    将 SAM 识别结果 GeoJSON 打包为 SHP + ZIP 下载
    """
    import zipfile
    import tempfile
    import io

    geojson = req.geojson
    if not geojson or not geojson.get("features"):
        raise HTTPException(status_code=400, detail="无识别结果可下载")

    try:
        import geopandas as gpd

        # 转 GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(geojson["features"], crs="EPSG:4326")

        # 写入临时目录
        tmp_dir = tempfile.mkdtemp(prefix="sam_shp_")
        shp_path = os.path.join(tmp_dir, "sam_result.shp")
        gdf.to_file(shp_path)

        # 打包 ZIP
        zip_path = os.path.join(tmp_dir, "sam_result.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
                fpath = shp_path.replace('.shp', ext)
                if os.path.exists(fpath):
                    zf.write(fpath, os.path.basename(fpath))

        # 读取 ZIP 返回
        with open(zip_path, 'rb') as f:
            content = f.read()

        # 清理
        shutil.rmtree(tmp_dir, ignore_errors=True)

        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="sam_result.zip"'}
        )
    except Exception as e:
        logger.exception(f"SHP download error: {e}")
        raise HTTPException(status_code=500, detail=f"打包下载失败: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "8006")
    uvicorn.run(app, host="0.0.0.0", port=_port)
