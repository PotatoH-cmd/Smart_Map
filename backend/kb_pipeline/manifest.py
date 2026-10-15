"""清单表读写：kb_documents（文档级）+ kb_chunks（切片级）。

设计要点：
  - kb_documents 兼容原有结构，仅补 source_file / source_mtime 两列，
    用于过期检测；建表语句与原 _kb_lookup 中一致，可直接复用旧库。
  - kb_chunks 是本次新增的切片级表，存放 node_id → chunk_fp 映射，
    让「某文档有哪些旧切片」可以索引查询，而不必遍历 40MB 的 docstore.json。
  - 表不存在时自动建表并做列迁移，保证首次运行零配置。

一致性策略：以 docstore 为准。先改 docstore 成功，再写清单；
清单写失败只告警，可由 backfill 脚本重新修复。
"""

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

_DOCS_DDL = """
CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id        TEXT PRIMARY KEY,
    name          TEXT,
    content_hash  TEXT,
    chunk_count   INTEGER DEFAULT 0,
    source        TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    source_file   TEXT,
    source_mtime  REAL,
    project       TEXT,
    project_name  TEXT
)
"""

_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    node_id    TEXT PRIMARY KEY,
    doc_id     TEXT NOT NULL,
    chunk_fp   TEXT NOT NULL,
    updated_at TEXT
)
"""

# kb_documents 的增量列迁移（旧库只有前 7 列）
_DOCS_MIGRATIONS = (
    ("source_file", "TEXT"),
    ("source_mtime", "REAL"),
    ("project", "TEXT"),
    ("project_name", "TEXT"),
)


def _default_db_path() -> str:
    return os.environ.get(
        "MAPASSIST_DB_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db"
        ),
    )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Manifest:
    """kb_documents / kb_chunks 的读写封装。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        self.ensure_schema()

    # ── 建表与迁移 ──────────────────────────────

    def ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_DOCS_DDL)
            conn.execute(_CHUNKS_DDL)
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(kb_documents)")
            }
            for col, col_type in _DOCS_MIGRATIONS:
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE kb_documents ADD COLUMN {col} {col_type}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_documents_hash "
                "ON kb_documents(content_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_fp ON kb_chunks(chunk_fp)"
            )
            conn.commit()

    # ── 文档级 ──────────────────────────────────

    def lookup(self, field: str, value: Any) -> Optional[Dict[str, Any]]:
        """按字段查文档。field 仅允许白名单列，避免 SQL 拼接风险。"""
        allowed = {
            "doc_id", "name", "content_hash", "source", "source_file",
        }
        if field not in allowed:
            raise ValueError(f"不支持的查询字段: {field}")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM kb_documents WHERE {field}=? LIMIT 1", (value,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_document(
        self,
        doc_id: str,
        name: str,
        content_hash: str = "",
        chunk_count: int = 0,
        source: str = "",
        source_file: Optional[str] = None,
        source_mtime: Optional[float] = None,
        project: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> None:
        now = _now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO kb_documents (doc_id, name, content_hash, chunk_count, "
                "source, created_at, updated_at, source_file, source_mtime, "
                "project, project_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "name=excluded.name, content_hash=excluded.content_hash, "
                "chunk_count=excluded.chunk_count, source=excluded.source, "
                "updated_at=excluded.updated_at, "
                # COALESCE：本次未提供源文件/项目信息时保留旧值，避免被 NULL 覆盖
                "source_file=COALESCE(excluded.source_file, kb_documents.source_file), "
                "source_mtime=COALESCE(excluded.source_mtime, kb_documents.source_mtime), "
                "project=COALESCE(excluded.project, kb_documents.project), "
                "project_name=COALESCE(excluded.project_name, kb_documents.project_name)",
                (
                    doc_id, name, content_hash, chunk_count, source, now, now,
                    source_file, source_mtime, project, project_name,
                ),
            )
            conn.commit()

    def set_document_project(
        self, doc_id: str, project: Optional[str], project_name: Optional[str]
    ) -> None:
        """仅更新项目归属，供回填脚本使用（不触碰其他列）。"""
        if not project and not project_name:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE kb_documents SET "
                "project=COALESCE(?, project), project_name=COALESCE(?, project_name) "
                "WHERE doc_id=?",
                (project, project_name, doc_id),
            )
            conn.commit()

    def chunk_count_of(self, doc_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM kb_chunks WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]

    def delete_document(self, doc_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM kb_documents WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
            conn.commit()

    def all_documents(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM kb_documents ORDER BY doc_id").fetchall()
        return [dict(r) for r in rows]

    # ── 切片级 ──────────────────────────────────

    def chunk_fps(self, doc_id: str) -> set:
        """取某文档已入库的全部切片指纹。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT chunk_fp FROM kb_chunks WHERE doc_id=?", (doc_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def node_ids_of(self, doc_id: str) -> Dict[str, List[str]]:
        """取某文档的 chunk_fp → [node_id…] 映射。

        用列表而非单值：库内可能存在同文档同指纹的重复切片
        （历史 insert_nodes 重复执行留下的脏数据），
        增量流程需要能识别并清理它们，故这里如实返回全部 node_id。
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT chunk_fp, node_id FROM kb_chunks WHERE doc_id=? ORDER BY node_id",
                (doc_id,),
            ).fetchall()
        out: Dict[str, List[str]] = {}
        for fp, nid in rows:
            out.setdefault(fp, []).append(nid)
        return out

    def upsert_chunks(self, doc_id: str, fp_to_nodes: Dict[str, Any]) -> None:
        """写入切片记录。fp_to_nodes 的值可以是 str 或 list[str]。"""
        if not fp_to_nodes:
            return
        now = _now()
        rows = []
        for fp, nids in fp_to_nodes.items():
            if isinstance(nids, str):
                nids = [nids]
            for nid in nids:
                rows.append((nid, doc_id, fp, now))
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO kb_chunks (node_id, doc_id, chunk_fp, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "doc_id=excluded.doc_id, chunk_fp=excluded.chunk_fp, "
                "updated_at=excluded.updated_at",
                rows,
            )
            conn.commit()

    def delete_chunks_by_fp(self, doc_id: str, fps) -> List[str]:
        """按指纹删切片记录，返回被删的 node_id 列表（供 docstore 联动删除）。"""
        fps = [f for f in fps if f]
        if not fps:
            return []
        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" * len(fps))
            rows = conn.execute(
                f"SELECT node_id FROM kb_chunks WHERE doc_id=? AND chunk_fp IN ({placeholders})",
                (doc_id, *fps),
            ).fetchall()
            node_ids = [r[0] for r in rows]
            if node_ids:
                ph2 = ",".join("?" * len(node_ids))
                conn.execute(
                    f"DELETE FROM kb_chunks WHERE node_id IN ({ph2})", node_ids
                )
            conn.commit()
        return node_ids

    def delete_chunks_by_node(self, node_ids) -> None:
        node_ids = [n for n in node_ids if n]
        if not node_ids:
            return
        with sqlite3.connect(self.db_path) as conn:
            ph = ",".join("?" * len(node_ids))
            conn.execute(f"DELETE FROM kb_chunks WHERE node_id IN ({ph})", node_ids)
            conn.commit()

    def replace_document_chunks(self, doc_id: str, fp_to_nodes: Dict[str, Any]) -> None:
        """整篇替换：清掉该文档旧切片记录后写入新记录。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
            conn.commit()
        self.upsert_chunks(doc_id, fp_to_nodes)

    def counts(self) -> Dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            docs = conn.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        return {"documents": docs, "chunks": chunks}
