"""HTTP Provider：YAML 声明的远程 HTTP/OpenAPI 工具。

响应归一化：响应体含 success 字段则透传，否则包装为 {"success": true, "data": body}。
"""
import logging
from typing import Any, Dict

import httpx

from ..models import HTTPToolSpec

logger = logging.getLogger(__name__)


class HTTPProvider:
    def __init__(self):
        self._tools: Dict[str, HTTPToolSpec] = {}

    def configure(self, specs: list) -> None:
        self._tools = {s.name: s for s in specs}

    def names(self):
        return list(self._tools.keys())

    def call(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        spec = self._tools.get(name)
        if spec is None:
            return {"success": False, "data": None, "message": "", "error": f"HTTP tool not found: {name}"}
        try:
            with httpx.Client(timeout=spec.timeout) as client:
                if spec.method == "GET":
                    resp = client.get(spec.endpoint, params=params, headers=spec.headers)
                elif spec.params_in == "query":
                    resp = client.post(spec.endpoint, params=params, headers=spec.headers)
                else:
                    resp = client.post(spec.endpoint, json=params, headers=spec.headers)
                resp.raise_for_status()
                body = resp.json() if "json" in resp.headers.get("content-type", "") else resp.text
        except httpx.HTTPStatusError as e:
            return {"success": False, "data": None, "message": "", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"success": False, "data": None, "message": "", "error": str(e)}

        if isinstance(body, dict) and "success" in body:
            return body
        return {"success": True, "data": body, "message": "", "error": ""}
