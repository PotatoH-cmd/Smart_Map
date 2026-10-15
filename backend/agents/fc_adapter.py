"""fc_adapter.py — 原生 tool_call ⇄ 内部 TaskStep 转换层（Phase 2）。

设计原则：只替换"请求的产生方式"，不替换执行内核。
- 模型输出：原生 tool_calls（OpenAI 协议：id / name / arguments）
- 系统内部：TaskStep（step_id 语义 —— 幂等键 / checkpoint / SSE 事件均依赖）
- 映射：tool_call.id → TaskStep.tool_call_id（等价映射，不替换 step_id）
- 回传：ordered_results → role:"tool" 消息（按 tool_call_id 对应），供多轮 FC（Phase 3）

兼容两种 tool_call 形态：
1. LangChain AIMessage.tool_calls（dict：{"name", "args", "id"}）
2. OpenAI 原始形态（{"id", "function": {"name", "arguments"(JSON 字符串)}}）
"""
import json
import logging
from typing import Any, Dict, List, Optional

from .intent_types import TaskStep
from core.toolhub.param_validate import params_to_json_schema

logger = logging.getLogger(__name__)

_MAX_TOOL_DESC = 1024
_MAX_TOOL_MSG_CHARS = 4000


def build_openai_tools(metas: Any) -> List[Dict[str, Any]]:
    """工具元数据（ToolSpec / dict{name,description,parameters}）→ OpenAI tools 定义。"""
    tools: List[Dict[str, Any]] = []
    for m in metas or []:
        if isinstance(m, dict):
            name = m.get("name")
            desc = m.get("description") or ""
            params = m.get("parameters")
        else:
            name = getattr(m, "name", None)
            desc = getattr(m, "description", "") or ""
            params = getattr(m, "parameters", None)
        if not name:
            continue
        schema = m.json_schema() if hasattr(m, "json_schema") else params_to_json_schema(params)
        tools.append({
            "type": "function",
            "function": {
                "name": str(name),
                "description": str(desc)[:_MAX_TOOL_DESC],
                "parameters": schema,
            },
        })
    return tools


def _extract_tool_call(tc: Any):
    """兼容 LangChain dict / OpenAI 原始形态 / 对象属性，返回 (name, args, call_id)。"""
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name")
        call_id = tc.get("id") or tc.get("tool_call_id")
        args = tc.get("args")
        if args is None:
            raw = fn.get("arguments")
            if isinstance(raw, str) and raw.strip():
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"[fc_adapter] tool_call arguments 非法 JSON: {raw[:120]!r}")
                    args = {}
            else:
                args = {}
    else:
        name = getattr(tc, "name", None)
        call_id = getattr(tc, "id", None)
        args = getattr(tc, "args", None)
        if args is None:
            fn = getattr(tc, "function", None)
            name = name or getattr(fn, "name", None)
            raw = getattr(fn, "arguments", None)
            try:
                args = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
            except json.JSONDecodeError:
                args = {}
    return (str(name) if name else "",
            args if isinstance(args, dict) else {},
            (str(call_id) if call_id else None))


def tool_calls_to_steps(tool_calls: Any, start_id: int = 1) -> List[TaskStep]:
    """原生 tool_calls → TaskStep 列表（保持模型给出的调用顺序；并行=同一消息内多个调用）。

    - step_id 从 start_id 递增；tool_call_id 原样落到 TaskStep（等价映射）
    - 参数 JSON 解析失败降级：params={}，由缺参检测 / schema 校验兜底报错
    """
    steps: List[TaskStep] = []
    for tc in (tool_calls or []):
        name, args, call_id = _extract_tool_call(tc)
        if not name:
            continue
        steps.append(TaskStep(
            step_id=start_id + len(steps),
            action=f"调用工具 {name}",
            tool=name,
            params=args,
            reasoning="模型原生 tool_call 决策",
            expected_output="",
            tool_call_id=call_id,
        ))
    return steps


def results_to_tool_messages(ordered_results: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """执行结果 → role:"tool" 消息（按 tool_call_id 对应；无 id 的条目跳过）。

    入参条目形态：{"step_id", "tool_name", "tool_call_id"?, "result": {...}}。
    content 为紧凑 JSON（success/message/error/data，截断防爆 prompt）。
    """
    messages: List[Dict[str, str]] = []
    for item in ordered_results or []:
        if not isinstance(item, dict):
            continue
        call_id = item.get("tool_call_id")
        if not call_id:
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            content = json.dumps({"success": None, "message": str(result)[:200]}, ensure_ascii=False)
        else:
            payload: Dict[str, Any] = {"success": result.get("success")}
            msg = result.get("message") or result.get("content") or ""
            if msg:
                payload["message"] = str(msg)[:1500]
            if result.get("error"):
                payload["error"] = str(result["error"])[:1500]
            data = result.get("data")
            if data is not None:
                payload["data"] = data
            try:
                content = json.dumps(payload, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                content = json.dumps({"success": payload.get("success")}, ensure_ascii=False)
            content = content[:_MAX_TOOL_MSG_CHARS]
        messages.append({"role": "tool", "tool_call_id": str(call_id), "content": content})
    return messages
