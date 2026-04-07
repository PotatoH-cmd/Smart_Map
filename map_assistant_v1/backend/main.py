"""FastAPI backend for Map Assistant"""
import os
import json
import logging
import traceback
import re
import base64

# 在导入 torch 或 transformer 之前检查显卡设置
cuda_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "Not Set")
logging.info(f"Current CUDA_VISIBLE_DEVICES: {cuda_devices}")
from typing import List, Dict, Any, Optional
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, Response
import httpx
from pydantic import BaseModel
from decimal import Decimal
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
from tools.knowledge_base_tool import KnowledgeBaseTool
from tools.data_visualizer_tool import DataVisualizerTool
from tools.report_generator_tool import ReportGeneratorTool
from tools.weather_tool import WeatherTool
from tools.cesium_tool import CesiumTool  # Cesium 3D 地图工具

from agents import TaskExecutor
from cesium_bridge_server import cesium_ws_endpoint, get_cesium_client_count

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global task_executor, bot
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)

# 注册 Cesium WebSocket 端点
app.add_api_websocket_route("/ws/cesium", cesium_ws_endpoint)

bot = None
task_executor = None
LLM_CFG = None

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    active_view: str = 'map'  # 'map'(2D) | 'cesium'(3D) | 'kb'

class ScreenshotRequest(BaseModel):
    image_data: str
    file_name: Optional[str] = None

class KnowledgeItem(BaseModel):
    title: str
    content: str
    tags: List[str] = []

class ChatResponse(BaseModel):
    response: str
    messages: List[Dict[str, Any]]
    map_commands: List[Dict[str, Any]] = []
    cesium_commands: List[Dict[str, Any]] = []
    charts: List[Dict[str, Any]] = []
    intent_info: Optional[Dict[str, Any]] = None


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

**警告：严禁将“实测高程”称为“实测深度”！两者物理意义相反！**

1. 字段物理意义：
   - Measured_Depth：代表“实测高程”（海拔高度）。
   - Control_Elevation：代表“控制高程”（准许挖掘的最低海拔）。

2. 判定逻辑：
   - 整体超深度开采定义：整个砂场的（实测高程-控制高程）测深点的 **平均差值超过 2米**，即定义为超深度开采。
     即：AVG(Control_Elevation - Measured_Depth) > 2。
   - 否则：视为整体合规（未构成超深度开采）。
   - 注意：如果平均差值未超过 2米，即使存在个别点位差值较大，也不能将该砂场定性为“超深度开采”。

3. 潘庄砂场专项修正：
   - 实测高程 (~30.5m) 远高于 控制高程 (17.1m)。
   - **结论：潘庄砂场完全不超深！** 它是未挖到控制深度，属于安全/合规状态。

4. 强制回答规范：
   - 若 平均差值 > 2m，回答格式：“{区域}存在超深度开采。平均实测高程比控制高程低 {diff}m，超过 2m 允许范围。”
   - 否则，回答格式：“{区域}未构成超深度开采。整体平均实测高程符合控制要求（平均偏差在 2m 以内）。”

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

“种子场可采区数据已成功加载到地图。”
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
            raise HTTPException(status_code=500, detail=res.get('error', '数据库查询失败'))
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
    return {"url": url, "filename": safe_name}


# ==============================
# 原有聊天接口（保持不变）
# ==============================
@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        if not request.messages:
            raise HTTPException(status_code=400, detail="消息不能为空")
        
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
                    "1. 术语规范：统一使用“实测高程”代替“实测深度”。\n"
                    "2. 业务定义：\n"
                    "   - Measured_Depth（实测高程）：实地测量的海拔高度。\n"
                    "   - Control_Elevation（控制高程）：红线标准（最低许可海拔）。\n"
                    "3. 判定原则：\n"
                    "   - 整个砂场（或区域）的“实测高程-控制高程”测深点的【平均差值】超过 2米，才定义为【超深度开采】。\n"
                    "   - 只有当 AVG(Control_Elevation - Measured_Depth) > 2 时，才判定整个区域违规。\n"
                    "   - 若平均差值 <= 2米，即使有个别点位超深，也判定为【未构成超深度开采】。"
                )
                logger.info(f"Injected elevation business rules.")
            # 地图数据加载意图守卫：根据 active_view 决定使用 2D 还是 3D 工具
            map_intent_keywords = ["加载", "上图", "矢量", "图层", "地图", "位置", "显示到地图", "加载到地图", "跳转", "定位", "标记", "标点", "经纬度"]
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
                            "当用户要求跳转或查找位置时，先调用 location_search 查找坐标，"
                            "再调用 cesium_tool(action='flyTo', lat=..., lng=..., height=100000) 执行飞行动画。"
                            "当用户要求添加标注时，使用 cesium_tool(action='addMarker')。"
                            "当用户要求加载矢量图层时，先查询数据库再用 cesium_tool(action='addGeoJsonLayer')。"
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
                            "当用户要求跳转或查找位置时，必须优先使用 location_search 查找坐标，"
                            "然后通过 map_tool(action='set_view') 进行跳转。"
                            "请生成 map_tool(action='load_vector_layer', table_name='ceshen', filter=...) 的加载命令，"
                            "其中字段名必须加双引号。"
                            "若用户给出了完整可采区/砂场名称（包含“可采区”或“砂场”），filter 必须使用等値匹配："
                            "\\\"Mineable_Area_Name\\\"='\u5b8c整名称'，禁止用 LIKE。"
                        )
                    })
                    logger.info("2D view detected: using map_tool for map operations.")

        use_intent_agent = os.environ.get("USE_INTENT_AGENT", "false").lower() == "true"

        if use_intent_agent and task_executor is not None:
            result = await task_executor.execute(
                user_message=messages[-1]['content'],
                chat_history=messages[:-1]
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

            return ChatResponse(
                response=result.get("response", "命令已执行。"),
                messages=result.get("messages", []),
                map_commands=result.get("map_commands", []),
                cesium_commands=result.get("cesium_commands", []),
                charts=result.get("charts", []),
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
        
        logger.info(f"Chat response: {response_text[:100]}... Map commands: {len(map_commands)}, Cesium commands: {len(cesium_commands)}")
        if map_commands:
            logger.info(f"First map command: {map_commands[0]}")
        if cesium_commands:
            logger.info(f"First cesium command: {cesium_commands[0]}")
        
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
async def chat_stream(request: ChatRequest):
    try:
        messages = [msg.dict() for msg in request.messages]
        def generate():
            for resp in bot.run(messages=messages):
                yield f"data: {json.dumps({'response': resp})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/proxy/gf-tiles/{z}/{y}/{x}")
async def proxy_gf_tiles(z: int, y: int, x: int):
    https_base = "https://123.149.20.94:60805/server/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile"
    http_base = "http://123.149.20.94:60805/server/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile"
    url_https = f"{https_base}/{z}/{y}/{x}"
    url_http = f"{http_base}/{z}/{y}/{x}"
    headers = {
        "User-Agent": "MapAssistant/1.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.get(url_https, headers=headers)
        content_type = r.headers.get("Content-Type", "image/png")
        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "public, max-age=600",
            "Content-Type": content_type,
        }
        return Response(content=r.content, headers=resp_headers, status_code=r.status_code)
    except Exception as e_https:
        logger.warning(f"HTTPS 上游访问失败，回退到 HTTP: {e_https}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url_http, headers=headers)
            content_type = r.headers.get("Content-Type", "image/png")
            resp_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Cache-Control": "public, max-age=600",
                "Content-Type": content_type,
            }
            return Response(content=r.content, headers=resp_headers, status_code=r.status_code)
        except Exception as e_http:
            logger.error(f"HTTP 上游也访问失败: {e_http}")
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
# ✅ 新增：知识库管理接口
# ==============================
@app.get("/api/knowledge")
async def list_knowledge():
    try:
        kb_tool = KnowledgeBaseTool()
        # 由于 Dify 不支持直接 list_all (可能有大量数据)，这里调用 list_topics 或者返回文档列表
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

@app.post("/api/knowledge")
async def add_knowledge(item: KnowledgeItem):
    try:
        kb_tool = KnowledgeBaseTool()
        result = kb_tool.call({
            'operation': 'add',
            'title': item.title,
            'content': item.content
        })
        if not result.get('success'):
            raise HTTPException(status_code=500, detail=result.get('error'))
        return result
    except Exception as e:
        logger.error(f"Add knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/knowledge/{kb_id}")
async def delete_knowledge(kb_id: str):
    # 注意：Dify 的文档 ID 通常是字符串 (UUID)
    try:
        # 目前 KnowledgeBaseTool 尚未实现 delete 操作，如果需要可以后续添加
        # 暂时返回不支持，或根据需要扩展工具类
        return {"success": False, "error": "Dify 模式下暂不支持通过此接口删除，请前往 Dify 控制台管理"}
    except Exception as e:
        logger.error(f"Delete knowledge error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def clean_response_content(content: str) -> str:
    if not content:
        return content
    content = re.sub(r'<\w+_tool[^>]*>', '', content)
    content = re.sub(r'\n\s*\n+', '\n\n', content)
    return content.strip()

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

if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "8006")
    uvicorn.run(app, host="0.0.0.0", port=_port)
