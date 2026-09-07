"""Native Provider：本地 Python 工具（backend/tools/*.py）。

发现机制：
1. 自动扫描 tools/ 目录下 *_tool.py 模块（import 副作用触发 qwen_agent @register_tool），
   从 qwen_agent 全局 TOOL_REGISTRY 读取 注册名 → 工具类；
2. 特殊构造（需 cfg 的工具）优先使用工厂函数（迁移自 agents/tool_registry.py）；
3. tools.yaml `native` 段按注册名覆盖/补充元数据（intents/keywords/constraint/...）。

目录元数据只读类属性，不实例化工具；实例在 invoke 时按需创建并缓存。
"""
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..models import ToolSpec

logger = logging.getLogger(__name__)

# 不参与自动扫描的模块（重依赖/同名注册冲突/非工具模块）
DEFAULT_SKIP_MODULES = {
    "ragflow_knowledge_tool",      # 与 llamaindex 版同名注册，由工厂按 env 选择
    "llamaindex_knowledge_tool",
}

# ---------------------------------------------------------------------------
# 特殊工厂（迁移自 agents/tool_registry.py，自含 import 与配置）
# ---------------------------------------------------------------------------

def _create_postgresql_tool():
    from tools.postgresql_tool import PostgreSQLTool
    return PostgreSQLTool(cfg={
        "host": "172.136.16.52",
        "port": 5432,
        "database": "postgres",
        "user": "postgres",
    })


def _create_mcp_postgresql_tool():
    from tools.mcp_postgres_tool import MCPPostgreSQLTool
    return MCPPostgreSQLTool(cfg={"readonly": True})


def _create_knowledge_base_tool():
    _kb = __import__("os").environ.get("KNOWLEDGE_BACKEND", "ragflow")
    if _kb == "llamaindex":
        from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    else:
        from tools.ragflow_knowledge_tool import KnowledgeBaseTool
    return KnowledgeBaseTool()


FACTORIES: Dict[str, Callable[[], Any]] = {
    "postgresql_tool": _create_postgresql_tool,
    "mcp_postgres_tool": _create_mcp_postgresql_tool,
    "knowledge_base_tool": _create_knowledge_base_tool,
}

# 自动扫描时额外跳过的注册名（由工厂提供实例化路径的保持一致）
SKIP_ON_SCAN = set(DEFAULT_SKIP_MODULES)


def scan_tool_classes(skip_modules: Optional[set] = None) -> Dict[str, type]:
    """import tools/*_tool.py 模块，返回 注册名 → 工具类（仅本项目模块注册的）。

    以 import 前后 TOOL_REGISTRY 的差集为准，避免混入 qwen_agent 内置工具。
    失败模块跳过并告警。
    """
    import importlib
    import pkgutil

    skip = set(skip_modules or set()) | SKIP_ON_SCAN
    registry: Dict[str, type] = {}
    try:
        import tools as tools_pkg
    except Exception as e:
        logger.error(f"[toolhub] Cannot import tools package: {e}")
        return registry

    from qwen_agent.tools.base import TOOL_REGISTRY
    before = set(TOOL_REGISTRY.keys())

    for m in pkgutil.iter_modules(tools_pkg.__path__):
        if not (m.name.endswith("_tool") or m.name == "map_tool"):
            continue
        if m.name in skip:
            continue
        try:
            importlib.import_module(f"tools.{m.name}")
        except Exception as e:
            logger.warning(f"[toolhub] Skip native module tools.{m.name}: {e}")
            continue

    ours = {name: cls for name, cls in dict(TOOL_REGISTRY).items() if name not in before}

    # knowledge_base_tool 由工厂按 env 选择实现（跳过列表内模块），单独补注册
    try:
        _kb = __import__("os").environ.get("KNOWLEDGE_BACKEND", "ragflow")
        mod = "tools.llamaindex_knowledge_tool" if _kb == "llamaindex" else "tools.ragflow_knowledge_tool"
        importlib.import_module(mod)
        for name, cls in dict(TOOL_REGISTRY).items():
            if name not in ours:
                ours[name] = cls
    except Exception as e:
        logger.warning(f"[toolhub] knowledge_base_tool class unavailable: {e}")

    registry.update(ours)
    return registry


def _class_meta(cls: type) -> Tuple[str, List[Dict[str, Any]]]:
    desc = str(getattr(cls, "description", "") or "").strip()
    raw = getattr(cls, "parameters", []) or []
    if isinstance(raw, dict):
        # 个别工具 parameters 为 JSON-Schema 风格 dict → 转列表
        if "properties" in raw:
            required = set(raw.get("required") or [])
            raw = [{"name": n, **(p if isinstance(p, dict) else {}), "required": n in required}
                   for n, p in (raw.get("properties") or {}).items()]
        else:
            raw = []
    params = [p for p in raw if isinstance(p, dict)]
    return desc, params


class NativeProvider:
    """本地工具目录构建与调用。"""

    def __init__(self):
        self._classes: Dict[str, type] = {}
        self._instances: Dict[str, Any] = {}
        self.scan()

    # ------------------------------------------------------------------
    def scan(self, skip_modules: Optional[set] = None) -> None:
        self._classes = scan_tool_classes(skip_modules)

    def names(self) -> List[str]:
        return sorted(self._classes.keys())

    def build_spec(self, name: str, override: Dict[str, Any]) -> Optional[ToolSpec]:
        cls = self._classes.get(name)
        if cls is None:
            return None
        desc, params = _class_meta(cls)
        spec = ToolSpec(
            name=name,
            provider="native",
            description=override.get("description") or desc,
            parameters=override.get("parameters") or params,
        )
        for field in ("intents", "keywords", "constraint", "excludes"):
            if field in override:
                setattr(spec, field, override[field])
        if "priority" in override:
            spec.priority = int(override["priority"])
        if "view_bound" in override:
            spec.view_bound = bool(override["view_bound"])
        if "enabled" in override:
            spec.enabled = bool(override["enabled"])
        return spec

    # ------------------------------------------------------------------
    def _instantiate(self, name: str):
        if name in self._instances:
            return self._instances[name]
        factory = FACTORIES.get(name)
        try:
            instance = factory() if factory else self._classes[name]()
        except Exception as e:
            logger.error(f"[toolhub] Instantiate native tool {name} failed: {e}")
            raise
        self._instances[name] = instance
        return instance

    def call(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        instance = self._instantiate(name)
        result = instance.call(params)
        return normalize_result(result)


def normalize_result(result: Any) -> Dict[str, Any]:
    """把工具返回归一化为 {success, data, message, error}。"""
    import json as _json

    if isinstance(result, str):
        try:
            result = _json.loads(result)
        except Exception:
            return {"success": True, "data": result, "message": "", "error": ""}
    if not isinstance(result, dict):
        return {"success": True, "data": result, "message": "", "error": ""}
    out = {
        "success": bool(result.get("success", True)),
        "data": result.get("data"),
        "message": str(result.get("message", "") or ""),
        "error": str(result.get("error", "") or ""),
    }
    # 保留工具的扩展字段（如 weather 的 current/forecasts、map 的 commands）
    for k, v in result.items():
        if k not in out:
            out[k] = v
    return out
