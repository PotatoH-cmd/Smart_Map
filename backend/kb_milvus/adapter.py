# -*- coding: utf-8 -*-
"""LlamaIndex 向量存储适配器 — 让 VectorStoreIndex 使用 Milvus 后端。

设计要义（与官方 MilvusVectorStore 的关键差异）：

1. `stores_text = False`：告诉 VectorStoreIndex「向量存储不负责文本」，
   于是 docstore 照常写入 —— 保持本库「docstore 为文本真相来源、Milvus 为向量副本」
   的架构。官方实现 stores_text=True 会让 docstore 静默跳过写入
   （表现为「写成功但列表/删除查无此片」的历史坑）。
2. 写入 add() → Milvus upsert（dense + jieba/BM25 sparse + 标量列 + meta_json），
   官方实现把 metadata 塞进 _node_content JSON，无法建标量索引与过滤。
3. 检索 query() → Milvus hybrid_search（稠密 + 稀疏，RRFRanker 服务端融合），
   MetadataFilters 转成 expr 前置过滤（走 partition key 剪枝 / INVERTED 索引）。
"""

import logging
import os
from typing import Any, List, Optional, Sequence

from llama_index.core.schema import BaseNode, MetadataMode, TextNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
    VectorStoreQuery,
    VectorStoreQueryResult,
)
from pydantic import PrivateAttr

from .sparse import BM25SparseEncoder
from .store import MilvusKBStore, build_row, escape_expr_value

logger = logging.getLogger(__name__)

# 允许转 expr 的 metadata 过滤键（白名单防注入；其余键忽略并告警）
_ALLOWED_FILTER_KEYS = {
    "project", "document_id", "chunk_type", "chunk_fp",
    "section_no", "title", "source_file", "created_at",
}


def _op_value(op: Any) -> str:
    return str(getattr(op, "value", op))


def _filter_to_expr(f: MetadataFilter) -> Optional[str]:
    key = str(getattr(f, "key", "") or "")
    if key not in _ALLOWED_FILTER_KEYS:
        logger.warning(f"[Milvus] 忽略不支持的过滤键: {key}")
        return None
    op = _op_value(getattr(f, "operator", FilterOperator.EQ))
    val = getattr(f, "value", None)
    if op == "==":
        return f'{key} == "{escape_expr_value(val)}"'
    if op == "!=":
        return f'{key} != "{escape_expr_value(val)}"'
    if op == "in":
        vals = val if isinstance(val, (list, tuple, set)) else [val]
        items = ", ".join(f'"{escape_expr_value(v)}"' for v in vals)
        return f"{key} in [{items}]"
    logger.warning(f"[Milvus] 忽略不支持的过滤操作符: {key} {op}")
    return None


def _filters_to_expr(filters: Optional[MetadataFilters]) -> Optional[str]:
    """MetadataFilters → Milvus expr（支持嵌套、AND/OR、EQ/NE/IN）。"""
    if not filters:
        return None
    parts: List[str] = []
    cond = _op_value(getattr(filters, "condition", FilterCondition.AND)).lower()
    joiner = " or " if cond == "or" else " and "
    for f in getattr(filters, "filters", []) or []:
        if isinstance(f, MetadataFilters):
            sub = _filters_to_expr(f)
            if sub:
                parts.append(f"({sub})")
        elif isinstance(f, MetadataFilter):
            expr = _filter_to_expr(f)
            if expr:
                parts.append(expr)
    return joiner.join(parts) if parts else None


def _fields_to_metadata(fields: dict) -> dict:
    """Milvus 行 → node.metadata（meta_json 全量 + 标量列覆盖，保证关键键存在）。"""
    meta = {}
    raw = fields.get("meta_json")
    if isinstance(raw, dict):
        meta.update(raw)
    elif isinstance(raw, str) and raw:
        try:
            import json

            meta.update(json.loads(raw))
        except Exception:
            pass
    for key in ("document_id", "title", "project", "project_name", "chunk_type",
                "breadcrumb", "section_no", "chunk_fp", "source_file", "created_at"):
        val = fields.get(key)
        if val not in (None, ""):
            meta[key] = val
    return meta


class LlamaIndexMilvusVectorStore(BasePydanticVectorStore):
    """LlamaIndex 侧 Milvus 向量存储（stores_text=False，docstore 仍由 LlamaIndex 管理）。"""

    stores_text: bool = False
    uri: str = ""
    collection: str = ""
    dim: int = 0

    _store: MilvusKBStore = PrivateAttr()
    _encoder: Optional[BM25SparseEncoder] = PrivateAttr(default=None)

    def __init__(
        self,
        uri: Optional[str] = None,
        collection: Optional[str] = None,
        dim: Optional[int] = None,
        encoder: Optional[BM25SparseEncoder] = None,
        **kwargs: Any,
    ):
        store = MilvusKBStore(uri=uri, collection=collection, dim=dim)
        super().__init__(
            uri=store.uri, collection=store.collection, dim=store.dim, **kwargs
        )
        self._store = store
        self._encoder = encoder

    # ── 构造 ──

    @classmethod
    def from_env(cls, encoder: Optional[BM25SparseEncoder] = None) -> "LlamaIndexMilvusVectorStore":
        """按环境变量构造；KB_SPARSE_ENABLE=1（默认）时启用 jieba/BM25 稀疏向量。"""
        if encoder is None and os.environ.get("KB_SPARSE_ENABLE", "1") == "1":
            encoder = BM25SparseEncoder()
            if not encoder.load():
                logger.info("[Milvus] BM25 统计缓存不存在；编码退化为 tf 权重，待迁移/同步时拟合")
        if encoder is not None:
            try:  # 探测 jieba 可用性，避免检索时才炸
                encoder._cut("初始化分词自检")
            except Exception as e:
                logger.warning(f"[Milvus] 稀疏编码器不可用，仅启用稠密检索: {e}")
                encoder = None
        return cls(encoder=encoder)

    # ── 访问器 ──

    @property
    def client(self):
        return self._store.client

    @property
    def milvus(self) -> MilvusKBStore:
        return self._store

    @property
    def bm25_encoder(self) -> Optional[BM25SparseEncoder]:
        return self._encoder

    def set_encoder(self, encoder: Optional[BM25SparseEncoder]) -> None:
        self._encoder = encoder

    # ── 写入 ──

    def add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> List[str]:
        """节点（含 embedding）upsert 到 Milvus；返回节点 id 列表（索引结构映射用）。"""
        rows, ids = [], []
        for node in nodes:
            emb = getattr(node, "embedding", None)
            if not emb:
                raise ValueError(f"节点 {node.node_id} 缺少 embedding，拒绝写入 Milvus")
            text = getattr(node, "text", None) or node.get_content(metadata_mode=MetadataMode.NONE)
            rows.append(build_row(node.node_id, text, dict(node.metadata or {}), emb, self._encoder))
            ids.append(node.node_id)
        if rows:
            self._store.upsert_rows(rows, flush=True)
            logger.info(f"[Milvus] 写入 {len(rows)} 个节点（dense{' + sparse' if self._encoder else ''}）")
        return ids

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """兼容 delete_ref_doc 语义：ref_doc_id 可能是节点 id 或文档 id，两者都尝试。"""
        if not ref_doc_id:
            return
        self._store.delete_node_ids([ref_doc_id])
        self._store.delete_by_document(ref_doc_id)
        self._store.flush()

    def delete_nodes(
        self,
        node_ids: Optional[List[str]] = None,
        filters: Optional[MetadataFilters] = None,
        **delete_kwargs: Any,
    ) -> None:
        if node_ids:
            n = self._store.delete_node_ids(list(node_ids))
            self._store.flush()
            logger.info(f"[Milvus] 删除 {n} 个节点")
        elif filters:
            expr = _filters_to_expr(filters)
            if expr:
                self._store.delete_by_expr(expr)
                self._store.flush()

    # ── 检索 ──

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        dense = list(query.query_embedding or [])
        if not dense:
            raise ValueError("query_embedding 为空；Milvus 检索需要稠密向量")
        sparse = None
        if self._encoder is not None and query.query_str:
            try:
                sparse = self._encoder.encode_query(query.query_str) or None
            except Exception as e:
                logger.warning(f"[Milvus] 稀疏编码失败（退化为纯稠密）: {e}")
        expr = _filters_to_expr(query.filters)
        top_k = int(query.similarity_top_k or 10)
        hits = self._store.hybrid_search(dense, sparse, top_k=top_k, expr=expr)

        nodes, similarities, ids = [], [], []
        for hit in hits:
            fields = hit["fields"]
            node = TextNode(id_=hit["pk"], text=fields.get("text") or "", metadata=_fields_to_metadata(fields))
            nodes.append(node)
            similarities.append(hit["score"])
            ids.append(hit["pk"])
        logger.info(
            f"[Milvus] 检索 top_k={top_k}{' +sparse' if sparse else ''}"
            f"{' expr=' + expr if expr else ''} → {len(nodes)} 条"
        )
        return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
