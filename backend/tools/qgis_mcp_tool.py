"""
QGIS MCP 工具 —— 通过 MCP streamable-http 协议连接 QGIS 引擎。

协议流程：
1. POST /mcp → initialize（获取 session ID）
2. POST /mcp → notifications/initialized（确认就绪）
3. POST /mcp → tools/call（调用工具，带 session header）

环境变量：
- QGIS_MCP_SERVER_URI: MCP Server 地址，默认 http://localhost:8036
- QGIS_MCP_TIMEOUT: 超时秒数，默认 60
"""
import json
import logging
import os
from typing import Dict, Any, List, Optional, Union

import httpx
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────

DEFAULT_SERVER_URI = "http://localhost:8036"
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 2  # 会话过期时最大重试次数

# compound 模式下 27 个工具类别，供 LLM 选择
COMPOUND_CATEGORIES = [
    "system",        # ping, diagnose, get_qgis_info, get_raster_info
    "project",       # 工程管理：load/save/create/get_info/set_crs
    "layer",         # 图层操作：add_vector/add_raster/remove/export/zoom_to...
    "layer_property",# 图层属性：set_visibility/crs/extent/active...
    "layer_tree",    # 图层树：get_tree/create_group/move/duplicate/order
    "features",      # 要素操作：add/update/delete/select/get_selection...
    "field",         # 字段操作：add/delete/rename/calculator
    "selection",     # 选择集：clear/get
    "style",         # 样式：set_layer_style/set_raster_style/qml...
    "canvas",        # 画布：get_extent/set_extent/scale/screenshot
    "render",        # 渲染：render_map/get_canvas_screenshot
    "processing",    # 处理算法：execute_processing/list_algorithms...
    "analysis",      # 空间分析：raster_calculator/zonal_statistics/spatial_join
    "query",         # 查询：execute_sql/evaluate_expression/identify_features
    "active_layer",  # 当前图层
    "transform",     # 坐标转换：transform_coordinates
    "code",          # 代码执行：execute_code
    "batch",         # 批量命令：batch_commands
    "plugins",       # 插件管理
    "variables",     # 项目变量
    "settings",      # QGIS 设置
    "expression",    # 表达式校验
    "message_log",   # 消息日志
    "bookmarks",     # 书签
    "map_themes",    # 地图主题
]


@register_tool('qgis_mcp_tool')
class QGISMcpTool(BaseTool):
    """
    QGIS 空间分析与处理工具。

    连接 QGIS 引擎，支持 27 类操作：缓冲区、裁剪、相交、空间关联、
    分区统计、坐标系转换、处理算法执行、地图渲染、数据导入导出等。

    环境变量：
    - QGIS_MCP_SERVER_URI: MCP 服务器地址（默认 http://localhost:8036）
    """

    description = (
        "QGIS 空间分析与处理工具。通过 MCP 协议连接 QGIS 引擎，提供专业 GIS 操作能力。\n"
        "推荐用法：直接用自然语言描述操作，系统自动匹配最佳算法。\n"
        "例如：'对郝楼砂场做200米缓冲区'、'计算各采区面积'、'裁剪河道红线内要素'\n"
        "高级用法：手动指定 category/action/params 精确控制\n"
        "常用类别：layer(图层), processing(算法), analysis(空间分析), query(查询), render(渲染), transform(坐标转换)\n"
        "缓冲区示例：{category:'processing', action:'execute', params:{algorithm:'native:buffer', parameters:{INPUT:layer_id, DISTANCE:100}}}"
    )

    parameters = [
        {
            "name": "category",
            "type": "string",
            "description": (
                "工具类别，共 27 类。常用：system(连接检测/Ping)、"
                "project(工程管理：新建/保存/加载/查看信息)、"
                "layer(图层操作：加载矢量/栅格/Web图层、移除、导出、缩放到图层)、"
                "features(要素操作：增删改查、选择、统计)、"
                "style(样式：单值/分类/渐变色、栅格伪彩/灰度/山体阴影)、"
                "processing(处理算法：执行、列出、帮助)、"
                "analysis(空间分析：栅格计算器、分区统计、空间关联)、"
                "render(渲染：地图截图、画布截图)、"
                "query(查询：SQL、表达式、点选识别)、"
                "transform(坐标转换：坐标系互转)。"
                "完整列表：" + ", ".join(COMPOUND_CATEGORIES)
            ),
            "enum": COMPOUND_CATEGORIES,
            "required": True,
        },
        {
            "name": "action",
            "type": "string",
            "description": (
                "具体操作名称，由所选 category 决定。"
                "常用示例：system/ping, layer/add_vector, layer/export, "
                "features/get, features/add, processing/execute(缓冲区native:buffer), "
                "analysis/spatial_join, analysis/zonal_statistics, "
                "render/render_map, transform/coordinates, query/sql"
            ),
            "required": True,
        },
        {
            "name": "params",
            "type": "object",
            "description": "操作参数，JSON 对象格式。例如 {path:'/data/road.shp'} 或 {distance:100, unit:'meters'}",
            "required": False,
        },
        {
            "name": "mcpServer",
            "type": "string",
            "description": (
                "QGIS MCP Server 地址，如 http://172.16.1.1:8036。"
                "留空则取环境变量 QGIS_MCP_SERVER_URI，其默认值为 http://localhost:8036"
            ),
            "required": False,
        },
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self._session_id: Optional[str] = None
        self._client: Optional[httpx.Client] = None
        self._retry_count = 0

    # ------------------------------------------------------------------
    # 会话管理（initialize → initialized）
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        """获取持久 HTTP 客户端（复用连接）。"""
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._client

    def _close_client(self):
        """关闭 HTTP 客户端，释放连接。"""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _server_uri(self, params: Dict[str, Any]) -> str:
        """解析 MCP Server 地址。"""
        return params.get("mcpServer") or os.environ.get(
            "QGIS_MCP_SERVER_URI", DEFAULT_SERVER_URI
        )

    def _send_jsonrpc(self, server_uri: str, method: str, params: dict) -> dict:
        """发送 JSON-RPC 2.0 请求到 MCP Server，返回解析后的响应体。"""
        client = self._get_client()
        payload = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        try:
            resp = client.post(f"{server_uri}/mcp", json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.ConnectError:
            raise ConnectionError(f"无法连接 QGIS MCP Server ({server_uri})，请确认 Docker 容器已启动")
        except httpx.TimeoutException:
            raise TimeoutError(f"QGIS MCP Server ({server_uri}) 请求超时")

        # 解析 SSE 流响应 (text/event-stream)
        body = resp.text
        return self._parse_sse(body)

    def _parse_sse(self, body: str) -> dict:
        """解析 SSE (Server-Sent Events) 格式的响应体。

        MCP streamable-http 响应格式：
        event: message
        data: {"jsonrpc":"2.0","result":{...},"id":1}
        """
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("data:"):
                data_str = stripped[5:].strip()
                try:
                    return json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning(f"[qgis_mcp] JSON decode failed for data line: {data_str[:200]}")
                    continue

        # 回退：尝试把整个 body 当纯 JSON 解析（非 SSE 响应）
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.error(f"[qgis_mcp] 无法解析 MCP 响应: {body[:500]}")
            return {"error": {"code": -1, "message": f"无法解析响应: {body[:200]}"}}

    def _ensure_session(self, server_uri: str):
        """确保 MCP 会话已建立。

        标准 MCP streamable-http 握手流程：
        1. initialize → 服务器返回 capabilities + session ID（在响应头）
        2. notifications/initialized → 客户端告知已就绪
        """
        if self._session_id and self._retry_count < MAX_RETRIES:
            return

        logger.info(f"[qgis_mcp] 建立 MCP 会话: {server_uri}")
        client = self._get_client()

        # Step 1: initialize
        try:
            resp = client.post(
                f"{server_uri}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "map-assistant-v1",
                            "version": "1.0.0",
                        },
                    },
                    "id": 1,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            resp.raise_for_status()
        except Exception as e:
            raise ConnectionError(f"MCP 握手失败 (initialize): {e}")

        # 提取 Session ID
        self._session_id = resp.headers.get("Mcp-Session-Id", "")
        if not self._session_id:
            logger.warning("[qgis_mcp] 服务器未返回 Mcp-Session-Id，尝试无会话模式")
        else:
            logger.info(f"[qgis_mcp] Session ID: {self._session_id[:12]}...")

        # Step 2: 发送 initialized 通知
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            client.post(
                f"{server_uri}/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            self._retry_count = 0
            logger.info("[qgis_mcp] MCP 会话就绪")
        except Exception as e:
            logger.error(f"[qgis_mcp] initialized 通知失败: {e}")

    def _reset_session(self):
        """重置会话（用于出错后重连）。"""
        self._session_id = None
        self._retry_count = 0
        self._close_client()

    # ------------------------------------------------------------------
    # qwen_agent BaseTool 调用入口
    # ------------------------------------------------------------------

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """qwen_agent 标准调用入口。

        Args:
            params: JSON 字符串或字典，包含 category、action、params、mcpServer

        Returns:
            {"success": bool, "data": ..., "message": str}
        """
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return self._error("参数不是有效的 JSON 字符串")

        category = params.get("category", "").strip()
        action = params.get("action", "").strip()
        tool_params = params.get("params") or {}
        server_uri = self._server_uri(params)

        if not category:
            return self._error("缺少 category 参数，请选择工具类别")
        if not action:
            # 如果只传了 category 没有 action，尝试 ping
            if category == "system":
                action = "ping"
            else:
                return self._error(f"缺少 action 参数（category={category} 下需要指定具体操作）")

        # 构建 MCP 工具名：compound 模式下是 {category}，参数里带 action
        mcp_tool_name = category

        for attempt in range(MAX_RETRIES + 1):
            try:
                self._ensure_session(server_uri)

                result = self._send_jsonrpc(
                    server_uri,
                    "tools/call",
                    {
                        "name": mcp_tool_name,
                        "arguments": {
                            "action": action,
                            "params": tool_params,
                        },
                    },
                )

                # 检查 JSON-RPC error
                if "error" in result:
                    err = result["error"]
                    err_msg = err.get("message", str(err))
                    logger.error(f"[qgis_mcp] MCP error: {err_msg}")
                    return self._error(f"QGIS 处理失败: {err_msg}")

                # 成功
                data = result.get("result", {})
                return self._success(data, f"{category}/{action} 执行成功")

            except (ConnectionError, TimeoutError) as e:
                logger.warning(f"[qgis_mcp] 连接失败 (attempt {attempt + 1}): {e}")
                self._reset_session()
                if attempt >= MAX_RETRIES:
                    return self._error(str(e))
                # 短暂等待后重试
                import time
                time.sleep(1)

            except Exception as e:
                logger.error(f"[qgis_mcp] 未知错误: {e}", exc_info=True)
                return self._error(str(e))

        return self._error("重连次数超过上限")

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def ping(self, server_uri: str = None) -> bool:
        """快速检测 QGIS MCP 连接是否可用。"""
        uri = server_uri or DEFAULT_SERVER_URI
        try:
            result = self.call({
                "category": "system",
                "action": "ping",
                "mcpServer": uri,
            })
            data = result.get("data", {})
            # ping 成功返回 {"pong": true} 或类似结构
            if result.get("success") and isinstance(data, dict):
                if data.get("pong", True):
                    return True
                # compound 模式的 ping 可能直接返回文本
                content = data.get("content", [])
                if content and isinstance(content, list):
                    for c in content:
                        if c.get("type") == "text" and "pong" in str(c.get("text", "")).lower():
                            return True
            return result.get("success", False)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _success(data: Any, message: str = "操作成功") -> Dict[str, Any]:
        return {"success": True, "data": data, "message": message}

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        return {"success": False, "data": None, "message": message}
