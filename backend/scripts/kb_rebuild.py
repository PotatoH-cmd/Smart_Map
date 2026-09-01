#!/usr/bin/env python3
"""
kb_rebuild.py — 知识库本地向量索引盘点与恢复（三级降级）。

用法：
    python scripts/kb_rebuild.py            # 盘点 + 恢复 + 冒烟
    python scripts/kb_rebuild.py --dry-run  # 只盘点，不改动

恢复顺序：
  1. 扫描常见备份目录中的 llama_index_storage（含 docstore.json）→ 直接复制恢复；
  2. 无备份 → 从 RagFlow 数据集拉取全部文档重建本地 LlamaIndex 索引；
  3. 均不可用 → 空库起步（RagFlow 检索兜底仍可用）。
"""
import glob
import json
import os
import shutil
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BACKEND_DIR, ".env"))
except Exception:
    pass

PERSIST_DIR = os.environ.get(
    "LLAMAINDEX_PERSIST_DIR",
    os.path.join(BACKEND_DIR, "llama_index_storage"),
)

BACKUP_SCAN_ROOTS = [
    "/home/szgczx/backups",
    "/home/szgczx",
    "/mnt",
    "/media",
    "/srv/backup",
]
_SELF = os.path.join(BACKEND_DIR, "llama_index_storage")

DRY_RUN = "--dry-run" in sys.argv


def find_backups():
    """扫描候选目录，返回包含 docstore.json 的 llama_index_storage 路径列表。"""
    hits = []
    seen = set()
    for root in BACKUP_SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "**", "llama_index_storage"), recursive=True):
            real = os.path.realpath(path)
            if real in seen or real == os.path.realpath(_SELF):
                continue
            seen.add(real)
            if os.path.exists(os.path.join(path, "docstore.json")):
                hits.append(path)
    return hits


def restore_from_backup(src: str) -> None:
    print(f"[restore] 从备份恢复: {src} -> {PERSIST_DIR}")
    if DRY_RUN:
        return
    os.makedirs(PERSIST_DIR, exist_ok=True)
    shutil.copytree(src, PERSIST_DIR, dirs_exist_ok=True)
    print("[restore] 完成。")


def ragflow_list_documents(api_base, key, ds_id):
    import requests
    docs, page = [], 1
    while True:
        r = requests.get(
            f"{api_base}/datasets/{ds_id}/documents",
            headers={"Authorization": f"Bearer {key}"},
            params={"page": page, "page_size": 100},
            timeout=15,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        items = data.get("docs", []) if isinstance(data, dict) else (data or [])
        if not items:
            break
        docs.extend(items)
        total = data.get("total") if isinstance(data, dict) else None
        page += 1
        if total is None or len(docs) >= int(total) or page > 50:
            break
    return docs


def ragflow_doc_text(api_base, key, ds_id, doc_id):
    """拼接文档全部 chunk 内容作为重建正文。"""
    import requests
    texts, page = [], 1
    while True:
        r = requests.get(
            f"{api_base}/datasets/{ds_id}/documents/{doc_id}/chunks",
            headers={"Authorization": f"Bearer {key}"},
            params={"page": page, "page_size": 100},
            timeout=15,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        chunks = data.get("chunks", []) if isinstance(data, dict) else (data or [])
        if not chunks:
            break
        texts.extend(str(c.get("content") or "") for c in chunks)
        total = data.get("total") if isinstance(data, dict) else None
        page += 1
        if total is None or len(texts) >= int(total) or page > 100:
            break
    return "\n".join(t for t in texts if t.strip())


def rebuild_from_ragflow() -> int:
    api_base = os.environ.get("RAGFLOW_API_BASE", "http://172.136.16.14:8080/api/v1").rstrip("/")
    key = os.environ.get("RAGFLOW_API_KEY", "")
    ds_id = os.environ.get("RAGFLOW_DATASET_ID", "")
    if not (key and ds_id):
        print("[ragflow] 缺少 RAGFLOW_API_KEY / RAGFLOW_DATASET_ID，跳过重建")
        return 0
    import requests
    try:
        docs = ragflow_list_documents(api_base, key, ds_id)
    except Exception as e:
        print(f"[ragflow] 文档列表获取失败: {e}")
        return 0
    if not docs:
        print("[ragflow] 数据集为空，无需重建")
        return 0
    print(f"[ragflow] 数据集共 {len(docs)} 个文档，开始重建本地索引…")
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    tool = KnowledgeBaseTool()
    ok = 0
    for d in docs:
        name = d.get("name") or d.get("id", "未命名")
        doc_id = d.get("id", "")
        try:
            text = ragflow_doc_text(api_base, key, ds_id, doc_id)
            if not text.strip():
                print(f"  - 跳过（无内容）: {name}")
                continue
            res = tool.call({"operation": "add_document", "name": name, "content": text})
            if res.get("success"):
                ok += 1
                print(f"  + 已重建: {name}")
            else:
                print(f"  - 失败: {name} -> {res.get('error')}")
        except Exception as e:
            print(f"  - 异常: {name} -> {e}")
    print(f"[ragflow] 重建完成：{ok}/{len(docs)} 个文档")
    return ok


def smoke_test():
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    tool = KnowledgeBaseTool()
    res = tool.call({"operation": "search", "query": "采砂 河道", "top_k": 3})
    n = len(res.get("results") or res.get("data") or [])
    src = res.get("source") or ("llamaindex" if res.get("success") else "fallback")
    print(f"[smoke] search 返回 {n} 条（source≈{src}）success={res.get('success')}")


def main():
    print(f"== 知识库盘点（persist_dir={PERSIST_DIR}）==")
    has_local = os.path.exists(os.path.join(PERSIST_DIR, "docstore.json"))
    print(f"本地索引: {'存在' if has_local else '为空/缺失 docstore.json'}")

    backups = find_backups()
    print(f"备份扫描: {len(backups)} 个候选 -> {backups[:3]}")

    if not has_local and backups and not DRY_RUN:
        restore_from_backup(backups[0])
        has_local = True
    elif DRY_RUN and backups:
        print("[dry-run] 若执行将恢复第一个备份")

    if not has_local:
        print("本地索引仍为空，尝试 RagFlow 重建…")
        if DRY_RUN:
            print("[dry-run] 若执行将从 RagFlow 重建")
            return
        rebuild_from_ragflow()

    if not DRY_RUN:
        smoke_test()


if __name__ == "__main__":
    main()
