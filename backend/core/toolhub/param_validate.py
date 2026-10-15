"""工具参数 Schema 校验（Phase 1：schema 一等公民）。

职责边界（全部为纯函数，可独立单测）：
- parameters（qwen_agent 风格列表 / JSON-Schema 风格 dict）→ 标准 JSON Schema
- 参数确定性修复（coerce）：数值字符串→数字、标量→数组、None 剔除，不做意图猜测
- 参数校验（validate）：required / type / enum 为 blocking（enforce 模式下拒绝执行）；
  未声明参数仅 warning（透传给工具，不阻断）

环境变量：
- PARAM_VALIDATE_MODE: off | warn | enforce（默认 warn 观察期，稳定后切 enforce）
  - off     → 跳过修复与校验
  - warn    → 只记录日志（附 _verification 报告），不阻断执行
  - enforce → blocking 级错误直接返回失败结果，不调用工具
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"

_TYPE_ALIASES = {
    "str": "string", "int": "integer", "float": "number", "bool": "boolean",
    "list": "array", "dict": "object",
}
_VALID_TYPES = {"string", "number", "integer", "boolean", "array", "object"}

_INT_RE = re.compile(r"^-?\d+$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

# 反问用户无意义的"机器参数"：由编排层（_normalize_tool_params）兜底填充或由工具语义决定，
# 缺失属执行错误（由 schema 校验报错），不适合走 pending-反问用户流程。
PROMPT_SKIP_PARAMS = frozenset({
    "operation", "sql", "params", "action", "category", "type", "variables", "tool",
})

_CODE_LABELS = {
    "required": "必填参数缺失",
    "type": "参数类型错误",
    "enum": "参数取值非法",
    "unknown": "未声明参数",
}


def validation_mode() -> str:
    """读取校验模式（调用时求值，便于测试 monkeypatch 环境变量）。"""
    mode = os.environ.get("PARAM_VALIDATE_MODE", MODE_WARN).strip().lower()
    return mode if mode in (MODE_OFF, MODE_WARN, MODE_ENFORCE) else MODE_WARN


# ---------------------------------------------------------------------------
# 转换：parameters → JSON Schema
# ---------------------------------------------------------------------------

def normalize_parameters(raw: Any) -> List[Dict[str, Any]]:
    """宽松归一化 parameters：支持 list（qwen_agent 风格）与 JSON-Schema 风格 dict。"""
    if isinstance(raw, dict):
        props = raw.get("properties") or {}
        if not isinstance(props, dict) or not props:
            return []
        required = set(raw.get("required") or [])
        return [
            {"name": n, **(p if isinstance(p, dict) else {}), "required": n in required}
            for n, p in props.items()
        ]
    if isinstance(raw, (list, tuple)):
        return [dict(p) for p in raw if isinstance(p, dict) and p.get("name")]
    return []


def params_to_json_schema(parameters: Any) -> Dict[str, Any]:
    """parameters → 标准 JSON Schema（参数校验与 tools= 注入共用同一转换来源）。"""
    if isinstance(parameters, dict) and "properties" in parameters:
        return parameters  # 已是 JSON Schema，原样返回
    props: Dict[str, Any] = {}
    required: List[str] = []
    for p in normalize_parameters(parameters):
        t = str(p.get("type") or "string").strip().lower()
        t = _TYPE_ALIASES.get(t, t)
        if t not in _VALID_TYPES:
            t = "string"
        node: Dict[str, Any] = {"type": t}
        if p.get("description"):
            node["description"] = str(p["description"])
        if p.get("enum"):
            node["enum"] = list(p["enum"])
        if t == "array":
            node["items"] = {}
        props[p["name"]] = node
        if p.get("required"):
            required.append(p["name"])
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# 修复：coerce
# ---------------------------------------------------------------------------

def _coerce_value(v: Any, t: Optional[str]) -> Any:
    try:
        if t in ("number", "integer") and not isinstance(v, bool):
            if isinstance(v, str):
                s = v.strip()
                if _INT_RE.match(s):
                    return int(s)
                if _NUM_RE.match(s):
                    return float(s)
            elif t == "integer" and isinstance(v, float) and v.is_integer():
                return int(v)
        elif t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        elif t == "array" and not isinstance(v, (list, dict)):
            return [v]
        elif t == "boolean" and isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
    except (TypeError, ValueError):
        pass
    return v


def coerce_params(params: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
    """确定性修复参数：只做语义不变的转换，无法修复的保持原样交给 validate 报错。"""
    if not isinstance(params, dict):
        return {}
    props = schema.get("properties") or {}
    out: Dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue  # None 等价于未提供（必填校验会接手）
        prop = props.get(k) if isinstance(props, dict) else None
        if isinstance(prop, dict):
            v = _coerce_value(v, prop.get("type"))
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# 校验：validate
# ---------------------------------------------------------------------------

def _translate_error(err: Any) -> Dict[str, Any]:
    """jsonschema 错误 → 结构化中文问题条目。"""
    if err.validator == "required":
        m = re.search(r"'([^']+)'", err.message or "")
        name = m.group(1) if m else (err.message or "required")
        return {"param": name, "code": "required", "level": "error",
                "message": f"缺少必填参数 {name}"}
    path = ".".join(str(p) for p in err.absolute_path) or "(根)"
    if err.validator == "type":
        return {"param": path, "code": "type", "level": "error",
                "message": f"参数 {path} 类型应为 {err.validator_value}（实际 {type(err.instance).__name__}）"}
    if err.validator == "enum":
        return {"param": path, "code": "enum", "level": "error",
                "message": f"参数 {path} 取值非法，允许：{err.validator_value}"}
    return {"param": path, "code": err.validator or "invalid", "level": "error",
            "message": f"参数 {path} 校验失败：{err.message}"}


def validate_params(params: Dict[str, Any], schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """校验参数，返回问题列表（level: error=blocking / warning=仅提示）。"""
    issues: List[Dict[str, Any]] = []
    props = schema.get("properties") or {}
    for k in params:
        if props and k not in props:
            issues.append({"param": k, "code": "unknown", "level": "warning",
                           "message": f"未知参数 {k}（工具未声明，将透传）"})
    try:
        from jsonschema import Draft7Validator
        validator = Draft7Validator(schema)
        errors = sorted(validator.iter_errors(params),
                        key=lambda e: (list(e.absolute_path), e.message or ""))
        issues.extend(_translate_error(e) for e in errors)
    except Exception as e:
        # schema 本身非法不阻断执行（配置问题记日志）
        logger.warning(f"[param_validate] schema 非法，跳过校验: {e}")
    return issues


def validate_against(parameters: Any, params: Any, *, tool_name: str = "") -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """组合入口：coerce → validate。工具未声明 parameters 时原样返回（跳过校验）。"""
    declared = normalize_parameters(parameters)
    if not declared:
        return (params if isinstance(params, dict) else {}), []
    schema = params_to_json_schema(declared)
    coerced = coerce_params(params, schema)
    issues = validate_params(coerced, schema)
    if tool_name and issues:
        logger.debug(f"[param_validate] {tool_name} issues: {issues}")
    return coerced, issues


# ---------------------------------------------------------------------------
# 消费辅助：阻断判定 / 报错文案 / 验证报告 / 缺参反问
# ---------------------------------------------------------------------------

def blocking_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """blocking（error 级）问题子集。"""
    return [i for i in issues if i.get("level") == "error"]


def format_issues(issues: List[Dict[str, Any]], limit: int = 3) -> str:
    """问题列表 → 单行错误文案（最多展示 limit 条）。"""
    msgs = [str(i.get("message") or i.get("code") or "参数错误") for i in issues]
    head = "；".join(msgs[:limit])
    return head + (f"（共 {len(msgs)} 条）" if len(msgs) > limit else "")


def verification_report(tool_name: str, issues: List[Dict[str, Any]],
                        *, phase: str = "param_schema") -> Dict[str, Any]:
    """问题列表 → 与 rules_gateway.VerificationReport.to_dict 同构的报告（事件流可复用渲染）。"""
    checks = [{
        "code": i.get("code", "invalid"),
        "name": _CODE_LABELS.get(i.get("code", ""), "参数校验"),
        "passed": False,
        "detail": i.get("message", ""),
        "severity": "critical" if i.get("level") == "error" else "warning",
    } for i in issues]
    blocking = blocking_issues(issues)
    return {
        "tool": tool_name,
        "phase": phase,
        "passed": not checks,
        "risk": "critical" if blocking else ("warning" if checks else "none"),
        "checks": checks,
        "errors": [i.get("message", "") for i in blocking],
        "details": [i.get("message", "") for i in issues],
    }


_LOCAL_CLASSES: Optional[Dict[str, Any]] = None


def _local_tool_class(tool_name: str):
    """进程内 native 工具类（缓存；扫描失败返回 None，调用方静默降级）。

    注意：scan_tool_classes 已做幂等处理（重复调用不丢目录）。
    """
    global _LOCAL_CLASSES
    if _LOCAL_CLASSES is None:
        try:
            from .providers.native import scan_tool_classes
            _LOCAL_CLASSES = scan_tool_classes()
        except Exception as e:
            logger.warning(f"[param_validate] native 工具类扫描失败，schema 功能降级: {e}")
            _LOCAL_CLASSES = {}
    return _LOCAL_CLASSES.get(tool_name)


def local_tool_parameters(tool_name: str) -> List[Dict[str, Any]]:
    """进程内工具声明的 parameters（类属性直读，不实例化工具）。未知工具返回 []。"""
    cls = _local_tool_class(tool_name)
    if cls is None:
        return []
    return normalize_parameters(getattr(cls, "parameters", []) or [])


def local_tool_meta(tool_name: str) -> Dict[str, Any]:
    """进程内工具元数据 {name, description, parameters}（供 FC tools= 注入）。未知返回 {}。"""
    cls = _local_tool_class(tool_name)
    if cls is None:
        return {}
    return {
        "name": tool_name,
        "description": str(getattr(cls, "description", "") or "").strip(),
        "parameters": normalize_parameters(getattr(cls, "parameters", []) or []),
    }


def render_parameters_text(parameters: Any, max_params: int = 12,
                           max_enum: int = 8) -> str:
    """parameters → 单行制文本摘要（供 prompt 按需注入；enum 超长截断，校验仍用全量 Schema）。"""
    lines: List[str] = []
    for p in normalize_parameters(parameters)[:max_params]:
        req = "必填" if p.get("required") else "可选"
        seg = f"- {p['name']}: {str(p.get('type') or 'string')}（{req}）"
        if p.get("enum"):
            vals = [str(x) for x in p["enum"]]
            shown = "|".join(vals[:max_enum]) + ("|…" if len(vals) > max_enum else "")
            seg += f" 取值: {shown}"
        if p.get("description"):
            seg += f" 说明: {str(p['description']).strip()[:80]}"
        lines.append(seg)
    return "\n".join(lines)


def missing_required_for_prompt(declared: List[Dict[str, Any]], params: Any, *,
                                skip: frozenset = PROMPT_SKIP_PARAMS) -> List[Dict[str, str]]:
    """schema.required 基线：从声明中找出缺失且适合反问用户的必填参数。

    返回 [{"param", "label"}]；机器参数（PROMPT_SKIP_PARAMS）不参与反问，
    其缺失由执行前 schema 校验（enforce）负责报错。
    """
    out: List[Dict[str, str]] = []
    params = params if isinstance(params, dict) else {}
    for p in declared or []:
        name = p.get("name")
        if not name or not p.get("required") or name in skip:
            continue
        if name not in params or params.get(name) in (None, ""):
            label = str(p.get("description") or name).strip()[:60] or name
            out.append({"param": name, "label": label})
    return out
