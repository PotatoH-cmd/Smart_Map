"""Intent Service 独立进程入口（PM2 管理，127.0.0.1:8010）。

用法：python services/intent_service_main.py
依赖 env：DASHSCOPE_API_KEY（或 INTENT_LLM_API_KEY）、TOOL_HUB_URL（默认 127.0.0.1:8011）
"""
import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 加载 backend/.env（DASHSCOPE_API_KEY 等）
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND / ".env")
except Exception:
    pass

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "core.intent.service:app",
        host=os.environ.get("INTENT_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("INTENT_SERVICE_PORT", "8010")),
        log_level="info",
    )
