"""Skill Provider：复合技能（声明式多工具编排）。

- 对外表现为一个普通 ToolSpec（provider=skill）
- 步骤执行支持参数模板：$param、$param|默认值、$prev[i].path（引用前序步骤结果）
- 非 optional 步骤失败即终止（fail-fast）
"""
import copy
import logging
import re
from typing import Any, Dict, List

from ..models import SkillSpec

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"^\$([A-Za-z_][\w\-]*)(?:\|([^$]*))?$")
_PREV_RE = re.compile(r"^\$prev\[(\d+)\]((?:\.\w+)*)$")


class SkillProvider:
    def __init__(self, dispatcher):
        """dispatcher: registry.invoke（步骤通过它调用其他工具）。"""
        self._skills: Dict[str, SkillSpec] = {}
        self._dispatch = dispatcher

    def configure(self, skills: List[SkillSpec]) -> None:
        self._skills = {s.name: s for s in skills}

    def names(self):
        return list(self._skills.keys())

    def get(self, name: str) -> SkillSpec:
        return self._skills[name]

    # ------------------------------------------------------------------
    def call(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        skill = self._skills[name]
        results: List[Dict[str, Any]] = []
        for idx, step in enumerate(skill.steps):
            resolved = self._resolve_params(step.params, params, results)
            logger.info(f"[skill:{name}] step{idx + 1} tool={step.tool} params={resolved}")
            out = self._dispatch(step.tool, resolved)
            results.append(out)
            if not out.get("success") and not step.optional and skill.fail_fast:
                return {
                    "success": False,
                    "data": {"steps_completed": idx, "step_results": results},
                    "message": f"步骤 {idx + 1}（{step.tool}）失败：{out.get('error') or out.get('message')}",
                    "error": out.get("error") or f"step {idx + 1} failed",
                }
        return {
            "success": True,
            "data": {"skill": name, "steps": [r for r in results], "step_results": results},
            "message": f"技能 {skill.name} 已完成 {len(results)} 个步骤",
            "error": "",
        }

    # ------------------------------------------------------------------
    def _resolve_params(self, node: Any, inputs: Dict[str, Any],
                        results: List[Dict[str, Any]]) -> Any:
        if isinstance(node, dict):
            return {k: self._resolve_params(v, inputs, results) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve_params(v, inputs, results) for v in node]
        if isinstance(node, str):
            m = _REF_RE.match(node.strip())
            if m:
                key, default = m.group(1), m.group(2)
                val = inputs.get(key)
                if val is None or val == "":
                    return _coerce(default) if default is not None else val
                return _coerce(val) if isinstance(val, str) and _looks_typed(val) else val
            m = _PREV_RE.match(node.strip())
            if m:
                back, path = int(m.group(1)), m.group(2)
                idx = len(results) - back
                if 0 <= idx < len(results):
                    return _dig(results[idx], path)
                return None
            return node
        return node


def _looks_typed(s: str) -> bool:
    return re.fullmatch(r"-?\d+(\.\d+)?|true|false|null", s.strip()) is not None


def _coerce(s: str) -> Any:
    s = s.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "null":
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return s


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
