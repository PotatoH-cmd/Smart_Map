"""过期检测：找出「源文件已改、但库内仍是旧版」的文档。

判定依据是源文件 mtime 与入库时记录的 source_mtime 之差，
带 1 秒容差避免同秒写入造成误报。

不做后台自动扫描：嵌入调用有成本，改为显式触发（API 查询 / CLI --fix），
避免静默消耗 embedding 配额。
"""

import os
from typing import Any, Dict, List

# mtime 容差：1 秒。同一秒内的写入不应判为过期。
MTIME_TOLERANCE = 1.0


def scan_stale(manifest) -> List[Dict[str, Any]]:
    """返回过期文档列表。

    无 source_file 的文档（如 API 上传、历史 backfill 记录）跳过——
    没有源文件就无从比对新鲜度。
    """
    stale: List[Dict[str, Any]] = []
    for doc in manifest.all_documents():
        path = doc.get("source_file")
        db_mtime = doc.get("source_mtime")
        if not path or not os.path.isfile(path):
            continue
        fs_mtime = os.path.getmtime(path)
        if fs_mtime > (db_mtime or 0) + MTIME_TOLERANCE:
            stale.append(
                {
                    "doc_id": doc.get("doc_id"),
                    "name": doc.get("name"),
                    "file": path,
                    "db_mtime": db_mtime,
                    "fs_mtime": fs_mtime,
                    "chunk_count": doc.get("chunk_count", 0),
                }
            )
    return stale


def mark_fresh(manifest, doc_id: str, source_file: str = None) -> None:
    """把某文档标记为「与源文件同步」，更新 source_mtime。"""
    doc = manifest.lookup("doc_id", doc_id)
    if not doc:
        return
    path = source_file or doc.get("source_file")
    if not path or not os.path.isfile(path):
        return
    manifest.upsert_document(
        doc_id=doc_id,
        name=doc.get("name") or "",
        content_hash=doc.get("content_hash") or "",
        chunk_count=doc.get("chunk_count") or 0,
        source=doc.get("source") or "",
        source_file=path,
        source_mtime=os.path.getmtime(path),
    )
