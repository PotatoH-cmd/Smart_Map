"""Map Assistant FastAPI 入口：装配应用与全局单例，挂载分域路由。

架构分工（P0~P2 拆分 + 清理收敛后，main 只做「装配」）：
- 分域路由        → backend/routers/（chat · files · geolibre · tiles ·
                    geoserver · knowledge · memory · falcon）
- DB 会话/记忆存储 → backend/core/db.py
- Agent 装配     → backend/services/agent_builder.py（tools 副作用注册 +
                    TaskExecutor 与旧版 Qwen Assistant 构造）
- 配置单一来源    → backend/core/config.py
- 提示词单一来源  → backend/prompts.py

除 chat.py 通过 `import main as _main` 访问 lifespan 装配的单例
（task_executor / bot）外，任何模块不再反向 import main 常量。
"""
import os
import sys
import logging
import asyncio
from datetime import datetime as dt
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:  # noqa: BLE001
    pass

# 在导入 torch 或 transformer 之前检查显卡设置
cuda_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "Not Set")
logging.info(f"Current CUDA_VISIBLE_DEVICES: {cuda_devices}")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.types import Scope, Receive, Send  # noqa: E402

from core.config import server as _cfg_server  # noqa: E402
from core.db import init_db  # noqa: E402
from agents.run_store import get_run_store  # noqa: E402
from services.agent_builder import build_task_executor, build_legacy_agent  # noqa: E402
from cesium_bridge_server import cesium_ws_endpoint  # noqa: E402
from tools.gis_tool_router import router as gis_tool_router  # noqa: E402

logger = logging.getLogger(__name__)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 日志：文件（backend/backend.log，项目相对路径）+ 控制台 双 handler
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(_BACKEND_DIR, "backend.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
with open(LOG_FILE, "a") as f:
    f.write(f"\n--- Service Restart at {dt.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 特别确保 qwen_agent, tools 和 uvicorn 日志能写入文件
for name in ["qwen_agent", "qwen_agent_logger", "tools", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx"]:
    l = logging.getLogger(name)
    l.setLevel(logging.DEBUG if name in ["httpx", "qwen_agent", "qwen_agent_logger"] else logging.INFO)
    l.addHandler(file_handler)


# ---------------------------------------------------------------------------
# 中间件：SSE 端点禁用 GZip，避免流式响应被缓冲后一次性返回
# ---------------------------------------------------------------------------
class SelectiveGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/chat/stream":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# ---------------------------------------------------------------------------
# 每日清理过期 run 数据（终态且超 7 天）
# ---------------------------------------------------------------------------
async def _daily_run_cleanup():
    while True:
        await asyncio.sleep(86400)
        try:
            removed = get_run_store().cleanup(keep_days=7)
            if removed:
                logger.info(f"[run-cleanup] 已清理 {removed} 个过期 run 及其事件/检查点")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[run-cleanup] 清理失败: {e}")


# ---------------------------------------------------------------------------
# 生命周期：DB 初始化 → run 清理 → 装配 Agent 全局单例
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global task_executor, bot
    init_db()
    try:
        removed = get_run_store().cleanup(keep_days=7)
        if removed:
            logger.info(f"[lifespan] 启动清理：移除 {removed} 个过期 run")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lifespan] run 清理失败: {e}")
    app.state.run_cleanup_task = asyncio.create_task(_daily_run_cleanup())
    task_executor = build_task_executor()
    bot = build_legacy_agent()
    yield


app = FastAPI(title="Map Assistant API", version="1.0.0", lifespan=lifespan)

# 静态资源（报告/截图等，相对项目 backend/static 解析）
app.mount("/static", StaticFiles(directory=os.path.join(_BACKEND_DIR, "static")), name="static")

# CORS + 选择性 GZip
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
from services.service_proxy import router as service_proxy_router  # noqa: E402
app.include_router(service_proxy_router)

# 全局单例（lifespan 装配；chat.py 经 `import main as _main` 属性访问）
bot = None
task_executor = None


@app.get("/")
async def root():
    return {"message": "Map Assistant API is running"}


# ---------------------------------------------------------------------------
# 分域路由注册
# ---------------------------------------------------------------------------
# python main.py 直跑时模块名为 __main__，routers 内 `import main` 会二次加载
# 造成循环导入；将 __main__ 别名注册为 'main'，使 routers 引用当前模块
# （uvicorn 以 main:app 导入时本就存在该别名）。
import sys as _sys  # noqa: E402
if __name__ == "__main__":
    _sys.modules.setdefault("main", _sys.modules["__main__"])

from routers.chat import router as chat_router  # noqa: E402
from routers.files import router as files_router
from routers.geolibre import router as geolibre_router
from routers.tiles import router as tiles_router
from routers.geoserver import router as geoserver_router
from routers.knowledge import router as knowledge_router
from routers.memory import router as memory_router
from routers.falcon import router as falcon_router
from routers.runs import router as runs_router
from routers.sessions import router as sessions_router

app.include_router(chat_router)
app.include_router(files_router)
app.include_router(geolibre_router)
app.include_router(tiles_router)
app.include_router(geoserver_router)
app.include_router(knowledge_router)
app.include_router(memory_router)
app.include_router(falcon_router)
app.include_router(runs_router)
app.include_router(sessions_router)

# 模块加载时自动注册已有 3D Tiles 数据集与自定义栅格图层
from services.tile_manager import _auto_register_existing_3dtiles_async  # noqa: E402
from routers.tiles import _register_custom_raster_layers  # noqa: E402
_auto_register_existing_3dtiles_async()
_register_custom_raster_layers()


if __name__ == "__main__":
    import uvicorn
    import socket

    _port = _cfg_server.port
    # 创建带 SO_REUSEADDR 的 socket，避免 PM2 重启时端口抢占导致启动失败
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", _port))
    sock.listen(2048)
    uvicorn.run(app, fd=sock.fileno())
