# -*- coding: utf-8 -*-
"""Kuzu 原生读写：建表 / 写节点 / 写关系 / 读全图。

**为什么不用 LlamaIndex 的 KuzuPropertyGraphStore**：其 `upsert_nodes` 的
`MERGE ... SET` 硬编码了 10 个固定列，自定义属性（溯源 refs、实测高程等）
会被静默丢弃——这正是旧代码必须旁挂 `kuzu_graph_meta.json` 的根因。
因此读写全部直连 kuzu 驱动，属性列由 `schema.py` 显式声明。

`kg_build.py build` 与运行时 `KnowledgeGraphTool` 共用本模块，保证两条写入
路径的列名/类型完全一致。
"""

import json
import logging

from .schema import NODE_TYPES, RELATION_TYPES

logger = logging.getLogger(__name__)

NODE_COLUMNS = {
    t["key"]: ["id", "name", "source", "refs"] + [c for c, _ in t["columns"]]
    for t in NODE_TYPES
}
# 列类型：Kuzu 不接受隐式转换（STRING 写入 DOUBLE 列会报错），
# 因此缺值必须按列类型给零值，不能统一给 ""。
NODE_COL_TYPES = {
    t["key"]: dict([("id", "STRING"), ("name", "STRING"),
                    ("source", "STRING"), ("refs", "STRING")] + list(t["columns"]))
    for t in NODE_TYPES
}
REL_SRC_DST = {label: (src, dst) for src, label, dst in RELATION_TYPES}


def ensure_schema(conn):
    """创建 8 张节点表与 8 张关系表（IF NOT EXISTS）。"""
    from .schema import all_ddl_statements

    for stmt in all_ddl_statements():
        conn.execute(stmt)


def _coerce(val, col_type: str = "STRING"):
    """把 JSON 值转成 Kuzu 可接受的类型（缺值按列类型给零值）。"""
    if val is None or val == "":
        return 0.0 if col_type == "DOUBLE" else (0 if col_type == "INT64" else "")
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    if col_type == "DOUBLE":
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    if col_type == "INT64":
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    return val


def write_nodes(conn, nodes: dict) -> int:
    """写入节点，返回写入条数。"""
    total = 0
    for ntype, bucket in nodes.items():
        cols = NODE_COLUMNS.get(ntype)
        if not cols:
            continue
        col_types = NODE_COL_TYPES.get(ntype, {})
        for node in bucket.values():
            params = {c: _coerce(node.get(c), col_types.get(c, "STRING")) for c in cols}
            if not params.get("name"):
                params["name"] = str(node.get("id", ""))
            sets = ", ".join(f"{c}: ${c}" for c in cols)
            conn.execute(f"CREATE (n:{ntype} {{{sets}}})", params)
            total += 1
    return total


def write_relations(conn, nodes: dict, relations: list) -> tuple:
    """写入关系，返回 (写入数, 丢弃数)。

    Kuzu 关系表按 label 分表，端点类型固定；端点缺失（或 label 不在 schema）
    的关系直接丢弃，避免写入悬空边。
    """
    written = dropped = 0
    for rel in relations:
        label = rel.get("rel")
        if label not in REL_SRC_DST:
            dropped += 1
            continue
        src_type, dst_type = REL_SRC_DST[label]
        src, dst = rel.get("src"), rel.get("dst")
        if src not in nodes.get(src_type, {}) or dst not in nodes.get(dst_type, {}):
            dropped += 1
            continue
        try:
            conn.execute(
                f"MATCH (a:{src_type} {{id: $src}}), (b:{dst_type} {{id: $dst}}) "
                f"CREATE (a)-[:{label} {{source: $source}}]->(b)",
                {"src": src, "dst": dst, "source": rel.get("source", "")},
            )
            written += 1
        except Exception as e:
            logger.warning(f"[KG] 关系写入失败 {label} {src}->{dst}: {e}")
            dropped += 1
    return written, dropped


def write_graph(conn, nodes: dict, relations: list) -> dict:
    ensure_schema(conn)
    n = write_nodes(conn, nodes)
    w, d = write_relations(conn, nodes, relations)
    logger.info(f"[KG] 写入节点 {n} 个，关系 {w} 条（丢弃 {d} 条）")
    return {"nodes": n, "relations": w, "dropped": d}


def table_counts(conn) -> dict:
    """各节点表行数。"""
    out = {}
    for t in NODE_TYPES:
        try:
            out[t["key"]] = conn.execute(
                f"MATCH (n:{t['key']}) RETURN count(*)"
            ).get_next()[0]
        except Exception:
            out[t["key"]] = 0
    return out


def is_empty(conn) -> bool:
    return sum(table_counts(conn).values()) == 0


def read_graph(conn) -> dict:
    """读出全图，产出前端可视化所需的 nodes / links / stats。

    - 每个节点带 `type`（8 类 key）、`category`（schema 序号，兼容旧前端）、
      `refs`（溯源 JSON 字符串，前端据此展示来源与跳转预览）
    - `stats` 同时给出 `by_type` 与兼容字段 `county_count` / `area_count`
    """
    nodes = []
    links = []
    by_type = {}

    for idx, t in enumerate(NODE_TYPES):
        ntype = t["key"]
        cols = NODE_COLUMNS[ntype]
        try:
            result = conn.execute(f"MATCH (n:{ntype}) RETURN n")
        except Exception as e:
            logger.warning(f"[KG] 读取 {ntype} 失败: {e}")
            by_type[ntype] = 0
            continue
        count = 0
        while result.has_next():
            props = result.get_next()[0]
            props = {k: props[k] for k in props} if not isinstance(props, dict) else dict(props)
            props = {k: v for k, v in props.items() if k not in ("_id", "_label")}
            name = str(props.get("name") or props.get("id") or "")
            nodes.append({
                "id": str(props.get("id", "")),
                "name": name,
                "type": ntype,
                "category": idx,
                "categoryName": t["name"],
                "symbolSize": t["size"],
                "itemStyle": {"color": t["color"]},
                "showLabel": t["show_label"],
                "refs": props.get("refs", ""),
                "properties": {k: v for k, v in props.items() if k in cols},
            })
            count += 1
        by_type[ntype] = count

    # 关系：按 schema 的 label 逐表读
    for src_type, label, dst_type in RELATION_TYPES:
        try:
            result = conn.execute(
                f"MATCH (a:{src_type})-[r:{label}]->(b:{dst_type}) "
                f"RETURN a.id, b.id, r.source"
            )
        except Exception as e:
            logger.warning(f"[KG] 读取关系 {label} 失败: {e}")
            continue
        while result.has_next():
            row = result.get_next()
            links.append({
                "source": row[0], "target": row[1],
                "label": label, "source_tag": row[2] if len(row) > 2 else "",
            })

    stats = {
        "node_count": len(nodes),
        "link_count": len(links),
        "by_type": by_type,
        # 兼容旧前端字段
        "county_count": by_type.get("County", 0),
        "area_count": by_type.get("MineableArea", 0),
    }
    return {"nodes": nodes, "links": links, "stats": stats}
