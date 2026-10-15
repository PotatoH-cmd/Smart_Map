# -*- coding: utf-8 -*-
"""溯源信息（refs）构建与合并。

每个图谱节点/关系都携带 refs，用于回答「这条数据从哪来」：

    {
      "doc_id": "huangchuan_2025_shishifangan",
      "title": "潢川县潢河、白露河2025年度河道采砂实施方案（报批稿）",
      "section": "1.6.2 黄堰村采区（白露河）",
      "snippet": "该位于伞陂镇黄堰村白露河河段…控制开采高程为35.15～34.21m",
      "chunk_id": "d4cb0f66-17b3-4e5b-b075-864993ee2452",
      "count": 3
    }

Document 节点 id 即 document_id，可直接用于
`/api/knowledge/preview/{document_id}` 预览原文，实现图谱→原文的溯源跳转。
"""

import json
import re

MAX_REFS = 5           # 每节点最多保留的溯源条目数
SNIPPET_LEN = 160      # 原文片段长度
MAX_REFS_JSON = 8000   # refs 序列化后的字符上限，防止单行属性过大


def make_ref(doc_id: str, title: str, section: str, snippet: str,
             chunk_id: str = "", count: int = 1) -> dict:
    """构造单条溯源记录。"""
    return {
        "doc_id": doc_id or "",
        "title": _clean_title(title),
        "section": _clean_section(section),
        "snippet": _clean_snippet(snippet),
        "chunk_id": chunk_id or "",
        "count": count,
    }


def _clean_title(title: str) -> str:
    """清洗文档标题中的内部文件名前缀。

    `section_5_0015_5.3.8_豫罗砂许〔2023〕第_08_号.txt` → `5.3.8 豫罗砂许〔2023〕第 08 号`
    """
    if not title:
        return ""
    t = re.sub(r"^section_\d+_", "", title)
    t = re.sub(r"^table_\d+_", "", t)
    t = re.sub(r"^doc_\d+_", "", t)
    t = re.sub(r"^\d+_(?=\d)", "", t)
    t = re.sub(r"\.(txt|md|docx?|pdf)$", "", t, flags=re.IGNORECASE)
    t = t.replace("_", " ").strip()
    return t


def _clean_section(section: str) -> str:
    """清洗章节路径，只保留最后两级，避免过长。"""
    if not section:
        return ""
    parts = [p.strip() for p in section.split(">") if p.strip()]
    return " > ".join(parts[-2:]) if len(parts) > 2 else " > ".join(parts)


def _clean_snippet(text: str) -> str:
    """截取原文片段，压掉换行与多余空白。"""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    return s[:SNIPPET_LEN]


def extract_snippet(text: str, keyword: str, window: int = 70) -> str:
    """围绕命中关键词截取上下文片段（比固定取开头更有溯源价值）。"""
    if not text:
        return ""
    if keyword:
        idx = text.find(keyword)
        if idx >= 0:
            start = max(0, idx - window)
            end = min(len(text), idx + len(keyword) + window)
            return text[start:end]
    return text[: SNIPPET_LEN * 2]


def merge_refs(existing_json: str, new_ref: dict) -> str:
    """把新溯源条目合并进已有 refs JSON（按 doc_id+section 去重，累计 count）。"""
    refs = parse_refs(existing_json)
    key = (new_ref.get("doc_id", ""), new_ref.get("section", ""))
    for r in refs:
        if (r.get("doc_id", ""), r.get("section", "")) == key:
            r["count"] = r.get("count", 1) + new_ref.get("count", 1)
            # 保留更长的片段
            if len(new_ref.get("snippet", "")) > len(r.get("snippet", "")):
                r["snippet"] = new_ref["snippet"]
            return dump_refs(refs)
    refs.append(new_ref)
    return dump_refs(refs)


def parse_refs(refs_json) -> list:
    """解析 refs JSON 字符串为列表（容错）。"""
    if not refs_json:
        return []
    if isinstance(refs_json, list):
        return refs_json
    try:
        val = json.loads(refs_json)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def dump_refs(refs: list) -> str:
    """序列化 refs，按 count 降序保留前 MAX_REFS 条，并限制总长度。"""
    if not refs:
        return ""
    refs = sorted(refs, key=lambda r: r.get("count", 1), reverse=True)[:MAX_REFS]
    s = json.dumps(refs, ensure_ascii=False)
    if len(s) > MAX_REFS_JSON:
        s = json.dumps(refs[:2], ensure_ascii=False)
    return s


def refs_summary(refs_json, limit: int = 2) -> str:
    """把 refs 压成一句人类可读的来源说明（供检索结果与提示词使用）。"""
    refs = parse_refs(refs_json)[:limit]
    if not refs:
        return ""
    parts = []
    for r in refs:
        title = r.get("title") or r.get("doc_id") or "未知文档"
        section = r.get("section") or ""
        parts.append(f"《{title}》{('·' + section) if section else ''}")
    return "；".join(parts)
