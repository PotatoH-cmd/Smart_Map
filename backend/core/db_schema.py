"""数据库 schema 的单一事实来源（符号常量 + 存在性校验）。

背景：业务库表/字段名此前散落在 9 个文件里直接拼字面量（SQL、正则、提示词），
任一处漏改就会出现行为不一致（如改字段名后只改了一半）。本模块收敛为唯一定义点：
- 权威 schema：config/db_schema.json（由 SchemaManager 从 Postgres 自动同步）；
- 本模块导出符号常量，调用方一律引用常量，不再拼字面量；
- 常量在首次使用时与 schema 文件做存在性校验，缺失仅告警（不阻断运行）。

环境变量：DB_SCHEMA_PATH 覆盖 schema 文件路径（测试隔离用）。
"""
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]

# ── 表名 ──
TABLE_CESHEN = "ceshen"          # 测深/采区业务表
TABLE_CAIQU = "caiqu"            # 可采区图层表
TABLE_HX = "hx"                  # 河道管理范围线（红线）图层表
TABLE_RTK_POINTS = "rtk_points"  # RTK 实测点位表

# ── ceshen 字段（本项目引用最频繁的一批）──
CESHEN_AREA_ID = "Mineable_Area_ID"
CESHEN_AREA_NAME = "Mineable_Area_Name"
CESHEN_COUNTY = "County_District"
CESHEN_YEAR = "Year"
CESHEN_MEASURED_DEPTH = "Measured_Depth"
CESHEN_CONTROL_ELEVATION = "Control_Elevation"
CESHEN_LON = "Lon_4326"
CESHEN_LAT = "Lat_4326"

# 常量 → 所属表（校验用；新增常量在此登记即可被自动校验）
_CONSTANT_TABLES = {
    TABLE_CESHEN: (
        CESHEN_AREA_ID, CESHEN_AREA_NAME, CESHEN_COUNTY, CESHEN_YEAR,
        CESHEN_MEASURED_DEPTH, CESHEN_CONTROL_ELEVATION, CESHEN_LON, CESHEN_LAT,
    ),
}

_COL_RE = re.compile(r"^\s*([^\s(]+)\s*(?:\(|$)")
_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "tables": {}, "warned": set()}


def schema_path() -> Path:
    override = os.environ.get("DB_SCHEMA_PATH")
    return Path(override) if override else (BACKEND_DIR / "config" / "db_schema.json")


def _parse_schema(raw: Dict[str, Any]) -> Dict[str, Set[str]]:
    """db_schema.json → {表名: {字段名}}；条目形如 "Mineable_Area_Name (character varying)" 或 "id"。"""
    tables: Dict[str, Set[str]] = {}
    for table, cols in (raw or {}).items():
        if not isinstance(cols, list):
            continue
        names: Set[str] = set()
        for item in cols:
            text = str(item)
            m = _COL_RE.match(text)
            if m:
                names.add(m.group(1))
        tables[str(table)] = names
    return tables


def schema_tables() -> Dict[str, Set[str]]:
    """加载 schema（按 mtime 缓存）；文件缺失/异常返回 {}（校验降级为放行）。"""
    path = schema_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["tables"]
        tables: Dict[str, Set[str]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                tables = _parse_schema(raw)
        except Exception as e:
            logger.warning(f"[db_schema] schema 加载失败（跳过存在性校验）: {e}")
        _cache["mtime"], _cache["tables"] = mtime, tables
        _cache["warned"] = set()
        return tables


def has_column(table: str, column: str) -> bool:
    """字段是否在 schema 中（schema 不可用时返回 True，避免误伤运行）。"""
    tables = schema_tables()
    if not tables:
        return True
    return column in (tables.get(table) or set())


def _warn_once(table: str, column: str) -> None:
    key = f"{table}.{column}"
    with _lock:
        if key in _cache["warned"]:
            return
        _cache["warned"].add(key)
    logger.warning(f"[db_schema] 常量 {key} 不在 db_schema.json 中（可能与库结构漂移）")


def quote(column: str, table: str = TABLE_CESHEN) -> str:
    """SQL 双引号包裹的列名（大小写敏感字段必需），并做存在性校验。"""
    if not has_column(table, column):
        _warn_once(table, column)
    return f'"{column}"'


def missing_constants() -> List[str]:
    """返回与 schema 漂移的常量清单（测试/巡检用）。schema 不可用时返回空表。"""
    tables = schema_tables()
    if not tables:
        return []
    missing: List[str] = []
    for table, cols in _CONSTANT_TABLES.items():
        known = tables.get(table) or set()
        missing.extend(f"{table}.{c}" for c in cols if c not in known)
    return missing
