#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把库内现有切片回填进增量清单（一次性基线操作）。

背景：当前 987 个切片由批量脚本灌入，从不写清单，导致
kb_documents / kb_chunks 为空，无法做增量差分。本脚本遍历 docstore，
按 document_id 分组，为每片计算 chunk_fp 写回节点 meta，
并落 kb_chunks / kb_documents，建立增量所需的基线。

用法：
  python scripts/kb_manifest_backfill.py            # 预览（不写库）
  python scripts/kb_manifest_backfill.py --apply    # 实际回填并持久化
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    apply = "--apply" in sys.argv

    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BACKEND_DIR, ".env"))

    from kb_pipeline import Manifest
    from kb_pipeline.fingerprint import fingerprint_of_meta

    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool

    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("初始化失败")
        return 1

    manifest = Manifest()
    docs = getattr(kb._index.docstore, "docs", {}) or {}

    grouped = {}
    for node_id, node in docs.items():
        if node_id == "placeholder":
            continue
        meta = node.metadata or {}
        doc_id = meta.get("document_id") or meta.get("doc_id")
        if not doc_id:
            continue
        text = getattr(node, "text", "") or node.get_content()
        fp = fingerprint_of_meta(meta, text)
        grouped.setdefault(doc_id, []).append((node_id, fp, node))

    total_chunks = sum(len(v) for v in grouped.values())
    print("库内节点总数: %d" % len(docs))
    print("可归属文档数: %d   可回填切片数: %d" % (len(grouped), total_chunks))

    if grouped:
        sample_doc = next(iter(grouped))
        nid, fp, _ = grouped[sample_doc][0]
        print("抽查 %s 首片 node_id=%s fp=%s" % (sample_doc, nid[:16], fp))

    if not apply:
        print("\n（预览模式，未写入。加 --apply 执行）")
        return 0

    touched_nodes = []
    dup_total = 0
    for doc_id, items in grouped.items():
        fp_to_nodes = {}
        for node_id, fp, node in items:
            meta = node.metadata or {}
            if meta.get("chunk_fp") != fp:
                meta["chunk_fp"] = fp
                node.metadata = meta
                if "chunk_fp" not in (node.excluded_embed_metadata_keys or []):
                    node.excluded_embed_metadata_keys = list(
                        node.excluded_embed_metadata_keys or []
                    ) + ["chunk_fp"]
                if "chunk_fp" not in (node.excluded_llm_metadata_keys or []):
                    node.excluded_llm_metadata_keys = list(
                        node.excluded_llm_metadata_keys or []
                    ) + ["chunk_fp"]
                touched_nodes.append(node)
            fp_to_nodes.setdefault(fp, []).append(node_id)

        first_meta = items[0][2].metadata or {}
        raw_title = str(first_meta.get("title", "")).split("\uff5c")[0]
        name = raw_title or doc_id

        dup_total += sum(len(v) - 1 for v in fp_to_nodes.values())
        manifest.upsert_document(
            doc_id=doc_id,
            name=name,
            chunk_count=len(fp_to_nodes),
            source="backfill",
            source_file=first_meta.get("source_file"),
        )
        manifest.replace_document_chunks(doc_id, fp_to_nodes)

    if touched_nodes:
        kb._index.docstore.add_documents(touched_nodes, allow_update=True)
    kb._index.storage_context.persist(persist_dir=kb._persist_dir)

    c = manifest.counts()
    print("\n回填完成：kb_documents=%d 行，kb_chunks=%d 行" % (c["documents"], c["chunks"]))
    print("   已更新 %d 个节点的 meta（chunk_fp）" % len(touched_nodes))
    if dup_total:
        print("   检测到 %d 个重复切片（同文档同内容），已全部登记待增量清理" % dup_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
