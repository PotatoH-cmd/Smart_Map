#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知识库迁移 CLI：本地 Simple 索引 → Milvus（Docker standalone @ 19530）。

    # 从 backend 目录运行
    python scripts/migrate_kb_to_milvus.py --check   # 只做一致性巡检（不写数据）
    python scripts/migrate_kb_to_milvus.py           # 迁移（按主键 upsert 增量补齐）
    python scripts/migrate_kb_to_milvus.py --drop    # 重建 collection 全量重写

要点：
- 复用本地存量向量（default__vector_store.json），不重复调用 embedding API；
- 稀疏统计（jieba/BM25）用全语料拟合一次并缓存到 data/bm25_stats.json；
- 以 chunk_fp（切片指纹）为准复核 docstore 与 Milvus 的一致性。
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_kb")

BACKEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _load_env():
    """加载 backend/.env（DashScope key 等）。"""
    path = os.path.join(BACKEND_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_check(args):
    """一致性巡检：docstore 与 Milvus 的节点/指纹对比。"""
    from kb_milvus import MilvusKBStore, load_local_docstore, verify_consistency

    persist_dir = os.path.abspath(
        args.persist_dir
        or os.environ.get("LLAMAINDEX_PERSIST_DIR", "./llama_index_storage")
    )
    docstore = load_local_docstore(persist_dir)
    store = MilvusKBStore(uri=args.uri, collection=args.collection)
    if not store.collection_ready():
        print(f"collection 不存在：{store.collection}（先执行迁移）")
        raise SystemExit(2)
    report = verify_consistency(docstore, store)
    _print(report)
    raise SystemExit(0 if report["consistent"] else 1)


def cmd_migrate(args):
    from kb_milvus import BM25SparseEncoder, MilvusKBStore, migrate_from_local

    encoder = None
    if not args.no_sparse:
        encoder = BM25SparseEncoder()
    store = MilvusKBStore(uri=args.uri, collection=args.collection)

    embed_fn = None
    if args.embed_missing:
        # 缺失向量现算（可选；默认关闭——本地存量应齐全，缺失先人工排查）
        from llama_index.core import Settings
        from llama_index.embeddings.dashscope import DashScopeEmbedding

        Settings.embed_model = DashScopeEmbedding(
            model_name=os.environ.get("LLAMAINDEX_EMBED_MODEL", "text-embedding-v3"),
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        )
        embed_fn = lambda t: list(Settings.embed_model.get_text_embedding(t))  # noqa: E731

    stats = migrate_from_local(
        persist_dir=args.persist_dir,
        store=store,
        encoder=encoder,
        batch_size=args.batch,
        drop=args.drop,
        embed_fn=embed_fn,
        refit_sparse=args.refit_sparse,
    )
    verify = stats.pop("verify", {})
    _print(stats)
    print("── 一致性复核 ──")
    _print(verify)
    print(f"collection_stats: ")
    _print(store.collection_stats())
    raise SystemExit(0 if verify.get("consistent") else 1)


def main():
    parser = argparse.ArgumentParser(description="知识库 Simple → Milvus 迁移")
    parser.add_argument("--check", action="store_true", help="只做一致性巡检，不写数据")
    parser.add_argument("--persist-dir", default=None, help="本地索引目录（默认 llama_index_storage）")
    parser.add_argument("--uri", default=None, help="Milvus URI（默认 http://127.0.0.1:19530）")
    parser.add_argument("--collection", default=None, help="collection 名（默认 map_kb）")
    parser.add_argument("--batch", type=int, default=200, help="写入批大小")
    parser.add_argument("--drop", action="store_true", help="重建 collection（清空重写）")
    parser.add_argument("--no-sparse", action="store_true", help="不生成稀疏向量（仅稠密）")
    parser.add_argument("--refit-sparse", action="store_true", help="强制重建 BM25 统计缓存")
    parser.add_argument("--embed-missing", action="store_true", help="本地缺失向量时调用 DashScope 现算")
    args = parser.parse_args()

    _load_env()
    if args.check:
        cmd_check(args)
    else:
        cmd_migrate(args)


if __name__ == "__main__":
    main()
