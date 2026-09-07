"""tools.yaml / skills.yaml 加载器。

config 目录解析顺序：
1. 环境变量 TOOLHUB_CONFIG_DIR
2. backend/config/（默认）
"""
import os
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .models import MCPServerSpec, HTTPToolSpec, SkillSpec

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    d = os.environ.get("TOOLHUB_CONFIG_DIR")
    return Path(d) if d else BACKEND_DIR / "config"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"[toolhub] Failed to load {path}: {e}")
        return {}


def load_tools_yaml() -> Dict[str, Any]:
    """返回 {native: {name: meta}, mcp_servers: [...], http_tools: [...]}。"""
    data = _read_yaml(config_dir() / "tools.yaml")
    return {
        "native": data.get("native") or {},
        "mcp_servers": data.get("mcp_servers") or [],
        "http_tools": data.get("http_tools") or [],
    }


def load_skills_yaml() -> List[Dict[str, Any]]:
    data = _read_yaml(config_dir() / "skills.yaml")
    return data.get("skills") or []


def parse_native_meta(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    """native 元数据条目 → ToolSpec 覆盖字段。"""
    meta = raw.get(name)
    return meta if isinstance(meta, dict) else {}


def build_mcp_server(raw: Dict[str, Any]) -> MCPServerSpec:
    # 支持 ${VAR} 与 ${VAR:-默认值} 展开（如 url: "${MCP_POSTGRES_URI:-http://localhost:8009/mcp}"）
    raw["url"] = _expand_env(str(raw.get("url", "")))
    return MCPServerSpec(**raw)


_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    def _sub(m):
        val = os.environ.get(m.group(1))
        return val if val not in (None, "") else (m.group(2) or "")
    return _ENV_RE.sub(_sub, text)


def build_http_tool(raw: Dict[str, Any]) -> HTTPToolSpec:
    return HTTPToolSpec(**raw)


def build_skill(raw: Dict[str, Any]) -> SkillSpec:
    return SkillSpec(**raw)
