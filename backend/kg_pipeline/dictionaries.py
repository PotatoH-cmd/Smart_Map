# -*- coding: utf-8 -*-
"""从知识库自动挖掘实体词表。

实测教训：初版规则只匹配「采区/可采区」，导致漏掉「王楼砂场」「潢河」等，
覆盖率仅 56%（552/987 chunk）。补全后缀词表后升至 90%（884/987）。
后缀词表是抽取完整性的第一决定因素。
"""

import json
import os
import re
from collections import Counter

from .normalize import (
    AREA_SUFFIXES, COUNTIES, RIVERS, NOISE_CONTAINS,
    is_noise, strip_admin_prefix,
)
from .refs import _clean_title as clean_title

# 抽词用的通用模式（挖掘阶段，宁多勿漏）
_MINE_PATTERNS = {
    "Area": re.compile(r"([\u4e00-\u9fa5]{2,7}(?:" + "|".join(AREA_SUFFIXES) + r"))"),
    "Town": re.compile(r"([\u4e00-\u9fa5]{2,6}(?:镇|乡|街道))"),
    "Village": re.compile(r"([\u4e00-\u9fa5]{2,6}村)"),
    "Reservoir": re.compile(r"([\u4e00-\u9fa5]{2,6}水库)"),
    "License": re.compile(r"(豫[\u4e00-\u9fa5]{0,4}砂许[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*第\s*\d+\s*号)"),
}

# 词表频次门槛：出现次数低于该值的候选丢弃（许可证不受限）
MIN_FREQ = 2


def load_chunks(persist_dir: str) -> list:
    """直接读取 LlamaIndex docstore.json，得到全部 chunk。

    不经过 KnowledgeBaseTool，避免触发 embedding 初始化。
    """
    path = os.path.join(persist_dir, "docstore.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"知识库文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    data = raw.get("docstore/data") or raw.get("docs") or {}

    chunks = []
    for node_id, node in data.items():
        inner = node.get("__data__") or node
        text = inner.get("text") or ""
        meta = inner.get("metadata") or {}
        if not text:
            continue
        chunks.append({
            "chunk_id": node_id,
            "text": text,
            "meta": meta,
            "doc_id": meta.get("document_id", ""),
            "title": meta.get("title", ""),
            "project": meta.get("project", ""),
            "project_name": meta.get("project_name", ""),
            "breadcrumb": meta.get("breadcrumb", ""),
            "section_no": meta.get("section_no", ""),
            "chunk_type": meta.get("chunk_type", ""),
            "year": meta.get("year", 0),
            "image_paths": meta.get("image_paths", ""),
        })
    return chunks


def mine_documents(chunks: list) -> list:
    """按 document_id 聚合出 Document 节点（含图片资源与章节根）。"""
    docs = {}
    for c in chunks:
        did = c["doc_id"]
        if not did:
            continue
        d = docs.setdefault(did, {
            "id": did,
            "title": clean_title(c["title"]) or did,
            "project": c["project"],
            "project_name": c["project_name"],
            "year": c["year"] or 0,
            "chunk_count": 0,
            "breadcrumb_root": "",
            "image_paths": [],
        })
        d["chunk_count"] += 1
        if not d["breadcrumb_root"] and c["breadcrumb"]:
            d["breadcrumb_root"] = c["breadcrumb"].split(">")[0].strip()
        if not d["year"] and c["year"]:
            d["year"] = c["year"]
        # 图片资源：meta 的 image_paths 为逗号分隔 URL；正文中的 <img>/图片地址 也一并收集
        for url in _collect_images(c):
            if url not in d["image_paths"]:
                d["image_paths"].append(url)
    return list(docs.values())


def _collect_images(chunk: dict) -> list:
    """收集 chunk 中的图片 URL（作为 Document 属性，不单独建节点）。"""
    urls = []
    meta_imgs = chunk.get("image_paths") or ""
    if isinstance(meta_imgs, str):
        urls += [u.strip() for u in meta_imgs.split(",") if u.strip()]
    elif isinstance(meta_imgs, list):
        urls += [str(u).strip() for u in meta_imgs if str(u).strip()]
    # 正文内嵌图片地址
    urls += re.findall(r"图片地址：(\S+)", chunk.get("text", ""))
    urls += re.findall(r"<img\s+src=[\"']?([^\"'\s>]+)", chunk.get("text", ""))
    return [u for u in urls if u.startswith("http")]


def mine_dictionaries(chunks: list) -> dict:
    """扫描全部 chunk，挖掘实体词表（含频次）。

    返回 {"Area": [...], "Town": [...], "Village": [...], "Reservoir": [...], "County": [...], "River": [...]}
    """
    counters = {k: Counter() for k in _MINE_PATTERNS}

    for c in chunks:
        text = c["text"]
        for kind, pat in _MINE_PATTERNS.items():
            for raw in pat.findall(text):
                name = raw if kind == "License" else strip_admin_prefix(raw)
                if kind != "License":
                    if is_noise(name) or NOISE_CONTAINS.search(name):
                        continue
                    if len(name) < 2:
                        continue
                counters[kind][name] += 1

    result = {}
    for kind, counter in counters.items():
        if kind == "License":
            result[kind] = sorted(counter.keys())
        else:
            result[kind] = sorted(
                [n for n, freq in counter.items() if freq >= MIN_FREQ],
                key=lambda n: -counter[n],
            )

    # 县区与主干河流走白名单（高置信，不依赖挖掘）
    result["County"] = list(COUNTIES)
    result["River"] = list(RIVERS)

    # 挖掘到的高频水库并入河流词表
    for res in result.get("Reservoir", []):
        if res not in result["River"]:
            result["River"].append(res)

    return result


def build_entity_index(dicts: dict) -> dict:
    """由词表构建「归一化名 → 实体类型」索引，供规则抽取快速匹配。"""
    index = {}
    for name in dicts.get("County", []):
        index[name] = "County"
    for name in dicts.get("River", []):
        index[name] = "River"
    for name in dicts.get("Reservoir", []):
        index[name] = "River"
    for name in dicts.get("Town", []):
        index[name] = "Town"
    for name in dicts.get("Village", []):
        index[name] = "Village"
    return index