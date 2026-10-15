"""
标注数据存储模块（SQLite）

提供交互式遥感影像标注的持久化存储，支持：
- 手动标注、SAM 辅助标注的保存/加载
- 按 session 分组管理
- GeoJSON 和 COCO 格式导出（用于 SSA 训练）
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 数据库文件路径（统一存储在项目 data 目录下）
DB_DIR = os.environ.get("ANNOTATION_DB_DIR",
                         os.path.join(os.path.dirname(__file__), "..", "data"))
DB_PATH = os.path.join(DB_DIR, "annotations.db")


def _ensure_db_dir():
    os.makedirs(DB_DIR, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动建表"""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            image_path TEXT NOT NULL,
            label TEXT NOT NULL,
            class_id INTEGER,
            geometry TEXT NOT NULL,
            mask_path TEXT,
            source TEXT DEFAULT 'manual',
            confidence REAL,
            iteration INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_annotations_session
        ON annotations(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_annotations_label
        ON annotations(label)
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 核心 CRUD ──

def save(annotation: dict) -> dict:
    """
    保存或更新一条标注。
    如果 annotation 已含 id 且存在则更新，否则新建。
    返回完整标注记录（含自动生成的 id、时间戳）。
    """
    now = _now_iso()
    annot_id = annotation.get("id") or str(uuid.uuid4())[:12]

    geometry = annotation.get("geometry", {})
    if isinstance(geometry, dict):
        geometry_str = json.dumps(geometry, ensure_ascii=False)
    else:
        geometry_str = str(geometry)

    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM annotations WHERE id = ?", (annot_id,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE annotations SET
                    session_id = ?,
                    image_path = ?,
                    label = ?,
                    class_id = ?,
                    geometry = ?,
                    mask_path = ?,
                    source = ?,
                    confidence = ?,
                    iteration = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                annotation.get("session_id", ""),
                annotation.get("image_path", ""),
                annotation.get("label", ""),
                annotation.get("class_id"),
                geometry_str,
                annotation.get("mask_path"),
                annotation.get("source", "manual"),
                annotation.get("confidence"),
                annotation.get("iteration", 0),
                now,
                annot_id,
            ))
        else:
            conn.execute("""
                INSERT INTO annotations
                    (id, session_id, image_path, label, class_id, geometry,
                     mask_path, source, confidence, iteration, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                annot_id,
                annotation.get("session_id", ""),
                annotation.get("image_path", ""),
                annotation.get("label", ""),
                annotation.get("class_id"),
                geometry_str,
                annotation.get("mask_path"),
                annotation.get("source", "manual"),
                annotation.get("confidence"),
                annotation.get("iteration", 0),
                annotation.get("created_at", now),
                now,
            ))

        conn.commit()
        return _row_to_dict(conn.execute(
            "SELECT * FROM annotations WHERE id = ?", (annot_id,)
        ).fetchone())
    finally:
        conn.close()


def save_batch(annotations: list[dict]) -> list[dict]:
    """批量保存标注"""
    results = []
    for ann in annotations:
        results.append(save(ann))
    return results


def load_by_session(session_id: str) -> list[dict]:
    """加载指定 session 的所有标注"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE session_id = ? ORDER BY created_at",
            (session_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def load_by_id(annot_id: str) -> dict | None:
    """加载单条标注"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM annotations WHERE id = ?", (annot_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def delete(annot_id: str) -> bool:
    """删除单条标注，成功返回 True"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM annotations WHERE id = ?", (annot_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_by_session(session_id: str) -> int:
    """删除整个 session 的标注，返回删除数量"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM annotations WHERE session_id = ?", (session_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_sessions() -> list[dict]:
    """列出所有标注 session"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT session_id,
                   COUNT(*) AS count,
                   MIN(created_at) AS created_at,
                   MAX(updated_at) AS updated_at
            FROM annotations
            GROUP BY session_id
            ORDER BY updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── 导出 ──

def export_geojson(session_id: str) -> dict:
    """
    导出为 GeoJSON FeatureCollection。
    每个标注作为一个 Feature，geometry 为标注多边形。
    """
    annotations = load_by_session(session_id)
    features = []
    for ann in annotations:
        geom = _parse_geometry(ann["geometry"])
        if geom is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": ann["id"],
                "label": ann["label"],
                "class_id": ann["class_id"],
                "source": ann["source"],
                "confidence": ann["confidence"],
                "iteration": ann["iteration"],
                "image_path": ann["image_path"],
            }
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "session_id": session_id,
            "exported_at": _now_iso(),
            "total": len(features),
        }
    }


def export_coco(session_id: str, categories: list[dict] | None = None) -> dict:
    """
    导出为 COCO 格式 JSON（用于 SSA 训练）。

    如果未提供 categories，则从标注数据自动推导。
    categories 格式: [{"id": 1, "name": "building", "supercategory": "object"}, ...]
    """
    annotations = load_by_session(session_id)
    images: list[dict] = []
    coco_annotations: list[dict] = []
    cat_map: dict[str, int] = {}  # label → category_id

    # 自动推导类别（如果未提供）
    if categories is None:
        unique_labels = sorted(set(a["label"] for a in annotations if a["label"]))
        categories = [
            {"id": i + 1, "name": lbl, "supercategory": "object"}
            for i, lbl in enumerate(unique_labels)
        ]

    for cat in categories:
        cat_map[cat["name"]] = cat["id"]

    image_id_map: dict[str, int] = {}
    next_image_id = 1
    annot_id_counter = 1

    for ann in annotations:
        img_path = ann.get("image_path", "")
        if img_path and img_path not in image_id_map:
            image_id_map[img_path] = next_image_id
            images.append({
                "id": next_image_id,
                "file_name": os.path.basename(img_path),
                "width": 0,   # 由调用方补充
                "height": 0,
            })
            next_image_id += 1

        geom = _parse_geometry(ann["geometry"])
        if geom is None:
            continue

        # 多边形 → COCO segmentation
        coords = geom.get("coordinates", [[]])[0]
        if len(coords) < 3:
            continue

        # COCO segmentation: flattened list of [x1,y1,x2,y2,...]
        segmentation = [c for pair in coords for c in pair]
        xs = [p[0] for p in coords]
        ys = [p[1] for p in coords]
        bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
        area = bbox[2] * bbox[3]

        cat_id = cat_map.get(ann["label"], cat_map.get("unknown", 1))

        coco_annotations.append({
            "id": annot_id_counter,
            "image_id": image_id_map.get(img_path, 1),
            "category_id": cat_id,
            "segmentation": [segmentation],
            "area": area,
            "bbox": bbox,
            "iscrowd": 0,
            "attributes": {
                "source": ann.get("source", "manual"),
                "confidence": ann.get("confidence"),
                "iteration": ann.get("iteration"),
            }
        })
        annot_id_counter += 1

    return {
        "info": {
            "description": f"Exported from annotation session {session_id}",
            "date_created": _now_iso(),
        },
        "images": images,
        "annotations": coco_annotations,
        "categories": categories,
    }


# ── 内部工具 ──

def _row_to_dict(row) -> dict:
    """sqlite3.Row → dict，自动反序列化 geometry JSON"""
    d = dict(row)
    geom_raw = d.get("geometry", "{}")
    if isinstance(geom_raw, str):
        try:
            d["geometry"] = json.loads(geom_raw)
        except (json.JSONDecodeError, TypeError):
            d["geometry"] = None
    return d


def _parse_geometry(geometry: Any) -> dict | None:
    """尝试将 geometry 字段解析为 GeoJSON geometry object"""
    if isinstance(geometry, str):
        try:
            geometry = json.loads(geometry)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(geometry, dict) and "type" in geometry and "coordinates" in geometry:
        return geometry
    return None
