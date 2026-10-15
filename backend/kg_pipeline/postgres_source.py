# -*- coding: utf-8 -*-
"""PostgreSQL 测深表数据源：采砂场实测高程/深度/坐标。

图谱中采区的「实测值」只能来自 PostgreSQL（知识库是方案文本，无测量点），
因此 build 阶段把 PG 聚合行并入同名采区节点：

    PG:  史灌河童庙可采区 / 大营-范营可采区 / 潘庄砂场
    KB:  童庙可采区 / 范营村可采区 / 王楼砂场

归一化后 `童庙` 两侧一致 → 合并为同一节点，PG 提供实测属性、KB 提供溯源。
`采区 ID 列` 在库里实为许可证号，与知识库侧许可证号归一化后对齐。
"""

import logging
import re

from .normalize import license_norm, normalize_area_name
from .refs import dump_refs, make_ref

logger = logging.getLogger(__name__)

from core import db_schema as DB  # 表/字段名单一来源

# PG 采区名称列既含批复采区，也含清淤/疏浚/生态修复等工程项目。
# 后者不是「可采区」，用 kind 区分而不是丢弃——保留全部源数据但可筛选。
_AREA_LIKE_RE = re.compile(r"(可采区|采区|砂场|采砂场|开采区|可采段|采点|可采点)")

PG_REF_DOC_ID = f"postgres:{DB.TABLE_CESHEN}"
PG_REF_TITLE = f"PostgreSQL {DB.TABLE_CESHEN} 测量点表"


def _kind(raw_name: str) -> str:
    """采区 or 工程（PG 侧 53 个名称里约一半是工程项目）。"""
    return "采区" if _AREA_LIKE_RE.search(raw_name or "") else "工程"

PG_CONFIG = {
    "host": "172.136.16.52",
    "port": 5432,
    "database": "postgres",
    "user": "postgres",
    "password": "",
}


def _pg_config() -> dict:
    import os
    cfg = dict(PG_CONFIG)
    cfg["host"] = os.environ.get("GEOSERVER_PG_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("GEOSERVER_PG_PORT", cfg["port"]))
    cfg["database"] = os.environ.get("GEOSERVER_PG_DB", cfg["database"])
    cfg["user"] = os.environ.get("GEOSERVER_PG_USER", cfg["user"])
    cfg["password"] = os.environ.get("GEOSERVER_PG_PASSWORD", cfg["password"])
    return cfg


SQL = f"""
    SELECT
        {DB.quote(DB.CESHEN_AREA_NAME)},
        {DB.quote(DB.CESHEN_COUNTY)},
        {DB.quote(DB.CESHEN_YEAR)},
        {DB.quote(DB.CESHEN_AREA_ID)},
        AVG({DB.quote(DB.CESHEN_CONTROL_ELEVATION)})::numeric(10,2) AS avg_control_elevation,
        AVG({DB.quote(DB.CESHEN_MEASURED_DEPTH)})::numeric(10,2)    AS avg_measured_depth,
        AVG({DB.quote(DB.CESHEN_LON)})::numeric(10,6)               AS lon,
        AVG({DB.quote(DB.CESHEN_LAT)})::numeric(10,6)               AS lat,
        COUNT(*)                                                    AS measurement_count
    FROM {DB.TABLE_CESHEN}
    GROUP BY {DB.quote(DB.CESHEN_AREA_NAME)}, {DB.quote(DB.CESHEN_COUNTY)},
             {DB.quote(DB.CESHEN_YEAR)}, {DB.quote(DB.CESHEN_AREA_ID)}
    ORDER BY {DB.quote(DB.CESHEN_COUNTY)}, {DB.quote(DB.CESHEN_AREA_NAME)}
"""


def load_pg_areas(alias: dict = None) -> list:
    """读取并聚合 PG 采砂场行。

    返回 [{"id","raw_name","county","year","license_id",
           "avg_control_elevation","avg_measured_depth","lon","lat",
           "measurement_count"}, ...]，id 为归一化名（经 alias 合并到规范名）。
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("[KG-PG] psycopg2 未安装，跳过 PostgreSQL 数据源")
        return []

    alias = alias or {}
    conn = psycopg2.connect(**_pg_config())
    try:
        cur = conn.cursor()
        cur.execute(SQL)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    out = []
    for row in rows:
        raw = row[0] or ""
        norm = normalize_area_name(raw)
        if not norm:
            continue
        out.append({
            "id": alias.get(norm, norm),
            "raw_name": raw,
            "county": (row[1] or "").strip(),
            "year": int(row[2]) if row[2] else 0,
            "license_id": license_norm(row[3] or ""),
            "avg_control_elevation": float(row[4]) if row[4] is not None else 0.0,
            "avg_measured_depth": float(row[5]) if row[5] is not None else 0.0,
            "lon": float(row[6]) if row[6] is not None else 0.0,
            "lat": float(row[7]) if row[7] is not None else 0.0,
            "measurement_count": int(row[8]) if row[8] else 0,
        })
    logger.info(f"[KG-PG] 读取 {len(out)} 条聚合采砂场记录")
    return out


def merge_pg_into_nodes(nodes: dict, pg_rows: list) -> dict:
    """把 PG 实测属性并入图谱节点（KB 已有属性优先保留，PG 只补空缺与实测值）。

    返回 {"merged": n, "new": m}。
    """
    merged = new = 0
    bucket = nodes.setdefault("MineableArea", {})
    for row in pg_rows:
        aid = row["id"]
        if aid not in bucket:
            ref = make_ref(
                doc_id=PG_REF_DOC_ID, title=PG_REF_TITLE,
                section=row["county"] or "实测数据",
                snippet=(
                    f"原名「{row['raw_name']}」，{row['measurement_count']} 个测量点，"
                    f"平均控制高程 {row['avg_control_elevation']}m，"
                    f"平均实测深度 {row['avg_measured_depth']}m"
                ),
                count=row["measurement_count"] or 1,
            )
            bucket[aid] = {
                "id": aid, "name": aid, "source": "postgres",
                "refs": dump_refs([ref]),
                "raw_name": row["raw_name"], "kind": _kind(row["raw_name"]),
                "county": row["county"],
                "river": "", "year": row["year"], "years": str(row["year"] or ""),
                "license_id": row["license_id"],
                "avg_control_elevation": row["avg_control_elevation"],
                "avg_measured_depth": row["avg_measured_depth"],
                "lon": row["lon"], "lat": row["lat"],
                "measurement_count": row["measurement_count"],
                "area_m2": 0.0,
            }
            new += 1
            continue

        node = bucket[aid]
        node.setdefault("kind", "采区")   # 知识库侧抽出的都是采区
        # 实测字段始终以 PG 为准（知识库无测量点）
        for key in ("avg_control_elevation", "avg_measured_depth", "lon", "lat",
                    "measurement_count"):
            if row[key]:
                node[key] = row[key]
        # 其余字段仅补空缺
        for key in ("county", "license_id", "raw_name"):
            if row[key] and not node.get(key):
                node[key] = row[key]
        # 多年份累积
        years = {y for y in (node.get("years") or "").split(",") if y}
        if row["year"]:
            years.add(str(row["year"]))
            node["years"] = ",".join(sorted(years))
            if not node.get("year"):
                node["year"] = row["year"]
        # 合并来源标记
        srcs = set((node.get("source") or "").split("+")) - {""}
        srcs.add("postgres")
        node["source"] = "+".join(sorted(srcs))
        merged += 1
    return {"merged": merged, "new": new}
