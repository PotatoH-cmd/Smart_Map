"""增量合并：把「新解析出的切片」与「库内已有切片」做差分并落库。

被 ingest_*.py 批量脚本与 KnowledgeBaseTool 的 API 路径共用，
避免两处各自实现一套增量语义。

用法（批量脚本）：
    from kb_pipeline import Manifest
    from kb_pipeline.applier import apply_incremental
    apply_incremental(kb, manifest, doc_id, doc_title, chunks, source_file=md_path)

用法（API 路径）：
    apply_incremental(kb, self._manifest, base_id, file_name, nodes, source_file=path)
"""

import os
from typing import Any, Dict, List, Optional

from .fingerprint import fingerprint_of_meta
from .merger import diff_chunks


def _delete_nodes_by_fp(kb, doc_id: str, fps, manifest) -> int:
    """按指纹删除 docstore 中的节点，并联动清清单与向量。"""
    node_ids = manifest.delete_chunks_by_fp(doc_id, fps)
    removed = 0
    if node_ids:
        try:
            kb._index.delete_nodes(node_ids, delete_from_docstore=True)
            removed = len(node_ids)
        except Exception:
            pass
    return removed


def apply_incremental(
    kb,
    manifest,
    doc_id: str,
    doc_title: str,
    nodes: List[Any],
    source_file: Optional[str] = None,
    source: str = "ingest_script",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """对新切片集合做增量 merge。

    nodes 需已带好 metadata（含 breadcrumb / section_no）；
    本函数会为每个节点补写 chunk_fp，并按差集决定插/删/留。

    返回统计：{keep, insert, delete, removed, source_file}
    """
    # 1) 给新节点打指纹（fp → [node_id…]，允许同批内出现同指纹的重复切片）
    fp_to_nodes = {}
    for n in nodes:
        meta = getattr(n, "metadata", None) or {}
        fp = fingerprint_of_meta(meta, getattr(n, "text", "") or n.get_content())
        meta["chunk_fp"] = fp
        n.metadata = meta
        if "chunk_fp" not in (n.excluded_embed_metadata_keys or []):
            n.excluded_embed_metadata_keys = list(
                n.excluded_embed_metadata_keys or []
            ) + ["chunk_fp"]
        if "chunk_fp" not in (n.excluded_llm_metadata_keys or []):
            n.excluded_llm_metadata_keys = list(
                n.excluded_llm_metadata_keys or []
            ) + ["chunk_fp"]
        fp_to_nodes.setdefault(fp, []).append(n.node_id)

    new_fps = set(fp_to_nodes)

    # 2) 取库内旧指纹并求差
    old_fps = manifest.chunk_fps(doc_id)
    d = diff_chunks(old_fps, new_fps)

    # 2.1) 清理库内重复切片：同指纹保留一个 node_id，多余的删除
    #      （历史 insert_nodes 重复执行留下的脏数据）
    old_nodes = manifest.node_ids_of(doc_id)
    redundant = [nid for nids in old_nodes.values() for nid in nids[1:]]

    stats = {
        "doc_id": doc_id,
        "keep": len(d["keep"]),
        "insert": len(d["insert"]),
        "delete": len(d["delete"]),
        "redundant": len(redundant),
        "removed": 0,
        "source_file": source_file,
        "dry_run": dry_run,
    }
    if dry_run:
        return stats

    # 3) 删除冗余与过期切片
    if redundant:
        manifest.delete_chunks_by_node(redundant)
        try:
            kb._index.delete_nodes(redundant, delete_from_docstore=True)
            stats["removed"] += len(redundant)
        except Exception:
            pass
    if d["delete"]:
        stats["removed"] += _delete_nodes_by_fp(kb, doc_id, d["delete"], manifest)

    # 4) 只插入新增切片（keep 集完全不碰，避免无谓重嵌入）
    to_insert = [n for n in nodes if n.metadata.get("chunk_fp") in d["insert"]]
    if to_insert:
        kb._index.insert_nodes(to_insert)

    # 5) 落清单：keep 集沿用库内已有 node_id（保留原向量），其余用新节点
    final_map = {}
    for fp, nids in fp_to_nodes.items():
        if fp in d["keep"] and old_nodes.get(fp):
            final_map[fp] = old_nodes[fp][0]
        else:
            final_map[fp] = nids
    manifest.replace_document_chunks(doc_id, final_map)

    # 6) 文档级记录（含源文件 mtime，供过期检测）
    mtime = None
    if source_file and os.path.isfile(source_file):
        mtime = os.path.getmtime(source_file)
    manifest.upsert_document(
        doc_id=doc_id,
        name=doc_title,
        chunk_count=len(final_map),
        source=source,
        source_file=source_file,
        source_mtime=mtime,
    )
    return stats


def full_rebuild(
    kb,
    manifest,
    doc_id: str,
    doc_title: str,
    nodes: List[Any],
    source_file: Optional[str] = None,
    source: str = "ingest_script",
) -> Dict[str, Any]:
    """整篇重建：删旧 ref_doc 后全量插入，清单整体替换。"""
    try:
        kb._index.delete_ref_doc(doc_id, delete_from_docstore=True)
    except Exception:
        pass

    fp_to_nodes = {}
    for n in nodes:
        meta = getattr(n, "metadata", None) or {}
        fp = fingerprint_of_meta(meta, getattr(n, "text", "") or n.get_content())
        meta["chunk_fp"] = fp
        n.metadata = meta
        fp_to_nodes.setdefault(fp, []).append(n.node_id)

    if nodes:
        kb._index.insert_nodes(nodes)

    manifest.replace_document_chunks(doc_id, fp_to_nodes)
    mtime = None
    if source_file and os.path.isfile(source_file):
        mtime = os.path.getmtime(source_file)
    manifest.upsert_document(
        doc_id=doc_id,
        name=doc_title,
        chunk_count=len(fp_to_nodes),
        source=source,
        source_file=source_file,
        source_mtime=mtime,
    )
    return {
        "doc_id": doc_id,
        "keep": 0,
        "insert": len(fp_to_nodes),
        "delete": 0,
        "redundant": 0,
        "removed": 0,
        "source_file": source_file,
        "rebuild": True,
    }
