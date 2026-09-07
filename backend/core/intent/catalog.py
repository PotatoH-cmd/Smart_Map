"""目录客户端：从工具中台拉取工具目录 + 本地意图目录，带 TTL 缓存。

意图目录 = config/intents.yaml（意图 key → 描述）。
工具目录 = {TOOL_HUB_URL}/v1/tools（enabled only）。
"""
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
import yaml

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]
CACHE_TTL = float(os.environ.get("INTENT_CATALOG_TTL", "60"))


def _load_intents_yaml() -> Dict[str, Any]:
    path = Path(os.environ.get("INTENTS_YAML", BACKEND_DIR / "config" / "intents.yaml"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[intent] load {path} failed: {e}")
        return {}


class Catalog:
    """意图 + 工具目录缓存。线程安全。"""

    def __init__(self, tool_hub_url: str):
        self.tool_hub_url = tool_hub_url.rstrip("/")
        self._lock = threading.Lock()
        self._fetched_at: float = 0.0
        self.intents: Dict[str, str] = {}           # intent key → 描述
        self.tools: List[Dict[str, Any]] = []       # enabled ToolSpec dicts
        self.last_error: str = ""

    # ------------------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._fetched_at < CACHE_TTL and self.tools:
                return
            yaml_intents = _load_intents_yaml()
            intents: Dict[str, str] = {}
            raw = yaml_intents.get("intents") or {}
            for key, val in raw.items():
                if isinstance(val, dict):
                    if not val.get("enabled", True):
                        continue
                    intents[key] = str(val.get("description", ""))
                else:
                    intents[str(key)] = str(val)
            tools = self._fetch_tools()
            self.intents = intents
            self.tools = tools
            self._fetched_at = time.time()
            logger.info(f"[intent] catalog refreshed: {len(intents)} intents, {len(tools)} tools")

    def _fetch_tools(self) -> List[Dict[str, Any]]:
        if not self.tool_hub_url:
            return []
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{self.tool_hub_url}/v1/tools", params={"include_disabled": False})
                resp.raise_for_status()
                body = resp.json()
            return body.get("data") or []
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"[intent] fetch tool catalog failed: {e}")
            return []

    # ------------------------------------------------------------------
    # 派生视图（分析器消费）
    # ------------------------------------------------------------------
    def intent_list_text(self) -> str:
        return "\n".join(f"- {k}: {v}" for k, v in self.intents.items())

    def tool_intent_text(self) -> str:
        lines = []
        for t in self.tools:
            if t.get("intents"):
                lines.append(f"- {t['name']}: {', '.join(t['intents'])}")
        return "\n".join(lines)

    def keyword_routes(self) -> List[Dict[str, Any]]:
        """关键词路由表（priority 升序）。"""
        routes = [t for t in self.tools if t.get("keywords")]
        return sorted(routes, key=lambda t: int(t.get("priority", 100)))
