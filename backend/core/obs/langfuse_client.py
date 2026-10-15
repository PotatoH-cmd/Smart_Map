"""
langfuse_client.py — LangFuse SDK 单例封装（v4，OTel 底座）。

对外提供进程级薄封装，全部 try/except 保护：
  obs_trace(run_id, session_id, user_message)          run 开始（创建 AGENT trace）
  obs_trace_update(run_id, output, status, metadata)   run 终态（补 output 后 end）
  obs_span(run_id, name, input, output, level, ...)    工具调用（TOOL span，自动计时）
  obs_generation(run_id, name, model, prompt, ...)     LLM 调用（GENERATION）
  obs_get_trace_id(run_id) / obs_trace_url(run_id)     深链支持
  flush() / shutdown()                                 生命周期

env：LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST（backend/.env）
"""
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_client = None                 # Langfuse | None（惰性单例）
_client_inited = False         # 初始化只尝试一次
_traces: Dict[str, Any] = {}   # run_id -> trace span 对象（进程内活跃 run）
_trace_ids: Dict[str, str] = {}  # run_id -> langfuse trace_id（终态后仍可查，用于深链）
_warned: set = set()           # 已 warning 过的错误类型（去重降噪）
_lock = threading.Lock()

_TRUNC = 4000  # input/output 截断长度，防止超大 payload 拖慢上报


def _warn_once(key: str, exc: Exception) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(f"[obs] LangFuse 上报失败（{key}，同类错误不再重复提示）: {exc}")


def _trunc(value: Any, limit: int = _TRUNC) -> Any:
    """截断超长字符串/嵌套结构中的字符串，避免超大 payload。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...(truncated {len(value) - limit} chars)"
    if isinstance(value, dict):
        return {k: _trunc(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trunc(v, limit) for v in value]
    return value


def get_client():
    """惰性初始化 Langfuse 单例；未配置 env 时返回 None（no-op 降级）。"""
    global _client, _client_inited
    if _client_inited:
        return _client
    with _lock:
        if _client_inited:
            return _client
        _client_inited = True
        try:
            import os
            if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
                logger.info("[obs] LANGFUSE_* 未配置，可观测性上报已禁用（功能不受影响）")
                return None
            from langfuse import get_client as _lf_get_client
            _client = _lf_get_client()
            if not _client.auth_check():
                _client = None
                _warn_once("auth", Exception("auth_check 失败，请检查 LANGFUSE_* 配置"))
        except Exception as exc:  # SDK 缺失 / 网络不可达
            _warn_once("init", exc)
            _client = None
    return _client


def is_enabled() -> bool:
    return get_client() is not None


# ----------------------------------------------------------------------
# trace（run 级）
# ----------------------------------------------------------------------

def obs_trace(run_id: str, session_id: str = "", user_message: str = "") -> None:
    """run 开始：创建 AGENT 类型 root observation（幂等，resume 续接不重复建）。"""
    lf = get_client()
    if lf is None or not run_id:
        return
    if run_id in _traces:
        return
    try:
        trace = lf.start_observation(
            name="agent_run",
            as_type="agent",
            input=_trunc(user_message),
            metadata={"run_id": run_id, "session_id": session_id},
        )
        with _lock:
            _traces[run_id] = trace
            _trace_ids[run_id] = trace.trace_id
    except Exception as exc:
        _warn_once("trace", exc)


def obs_trace_update(run_id: str, output: Any = None, status: str = "",
                     metadata: Optional[Dict] = None) -> None:
    """run 终态：补 output/metadata 后 end（completed/failed/cancelled）。"""
    trace = _traces.get(run_id)
    if trace is None:
        return
    try:
        kwargs: Dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = _trunc(output)
        if metadata:
            kwargs["metadata"] = metadata
        if status == "failed":
            kwargs["level"] = "ERROR"
        trace.update(**kwargs)
        trace.end()
    except Exception as exc:
        _warn_once("trace_update", exc)
    finally:
        with _lock:
            _traces.pop(run_id, None)


# ----------------------------------------------------------------------
# span（工具调用级，OTel 自动计时）
# ----------------------------------------------------------------------

def obs_span(run_id: str, name: str, input: Any = None, output: Any = None,
             level: str = "DEFAULT", status_message: str = "",
             elapsed_ms: Optional[int] = None) -> None:
    """工具调用：TOOL span。工具执行完毕后回放创建（start/end_time 回填真实耗时）。

    elapsed_ms：工具真实耗时（_invoke_step 计时）。span 的 start_time 取当前时间，
    end_time = start + elapsed_ms，LangFuse waterfall 中显示真实执行区间。
    """
    trace = _traces.get(run_id)
    if trace is None:
        return
    try:
        span = trace.start_observation(name=name, as_type="tool", input=_trunc(input))
        end_kw: Dict[str, Any] = {}
        if isinstance(elapsed_ms, (int, float)) and elapsed_ms >= 0:
            import time as _time
            # end_time 单位为纳秒（OTel 语义）：当前时刻 + 真实耗时
            end_ns = int(_time.time_ns()) + int(elapsed_ms) * 1_000_000
            end_kw["end_time"] = end_ns
        span.update(
            output=_trunc(output) if output is not None else None,
            level=level,
            **({"status_message": status_message} if status_message else {}),
        )
        span.end(**end_kw)
    except Exception as exc:
        _warn_once("span", exc)


# ----------------------------------------------------------------------
# generation（LLM 调用级）
# ----------------------------------------------------------------------

def obs_generation(run_id: str, name: str, model: str, prompt: Any = None,
                   completion: Any = None, usage: Optional[Dict[str, int]] = None,
                   model_parameters: Optional[Dict] = None) -> None:
    """LLM 调用：GENERATION observation，记录 prompt/completion/模型/token。"""
    trace = _traces.get(run_id)
    if trace is None:
        return
    try:
        gen = trace.start_observation(
            name=name, as_type="generation", model=model,
            input=_trunc(prompt),
            **({"model_parameters": model_parameters} if model_parameters else {}),
        )
        gen.update(
            output=_trunc(completion) if completion is not None else None,
            **({"usage_details": usage} if usage else {}),
        )
        gen.end()
    except Exception as exc:
        _warn_once("generation", exc)


# ----------------------------------------------------------------------
# 深链支持
# ----------------------------------------------------------------------

def obs_get_trace_id(run_id: str) -> str:
    """返回 run 对应的 LangFuse trace_id（未上报/未启用返回空串）。"""
    with _lock:
        return _trace_ids.get(run_id, "")


def obs_trace_url(run_id: str) -> str:
    """返回 LangFuse UI trace 深链（未启用返回空串）。"""
    trace_id = obs_get_trace_id(run_id)
    lf = get_client()
    if not trace_id or lf is None:
        return ""
    try:
        return lf.get_trace_url(trace_id=trace_id)
    except Exception:
        return ""


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------

def flush() -> None:
    """阻塞式冲刷上报队列（FastAPI shutdown 调用）。"""
    lf = get_client()
    if lf is None:
        return
    try:
        lf.flush()
    except Exception as exc:
        _warn_once("flush", exc)


def shutdown() -> None:
    """flush + 关闭后台线程。"""
    lf = get_client()
    if lf is None:
        return
    try:
        lf.shutdown()
    except Exception as exc:
        _warn_once("shutdown", exc)
