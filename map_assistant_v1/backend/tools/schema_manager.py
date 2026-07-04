"""
schema_manager.py
──────────────────────────────────────────────────────────────
进程级单例：集中管理 PostgreSQL Schema 缓存。

所有需要 schema 的模块（IntentAgent、PostgreSQLTool、DataVisualizerTool）
都通过 SchemaManager.instance() 获取同一份缓存，避免重复查询。
"""

import logging
import os
import json
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False


class SchemaManager:
    """进程级 Schema 单例。"""

    _instance: Optional["SchemaManager"] = None
    _lock = threading.Lock()

    # ── 单例接口 ────────────────────────────────────────────
    @classmethod
    def instance(cls, db_cfg: Optional[Dict[str, Any]] = None) -> "SchemaManager":
        """获取单例。首次调用时可传入 db_cfg 完成初始化。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_cfg or {})
        return cls._instance

    # ── 初始化 ──────────────────────────────────────────────
    def __init__(self, db_cfg: Dict[str, Any]):
        self._db_cfg = {
            "host": db_cfg.get("host", "172.136.16.52"),
            "port": db_cfg.get("port", 5432),
            "database": db_cfg.get("database", "postgres"),
            "user": db_cfg.get("user", "postgres"),
            "password": db_cfg.get("password", "8720622"),
        }
        # 缓存数据
        self._schema_dict: Optional[Dict[str, List[str]]] = None
        self._formatted_schema: Optional[str] = None
        self._columns: List[str] = []
        self._case_sensitive: List[str] = []
        self._lower_map: Dict[str, str] = {}
        self._loaded = False
        self._data_lock = threading.Lock()

    # ── 公共读接口 ──────────────────────────────────────────

    def get_formatted_schema(self) -> str:
        """返回格式化的 schema 文本（供 prompt 使用）。"""
        self._ensure_loaded()
        return self._formatted_schema or ""

    def get_schema_dict(self) -> Dict[str, List[str]]:
        """返回 {table_name: [col_info, ...]} 字典。"""
        self._ensure_loaded()
        return self._schema_dict or {}

    def get_columns(self) -> List[str]:
        """所有列名列表。"""
        self._ensure_loaded()
        return self._columns

    def get_case_sensitive(self) -> List[str]:
        """含大写字母的列名列表。"""
        self._ensure_loaded()
        return self._case_sensitive

    def get_lower_map(self) -> Dict[str, str]:
        """小写列名 → 真实列名映射。"""
        self._ensure_loaded()
        return self._lower_map

    def get_column_cache(self) -> Dict[str, Any]:
        """兼容 PostgreSQLTool._load_schema_cache() 的返回格式。"""
        self._ensure_loaded()
        return {
            "columns": self._columns,
            "case_sensitive": self._case_sensitive,
            "lower_map": self._lower_map,
        }

    # ── 刷新 ────────────────────────────────────────────────

    def refresh(self) -> bool:
        """强制从数据库重新加载 schema。"""
        with self._data_lock:
            self._loaded = False
            return self._load()

    # ── 内部加载 ────────────────────────────────────────────

    def _ensure_loaded(self):
        if not self._loaded:
            with self._data_lock:
                if not self._loaded:
                    self._load()

    def _load(self) -> bool:
        """从 PostgreSQL information_schema 加载全部公共表结构。"""
        ok = self._load_from_db()
        if not ok:
            ok = self._load_from_file()
        self._loaded = True
        return ok

    def _load_from_db(self) -> bool:
        if not PG_AVAILABLE:
            return False
        conn = None
        try:
            conn = psycopg2.connect(
                host=self._db_cfg["host"],
                port=self._db_cfg["port"],
                database=self._db_cfg["database"],
                user=self._db_cfg["user"],
                password=self._db_cfg["password"],
                cursor_factory=RealDictCursor,
                connect_timeout=5,
            )
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        t.table_name,
                        c.column_name,
                        c.data_type,
                        c.is_nullable,
                        pg_catalog.col_description(
                            (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                            c.ordinal_position
                        ) AS column_comment
                    FROM information_schema.tables t
                    JOIN information_schema.columns c
                      ON t.table_name = c.table_name
                     AND t.table_schema = c.table_schema
                    WHERE t.table_schema = 'public'
                      AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name, c.ordinal_position;
                """)
                rows = cur.fetchall()

            schema_dict: Dict[str, List[str]] = {}
            all_columns: List[str] = []
            for row in rows:
                table = row["table_name"]
                col_name = row["column_name"]
                col_info = f"{col_name} ({row['data_type']})"
                if row.get("column_comment"):
                    col_info += f" -- {row['column_comment']}"
                schema_dict.setdefault(table, []).append(col_info)
                all_columns.append(col_name)

            self._schema_dict = schema_dict
            self._formatted_schema = self._format_schema(schema_dict)
            self._build_column_index(all_columns)
            self._save_to_file(schema_dict)
            logger.info(f"[SchemaManager] Loaded schema from DB: {len(schema_dict)} tables, {len(all_columns)} columns")
            return True
        except Exception as e:
            logger.warning(f"[SchemaManager] Failed to load schema from DB: {e}")
            return False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _load_from_file(self) -> bool:
        """DB 不可用时回退到本地缓存文件。"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(current_dir)
            json_path = os.path.join(base_dir, "config", "db_schema.json")
            if not os.path.exists(json_path):
                return False
            with open(json_path, "r", encoding="utf-8") as f:
                schema_dict = json.load(f)

            all_columns: List[str] = []
            for cols in schema_dict.values():
                for col in cols:
                    col_name = col.split(" (", 1)[0].strip()
                    if col_name:
                        all_columns.append(col_name)

            self._schema_dict = schema_dict
            self._formatted_schema = self._format_schema(schema_dict)
            self._build_column_index(all_columns)
            logger.info(f"[SchemaManager] Loaded schema from file cache: {len(schema_dict)} tables")
            return True
        except Exception as e:
            logger.warning(f"[SchemaManager] Failed to load schema from file: {e}")
            return False

    # ── 辅助 ────────────────────────────────────────────────

    @staticmethod
    def _format_schema(schema_dict: Dict[str, List[str]]) -> str:
        lines = ["当前数据库公开表结构如下："]
        for table, cols in schema_dict.items():
            lines.append(f"\n表名: {table}\n字段:\n  - " + "\n  - ".join(cols))
        return "\n".join(lines)

    def _build_column_index(self, columns: List[str]):
        unique = list(dict.fromkeys(columns))
        self._columns = unique
        self._lower_map = {c.lower(): c for c in unique}
        self._case_sensitive = [c for c in unique if any(ch.isupper() for ch in c)]

    def _save_to_file(self, schema_dict: Dict[str, List[str]]):
        """持久化到本地 JSON 文件供离线使用。"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(current_dir)
            config_dir = os.path.join(base_dir, "config")
            os.makedirs(config_dir, exist_ok=True)
            json_path = os.path.join(config_dir, "db_schema.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(schema_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[SchemaManager] Failed to save schema file: {e}")
