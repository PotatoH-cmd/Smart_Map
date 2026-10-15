# -*- coding: utf-8 -*-
"""实体名称归一化与 id 生成。

知识库与 PostgreSQL 对同一采区的写法差异很大，必须归一化后才能合并：

    PostgreSQL                        知识库
    ─────────────────────────────     ─────────────────────
    史灌河童庙可采区                   童庙可采区
    大营-范营可采区                    范营可采区
    灌河淮堰-周集可采区                周集可采区
    潘庄砂场                          王楼砂场

归一化后 `童庙` 两侧一致 → 合并为同一节点，river/year 作为属性共存。
"""

import re

# ── 县区白名单（信阳市辖 10 个县区）──
COUNTIES = [
    "浉河区", "平桥区", "罗山县", "光山县", "新县",
    "商城县", "潢川县", "淮滨县", "息县", "固始县",
]

# ── 主干河流/水库白名单（按长度降序，保证长名优先匹配）──
RIVERS = sorted([
    "史灌河", "史河", "灌河", "淮河", "浉河", "白露河", "竹竿河",
    "潢河", "小潢河", "洪河", "泉河", "春河", "马家河", "夹河",
    "出山店水库", "袁湾水库", "鲇鱼山水库", "泼陂河", "塔沙河",
], key=len, reverse=True)

# ── 采区类实体后缀（按长度降序，先匹配长后缀）──
# 注意：不含「河段」「清淤区」——实测这两个太泛，会抽出
# 「重要支流易盗采河段」「疏浚砂处置利用河段」这类非实体短语。
AREA_SUFFIXES = sorted([
    "可采区", "采砂场", "开采区", "可采段", "可采点", "采区", "采点", "砂场",
], key=len, reverse=True)

# ── 县区简称（用于剥离「固始史河毕店」这类缺「县」字的前缀）──
COUNTY_SHORTS = ["固始", "潢川", "罗山", "光山", "商城", "淮滨", "息县", "新县", "平桥", "浉河"]

# ── 无意义前缀（抽出的「本采区/该采区/各采区」等不是实体）──
NOISE_PREFIX = re.compile(
    r"^(本|该|各|上述|全部|每|号|此|同一|相关|部分|年度|规划|批复|"
    r"开采|禁|某|两|三|四|五|新老|现有|原有|其余|其他|以下)"
)

# ── 非实体词（实测抽出过这类设备/作业/临时设施/动作描述，均不是实体）──
# 该语料是公文体，抽象名词与动词密集，不滤会抽出
# 「重要支流易盗采河段」「疏浚砂处置利用」这类短语。
NOISE_CONTAINS = re.compile(
    r"(声呐|波束|测量|监测|影像|航拍|无人机|定位|视频|监控|"
    r"数据|系统|平台|报告|意见|方案|标准|规范|要求|措施|管理|"
    r"堆砂|临时|布置|转运|堆放|筛分|停靠|砂石|分离|弃料|"
    r"建议|应当|进行|实施|开展|加强|完善|推进|确保|结合|按照|根据|"
    r"支流|处置|利用|涉及|包括|存在|发现|位于|属于|用于|通过|采取|"
    r"禁止|防止|避免|控制|落实|督促|整改|核查|情况|工作|内容|方面|"
    # 泛化指代/数量词（「所有采区」「确定本…」「其中两个」不是实体）
    r"所有|其中|其他|其余|全部|确定|规定|制定|明确|监督|统筹|涵盖|关联|生成|"
    r"实际|分别|同时|目前|现有|新增|计划|划分|划定|符合|满足|考虑|支持|"
    r"下游|上游|左岸|右岸|河段|部分|区域|范围|所属|许可|共设|批复|上级|面积|"
    r"长度|宽度|高程|深度|桩号|坐标|名称|数量|位置|边界|总量|开采量|"
    # 机构名/公文体（「市农业农村局」「276 个行政村」不是村）
    r"农业|行政|负责|附近)"
)

# 裸后缀本身不是实体名（正则会把「各采区」的「采区」单独抽出来）
GENERIC_NAMES = set(AREA_SUFFIXES) | {"采区", "砂场", "采点", "可采区", "采砂场"}


def is_generic(name: str) -> bool:
    """是否为裸后缀/纯河流名等泛化词，不构成采区实体。"""
    if name in GENERIC_NAMES:
        return True
    if name in RIVERS or name in COUNTIES:
        return True
    # 公文虚词碎片（「的三个」「所属」「该采区」）
    if re.match(r"^(的|该|本|所|其|各|每)", name):
        return True
    # 采区名不应带乡镇后缀（「鄢岗镇李家楼」是乡镇+村名的残留）；
    # 但「吴乡村」这类以村结尾的名称，其中的「乡」是村名的一部分
    if re.search(r"(镇|街道)", name):
        return True
    if "乡" in name and not name.endswith("村"):
        return True
    return bool(re.search(r"(批复|上述|以上|以下|几个|两个|三个|四个|五个|所有|其中)", name))

# ── 行政区后缀 ──
TOWN_SUFFIXES = ["镇", "乡", "街道"]
VILLAGE_SUFFIXES = ["村"]

# 名称中需要剥离的分隔符
_SEPARATORS = re.compile(r"[-—–·、,，\s]+")

# 名称开头的乡镇前缀（「庙仙乡南李店村、吴乡村」→ 剥掉 庙仙乡）
_TOWN_PREFIX_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}(?:镇|乡|街道)(?=[\u4e00-\u9fa5]{2,})")

# OCR 混淆字归一（源文档扫描识别错误导致同名采区出现两种写法）
_OCR_FIX = [("塆", "湾"), ("谭", "潭"), ("迎合", "迎河"), ("胜湖", "人主")]


def strip_admin_prefix(name: str) -> str:
    """剥离名称前面的行政/河流前缀。

    实测必需：否则会抽出「潢川县潢河」「阳市竹竿河」「固始县史灌河」这类脏名称。
    """
    n = name
    # 剥离「信阳市/阳市/全市」等残缺前缀（长前缀优先，最后才剥离单个残留的市/县/区）
    n = re.sub(r"^(信阳市|阳市|全市|河南省|豫)", "", n)
    if len(n) > 3 and n[0] in "市县区":
        n = n[1:]
    # 剥离县区前缀
    for c in COUNTIES:
        if n.startswith(c) and len(n) > len(c):
            n = n[len(c):]
            break
    else:
        # 白名单未覆盖的县区写法（如 OCR 变体「泺河区东双河镇」）用通用规则剥离，
        # 但要求剥离后仍是完整词，避免误伤「东双河镇」这类本身含「河」的名称
        n = re.sub(r"^[\u4e00-\u9fa5]{2,3}(?:县|区)(?=[\u4e00-\u9fa5]{3,})", "", n, count=1)
    # 剥离最长河流前缀（史灌河 优先于 史河/灌河）
    # 要求剩余部分至少 2 字，否则「淮河村采区」会被剥成「村」
    for r in RIVERS:
        if n.startswith(r) and len(n) - len(r) >= 2:
            rest = n[len(r):]
            # 剩余部分必须含汉字：否则「竹竿河-1」会被剥成「1」（PG 侧的
            # 竹竿河 1~7 号采区编号），丢失全部语义
            if re.search(r"[\u4e00-\u9fa5]", rest):
                n = rest
                break
    return n.strip()


def strip_upper_admin(name: str) -> str:
    """剥离名称中重复出现的上级行政区前缀。

    实测「王岗乡牛湾村」「洋河镇小洋河村」这类村级名称带上了乡镇前缀，
    「东双河镇」这类镇级名称带上了县区前缀，需逐层剥掉。
    """
    n = strip_admin_prefix(name)
    changed = True
    while changed and len(n) > 3:
        changed = False
        m = re.match(r"^([\u4e00-\u9fa5]{2,5}(?:镇|乡|街道))", n)
        if m and len(n) > len(m.group(1)):
            rest = n[len(m.group(1)):]
            if rest.endswith(("村", "镇", "乡", "街道")):
                n = rest
                changed = True
    return n


def strip_area_suffix(name: str) -> str:
    """剥离「可采区/采区/砂场/可采点」等后缀，得到核心地名。

    循环剥离：实测有「潘庄可采区储砂场」这类后缀叠加的写法。
    """
    n = name
    changed = True
    while changed:
        changed = False
        for s in AREA_SUFFIXES:
            if n.endswith(s) and len(n) > len(s):
                n = n[: -len(s)]
                changed = True
                break
    return n


def strip_county_short(name: str) -> str:
    """剥离缺「县」字的县区简称前缀：「固始史河毕店」→「史河毕店」。"""
    for s in sorted(COUNTY_SHORTS, key=len, reverse=True):
        if name.startswith(s) and len(name) > len(s):
            return name[len(s):]
    return name


def _clean_area_core(name: str) -> str:
    """剥掉采区名前后缀（县区/河流/乡镇/采区后缀），保留中间的分隔符。

    循环剥离：剥掉县区简称后可能又露出河流前缀（固始史河郑堂大庄），
    必须反复直到稳定。
    """
    n = name
    for _ in range(4):
        before = n
        # 先去后缀再剥前缀：否则「淮河村采区」会先被当成「淮河」+剩余3字而剥成「村」
        n = strip_area_suffix(n)
        n = _TOWN_PREFIX_RE.sub("", n)
        n = strip_admin_prefix(n)
        n = strip_county_short(n)
        if n == before:
            break
    for a, b in _OCR_FIX:
        n = n.replace(a, b)
    return n.strip()


def normalize_area_name(name: str) -> str:
    """采区/砂场名称归一化（合并主键，去掉全部分隔符）。

    「史灌河童庙可采区」→「童庙」；「陈营-柳沟采区」→「陈营柳沟」；
    「南元-祝家楼采区」→「南元祝家楼」；「固始史河郑堂大庄采区」→「郑堂大庄」。
    """
    return _SEPARATORS.sub("", _clean_area_core(name))


def display_area_name(name: str) -> str:
    """采区名展示形态：与 normalize 同源，但保留分隔符（统一为半角连字符）。

    「陈营-柳沟采区」→「陈营-柳沟」、「南元、祝家楼采区」→「南元-祝家楼」。
    作为节点 raw_name 落库，保证图谱能还原源文档的原始书写形态。
    """
    n = _clean_area_core(name)
    n = re.sub(r"\s+", "", n)
    n = re.sub(r"[-—–·、,，]+", "-", n)
    return n.strip("-")


# 名称尾部虚词：中文地名不会以这些字结尾，出现即说明正则把限定词/助词
# 一起吞了（「商城县灌河各采区」→「灌河各」）
_BAD_TAIL = set("各的等其该本所每此又并")


def is_noise(name: str) -> bool:
    """判断是否为无意义名称（本采区/该采区/波束声呐测量采区…）。"""
    if not name or len(name) < 2:
        return True
    if name[-1] in _BAD_TAIL:
        return True
    if "办事处" in name:
        return True
    if NOISE_PREFIX.match(name):
        return True
    return bool(NOISE_CONTAINS.search(name))


def county_short(county: str) -> str:
    """县区简称：固始县 → 固始。"""
    if not county:
        return ""
    for suffix in ("县", "区"):
        if county.endswith(suffix):
            return county[: -len(suffix)]
    return county


def county_of(text: str) -> str:
    """从文本中识别县区（返回白名单中的第一个命中）。"""
    for c in COUNTIES:
        if c in text:
            return c
    return ""


def nearest_of(text: str, keyword: str, names: list) -> str:
    """返回离关键词最近的词表命中（按字符距离，同距取先出现的）。

    实测必需：`county_of(200 字窗口)` 取的是窗口内**先出现**的县区，而汇总表
    类 chunk 常同时列举多个县区，导致「郑堂大庄」被同时挂到固始县与新县。
    改为按字符距离取最近者，显著减少这类误挂。
    """
    if not text:
        return ""
    idx = text.find(keyword) if keyword else -1
    if idx < 0:
        return ""
    best, best_d = "", 1 << 30
    for name in names:
        start = 0
        while True:
            p = text.find(name, start)
            if p < 0:
                break
            d = abs(p - idx)
            if d < best_d:
                best, best_d = name, d
            start = p + 1
    return best


def county_nearest(text: str, keyword: str) -> str:
    return nearest_of(text, keyword, COUNTIES) or county_of(text)


def river_nearest(text: str, keyword: str) -> str:
    return nearest_of(text, keyword, RIVERS) or river_of(text)


def river_of(text: str) -> str:
    """从文本中识别河流（长名优先，如 史灌河 优先于 史河）。"""
    for r in RIVERS:
        if r in text:
            return r
    return ""


def area_id(norm_name: str) -> str:
    """采区节点 id。

    以归一化地名为主键，使 PostgreSQL 与知识库两侧的同名采区合并；
    仅当确需区分时才由调用方追加县名（见 disambiguate）。
    """
    return norm_name


def disambiguate(norm_name: str, county: str) -> str:
    """跨县同名采区消歧：id 追加县简称。"""
    return f"{norm_name}__{county_short(county)}" if county else norm_name


def license_norm(raw: str) -> str:
    """许可证号归一化：统一括号、去除空白。

    PG 侧用「豫信固砂许[2025]第05号」（半角方括号），
    知识库侧用「豫罗砂许〔2023〕第 01 号」（全角六角括号）→ 归一化后才能对齐。
    """
    if not raw:
        return ""
    n = raw.strip()
    n = re.sub(r"\s+", "", n)
    n = n.replace("[", "〔").replace("]", "〕")
    n = n.replace("(", "〔").replace(")", "〕")
    n = n.replace("（", "〔").replace("）", "〕")
    # 统一「第01号」/「第1号」→「第01号」
    n = re.sub(r"第(\d+)号", lambda m: "第%02d号" % int(m.group(1)), n)
    return n


def license_year(norm_license: str) -> int:
    """从归一化许可证号中提取年份。"""
    m = re.search(r"〔(\d{4})〕", norm_license or "")
    return int(m.group(1)) if m else 0


def license_county(norm_license: str) -> str:
    """从许可证号前缀推断发证县区，如「豫信固砂许」→ 固始县。"""
    if not norm_license:
        return ""
    # 豫{地市简称}{县区简称}砂许
    m = re.match(r"豫[\u4e00-\u9fa5]{0,4}砂许", norm_license)
    if not m:
        return ""
    prefix = m.group(0)
    short_map = {
        "固": "固始县", "潢": "潢川县", "罗": "罗山县", "息": "息县",
        "商城": "商城县", "光山": "光山县", "淮滨": "淮滨县",
        "平": "平桥区", "浉": "浉河区", "新": "新县",
    }
    for k, v in short_map.items():
        if k in prefix:
            return v
    return ""
