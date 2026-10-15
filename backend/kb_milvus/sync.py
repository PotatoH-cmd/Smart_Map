# -*- coding: utf-8 -*-
"""docstore ↔ Milvus 一致性：校验 / 修复 / 从本地 JSON 迁移。

架构前提：docstore（llama_index_storage/docstore.json）是文本与元数据的
**真相来源**，Milvus 是向量副本（含标量列与正文快照）。本模块以
chunk_fp（kb_pipeline.fingerprint）为差异判据：

- 缺失 / 指纹不一致（docstore 有、Milvus 无或旧）→ 重新 upsert；
- 多余（Milvus 有、docstore 无）→ 删除；
- 向量来源优先级：本地向量 JSON（旧 Simple 模式存量）→ embed_fn 现场计算。

三种使用场景：
  verify_consistency    —— 只读巡检（运维/测试）
  repair_from_docstore  —— 启动自愈、异常恢复（增量修复）
  migrate_from_local    —— 一次性迁移：本地 Simple 索引 → Milvus
"""

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sparse import BM25SparseEncoder
from .store import MilvusKBStore, build_row

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llama_index_storage"
)


def node_fingerprint(meta: Dict[str, Any], text: str) -> str:
    """节点指纹（与 kb_pipeline 增量管线同源，保证口径一致）。"""
    from kb_pipeline.fingerprint import fingerprint_of_meta

    return fingerprint_of_meta(meta or {}, text or "")


def prepare_meta(meta: Optional[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """复制 metadata 并补齐 chunk_fp（Milvus 行与 docstore 的差异判据）。"""
    out = dict(meta or {})
    if not out.get("chunk_fp"):
        out["chunk_fp"] = node_fingerprint(out, text)
    return out


def load_local_docstore(persist_dir: str = DEFAULT_PERSIST_DIR):
    """加载本地 docstore（SimpleDocumentStore，含全部节点文本与 metadata）。"""
    from llama_index.core.storage.docstore import SimpleDocumentStore

    path = os.path.join(persist_dir, "docstore.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"docstore 不存在: {path}")
    return SimpleDocumentStore.from_persist_path(path)


def load_local_embeddings(persist_dir: str = DEFAULT_PERSIST_DIR) -> Dict[str, List[float]]:
    """读取本地 Simple 向量库的 {node_id: vector}（迁移/修复的向量优先来源）。"""
    path = os.path.join(persist_dir, "default__vector_store.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        emb = data.get("embedding_dict") or {}
        return {str(k): v for k, v in emb.items() if isinstance(v, list)}
    except Exception as e:
        logger.warning(f"[Milvus] 本地向量读取失败 {path}: {e}")
        return {}


def _docstore_fp_map(docstore) -> Dict[str, str]:
    """{node_id: chunk_fp}（metadata 缺失指纹时现场计算）。"""
    out: Dict[str, str] = {}
    for nid, node in (getattr(docstore, "docs", {}) or {}).items():
        meta = getattr(node, "metadata", {}) or {}
        text = getattr(node, "text", "") or node.get_content()
        out[str(nid)] = meta.get("chunk_fp") or node_fingerprint(meta, text)
    return out


def verify_consistency(docstore, store: MilvusKBStore, max_samples: int = 10) -> Dict[str, Any]:
    """对比 docstore 与 Milvus 的 (node_id, chunk_fp) 集合。"""
    doc_map = _docstore_fp_map(docstore)
    milvus_map = store.export_id_fp()
    missing = [k for k in doc_map if k not in milvus_map]
    extra = [k for k in milvus_map if k not in doc_map]
    mismatch = [k for k in doc_map if k in milvus_map and milvus_map[k] != doc_map[k]]
    report = {
        "docstore_nodes": len(doc_map),
        "milvus_rows": len(milvus_map),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "fp_mismatch_count": len(mismatch),
        "missing_sample": missing[:max_samples],
        "extra_sample": extra[:max_samples],
        "fp_mismatch_sample": mismatch[:max_samples],
    }
    report["consistent"] = not (missing or extra or mismatch)
    return report


def _resolve_vectors(
    node_ids: List[str],
    texts: Dict[str, str],
    persist_dir: Optional[str] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
) -> Tuple[Dict[str, List[float]], List[str], int]:
    """为待写节点解析向量：本地 JSON 优先，缺者用 embed_fn 现算。

    返回 (vec_map, 未能解析的 node_id 列表, 命中本地 JSON 的数量)。
    """
    vec_map: Dict[str, List[float]] = {}
    if persist_dir:
        local = load_local_embeddings(persist_dir)
        for nid in node_ids:
            if nid in local:
                vec_map[nid] = local[nid]
    from_local = len(vec_map)
    unresolved = [nid for nid in node_ids if nid not in vec_map]
    if unresolved and embed_fn is not None:
        still: List[str] = []
        for nid in unresolved:
            try:
                vec_map[nid] = list(embed_fn(texts.get(nid, "")))
            except Exception as e:
                logger.warning(f"[Milvus] 向量计算失败 {nid}: {e}")
                still.append(nid)
        unresolved = still
    return vec_map, unresolved, from_local


def _rows_for(
    node_ids: List[str],
    nodes: Dict[str, Any],
    vec_map: Dict[str, List[float]],
    encoder: Optional[BM25SparseEncoder],
) -> List[Dict[str, Any]]:
    rows = []
    for nid in node_ids:
        if nid not in vec_map:
            continue
        node = nodes[nid]
        text = getattr(node, "text", "") or node.get_content()
        meta = prepare_meta(getattr(node, "metadata", {}) or {}, text)
        rows.append(build_row(nid, text, meta, vec_map[nid], encoder))
    return rows


def repair_from_docstore(
    docstore,
    store: MilvusKBStore,
    encoder: Optional[BM25SparseEncoder] = None,
    persist_dir: Optional[str] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    batch_size: int = 200,
) -> Dict[str, Any]:
    """增量修复 Milvus 与 docstore 的差异（启动自愈 / 异常恢复）。"""
    doc_map = _docstore_fp_map(docstore)
    milvus_map = store.export_id_fp()
    need = [k for k, fp in doc_map.items() if milvus_map.get(k) != fp]  # 缺失 + 指纹变化
    extra = [k for k in milvus_map if k not in doc_map]
    if not need and not extra:
        return {"changed": False, "upserted": 0, "deleted": 0, "docstore_nodes": len(doc_map)}

    docs = getattr(docstore, "docs", {}) or {}
    texts = {
        nid: (getattr(docs[nid], "text", "") or docs[nid].get_content())
        for nid in need if nid in docs
    }
    vec_map, unresolved, _ = _resolve_vectors(need, texts, persist_dir, embed_fn)
    rows = _rows_for(need, docs, vec_map, encoder)
    upserted = store.upsert_batched(rows, batch_size=batch_size, flush=False)
    deleted = 0
    if extra:
        store.delete_node_ids(extra)
        deleted = len(extra)
    store.flush()
    stats = {
        "changed": bool(upserted or deleted),
        "upserted": upserted,
        "deleted": deleted,
        "unresolved_vectors": len(unresolved),
        "unresolved_sample": unresolved[:5],
        "docstore_nodes": len(doc_map),
    }
    logger.info(
        f"[Milvus] 一致性修复：upsert {upserted}（缺向量 {len(unresolved)}）、删除 {deleted}"
    )
    return stats


def migrate_from_local(
    persist_dir: Optional[str] = None,
    store: Optional[MilvusKBStore] = None,
    encoder: Optional[BM25SparseEncoder] = None,
    batch_size: int = 200,
    drop: bool = False,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    refit_sparse: bool = False,
) -> Dict[str, Any]:
    """一次性迁移：本地 Simple 索引（docstore + 向量 JSON）→ Milvus。

    - 复用本地存量向量（不重复调用 embedding API）；
    - 稀疏统计用全语料拟合一次并缓存；
    - drop=True 时重建 collection（清空重写），否则按主键 upsert 增量补齐。
    """
    persist_dir = persist_dir or os.environ.get("LLAMAINDEX_PERSIST_DIR", DEFAULT_PERSIST_DIR)
    persist_dir = os.path.abspath(persist_dir)
    store = store or MilvusKBStore()
    if encoder is None and os.environ.get("KB_SPARSE_ENABLE", "1") == "1":
        encoder = BM25SparseEncoder()

    docstore = load_local_docstore(persist_dir)
    docs = getattr(docstore, "docs", {}) or {}
    node_ids = list(docs.keys())
    texts = {
        nid: (getattr(docs[nid], "text", "") or docs[nid].get_content())
        for nid in node_ids
    }
    logger.info(f"[Milvus] 本地索引：{len(node_ids)} 个节点（{persist_dir}）")

    # 1) 稀疏统计（全语料拟合，缓存复用）
    if encoder is not None:
        if refit_sparse or not encoder.load():
            encoder.fit(texts.values())

    # 2) 向量（本地存量优先；缺失可选现算）
    vec_map, unresolved, from_local = _resolve_vectors(node_ids, texts, persist_dir, embed_fn)

    # 3) 建 collection 并批量写入
    created = store.ensure_collection(drop=drop)
    rows = _rows_for(node_ids, docs, vec_map, encoder)
    upserted = store.upsert_batched(rows, batch_size=batch_size, flush=True)

    # 4) 复核
    report = verify_consistency(docstore, store)
    stats = {
        "persist_dir": persist_dir,
        "collection": store.collection,
        "collection_created": created,
        "nodes_total": len(node_ids),
        "vectors_from_local": from_local,
        "vectors_embedded": max(len(vec_map) - from_local, 0),
        "upserted": upserted,
        "unresolved_vectors": len(unresolved),
        "verify": report,
    }
    logger.info(
        f"[Milvus] 迁移完成：upsert {upserted}/{len(node_ids)}，"
        f"缺向量 {len(unresolved)}，consistent={report['consistent']}"
    )
    return stats
