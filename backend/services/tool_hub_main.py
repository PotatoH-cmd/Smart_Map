"""Tool Hub 独立进程入口（PM2 管理，127.0.0.1:8011）。

用法：python services/tool_hub_main.py
"""
import os
import sys
from pathlib import Path

# 保证以任意 cwd 启动时都能 import backend 下的包（core/tools/...）
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 加载 backend/.env（DASHSCOPE_API_KEY 等，工具降级链需要）
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND / ".env")
except Exception:
    pass

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "core.toolhub.service:app",
        host=os.environ.get("TOOL_HUB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TOOL_HUB_PORT", "8011")),
        log_level="info",
    )
