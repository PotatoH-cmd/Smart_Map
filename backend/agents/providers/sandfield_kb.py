"""采砂场统计的知识库回退 Provider（薄领域适配层，L1+L2+L3 通用化后重写）。

本模块**不含检索策略**：
- 通用机制在 core/kb_rules.py（聚合 query 派生、计数抽取、众数汇总、口径判定）、
  core/grounded.py（有据校验）、tools/llamaindex_knowledge_tool.py（父片回溯、同文档配额）；
- 语料级术语/模板/口径词/单位/提示词全部声明在 config/kb_rules.yaml（profiles）。

本模块只做三件事：
1. 依据命中的规则档案做定向检索（query 模板来自档案）；
2. 调通用引擎抽取主/次口径计数，并用有据校验确认「数字/证据句见于检索原文」；
3. 有据则挂 result.plan_extract 供 extract 侧产出确定性回答；无据则回退 LLM 语义读数。
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage

from core import grounded
from core.kb_rules import (
    extract_counts,
    fill_template,
    match_profiles,
    pick_mode,
    profile_min_year,
)

from ..intent_types import extract_county_xuke
from .kb_fallback import KbFallbackProvider, register_kb_fallback

logger = logging.getLogger(__name__)


@dataclass
class _PlanStep:
    tool: str = "knowledge_base_tool"
    params: Optional[dict] = None
    step_id: int = 9998


@register_kb_fallback
class SandfieldKbProvider(KbFallbackProvider):
    async def targeted_search(
        self,
        user_msg: str,
        pre_results: List[Dict],
        *,
        invoke_step: Callable,
        llm_factory: Callable,
    ) -> Optional[List[Dict]]:
        """档案驱动的定向检索 + 通用引擎计数抽取。"""
        extracted = extract_county_xuke(user_msg or "")
        if not extracted:
            return None
        county, _abbr, year = extracted
        profiles = match_profiles(user_msg or "", year)
        if not profiles:
            return None
        profile = profiles[0]
        # 许可编号口径的历史年度问题不走本档案的定向检索（由许可统计路径回答）
        min_year = profile_min_year(profile)
        if min_year is not None and int(year) < min_year:
            return None
        try:
            data = self._dedupe(await self._search_by_profile(
                profile, county, str(year), invoke_step
            ))[:10]
            plan_result: Dict[str, Any] = {
                "success": True,
                "data": data,
                "count": len(data),
                "method": "rules_targeted_search",
                "profile": profile.get("name"),
            }
            logger.info(
                f"[tool_node] 档案定向检索（profile={profile.get('name')}, "
                f"project={profile.get('project')}）: count={len(data)}"
            )
            # 1) 通用引擎读数 + 有据校验（首选，确定性）
            plan_extract = self._extract(profile, data, county, str(year))
            # 2) 兜底：无标准表述时由 LLM 语义读数（提示词与口径标签同样来自档案）
            if plan_extract is None and data:
                plan_extract = await self._extract_llm(
                    profile, data, county, str(year), llm_factory
                )
            if plan_extract:
                plan_result["plan_extract"] = plan_extract
                logger.info(f"[tool_node] 方案口径读数: {plan_extract}")
            return [{"tool_name": "knowledge_base_tool", "result": plan_result}]
        except Exception as e:
            logger.warning(f"[tool_node] 档案定向检索失败: {e}")
        return None

    def query_for(self, user_msg: str) -> Optional[str]:
        """空库回退检索词：按档案模板生成（{abbr}/{year} 替换）。"""
        extracted = extract_county_xuke(user_msg or "")
        if not extracted:
            return None
        _county, abbr, year = extracted
        profiles = match_profiles(user_msg or "", year)
        if not profiles:
            return None
        tpl = profiles[0].get("fallback_query_template")
        return fill_template(tpl, abbr=abbr, year=year) or None

    # ------------------------------------------------------------------
    # 内部：检索 / 抽取
    # ------------------------------------------------------------------

    async def _search_by_profile(self, profile: Dict[str, Any], county: str, year: str,
                                 invoke_step: Callable) -> List[Dict]:
        """按档案 query 模板逐条定向检索并合并（模板缺省时用通用问题式表达）。"""
        project = profile.get("project")
        merged: List[Dict] = []
        for tpl in (profile.get("query_templates") or []):
            query = fill_template(tpl, county=county, year=year)
            if not query.strip():
                continue
            params: Dict[str, Any] = {"operation": "search", "query": query, "top_k": 10}
            if project:
                params["project"] = project
            try:
                res = await invoke_step(_PlanStep(tool="knowledge_base_tool", params=params))
                result = res.get("result") if isinstance(res.get("result"), dict) else {}
                for ch in (result.get("data") or []):
                    if isinstance(ch, dict):
                        merged.append(ch)
            except Exception as e:
                logger.warning(f"[tool_node] 定向检索单路失败（继续下一路）: {e}")
        return merged

    @staticmethod
    def _dedupe(chunks: List[Dict]) -> List[Dict]:
        out, seen = [], set()
        for ch in chunks:
            key = (
                str(ch.get("document_id") or ""),
                str(ch.get("id") or ""),
                str(ch.get("title") or ""),
                str(ch.get("content") or "")[:120],
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(ch)
        return out

    def _extract(self, profile: Dict[str, Any], data: List[Dict],
                 county: str, year: str) -> Optional[Dict]:
        """通用引擎计数抽取 + 有据校验；校验不通过返回 None（宁缺勿编）。"""
        counts = extract_counts(data, profile, year)
        total = pick_mode(counts["primary"])
        if total is None:
            return None
        point = pick_mode(counts["secondary"])
        hits = [it for it in counts["primary"] if int(it["value"]) == total]
        evidence = "；".join(dict.fromkeys(str(it.get("sentence") or "") for it in hits))[:200]
        corpus = [str(ch.get("content") or "") for ch in data]
        if not grounded.verify_conclusion(total, profile.get("count_units") or [], evidence, corpus):
            logger.warning(f"[tool_node] 计数读数未通过有据校验，已丢弃: total={total}")
            return None
        titles: List[str] = []
        for it in hits:
            t = str(it.get("title") or "").split("｜")[0].strip()
            if t and t not in titles:
                titles.append(t)
        return {
            "total": int(total),
            "point_total": int(point) if point is not None else None,
            "evidence": evidence,
            "sources": titles[:3],
            "profile": profile.get("name"),
            "source": "rules_extract",
        }

    async def _extract_llm(self, profile: Dict[str, Any], data: List[Dict],
                           county: str, year: str, llm_factory: Callable) -> Optional[Dict]:
        """LLM 语义读数（兜底）：提示词、口径标签、主口径单位均来自档案。"""
        try:
            llm = llm_factory()
        except Exception as e:
            logger.warning(f"[tool_node] LLM 构造失败（跳过语义读数）: {e}")
            return None
        feed = [ch for ch in data[:8] if isinstance(ch, dict) and str(ch.get("content") or "").strip()]
        if not feed:
            return None
        parts = [
            f"【{str(ch.get('title') or '')}】\n{str(ch.get('content') or '')[:1200]}"
            for ch in feed
        ]
        prompt = fill_template(profile.get("llm_extract_prompt"), county=county, year=year)
        try:
            resp = await llm.ainvoke([HumanMessage(content=prompt + "\n\n" + "\n---\n".join(parts))])
        except Exception as e:
            logger.warning(f"[tool_node] LLM 语义读数调用失败: {e}")
            return None
        import json
        import re

        text = (resp.content or "").strip() if isinstance(resp.content, str) else str(resp.content)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            logger.warning(f"[tool_node] LLM 语义读数：无法解析返回, raw={text[:200]}")
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            logger.warning(f"[tool_node] LLM 语义读数：JSON 解析失败 {e}")
            return None
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list) or not items:
            return None
        units = [str(u) for u in (profile.get("count_units") or [])]
        label = str(profile.get("llm_current_scope_label") or "")
        valid: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            scope = str(it.get("scope") or "").strip()
            if scope != label and not (year in scope and "~" not in scope):
                continue
            unit = str(it.get("unit") or it.get("object") or "")
            if units and unit and not any(u in unit for u in units):
                # 单位未声明（模型可能答次口径/无关单位）：仍按主口径处理，由有据校验兜底
                pass
            try:
                n = int(it.get("count"))
            except (TypeError, ValueError):
                continue
            if not 0 < n < 100000:
                continue
            valid.append({"value": n, "sentence": str(it.get("sentence") or "")[:200]})
        total = pick_mode(valid)
        if total is None:
            logger.info("[tool_node] LLM 语义读数：无本年度口径的数量表述")
            return None
        evidence = "；".join(
            dict.fromkeys(str(i["sentence"]) for i in valid if int(i["value"]) == total)
        )[:200]
        corpus = [str(ch.get("content") or "") for ch in feed]
        if not grounded.verify_conclusion(total, profile.get("count_units") or [], evidence, corpus):
            logger.warning(f"[tool_node] LLM 读数未通过有据校验，已丢弃: total={total}")
            return None
        titles: List[str] = []
        for ch in feed:
            t = str(ch.get("title") or "").split("｜")[0].strip()
            if t and t not in titles:
                titles.append(t)
        return {
            "total": int(total),
            "point_total": None,
            "evidence": evidence,
            "sources": titles[:3],
            "profile": profile.get("name"),
            "source": "llm_extract",
        }
