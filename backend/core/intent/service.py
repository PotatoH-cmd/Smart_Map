"""Intent Service FastAPI 壳（独立进程入口，默认 127.0.0.1:8010）。

LLM 配置（env，均可选）：
- INTENT_LLM_MODEL（默认 qwen-flash-2025-07-28）
- INTENT_LLM_BASE_URL（默认 DashScope compatible-mode）
- DASHSCOPE_API_KEY / INTENT_LLM_API_KEY
依赖：TOOL_HUB_URL（默认 http://127.0.0.1:8011）
"""
import logging
import os

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("intent-service")

from .analyzer import GenericIntentAnalyzer  # noqa: E402
from .catalog import Catalog  # noqa: E402
from .models import AnalyzeRequest  # noqa: E402

app = FastAPI(title="Intent Service", version="1.0.0")

TOOL_HUB_URL = os.environ.get("TOOL_HUB_URL", "http://127.0.0.1:8011")

LLM_CFG = {
    "model": os.environ.get("INTENT_LLM_MODEL", "qwen-flash-2025-07-28"),
    "model_server": os.environ.get(
        "INTENT_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "api_key": os.environ.get("INTENT_LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", ""),
}

catalog = Catalog(TOOL_HUB_URL)
analyzer = GenericIntentAnalyzer(LLM_CFG, catalog)


@app.get("/health")
def health():
    return {"status": "ok", "tool_hub": TOOL_HUB_URL,
            "intents": len(catalog.intents), "tools": len(catalog.tools)}


@app.post("/v1/intent/analyze")
def analyze(req: AnalyzeRequest):
    result = analyzer.analyze(
        req.message,
        history=req.history,
        view=req.context.view,
        extra=req.context.extra,
    )
    return result.model_dump()


@app.get("/v1/intents")
def intents():
    catalog.refresh()
    return {"success": True,
            "data": {"intents": catalog.intents,
                     "tools": [{"name": t.get("name"), "intents": t.get("intents"),
                                "keywords": t.get("keywords"), "priority": t.get("priority")}
                               for t in catalog.tools]},
            "message": ""}
