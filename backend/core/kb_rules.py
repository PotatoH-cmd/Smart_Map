"""通用知识库规则引擎：语料级规则的唯一解释者（L3）。

职责（通用机制，代码内不含任何领域术语）：
- 读取 config/kb_rules.yaml（env KB_RULES_PATH 可覆盖）；文件缺失或解析失败即关闭增强
  （回退基础检索行为，不抛错、不影响主流程）；
- 聚合语义判定与互补 query 派生（retrieval.aggregation_terms / _templates）；
- 检索增强参数（父片回溯 parent_expand、同文档配额 doc_quota）；
- 计数口径抽取：按 profile 声明的 count_units 生成通用「数字+单位」模式，
  按 current_year_markers / plan_markers 判定口径，众数汇总出总数。

调用方（检索工具/领域 Provider）只消费本模块的函数，不自行拼装术语与正则。
"""
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
_YEAR_RE = re.compile(r"20\d{2}")
_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "data": {}}


def rules_path() -> Path:
    return Path(os.environ.get("KB_RULES_PATH") or (BACKEND_DIR / "config" / "kb_rules.yaml"))


def load_rules(force: bool = False) -> Dict[str, Any]:
    """加载规则文件（按 mtime 缓存）。任何异常返回 {}（增强功能关闭）。"""
    path = rules_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if not force and _cache["mtime"] == mtime:
            return _cache["data"]
        data: Dict[str, Any] = {}
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
            else:
                logger.warning(f"[kb_rules] 规则文件格式异常（非 dict），忽略: {path}")
        except Exception as e:
            logger.warning(f"[kb_rules] 规则文件加载失败（增强功能关闭）: {e}")
        _cache["mtime"], _cache["data"] = mtime, data
        return data


# ---------------------------------------------------------------------------
# 检索增强参数
# ---------------------------------------------------------------------------

def fill_template(template: Any, **kw) -> str:
    """通用模板填充：把 {key} 占位替换为传入值（缺失键原样保留，便于排查）。"""
    out = str(template or "")
    for k, v in kw.items():
        out = out.replace("{" + k + "}", "" if v is None else str(v))
    return out


def retrieval_rules() -> Dict[str, Any]:
    r = load_rules().get("retrieval")
    return r if isinstance(r, dict) else {}


def aggregation_terms() -> List[str]:
    return [str(t) for t in (retrieval_rules().get("aggregation_terms") or []) if str(t).strip()]


def is_aggregation_query(text: str) -> bool:
    """是否计数/汇总类问题（按规则声明的聚合语义词判定）。"""
    terms = aggregation_terms()
    return bool(terms) and any(t in (text or "") for t in terms)


def aggregation_queries(query: str) -> List[str]:
    """聚合类问题 → 互补 query 列表（不含原 query）。非聚合问题返回 []。

    模板来自规则文件，{q} 替换为用户问题原文；用于把"总数/口径/汇总"类表述召回进来。
    """
    if not query or not is_aggregation_query(query):
        return []
    out: List[str] = []
    for tpl in (retrieval_rules().get("aggregation_query_templates") or []):
        t = str(tpl).replace("{q}", query).strip()
        if t and t != query and t not in out:
            out.append(t)
    return out


def parent_expand_settings() -> Tuple[bool, int]:
    r = retrieval_rules()
    enabled = bool(r.get("parent_expand", False))
    try:
        top = int(r.get("parent_expand_top", 3))
    except (TypeError, ValueError):
        top = 3
    return enabled, max(0, top)


def doc_quota() -> int:
    try:
        return max(0, int(retrieval_rules().get("doc_quota", 0)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 语料档案（profiles）
# ---------------------------------------------------------------------------

def match_profiles(text: str, year: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 trigger（实体词 + 计数词）匹配启用的语料档案；年度门槛由 profile_min_year 单独判定。"""
    out: List[Dict[str, Any]] = []
    for p in (load_rules().get("profiles") or []):
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        trig = p.get("trigger") or {}
        ents = [str(t) for t in (trig.get("entity_terms") or [])]
        cnts = [str(t) for t in (trig.get("count_terms") or [])]
        msg = text or ""
        if ents and not any(t in msg for t in ents):
            continue
        if cnts and not any(t in msg for t in cnts):
            continue
        out.append(p)
    return out


def profile_min_year(profile: Dict[str, Any]) -> Optional[int]:
    """档案级最低适用年度（仅定向检索路径使用；供调用方判定是否适用）。"""
    try:
        v = profile.get("min_year")
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_unit_pattern(units: Any) -> Optional[re.Pattern]:
    """按声明的单位列表生成通用「数字+单位」模式。

    负向断言排除代码/编号误命中（如 "SGH4 采区"、"图 5.6-55 采区"、"第 3.2.1 采区"）：
    数字前一位不得为字母/数字/点/连字符。
    """
    clean = [str(u) for u in (units or []) if str(u).strip()]
    if not clean:
        return None
    alt = "|".join(re.escape(u) for u in clean)
    try:
        return re.compile(rf"(?<![A-Za-z0-9.\-])(\d{{1,4}})\s*个?\s*({alt})")
    except re.error as e:
        logger.warning(f"[kb_rules] 单位模式构造失败: {e}")
        return None


def sentence_scope_ok(sentence: str, year: Optional[str], profile: Dict[str, Any]) -> bool:
    """口径判定：句中含目标年度 → 属本年度（即使同句引用规划报告）；含其他年度 → 否；
    无年度时需含 current_year_markers。"""
    years = set(_YEAR_RE.findall(sentence or ""))
    if year is not None and str(year) in years:
        return True
    if years:
        return False
    return any(m in (sentence or "") for m in (profile.get("current_year_markers") or []))


def extract_counts(chunks: List[Dict[str, Any]], profile: Dict[str, Any], year: Optional[str]) -> Dict[str, List[Dict]]:
    """从检索片段按 profile 口径抽取计数条目。

    返回 {"primary": [...], "secondary": [...]}；每条含 value/unit/sentence/title/chunk_id。
    仅接受口径合法的句子（sentence_scope_ok），并按主/次单位分别抽取。
    """
    primary_pat = build_unit_pattern(profile.get("count_units"))
    secondary_pat = build_unit_pattern(profile.get("secondary_units"))
    out: Dict[str, List[Dict]] = {"primary": [], "secondary": []}
    for ch in chunks or []:
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or "")
        text = str(ch.get("content") or "")
        if not text:
            continue
        for raw in re.split(r"[。；;\n]", text):
            sent = raw.strip()
            if not sent:
                continue
            # 主/次口径各自独立抽取：同一句可同时给出主口径与次口径
            # （如"14 个可采区16 个可采点"），故命中后不中断另一口径的抽取。
            for key, pat in (("primary", primary_pat), ("secondary", secondary_pat)):
                if pat is None:
                    continue
                m = pat.search(sent)
                if not m:
                    continue
                if not sentence_scope_ok(sent, year, profile):
                    continue
                n = int(m.group(1))
                if not 0 < n < 100000:
                    continue
                out[key].append({
                    "value": n,
                    "unit": m.group(2),
                    "sentence": sent[:200],
                    "title": title,
                    "chunk_id": ch.get("id") or ch.get("document_id") or "",
                })
    return out


def pick_mode(items: List[Dict[str, Any]]) -> Optional[int]:
    """同类计数条目取众数（确定性汇总；口径冲突时不猜）。"""
    if not items:
        return None
    from collections import Counter

    counter = Counter(int(it["value"]) for it in items if isinstance(it.get("value"), int))
    if not counter:
        return None
    return int(counter.most_common(1)[0][0])


def quota_by_doc(items: List[Any], per_doc: int, key_fn) -> List[Any]:
    """同一文档最多保留 per_doc 条（去冗余），保持原序。per_doc<=0 不过滤。"""
    if per_doc <= 0 or not items:
        return list(items)
    seen: Dict[str, int] = {}
    out = []
    for it in items:
        k = str(key_fn(it) or "")
        if seen.get(k, 0) >= per_doc:
            continue
        seen[k] = seen.get(k, 0) + 1
        out.append(it)
    return out
