#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RagFlow → LlamaIndex 知识库数据迁移脚本

从 RagFlow API 批量导出所有文档，逐条导入 LlamaIndex 本地索引。

用法:
    # 预览（仅导出，不导入）
    python scripts/migrate_kb_to_llamaindex.py --dry-run

    # 执行迁移
    python scripts/migrate_kb_to_llamaindex.py

    # 断点续传（跳过已存在的文档）
    python scripts/migrate_kb_to_llamaindex.py --resume

    # 全量重建
    python scripts/migrate_kb_to_llamaindex.py --rebuild
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 确保可以导入 tools 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests


# ── RagFlow 配置 ──
RAGFLOW_API_KEY = os.environ.get(
    "RAGFLOW_API_KEY", "ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
)
RAGFLOW_API_BASE = os.environ.get(
    "RAGFLOW_API_BASE", "http://172.136.16.14:8080/api/v1"
)
RAGFLOW_DATASET_ID = os.environ.get(
    "RAGFLOW_DATASET_ID", "538b0a5c36ff11f18e7d3d43671e73e4"
)


def _ragflow_request(method: str, path: str, **kwargs) -> dict:
    """封装 RagFlow API 请求"""
    url = f"{RAGFLOW_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {RAGFLOW_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"RagFlow error: {data.get('message')}")
    return data


def export_all_documents() -> list:
    """从 RagFlow 导出所有文档及其内容"""
    print("=" * 60)
    print("📤 从 RagFlow 导出文档...")
    print("=" * 60)

    all_docs = []
    page = 1
    page_size = 100

    # Step 1: 获取文档列表
    while True:
        data = _ragflow_request(
            "GET",
            f"/datasets/{RAGFLOW_DATASET_ID}/documents",
            params={
                "page": page,
                "page_size": page_size,
                "orderby": "create_time",
                "desc": "true",
            },
        )
        batch = data.get("data", {}).get("docs", [])
        total = data.get("data", {}).get("total", 0)

        # Step 2: 逐文档获取内容
        for doc in batch:
            doc_id = doc.get("id")
            doc_name = doc.get("name")
            try:
                content_data = _ragflow_request(
                    "GET",
                    f"/datasets/{RAGFLOW_DATASET_ID}/documents/{doc_id}/chunks",
                    params={"page": 1, "page_size": 500},
                )
                chunks = content_data.get("data", {}).get("chunks", [])
                full_text = "\n\n".join(
                    c.get("content", "") for c in chunks if c.get("content")
                )
                all_docs.append({
                    "ragflow_id": doc_id,
                    "name": doc_name,
                    "content": full_text,
                    "chunk_count": len(chunks),
                })
                print(f"  ✅ {doc_name} ({len(chunks)} chunks, {len(full_text)} chars)")
            except Exception as e:
                print(f"  ⚠️  跳过 {doc_name}: {e}")

        if len(all_docs) >= total or not batch:
            break
        page += 1

    print(f"\n📊 共导出 {len(all_docs)} 份文档")
    return all_docs


def import_to_llamaindex(documents: list, persist_dir: str, resume: bool, rebuild: bool):
    """将文档导入 LlamaIndex"""
    print("\n" + "=" * 60)
    print("📥 导入到 LlamaIndex...")
    print("=" * 60)

    os.environ["LLAMAINDEX_PERSIST_DIR"] = persist_dir

    # 如果需要重建，先清空索引目录
    if rebuild and os.path.exists(persist_dir):
        import shutil
        shutil.rmtree(persist_dir)
        print(f"  🧹 已清空索引目录: {persist_dir}")

    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool

    kb_tool = KnowledgeBaseTool()
    if not kb_tool._ensure_initialized():
        print("❌ LlamaIndex 初始化失败，请检查依赖: pip install llama-index llama-index-embeddings-dashscope")
        return 0, 0

    # 获取已有文档列表（用于断点续传）
    existing = set()
    if resume:
        list_result = kb_tool._list_topics()
        for doc in list_result.get("data", []):
            existing.add(doc.get("name", ""))

    success_count = 0
    fail_count = 0

    for i, doc in enumerate(documents, 1):
        name = doc["name"]
        content = doc["content"]

        if resume and name in existing:
            print(f"  ⏭️  [{i}/{len(documents)}] {name} (已存在，跳过)")
            continue

        try:
            result = kb_tool._add_document(name=name, content=content)
            if result.get("success"):
                success_count += 1
                print(f"  ✅ [{i}/{len(documents)}] {name}")
            else:
                fail_count += 1
                print(f"  ❌ [{i}/{len(documents)}] {name}: {result.get('error')}")
        except Exception as e:
            fail_count += 1
            print(f"  ❌ [{i}/{len(documents)}] {name}: {e}")

        # 每 10 个文档暂停一下，避免 embedding API 限流
        if i % 10 == 0:
            time.sleep(2)

    print(f"\n📊 导入完成: {success_count} 成功, {fail_count} 失败")
    return success_count, fail_count


def main():
    parser = argparse.ArgumentParser(description="RagFlow → LlamaIndex 知识库迁移")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅导出预览，不执行导入",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="断点续传，跳过已存在的文档",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="清空 LlamaIndex 索引后全量重建",
    )
    parser.add_argument(
        "--persist-dir",
        default=os.path.join(os.path.dirname(__file__), "..", "llama_index_storage"),
        help="LlamaIndex 持久化目录 (默认: ../llama_index_storage)",
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("🔄 RagFlow → LlamaIndex 知识库迁移工具")
    print("=" * 60)
    print(f"  RagFlow API:  {RAGFLOW_API_BASE}")
    print(f"  Dataset ID:   {RAGFLOW_DATASET_ID}")
    print(f"  持久化目录:   {args.persist_dir}")
    print(f"  Dry Run:      {args.dry_run}")
    print(f"  断点续传:     {args.resume}")
    print(f"  全量重建:     {args.rebuild}")
    print("=" * 60 + "\n")

    # Step 1: 导出
    documents = export_all_documents()

    if args.dry_run:
        print("\n📋 Dry Run 完成。以下是将被导入的文档：")
        for doc in documents:
            print(f"  - {doc['name']} ({doc['chunk_count']} chunks)")
        print(f"\n💡 运行不带 --dry-run 的命令执行实际迁移。")
        return

    if not documents:
        print("⚠️  没有可导出的文档。")
        return

    # Step 2: 导入
    success, fail = import_to_llamaindex(
        documents, args.persist_dir, args.resume, args.rebuild
    )

    # 报告
    print("\n" + "=" * 60)
    print("📋 迁移报告")
    print("=" * 60)
    print(f"  导出文档:   {len(documents)}")
    print(f"  导入成功:   {success}")
    print(f"  导入失败:   {fail}")
    print(f"  完成时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
