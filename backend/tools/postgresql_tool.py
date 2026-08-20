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
from .schema_manager import SchemaManager
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
        PostgreSQL 数据库操作工具，支持 Text-to-SQL 和空间查询。
        你可以使用此工具：
        1. 执行 SQL 查询 (query)
        2. 执行 SQL 修改 (execute)
        3. 获取数据库表结构 (get_db_schema) - **在生成 SQL 前，强烈建议先调用此操作以获取最新的表结构和字段信息**。
        4. 空间查询 (spatial_query) - 根据几何约束筛选数据，如"红线范围内的采砂场"
        '''
    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '数据库操作类型',
            'enum': ['query', 'execute', 'get_db_schema', 'list_tables', 'sync_schema', 'spatial_query'],
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
            'name': 'spatial_op',
            'type': 'string',
            'description': '空间关系操作符（仅 spatial_query 需要）：intersects=相交, within=在内部, contains=包含, dwithin=距离内',
            'enum': ['intersects', 'within', 'contains', 'dwithin'],
            'required': False
        },
        {
            'name': 'spatial_table',
            'type': 'string',
            'description': '要查询的空间数据表名（仅 spatial_query 需要），如 ceshen',
            'required': False
        },
        {
            'name': 'spatial_geom_wkt',
            'type': 'string',
            'description': 'WKT 格式的空间约束几何（仅 spatial_query 需要）。与 spatial_ref_layer 二选一',
            'required': False
        },
        {
            'name': 'spatial_ref_layer',
            'type': 'string',
            'description': '空间参考图层 key，如 hx（红线）或 caiqu（采区）。传入后自动加载对应几何做空间筛选，无需手动传 WKT。推荐用法，与 spatial_geom_wkt 二选一',
            'required': False
        },
        {
            'name': 'spatial_geom_col',
            'type': 'string',
            'description': '几何列名（仅 spatial_query 需要），默认 geom',
            'required': False
        },
        {
            'name': 'spatial_extra_columns',
            'type': 'string',
            'description': '额外 SELECT 列，逗号分隔（仅 spatial_query 需要）。不传则 SELECT *',
            'required': False
        },
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
            elif operation == 'spatial_query':
                return self._spatial_query(params)
            else:
                return {
                    'success': False,
                    'error': f'不支持的操作类型: {operation}。支持的操作: query, execute, get_db_schema, list_tables, sync_schema, spatial_query',
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
        """委托给集中式 SchemaManager 单例。"""
        if self._schema_cache is not None:
            return self._schema_cache
        sm = SchemaManager.instance(self.cfg)
        self._schema_cache = sm.get_column_cache()
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
                        
                        # 返回全部数据，让下游（报告/可视化/统计）基于完整数据集计算。
                        # 防止 token 爆炸的截断已在下游各自完成：
                        #   - response_node 的 tool_summaries 仅取 data[:10]
                        #   - report_builder 的 raw_rows 仅取 rows[:200] 给 LLM 看，统计基于全量
                        rows_all = cur.fetchall()
                        results = []
                        for row in rows_all:
                            d = dict(row)
                            for k, v in d.items():
                                if isinstance(v, Decimal):
                                    d[k] = float(v)
                            results.append(d)

                        total_count = len(results)
                        message = f'查询成功，返回 {total_count} 条记录。（已返回全部）'
                            
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
                err_text = str(e)
                # 若字段不存在，刷新 schema 缓存后重试一次（用于大小写字段自动纠正）
                if attempt == 0 and ('does not exist' in err_text.lower()) and ('column' in err_text.lower()):
                    logger.warning(f"Query failed with undefined column, refreshing schema cache and retrying once: {err_text}")
                    self._schema_cache = None
                    SchemaManager.instance(self.cfg).refresh()
                    continue
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

    def _spatial_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """空间查询：根据几何约束筛选数据。

        支持两种方式指定空间约束：
        1. spatial_ref_layer='hx' → 自动加载 hx 图层几何（推荐）
        2. spatial_geom_wkt='POLYGON((...))' → 手动传入 WKT
        根据 spatial_op 构建 PostGIS ST_ 空间关系查询。
        """
        spatial_table = params.get('spatial_table', '')
        spatial_op = params.get('spatial_op', 'intersects')
        geom_wkt = params.get('spatial_geom_wkt', '')
        ref_layer = params.get('spatial_ref_layer', '')
        geom_col = params.get('spatial_geom_col', 'geom')
        extra_cols = params.get('spatial_extra_columns', '')

        if not spatial_table:
            return {'success': False, 'error': 'spatial_query 必须指定 spatial_table'}

        # 如果传入了 spatial_ref_layer，服务端自动加载几何（推荐方式，免去 LLM 传递大几何）
        if ref_layer and not geom_wkt:
            try:
                from .overlay_tile_service import get_layer
                layer = get_layer(ref_layer)
                if layer is None:
                    return {'success': False, 'error': f'空间参考图层 {ref_layer} 不存在'}
                layer.load()
                if not layer.geoms:
                    return {'success': False, 'error': f'空间参考图层 {ref_layer} 无数据'}
                # 用整体边界框(BBOX)生成 POLYGON WKT 做空间筛选
                # 不用 GeometryCollection，避免海量几何导致 WKT 过大(>64MB)和查询超时
                from pyproj import Transformer
                to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
                xs_min, ys_min, xs_max, ys_max = [], [], [], []
                for g in layer.geoms:
                    bx = g.bounds
                    xs_min.append(bx[0]); ys_min.append(bx[1])
                    xs_max.append(bx[2]); ys_max.append(bx[3])
                min_lng, min_lat = to_4326(min(xs_min), min(ys_min))
                max_lng, max_lat = to_4326(max(xs_max), max(ys_max))
                # 稍微扩展边界(约 0.5km)避免边界遗漏
                pad = 0.005
                geom_wkt = (
                    f"POLYGON(({min_lng-pad} {min_lat-pad},"
                    f"{max_lng+pad} {min_lat-pad},"
                    f"{max_lng+pad} {max_lat+pad},"
                    f"{min_lng-pad} {max_lat+pad},"
                    f"{min_lng-pad} {min_lat-pad}))"
                )
                logger.info(f"[spatial_query] 用空间参考图层 {ref_layer} 的 BBOX 做筛选: "
                            f"[{min_lng:.4f},{min_lat:.4f} ~ {max_lng:.4f},{max_lat:.4f}]")
            except Exception as e:
                return {'success': False, 'error': f'加载空间参考图层 {ref_layer} 失败: {e}'}

        if not geom_wkt:
            return {'success': False, 'error': 'spatial_query 必须指定 spatial_geom_wkt 或 spatial_ref_layer'}

        # 构建空间 SQL
        st_func = {
            'intersects': 'ST_Intersects',
            'within': 'ST_Within',
            'contains': 'ST_Contains',
            'dwithin': 'ST_DWithin',
        }.get(spatial_op, 'ST_Intersects')

        cols = extra_cols if extra_cols else '*'

        if geom_col and geom_col.lower() != 'geom':
            # 如果几何列名不是默认的 geom，需要加双引号（PostgreSQL 大小写敏感）
            geom_col_quoted = f'"{geom_col}"'
        else:
            geom_col_quoted = '"geom"'

        sql = (
            f'SELECT {cols}, ST_AsGeoJSON({geom_col_quoted}) AS _geojson '
            f'FROM "{spatial_table}" '
            f'WHERE {st_func}({geom_col_quoted}, ST_GeomFromText(%s, 4326))'
        )

        logger.info(f"[spatial_query] {st_func} on {spatial_table} with WKT len={len(geom_wkt)}")

        # 复用 _query 的重连+执行逻辑，但需要适配参数传递
        for attempt in range(2):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                if not self.conn:
                    return {'success': False, 'error': '数据库连接失败'}

                with self.conn.cursor() as cur:
                    cur.execute(sql, [geom_wkt])
                    rows_all = cur.fetchall()
                    results = []
                    for row in rows_all:
                        d = dict(row)
                        for k, v in d.items():
                            if isinstance(v, Decimal):
                                d[k] = float(v)
                        results.append(d)

                    return {
                        'success': True,
                        'data': results,
                        'count': len(results),
                        'message': f'空间查询成功（{spatial_op}），返回 {len(results)} 条记录',
                        'sql_hint': sql.replace(geom_wkt, '<WKT_GEOM>'),
                    }
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"[spatial_query] 连接异常 attempt {attempt+1}: {e}")
                self._connect()
            except Exception as e:
                if self.conn:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                return {'success': False, 'error': f'空间查询失败: {str(e)}'}

        return {'success': False, 'error': '数据库连接持续异常'}

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
        """获取数据库所有表的详细 Schema 信息，用于 Text-to-SQL。
        委托给集中式 SchemaManager 单例。"""
        try:
            sm = SchemaManager.instance(self.cfg)
            schema_dict = sm.get_schema_dict()
            formatted = sm.get_formatted_schema()
            if schema_dict:
                return {
                    'success': True,
                    'data': {'schema': schema_dict, 'formatted_schema': formatted},
                    'message': '数据库 Schema 获取成功'
                }
            # 缓存为空，尝试刷新一次
            sm.refresh()
            schema_dict = sm.get_schema_dict()
            formatted = sm.get_formatted_schema()
            if schema_dict:
                return {
                    'success': True,
                    'data': {'schema': schema_dict, 'formatted_schema': formatted},
                    'message': '数据库 Schema 获取成功'
                }
            return {'success': False, 'error': '获取数据库结构失败：Schema 为空', 'data': None}
        except Exception as e:
            logger.error(f"Get DB schema failed: {e}")
            return {'success': False, 'error': f'获取数据库结构失败: {str(e)}', 'data': None}

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

