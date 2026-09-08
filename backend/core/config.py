"""集中环境配置（P1.5）— os.environ 读取的单一来源。

此前 env 读取散落在 main.py / services / routers 各处；本模块以分域 dataclass
集中声明默认值与读取点。main.py 仍 re-export 原常量名，routers 的
`from main import ...` 引用方式保持不变。

约定：
- 新增环境变量 → 在此定义 + .env.template 登记，禁止在业务代码直接 os.environ.get
- 默认值只写「开发机回退值」，生产值一律走 backend/.env
"""
import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class ServerConfig:
    """FastAPI 服务。"""
    port: int = int(os.environ.get("PORT") or os.environ.get("APP_PORT") or "8006")


@dataclass(frozen=True)
class DbConfig:
    """SQLite 会话库。"""
    path: str = os.environ.get(
        "MAPASSIST_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db"),
    )


@dataclass(frozen=True)
class FalconConfig:
    """Falcon 遥感推理服务。"""
    service_url: str = os.environ.get("FALCON_SERVICE_URL", "http://127.0.0.1:8765").rstrip("/")
    python_bin: str = os.environ.get("FALCON_PYTHON_BIN", "/home/szgczx/miniconda3/envs/mapagent6/bin/python")


@dataclass(frozen=True)
class PostgisConfig:
    """GeoServer PostGIS 连接（环境变量见 .env GEOSERVER_PG_*）。"""
    host: str = os.environ.get("GEOSERVER_PG_HOST", "172.136.16.52")
    port: int = int(os.environ.get("GEOSERVER_PG_PORT", "5432"))
    dbname: str = os.environ.get("GEOSERVER_PG_DB", "postgres")
    user: str = os.environ.get("GEOSERVER_PG_USER", "postgres")
    password: str = os.environ.get("GEOSERVER_PG_PASSWORD", "")

    def as_dict(self) -> Dict[str, str]:
        return {
            "host": self.host, "port": self.port, "dbname": self.dbname,
            "user": self.user, "password": self.password,
        }


@dataclass(frozen=True)
class KeysConfig:
    """第三方服务密钥（P0 收敛：源码禁止明文，一律经 backend/.env 注入）。

    约定：无生产默认值（secret 不在代码出现）；缺失时显式为空字符串，
    由调用方在运行时报错，杜绝「默认值即生产密钥」的泄露模式。
    """
    dashscope_api_key: str = os.environ.get("DASHSCOPE_API_KEY", "")
    ragflow_api_key: str = os.environ.get("RAGFLOW_API_KEY", "")
    ragflow_api_base: str = os.environ.get("RAGFLOW_API_BASE", "")
    ragflow_dataset_id: str = os.environ.get("RAGFLOW_DATASET_ID", "")
    dify_knowledge_api_key: str = os.environ.get("DIFY_KNOWLEDGE_API_KEY", "")
    dify_api_base: str = os.environ.get("DIFY_API_BASE", "")
    dify_dataset_id: str = os.environ.get("DIFY_DATASET_ID", "")
    tianditu_token: str = os.environ.get("TIANDITU_TOKEN", "")
    weather_api_key: str = os.environ.get("WEATHER_API_KEY", "")


server = ServerConfig()
db = DbConfig()
falcon = FalconConfig()
postgis = PostgisConfig()
keys = KeysConfig()
