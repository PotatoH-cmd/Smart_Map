# -*- coding: utf-8 -*-
"""图谱 Schema 单一来源：8 类节点 + 8 类关系。

Kuzu 需要在建表时显式声明属性列，且属性类型固定（无 JSON/MAP 类型），
因此溯源信息 `refs` 与 `image_paths` 以 JSON 字符串形式存储。

所有节点表统一包含：
  id           主键
  name         展示名
  source       数据来源标记：postgres | rule | llm
  refs         溯源 JSON 字符串（文档/章节/原文片段），见 refs.py
"""

# ── 节点类型（顺序即前端的 category 序号）──
NODE_TYPES = [
    {
        "key": "County",
        "name": "县区",
        "color": "#0ea5e9",
        "size": 46,
        "show_label": True,
        "columns": [("short", "STRING")],
    },
    {
        "key": "River",
        "name": "河流",
        "color": "#06b6d4",
        "size": 36,
        "show_label": True,
        "columns": [("parent", "STRING")],
    },
    {
        "key": "MineableArea",
        "name": "可采区",
        "color": "#f59e0b",
        "size": 22,
        "show_label": False,
        "columns": [
            ("raw_name", "STRING"),
            ("kind", "STRING"),
            ("river", "STRING"),
            ("county", "STRING"),
            ("year", "INT64"),
            ("years", "STRING"),
            ("license_id", "STRING"),
            ("avg_control_elevation", "DOUBLE"),
            ("avg_measured_depth", "DOUBLE"),
            ("lon", "DOUBLE"),
            ("lat", "DOUBLE"),
            ("measurement_count", "INT64"),
            ("area_m2", "DOUBLE"),
        ],
    },
    {
        "key": "Town",
        "name": "乡镇",
        "color": "#8b5cf6",
        "size": 20,
        "show_label": False,
        "columns": [("county", "STRING")],
    },
    {
        "key": "Village",
        "name": "行政村",
        "color": "#a855f7",
        "size": 16,
        "show_label": False,
        "columns": [("town", "STRING"), ("county", "STRING")],
    },
    {
        "key": "Document",
        "name": "文档",
        "color": "#10b981",
        "size": 30,
        "show_label": False,
        "columns": [
            ("title", "STRING"),
            ("project", "STRING"),
            ("year", "INT64"),
            ("chunk_count", "INT64"),
            ("breadcrumb_root", "STRING"),
            ("image_paths", "STRING"),
        ],
    },
    {
        "key": "License",
        "name": "许可证",
        "color": "#ef4444",
        "size": 18,
        "show_label": False,
        "columns": [("year", "INT64"), ("county", "STRING")],
    },
    {
        "key": "Project",
        "name": "项目",
        "color": "#64748b",
        "size": 34,
        "show_label": True,
        "columns": [("doc_count", "INT64"), ("year", "INT64")],
    },
]

NODE_TYPE_KEYS = [t["key"] for t in NODE_TYPES]
NODE_TYPE_INDEX = {t["key"]: i for i, t in enumerate(NODE_TYPES)}
NODE_TYPE_MAP = {t["key"]: t for t in NODE_TYPES}

# ── 关系类型（label 必须全局唯一，否则 Kuzu 关系表会串表）──
RELATION_TYPES = [
    ("MineableArea", "BELONGS_TO", "County"),
    ("MineableArea", "LOCATED_IN_RIVER", "River"),
    ("MineableArea", "LOCATED_IN_TOWN", "Town"),
    ("License", "ISSUED_FOR", "MineableArea"),
    ("Document", "DOCUMENTS_AREA", "MineableArea"),
    ("Document", "MENTIONS_RIVER", "River"),
    ("Document", "BELONGS_TO_PROJECT", "Project"),
    ("Town", "PART_OF", "County"),
]

RELATION_LABELS = [r[1] for r in RELATION_TYPES]

# 关系表名 = label，直接用于 Cypher 的 -[r:LABEL]->
REL_TABLE = {r[1]: r for r in RELATION_TYPES}


def _node_ddl(node_type: dict) -> str:
    """生成单个节点表的 CREATE 语句。"""
    cols = [
        "    id STRING",
        "    name STRING",
        "    source STRING",
        "    refs STRING",
    ]
    cols += [f"    {c} {t}" for c, t in node_type["columns"]]
    cols.append("    PRIMARY KEY(id)")
    return (
        f"CREATE NODE TABLE IF NOT EXISTS {node_type['key']} (\n"
        + ",\n".join(cols)
        + "\n)"
    )


def node_ddl_statements() -> list:
    """全部节点表的 CREATE 语句。"""
    return [_node_ddl(t) for t in NODE_TYPES]


def relation_ddl_statements() -> list:
    """全部关系表的 CREATE 语句。"""
    return [
        f"CREATE REL TABLE IF NOT EXISTS {label} (FROM {src} TO {dst}, source STRING)"
        for src, label, dst in RELATION_TYPES
    ]


def all_ddl_statements() -> list:
    return node_ddl_statements() + relation_ddl_statements()


def schema_prompt() -> str:
    """供 Text-to-Cypher 使用的图谱结构描述。"""
    lines = ["节点类型："]
    for t in NODE_TYPES:
        cols = ", ".join(["name", "source", "refs"] + [c for c, _ in t["columns"]])
        lines.append(f"  - {t['key']}（{t['name']}），属性：{cols}")
    lines.append("关系：")
    for src, label, dst in RELATION_TYPES:
        lines.append(f"  - ({src})-[{label}]->({dst})")
    return "\n".join(lines)
