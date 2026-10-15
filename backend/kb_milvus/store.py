# -*- coding: utf-8 -*-
"""Milvus 原生读写层：schema/索引管理、upsert、删除、混合检索、统计。

为什么直连 pymilvus（不用 llama_index 官方 MilvusVectorStore）：
  1. 官方实现 `stores_text=True` 会让 VectorStoreIndex 跳过 docstore 写入，
     与本库「docstore 为文本真相来源」的架构冲突（曾致「报成功但查不到」）。
     本模块配套的 LlamaIndex 适配器声明 `stores_text=False`，规避该问题；
  2. 官方 schema 固定（metadata 塞在 _node_content JSON 里），无法做
     标量列索引 / 分区键 / 稀疏向量 / JSON 字段等 Milvus 原生能力。

本层用到的 Milvus 特性：
  - HNSW 稠密索引（COSINE）+ 查询期 ef 调参
  - SPARSE_FLOAT_VECTOR + SPARSE_INVERTED_INDEX（jieba+BM25 稀疏向量，见 sparse.py）
  - hybrid_search：稠密 + 稀疏双路，RRFRanker(60) 服务端融合
  - partition key：project 字段（按项目分区剪枝），空串进独立分区（实测可行）
  - INVERTED 标量索引：document_id / chunk_fp / chunk_type / created_at
  - JSON 字段：meta_json（全量 metadata 备份）
  - upsert：同主键覆盖去重（实测无重复行）
  - query_iterator：全量流式导出（一致性校验用）
  - 一致性：写入后 flush；检索默认 Strong（保证「添加文档后立即可搜」）

实测坑（Milvus 2.4.15 + pymilvus 2.6.8）：
  - hybrid_search 的外层 `filter` / `filter_params` 参数不生效，
    过滤表达式必须挂在每个 AnnSearchRequest 的 `expr` 上；
  - upsert 后立即 query 可能读到旧值（Bounded 一致性），需 flush + Strong。
"""

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from pymilvus import (
        AnnSearchRequest,
        CollectionSchema,
        DataType,
        FieldSchema,
        MilvusClient,
        RRFRanker,
    )
    _PYMILVUS_OK = True
except ImportError:  # 与项目对第三方依赖的容错约定一致：导入失败延迟到调用时报错
    _PYMILVUS_OK = False

DEFAULT_URI = "http://127.0.0.1:19530"
DEFAULT_COLLECTION = "map_kb"
DEFAULT_DIM = 1024
DEFAULT_CONSISTENCY = "Bounded"      # collection 写入侧
DEFAULT_QUERY_CONSISTENCY = "Strong"  # 检索侧：读己之写

# 标量列（node.metadata → 列）；project 为 partition key（要求所有行必填）
SCALAR_COLUMNS = (
    "project",
    "project_name",
    "document_id",
    "title",
    "chunk_type",
    "breadcrumb",
    "section_no",
    "chunk_fp",
    "source_file",
    "created_at",
)
# 各字符串列最大字符数（防超长写入报错）
_COL_MAX_LEN = {
    "project": 64,
    "project_name": 128,
    "document_id": 512,
    "title": 1024,
    "chunk_type": 64,
    "breadcrumb": 4096,
    "section_no": 64,
    "chunk_fp": 64,
    "source_file": 1024,
    "created_at": 64,
    "text": 60000,
}
# 全量导出字段（一致性校验 / 迁移核对）
FULL_EXPORT_FIELDS = ["pk", "chunk_fp", "document_id", "project", "title"]


def _require_pymilvus():
    if not _PYMILVUS_OK:
        raise RuntimeError("pymilvus 未安装（pip install pymilvus>=2.4）")


def _truncate(val: Any, col: str) -> str:
    s = "" if val is None else str(val)
    limit = _COL_MAX_LEN.get(col)
    if limit and len(s) > limit:
        s = s[:limit]
    return s


def _sanitize_json(meta: Dict[str, Any]) -> Dict[str, Any]:
    """metadata 保证转成 JSON 可序列化 dict（丢弃不可序列化的值）。"""
    out: Dict[str, Any] = {}
    for k, v in (meta or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, (list, dict)):
            try:
                json.dumps(v, ensure_ascii=False)
                out[str(k)] = v
            except (TypeError, ValueError):
                out[str(k)] = str(v)
        else:
            out[str(k)] = str(v)
    return out


def build_row(
    node_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]],
    embedding: Optional[List[float]],
    encoder=None,
) -> Dict[str, Any]:
    """node → Milvus 行（dense + sparse + 标量 + JSON + 文本）。"""
    meta = metadata or {}
    row: Dict[str, Any] = {
        "pk": _truncate(node_id, "document_id") or "",
        "vector": [float(x) for x in (embedding or [])],
        "sparse": (encoder.encode_document(text) if encoder is not None else {}),
        "text": _truncate(text, "text"),
        "meta_json": _sanitize_json(meta),
    }
    for col in SCALAR_COLUMNS:
        row[col] = _truncate(meta.get(col), col)
    return row


def escape_expr_value(value: Any) -> str:
    """转义 expr 字符串字面量（防注入）；None → 空串。"""
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace('"', '\\"')


class MilvusKBStore:
    """知识库 Milvus 存储访问（进程内单例由外层保证）。"""

    def __init__(
        self,
        uri: Optional[str] = None,
        collection: Optional[str] = None,
        dim: Optional[int] = None,
        consistency: Optional[str] = None,
        query_consistency: Optional[str] = None,
        ef: Optional[int] = None,
    ):
        self.uri = uri or os.environ.get("MILVUS_URI", DEFAULT_URI)
        self.collection = collection or os.environ.get("KB_MILVUS_COLLECTION", DEFAULT_COLLECTION)
        self.dim = int(dim or os.environ.get("KB_EMBED_DIM", str(DEFAULT_DIM)))
        self.consistency = consistency or os.environ.get("KB_MILVUS_CONSISTENCY", DEFAULT_CONSISTENCY)
        self.query_consistency = query_consistency or os.environ.get(
            "KB_MILVUS_SEARCH_CONSISTENCY", DEFAULT_QUERY_CONSISTENCY
        )
        self.ef = int(ef or os.environ.get("KB_MILVUS_EF", "64"))
        self._client: Optional[Any] = None

    # ─────────────────────────────────────────────
    # 连接与 schema
    # ─────────────────────────────────────────────

    @property
    def client(self):
        if self._client is None:
            _require_pymilvus()
            self._client = MilvusClient(uri=self.uri)
        return self._client

    def ping(self) -> bool:
        try:
            return bool(self.client.get_server_version())
        except Exception as e:
            logger.warning(f"[Milvus] 连接失败 {self.uri}: {e}")
            return False

    def collection_ready(self) -> bool:
        try:
            return self.client.has_collection(self.collection)
        except Exception:
            return False

    def _schema(self):
        fields = [
            FieldSchema("pk", DataType.VARCHAR, is_primary=True, max_length=512),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=self.dim),
            FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
            FieldSchema("project", DataType.VARCHAR, max_length=64, is_partition_key=True),
        ]
        for col in SCALAR_COLUMNS:
            if col == "project":
                continue
            fields.append(FieldSchema(col, DataType.VARCHAR, max_length=_COL_MAX_LEN[col]))
        fields.append(FieldSchema("text", DataType.VARCHAR, max_length=60000))
        fields.append(FieldSchema("meta_json", DataType.JSON))
        return CollectionSchema(fields)

    def ensure_collection(self, drop: bool = False) -> bool:
        """建 collection + 索引（幂等）；drop=True 时先删后建（迁移重建用）。"""
        _require_pymilvus()
        client = self.client
        if drop and client.has_collection(self.collection):
            client.drop_collection(self.collection)
            logger.warning(f"[Milvus] 已删除 collection {self.collection}（drop=True）")
        if client.has_collection(self.collection):
            return False
        index_params = client.prepare_index_params()
        # 稠密：HNSW + COSINE（DashScope text-embedding-v3 归一化向量）
        index_params.add_index(
            "vector", index_type="HNSW", metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        # 稀疏：倒排索引 + 内积（BM25 权重分数）
        index_params.add_index(
            "sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP",
            params={"drop_ratio_build": 0.0},
        )
        # 标量：INVERTED（等值过滤 / 删除按 document_id）
        for col in ("document_id", "chunk_fp", "chunk_type", "created_at"):
            index_params.add_index(col, index_type="INVERTED")
        client.create_collection(
            self.collection,
            schema=self._schema(),
            index_params=index_params,
            consistency_level=self.consistency,
        )
        logger.info(
            f"[Milvus] 已创建 collection {self.collection}"
            f"（HNSW+SPARSE_INVERTED+INVERTED，partition key=project，dim={self.dim}）"
        )
        return True

    def drop_collection(self) -> None:
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)
            logger.warning(f"[Milvus] 已删除 collection {self.collection}")

    # ─────────────────────────────────────────────
    # 写入
    # ─────────────────────────────────────────────

    def upsert_rows(self, rows: List[Dict[str, Any]], flush: bool = True) -> int:
        """upsert 行（同 pk 覆盖）；默认 flush 保证写入立即可见。"""
        if not rows:
            return 0
        client = self.client
        if not client.has_collection(self.collection):
            self.ensure_collection()
        client.upsert(self.collection, data=rows)
        if flush:
            client.flush(self.collection)
        return len(rows)

    def upsert_batched(
        self, rows: Iterable[Dict[str, Any]], batch_size: int = 200, flush: bool = True
    ) -> int:
        """分批 upsert（迁移/修复大批量写入用）。"""
        total = 0
        batch: List[Dict[str, Any]] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                total += self.upsert_rows(batch, flush=False)
                batch = []
        if batch:
            total += self.upsert_rows(batch, flush=False)
        if flush and total:
            self.client.flush(self.collection)
        return total

    def delete_node_ids(self, node_ids: List[str]) -> int:
        """按主键删除；返回请求删除的条数。"""
        ids = [i for i in (node_ids or []) if i]
        if not ids:
            return 0
        self.client.delete(self.collection, ids=ids)
        return len(ids)

    def delete_by_document(self, document_id: str) -> None:
        """按 document_id 整篇删除（expr 走 INVERTED 索引）。"""
        if not document_id:
            return
        expr = f'document_id == "{escape_expr_value(document_id)}"'
        self.client.delete(self.collection, filter=expr)

    def delete_by_expr(self, expr: str) -> None:
        self.client.delete(self.collection, filter=expr)

    def flush(self) -> None:
        try:
            self.client.flush(self.collection)
        except Exception as e:
            logger.warning(f"[Milvus] flush 失败: {e}")

    # ─────────────────────────────────────────────
    # 读取
    # ─────────────────────────────────────────────

    def count(self) -> int:
        rows = self.client.query(
            self.collection, filter="", output_fields=["count(*)"],
            consistency_level=self.query_consistency,
        )
        return int(rows[0]["count(*)"]) if rows else 0

    def iter_rows(
        self, fields: Optional[List[str]] = None, expr: str = "", batch_size: int = 500
    ) -> Iterable[Dict[str, Any]]:
        """全量/条件流式导出（query_iterator，绕过 limit 上限）。"""
        fields = fields or FULL_EXPORT_FIELDS
        it = self.client.query_iterator(
            self.collection, batch_size=batch_size, filter=expr, output_fields=fields,
            consistency_level=self.query_consistency,
        )
        try:
            while True:
                batch = it.next()
                if not batch:
                    break
                for row in batch:
                    yield row
        finally:
            try:
                it.close()
            except Exception:
                pass

    def export_id_fp(self, batch_size: int = 500) -> Dict[str, str]:
        """导出 {pk: chunk_fp}（一致性差分用）。"""
        out: Dict[str, str] = {}
        for row in self.iter_rows(fields=["pk", "chunk_fp"], batch_size=batch_size):
            out[str(row["pk"])] = str(row.get("chunk_fp") or "")
        return out

    def get_rows(self, expr: str, fields: Optional[List[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self.client.query(
            self.collection, filter=expr, output_fields=fields, limit=limit,
            consistency_level=self.query_consistency,
        )

    def collection_stats(self) -> Dict[str, Any]:
        """统计信息（count / 分区数 / 索引概览），供运维与校验展示。"""
        out: Dict[str, Any] = {"collection": self.collection, "uri": self.uri}
        try:
            out["count"] = self.count()
            desc = self.client.describe_collection(self.collection)
            out["consistency_level"] = desc.get("consistency_level")
            idx = self.client.list_indexes(self.collection)
            out["indexes"] = list(idx)
        except Exception as e:
            out["error"] = str(e)
        return out

    # ─────────────────────────────────────────────
    # 混合检索
    # ─────────────────────────────────────────────

    def hybrid_search(
        self,
        dense: List[float],
        sparse: Optional[Dict[int, float]] = None,
        top_k: int = 10,
        expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """稠密 + 稀疏双路检索，RRFRanker(60) 服务端融合。

        - 过滤必须挂在每个 AnnSearchRequest.expr 上（外层 filter 参数不生效，实测）。
        - 稀疏向量为空（分词后无有效词）时退化为纯稠密检索。
        - 返回 [{pk, score, fields}]，fields 含 output_fields 指定列。
        """
        client = self.client
        fields = output_fields or (
            ["pk", "document_id", "project", "title", "chunk_type", "breadcrumb",
             "section_no", "chunk_fp", "source_file", "created_at", "text", "meta_json"]
        )
        dense_req = AnnSearchRequest(
            data=[list(dense)], anns_field="vector",
            param={"metric_type": "COSINE", "params": {"ef": self.ef}},
            limit=top_k, expr=expr,
        )
        if sparse:
            reqs = [dense_req, AnnSearchRequest(
                data=[sparse], anns_field="sparse", param={"metric_type": "IP"},
                limit=top_k, expr=expr,
            )]
            res = client.hybrid_search(
                self.collection, reqs=reqs, ranker=RRFRanker(60), limit=top_k,
                output_fields=fields, consistency_level=self.query_consistency,
            )
        else:
            res = client.search(
                self.collection, data=[list(dense)], anns_field="vector",
                search_params={"metric_type": "COSINE", "params": {"ef": self.ef}},
                limit=top_k, filter=expr, output_fields=fields,
                consistency_level=self.query_consistency,
            )
        hits: List[Dict[str, Any]] = []
        for hit in res[0]:
            hits.append({
                "pk": str(hit.get("id")),
                "score": float(hit.get("distance", 0.0)),
                "fields": dict(hit.get("entity") or {}),
            })
        return hits
