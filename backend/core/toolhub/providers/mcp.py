"""MCP Provider：通用 MCP Server 客户端。

transport 支持：
- http             JSON-RPC 2.0 over HTTP POST（与现 qgis/postgres MCP Server 一致，默认）
- stdio            mcp SDK stdio_client + ClientSession
- sse              mcp SDK sse_client + ClientSession
- streamable-http  mcp SDK streamablehttp_client + ClientSession

远端工具自动发现（tools/list），命名：mcp_{server}_{远端工具名}；
YAML `tools_meta` / `alias` 可补充元数据或映射旧注册名。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from ..models import MCPServerSpec, ToolSpec

logger = logging.getLogger(__name__)


class MCPServerStatus:
    def __init__(self, spec: MCPServerSpec):
        self.spec = spec
        self.connected: bool = False
        self.tool_count: int = 0
        self.tool_names: List[str] = []
        self.error: str = ""
        self.checked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.spec.name,
            "transport": self.spec.transport,
            "url": self.spec.url,
            "command": self.spec.command,
            "connected": self.connected,
            "tool_count": self.tool_count,
            "tool_names": self.tool_names,
            "error": self.error,
            "expose": self.spec.expose,
            "checked_at": int(self.checked_at),
        }


class MCPProvider:
    """多 MCP Server 管理：发现 + 调用 + 状态。"""

    def __init__(self):
        self._servers: Dict[str, MCPServerSpec] = {}
        self._status: Dict[str, MCPServerStatus] = {}
        # streamable-http 会话缓存：server name → {"client", "session_id"}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def configure(self, server_specs: List[MCPServerSpec]) -> None:
        self._servers = {s.name: s for s in server_specs}
        self._status = {name: MCPServerStatus(s) for name, s in self._servers.items()}
        self.connect_all()

    def servers(self) -> List[str]:
        return list(self._servers.keys())

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    def connect_all(self) -> None:
        for name in self._servers:
            try:
                self._probe(name)
            except Exception as e:
                st = self._status[name]
                st.connected = False
                st.error = str(e)

    def _probe(self, server_name: str) -> None:
        st = self._status[server_name]
        spec = self._servers[server_name]
        st.checked_at = time.time()
        try:
            tools = self._list_tools(spec)
            st.connected = True
            st.error = ""
            st.tool_names = [t.get("name", "") for t in tools]
            st.tool_count = len(st.tool_names)
        except Exception as e:
            st.connected = False
            st.error = f"{type(e).__name__}: {e}"
            st.tool_names = []
            st.tool_count = 0

    # ------------------------------------------------------------------
    # 传输层：tools/list 与 tools/call
    # ------------------------------------------------------------------
    def _list_tools(self, spec: MCPServerSpec) -> List[Dict[str, Any]]:
        if spec.transport == "http":
            return self._http_request(spec, "tools/list", {}).get("tools", [])
        if spec.transport == "streamable-http":
            return self._mcp_http_request(spec, "tools/list", {}).get("tools", [])
        return self._sdk_request(spec, "list", None)

    def call_remote(self, spec: MCPServerSpec, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if spec.transport == "http":
            res = self._http_request(spec, "tools/call",
                                     {"name": tool_name, "arguments": arguments})
            return self._normalize_call_result(res)
        if spec.transport == "streamable-http":
            res = self._mcp_http_request(spec, "tools/call",
                                         {"name": tool_name, "arguments": arguments})
            return self._normalize_call_result(res)
        res = self._sdk_request(spec, "call", (tool_name, arguments))
        return self._normalize_call_result(res)

    @staticmethod
    def _normalize_call_result(res: Dict[str, Any]) -> Dict[str, Any]:
        """MCP tools/call 结果 → {success, data, message}。

        MCP 结果形如 {content: [{type: text, text: "..."}], isError: bool}
        或既有业务字段（qgis/postgres server 直接返回 {success, data, message}）。
        """
        if "success" in res or "data" in res:
            return res
        if res.get("isError"):
            text = _extract_text(res)
            return {"success": False, "data": None, "message": "", "error": text or "MCP tool error"}
        text = _extract_text(res)
        data: Any = text
        if isinstance(text, str):
            try:
                data = json.loads(text)
            except Exception:
                data = text
        return {"success": True, "data": data, "message": "", "error": ""}

    # ---- transport: http（JSON-RPC 2.0，无会话握手，与存量简单 server 兼容） ----
    def _http_request(self, spec: MCPServerSpec, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        req = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        with httpx.Client(timeout=spec.timeout) as client:
            resp = client.post(spec.url, json=req, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            body = resp.json()
        if "error" in body:
            raise RuntimeError(f"MCP {method} error: {body['error']}")
        return body.get("result", {}) or {}

    # ---- transport: streamable-http（标准 MCP 握手：initialize → session → initialized） ----
    def _mcp_http_request(self, spec: MCPServerSpec, method: str,
                          params: Dict[str, Any]) -> Dict[str, Any]:
        sess = self._ensure_mcp_session(spec)
        req = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params:
            req["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sess.get("session_id"):
            headers["Mcp-Session-Id"] = sess["session_id"]
        resp = sess["client"].post(spec.url, json=req, headers=headers)
        if resp.status_code in (400, 404) and sess.get("session_id"):
            # 会话失效 → 重新握手重试一次
            self._reset_mcp_session(spec)
            sess = self._ensure_mcp_session(spec)
            if sess.get("session_id"):
                headers["Mcp-Session-Id"] = sess["session_id"]
            resp = sess["client"].post(spec.url, json=req, headers=headers)
        resp.raise_for_status()
        body = _parse_sse_or_json(resp.text)
        if "error" in body:
            raise RuntimeError(f"MCP {method} error: {body['error']}")
        return body.get("result", {}) or {}

    def _ensure_mcp_session(self, spec: MCPServerSpec) -> Dict[str, Any]:
        sess = self._sessions.get(spec.name)
        if sess and sess.get("session_id") is not None:
            return sess
        client = (sess or {}).get("client") or httpx.Client(timeout=max(spec.timeout, 30.0))
        resp = client.post(
            spec.url,
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tool-hub", "version": "1.0.0"},
                },
                "id": 1,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        resp.raise_for_status()
        session_id = resp.headers.get("Mcp-Session-Id") or ""
        if session_id:
            client.post(
                spec.url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Mcp-Session-Id": session_id,
                },
            )
        sess = {"client": client, "session_id": session_id or None}
        self._sessions[spec.name] = sess
        return sess

    def _reset_mcp_session(self, spec: MCPServerSpec) -> None:
        sess = self._sessions.pop(spec.name, None)
        if sess:
            try:
                sess["client"].close()
            except Exception:
                pass

    # ---- transport: mcp SDK（stdio / sse / streamable-http，临时会话） ----
    def _sdk_request(self, spec: MCPServerSpec, op: str, payload: Optional[tuple]) -> Any:
        import anyio

        async def _run():
            from mcp import ClientSession

            if spec.transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client
                sp = StdioServerParameters(command=spec.command[0],
                                           args=spec.command[1:], env=spec.env or None)
                cm = stdio_client(sp)
            elif spec.transport == "sse":
                from mcp.client.sse import sse_client
                cm = sse_client(spec.url)
            else:  # streamable-http
                from mcp.client.streamable_http import streamablehttp_client
                cm = streamablehttp_client(spec.url)

            async with cm as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if op == "list":
                        res = await session.list_tools()
                        return [{"name": t.name,
                                 "description": t.description or "",
                                 "inputSchema": t.inputSchema or {}}
                                for t in res.tools]
                    tool_name, arguments = payload
                    res = await session.call_tool(tool_name, arguments or {})
                    return {"content": [c.model_dump() for c in (res.content or [])],
                            "isError": bool(res.isError)}

        return anyio.run(_run)

    # ------------------------------------------------------------------
    # 目录构建
    # ------------------------------------------------------------------
    def build_specs(self) -> List[ToolSpec]:
        """对 expose=True 的 server，把发现的远端工具生成 ToolSpec（默认禁用，YAML 可开启）。"""
        specs: List[ToolSpec] = []
        for name, spec in self._servers.items():
            st = self._status[name]
            if not spec.expose or not st.connected:
                continue
            try:
                tools = self._list_tools(spec)
            except Exception as e:
                logger.warning(f"[toolhub] MCP {name} list_tools failed: {e}")
                continue
            for t in tools:
                rname = t.get("name", "")
                if spec.include and rname not in spec.include:
                    continue
                if rname in spec.exclude:
                    continue
                local_name = f"mcp_{name}_{rname}"
                meta = dict(spec.tools_meta.get(rname) or {})
                alias_meta = spec.alias.get(rname) or {}
                params = _schema_to_parameters(t.get("inputSchema") or {})
                specs.append(ToolSpec(
                    name=alias_meta.get("name") or meta.get("name") or local_name,
                    provider="mcp",
                    description=alias_meta.get("description") or meta.get("description")
                                or t.get("description") or "",
                    parameters=meta.get("parameters") or params,
                    intents=meta.get("intents", []),
                    keywords=meta.get("keywords", []),
                    priority=int(meta.get("priority", 100)),
                    constraint=meta.get("constraint", ""),
                    config={"server": name, "remote_tool": rname,
                            "transport": spec.transport, "url": spec.url},
                    enabled=bool(meta.get("enabled", False)),  # 自动发现的远端工具默认不启用，YAML 显式开启
                ))
        return specs

    def status(self) -> List[Dict[str, Any]]:
        return [self._status[n].to_dict() for n in self._servers]

    def get_spec_by_name(self, name: str) -> Optional[MCPServerSpec]:
        for spec in self._servers.values():
            prefix = f"mcp_{spec.name}_"
            if name.startswith(prefix):
                return spec
        return None

    def remote_tool_of(self, local_name: str) -> Optional[str]:
        for spec in self._servers.values():
            prefix = f"mcp_{spec.name}_"
            if local_name.startswith(prefix):
                return local_name[len(prefix):]
        return None


def _extract_text(res: Dict[str, Any]) -> str:
    parts = []
    for c in res.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


def _parse_sse_or_json(body: str) -> Dict[str, Any]:
    """MCP streamable-http 响应：SSE 流（event/data 行）或纯 JSON。"""
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("data:"):
            try:
                return json.loads(s[5:].strip())
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def _schema_to_parameters(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """MCP inputSchema（JSON Schema）→ qwen_agent parameters 风格列表。"""
    if not schema:
        return []
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    params = []
    for pname, p in props.items():
        p = p if isinstance(p, dict) else {}
        params.append({
            "name": pname,
            "type": p.get("type", "string"),
            "description": p.get("description", ""),
            "required": pname in required,
        })
    return params
