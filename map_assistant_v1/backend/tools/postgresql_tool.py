"""
PostgreSQL Tool for database operations
"""
import json
import logging  # ✅ 正确导入
import os
import re
from typing import Dict, List, Any, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool
from decimal import Decimal  # 在文件顶部添加
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

logger = logging.getLogger(__name__)  # ✅ 正确使用

@register_tool('postgresql_tool')
class PostgreSQLTool(BaseTool):
    """
    PostgreSQL数据库操作工具，支持连接PostgreSQL并执行查询、写入、列出表等操作。
    """

    description = '''
        PostgreSQL 数据库操作工具，支持 Text-to-SQL。
        你可以使用此工具：
        1. 执行 SQL 查询 (query)
        2. 执行 SQL 修改 (execute)
        3. 获取数据库表结构 (get_db_schema) - **在生成 SQL 前，强烈建议先调用此操作以获取最新的表结构和字段信息**。
        '''
    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '数据库操作类型',
            'enum': ['query', 'execute', 'get_db_schema', 'list_tables', 'sync_schema'],
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
        }
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.conn = None
        self._schema_cache = None
        self._connect()

    def _connect(self) -> bool:
        """连接到PostgreSQL数据库"""
        if not PG_AVAILABLE:
            logger.error("psycopg2 not installed. Please run: pip install psycopg2-binary")
            return False

        try:
            host = self.cfg.get('host', '172.136.16.52')
            port = self.cfg.get('port', 5432)
            database = self.cfg.get('database', 'postgres')
            user = self.cfg.get('user', 'postgres')
            password = self.cfg.get('password', '8720622')  # 建议通过安全方式传入

            if not password:
                logger.error("Database password not provided in config or PG_PASSWORD environment variable.")
                return False

            self.conn = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                cursor_factory=RealDictCursor
            )
            logger.info(f"Successfully connected to PostgreSQL database: {database} @ {host}:{port}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            self.conn = None
            return False

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        try:
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    return {
                        'success': False,
                        'error': '输入参数不是有效的JSON格式',
                        'data': None
                    }

            # 确保连接有效，尝试重连一次
            if self.conn is None or self.conn.closed:
                if not self._connect():
                    return {
                        'success': False,
                        'error': 'PostgreSQL连接失败，请检查数据库服务、网络或认证信息',
                        'data': None
                    }

            operation = params.get('operation')
            if operation == 'query':
                sql = params.get('sql', '').strip()
                if not sql.upper().startswith('SELECT'):
                    return {
                        'success': False,
                        'error': 'query 操作仅允许 SELECT 语句',
                        'data': None
                    }
                return self._query(sql, params.get('params', []))
            elif operation == 'execute':
                sql = params.get('sql', '').strip()
                if sql.upper().startswith('SELECT'):
                    return {
                        'success': False,
                        'error': 'execute 操作不支持 SELECT 语句，请使用 query',
                        'data': None
                    }
                return self._execute(sql, params.get('params', []))
            elif operation == 'get_db_schema':
                return self._get_db_schema()
            elif operation == 'sync_schema':
                return self._sync_schema_to_file()
            elif operation == 'list_tables':
                return self._list_tables()
            else:
                return {
                    'success': False,
                    'error': f'不支持的操作类型: {operation}。支持的操作: query, execute, get_db_schema, list_tables, sync_schema',
                    'data': None
                }

        except Exception as e:
            logger.exception("Unexpected error in PostgreSQLTool.call")
            return {
                'success': False,
                'error': '数据库操作过程中发生未知错误，请联系管理员',
                'data': None
            }

    def _load_schema_cache(self) -> Dict[str, Any]:
        if self._schema_cache is not None:
            return self._schema_cache
        self._schema_cache = {"columns": [], "case_sensitive": [], "lower_map": {}}
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(current_dir)
            json_path = os.path.join(base_dir, 'config', 'db_schema.json')
            if not os.path.exists(json_path):
                return self._schema_cache
            with open(json_path, 'r', encoding='utf-8') as f:
                schema = json.load(f)
            columns = []
            for cols in schema.values():
                for col in cols:
                    col_name = col.split(" (", 1)[0].strip()
                    if col_name:
                        columns.append(col_name)
            lower_map = {c.lower(): c for c in columns}
            case_sensitive = [c for c in columns if any(ch.isupper() for ch in c)]
            self._schema_cache = {"columns": columns, "case_sensitive": case_sensitive, "lower_map": lower_map}
        except Exception:
            self._schema_cache = {"columns": [], "case_sensitive": [], "lower_map": {}}
        return self._schema_cache

    def _auto_quote_sql(self, sql: str) -> str:
        cache = self._load_schema_cache()
        case_sensitive = cache.get("case_sensitive") or []
        if not case_sensitive:
            return sql
        parts = re.split(r"('(?:''|[^'])*')", sql)
        for i in range(0, len(parts), 2):
            segment = parts[i]
            for col in case_sensitive:
                segment = re.sub(r'(?<!")\b' + re.escape(col) + r'\b(?!")', f'"{col}"', segment)
                lower_col = col.lower()
                if lower_col != col:
                    segment = re.sub(r'(?<!")\b' + re.escape(lower_col) + r'\b(?!")', f'"{col}"', segment)
            parts[i] = segment
        return "".join(parts)

    def _query(self, sql: str, params: List[Any]) -> Dict[str, Any]:
        # 尝试执行查询，如果失败尝试重连一次
        for attempt in range(2):
            try:
                # 如果连接已关闭，先尝试重连
                if self.conn is None or self.conn.closed:
                    self._connect()
                
                if self.conn:
                    with self.conn.cursor() as cur:
                        sql = self._auto_quote_sql(sql)
                        cur.execute(sql, params)
                        
                        MAX_ROWS_FOR_AGENT = 20
                        rows_all = cur.fetchall()
                        results = []
                        for row in rows_all[:MAX_ROWS_FOR_AGENT]:
                            d = dict(row)
                            for k, v in d.items():
                                if isinstance(v, Decimal):
                                    d[k] = float(v)
                            results.append(d)
                        message = f'查询成功，返回 {len(results)} 条记录进行预览。'
                        if len(rows_all) > MAX_ROWS_FOR_AGENT:
                            message += f' 注意：实际查询结果超过 {MAX_ROWS_FOR_AGENT} 条，为了性能仅展示前 {MAX_ROWS_FOR_AGENT} 条。若需在地图上查看全部数据，请务必使用 map_tool 的 load_vector_layer 操作。'
                            
                        return {
                            'success': True,
                            'data': results,
                            'message': message
                        }
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Database connection issue on attempt {attempt + 1}: {e}")
                self._connect() # 强制重连
            except IndexError as e:
                return {'success': True, 'data': [], 'message': '查询成功，返回 0 条记录。'}
            except Exception as e:
                if self.conn:
                    try:
                        self.conn.rollback()
                    except:
                        pass
                return {'success': False, 'error': f'查询失败: {str(e)}', 'data': None}
        
        return {'success': False, 'error': '数据库连接持续异常，请检查数据库服务状态', 'data': None}

    def _execute(self, sql: str, params: List[Any]) -> Dict[str, Any]:
        for attempt in range(2):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                
                if self.conn:
                    with self.conn.cursor() as cur:
                        sql = self._auto_quote_sql(sql)
                        cur.execute(sql, params)
                        self.conn.commit()
                        return {
                            'success': True,
                            'data': {'rows_affected': cur.rowcount},
                            'message': f'执行成功，影响 {cur.rowcount} 行'
                        }
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Database connection issue on attempt {attempt + 1}: {e}")
                self._connect()
            except Exception as e:
                if self.conn:
                    try:
                        self.conn.rollback()
                    except:
                        pass
                return {
                    'success': False,
                    'error': f'数据写入失败: {str(e)}',
                    'data': None
                }
        return {'success': False, 'error': '数据库连接持续异常，请检查数据库服务状态', 'data': None}

    def _list_tables(self) -> Dict[str, Any]:
        for attempt in range(2):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                
                if self.conn:
                    with self.conn.cursor() as cur:
                        cur.execute("""
                            SELECT table_name 
                            FROM information_schema.tables 
                            WHERE table_schema = 'public'
                            ORDER BY table_name
                        """)
                        tables = [row['table_name'] for row in cur.fetchall()]
                        return {
                            'success': True,
                            'data': {'tables': tables},
                            'message': f'共找到 {len(tables)} 张表'
                        }
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Database connection issue in list_tables on attempt {attempt + 1}: {e}")
                self._connect()
            except Exception as e:
                logger.error(f"List tables failed: {e}")
                return {
                    'success': False,
                    'error': f'无法获取表列表: {str(e)}',
                    'data': None
                }
        return {'success': False, 'error': '数据库连接持续异常，请检查数据库服务状态', 'data': None}

    def _get_db_schema(self) -> Dict[str, Any]:
        """获取数据库所有表的详细 Schema 信息，用于 Text-to-SQL"""
        for attempt in range(2):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                
                if self.conn:
                    with self.conn.cursor() as cur:
                        # 获取所有表及其列信息
                        cur.execute("""
                            SELECT 
                                t.table_name,
                                c.column_name,
                                c.data_type,
                                c.is_nullable,
                                pg_catalog.col_description(format('%I.%I', t.table_schema, t.table_name)::regclass::oid, c.ordinal_position) as column_comment
                            FROM 
                                information_schema.tables t
                            JOIN 
                                information_schema.columns c ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                            WHERE 
                                t.table_schema = 'public'
                                AND t.table_type = 'BASE TABLE'
                            ORDER BY 
                                t.table_name, c.ordinal_position;
                        """)
                        
                        rows = cur.fetchall()
                        schema_dict = {}
                        for row in rows:
                            table_name = row['table_name']
                            if table_name not in schema_dict:
                                schema_dict[table_name] = []
                            
                            col_info = f"{row['column_name']} ({row['data_type']})"
                            if row['column_comment']:
                                col_info += f" -- {row['column_comment']}"
                            schema_dict[table_name].append(col_info)
                        
                        # 格式化输出为字符串
                        schema_text = "当前数据库公开表结构如下：\n"
                        for table, cols in schema_dict.items():
                            schema_text += f"\n表名: {table}\n字段:\n  - " + "\n  - ".join(cols) + "\n"
                        
                        return {
                            'success': True,
                            'data': {'schema': schema_dict, 'formatted_schema': schema_text},
                            'message': '数据库 Schema 获取成功'
                        }
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Database connection issue in get_db_schema on attempt {attempt + 1}: {e}")
                self._connect()
            except Exception as e:
                logger.error(f"Get DB schema failed: {e}")
                return {
                    'success': False,
                    'error': f'获取数据库结构失败: {str(e)}',
                    'data': None
                }
        return {'success': False, 'error': '数据库连接持续异常，请检查数据库服务状态', 'data': None}

    def _sync_schema_to_file(self) -> Dict[str, Any]:
        """将数据库 Schema 同步到本地 JSON 和 Markdown 文件中"""
        schema_res = self._get_db_schema()
        if not schema_res['success']:
            return schema_res
            
        data = schema_res['data']
        schema_dict = data['schema']
        formatted_text = data['formatted_schema']
        
        # 定义存储路径
        # __file__ 是 backend/tools/postgresql_tool.py
        # base_dir 应该是 backend/
        current_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.dirname(current_dir)
        
        config_dir = os.path.join(base_dir, 'config')
        docs_dir = os.path.join(base_dir, 'docs')
        
        # 确保目录存在
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(docs_dir, exist_ok=True)
        
        json_path = os.path.join(config_dir, 'db_schema.json')
        md_path = os.path.join(docs_dir, 'db_schema.md')
        
        try:
            # 1. 存储 JSON 缓存
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(schema_dict, f, ensure_ascii=False, indent=2)
            
            # 2. 存储 Markdown 文档
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write("# 数据库结构文档 (自动生成)\n\n")
                f.write(formatted_text)
            
            return {
                'success': True,
                'data': {'json_path': json_path, 'md_path': md_path},
                'message': f'Schema 已成功同步至本地文件'
            }
        except Exception as e:
            logger.error(f"Sync schema to file failed: {e}")
            return {'success': False, 'error': f'同步文件失败: {str(e)}', 'data': None}

    def __del__(self):
        if self.conn and not self.conn.closed:
            try:
                self.conn.close()
                logger.debug("PostgreSQL connection closed.")
            except Exception as e:
                logger.warning(f"Error closing PostgreSQL connection: {e}")

