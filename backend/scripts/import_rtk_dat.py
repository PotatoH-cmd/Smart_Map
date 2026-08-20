#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RTK 测点 dat 文件入库工作流：
  1. 解析 dat（格式：点号,编码,东坐标,北坐标,高程），默认 EPSG:4547
  2. 生成点 shp（与 dat 同目录，坐标系与源一致）
  3. 从 LlamaIndex 知识库"实施方案"文件夹检索该采区的控制开采高程
  4. 转 WGS84 后入库 PG 表 rtk_points（同砂场重复导入先清旧）

用法：
  python import_rtk_dat.py --dat /path/xx.dat --site 郝楼砂场 --permit "豫潢砂许[2025]第1号" [--epsg 4547] [--kb-query 检索词]
"""

import argparse
import os
import re
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

# 河南省常见河流名（用于关键词过滤）
COMMON_RIVERS = {"灌河", "史灌河", "潢河", "竹竿河", "淮河", "白露河",
                  "湄河", "浉河", "小潢河", "寨河", "春河"}

# 加载 backend/.env（DASHSCOPE_API_KEY / KNOWLEDGE_BACKEND 等）
def _load_env():
    env_path = os.path.join(BACKEND_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

DB_CFG = {
    "host": "172.136.16.52",
    "port": 5432,
    "dbname": "postgres",
    "user": "postgres",
    "password": "8720622",
}

DDL_RTK = """
CREATE TABLE IF NOT EXISTS rtk_points (
    id SERIAL PRIMARY KEY,
    point_id TEXT,
    code TEXT,
    geom geometry(POINT, 4326),
    x_4547 double precision,
    y_4547 double precision,
    elev numeric,
    site_name TEXT,
    permit_no TEXT,
    ctrl_elev_max numeric,
    ctrl_elev_min numeric,
    ctrl_elev_source TEXT,
    source_file TEXT,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rtk_points_gix ON rtk_points USING GIST(geom);
CREATE INDEX IF NOT EXISTS rtk_points_site_idx ON rtk_points(site_name);
"""


def parse_dat(dat_path):
    """解析 dat：点号,编码,东坐标,北坐标,高程。返回 [(pid, code, e, n, z), ...]"""
    rows = []
    bad = 0
    with open(dat_path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip().lstrip("\ufeff")
            if not ln:
                continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 5:
                bad += 1
                continue
            try:
                e, n, z = float(parts[2]), float(parts[3]), float(parts[4])
            except ValueError:
                bad += 1
                continue
            rows.append((parts[0], parts[1], e, n, z))
    return rows, bad


def write_shp(rows, epsg, out_path):
    """生成点 shp（保持源坐标系）"""
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        {
            "point_id": [r[0] for r in rows],
            "code": [r[1] for r in rows],
            "elev": [r[4] for r in rows],
        },
        geometry=[Point(r[2], r[3]) for r in rows],
        crs=f"EPSG:{epsg}",
    )
    gdf.to_file(out_path, driver="ESRI Shapefile", encoding="utf-8")
    return len(gdf)


def search_ctrl_elevation(query):
    """从知识库检索控制开采高程区间。
    
    四层策略（按优先级）：
      0. SHP 批复数据：信阳市2025-2026年批复许可采区.shp（官方许可证，最权威）
      1. LLM 缓存：批量预提取兜底
      2. 正则：纯文本 7 种格式匹配
      3. HTML 表格：智能别名匹配（处理编码名如 LS12/GH2）
    """
    # ── 提取 site_hint（全名）和 site_keywords（智能别名）──
    site_hint, site_keywords = _extract_site_info(query)

    # ── 步骤0：SHP 批复数据（最权威，不需联网，秒出）──
    result = _shp_lookup(site_hint, site_keywords)
    if result[0] is not None:
        return result

    try:
        from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    except ImportError as e:
        return None, None, f"知识库工具导入失败: {e}"

    kb = KnowledgeBaseTool()
    res = kb.call({
        "operation": "search",
        "query": query,
        "top_k": 30,
        "project": "shishifangan",
    })
    if not res.get("success") or not res.get("data"):
        return None, None, f"知识库未检索到（query={query}）"

    items = res["data"]

    # 排序：全名匹配的切片在最前面
    if site_hint:
        items = sorted(items, key=lambda x: (
            0 if site_hint in x.get("content", "") else 1,
            x.get("relevance", 0)
        ), reverse=False)

    # ── 步骤1：LLM 缓存（最可靠，LLM 解析表格不受干扰）──
    result = _cache_lookup(query, site_keywords)
    if result[0] is not None:
        return result

    # ── 步骤2：纯文本正则（严格全名验证）──
    result = _regex_extract(items, site_hint, site_keywords)
    if result[0] is not None:
        return result

    # ── 步骤3：HTML 表格匹配（智能别名）──
    result = _table_extract(items, site_keywords)
    if result[0] is not None:
        return result

    titles = "; ".join(i.get("title", "") for i in items[:3])
    return None, None, f"命中文档但未匹配到控制高程数值（前3条：{titles}）"


def _extract_site_info(query):
    """从查询中提取全名和智能别名列表"""
    site_hint = ""
    # 全名：按可采区/砂场等分隔符取前缀
    for term_sep in ["可采区", "采区", "砂场", "村", "河段", "工程", "可采点"]:
        idx = query.find(term_sep)
        if idx > 0:
            start = max(0, idx - 8)
            site_hint = query[start:idx + len(term_sep)]
            break
    if not site_hint:
        site_hint = query

    # 智能别名：去河流前缀，生成所有2-3字片段
    core = site_hint
    for sep in ["可采区", "采区", "砂场"]:
        idx = core.find(sep)
        if idx > 0:
            core = core[:idx]
            break

    keywords = [site_hint, core]
    # 去代码前缀再试河流前缀（如 "GH2 灌河道超" → "灌河道超" → "道超"）
    core_no_code = re.sub(r'^[A-Z]+[0-9]+\s*', '', core)
    if core_no_code != core and len(core_no_code) >= 2:
        keywords.append(core_no_code)
    # 去河流前缀
    check_core = core_no_code if core_no_code != core else core
    for river in sorted(COMMON_RIVERS, key=len, reverse=True):
        if check_core.startswith(river):
            specific = check_core[len(river):]
            if len(specific) >= 2:
                keywords.append(specific)
            break
    # 2-3字滑动窗口
    for wsize in [2, 3]:
        for i in range(len(core) - wsize + 1):
            sub = core[i:i + wsize]
            if sub not in COMMON_RIVERS and sub not in keywords:
                keywords.append(sub)
    # 去重，长优先
    seen = set()
    unique = []
    for kw in sorted(keywords, key=len, reverse=True):
        if kw not in seen and len(kw) >= 2:
            seen.add(kw)
            unique.append(kw)
    return site_hint, unique


def _regex_extract(items, site_hint, site_keywords):
    """正则匹配：多级全名尝试 + 关键词降级 + 邻近性检查 + 7 种文本格式"""
    pat_range = r"([0-9]{1,3}\.[0-9]{1,2})\s*[～~—\-]\s*([0-9]{1,3}\.[0-9]{1,2})"
    patterns = [
        r"划定本段控制开采高程为\s*" + pat_range + r"\s*[m米]",
        r"控制开采高程为\s*" + pat_range + r"\s*[m米]",
        r"开采后控制高程为\s*" + pat_range + r"\s*[m米]",
        r"控制高程为\s*" + pat_range + r"\s*[m米]",
        r"控制开采高程\s*" + pat_range + r"\s*[m米]",
        r"控制高程\s*" + pat_range + r"\s*[m米]",
        r"开采控制底高程[为是]?\s*" + pat_range + r"\s*[m米]",
    ]

    # 尝试多个 site_hint 变体（从全名到短名）
    hints = [site_hint] if site_hint else []
    if site_hint:
        # 去代码前缀（如 "GH2 灌河道超" → "灌河道超"，让后续河流前缀检测生效）
        core_no_code = re.sub(r'^[A-Z]+[0-9]+\s*', '', site_hint)
        if core_no_code != site_hint and len(core_no_code) >= 2:
            hints.append(core_no_code)
        # 去河流前缀版本
        for river in sorted(COMMON_RIVERS, key=len, reverse=True):
            if site_hint.startswith(river):
                hints.append(site_hint[len(river):])  # "灌河叶岗可采区" → "叶岗可采区"
                break
        # 去"可采区"后缀
        if "可采区" in site_hint:
            hints.append(site_hint.replace("可采区", ""))  # → "灌河叶岗"

    for item in items:
        text = item.get("content", "")
        title = item.get("title", "")
        # 找第一个匹配的 hint 在切片中的位置
        best_hint_pos = -1
        for hint in hints:
            pos = text.find(hint)
            if pos >= 0:
                best_hint_pos = pos
                break
        # 全名变体都没匹配 → 用关键词做降级尝试（如 "道超" 匹配 "GH2(道超集可采区)"）
        if best_hint_pos < 0:
            for kw in (site_keywords or []):
                pos = text.find(kw)
                if pos >= 0 and len(kw) >= 2:
                    best_hint_pos = pos
                    break
        if best_hint_pos < 0:
            continue  # 没有任何 hint 变体匹配

        for pat in patterns:
            m = re.search(pat, text)
            if m:
                # 邻近性：匹配位置必须在 hint 附近(±1200字)
                if abs(m.start() - best_hint_pos) > 1200:
                    continue
                v1, v2 = float(m.group(1)), float(m.group(2))
                if 5 < v1 < 200 and 5 < v2 < 200:
                    return max(v1, v2), min(v1, v2), title
    return None, None, None


def _table_extract(items, site_keywords):
    """HTML 表格匹配（用智能别名做模糊匹配）"""
    for item in items:
        text = item.get("content", "")
        title = item.get("title", "")
        if "<table>" not in text:
            continue
        result = _extract_html_table_elevation(text, title, site_keywords)
        if result is not None:
            return result
    return None, None, None


def _shp_lookup(site_hint, site_keywords):
    """从信阳市2025-2026年批复许可采区.shp中匹配控制高程（官方许可证数据，最权威）
    
    注意：会先过滤明显是工程类项目（水毁修复/防洪/疏浚/生态等）的查询词，
    避免把工程名误匹配到砂场。
    """
    import json as _json
    import os as _os

    # ── 工程类项目黑名单：这些不是采砂许可，不应该匹配SHP ──
    ENGINEERING_KEYWORDS = [
        "水毁修复", "防洪", "疏浚", "生态修复", "航道", "港区",
        "除险", "加固", "治理工程", "整治工程", "提升工程",
    ]
    query_text = " ".join([site_hint] + (site_keywords or []))
    for ek in ENGINEERING_KEYWORDS:
        if ek in query_text:
            return None, None, None  # 工程类项目，不匹配砂场许可数据

    shp_path = _os.path.join(_os.path.dirname(__file__), "_shp_elevation.json")
    if not _os.path.isfile(shp_path):
        return None, None, None
    try:
        with open(shp_path, "r", encoding="utf-8") as f:
            shp_data = _json.load(f)
    except Exception:
        return None, None, None

    # 对每条SHP记录做双向模糊匹配
    best_score = 0
    best_entry = None
    for entry in shp_data:
        shp_name = entry.get("site", "")
        location = entry.get("location", "")
        score = 0
        # 关键词在SHP名称中
        for kw in (site_keywords or []):
            if kw in shp_name:
                score += len(kw)
            if kw in location:
                score += len(kw)
        # SHP名称在关键词中（反向匹配）
        for kw in (site_keywords or []):
            if shp_name in kw:
                score += len(shp_name)
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry and best_score >= 3:  # 至少3字匹配才采纳（比LLM缓存的2字更严格）
        mx = best_entry.get("max")
        mn = best_entry.get("min")
        if mx is not None and mn is not None:
            river = best_entry.get("river", "")
            return float(mx), float(mn), f"SHP批复数据({best_entry.get('site', '')}{' '+river if river else ''})"
    return None, None, None


def _cache_lookup(query, site_keywords):
    """从 LLM 预提取缓存中查找（兜底方案）"""
    import json as _json
    import os as _os
    cache_path = _os.path.join(_os.path.dirname(__file__), "_elevation_cache.json")
    if not _os.path.isfile(cache_path):
        return None, None, None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = _json.load(f)
    except Exception:
        return None, None, None

    # 对每条缓存记录，用 site_keywords 做模糊匹配
    best_score = 0
    best_entry = None
    for entry in cache:
        site_name = entry.get("site", "")
        score = 0
        for kw in (site_keywords or []):
            if kw in site_name:
                score += len(kw)  # 匹配越长权重越高
        # 也检查反向匹配（缓存名包含查询词）
        for kw in (site_keywords or []):
            if site_name in kw:
                score += len(site_name)
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_entry and best_score >= 2:  # 至少2字匹配（如"叶岗"="固始县叶岗采区GH4"）
        mx = best_entry.get("max")
        mn = best_entry.get("min")
        if mx is not None and mn is not None:
            return float(mx), float(mn), f"缓存({best_entry.get('site', '')})"
    return None, None, None



def _extract_html_table_elevation(text, title, site_keywords=None):
    """从 HTML 表格中提取控制采砂高程上限/下限。
    用 site_keywords 列表（而非单个 site_hint）做模糊匹配，支持别名变体。"""
    import re as _re
    if site_keywords is None:
        site_keywords = []

    # ── 策略1: 大表格 + 采区名定位 ──
    if site_keywords and "<table>" in text and "<tr>" in text:
        rows = _re.split(r"<tr[^>]*>", text, flags=_re.IGNORECASE)
        for ri, row in enumerate(rows):
            # 任一关键词匹配即视为采区行
            matched_kw = None
            for kw in site_keywords:
                if kw in row:
                    matched_kw = kw
                    break
            if not matched_kw:
                continue
            # 在采区行附近(前后3行)找同时包含"控制"+"高程"标志的行
            has_ctrl_marker = False
            ctx_rows = rows[max(0, ri-2):ri+4]
            for cr in ctx_rows:
                # 匹配 "控制开采高程" / "控制采砂高程" / "控制高程"
                if _re.search(r'控制.{0,6}(开采|采砂|高程)', cr):
                    has_ctrl_marker = True
                    break
            if not has_ctrl_marker:
                continue

            # 收集附近行的所有数字对
            for cr in ctx_rows:
                m = _re.findall(
                    r"<td[^>]*>(\d+\.?\d*)</td>\s*<td[^>]*>(\d+\.?\d*)</td>",
                    cr
                )
                for v1, v2 in m:
                    f1, f2 = float(v1), float(v2)
                    if 5 < f1 < 200 and 5 < f2 < 200:
                        return max(f1, f2), min(f1, f2), f"{title}（{matched_kw}行）"
                # colspan 格式：<td colspan=2>30.00-30.16m</td>（允许单位后缀 m/米）
                m2 = _re.findall(
                    r"<td[^>]*colspan[^>]*>(\d+\.?\d*)\s*[—\-～~]\s*(\d+\.?\d*)[m米]?</td>",
                    cr
                )
                for v1, v2 in m2:
                    f1, f2 = float(v1), float(v2)
                    if 5 < f1 < 200 and 5 < f2 < 200:
                        return max(f1, f2), min(f1, f2), f"{title}（{matched_kw}行）"
            break

    # ── 策略2: 单行表格（一行就包含 控制(开采|采砂)高程 + 两个数字）──
    m = _re.findall(
        r"控制(?:开采|采砂)高程.*?<td[^>]*>(\d+\.?\d*)</td>\s*<td[^>]*>(\d+\.?\d*)</td>",
        text, _re.IGNORECASE | _re.DOTALL
    )
    for v1, v2 in m:
        f1, f2 = float(v1), float(v2)
        if 5 < f1 < 200 and 5 < f2 < 200:
            return max(f1, f2), min(f1, f2), title

    # 策略3: colspan 合并格式（允许单位后缀 m/米）
    m = _re.findall(
        r"控制(?:开采|采砂)高程.*?<td[^>]*colspan[^>]*>(\d+\.?\d*)\s*[—\-～~]\s*(\d+\.?\d*)[m米]?</td>",
        text, _re.IGNORECASE | _re.DOTALL
    )
    for v1, v2 in m:
        f1, f2 = float(v1), float(v2)
        if 5 < f1 < 200 and 5 < f2 < 200:
            return max(f1, f2), min(f1, f2), title

    return None


def import_pg(rows, epsg, site, permit, ctrl_max, ctrl_min, ctrl_src, source_file):
    import psycopg2
    from psycopg2.extras import execute_values
    from pyproj import Transformer

    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)

    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(DDL_RTK)
    # 同砂场重复导入先清旧
    cur.execute("DELETE FROM rtk_points WHERE site_name = %s", (site,))
    deleted = cur.rowcount

    values = []
    for pid, code, e, n, z in rows:
        lon, lat = transformer.transform(e, n)
        values.append((pid, code, lon, lat, e, n, z, site, permit,
                       ctrl_max, ctrl_min, ctrl_src, source_file))
    # 坐标合理性校验：信阳地区经度 113~116.5°E（东至固始 116°E，西至平桥 113.5°E）。
    # 误用相邻带 EPSG 转换（如 dat 为 4547 体系却按 4548 转）会导致经度整体偏移 ±3°，
    # 偏移后落在 116.5~120°E 区间，无法落在影像/边界内。发现异常立即终止并提示。
    if values:
        lons = [v[2] for v in values]
        lon_min, lon_max = min(lons), max(lons)
        if lon_min < 112.5 or lon_max > 117.5:
            print(f"[错误] 转换后经度范围 {lon_min:.4f}~{lon_max:.4f} 超出信阳地区合理范围"
                  f"（112.5~117.5°E），疑似 --epsg 选错（如 dat 为 4547 体系却用了 4548，"
                  f"会整体偏移 +3°）。请核对 dat 源坐标系后重试，本次导入已取消。")
            conn.close()
            return deleted, None

    execute_values(
        cur,
        """INSERT INTO rtk_points
           (point_id, code, geom, x_4547, y_4547, elev, site_name, permit_no,
            ctrl_elev_max, ctrl_elev_min, ctrl_elev_source, source_file)
           VALUES %s""",
        values,
        template="(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
    )
    conn.commit()

    cur.execute(
        """SELECT count(*), min(elev), round(avg(elev), 3), max(elev),
                  min(ST_X(geom)), max(ST_X(geom)), min(ST_Y(geom)), max(ST_Y(geom))
           FROM rtk_points WHERE site_name = %s""",
        (site,),
    )
    stats = cur.fetchone()
    conn.close()
    return deleted, stats


def main():
    ap = argparse.ArgumentParser(description="RTK dat 转 shp 并入库 PG")
    ap.add_argument("--dat", required=True, help="dat 文件路径")
    ap.add_argument("--site", required=True, help="砂场名称")
    ap.add_argument("--permit", required=True, help="许可证号")
    ap.add_argument("--epsg", type=int, default=4547, help="源坐标系 EPSG（默认4547）")
    ap.add_argument("--kb-query", default="", help="控制高程知识库检索词（默认：<砂场名> 采区 控制开采高程）")
    args = ap.parse_args()

    dat_path = os.path.abspath(args.dat)
    if not os.path.isfile(dat_path):
        print(f"[错误] dat 文件不存在: {dat_path}")
        sys.exit(1)

    print(f"=== RTK 入库工作流: {args.site}（{args.permit}）===")
    rows, bad = parse_dat(dat_path)
    print(f"[1/4] 解析 dat: {len(rows)} 个有效点，跳过坏行 {bad}")
    if not rows:
        print("[错误] 无有效测点")
        sys.exit(1)

    shp_path = os.path.join(os.path.dirname(dat_path),
                            os.path.splitext(os.path.basename(dat_path))[0] + "_点.shp")
    n = write_shp(rows, args.epsg, shp_path)
    print(f"[2/4] 生成点 shp: {shp_path}（{n} 点, EPSG:{args.epsg}）")

    query = args.kb_query or f"{args.site.replace('砂场', '')} 采区 控制开采高程"
    ctrl_max, ctrl_min, ctrl_src = search_ctrl_elevation(query)
    if ctrl_max is not None:
        print(f"[3/4] 控制开采高程: {ctrl_max}m ~ {ctrl_min}m（来源：{ctrl_src}）")
    else:
        print(f"[3/4] 未提取到控制高程: {ctrl_src}（入库时该字段为空，不编造）")

    deleted, stats = import_pg(rows, args.epsg, args.site, args.permit,
                               ctrl_max, ctrl_min, ctrl_src or "", dat_path)
    cnt, zmin, zavg, zmax, lon0, lon1, lat0, lat1 = stats
    print(f"[4/4] 入库 rtk_points: 新增 {cnt} 点（清理旧记录 {deleted} 条）")
    print(f"      高程 min/avg/max = {zmin}/{zavg}/{zmax} m")
    print(f"      经度范围 {lon0:.6f}~{lon1:.6f}, 纬度范围 {lat0:.6f}~{lat1:.6f}")
    if ctrl_min is not None and zavg is not None:
        diff = float(ctrl_min) - float(zavg)
        if diff > 2:
            verdict = "平均高程低于控制高程下限超2米 → 存在超深度开采"
        elif diff > 0:
            verdict = "平均高程低于控制高程下限0-2米 → 采区局部提砂区修复不到位，后续应加强管理"
        else:
            verdict = "平均高程不低于控制高程下限 → 本年度开采深度基本符合年度实施方案"
        print(f"      平均高程与控制高程下限差值: {diff:+.3f} m（控制下限-平均）→ {verdict}")
    print("完成。")


if __name__ == "__main__":
    main()
