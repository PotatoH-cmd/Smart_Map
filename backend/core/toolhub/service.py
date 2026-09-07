"""Tool Hub FastAPI 壳（独立进程入口，默认 127.0.0.1:8011）。

前端控制台流量经主服务 /api/toolhub/* 反向代理，不直连本服务。
"""
import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("tool-hub")

from .registry import ToolHub  # noqa: E402

app = FastAPI(title="Tool Hub", version="1.0.0")
hub = ToolHub()


class InvokeBody(BaseModel):
    params: Dict[str, Any] = {}


class EnabledBody(BaseModel):
    enabled: bool


@app.get("/health")
def health():
    return {"status": "ok", "tools": len(hub.catalog(include_disabled=True))}


@app.get("/v1/tools")
def list_tools(include_disabled: bool = True):
    return {"success": True,
            "data": [s.model_dump() for s in hub.catalog(include_disabled=include_disabled)],
            "message": ""}


@app.get("/v1/tools/{name}")
def get_tool(name: str):
    spec = hub.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    return {"success": True, "data": spec.model_dump(), "message": ""}


@app.patch("/v1/tools/{name}")
def toggle_tool(name: str, body: EnabledBody):
    if not hub.set_enabled(name, body.enabled):
        raise HTTPException(status_code=404, detail=f"tool not found: {name}")
    return {"success": True, "data": {"name": name, "enabled": body.enabled}, "message": ""}


@app.post("/v1/tools/reload")
def reload_tools():
    return hub.reload()


@app.post("/v1/tools/{name}/invoke")
def invoke_tool(name: str, body: InvokeBody):
    result = hub.invoke(name, body.params)
    return result


@app.get("/v1/mcp/servers")
def mcp_servers():
    return {"success": True, "data": hub.mcp_status(), "message": ""}
