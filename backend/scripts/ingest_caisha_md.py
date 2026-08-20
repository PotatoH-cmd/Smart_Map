#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
潢川县实施方案 Markdown → LlamaIndex 知识库导入脚本

切片规则（按“编号层级”而非 markdown # 级别判断，因文档存在标题级别异常）：
  - 编号段数+1 = 逻辑层级：  "1"→二级(章)  "1.1"→三级(节，父块)  "1.1.1"/"5.2.5.1"→四/五级(子块)
  - 三级标题(节)   → 父块：保留 章面包屑 + 章引言 + 本节直属内容(+子目录)
  - 四/五级标题    → 子块：保留 完整标题路径(到二级) + 各级引言正文 + 本块内容
  - 二级标题下无编号子节(如“2 编制依据”只有（一）（二）（三）) → 整章作单一父块
  - 封面(首个 # 标题前置内容) / 前言 → 各作独立父块
  - 表格：整块 <table> 原样保留，表名置于表前，绝不进入二次切分
  - 图片：给 images/xxx 拼接前缀 http://172.136.16.52:82/ 渲染为可访问 URL 并存入元数据

用法：
  python scripts/ingest_caisha_md.py --dry-run     # 仅解析并导出 chunks JSON，不入库
  python scripts/ingest_caisha_md.py --rebuild      # 先删旧节点再入库并持久化
  python scripts/ingest_caisha_md.py                # 增量入库（不删旧）
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─────────────────────────────────────────────
# 配置常量
# ─────────────────────────────────────────────
MD_PATH = os.environ.get(
    "CAISHA_MD_PATH", "/home/server/python/采砂/潢川县实施方案.md"
)
DOC_ID = "huangchuan_2025_shishifangan"
DOC_TITLE = "潢川县潢河、白露河2025年度河道采砂实施方案（报批稿）"
# 项目“文件夹”（知识库分组标签）
PROJECT = "shishifangan"
PROJECT_NAME = "2025年实施方案"
PROJECT_YEAR = "2025"
IMAGE_BASE_URL = "http://172.136.16.52:82/"
OUTPUT_JSON = os.path.join(
    os.path.dirname(__file__), "..", "output", "潢川县实施方案_chunks.json"
)

# ─────────────────────────────────────────────
# 正则
# ─────────────────────────────────────────────
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)[\.\s、]+(.*)$")
TABLE_NAME_RE = re.compile(r"^表\s*\d")
IMG_MD_RE = re.compile(r"^!\[\]\((images/[^)]+)\)\s*(.*)$")
CAPTION_RE = re.compile(r"^图\s*\d")
IMG_SRC_RE = re.compile(r'(<img\s+src=")(images/[^"]+)(")')


def ensure_env():
    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048")


def abs_url(path: str) -> str:
    """images/xxx.jpg → http://172.136.16.52:82/images/xxx.jpg"""
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return IMAGE_BASE_URL + path.lstrip("/")


def rewrite_table_images(html: str):
    """把表格内 <img src="images/..."> 改写为完整 URL，并收集图片地址"""
    found = []

    def _repl(m):
        full = abs_url(m.group(2))
        found.append(full)
        return m.group(1) + full + m.group(3)

    new_html = IMG_SRC_RE.sub(_repl, html)
    return new_html, found


# ─────────────────────────────────────────────
# 文档解析：切成有序的 section（每个 section 只含自身直属元素）
# ─────────────────────────────────────────────
def parse_document(lines):
    sections = []

    def new_section(kind, num, level, title, heading_raw, line_no):
        s = {
            "kind": kind,
            "num": num,
            "level": level,
            "title": title,
            "heading": heading_raw,
            "line": line_no,
            "elements": [],  # {type: text|subhead|table|image, ...}
        }
        sections.append(s)
        return s

    current = None
    pending_table_name = None
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i].rstrip("\n")
        line = raw.strip()
        if not line:
            i += 1
            continue

        # ---- 标题行 ----
        m = HEADING_RE.match(line)
        if m:
            hashes, htext = m.group(1), m.group(2).strip()
            if htext == "目录":
                current = new_section("skip", None, 99, htext, line, i + 1)
                i += 1
                continue
            numm = NUM_RE.match(htext)
            if numm:
                num = numm.group(1)
                title = numm.group(2).strip()
                level = num.count(".") + 2  # "1"->2 "1.1"->3 "1.1.1"->4
                kind = (
                    "chapter"
                    if level == 2
                    else ("section" if level == 3 else "subsection")
                )
                current = new_section(kind, num, level, title, line, i + 1)
            elif htext == "前言":
                current = new_section("preface", None, 2, htext, line, i + 1)
            elif hashes == "#":
                current = new_section("cover", None, 2, htext, line, i + 1)
            else:
                # 非编号小标题（如 ### （一）...）→ 并入当前块正文
                if current is not None and current["kind"] != "skip":
                    current["elements"].append({"type": "subhead", "text": htext})
            i += 1
            continue

        # ---- 内容行 ----
        if current is None:
            current = new_section("cover", None, 2, DOC_TITLE, "# " + DOC_TITLE, i + 1)
        if current["kind"] == "skip":
            i += 1
            continue

        # 表格（单行 <table>...</table>；容错跨行）
        if line.startswith("<table>"):
            table_html = line
            while "</table>" not in table_html and i + 1 < n:
                i += 1
                table_html += lines[i].strip()
            new_html, imgs = rewrite_table_images(table_html)
            current["elements"].append(
                {
                    "type": "table",
                    "html": new_html,
                    "name": pending_table_name,
                    "images": imgs,
                }
            )
            pending_table_name = None
            i += 1
            continue

        # 表名行：暂存，关联到下一个表格
        if TABLE_NAME_RE.match(line):
            # 向后确认下一非空行是否为表格
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and lines[j].strip().startswith("<table>"):
                pending_table_name = line
                i += 1
                continue
            # 否则当普通文本

        # 图片行
        img_m = IMG_MD_RE.match(line)
        if img_m:
            path = img_m.group(1)
            trailing = img_m.group(2).strip()
            caption = trailing if CAPTION_RE.match(trailing) else ""
            if not caption:
                # 向后看下一非空行是否为图注
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and CAPTION_RE.match(lines[j].strip()):
                    caption = lines[j].strip()
                    i = j  # 消费掉图注行
            current["elements"].append(
                {"type": "image", "path": abs_url(path), "caption": caption}
            )
            i += 1
            continue

        # 独立图注行（若紧跟在无图注的图片后，补挂；否则当文本）
        if CAPTION_RE.match(line):
            if current["elements"] and current["elements"][-1]["type"] == "image" and not current["elements"][-1]["caption"]:
                current["elements"][-1]["caption"] = line
            else:
                current["elements"].append({"type": "text", "text": line})
            i += 1
            continue

        # 普通段落
        current["elements"].append({"type": "text", "text": line})
        i += 1

    return sections


# ─────────────────────────────────────────────
# 元素渲染
# ─────────────────────────────────────────────
def render_elements(elements, include_tables=True, include_images=True):
    parts = []
    img_paths = []
    has_table = False
    for el in elements:
        t = el["type"]
        if t == "text":
            parts.append(el["text"])
        elif t == "subhead":
            parts.append(f"【{el['text']}】")
        elif t == "table":
            if include_tables:
                has_table = True
                name = el.get("name")
                block = (name + "\n" if name else "") + el["html"]
                parts.append(block)
                img_paths.extend(el.get("images", []))
        elif t == "image":
            if include_images:
                cap = el.get("caption", "")
                path = el["path"]
                img_paths.append(path)
                parts.append(f"【图】{cap}｜图片地址：{path}")
    return "\n\n".join([p for p in parts if p]), img_paths, has_table


def text_only(elements):
    """仅取正文/子标题文本，用作上级引言（不含表格/图片，避免重复膨胀）"""
    out = []
    for el in elements:
        if el["type"] == "text":
            out.append(el["text"])
        elif el["type"] == "subhead":
            out.append(f"【{el['text']}】")
    return "\n".join(out).strip()


# ─────────────────────────────────────────────
# 组块：生成父块/子块
# ─────────────────────────────────────────────
def build_chunks(sections):
    # 建立编号 → section 索引，便于查子节 / 祖先
    num_map = {s["num"]: s for s in sections if s.get("num")}
    all_nums = [s["num"] for s in sections if s.get("num")]

    def has_children(num):
        prefix = num + "."
        return any(x.startswith(prefix) for x in all_nums)

    def ancestors(num):
        """返回从章到父节的祖先 num 列表（不含自身）"""
        segs = num.split(".")
        res = []
        for k in range(1, len(segs)):
            res.append(".".join(segs[:k]))
        return res

    chunks = []

    def emit(sec, chunk_type, text, breadcrumb, extra_imgs=None, has_table=False):
        text = text.strip()
        if not text:
            return
        img_set = list(dict.fromkeys((extra_imgs or [])))
        chunks.append(
            {
                "chunk_type": chunk_type,
                "section_no": sec.get("num") or "",
                "level": sec.get("level"),
                "breadcrumb": breadcrumb,
                "title": breadcrumb.split(" > ")[-1] if breadcrumb else sec["title"],
                "has_table": has_table,
                "image_paths": img_set,
                "text": text,
                "source_line": sec["line"],
            }
        )

    for sec in sections:
        kind = sec["kind"]
        if kind == "skip":
            continue

        # 封面 / 前言
        if kind in ("cover", "preface"):
            label = "封面与批复意见" if kind == "cover" else "前言"
            body, imgs, has_tbl = render_elements(sec["elements"])
            header = f"【文档】{DOC_TITLE}\n【章节】{label}"
            emit(sec, kind, header + "\n\n" + body, f"{DOC_TITLE} > {label}", imgs, has_tbl)
            continue

        # 章（二级）
        if kind == "chapter":
            if has_children(sec["num"]):
                # 有编号子节 → 章引言仅作上下文，不单独成块
                continue
            # 无子节 → 整章作父块
            body, imgs, has_tbl = render_elements(sec["elements"])
            bc = f"{DOC_TITLE} > {sec['num']} {sec['title']}"
            header = f"【文档】{DOC_TITLE}\n【章节路径】{sec['num']} {sec['title']}"
            emit(sec, "parent", header + "\n\n" + body, bc, imgs, has_tbl)
            continue

        # 节（三级）→ 父块
        if kind == "section":
            chap_num = sec["num"].split(".")[0]
            chap = num_map.get(chap_num)
            chap_title = f"{chap['num']} {chap['title']}" if chap else chap_num
            bc = f"{DOC_TITLE} > {chap_title} > {sec['num']} {sec['title']}"
            chap_lead = text_only(chap["elements"]) if chap else ""
            own_body, imgs, has_tbl = render_elements(sec["elements"])
            # 子目录（若有子节）
            child_titles = [
                f"{num_map[x]['num']} {num_map[x]['title']}"
                for x in all_nums
                if x.startswith(sec["num"] + ".") and num_map[x]["num"].count(".") == sec["num"].count(".") + 1
            ]
            toc = ("；".join(child_titles)) if child_titles else ""
            header = (
                f"【文档】{DOC_TITLE}\n【章节路径】{chap_title} > {sec['num']} {sec['title']}"
            )
            body_parts = [header]
            if chap_lead:
                body_parts.append(f"【上级引言】{chap_lead}")
            if own_body:
                body_parts.append(own_body)
            if toc:
                body_parts.append(f"【本节子目】{toc}")
            emit(sec, "parent", "\n\n".join(body_parts), bc, imgs, has_tbl)
            continue

        # 子节（四/五级）→ 子块
        if kind == "subsection":
            anc = ancestors(sec["num"])  # e.g. 5.2.5.1 → [5, 5.2, 5.2.5]
            bc_parts = [DOC_TITLE]
            lead_parts = []
            for a in anc:
                asec = num_map.get(a)
                if asec:
                    bc_parts.append(f"{asec['num']} {asec['title']}")
                    lead = text_only(asec["elements"])
                    if lead:
                        lead_parts.append(f"{asec['num']} {asec['title']}：{lead}")
                else:
                    bc_parts.append(a)
            bc_parts.append(f"{sec['num']} {sec['title']}")
            breadcrumb = " > ".join(bc_parts)
            own_body, imgs, has_tbl = render_elements(sec["elements"])
            header = f"【文档】{DOC_TITLE}\n【章节路径】{' > '.join(bc_parts[1:])}"
            body_parts = [header]
            if lead_parts:
                body_parts.append("【上级引言】\n" + "\n".join(lead_parts))
            if own_body:
                body_parts.append(own_body)
            emit(sec, "child", "\n\n".join(body_parts), breadcrumb, imgs, has_tbl)
            continue

    return chunks


# ─────────────────────────────────────────────
# 入库
# ─────────────────────────────────────────────
def ingest(chunks, rebuild=False):
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    from llama_index.core.schema import TextNode, NodeRelationship, RelatedNodeInfo

    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("❌ LlamaIndex 初始化失败")
        return False

    if rebuild:
        try:
            kb._index.delete_ref_doc(DOC_ID, delete_from_docstore=True)
            print(f"🗑  已删除旧文档节点 ref_doc={DOC_ID}")
        except Exception as e:
            print(f"（未找到旧节点或删除跳过：{e}）")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nodes = []
    for idx, c in enumerate(chunks):
        meta = {
            "title": f"{DOC_TITLE}｜{c['breadcrumb'].split(' > ')[-1]}",
            "document_id": DOC_ID,
            "project": PROJECT,
            "project_name": PROJECT_NAME,
            "chunk_type": c["chunk_type"],
            "section_no": c["section_no"],
            "breadcrumb": c["breadcrumb"],
            "level": c["level"] or 0,
            "has_table": c["has_table"],
            "image_paths": ", ".join(c["image_paths"]),
            "year": PROJECT_YEAR,
            "created_at": now,
        }
        node = TextNode(text=c["text"], metadata=meta)
        # 元数据不参与向量化/LLM 上下文（正文已含面包屑），避免重复膨胀
        node.excluded_embed_metadata_keys = list(meta.keys())
        node.excluded_llm_metadata_keys = list(meta.keys())
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=DOC_ID)
        nodes.append(node)

    print(f"📥 准备插入 {len(nodes)} 个节点（向量化中，按 10 条/批）...")
    kb._index.insert_nodes(nodes)
    kb._index.storage_context.persist(persist_dir=kb._persist_dir)
    print(f"✅ 已入库并持久化到：{kb._persist_dir}")
    return True


# ─────────────────────────────────────────────
# 导出预览
# ─────────────────────────────────────────────
def export_json(chunks):
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    payload = {
        "document_id": DOC_ID,
        "document_title": DOC_TITLE,
        "source": MD_PATH,
        "image_base_url": IMAGE_BASE_URL,
        "chunk_count": len(chunks),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chunks": chunks,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"📄 切片预览已导出：{OUTPUT_JSON}")


def print_summary(chunks):
    n_parent = sum(1 for c in chunks if c["chunk_type"] in ("parent", "cover", "preface"))
    n_child = sum(1 for c in chunks if c["chunk_type"] == "child")
    n_table = sum(1 for c in chunks if c["has_table"])
    n_img = sum(len(c["image_paths"]) for c in chunks)
    sizes = [len(c["text"]) for c in chunks]
    print("=" * 60)
    print(f"  总块数: {len(chunks)}  父块/独立块: {n_parent}  子块: {n_child}")
    print(f"  含表格块: {n_table}  图片引用总数: {n_img}")
    print(f"  文本长度: min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}")
    big = [(c['section_no'] or c['chunk_type'], len(c['text'])) for c in chunks if len(c['text']) > 3000]
    if big:
        print(f"  ⚠ 超长块(>3000字，注意向量化token上限8192)：{big}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="潢川县实施方案 → LlamaIndex 知识库导入")
    parser.add_argument("--dry-run", action="store_true", help="仅解析导出 JSON，不入库")
    parser.add_argument("--rebuild", action="store_true", help="入库前先删除本文档旧节点")
    args = parser.parse_args()

    ensure_env()

    if not os.path.isfile(MD_PATH):
        print(f"❌ 源文件不存在: {MD_PATH}")
        sys.exit(1)

    with open(MD_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    print(f"📖 解析 {MD_PATH}（{len(lines)} 行）...")
    sections = parse_document(lines)
    chunks = build_chunks(sections)
    print_summary(chunks)
    export_json(chunks)

    if args.dry_run:
        print("🧪 dry-run 完成，未入库。")
        return

    ingest(chunks, rebuild=args.rebuild)


if __name__ == "__main__":
    main()
