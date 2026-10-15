"""采砂场数量统计的确定性回答策略（薄领域适配层，文本与规则全部外置到 config/kb_rules.yaml）。

适用："XX县YYYY年有多少个砂场/采区/采砂许可"。该领域的触发词、许可标题模式、
回答文案模板、口径单位、来源偏好词均声明在规则档案中，本模块只做：
- 触发判定（档案匹配）、许可编号统计（多处口径校准）、文案填充（fill_template）；
- 方案口径读数（plan_extract）直接采用上游检索层经 grounded 校验后的结果，不自行判读。

作为 DeterministicAnswerProvider 子类注册，由 extract_node 通过注册表统一调度。
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from core.kb_rules import fill_template, match_profiles

from ..intent_types import extract_county_xuke
from .registry import DeterministicAnswerProvider, register

logger = logging.getLogger(__name__)


def _docstore_path() -> str:
    """docstore.json 路径（与 llama_index 存储一致；支持 env 覆盖便于测试隔离）。"""
    override = os.environ.get("LLAMAINDEX_DOCSTORE_PATH")
    if override:
        return override
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..",
        "llama_index_storage", "docstore.json",
    )


def _doc_title(node: Any) -> str:
    """docstore 节点 → 文档标题（去 section 前缀与扩展名）。

    兼容三种形态：docstore 原始条目 {"__data__": {...}}、LlamaIndex 节点对象、
    检索结果 dict（直接带 metadata）。
    """
    if isinstance(node, dict):
        inner = node.get("__data__") if isinstance(node.get("__data__"), dict) else node
        meta = inner.get("metadata") if isinstance(inner, dict) else None
    else:
        meta = getattr(node, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    t = str(meta.get("file_name") or meta.get("title") or "")
    t = re.sub(r"^section_\d+_", "", t)
    return re.sub(r"\.(txt|md|docx?|pdf)$", "", t, flags=re.IGNORECASE)


def _permit_pattern(profile: Dict[str, Any], abbr: str, year: str):
    """许可标题模式来自规则档案（语料命名规则），代码不做硬编码。"""
    tpl = (profile or {}).get("permit_title_pattern")
    if not tpl:
        return None
    try:
        return re.compile(fill_template(tpl, abbr=re.escape(str(abbr)), year=re.escape(str(year))))
    except re.error as e:
        logger.warning(f"[sandfield] 许可标题模式构造失败: {e}")
        return None


def _templates(profile: Dict[str, Any]) -> Dict[str, Any]:
    tpl = (profile or {}).get("answer_templates")
    return tpl if isinstance(tpl, dict) else {}


@register
class SandfieldCountProvider(DeterministicAnswerProvider):
    """从 DB/KB 结果产出砂场数确定性回答；文案与规则来自规则档案。"""

    def get_answer(self, state: Dict, tool_results: List[Dict]) -> Optional[str]:
        user_msg = state.get("user_message") or ""
        # 触发判定：实体词 + 计数词均在档案 trigger 中声明（代码不含领域词表）
        profiles = match_profiles(user_msg)
        if not profiles:
            return None
        profile = profiles[0]
        extracted = extract_county_xuke(user_msg)
        if not extracted:
            return None
        county_full, abbr, year = extracted
        tpl = _templates(profile)
        if not tpl:
            logger.warning("[sandfield] 规则档案缺少回答文案模板，跳过确定性回答")
            return None

        db_cnt: Optional[int] = None
        db_ok = False
        xuke_nums = set()
        plan_extract: Optional[Dict] = None
        permit_pat = _permit_pattern(profile, abbr, year)
        for item in tool_results:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if tool_name in ("postgresql_tool", "mcp_postgres_tool"):
                if result.get("success") is True:
                    db_ok = True
                    if isinstance(data, list):
                        for row in data:
                            if isinstance(row, dict):
                                for v in row.values():
                                    if isinstance(v, int) and v > 0:
                                        db_cnt = v
                                        break
            elif tool_name == "knowledge_base_tool" and isinstance(data, list):
                # 方案口径读数由检索层通用引擎产出并已过 grounded 校验，此处直接采用
                _pe = result.get("plan_extract")
                if isinstance(_pe, dict) and _pe.get("total") is not None and plan_extract is None:
                    plan_extract = _pe
                if permit_pat is not None:
                    for chunk in data:
                        if not isinstance(chunk, dict):
                            continue
                        m = permit_pat.search(str(chunk.get("title") or ""))
                        if m:
                            xuke_nums.add(int(m.group(1)))

        # 检索 top_k 截断可能漏召回部分许可文档，用本地 docstore 全量标题校准（确定性优先）
        ds_nums = self._count_xuke_from_docstore(permit_pat)
        if ds_nums:
            xuke_nums = ds_nums

        # ── 方案口径优先：年度采区总数以《年度实施方案》为权威口径 ──
        if plan_extract:
            plan_n = int(plan_extract["total"])
            point_n = plan_extract.get("point_total")
            plan_titles: List[str] = []
            for _t in plan_extract.get("sources") or []:
                _base = str(_t).split("｜")[0].strip()
                if _base and _base not in plan_titles:
                    plan_titles.append(_base)
            plan_src = "、".join(plan_titles[:1]) or fill_template(
                tpl.get("default_source"), county=county_full, year=year
            )
            point_clause = fill_template(tpl.get("point_clause"), point=point_n) if point_n else ""
            if db_cnt is not None and db_cnt == plan_n:
                db_clause = tpl.get("db_match_clause") or ""
            elif db_cnt is not None:
                db_clause = fill_template(tpl.get("db_clause"), db_n=db_cnt)
            else:
                db_clause = ""
            plan_text = fill_template(
                tpl.get("with_total"), source=plan_src, county=county_full, year=year,
                n=plan_n, point_clause=point_clause, db_clause=db_clause,
            )
            plan_text += self._append_sources(plan_titles, tpl)
            return plan_text

        if not xuke_nums and db_cnt is None:
            # DB 查询成功但无数据、知识库也未检索到该年许可文档时确定性告知，
            # 避免 LLM 从邻近年份文档编造数量；DB 调用失败时不下结论，交回 LLM。
            if db_ok and profile.get("no_record_template"):
                return fill_template(profile.get("no_record_template"), county=county_full, year=year)
            return None

        parts: List[str] = []
        if xuke_nums:
            nums = sorted(xuke_nums)
            prefix = fill_template(profile.get("permit_prefix"), abbr=abbr, year=year)
            if nums[-1] - nums[0] + 1 == len(nums):
                parts.append(fill_template(
                    tpl.get("xuke_range"), county=county_full, year=year, n=len(nums),
                    prefix=prefix, a=nums[0], b=nums[-1],
                ))
            else:
                parts.append(fill_template(
                    tpl.get("xuke_list"), county=county_full, year=year, n=len(nums),
                    nums="、".join(str(n) for n in nums),
                ))
        if db_cnt is not None:
            if xuke_nums and db_cnt == len(xuke_nums):
                parts.append(fill_template(tpl.get("db_same"), n=db_cnt))
            else:
                parts.append(fill_template(tpl.get("db_only"), n=db_cnt))
        if not parts:
            return None
        answer = "，".join(parts) + "。"

        sources = self._collect_sandfield_sources(permit_pat, profile, tool_results)
        answer += self._append_sources(sources, tpl)
        return answer

    @staticmethod
    def _append_sources(titles: List[str], tpl: Dict[str, Any]) -> str:
        if not titles or not tpl.get("sources_prefix"):
            return ""
        shown = "、".join(f"《{t}》" for t in titles[:3])
        if len(titles) > 3:
            shown += fill_template(tpl.get("sources_more"), n=len(titles))
        return fill_template(tpl.get("sources_prefix")) + shown

    def _collect_sandfield_sources(self, permit_pat, profile: Dict[str, Any],
                                  tool_results: List[Dict]) -> List[str]:
        """溯源文档名（优先本地 docstore 全量标题；不可用时回退检索结果标题）。"""
        prefer = [str(t) for t in (profile.get("source_prefer_terms") or [])]
        titles: List[str] = []
        try:
            with open(_docstore_path(), "r", encoding="utf-8") as f:
                doc = json.load(f)
            data = doc.get("docstore/data") or {}
            if isinstance(data, dict):
                for node in data.values():
                    t = _doc_title(node)
                    if not t:
                        continue
                    if permit_pat is not None and permit_pat.search(t) and t not in titles:
                        titles.append(t)
                    elif prefer and any(k in t for k in prefer) and t not in titles:
                        titles.append(t)
        except Exception:
            pass
        if titles:
            return sorted(titles, key=self._sort_key(prefer))
        for item in tool_results:
            if not isinstance(item, dict) or item.get("tool_name") != "knowledge_base_tool":
                continue
            result = item.get("result", {})
            if not isinstance(result, dict) or not result.get("success"):
                continue
            for chunk in result.get("data") or []:
                if not isinstance(chunk, dict):
                    continue
                t = _doc_title(chunk)
                if t and t not in titles:
                    titles.append(t)
        return titles

    @staticmethod
    def _sort_key(prefer: List[str]):
        def key(t: str):
            if prefer and any(k in t for k in prefer):
                return (0, 0)
            m = re.search(r"第[_\s]*(\d+)[_\s]*号", t)
            return (1, int(m.group(1)) if m else 999)
        return key

    @staticmethod
    def _count_xuke_from_docstore(permit_pat) -> Optional[set]:
        """从本地 docstore 全量标题统计许可编号（确定性，不受检索召回影响）。"""
        if permit_pat is None:
            return None
        try:
            with open(_docstore_path(), "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            return None
        data = doc.get("docstore/data") or {}
        if not isinstance(data, dict):
            return None
        nums = set()
        for node in data.values():
            m = permit_pat.search(_doc_title(node))
            if m:
                nums.add(int(m.group(1)))
        return nums or None
