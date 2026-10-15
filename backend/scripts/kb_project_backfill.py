"""把 docstore 节点上的 project / project_name 回填进 kb_documents。

背景：kb_documents 原先只记录文档级身份，没有项目归属，导致文档管理页
的文件夹视图只能用「切片数」充数（见优化方案 A2）。项目信息在入库时
已写进每个切片节点的 metadata，这里按 document_id 聚合回填。

幂等：只做 COALESCE 更新，重复执行结果一致；已有值的文档不被覆盖。
用法：
    python3 scripts/kb_project_backfill.py          # 预览
    python3 scripts/kb_project_backfill.py --apply  # 执行
"""

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb_pipeline import Manifest  # noqa: E402


def collect_projects(index):
    """从 docstore 节点聚合 doc_id → (project, project_name)。"""
    by_doc = defaultdict(lambda: {"project": None, "project_name": None})
    for node in index.docstore.docs.values():
        meta = node.metadata or {}
        doc_id = meta.get("document_id")
        if not doc_id:
            continue
        slot = by_doc[doc_id]
        if not slot["project"] and meta.get("project"):
            slot["project"] = meta["project"]
        if not slot["project_name"] and meta.get("project_name"):
            slot["project_name"] = meta["project_name"]
    return by_doc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = parser.parse_args()

    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool

    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("知识库初始化失败")
        return 1

    by_doc = collect_projects(kb._index)
    manifest = Manifest()
    known = {d["doc_id"] for d in manifest.all_documents()}

    hit = miss = filled = 0
    for doc_id, info in sorted(by_doc.items()):
        if doc_id not in known:
            miss += 1
            continue
        hit += 1
        row = manifest.lookup("doc_id", doc_id) or {}
        if not row.get("project") and not row.get("project_name"):
            filled += 1
        if args.apply:
            manifest.set_document_project(
                doc_id, info["project"], info["project_name"]
            )

    print(f"docstore 文档数: {len(by_doc)}")
    print(f"命中清单: {hit}，不在清单中: {miss}")
    print(f"本次需要补 project 的文档: {filled}")

    # 回填后按项目统计，确认与文件夹视图口径一致
    rows = manifest.all_documents()
    projects = defaultdict(int)
    for r in rows:
        projects[r.get("project_name") or "未分组"] += 1
    print(f"清单文档总数: {len(rows)}，项目数: {len(projects)}")
    for name, cnt in sorted(projects.items(), key=lambda x: -x[1])[:8]:
        print(f"  {name}: {cnt} 篇")
    if not args.apply:
        print("\n（预览模式，未写入；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
