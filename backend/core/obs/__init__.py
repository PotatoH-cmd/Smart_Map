# ============================================================
# core/obs — 智能体可观测性（LangFuse 上报封装）
#
# 设计原则：零侵入降级
#   - .env 未配置 LANGFUSE_* → 所有 obs_* 调用直接 no-op
#   - 上报失败仅 warning 一次（按错误类型去重），绝不影响主流程
#   - 延续 run_engine 的 run_id 语义：一个 run = 一个 LangFuse trace
# ============================================================
from .langfuse_client import (  # noqa: F401
    get_client,
    is_enabled,
    obs_trace,
    obs_trace_update,
    obs_span,
    obs_generation,
    obs_get_trace_id,
    obs_trace_url,
    flush,
    shutdown,
)
