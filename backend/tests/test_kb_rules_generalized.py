"""
test_kb_rules_generalized.py — 检索策略通用化（L1 检索增强 / L2 有据校验 / L3 规则外置）。

覆盖（纯内存，不联网、不调真实 LLM）：
1. L3 规则引擎：聚合语义判定与 query 派生、单位模式负向断言、口径判定（年度锚定优先）、
   主/次口径抽取与众数汇总、同文档配额、模板填充。
2. L2 有据校验：归一化、证据句比对、数字+单位比对（编号类误命中必须被拒）、结论整体校验。
3. L1 检索增强：child→父片结构回溯、同文档配额去冗余（rerank 关闭时的确定性路径）。
4. 领域层薄化：档案驱动的定向检索端到端（含 grounded 拒编造）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import grounded
from core import kb_rules as R

PROFILE = {
    "name": "test_profile",
    "trigger": {"entity_terms": ["砂场"], "count_terms": ["多少"]},
    "count_units": ["可采区", "采区"],
    "secondary_units": ["可采点"],
    "current_year_markers": ["本方案", "本年度", "年度计划"],
    "answer_templates": {"with_total": "根据《{source}》，{county} {year} 年度共规划 {n} 个可采区（砂场）{point_clause}{db_clause}。"},
    "no_evidence_template": "未找到 {county} {year} 年度可采区数量的明确表述。",
}


# ----------------------------------------------------------------------
# L3 规则引擎
# ----------------------------------------------------------------------

def test_aggregation_query_detection_and_expansion():
    # 规则文件缺失时不得抛错（增强关闭）
    os.environ["KB_RULES_PATH"] = "/nonexistent/kb_rules.yaml"
    R.load_rules(force=True)
    assert R.is_aggregation_query("2025年固始县有多少个砂场") is False
    assert R.aggregation_queries("2025年固始县有多少个砂场") == []
    assert R.parent_expand_settings() == (False, 3)
    assert R.doc_quota() == 0
    del os.environ["KB_RULES_PATH"]
    R.load_rules(force=True)
    # 规则文件存在时：聚合判定 + 模板派生
    assert R.is_aggregation_query("2025年固始县有多少个砂场") is True
    assert R.is_aggregation_query("帮我切换到卫星图层") is False
    extra = R.aggregation_queries("固始县2025砂场数量")
    assert len(extra) == 2 and all("固始县2025砂场数量" in q for q in extra)


def test_unit_pattern_rejects_code_labels():
    pat = R.build_unit_pattern(["可采区", "采区"])
    assert pat.search("计划14 个可采区").group(1) == "14"
    assert pat.search("本方案共涉及14 个采区").group(1) == "14"
    # 编号/图号类不得误命中
    for bad in ("SGH4 采区主河槽", "SH5 采区主槽", "图 5.6-55 采区现场照片", "5.3.11 采区"):
        assert pat.search(bad) is None, bad


def test_scope_current_year_wins_over_plan_citation():
    """核心回归：引用《规划报告》但含目标年度的句子必须记为本年度口径。"""
    assert R.sentence_scope_ok("根据《规划报告》，2025 年计划对史灌河、史河、灌河14 个可采区", "2025", PROFILE) is True
    assert R.sentence_scope_ok("本方案共涉及14 个采区", "2025", PROFILE) is True
    assert R.sentence_scope_ok("2024年度计划开采5 个采区", "2025", PROFILE) is False
    assert R.sentence_scope_ok("竹竿河共设可采区14段", "2025", PROFILE) is False


def test_extract_counts_primary_secondary_and_mode():
    from collections import Counter
    chunks = [
        {"id": "c1", "title": "固始县史灌河2025年度方案", "content":
         "根据《规划报告》，2025 年计划对史灌河、史河、灌河14 个可采区，16 个可采点进行开采。"},
        {"id": "c2", "title": "固始县史灌河2025年度方案", "content":
         "本方案共涉及14 个采区，开采总量582.39万m3。"
         "禁采区可采区17个其中史河7个灌河5个史灌河5个可采区（规划期口径，无年度）。"},
    ]
    counts = R.extract_counts(chunks, PROFILE, "2025")
    primary = Counter(it["value"] for it in counts["primary"])
    assert primary == Counter({14: 2})           # 规划期无年度句被口径过滤，"5" 不计入
    assert R.pick_mode(counts["primary"]) == 14
    assert R.pick_mode(counts["secondary"]) == 16


def test_quota_by_doc_and_fill_template():
    items = [{"d": "a"}, {"d": "a"}, {"d": "a"}, {"d": "b"}]
    kept = R.quota_by_doc(items, 2, lambda x: x["d"])
    assert [x["d"] for x in kept] == ["a", "a", "b"]
    assert R.quota_by_doc(items, 0, lambda x: x["d"]) == items
    assert R.fill_template("A{q}B{n}", q="x", n=3) == "AxB3"
    assert R.fill_template("A{q}B", other=1) == "A{q}B"   # 缺失键保留，便于排查


# ----------------------------------------------------------------------
# L2 有据校验
# ----------------------------------------------------------------------

def test_normalize_and_grounding():
    corpus = ["涉及14 个可采区16 个可采点，利用已有12 处储砂点"]
    assert grounded.sentence_grounded("涉及14 个可采区16 个可采点", corpus) is True
    assert grounded.sentence_grounded("共规划 99 个可采区", corpus) is False
    assert grounded.value_grounded(14, ["可采区"], corpus) is True
    assert grounded.value_grounded(99, ["可采区"], corpus) is False
    # 归一化：HTML 标签、空白、标点差异不应影响判定
    assert grounded.sentence_grounded("涉及 14 个可采区，16 个可采点", corpus) is True


def test_value_grounded_rejects_code_label_hits():
    """编号类文本（SGH4 采区 / SH5 采区）不得把 4、5 当作"4/5 个采区"放行。"""
    corpus = ["SGH4 采区位于史灌河主河槽，SH5 采区主槽上游"]
    assert grounded.value_grounded(4, ["可采区", "采区"], corpus) is False
    assert grounded.value_grounded(5, ["可采区", "采区"], corpus) is False
    # 真值仍需放行
    corpus2 = ["2025 年度计划14 个可采区"]
    assert grounded.value_grounded(14, ["可采区"], corpus2) is True


def test_verify_conclusion_requires_grounding():
    corpus = ["本方案共涉及14 个采区"]
    assert grounded.verify_conclusion(14, ["采区"], "本方案共涉及14 个采区", corpus) is True
    assert grounded.verify_conclusion(5, ["采区"], "共涉及5 个采区", corpus) is False
    assert grounded.verify_conclusion(None, ["采区"], "", corpus) is False


# ----------------------------------------------------------------------
# L1 检索增强（父片回溯 / 同文档配额）
# ----------------------------------------------------------------------

class _FakeNode:
    def __init__(self, nid, sec, did, title, text, chunk_type):
        self.node_id = nid
        self.text = text
        self.metadata = {"section_no": sec, "document_id": did, "title": title, "chunk_type": chunk_type}


class _FakeDocstore:
    def __init__(self, docs):
        self.docs = docs


class _FakeIndex:
    def __init__(self, docs):
        self.docstore = _FakeDocstore(docs)


def _kb_tool():
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
    return KnowledgeBaseTool()


def test_expand_parents_small_to_big():
    docs = {
        "p15": _FakeNode("p15", "1.5", "d1", "方案｜1.5 采区基本情况",
                         "根据《规划报告》，2025 年计划…14 个可采区，16 个可采点", "parent"),
        "c151": _FakeNode("c151", "1.5.1", "d1", "方案｜1.5.1 沈营采区", "本年度开采点位于…", "child"),
        "other": _FakeNode("other", "9", "d2", "他文｜9 章节", "无关内容", "parent"),
    }
    tool = _kb_tool()
    tool._index = _FakeIndex(docs)
    hits = [{"id": "c151", "content": "本年度开采点位于…",
             "metadata": {"section_no": "1.5.1", "document_id": "d1"}}]
    out = tool._expand_parents(hits)
    assert [r["id"] for r in out] == ["c151", "p15"]      # child 命中后补回父片
    assert out[1]["expanded_from"] == "c151"
    # 无父片可回溯时不追加、不报错
    lone = [{"id": "other", "content": "无关内容", "metadata": {"section_no": "9", "document_id": "d2"}}]
    assert tool._expand_parents(lone) == lone


def test_rerank_and_trim_doc_quota(monkeypatch):
    monkeypatch.setenv("KB_RERANK", "0")   # 关闭精排，验证纯确定性后处理
    tool = _kb_tool()
    tool._index = _FakeIndex({})
    results = [
        {"id": f"a{i}", "content": "x", "metadata": {"document_id": "A"}} for i in range(5)
    ] + [{"id": "b0", "content": "y", "metadata": {"document_id": "B"}}]
    kept = tool._rerank_and_trim("q", results, 10)
    per_doc = {}
    for r in kept:
        per_doc[r["metadata"]["document_id"]] = per_doc.get(r["metadata"]["document_id"], 0) + 1
    assert per_doc["A"] == R.doc_quota() and "B" in per_doc


# ----------------------------------------------------------------------
# 领域层薄化：档案驱动定向检索（含 grounded 拒编造）
# ----------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    async def ainvoke(self, messages):
        return type("R", (), {"content": self.payload})()


def _run(coro):
    return asyncio.run(coro)


CHUNKS_GUSHI = [
    {"id": "p15", "document_id": "d1", "title": "固始县史灌河2025年度河道采砂实施方案（报批本）｜1.5 采区基本情况",
     "content": "根据《规划报告》，2025 年计划对史灌河、史河、灌河14 个可采区，16 个可采点进行开采。"},
    {"id": "p61", "document_id": "d1", "title": "固始县史灌河2025年度河道采砂实施方案（报批本）｜6.1 主要结论",
     "content": "（1）主要编制成果是：涉及14 个可采区16 个可采点，利用已有12 处储砂点。"},
    {"id": "plan", "document_id": "d9", "title": "规划报告｜3 采区规划",
     "content": "可采区17个，其中史河7个、灌河5个、史灌河5个可采区。"},
]


def _invoke_factory(chunks):
    async def _invoke(step):
        return {"tool_name": "knowledge_base_tool",
                "result": {"success": True, "data": list(chunks), "count": len(chunks)}}
    return _invoke


def test_provider_targeted_search_extracts_and_grounds():
    from agents.providers.sandfield_kb import SandfieldKbProvider
    prov = SandfieldKbProvider()
    out = _run(prov.targeted_search(
        "2025年固始县有多少个砂场", [],
        invoke_step=_invoke_factory(CHUNKS_GUSHI), llm_factory=lambda: _FakeLLM("{}"),
    ))
    assert out and out[0]["tool_name"] == "knowledge_base_tool"
    pe = out[0]["result"]["plan_extract"]
    assert pe["total"] == 14 and pe["point_total"] == 16
    assert pe["source"] == "rules_extract"
    assert "固始县" in pe["sources"][0]
    # 规划期口径句（17个/5个）不得污染读数
    assert pe["total"] != 17 and pe["total"] != 5
    # 历史年度问题不走该档案定向检索
    assert _run(prov.targeted_search(
        "2023年罗山县有多少个砂场", [],
        invoke_step=_invoke_factory(CHUNKS_GUSHI), llm_factory=lambda: _FakeLLM("{}"),
    )) is None
    # 非触发问题不介入
    assert _run(prov.targeted_search(
        "切换到卫星图层", [], invoke_step=_invoke_factory(CHUNKS_GUSHI),
        llm_factory=lambda: _FakeLLM("{}"),
    )) is None


def test_provider_llm_reading_rejected_when_ungrounded():
    """LLM 兜底读数若在原文中无据（如编造"5 个采区"），必须被拒。"""
    from agents.providers.sandfield_kb import SandfieldKbProvider
    prov = SandfieldKbProvider()
    chunks = [{"id": "c1", "document_id": "d1", "title": "固始县史灌河2025年度方案｜3.2 开采控制条件",
               "content": "表 3.2-1 各采区控制开采高程与开采量统计，SGH4 采区主河槽桩号33+000。"}]
    fabricated = '{"items": [{"object": "", "scope": "本年度", "count": 5, "sentence": "本年度共涉及5 个采区"}]}'
    out = _run(prov.targeted_search(
        "2025年固始县有多少个砂场", [],
        invoke_step=_invoke_factory(chunks), llm_factory=lambda: _FakeLLM(fabricated),
    ))
    assert out and "plan_extract" not in out[0]["result"]   # 无据 → 不产出确定性读数


def test_provider_llm_reading_accepted_when_grounded():
    from agents.providers.sandfield_kb import SandfieldKbProvider
    prov = SandfieldKbProvider()
    chunks = [{"id": "c1", "document_id": "d1", "title": "固始县史灌河2025年度方案｜6.1 主要结论",
               "content": "（2）主要编制成果为：涉及 14 个可采区、16 个可采点。"}]
    payload = '{"items": [{"object": "", "scope": "本年度", "count": 14, "sentence": "主要编制成果为：涉及 14 个可采区、16 个可采点。"}]}'
    out = _run(prov.targeted_search(
        "2025年固始县有多少个砂场", [],
        invoke_step=_invoke_factory(chunks), llm_factory=lambda: _FakeLLM(payload),
    ))
    pe = out[0]["result"]["plan_extract"]
    assert pe["total"] == 14 and pe["source"] == "llm_extract"


def test_count_provider_uses_yaml_templates(tmp_path, monkeypatch):
    """确定性回答文案来自规则模板（非代码硬编码），且无据时不编造。"""
    from agents.providers.sandfield import SandfieldCountProvider
    prov = SandfieldCountProvider()
    state = {"user_message": "2025年固始县有多少个砂场"}
    results = [{"tool_name": "knowledge_base_tool", "result": {
        "success": True, "data": [{"title": "固始县史灌河2025年度河道采砂实施方案（报批本）"}],
        "plan_extract": {"total": 14, "point_total": 16, "sources": ["固始县史灌河2025年度河道采砂实施方案（报批本）"],
                         "source": "rules_extract"},
    }}]
    answer = prov.get_answer(state, results)
    assert answer and "共规划 14 个可采区（砂场）" in answer and "16 个可采点" in answer
    assert "固始县史灌河2025年度河道采砂实施方案" in answer
    # 无 plan_extract、无许可文档、DB 未调用 → 不下结论（交回 LLM）
    assert prov.get_answer(state, [{"tool_name": "knowledge_base_tool",
                                    "result": {"success": True, "data": []}}]) is None


# ----------------------------------------------------------------------
# 砂场统计双步计划：许可口径检索步骤按档案年度上限开关（规则驱动）
# ----------------------------------------------------------------------

def _make_intent():
    from agents.intent_types import IntentType, IntentResult
    return IntentResult(primary_intent=IntentType.DATA_QUERY, confidence=0.9, entities=[],
                        task_context="t", execution_plan=[], requires_confirmation=False,
                        suggestions=[])


def test_sandfield_plan_skips_permit_step_after_cap_year():
    """2025（许可停发后）不再发许可口径 KB 检索步骤；2023 仍保留（规则上限来自 YAML）。"""
    from agents.providers.intent_corrections import SandfieldCountPlanProvider
    prov = SandfieldCountPlanProvider()
    out2025 = prov.correct("2025年固始县有多少个砂场", _make_intent())
    tools2025 = [s.tool for s in out2025["execution_plan"]]
    assert tools2025 == ["postgresql_tool"]          # 无无效的许可文档检索步骤
    out2023 = prov.correct("2023年罗山县有多少个砂场", _make_intent())
    steps2023 = out2023["execution_plan"]
    assert [s.tool for s in steps2023] == ["postgresql_tool", "knowledge_base_tool"]
    assert "采砂许可" in steps2023[1].params["query"]  # 检索词来自档案模板
    # 非触发问题不介入
    assert prov.correct("切换到卫星图层", _make_intent()) is None
