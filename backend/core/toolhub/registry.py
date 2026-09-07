"""ToolHub：工具中台核心 — 目录聚合 + 统一调用分发。

目录优先级：native 扫描/工厂 → tools.yaml 覆盖（intents/keywords/constraint/...）
          → mcp 自动发现 → http 声明 → skill 声明。
统一调用契约：{success, data, message, error, elapsed_ms}。
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .loader import load_skills_yaml, load_tools_yaml, build_http_tool, build_mcp_server, build_skill
from .models import ToolSpec
from .providers.http_tool import HTTPProvider
from .providers.mcp import MCPProvider
from .providers.native import NativeProvider
from .providers.skill import SkillProvider

logger = logging.getLogger(__name__)


class ToolHub:
    def __init__(self):
        self._lock = threading.RLock()
        self._specs: Dict[str, ToolSpec] = {}
        self._runtime_enabled: Dict[str, bool] = {}   # 控制台开关（内存态，YAML 为准）
        self.native = NativeProvider()
        self.mcp = MCPProvider()
        self.http = HTTPProvider()
        self.skill = SkillProvider(self._dispatch_internal)
        self.load()

    # ------------------------------------------------------------------
    # 目录构建
    # ------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            cfg = load_tools_yaml()
            specs: Dict[str, ToolSpec] = {}

            # 1. native：扫描 + YAML 元数据覆盖
            native_meta = cfg["native"]
            for name in self.native.names():
                spec = self.native.build_spec(name, native_meta.get(name) or {})
                if spec is not None:
                    specs[name] = spec

            # 2. YAML 中声明但当前未扫描到的 native 工具（如模块 import 失败）→ 占位提示
            for name, meta in native_meta.items():
                if name not in specs and name not in ("mcp_servers", "http_tools"):
                    specs[name] = ToolSpec(
                        name=name, provider="native",
                        description=meta.get("description", "") + "（模块未加载）",
                        parameters=meta.get("parameters", []),
                        intents=meta.get("intents", []), keywords=meta.get("keywords", []),
                        priority=int(meta.get("priority", 100)),
                        constraint=meta.get("constraint", ""),
                        view_bound=bool(meta.get("view_bound", False)),
                        excludes=meta.get("excludes", []),
                        enabled=bool(meta.get("enabled", False)),
                    )

            # 3. MCP servers + 自动发现
            try:
                self.mcp.configure([build_mcp_server(raw) for raw in cfg["mcp_servers"]])
                for spec in self.mcp.build_specs():
                    specs.setdefault(spec.name, spec)
            except Exception as e:
                logger.warning(f"[toolhub] MCP provider init failed: {e}")

            # 4. HTTP 工具
            self.http.configure([build_http_tool(raw) for raw in cfg["http_tools"]])
            for name in self.http.names():
                raw = next((r for r in cfg["http_tools"] if r.get("name") == name), {})
                specs.setdefault(name, ToolSpec(
                    name=name, provider="http",
                    description=raw.get("description", ""),
                    parameters=raw.get("parameters", []),
                    intents=raw.get("intents", []), keywords=raw.get("keywords", []),
                    priority=int(raw.get("priority", 100)),
                    constraint=raw.get("constraint", ""),
                    excludes=raw.get("excludes", []),
                    config={"endpoint": raw.get("endpoint", ""), "method": raw.get("method", "POST")},
                ))

            # 5. Skill 技能
            skills = [build_skill(raw) for raw in load_skills_yaml()]
            self.skill.configure(skills)
            for s in skills:
                specs[s.name] = ToolSpec(
                    name=s.name, provider="skill",
                    description=s.description, parameters=s.parameters,
                    intents=s.intents, keywords=s.keywords, priority=s.priority,
                    constraint=s.constraint or s.prompt_hint,
                    excludes=s.excludes,
                    config={"steps": [st.model_dump() for st in s.steps]},
                )

            self._specs = specs
            logger.info(f"[toolhub] catalog loaded: {len(specs)} tools "
                        f"(native={sum(1 for s in specs.values() if s.provider == 'native')}, "
                        f"mcp={sum(1 for s in specs.values() if s.provider == 'mcp')}, "
                        f"http={sum(1 for s in specs.values() if s.provider == 'http')}, "
                        f"skill={sum(1 for s in specs.values() if s.provider == 'skill')})")

    def reload(self) -> Dict[str, Any]:
        with self._lock:
            self.native.scan()
            self._runtime_enabled.clear()
            self.load()
            return {"success": True, "data": {"tool_count": len(self._specs)}, "message": "reloaded"}

    # ------------------------------------------------------------------
    # 目录查询
    # ------------------------------------------------------------------
    def catalog(self, include_disabled: bool = False) -> List[ToolSpec]:
        out = []
        for spec in self._specs.values():
            if not include_disabled and not self.is_enabled(spec.name):
                continue
            out.append(spec)
        out.sort(key=lambda s: (s.priority, s.name))
        return out

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def is_enabled(self, name: str) -> bool:
        if name in self._runtime_enabled:
            return self._runtime_enabled[name]
        spec = self._specs.get(name)
        return bool(spec.enabled) if spec else False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._specs:
            return False
        self._runtime_enabled[name] = enabled
        return True

    # ------------------------------------------------------------------
    # 统一调用
    # ------------------------------------------------------------------
    def invoke(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()
        try:
            result = self._dispatch_internal(name, params or {})
        except Exception as e:
            logger.error(f"[toolhub] invoke {name} failed: {e}", exc_info=True)
            result = {"success": False, "data": None, "message": "", "error": str(e)}
        result.setdefault("elapsed_ms", int((time.time() - start) * 1000))
        return result

    def _dispatch_internal(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            return {"success": False, "data": None, "message": "",
                    "error": f"工具未注册: {name}"}
        if not self.is_enabled(name):
            return {"success": False, "data": None, "message": "",
                    "error": f"工具 {name} 已被禁用"}

        if spec.provider == "native":
            return self.native.call(name, params)
        if spec.provider == "mcp":
            server_name = (spec.config or {}).get("server")
            remote = (spec.config or {}).get("remote_tool")
            mcp_spec = self.mcp._servers.get(server_name)
            if mcp_spec is None:
                return {"success": False, "data": None, "message": "",
                        "error": f"MCP server 不存在: {server_name}"}
            return self.mcp.call_remote(mcp_spec, remote, params)
        if spec.provider == "http":
            return self.http.call(name, params)
        if spec.provider == "skill":
            return self.skill.call(name, params)
        return {"success": False, "data": None, "message": "",
                "error": f"未知 provider: {spec.provider}"}

    # ------------------------------------------------------------------
    def mcp_status(self) -> List[Dict[str, Any]]:
        return self.mcp.status()
