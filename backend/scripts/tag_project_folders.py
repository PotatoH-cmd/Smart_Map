#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 LlamaIndex 知识库节点补打「项目文件夹」标签 project / project_name。

规则：
  - 已有 project 的节点（如本次已重建的《潢川县实施方案》= shishifangan）保持不变；
  - 其余所有节点归入「采砂成果报告」= chengguobaogao。

用法：
  python scripts/tag_project_folders.py            # 预览将要变更的数量
  python scripts/tag_project_folders.py --apply     # 实际写入并持久化
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_PROJECT = "chengguobaogao"
DEFAULT_PROJECT_NAME = "采砂成果报告"


def main():
    apply = "--apply" in sys.argv
    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048")

    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool

    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("❌ 知识库初始化失败")
        return

    docstore = kb._index.docstore
    nodes = [(k, v) for k, v in docstore.docs.items() if k != "placeholder"]

    to_tag = []
    kept = {}
    for _, node in nodes:
        meta = node.metadata or {}
        cur = meta.get("project")
        if cur:
            kept[cur] = kept.get(cur, 0) + 1
        else:
            to_tag.append(node)

    print(f"库内节点总数: {len(nodes)}")
    print(f"已有 project 标签: {sum(kept.values())}  明细: {kept}")
    print(f"待打 [{DEFAULT_PROJECT}] 标签: {len(to_tag)}")

    if not apply:
        print("\n（预览模式，未写入。加 --apply 执行）")
        return

    for node in to_tag:
        node.metadata["project"] = DEFAULT_PROJECT
        node.metadata["project_name"] = DEFAULT_PROJECT_NAME
        # 保持与嵌入/LLM 元数据排除策略一致：分组标签不参与向量化
        for key in ("project", "project_name"):
            if key not in (node.excluded_embed_metadata_keys or []):
                node.excluded_embed_metadata_keys = list(node.excluded_embed_metadata_keys or []) + [key]
            if key not in (node.excluded_llm_metadata_keys or []):
                node.excluded_llm_metadata_keys = list(node.excluded_llm_metadata_keys or []) + [key]

    # 回写 docstore 并持久化
    docstore.add_documents([n for n in to_tag], allow_update=True)
    kb._index.storage_context.persist(persist_dir=kb._persist_dir)
    print(f"\n✅ 已为 {len(to_tag)} 个节点打上 [{DEFAULT_PROJECT}] 标签并持久化到 {kb._persist_dir}")


if __name__ == "__main__":
    main()
