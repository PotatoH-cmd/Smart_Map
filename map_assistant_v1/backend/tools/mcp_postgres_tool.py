"""
MCP PostgreSQL Client - 通过 MCP 协议连接 PostgreSQL
支持 HTTP 传输连接远程 MCP Server
"""
import json
import logging
import os
import httpx
from typing import Dict, List, Any, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool('mcp_postgres_tool')
class MCPPostgreSQLTool(BaseTool):
    """
    MCP PostgreSQL 工具 - 通过 MCP HTTP 协议连接 PostgreSQL 数据库
    保持与原有 PostgreSQLTool 完全相同的接口，方便切换
    """

    description = '''
        MCP PostgreSQL 数据库操作工具，通过 MCP 协议连接数据库。
        支持操作：query, execute, get_db_schema, list_tables
        连接方式：设置环境变量 MCP_POSTGRES_URI 或 mcpServer 参数
        例如：MCP_POSTGRES_URI=http://172.136.16.52:8000/mcp
    '''

    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '数据库操作类型',
            'enum': ['query', 'execute', 'get_db_schema', 'list_tables'],
            'required': True
        },
        {
            'name': 'sql',
            'type': 'string',
            'description': '要执行的SQL语句（仅 query 或 execute 时需要）',
            'required': False
        },
        {
            'name': 'params',
            'type': 'array',
            'description': 'SQL参数列表，用于防止SQL注入',
            'required': False
        },
        {
            'name': 'mcpServer',
            'type': 'string',
            'description': 'MCP Server URL（可选，默认使用环境变量 MCP_POSTGRES_URI）',
            'required': False
        }
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.timeout = 30.0
        self._schema_cache = None

    def _get_server_uri(self, params: Dict[str, Any]) -> str:
        """获取 MCP Server URI"""
        return params.get('mcpServer') or os.environ.get(
            'MCP_POSTGRES_URI',
            'http://localhost:8009/mcp'
        )

    def _build_jsonrpc_request(self, method: str, params: Dict[str, Any], request_id: int = 1) -> Dict:
        """构建 JSON-RPC 2.0 请求"""
        return {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id
        }

    def _call_mcp_tool(self, server_uri: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """通过 HTTP 调用 MCP Tool"""
        try:
            jsonrpc_request = self._build_jsonrpc_request(
                method="tools/call",
                params={
                    "name": tool_name,
                    "arguments": arguments
                }
            )

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    server_uri,
                    json=jsonrpc_request,
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                result = response.json()

                if "error" in result:
                    return {
                        'success': False,
                        'error': result['error'].get('message', str(result['error'])),
                        'data': None
                    }

                return result.get("result", {})

        except httpx.ConnectError:
            return {
                'success': False,
                'error': f'无法连接到 MCP Server: {server_uri}',
                'data': None
            }
        except httpx.TimeoutException:
            return {
                'success': False,
                'error': 'MCP Server 请求超时',
                'data': None
            }
        except Exception as e:
            logger.error(f"MCP call failed: {e}")
            return {
                'success': False,
                'error': str(e),
                'data': None
            }

    def _list_mcp_tools(self, server_uri: str) -> List[str]:
        """列出 MCP Server 支持的工具"""
        try:
            jsonrpc_request = self._build_jsonrpc_request(method="tools/list", params={})

            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    server_uri,
                    json=jsonrpc_request,
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                result = response.json()

                if "result" in result and "tools" in result["result"]:
                    return [t["name"] for t in result["result"]["tools"]]
                return []

        except Exception as e:
            logger.error(f"List tools failed: {e}")
            return []

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """同步调用入口 - 适配 qwen_agent 框架"""
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {'success': False, 'error': 'Invalid JSON params', 'data': None}

        server_uri = self._get_server_uri(params)
        operation = params.get('operation')

        if operation == 'query':
            sql = params.get('sql', '').strip()
            if not sql.upper().startswith('SELECT'):
                return {'success': False, 'error': 'query 操作仅允许 SELECT 语句', 'data': None}
            return self._query(server_uri, sql, params.get('params', []))

        elif operation == 'execute':
            sql = params.get('sql', '').strip()
            if sql.upper().startswith('SELECT'):
                return {'success': False, 'error': 'execute 操作不支持 SELECT 语句', 'data': None}
            return self._execute(server_uri, sql, params.get('params', []))

        elif operation == 'get_db_schema':
            return self._get_db_schema(server_uri)

        elif operation == 'list_tables':
            return self._list_tables(server_uri)

        else:
            return {'success': False, 'error': f'Unsupported operation: {operation}'}

    def _query(self, server_uri: str, sql: str, params: List[Any]) -> Dict[str, Any]:
        """执行查询"""
        result = self._call_mcp_tool(server_uri, "query", {"sql": sql, "params": params})

        if result.get('success') is False:
            return result

        try:
            data = result.get('data', [])
            if isinstance(data, str):
                data = json.loads(data)

            MAX_ROWS = 20
            preview = data[:MAX_ROWS] if isinstance(data, list) else data
            message = f'查询成功，返回 {len(data)} 条记录。'
            if len(data) > MAX_ROWS:
                message += f' 仅展示前 {MAX_ROWS} 条。'

            return {
                'success': True,
                'data': preview,
                'message': message
            }
        except (json.JSONDecodeError, TypeError):
            return {
                'success': True,
                'data': result.get('data', []),
                'message': '查询成功'
            }

    def _execute(self, server_uri: str, sql: str, params: List[Any]) -> Dict[str, Any]:
        """执行写入"""
        return self._call_mcp_tool(server_uri, "execute", {"sql": sql, "params": params})

    def _get_db_schema(self, server_uri: str) -> Dict[str, Any]:
        """获取数据库 Schema"""
        result = self._call_mcp_tool(server_uri, "get_schema", {})

        if result.get('success') is False:
            return result

        return {
            'success': True,
            'data': {'schema': result.get('data', {})},
            'message': 'Schema retrieved'
        }

    def _list_tables(self, server_uri: str) -> Dict[str, Any]:
        """列出所有表"""
        result = self._call_mcp_tool(server_uri, "list_tables", {})

        if result.get('success') is False:
            return result

        tables = result.get('data', [])
        if isinstance(tables, str):
            try:
                tables = json.loads(tables)
            except json.JSONDecodeError:
                tables = [t.strip() for t in tables.split('\n') if t.strip()]

        return {
            'success': True,
            'data': {'tables': tables},
            'message': f'Found {len(tables)} tables'
        }