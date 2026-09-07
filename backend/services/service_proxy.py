"""主服务反向代理：前端控制台流量 → tool-hub(8011) / intent-service(8010)。

前端一律走同源 /api/*，避免跨域；两个下游服务 bind 127.0.0.1 不对外暴露。
env：TOOL_HUB_URL / INTENT_SERVICE_URL（与 task_executor 的服务化开关共用）。
"""
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

TOOL_HUB_URL = os.environ.get("TOOL_HUB_URL", "http://127.0.0.1:8011").rstrip("/")
INTENT_SERVICE_URL = os.environ.get("INTENT_SERVICE_URL", "http://127.0.0.1:8010").rstrip("/")

# invoke/报告类调用可能耗时较长
_LONG_TIMEOUT = 600.0
_SHORT_TIMEOUT = 15.0


def _forward(base: str, path: str, method: str = "GET", json_body=None,
             params=None, timeout: float = _SHORT_TIMEOUT) -> Response:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, f"{base}{path}", json=json_body, params=params)
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"下游服务不可用: {base}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"下游服务超时: {base}")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


# ---------------------------------------------------------------------------
# Tool Hub（8011）
# ---------------------------------------------------------------------------

@router.get("/toolhub/tools")
def toolhub_tools():
    return _forward(TOOL_HUB_URL, "/v1/tools", params={"include_disabled": "true"})


@router.get("/toolhub/tools/{name}")
def toolhub_tool_detail(name: str):
    return _forward(TOOL_HUB_URL, f"/v1/tools/{name}")


@router.patch("/toolhub/tools/{name}")
async def toolhub_toggle(name: str, request: Request):
    body = await request.json()
    return _forward(TOOL_HUB_URL, f"/v1/tools/{name}", method="PATCH", json_body=body)


@router.post("/toolhub/tools/reload")
def toolhub_reload():
    return _forward(TOOL_HUB_URL, "/v1/tools/reload", method="POST", timeout=60.0)


@router.post("/toolhub/tools/{name}/invoke")
async def toolhub_invoke(name: str, request: Request):
    body = await request.json()
    return _forward(TOOL_HUB_URL, f"/v1/tools/{name}/invoke", method="POST",
                    json_body=body, timeout=_LONG_TIMEOUT)


@router.get("/toolhub/mcp/servers")
def toolhub_mcp_servers():
    return _forward(TOOL_HUB_URL, "/v1/mcp/servers")


# ---------------------------------------------------------------------------
# Intent Service（8010）
# ---------------------------------------------------------------------------

@router.post("/intent/analyze")
async def intent_analyze(request: Request):
    body = await request.json()
    return _forward(INTENT_SERVICE_URL, "/v1/intent/analyze", method="POST",
                    json_body=body, timeout=120.0)


@router.get("/intent/intents")
def intent_intents():
    return _forward(INTENT_SERVICE_URL, "/v1/intents")
