#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LlamaIndex 知识库索引诊断工具

用法：
    # 查看切片效果
    python scripts/test_kb_index.py --chunks "你的测试文本"

    # 检索测试
    python scripts/test_kb_index.py --search "采砂许可"

    # 索引统计
    python scripts/test_kb_index.py --stats

    # 全功能交互模式
    python scripts/test_kb_index.py

    # 指定查询并展示 Top-N 结果详情
    python scripts/test_kb_index.py --search "淮河治理" --top 5 --verbose
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def ensure_env():
    """确保环境变量已设置"""
    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048")


def cmd_stats():
    """索引统计信息"""
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("❌ LlamaIndex 未初始化")
        return

    store = kb._index.docstore
    all_docs = list(store.docs.items())

    # 过滤占位文档
    real_docs = [(k, v) for k, v in all_docs if k != "placeholder"]
    # 按文档分组（合并同一 document_id 的 chunk）
    doc_groups = {}
    for doc_id, doc in real_docs:
        meta = doc.metadata or {}
        parent_id = meta.get("document_id", doc_id)
        doc_groups.setdefault(parent_id, []).append((doc_id, doc))

    print("=" * 60)
    print("📊 LlamaIndex 索引统计")
    print("=" * 60)
    print(f"  总 chunk 数（向量节点）: {len(real_docs)}")
    print(f"  独立文档数:               {len(doc_groups)}")
    print(f"  持久化目录:               {kb._persist_dir}")
    print(f"  分块参数:                 chunk_size={kb._index.settings.chunk_size if hasattr(kb._index, 'settings') else 512}")
    print()

    # 文档详情
    print("-" * 60)
    print(f"{'文档名称':<45} {'chunk数':>7} {'总字数':>7}")
    print("-" * 60)
    sorted_docs = sorted(doc_groups.items(), key=lambda x: len(x[1]), reverse=True)
    for parent_id, chunks in sorted_docs[:30]:  # 只显示前30
        name = chunks[0][1].metadata.get("title", parent_id[:40]) if chunks else parent_id[:40]
        total_chars = sum(len(c.text) for _, c in chunks)
        print(f"  {name:<43} {len(chunks):>7} {total_chars:>7}")
    if len(sorted_docs) > 30:
        print(f"  ... 还有 {len(sorted_docs) - 30} 个文档未列出")
    print()


def cmd_chunks(text: str):
    """查看文本切片效果"""
    from llama_index.core import Settings
    from llama_index.core.node_parser import SentenceSplitter

    ensure_env()
    # 使用与生产环境相同的参数
    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)

    chunks = splitter.split_text(text)

    print("=" * 60)
    print("🔪 文本切片效果演示")
    print("=" * 60)
    print(f"  原始文本长度:  {len(text)} 字符")
    print(f"  分块参数:      chunk_size=512, chunk_overlap=64")
    print(f"  生成 chunk 数: {len(chunks)}")
    print()

    for i, chunk in enumerate(chunks):
        # 显示重叠区域标记
        if i > 0 and i < len(chunks) - 1:
            # 找与前一块的重叠部分
            prev_end = chunks[i-1][-64:] if len(chunks[i-1]) >= 64 else chunks[i-1]
            curr_start = chunk[:64] if len(chunk) >= 64 else chunk
            overlap = ""
            for j in range(min(len(prev_end), len(curr_start))):
                if prev_end[-j-1:] == curr_start[:j+1]:
                    overlap = curr_start[:j+1]
            if overlap and len(overlap) > 10:
                overlap_display = f"  ↳ 重叠: ...{overlap[:40]}..."
            else:
                overlap_display = ""
        else:
            overlap_display = ""

        print(f"  ┌─ Chunk #{i+1} ({len(chunk)} 字符) ─────────────────────")
        # 显示前 120 字符
        preview = chunk[:120].replace('\n', '↵')
        print(f"  │ {preview}")
        if len(chunk) > 120:
            print(f"  │ ... (省略 {len(chunk)-120} 字符)")
        print(f"  └{'─' * 50}")
        if overlap_display:
            print(f"  {overlap_display}")
        print()


def cmd_search(query: str, top_k: int = 5, verbose: bool = False):
    """检索测试"""
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool

    ensure_env()
    kb = KnowledgeBaseTool()
    result = kb._search(query, top_k=top_k)

    print("=" * 60)
    print(f"🔍 检索测试: \"{query}\"")
    print("=" * 60)
    print(f"  检索方法: {result.get('method', 'unknown')}")
    print(f"  返回结果: {result.get('count', 0)} 条")
    print()

    if not result.get("success"):
        print(f"  ❌ 检索失败: {result.get('error')}")
        return

    for i, item in enumerate(result.get("data", [])):
        score = item.get("relevance", 0)
        # 可视化分数
        bar_len = min(int(score * 20), 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)

        title = item.get("title", "?")
        content = item.get("content", "")
        doc_id = item.get("document_id", "")

        print(f"  #{i+1}  相关度: {score:.4f}  [{bar}]")
        print(f"     文档:   {title}")
        print(f"     ID:     {doc_id}")

        if verbose:
            # 显示完整内容（截断）
            content_preview = content[:300].replace('\n', '\n      ')
            print(f"     内容:   {content_preview}")
            if len(content) > 300:
                print(f"     ... (还有 {len(content)-300} 字符)")
            # 显示元数据
            meta = item.get("metadata", {})
            if meta:
                meta_str = {k: v for k, v in meta.items() if k not in ("title", "document_id")}
                if meta_str:
                    print(f"     元数据: {json.dumps(meta_str, ensure_ascii=False)}")
        else:
            # 简洁模式：仅显示前 120 字符内容
            short = content[:120].replace('\n', ' ')
            print(f"     摘要:   {short}...")
        print()


def interactive():
    """交互模式"""
    print("╔══════════════════════════════════════════════════╗")
    print("║   LlamaIndex 知识库索引诊断工具                   ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  命令:                                           ║")
    print("║    s <关键词>  搜索知识库                         ║")
    print("║    t <文本>    测试文本切片                        ║")
    print("║    stats       索引统计                           ║")
    print("║    q / quit    退出                               ║")
    print("╚══════════════════════════════════════════════════╝")

    while True:
        try:
            line = input("\n🔎 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break

        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break
        if line.lower() == "stats":
            cmd_stats()
        elif line.startswith("s "):
            query = line[2:].strip()
            if query:
                cmd_search(query, top_k=5, verbose=True)
        elif line.startswith("t "):
            text = line[2:].strip()
            if text:
                cmd_chunks(text)
        else:
            # 默认当作搜索
            cmd_search(line, top_k=5, verbose=False)


def main():
    parser = argparse.ArgumentParser(
        description="LlamaIndex 知识库索引诊断工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --stats                         查看索引统计
  %(prog)s --search "采砂许可"              检索测试
  %(prog)s --search "河道治理" --top 10     指定 Top-K
  %(prog)s --search "淮河工程" --verbose     显示完整匹配内容
  %(prog)s --chunks "这是一段很长的测试文本"  查看切片效果
  %(prog)s                                  交互模式
        """
    )
    parser.add_argument("--stats", action="store_true", help="索引统计")
    parser.add_argument("--search", type=str, help="检索关键词")
    parser.add_argument("--top", type=int, default=5, help="返回 Top-K 条结果 (默认 5)")
    parser.add_argument("--verbose", action="store_true", help="显示匹配内容的完整详情")
    parser.add_argument("--chunks", type=str, help="测试文本切片效果")

    args = parser.parse_args()

    if args.stats:
        cmd_stats()
    elif args.search:
        cmd_search(args.search, top_k=args.top, verbose=args.verbose)
    elif args.chunks:
        cmd_chunks(args.chunks)
    else:
        interactive()


if __name__ == "__main__":
    main()
