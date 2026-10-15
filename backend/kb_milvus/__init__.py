# -*- coding: utf-8 -*-
"""kb_milvus — 知识库 Milvus 向量层（原生 pymilvus）。

子模块职责：
  store.py    MilvusKBStore：collection/索引管理、upsert、删除、混合检索、统计
  sparse.py   BM25SparseEncoder：jieba + BM25 稀疏向量（SPARSE_FLOAT_VECTOR）
  adapter.py  LlamaIndexMilvusVectorStore：LlamaIndex 适配器（stores_text=False，
              保持 docstore 为真相来源）
  sync.py     一致性校验 / 修复 / 本地索引迁移

公共 API 在包级集中 re-export，子模块仅保留内部实现（与项目 kb_pipeline /
kg_pipeline 的导出约定一致）。
"""

from .sparse import BM25SparseEncoder
from .store import (
    DEFAULT_COLLECTION,
    DEFAULT_DIM,
    DEFAULT_URI,
    FULL_EXPORT_FIELDS,
    MilvusKBStore,
    SCALAR_COLUMNS,
    build_row,
    escape_expr_value,
)
from .adapter import LlamaIndexMilvusVectorStore
from .sync import (
    load_local_docstore,
    load_local_embeddings,
    migrate_from_local,
    node_fingerprint,
    prepare_meta,
    repair_from_docstore,
    verify_consistency,
)

__all__ = [
    # store
    "MilvusKBStore",
    "build_row",
    "escape_expr_value",
    "SCALAR_COLUMNS",
    "FULL_EXPORT_FIELDS",
    "DEFAULT_URI",
    "DEFAULT_COLLECTION",
    "DEFAULT_DIM",
    # sparse
    "BM25SparseEncoder",
    # adapter
    "LlamaIndexMilvusVectorStore",
    # sync
    "node_fingerprint",
    "prepare_meta",
    "load_local_docstore",
    "load_local_embeddings",
    "verify_consistency",
    "repair_from_docstore",
    "migrate_from_local",
]
