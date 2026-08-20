#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
信阳各县区实施方案 Markdown → LlamaIndex 知识库批量导入脚本
（商城县 / 平桥区 / 罗山县 / 固始县，切片规则与潢川县 ingest_caisha_md.py 完全一致）

切片规则（按"编号层级"而非 markdown # 级别判断）：
  - 编号段数+1 = 逻辑层级："1"→二级(章)  "1.1"→三级(节，父块)  "1.1.1"→四/五级(子块)
  - 三级标题(节)   → 父块：章面包屑 + 章引言 + 本节直属内容(+子目录)
  - 四/五级标题    → 子块：完整标题路径(到二级) + 各级引言正文 + 本块内容
  - 二级标题下无编号子节 → 整章作单一父块
  - 封面 / 前言 → 各作独立父块；表格整块保留；图片 URL 加代理前缀

相对潢川县脚本的兼容性增强：
  - 编号后无空格的标题（罗山县 "1.1河道基本情况"）
  - "前 言"/"目 录" 等标题内含空格 → 去空格后再匹配
  - 首段编号为年份（如 "2025 年度…"）不当作章编号，防误判
  - 空标题行（固始县 "#"）直接跳过
  - 超长块拆分：超过 MAX_CHUNK_CHARS 的块按段落/表格边界拆成多个部分（每部分保留
    完整章节路径 + 部分序号）；仅当单个表格本身超限时才按行拆分并重复表头（续表）

用法：
  python scripts/ingest_shishifangan_multi.py --dry-run             # 全部4个文档仅解析导出JSON
  python scripts/ingest_shishifangan_multi.py --doc luoshan --dry-run
  python scripts/ingest_shishifangan_multi.py --rebuild             # 全部正式入库（先删旧节点）
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─────────────────────────────────────────────
# 文档配置（project 统一归入"实施方案"文件夹）
# ─────────────────────────────────────────────
PROJECT = "shishifangan"
PROJECT_NAME = "2025年实施方案"
PROJECT_YEAR = "2025"
IMAGE_BASE_URL = "http://172.136.16.52:82/"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
# 单块字符上限（DashScope text-embedding-v3 上限 8192 tokens，表格HTML按约0.6 token/字符
# 保守估算，10000 字符 ≈ 6000 tokens，留足余量）
MAX_CHUNK_CHARS = 10000

CONFIGS = {
    "shangcheng": {
        "md": "/home/server/python/采砂/商城县实施方案.md",
        "doc_id": "shangcheng_2025_shishifangan",
        "title": "商城县灌河2025年度河道采砂实施方案（报批稿）",
    },
    "pingqiao": {
        "md": "/home/server/python/采砂/平桥区实施方案.md",
        "doc_id": "pingqiao_2025_shishifangan",
        "title": "淮河干流信阳市平桥区（出山店水库上游段）2025年度河道采砂实施方案",
    },
    "luoshan": {
        "md": "/home/server/python/采砂/罗山县实施方案.md",
        "doc_id": "luoshan_2025_shishifangan",
        "title": "信阳市竹竿河、浉河罗山段2025年度河道采砂实施方案（报批本）",
    },
    "gushi": {
        "md": "/home/server/python/采砂/固始县实施方案.md",
        "doc_id": "gushi_2025_shishifangan",
        "title": "固始县史灌河2025年度河道采砂实施方案（报批本）",
    },
}

# ─────────────────────────────────────────────
# 正则
# ─────────────────────────────────────────────
HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
# 编号后可以是 . 空格分隔，也可以直接跟中文（罗山县 "1.1河道基本情况"）
NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[\.]\s*|\s+|(?=[^\d\s.]))(.*)$")
# 列表式小标题（"1、xxx" / "2）xxx"）→ 不当章节，并入当前块正文
LIST_HEAD_RE = re.compile(r"^\d+\s*[、）)]")
# 编号内夹空格/全角点（罗山县 "5 . 5 . 3储砂场"）→ 归一化为 "5.5.3储砂场"
NUM_SPACE_RE = re.compile(r"(?<=\d)\s*[\.．]\s*(?=\d)")
TABLE_NAME_RE = re.compile(r"^表\s*\d")
IMG_MD_RE = re.compile(r"^!\[\]\((images/[^)]+)\)\s*(.*)$")
CAPTION_RE = re.compile(r"^图\s*\d")
IMG_SRC_RE = re.compile(r'(<img\s+src=")(images/[^"]+)(")')


def ensure_env():
    os.environ.setdefault("KNOWLEDGE_BACKEND", "llamaindex")
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048")


def abs_url(path: str) -> str:
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return IMAGE_BASE_URL + path.lstrip("/")


def rewrite_table_images(html: str):
    found = []

    def _repl(m):
        full = abs_url(m.group(2))
        found.append(full)
        return m.group(1) + full + m.group(3)

    new_html = IMG_SRC_RE.sub(_repl, html)
    return new_html, found


# ─────────────────────────────────────────────
# 文档解析（与潢川县脚本同构，多几处容错）
# ─────────────────────────────────────────────
def parse_document(lines, doc_title):
    sections = []

    def new_section(kind, num, level, title, heading_raw, line_no):
        s = {
            "kind": kind, "num": num, "level": level, "title": title,
            "heading": heading_raw, "line": line_no, "elements": [],
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
            compact = htext.replace(" ", "").replace("\u3000", "")
            if not compact:  # 空标题（固始县 "#"）
                i += 1
                continue
            if compact in ("目录", "目次"):
                current = new_section("skip", None, 99, compact, line, i + 1)
                i += 1
                continue
            # 列表式小标题（1、/2））→ 并入当前块正文，不当章节
            if LIST_HEAD_RE.match(htext):
                if current is not None and current["kind"] != "skip":
                    current["elements"].append({"type": "subhead", "text": htext})
                i += 1
                continue
            # 编号内夹空格归一化（"5 . 5 . 3xxx" → "5.5.3xxx"）
            htext_norm = NUM_SPACE_RE.sub(".", htext)
            numm = NUM_RE.match(htext_norm)
            # 首段编号为年份等大数（如 "2025 年度…"）不算章节编号
            if numm and int(numm.group(1).split(".")[0]) > 30:
                numm = None
            if numm:
                num = numm.group(1)
                title = re.sub(r"\s+", " ", numm.group(2).strip())
                level = num.count(".") + 2
                kind = ("chapter" if level == 2
                        else ("section" if level == 3 else "subsection"))
                current = new_section(kind, num, level, title, line, i + 1)
            elif compact == "前言":
                current = new_section("preface", None, 2, "前言", line, i + 1)
            elif hashes == "#":
                current = new_section("cover", None, 2, htext, line, i + 1)
            else:
                # 非编号小标题（如 ## 浉河： / ### （一）...）→ 并入当前块正文
                if current is not None and current["kind"] != "skip":
                    current["elements"].append({"type": "subhead", "text": htext})
            i += 1
            continue

        # ---- 内容行 ----
        if current is None:
            current = new_section("cover", None, 2, doc_title, "# " + doc_title, i + 1)
        if current["kind"] == "skip":
            i += 1
            continue

        if line.startswith("<table>"):
            table_html = line
            while "</table>" not in table_html and i + 1 < n:
                i += 1
                table_html += lines[i].strip()
            new_html, imgs = rewrite_table_images(table_html)
            current["elements"].append(
                {"type": "table", "html": new_html,
                 "name": pending_table_name, "images": imgs})
            pending_table_name = None
            i += 1
            continue

        if TABLE_NAME_RE.match(line):
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and lines[j].strip().startswith("<table>"):
                pending_table_name = line
                i += 1
                continue

        img_m = IMG_MD_RE.match(line)
        if img_m:
            path = img_m.group(1)
            trailing = img_m.group(2).strip()
            caption = trailing if CAPTION_RE.match(trailing) else ""
            if not caption:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and CAPTION_RE.match(lines[j].strip()):
                    caption = lines[j].strip()
                    i = j
            current["elements"].append(
                {"type": "image", "path": abs_url(path), "caption": caption})
            i += 1
            continue

        if CAPTION_RE.match(line):
            if (current["elements"] and current["elements"][-1]["type"] == "image"
                    and not current["elements"][-1]["caption"]):
                current["elements"][-1]["caption"] = line
            else:
                current["elements"].append({"type": "text", "text": line})
            i += 1
            continue

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
    out = []
    for el in elements:
        if el["type"] == "text":
            out.append(el["text"])
        elif el["type"] == "subhead":
            out.append(f"【{el['text']}】")
    return "\n".join(out).strip()


# ─────────────────────────────────────────────
# 综合汇总表 → 自然语言摘要（提升 embedding 检索命中率）
# ─────────────────────────────────────────────
TABLE_CELL_RE = re.compile(r'<t[hd][^>]*>(.*?)</t[hd]>', re.DOTALL)


def _is_comprehensive_table(html: str) -> bool:
    """判断是否是包含所有采砂点数据的综合汇总表。

    固始县格式：开采点名称 + 年度采砂控制量 + (河段长度|采砂船)
    罗山/商城/平桥格式：可采区 + 序号 + (规划量|开采量|控制开采高程)
    罗山县控制指标表（key-value 型）：采区编号 + 桩号范围 + 控制高程 + 控制采砂量
    """
    # 固始格式
    if ('开采点名称' in html and '年度采砂控制量' in html
            and ('河段长度' in html or '采砂船' in html)):
        return True
    # 罗山/商城/平桥格式：有序号列 + 可采区/采区名 + 数值指标列
    if ('序号' in html and '可采区' in html
            and ('规划量' in html or '开采量' in html or '控制开采高程' in html or '控制开采底高程' in html)):
        return True
    if ('序号' in html and '开采区名称' in html
            and ('规划量' in html or '开采量' in html or '控制开采' in html)):
        return True
    # 罗山县采区控制指标表：key-value 型（采区编号/桩号范围/行政区域/控制高程/控制采砂量/控制机具数量）
    if ('采区编号' in html and '桩号范围' in html and '控制高程' in html
            and '控制采砂量' in html):
        return True
    return False


def _calc_length_from_stake(stake: str) -> int:
    """从桩号范围推导河段长度（米）：'5+200-6+160' → 960；多段相加。

    仅当桩号均为 K 制三位尾数（如 5+200）时计算，格式异常（如 1+00）跳过。
    """
    total = 0
    for m in re.finditer(r'(\d+)\+(\d{3})\s*[-~]\s*(\d+)\+(\d{3})', stake):
        s = int(m.group(1)) * 1000 + int(m.group(2))
        e = int(m.group(3)) * 1000 + int(m.group(4))
        if e > s:
            total += e - s
    return total if total > 0 else 0


def _summarize_ctrl_table_rows_per_site(html: str) -> list:
    """解析罗山县"采区控制指标表"（key-value 型），每个采区生成一条自然语言摘要。

    表格结构（每采区 6 行 key-value 对，多个采区在同一张表内连续排列）：
        <td>采区编号</td><td>浉河SH2</td>
        <td>桩号范围</td><td>5+200-6+160</td>
        <td>行政区域</td><td>高店乡(王湾村)</td>
        <td>控制高程</td><td>41.6-42.0</td>
        <td>控制采砂量</td><td>20.0万m3</td>
        <td>控制机具数量</td><td>采砂船（2) 提砂船（1） 挖掘机（2) 铲车（1)</td>
    """
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    if len(rows) < 2:
        return []

    result = []
    cur = {}  # 当前采区的 key-value 累积
    for row_html in rows:
        cells = [c.strip() for c in TABLE_CELL_RE.findall(row_html)]
        cells = [re.sub(r'\s+', '', c) for c in cells]
        # 非 key-value 行（坐标行/合并行）跳过
        if len(cells) < 2 or len(cells) > 2:
            continue
        key, val = cells[0], cells[1]
        if not key or not val:
            continue
        if key == '采区编号':
            if cur.get('编号'):
                result.append(cur)
            cur = {'编号': val}
        elif key in ('桩号范围', '行政区域', '控制高程', '控制采砂量', '控制机具数量', '采区长度', '采区面积'):
            cur[key] = val
    if cur.get('编号'):
        result.append(cur)

    summaries = []
    for r in result:
        code = r.get('编号', '')
        loc = r.get('行政区域', '')
        # 摘要锚点：编号 + 行政区域村庄名（如 SH2（高店乡王湾村）），保证语义检索可命中
        loc_clean = re.sub(r'^[^(),，、]+[乡镇](?:街道)?[（(]', '', loc)
        loc_clean = re.sub(r'[)）]$', '', loc_clean)
        parts = [f'{code}（{loc_clean}）' if loc_clean and code not in loc_clean else (code or loc_clean)]
        if r.get('桩号范围'):
            parts.append(f'桩号范围{r["桩号范围"]}')
            length = _calc_length_from_stake(r['桩号范围'])
            if length:
                parts.append(f'河段长度{length}米')
        if loc:
            parts.append(f'位于{loc}')
        if r.get('控制高程'):
            parts.append(f'控制开采高程{r["控制高程"]}米')
        if r.get('控制采砂量'):
            parts.append(f'年度采砂控制量{r["控制采砂量"]}')
        if r.get('控制机具数量'):
            parts.append(f'采砂机具{r["控制机具数量"]}')
        # 面积换算：桩号范围可推出长度，但控制指标表无长度字段时从桩号范围推导
        if not r.get('桩号范围'):
            continue
        summaries.append((code, '，'.join(parts)))
    return summaries


def _summarize_table_rows(html: str) -> str:
    """解析综合汇总表的每一行，生成自然语言摘要。

    原始 HTML 表格 embedding 效果极差（标签噪音、数字无语义），
    这里为每行生成类似"史河南元-祝家楼可采点：位于...，河段长度2070米..."的描述，
    使 embedding 能正确匹配"南元 祝家楼 长度 面积"等查询。
    """
    pairs = _summarize_table_rows_per_site(html)
    if not pairs:
        return ''
    summaries = ['  - ' + s for _, s in pairs]
    return '\n【综合汇总表数据摘要】（以下为各采砂点核心参数的自然语言描述）\n' + '\n'.join(summaries)


def _summarize_table_rows_per_site(html: str) -> list:
    """解析综合汇总表的每一行，返回 [(砂场名, 自然语言摘要), ...]。

    每行独立返回，用于生成每站点一个 chunk 的精准 embedding。

    支持两种表格格式：
    1. 固始格式：cells[1]=描述性名称（如 牛老家、龙潭）
    2. 罗山/商城格式：cells[1]=短码（如 SH2）, cells[2]=所属区段（如 高店乡(王湾村)）
       → 用所属区段做语义匹配锚点
    """
    if not _is_comprehensive_table(html):
        return []

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    if len(rows) < 3:
        return []

    result = []
    for row_html in rows[2:]:  # skip 2 header rows
        cells = [c.strip() for c in TABLE_CELL_RE.findall(row_html)]
        if len(cells) < 4:
            continue
        name = cells[1] if len(cells) > 1 else ''
        # 罗山/商城格式：cells[1] 是短码（如 SH2），真正的语义名称在 cells[2]（所属区段）
        location_hint = ''
        if name and not re.search(r'[\u4e00-\u9fa5]{2,}', name):
            # 短码格式：cells[2] 如 "高店乡(王湾村)" → 提取村庄名
            location_hint = cells[2] if len(cells) > 2 else ''
            # 无有效位置信息（坐标行/纯数字行如 "1745.3"/"X=3573..."）直接跳过
            if not location_hint or not re.search(r'[\u4e00-\u9fa5]{2,}', location_hint):
                continue
            # 去掉乡镇前缀，只保留村庄名（如 王湾村）
            loc_clean = re.sub(r'^[^(),，、]+[乡镇](?:街道)?[（(]', '', location_hint)
            loc_clean = re.sub(r'[)）]$', '', loc_clean)
            parts = [f'{name}（{loc_clean}）']
        elif not name or name in ('合计', '小计', '') or not re.search(r'[\u4e00-\u9fa5]', name):
            continue
        else:
            parts = [name]

        # ── 河段长度：3-4 位纯数字，后紧跟面积小数 ──
        len_idx = -1
        for i, c in enumerate(cells):
            if re.match(r'^\d{3,4}$', c):
                n = int(c)
                if 500 <= n <= 4500:
                    if i + 1 < len(cells) and re.match(r'^\d+\.\d+$', cells[i + 1]):
                        len_idx = i
                        break
        if len_idx >= 0:
            parts.append(f'河段长度{cells[len_idx]}米')
            if len_idx + 1 < len(cells) and re.match(r'^\d+\.\d+$', cells[len_idx + 1]):
                parts.append(f'河段面积{cells[len_idx + 1]}平方千米')

        # ── 行政位置：靠前的中文长文本 ──
        for c in cells[2:9]:
            if len(c) >= 4 and re.search(r'[村镇乡社区办事处]', c) and not re.search(r'[\d+]', c):
                parts.append(f'位于{c}')
                break

        # ── 计划采砂范围：最靠近河段长度的桩号 ──
        best_stake, best_dist = None, 999
        for i, c in enumerate(cells):
            if re.match(r'^\d+\+[\d]+\s*[-~]\s*\d+\+[\d]+$', c):
                if len_idx >= 0:
                    dist = abs(i - len_idx)
                    if dist < best_dist:
                        best_dist, best_stake = dist, c
                elif best_stake is None:
                    best_stake = c
        if best_stake:
            parts.append(f'计划采砂范围{best_stake}')

        # ── 年度控制开采高程：长度之后出现的 XX.XX-XX.XX ──
        ctrl_idx = -1
        for i, c in enumerate(cells):
            if (len_idx < 0 or i > len_idx) and re.match(r'^\d+\.\d+\s*[-~]\s*\d+\.\d+$', c):
                parts.append(f'控制开采高程{c}米')
                ctrl_idx = i
                break

        # ── 年度采砂控制量：控制高程之后首个 >1 的小数（排除可利用系数）──
        if ctrl_idx >= 0:
            for j in range(ctrl_idx + 1, min(ctrl_idx + 5, len(cells))):
                if re.match(r'^\d+\.\d+$', cells[j]):
                    try:
                        if float(cells[j]) > 1.0:
                            parts.append(f'年度采砂控制量{cells[j]}万立方米')
                            break
                    except ValueError:
                        pass

        # ── 采砂机具：末尾连续整数 ──
        nums = []
        for c in reversed(cells):
            if re.match(r'^\d{1,2}$', c):
                nums.append(c)
            else:
                break
        nums.reverse()
        equip = []
        if len(nums) >= 3:
            if nums[-3] != '0':
                equip.append(f'采砂船{nums[-3]}艘')
            if nums[-2] != '0':
                equip.append(f'提砂船{nums[-2]}艘')
            if nums[-1] != '0':
                equip.append(f'挖掘机{nums[-1]}辆')
        if equip:
            parts.append('配备' + '、'.join(equip))

        # ── 储砂场 ──
        for c in cells:
            if c.endswith('储砂场'):
                parts.append(f'储砂场为{c}')
                break

        result.append((name, '，'.join(parts)))

    return result


def _augment_chunks_with_table_summary(chunks):
    """对包含综合汇总表的 chunk，为每个砂场创建独立摘要 chunk。

    之前一个摘要 chunk 包含全部 16 个砂场数据，embedding 代表 16 个站点的"平均值"，
    搜索单个站点（如 牛老家-龙潭）时信号被其他 15 个站点稀释，检索命中率极低。
    现在改为每行（每个砂场）一个独立 chunk，embedding 精准聚焦单一站点。
    """
    extra_chunks = []
    source_map = {}  # (section_no, breadcrumb, kind, site_name) -> 去重
    for c in chunks:
        text = c['text']
        tables = TABLE_BLOCK_RE.findall(text)
        for tbl in tables:
            # 优先识别罗山式控制指标表（key-value 型），它也可能满足综合汇总表特征
            if '采区编号' in tbl and '桩号范围' in tbl and '控制高程' in tbl and '控制采砂量' in tbl:
                kind = 'ctrl'
                summaries = _summarize_ctrl_table_rows_per_site(tbl)
            elif _is_comprehensive_table(tbl):
                kind = 'sum'
                summaries = _summarize_table_rows_per_site(tbl)
            else:
                continue
            if not summaries:
                continue
            c['has_table'] = True
            doc_title = c.get('breadcrumb', '').split(' > ')[0] or ''
            for site_name, site_summary in summaries:
                key = (c.get('section_no', ''), c.get('breadcrumb', ''), kind, site_name)
                if key in source_map:
                    continue
                source_map[key] = True
                header = f"【文档】{doc_title}\n【章节】综合汇总表 - {site_name}"
                extra_chunks.append({
                    'chunk_type': 'summary',
                    'section_no': c.get('section_no', ''),
                    'level': c.get('level'),
                    'breadcrumb': (c.get('breadcrumb', '') + f' > 综合汇总表 > {site_name}'),
                    'title': (c.get('title', '') + f'｜{site_name}'),
                    'has_table': False,
                    'image_paths': [],
                    'text': header + '\n\n' + site_summary,
                    'source_line': c.get('source_line', 0),
                })
    return chunks + extra_chunks


# ─────────────────────────────────────────────
# 组块
# ─────────────────────────────────────────────
def build_chunks(sections, doc_title):
    num_map = {s["num"]: s for s in sections if s.get("num")}
    all_nums = [s["num"] for s in sections if s.get("num")]

    def has_children(num):
        prefix = num + "."
        return any(x.startswith(prefix) for x in all_nums)

    def ancestors(num):
        segs = num.split(".")
        return [".".join(segs[:k]) for k in range(1, len(segs))]

    chunks = []

    def emit(sec, chunk_type, text, breadcrumb, extra_imgs=None, has_table=False):
        text = text.strip()
        if not text:
            return
        img_set = list(dict.fromkeys((extra_imgs or [])))
        chunks.append({
            "chunk_type": chunk_type,
            "section_no": sec.get("num") or "",
            "level": sec.get("level"),
            "breadcrumb": breadcrumb,
            "title": breadcrumb.split(" > ")[-1] if breadcrumb else sec["title"],
            "has_table": has_table,
            "image_paths": img_set,
            "text": text,
            "source_line": sec["line"],
        })

    for sec in sections:
        kind = sec["kind"]
        if kind == "skip":
            continue

        if kind in ("cover", "preface"):
            label = "封面与批复意见" if kind == "cover" else "前言"
            body, imgs, has_tbl = render_elements(sec["elements"])
            header = f"【文档】{doc_title}\n【章节】{label}"
            emit(sec, kind, header + "\n\n" + body, f"{doc_title} > {label}", imgs, has_tbl)
            continue

        if kind == "chapter":
            if has_children(sec["num"]):
                continue
            body, imgs, has_tbl = render_elements(sec["elements"])
            bc = f"{doc_title} > {sec['num']} {sec['title']}"
            header = f"【文档】{doc_title}\n【章节路径】{sec['num']} {sec['title']}"
            emit(sec, "parent", header + "\n\n" + body, bc, imgs, has_tbl)
            continue

        if kind == "section":
            chap_num = sec["num"].split(".")[0]
            chap = num_map.get(chap_num)
            chap_title = f"{chap['num']} {chap['title']}" if chap else chap_num
            bc = f"{doc_title} > {chap_title} > {sec['num']} {sec['title']}"
            chap_lead = text_only(chap["elements"]) if chap else ""
            own_body, imgs, has_tbl = render_elements(sec["elements"])
            child_titles = [
                f"{num_map[x]['num']} {num_map[x]['title']}"
                for x in all_nums
                if x.startswith(sec["num"] + ".")
                and num_map[x]["num"].count(".") == sec["num"].count(".") + 1
            ]
            toc = ("；".join(child_titles)) if child_titles else ""
            header = f"【文档】{doc_title}\n【章节路径】{chap_title} > {sec['num']} {sec['title']}"
            body_parts = [header]
            if chap_lead:
                body_parts.append(f"【上级引言】{chap_lead}")
            if own_body:
                body_parts.append(own_body)
            if toc:
                body_parts.append(f"【本节子目】{toc}")
            emit(sec, "parent", "\n\n".join(body_parts), bc, imgs, has_tbl)
            continue

        if kind == "subsection":
            anc = ancestors(sec["num"])
            bc_parts = [doc_title]
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
            header = f"【文档】{doc_title}\n【章节路径】{' > '.join(bc_parts[1:])}"
            body_parts = [header]
            if lead_parts:
                body_parts.append("【上级引言】\n" + "\n".join(lead_parts))
            if own_body:
                body_parts.append(own_body)
            emit(sec, "child", "\n\n".join(body_parts), breadcrumb, imgs, has_tbl)
            continue

    return split_oversized(_augment_chunks_with_table_summary(chunks), doc_title)


# ─────────────────────────────────────────────
# 超长块拆分（段落/表格边界，表格不拆；单表超限才按行拆并重复表头）
# ─────────────────────────────────────────────
TABLE_BLOCK_RE = re.compile(r"<table>.*?</table>", re.S)
TR_RE = re.compile(r"<tr>.*?</tr>", re.S)


def split_table_html(html, limit):
    """单个表格超限 → 按 <tr> 拆成多个子表，每个子表重复表头行"""
    rows = TR_RE.findall(html)
    if len(rows) <= 2:
        return [html]  # 无法再拆
    head = rows[0]
    parts, cur = [], [head]
    cur_len = len(head)
    for r in rows[1:]:
        if cur_len + len(r) > limit - 20 and len(cur) > 1:
            parts.append("<table>" + "".join(cur) + "</table>")
            cur, cur_len = [head], len(head)
        cur.append(r)
        cur_len += len(r)
    if len(cur) > 1:
        parts.append("<table>" + "".join(cur) + "</table>")
    return parts


def split_oversized(chunks, doc_title):
    """把超过 MAX_CHUNK_CHARS 的块拆成多部分，每部分保留完整章节路径头"""
    out = []
    for c in chunks:
        text = c["text"]
        if len(text) <= MAX_CHUNK_CHARS:
            out.append(c)
            continue
        # 头部（【文档】/【章节路径】行）在每个分块重复
        lines = text.split("\n\n")
        header = lines[0] if lines[0].startswith("【文档】") else f"【文档】{doc_title}"
        body = "\n\n".join(lines[1:]) if lines[0].startswith("【文档】") else text
        # 按表格边界切成原子段：表格整块，其余按段落
        segments = []
        pos = 0
        for m in TABLE_BLOCK_RE.finditer(body):
            before = body[pos:m.start()].strip()
            if before:
                segments.extend(s for s in before.split("\n\n") if s.strip())
            tbl = m.group(0)
            if len(tbl) > MAX_CHUNK_CHARS - len(header) - 100:
                segments.extend(split_table_html(tbl, MAX_CHUNK_CHARS - len(header) - 100))
            else:
                segments.append(tbl)
            pos = m.end()
        tail = body[pos:].strip()
        if tail:
            segments.extend(s for s in tail.split("\n\n") if s.strip())
        # 贪心合并成分块
        parts, cur, cur_len = [], [], 0
        budget = MAX_CHUNK_CHARS - len(header) - 100
        for seg in segments:
            if cur and cur_len + len(seg) > budget:
                parts.append("\n\n".join(cur))
                cur, cur_len = [], 0
            cur.append(seg)
            cur_len += len(seg) + 2
        if cur:
            parts.append("\n\n".join(cur))
        total = len(parts)
        for k, ptext in enumerate(parts, 1):
            sub = dict(c)
            suffix = f"（第{k}/{total}部分）"
            sub["text"] = f"{header}{suffix}\n\n{ptext}"
            sub["title"] = c["title"] + suffix
            sub["breadcrumb"] = c["breadcrumb"] + suffix
            sub["has_table"] = "<table>" in ptext
            sub["image_paths"] = [u for u in c["image_paths"] if u in ptext]
            out.append(sub)
    return out


# ─────────────────────────────────────────────
# 入库 / 导出
# ─────────────────────────────────────────────
def ingest(kb, chunks, doc_id, doc_title, rebuild=False):
    from llama_index.core.schema import TextNode, NodeRelationship, RelatedNodeInfo

    if rebuild:
        try:
            kb._index.delete_ref_doc(doc_id, delete_from_docstore=True)
            print(f"🗑  已删除旧文档节点 ref_doc={doc_id}")
        except Exception as e:
            print(f"（未找到旧节点或删除跳过：{e}）")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nodes = []
    for c in chunks:
        meta = {
            "title": f"{doc_title}｜{c['breadcrumb'].split(' > ')[-1]}",
            "document_id": doc_id,
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
        node.excluded_embed_metadata_keys = list(meta.keys())
        node.excluded_llm_metadata_keys = list(meta.keys())
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=doc_id)
        nodes.append(node)

    print(f"📥 {doc_title}：插入 {len(nodes)} 个节点（向量化中）...")
    kb._index.insert_nodes(nodes)
    return True


def export_json(chunks, slug, cfg):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{os.path.splitext(os.path.basename(cfg['md']))[0]}_chunks.json")
    payload = {
        "document_id": cfg["doc_id"],
        "document_title": cfg["title"],
        "source": cfg["md"],
        "image_base_url": IMAGE_BASE_URL,
        "chunk_count": len(chunks),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chunks": chunks,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"📄 切片预览已导出：{out_path}")


def print_summary(slug, chunks):
    n_parent = sum(1 for c in chunks if c["chunk_type"] in ("parent", "cover", "preface"))
    n_child = sum(1 for c in chunks if c["chunk_type"] == "child")
    n_table = sum(1 for c in chunks if c["has_table"])
    n_img = sum(len(c["image_paths"]) for c in chunks)
    sizes = [len(c["text"]) for c in chunks]
    print(f"── {slug} ──────────────────────────")
    print(f"  总块数: {len(chunks)}  父块/独立块: {n_parent}  子块: {n_child}")
    print(f"  含表格块: {n_table}  图片引用总数: {n_img}")
    print(f"  文本长度: min={min(sizes)} max={max(sizes)} avg={sum(sizes)//len(sizes)}")
    big = [(c['section_no'] or c['chunk_type'], len(c['text'])) for c in chunks if len(c['text']) > MAX_CHUNK_CHARS]
    if big:
        print(f"  ⚠ 仍有超限块(>{MAX_CHUNK_CHARS}字)：{big}")


def main():
    parser = argparse.ArgumentParser(description="信阳各县区实施方案 → LlamaIndex 批量导入")
    parser.add_argument("--doc", default="all", choices=["all"] + list(CONFIGS.keys()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    ensure_env()
    slugs = list(CONFIGS.keys()) if args.doc == "all" else [args.doc]

    all_chunks = {}
    for slug in slugs:
        cfg = CONFIGS[slug]
        if not os.path.isfile(cfg["md"]):
            print(f"❌ 源文件不存在: {cfg['md']}")
            sys.exit(1)
        with open(cfg["md"], "r", encoding="utf-8") as f:
            lines = f.readlines()
        sections = parse_document(lines, cfg["title"])
        chunks = build_chunks(sections, cfg["title"])  # 内部已做综合表摘要增强
        print_summary(slug, chunks)
        export_json(chunks, slug, cfg)
        all_chunks[slug] = chunks

    if args.dry_run:
        print("🧪 dry-run 完成，未入库。")
        return

    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    kb = KnowledgeBaseTool()
    if not kb._ensure_initialized():
        print("❌ LlamaIndex 初始化失败")
        sys.exit(1)

    for slug in slugs:
        cfg = CONFIGS[slug]
        ingest(kb, all_chunks[slug], cfg["doc_id"], cfg["title"], rebuild=args.rebuild)

    kb._index.storage_context.persist(persist_dir=kb._persist_dir)
    print(f"✅ 全部入库并持久化到：{kb._persist_dir}")


if __name__ == "__main__":
    main()
