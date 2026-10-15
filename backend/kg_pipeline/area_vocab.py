# -*- coding: utf-8 -*-
"""采区名词表挖掘（两遍抽取的第 1 遍）。

背景：该语料中「采区/可采区」在公文体里被大量泛用（「各采区」「本采区」
「采区周围河岸」），直接正则抽取会把整句碎片当成采区名（实测 450 个候选里
近半是噪声）。但采区名恰恰集中出现在若干**结构化位置**，精度极高：

  1. 表格单元格      <td>郝楼村可采区 HHK1#</td>
  2. 章节路径末段    … > 1.5.1 石牛村采区
  3. 【本节子目】     1.5.1 石牛村采区；1.5.2 金寨村采区
  4. 许可证绑定     豫信固砂许〔2023〕第 08 号采区（陈营-柳沟采区）
  5. 编号前缀       【浉河：】SH1浉淮村可采区：位于高店乡浉淮村…
  6. 河流/县区前缀   史河汪营可采区、固始县毕店可采区

因此第 1 遍先只从这些位置挖出**采区名词表**，第 2 遍再用词表回扫全文补溯源，
从而同时获得高精度与高覆盖。
"""

import re
from collections import Counter

from .normalize import (
    AREA_SUFFIXES, COUNTIES, RIVERS, is_noise, is_generic,
    display_area_name, normalize_area_name,
)

_SUF = "|".join(AREA_SUFFIXES)

# 采区名核心：允许连字符/顿号等分隔符（陈营-柳沟、南元、祝家楼）
_CORE = r"[\u4e00-\u9fa5][\u4e00-\u9fa5\-—–·、]{1,15}?"

_FACILITY_RE = re.compile(
    r"[\u4e00-\u9fa5\-—–·、]{0,8}(?:储砂场|堆砂场|储砂点|堆砂点|临时堆砂|储料场|砂石集散)"
)
_RESERVED_RE = re.compile(r"[\u4e00-\u9fa5\-—–·、]{0,12}(?:禁采区|保留区)")

# 1. 表格单元格
TD_RE = re.compile(r"<td[^>]*>\s*(" + _CORE + r"(?:" + _SUF + r"))\s*[A-Za-z]{0,4}\s*\d{0,3}\s*[#＃]?\s*</td>")
# 2/3. 章节路径末段、本节子目条目（末尾可带编号，如「郝楼村可采区 HHK1#」）
TITLE_RE = re.compile(r"(" + _CORE + r"(?:" + _SUF + r"))\s*[A-Za-z]{0,4}\s*\d{0,3}\s*[#＃]?\s*$")
# 4. 许可证绑定：「第 08 号采区（陈营-柳沟采区）」——同时给出 License→Area 关系
LICENSE_AREA_RE = re.compile(
    r"(豫[\u4e00-\u9fa5]{0,4}砂许[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*第\s*\d+\s*号)"
    r"\s*(?:可)?采区[^（(]{0,6}[（(]\s*(" + _CORE + r"(?:" + _SUF + r"))\s*[）)]"
)
# 5. 编号前缀：「SH1浉淮村可采区」
CODE_AREA_RE = re.compile(r"([A-Z]{1,3}\d{1,3})\s*(" + _CORE + r"(?:" + _SUF + r"))")
# 6. 河流/县区前缀：「史河汪营可采区」「固始县毕店可采区」
ANCHORED_RE = re.compile(
    r"(" + "|".join(sorted(RIVERS + COUNTIES, key=len, reverse=True)) + r")"
    r"(" + _CORE + r"(?:" + _SUF + r"))"
)
# 图题：「图 5.6-29 固始县陈营-柳沟采区无人机正射影像」
CAPTION_RE = re.compile(r"图\s*\d+(?:[.．]\d+)*[-—–]\d+\s*(" + _CORE + r"(?:" + _SUF + r"))")

_NUM_PREFIX_RE = re.compile(r"^\s*\d+(?:[.．]\d+)*[\s、.．]*")
# 复合名拆分：仅按斜杠，以及「石牛村金寨村」这类多村粘连。
# 顿号/连字符不拆——normalize_area_name 会把它们去掉得到复合名
# （「南元、祝家楼采区」→「南元祝家楼」），拆开反而产生碎片节点。
_SPLIT_RE = re.compile(r"[/]|(?<=村)[、，,]|(?<=村)(?=[\u4e00-\u9fa5]{2,})")
# 编号前缀（SH1/LS13/LG9）与条目前缀
_CODE_PREFIX_RE = re.compile(r"^[A-Z]{1,3}\d{1,3}")
_SUB_ITEM_RE = re.compile(r"【本节子目】([^\n]{0,400})")


def _clean_text(text: str) -> str:
    """挖掉设施名与禁采/保留区，避免其尾部「砂场/采区」被当采区名。"""
    return _RESERVED_RE.sub("〔禁保〕", _FACILITY_RE.sub("〔设施〕", text or ""))


def _candidates(raw: str) -> list:
    """把一处锚点文本拆成若干采区名候选（处理复合名与编号残留）。"""
    raw = _NUM_PREFIX_RE.sub("", (raw or "").strip())
    out = []
    for part in _SPLIT_RE.split(raw):
        part = _CODE_PREFIX_RE.sub("", (part or "").strip())
        if part:
            out.append(part)
    return out


def mine_area_vocabulary(chunks: list) -> dict:
    """挖掘采区名词表。

    返回 {"vocab": {规范名: 频次}, "alias": {变体名: 规范名}}，
    见 _disambiguate 的说明。
    """
    vocab = Counter()
    # 规范名 → 原始书写形态计数（保留连字符/顿号，供节点 raw_name 溯源展示）
    raw_map = {}

    def add(raw):
        for part in _candidates(raw):
            name = normalize_area_name(part)
            disp = display_area_name(part)
            if not name or is_noise(name) or is_generic(name):
                continue
            if not (2 <= len(name) <= 6):
                continue
            # 归一化后「村」出现在中间的是拆分残留（「石牛村金寨」）；
            # 以「村」结尾的是合法采区名（浉淮村可采区 → 浉淮村）
            if "村" in name and not name.endswith("村"):
                continue
            vocab[name] += 1
            if disp:
                raw_map.setdefault(name, Counter())[disp] += 1

    for chunk in chunks:
        text = _clean_text(chunk.get("text", ""))

        # 1. 表格单元格
        for m in TD_RE.finditer(text):
            add(m.group(1))
        # 2/3. 章节路径末段 + 本节子目
        bc = (chunk.get("breadcrumb") or "").strip()
        if bc:
            m = TITLE_RE.search(bc.split(">")[-1].strip())
            if m:
                add(m.group(1))
        for m in _SUB_ITEM_RE.finditer(text):
            for seg in re.split(r"[；;]", m.group(1)):
                mm = TITLE_RE.search(seg.strip())
                if mm:
                    add(mm.group(1))
        # 4. 许可证绑定（括号内是权威采区名）
        for m in LICENSE_AREA_RE.finditer(text):
            add(m.group(2))
        # 5. 编号前缀
        for m in CODE_AREA_RE.finditer(text):
            add(m.group(2))
        # 6. 河流/县区前缀
        for m in ANCHORED_RE.finditer(text):
            add(m.group(2))
        # 7. 图题
        for m in CAPTION_RE.finditer(text):
            add(m.group(1))

    return _disambiguate(vocab, raw_map)


# 消歧阈值：短名频次不足长名（前缀/后缀包含）的 1/4 时，判定为碎片并入长名
_MERGE_RATIO = 4


def _disambiguate(vocab: Counter, raw_map: dict = None) -> dict:
    """合并指向同一采区的名称变体。

    返回 {"vocab": {规范名: 频次}, "alias": {变体名: 规范名}, "raw": {规范名: [原始写法]}}。
    alias 同时供 PostgreSQL 侧合并使用——PG 写「郝楼砂场」而知识库写
    「郝楼村可采区」，两侧归一化后分别是「郝楼」「郝楼村」，必须靠 alias 对齐。
    """
    names = sorted(vocab, key=lambda n: (-vocab[n], -len(n)))
    alias = {}

    for name in names:
        if name in alias:
            continue
        # 碎片：是某个更高频名称的子串（前缀「大营」⊂「大营范营」、
        # 后缀「大庄」⊂「郑堂大庄」），且频次远低
        best = ""
        for other in names:
            if other == name or other in alias or name not in other:
                continue
            if vocab[name] * _MERGE_RATIO <= vocab[other]:
                if not best or vocab[other] > vocab[best]:
                    best = other
        if best:
            alias[name] = best

    # 「X村」与「X」同指一采区（郝楼村可采区 ↔ 郝楼砂场）
    for name in names:
        if name in alias or not name.endswith("村") or len(name) < 3:
            continue
        bare = name[:-1]
        if bare in vocab and bare not in alias:
            if vocab[bare] > vocab[name]:
                alias[name] = bare
            else:
                alias[bare] = name

    # 拼接伪影：低频名恰好是两个更高频名的拼接/换序，或去掉首字后即为已知名
    # （「汪营红石」「叶岗沈营」「龙潭牛老家」「黎郑堂大庄」）。这些是 _CORE
    # 正则跨顿号/跨前缀贪婪匹配的产物，其组成片段已是独立节点，直接丢弃。
    known = set(vocab) | set(alias)

    def _canon(n):
        return alias.get(n, n)

    artifacts = set()
    for name, count in vocab.items():
        if count > 3 or name in alias:
            continue
        if name[1:] in known and vocab.get(_canon(name[1:]), 0) > count:
            artifacts.add(name)
            continue
        for i in range(1, len(name)):
            a, b = name[:i], name[i:]
            if a in known and b in known:
                if vocab.get(_canon(a), 0) > count and vocab.get(_canon(b), 0) > count:
                    artifacts.add(name)
                    break
            if (b + a) in vocab and vocab[b + a] > count:
                artifacts.add(name)
                break

    merged = Counter()
    merged_raw = {}
    for name, count in vocab.items():
        if name in artifacts:
            continue
        canon = alias.get(name, name)
        merged[canon] += count
        if raw_map and name in raw_map:
            merged_raw.setdefault(canon, Counter()).update(raw_map[name])
    raw_out = {k: [w for w, _ in c.most_common(5)] for k, c in merged_raw.items()}
    return {"vocab": dict(merged.most_common()), "alias": alias, "raw": raw_out}


def mine_license_area_relations(chunks: list) -> list:
    """抽取「许可证 → 采区」绑定关系（许可证号 + 采区名）。"""
    rels = []
    seen = set()
    for chunk in chunks:
        text = _clean_text(chunk.get("text", ""))
        for m in LICENSE_AREA_RE.finditer(text):
            lic, area = m.group(1), normalize_area_name(m.group(2))
            if not area or is_noise(area) or is_generic(area) or not (2 <= len(area) <= 6):
                continue
            key = (lic, area)
            if key in seen:
                continue
            seen.add(key)
            rels.append({"license": lic, "area": area, "chunk_id": chunk.get("chunk_id", "")})
    return rels