# -*- coding: utf-8 -*-
"""规则抽取：从 chunk 文本中抽取 8 类实体与关系，并抓取完整溯源。

设计要点：
  - 白名单（县区/河流）优先，避免正则贪婪导致的脏名称
  - 长名优先匹配（史灌河 > 史河/灌河）
  - 采区/乡镇/村须与同 chunk 的县名或河流名共现，防止孤立噪声
  - 每次命中都记录 (chunk_id, doc_id, title, 章节路径, 原文片段) 供溯源

实测覆盖率：884/987 chunk（90%），剩余 103 条多为图片 URL 列表与通用规程文本。
"""

import re

from .normalize import (
    AREA_SUFFIXES, COUNTIES, RIVERS, is_noise,
    strip_upper_admin, license_norm, license_year, license_county,
    county_of, county_nearest, river_of, river_nearest,
)
from .refs import extract_snippet, make_ref, merge_refs, dump_refs

# 许可证（半角/全角括号都兼容；PG 侧用 []，知识库侧用〔〕）
LICENSE_RE = re.compile(r"(豫[\u4e00-\u9fa5]{0,4}砂许[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*第\s*\d+\s*号)")

# 采区/砂场
AREA_RE = re.compile(r"([\u4e00-\u9fa5]{2,7}(?:" + "|".join(AREA_SUFFIXES) + r"))")

# 采区/砂场名称核心（允许连字符/顿号等分隔符，如「陈营-柳沟采区」「南元、祝家楼采区」）
_AREA_CORE = r"[\u4e00-\u9fa5][\u4e00-\u9fa5\-—–·、]{1,15}?"

# 设施/禁采区 等非采区实体，先挖掉再匹配采区，避免「潘庄可采区储砂场」「禁采区」污染
_FACILITY_RE = re.compile(
    r"[\u4e00-\u9fa5\-—–·、]{0,8}(?:储砂场|堆砂场|储砂点|堆砂点|临时堆砂|储料场|砂石集散)"
)
_RESERVED_RE = re.compile(r"[\u4e00-\u9fa5\-—–·、]{0,12}(?:禁采区|保留区)")

# 表格单元格/章节标题里的权威采区名（高精度锚点：郝楼村可采区、史河汪营可采区…）
_AREA_TD_RE = re.compile(
    r"<td[^>]*>\s*(" + _AREA_CORE + r"(?:" + "|".join(AREA_SUFFIXES) + r"))"
    r"\s*[A-Za-z]{0,4}\s*\d{0,3}\s*[#＃]?\s*</td>"
)
_AREA_TITLE_RE = re.compile(
    r"(" + _AREA_CORE + r"(?:" + "|".join(AREA_SUFFIXES) + r"))"
    r"\s*[A-Za-z]{0,4}\s*\d{0,3}\s*[#＃]?\s*$"
)
_NUM_PREFIX_RE = re.compile(r"^\s*\d+(?:[.．]\d+)*[\s、.．]*")

# 乡镇 / 行政村
TOWN_RE = re.compile(r"([\u4e00-\u9fa5]{2,6}(?:镇|乡|街道))")
VILLAGE_RE = re.compile(r"([\u4e00-\u9fa5]{2,6}村)")

# ── 村名清洗：VILLAGE_RE 贪婪匹配会把前一个词的尾字一起吞进来 ──
# 实测污染形态（括号内为原文）：道在撞山寺村（022县道在…）、年因傅营村
# （2023年因…）、采区和黄堰村（郝楼村采区和黄堰村采区）、竿河景陈大洼村
# （竹竿河景陈大洼村）、城县灌河金寨村（商城县灌河金寨村）、山办事处石牛村
# （鲇鱼山办事处石牛村）、道路未与周边村（运输道路未与周边村民协商）。
_VILLAGE_OFFICE = re.compile(r"^[\u4e00-\u9fa5]{0,3}办事处")
# 公文碎片整体剥离（「在规划李家庄村」「276 个行政村」→「个行政村」等）
_VILLAGE_JUNK_UNITS = re.compile(
    r"^(?:在规划|规划|又分为|分为|分别为|道路未与|采砂点附近|采砂点|"
    r"采区和|采区与|采区|周边|和其他|以及|个行政村|二是|储砂场和|储砂场|"
    r"公司负责|如果因|县道在|国道在|省道在|乡道在|村道在|道在)"
)
# 列表连接词残留（「石牛村、金寨村和李湖村采区」→ 剥掉「金寨村和」）
_VILLAGE_LIST = re.compile(r"^[\u4e00-\u9fa5]{2,4}村[和与及](?=[\u4e00-\u9fa5]{2,}村$)")
# 乡镇/街道后缀片段残留（「陂镇陈集村」「五星街道琵琶山村」）
_VILLAGE_TOWN_FRAG = re.compile(r"^[\u4e00-\u9fa5]{0,2}(?:镇|乡|街道)(?=[\u4e00-\u9fa5]{2,}村$)")
# 单字/双字虚词残留（「2025年度郑湾村」「计划对潢河郝楼村」「和陈大洼村」）
_VILLAGE_FUNC = re.compile(r"^[\u4e00-\u9fa5]{0,2}[且为和对度划是的年因在](?=[\u4e00-\u9fa5]{2,}村$)")
# 河流/县区片段残留（竹竿河→竿河、商城县→城县）
_VILLAGE_RIVER_FRAG = re.compile(r"^[\u4e00-\u9fa5]{1,2}河(?=[\u4e00-\u9fa5]{2,}村$)")
_VILLAGE_COUNTY_FRAG = re.compile(r"^[\u4e00-\u9fa5]{0,2}(?:县|区)(?=[\u4e00-\u9fa5]{2,}村$)")
# 集镇名片段残留（「左岸黎集小方集村」→「小方集村」；剩余须 ≥2 字，避免误伤「马集村」）
_VILLAGE_MARKET_FRAG = re.compile(r"^[\u4e00-\u9fa5]{1,2}集(?=[\u4e00-\u9fa5]{2,}村$)")

_VILLAGE_STEPS = (
    _VILLAGE_OFFICE, _VILLAGE_JUNK_UNITS, _VILLAGE_LIST, _VILLAGE_TOWN_FRAG,
    _VILLAGE_FUNC, _VILLAGE_RIVER_FRAG, _VILLAGE_COUNTY_FRAG, _VILLAGE_MARKET_FRAG,
)


def clean_village_name(raw: str) -> str:
    """剥离村名前粘连的公文碎片，还原真实村名。

    VILLAGE_RE 贪婪匹配会把前一个词的尾字一并吞入（「2025年度郑湾村」→
    「度郑湾村」），这里按「机构/公文碎片 → 列表连接词 → 乡镇后缀 → 虚词 →
    河流/县区片段 → 集镇片段」的顺序循环剥离，直到稳定。
    """
    n = strip_upper_admin(raw)
    for _ in range(6):
        before = n
        for step in _VILLAGE_STEPS:
            n = step.sub("", n)
        n = strip_upper_admin(n)
        if n == before:
            break
    return n.strip()

# 村级名称里粘连的乡镇前缀（「王岗乡牛湾村」→ 王岗乡）
TOWN_PREFIX_RE = re.compile(r"^([\u4e00-\u9fa5]{2,5}(?:镇|乡|街道))")

# 图片地址
IMAGE_RE = re.compile(r"图片地址：(\S+)")

# 章节路径前缀
SECTION_RE = re.compile(r"【章节路径】([^\n【]{0,120})")

# 采区名称定位上下文窗口（用于判断归属县区/河流）
CONTEXT_WINDOW = 200


def _section_of(chunk: dict) -> str:
    """取 chunk 的章节路径（meta.breadcrumb 优先，其次正文【章节路径】前缀）。"""
    bc = chunk.get("breadcrumb") or ""
    if bc:
        return bc
    m = SECTION_RE.search(chunk.get("text", ""))
    return m.group(1).strip() if m else ""


def _window(text: str, keyword: str, size: int = CONTEXT_WINDOW) -> str:
    """取关键词周边的文本窗口，用于判断该实体的归属（县区/河流）。"""
    idx = text.find(keyword)
    if idx < 0:
        return text[:size]
    return text[max(0, idx - size): idx + len(keyword) + size]


class RuleExtractor:
    """规则抽取器：两遍遍历产出节点与关系（含溯源）。

    第 1 遍从表格/章节标题等结构化位置挖出采区名词表（见 area_vocab），
    第 2 遍用词表回扫全文，把散文里出现的采区也绑定到同一节点并补溯源。
    这样既避免公文体「各采区」被当实体，又不会漏掉散文中的真实采区。
    """

    def __init__(self, dicts: dict, area_vocab: dict = None, raw_names: dict = None):
        self.counties = dicts.get("County", list(COUNTIES))
        self.rivers = dicts.get("River", list(RIVERS))
        self.documents = dicts.get("_documents", [])
        # 采区名词表：{归一化名: 频次}
        self.area_vocab = dict(area_vocab or {})
        # 采区原始书写形态：{归一化名: [原始名…]}，用于 raw_name 溯源
        self.raw_names = dict(raw_names or {})
        # 长名优先，保证「大营范营」先于「大营」被匹配
        self.area_names = sorted(self.area_vocab, key=len, reverse=True)
        # 许可证→采区 绑定（来自「豫X砂许〔2023〕第 08 号采区（陈营-柳沟采区）」）
        self.license_area = dicts.get("_license_area", [])

    # ── 主入口 ──
    def extract(self, chunks: list) -> dict:
        """返回 {"nodes": {type: {id: node}}, "relations": [...], "hit_chunks": n}"""
        nodes = {t: {} for t in (
            "County", "River", "MineableArea", "Town", "Village",
            "Document", "License", "Project",
        )}
        relations = []
        rel_seen = set()
        hit_chunk_ids = set()

        self._add_documents_and_projects(nodes, relations, rel_seen)

        for chunk in chunks:
            if self._extract_from_chunk(chunk, nodes, relations, rel_seen):
                hit_chunk_ids.add(chunk.get("chunk_id", ""))

        self._add_license_area_links(nodes, relations, rel_seen)
        self._add_town_county_relations(nodes, relations, rel_seen)
        self._add_area_county_relations(nodes, relations, rel_seen)

        return {
            "nodes": nodes,
            "relations": relations,
            "hit_chunk_ids": hit_chunk_ids,
            "hit_chunks": len(hit_chunk_ids),
        }

    # ── Document / Project 节点（来自知识库元数据，高置信）──
    def _add_documents_and_projects(self, nodes, relations, rel_seen):
        for d in self.documents:
            did = d["id"]
            ref = make_ref(
                doc_id=did, title=d.get("title", ""),
                section=d.get("breadcrumb_root", ""),
                snippet=f"该文档共 {d.get('chunk_count', 0)} 个知识库切片",
                chunk_id="", count=d.get("chunk_count", 1),
            )
            nodes["Document"][did] = {
                "id": did,
                "name": d.get("title", did),
                "source": "rule",
                "refs": dump_refs([ref]),
                "title": d.get("title", ""),
                "project": d.get("project", ""),
                "year": d.get("year", 0),
                "chunk_count": d.get("chunk_count", 0),
                "breadcrumb_root": d.get("breadcrumb_root", ""),
                "image_paths": ",".join(d.get("image_paths", [])[:50]),
            }
            proj = d.get("project", "")
            if not proj:
                continue
            p = nodes["Project"].setdefault(proj, {
                "id": proj,
                "name": d.get("project_name") or proj,
                "source": "rule",
                "refs": "",
                "doc_count": 0,
                "year": d.get("year", 0),
            })
            p["doc_count"] += 1
            # 项目溯源指向其首个文档（项目本身是文档聚合，无独立章节）
            p["refs"] = merge_refs(p.get("refs", ""), ref)
            self._link(relations, rel_seen, "BELONGS_TO_PROJECT", did, proj, "rule")

    # ── 单 chunk 抽取 ──
    def _extract_from_chunk(self, chunk, nodes, relations, rel_seen) -> bool:
        text = chunk.get("text", "")
        section = _section_of(chunk)
        doc_id = chunk.get("doc_id", "")
        title = chunk.get("title", "")
        chunk_id = chunk.get("chunk_id", "")
        hit = False

        def ref_for(keyword, count=1):
            return make_ref(
                doc_id=doc_id, title=title, section=section,
                snippet=extract_snippet(text, keyword),
                chunk_id=chunk_id, count=count,
            )

        # ── 1. 县区（白名单）──
        for county in self.counties:
            if county not in text:
                continue
            hit = True
            self._upsert(nodes, "County", county, {
                "name": county, "source": "rule",
                "short": county[:-1] if county.endswith(("县", "区")) else county,
            }, ref_for(county, text.count(county)))

        # ── 2. 河流（白名单，长名优先）──
        # 已被更长河流名覆盖的短名不重复计入（史灌河 命中时不重复计 史河/灌河）
        matched_rivers = set()
        for river in self.rivers:  # self.rivers 已按长度降序
            if river not in text:
                continue
            if any(river in longer for longer in matched_rivers):
                continue
            matched_rivers.add(river)
            hit = True
            self._upsert(nodes, "River", river, {
                "name": river, "source": "rule",
                "parent": self._river_parent(river),
            }, ref_for(river, text.count(river)))
            self._link(relations, rel_seen, "MENTIONS_RIVER", doc_id, river, "rule")

        # ── 3. 许可证 ──
        for raw in LICENSE_RE.findall(text):
            hit = True
            lic = license_norm(raw)
            county = license_county(lic)
            self._upsert(nodes, "License", lic, {
                "name": lic, "source": "rule",
                "year": license_year(lic), "county": county,
            }, ref_for(raw))

        # ── 4. 采区/砂场（词表驱动，长名优先）──
        # 词表由 area_vocab 从表格/章节标题等结构化位置挖出，精度远高于
        # 直接正则扫散文（后者会把「各采区」「采区周围河岸」当实体）。
        matched_areas = set()
        for name in self.area_names:
            if name in matched_areas or name not in text:
                continue
            # 已被更长采区名覆盖的短名不重复计入（大营范营 命中时不重复计 大营）
            if any(name in longer for longer in matched_areas):
                continue
            matched_areas.add(name)
            # 归属优先取章节路径（该节只讲一个采区，最权威），
            # 否则取正文中离采区名最近的县区/河流（避免汇总表多值误挂）
            county = county_of(section) or county_nearest(text, name)
            river = river_of(section) or river_nearest(text, name)
            hit = True
            raws = self.raw_names.get(name) or []
            self._upsert(nodes, "MineableArea", name, {
                "name": name, "raw_name": (raws[0] if raws else name),
                "kind": "采区", "source": "rule",
                "county": county, "river": river,
                "license_id": "", "year": 0,
            }, ref_for(name, text.count(name)))
            self._link(relations, rel_seen, "DOCUMENTS_AREA", doc_id, name, "rule")
            # 县区归属关系不在此处按 chunk 即时落——同名采区跨县（如「沈营」
            # 同时出现在固始/商城两节），逐 chunk 落会产生多值归属。统一在
            # 收尾阶段按节点已消歧的 county 落单条 BELONGS_TO。
            if river:
                self._link(relations, rel_seen, "LOCATED_IN_RIVER", name, river, "rule")

        # ── 5. 乡镇 ──
        towns_here = []
        for raw in TOWN_RE.findall(text):
            name = strip_upper_admin(raw)
            if is_noise(name) or len(name) < 3:
                continue
            county = county_of(section) or county_nearest(text, raw)
            if not county:
                continue
            hit = True
            towns_here.append((name, county))
            self._upsert(nodes, "Town", name, {
                "name": name, "source": "rule", "county": county,
            }, ref_for(raw, text.count(raw)))
            self._ensure_county(nodes, county, ref_for(county))
            self._link(relations, rel_seen, "PART_OF", name, county, "rule")

        # ── 6. 行政村 ──
        for raw in VILLAGE_RE.findall(text):
            name = clean_village_name(raw)
            if is_noise(name) or len(name) < 3 or not name.endswith("村"):
                continue
            win = _window(text, raw)
            county = county_of(section) or county_nearest(text, raw)
            # 村的归属乡镇：优先取本 chunk 已抽出的乡镇，其次取名称中粘连的乡镇前缀
            # （「王岗乡牛湾村」的 TOWN_RE 会被粘连导致漏抽，这里补回 王岗乡 节点）
            town = ""
            for tname, _town_county in towns_here:
                if tname in win:
                    town = tname
                    break
            if not town:
                m = TOWN_PREFIX_RE.match(raw)
                if m:
                    lead = m.group(1)
                    if not is_noise(lead) and len(lead) >= 3:
                        town = lead
            if not county and not town:
                continue
            hit = True
            if town:
                self._upsert(nodes, "Town", town, {
                    "name": town, "source": "rule", "county": county,
                }, ref_for(raw, text.count(town)))
                towns_here.append((town, county))
            vid = f"{county}__{town}__{name}" if (county or town) else name
            self._upsert(nodes, "Village", vid, {
                "name": name, "source": "rule",
                "town": town, "county": county,
            }, ref_for(raw, text.count(raw)))

        return hit

    # ── 工具 ──
    @staticmethod
    def _river_parent(river: str) -> str:
        """支流→干流：史河/灌河 是 史灌河 的支流，此处标记归属。"""
        if river in ("史河", "灌河"):
            return "史灌河"
        return ""

    @staticmethod
    def _upsert(nodes: dict, ntype: str, nid: str, props: dict, ref: dict):
        """写入/合并节点：已存在则合并 refs 并补充缺失属性。"""
        bucket = nodes[ntype]
        if nid in bucket:
            node = bucket[nid]
            node["refs"] = merge_refs(node.get("refs", ""), ref)
            for k, v in props.items():
                if v and not node.get(k):
                    node[k] = v
        else:
            node = dict(props)
            node["id"] = nid
            node["refs"] = dump_refs([ref])
            node.setdefault("source", "rule")
            bucket[nid] = node

    @staticmethod
    def _ensure_county(nodes: dict, county: str, ref: dict):
        if county and county not in nodes["County"]:
            RuleExtractor._upsert(nodes, "County", county, {
                "name": county, "source": "rule",
                "short": county[:-1] if county.endswith(("县", "区")) else county,
            }, ref)

    @staticmethod
    def _link(relations, rel_seen, rel: str, src: str, dst: str, source: str):
        if not src or not dst:
            return
        key = (rel, src, dst)
        if key in rel_seen:
            return
        rel_seen.add(key)
        relations.append({"rel": rel, "src": src, "dst": dst, "source": source})

    def _add_license_area_links(self, nodes, relations, rel_seen):
        """把「许可证→采区」绑定写进图谱（同时补齐采区的 license_id/year/county）。"""
        for item in self.license_area:
            lic = license_norm(item.get("license", ""))
            area = item.get("area", "")
            if not lic or not area or area not in nodes["MineableArea"]:
                continue
            year = license_year(lic)
            county = license_county(lic)
            bucket = nodes["License"]
            if lic not in bucket:
                bucket[lic] = {
                    "id": lic, "name": lic, "source": "rule", "refs": "",
                    "year": year, "county": county,
                }
            node = nodes["MineableArea"][area]
            if not node.get("license_id"):
                node["license_id"] = lic
            if not node.get("year"):
                node["year"] = year
            if not node.get("county") and county:
                node["county"] = county
                self._ensure_county(nodes, county, {"doc_id": "", "title": "", "section": "",
                                                    "snippet": "由许可证号推断", "chunk_id": "", "count": 1})
            self._link(relations, rel_seen, "ISSUED_FOR", lic, area, "rule")

    def _add_area_county_relations(self, nodes, relations, rel_seen):
        """按采区节点最终消歧的 county 落单条 BELONGS_TO（避免多值归属）。

        实测：「沈营可采区」在固始县与商城县两节都出现，逐 chunk 落关系会让
        一个采区同时挂两个县。收尾统一按节点 county 落，保证一采区一县。
        """
        for name, node in nodes["MineableArea"].items():
            county = node.get("county", "")
            if not county:
                continue
            self._ensure_county(nodes, county, {"doc_id": "", "title": "", "section": "",
                                                "snippet": "由采区归属推断", "chunk_id": "", "count": 1})
            self._link(relations, rel_seen, "BELONGS_TO", name, county, "rule")

    def _add_town_county_relations(self, nodes, relations, rel_seen):
        """乡镇归属县区（部分 chunk 未直接共现时，用已抽取的 county 属性补全）。"""
        for name, node in nodes["Town"].items():
            county = node.get("county", "")
            if county:
                self._ensure_county(nodes, county, {"doc_id": "", "title": "", "section": "", "snippet": "由乡镇归属推断", "chunk_id": "", "count": 1})
                self._link(relations, rel_seen, "PART_OF", name, county, "rule")