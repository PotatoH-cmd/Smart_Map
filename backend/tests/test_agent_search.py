"""
test_agent_search.py — 知识检索 Agent Search 分级迭代单元测试。

覆盖：
  - _gen_query_variants：解析/去重/失败降级
  - _assess_recall：L0 启发式（不调 LLM）/ L1 LLM 评估 / 解析失败按充分
  - _rrf_fuse：多路融合去重排序
  - _search 分发：KB_AGENT_SEARCH 开关
  - _search_agent：轮1 充分即停 / 多轮迭代（变体+改写）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from tools.llamaindex_knowledge_tool import KnowledgeBaseTool


class FakeNode:
    def __init__(self, nid, score=0.5, text="内容", metadata=None):
        self.node_id = nid
        self.score = score
        self.text = text
        self.metadata = metadata or {}


@pytest.fixture()
def tool():
    t = KnowledgeBaseTool(cfg=None)
    t._api_key = "test-key"
    return t


# ----------------------------------------------------------------------
# _gen_query_variants
# ----------------------------------------------------------------------

def test_gen_query_variants_parses_and_dedupes(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "_qwen_call",
        lambda prompt, timeout=8: "河道采砂管理部门; 采砂许可审批流程；河道采砂管理部门、非法采砂处罚规定")
    variants = tool._gen_query_variants("挖沙子要找谁批准")
    assert variants == ["河道采砂管理部门", "采砂许可审批流程", "非法采砂处罚规定"]


def test_gen_query_variants_failure_returns_empty(tool, monkeypatch):
    def boom(prompt, timeout=8):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(tool, "_qwen_call", boom)
    assert tool._gen_query_variants("任意问题") == []


# ----------------------------------------------------------------------
# _assess_recall
# ----------------------------------------------------------------------

def _mk_results(n, relevance):
    return [{"relevance": relevance, "title": "t", "content": "c"} for _ in range(n)]


def test_assess_recall_l0_pass_no_llm(tool, monkeypatch):
    def no_llm(prompt, timeout=8):
        raise AssertionError("L0 达标不应调用 LLM")
    monkeypatch.setattr(tool, "_qwen_call", no_llm)
    verdict = tool._assess_recall("问题", _mk_results(5, 0.8))
    assert verdict["sufficient"] is True
    assert verdict["level"] == 0


def test_assess_recall_l1_llm_verdict(tool, monkeypatch):
    monkeypatch.setattr(
        tool, "_qwen_call",
        lambda prompt, timeout=8: '{"sufficient": false, "gaps": "缺少处罚条款"}')
    verdict = tool._assess_recall("问题", _mk_results(2, 0.2))
    assert verdict["sufficient"] is False
    assert verdict["level"] == 1
    assert verdict["reason"] == "缺少处罚条款"


def test_assess_recall_l1_parse_fail_treated_sufficient(tool, monkeypatch):
    monkeypatch.setattr(tool, "_qwen_call", lambda prompt, timeout=8: "不是JSON")
    verdict = tool._assess_recall("问题", [])
    assert verdict["sufficient"] is True
    assert verdict["level"] == 1


# ----------------------------------------------------------------------
# _rrf_fuse
# ----------------------------------------------------------------------

def test_rrf_fuse_dedup_and_rank():
    a = [FakeNode("n1", 0.9), FakeNode("n2", 0.8), FakeNode("n3", 0.7)]
    b = [FakeNode("n2", 0.95), FakeNode("n4", 0.6), FakeNode("n1", 0.5)]
    fused = KnowledgeBaseTool._rrf_fuse([a, b], fetch_k=10)
    ids = [n.node_id for n in fused]
    assert set(ids) == {"n1", "n2", "n3", "n4"}  # 去重
    # n1/n2 双路命中，RRF 累计分更高，应排在单路 n3/n4 前
    assert set(ids[:2]) == {"n1", "n2"}
    assert len(KnowledgeBaseTool._rrf_fuse([a, b], fetch_k=2)) == 2  # 截断


# ----------------------------------------------------------------------
# _search 分发：KB_AGENT_SEARCH 开关
# ----------------------------------------------------------------------

def test_search_dispatch_disabled(monkeypatch, tool):
    monkeypatch.setenv("KB_AGENT_SEARCH", "0")

    def agent_should_not_run(query, top_k, project=None):
        raise AssertionError("关闭开关不应走 agent search")
    monkeypatch.setattr(tool, "_search_agent", agent_should_not_run)
    monkeypatch.setattr(tool, "_ensure_initialized", lambda: False)
    sentinel = {"success": True, "data": [], "from": "ragflow"}
    monkeypatch.setattr(tool, "_ragflow_fallback_search", lambda q: sentinel)
    assert tool._search("任意", 8) is sentinel


def test_search_dispatch_enabled(monkeypatch, tool):
    monkeypatch.setenv("KB_AGENT_SEARCH", "1")
    monkeypatch.setenv("KB_DETERMINISTIC", "0")  # 确定性模式优先，须显式关闭才能分发到 agent search
    monkeypatch.setattr(tool, "_ensure_initialized", lambda: True)
    sentinel = {"success": True, "data": [], "search_meta": {"rounds": 1}}
    monkeypatch.setattr(tool, "_search_agent", lambda q, k, p=None: sentinel)
    assert tool._search("任意", 8) is sentinel


def test_search_agent_exception_falls_back_to_single(monkeypatch, tool):
    monkeypatch.setenv("KB_AGENT_SEARCH", "1")
    monkeypatch.setenv("KB_DETERMINISTIC", "0")
    monkeypatch.setattr(tool, "_ensure_initialized", lambda: True)

    def boom(query, top_k, project=None):
        raise RuntimeError("agent search 崩了")
    monkeypatch.setattr(tool, "_search_agent", boom)
    nodes = [FakeNode("n1", 0.9), FakeNode("n2", 0.8)]
    monkeypatch.setattr(tool, "_retrieve_hybrid", lambda q, k, project=None: nodes)
    monkeypatch.setattr(tool, "_rerank", lambda q, rs, k: None)
    out = tool._search("任意", 8)
    assert out["success"] is True
    assert out["method"] == "LlamaIndex Hybrid Retrieval"


# ----------------------------------------------------------------------
# _search_agent 迭代逻辑
# ----------------------------------------------------------------------

def test_search_agent_round1_sufficient_stops(monkeypatch, tool):
    """轮1 L0 评估充分 → 不生成变体，rounds=1。"""
    monkeypatch.setenv("KB_AGENT_SEARCH_MAX_ROUNDS", "3")
    nodes = [FakeNode(f"n{i}", 0.9 - i * 0.01) for i in range(5)]

    def variants_should_not_run(query):
        raise AssertionError("轮1充分不应生成变体")
    monkeypatch.setattr(tool, "_retrieve_hybrid", lambda q, k, project=None: nodes)
    monkeypatch.setattr(tool, "_assess_recall",
                        lambda q, rs: {"sufficient": True, "level": 0, "reason": "ok"})
    monkeypatch.setattr(tool, "_gen_query_variants", variants_should_not_run)
    monkeypatch.setattr(tool, "_rerank", lambda q, rs, k: None)

    out = tool._search_agent("问题", 8)
    assert out["search_meta"]["rounds"] == 1
    assert out["search_meta"]["queries_tried"] == ["问题"]
    assert out["count"] <= 8


def test_search_agent_multi_round_iterations(monkeypatch, tool):
    """轮1不充分 → 轮2变体 → 轮3缺口改写，逐轮评估直到充分或轮次耗尽。"""
    monkeypatch.setenv("KB_AGENT_SEARCH_MAX_ROUNDS", "3")
    nodes = [FakeNode(f"n{i}", 0.3) for i in range(5)]
    monkeypatch.setattr(tool, "_retrieve_hybrid", lambda q, k, project=None: nodes)
    monkeypatch.setattr(tool, "_gen_query_variants",
                        lambda query: ["变体A", "变体B"])
    # LLM 缺口改写（轮3）
    monkeypatch.setattr(tool, "_qwen_call", lambda prompt, timeout=8: "改写后的检索词")
    monkeypatch.setattr(tool, "_rerank", lambda q, rs, k: None)

    verdicts = [
        {"sufficient": False, "level": 1, "reason": "缺少管理部门信息"},  # 轮1（L1）
        {"sufficient": False, "level": 1, "reason": "仍缺手续条款"},      # 轮2
        {"sufficient": True, "level": 1, "reason": "已覆盖"},            # 轮3
    ]
    calls = {"n": 0}

    def fake_assess(query, results):
        v = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        return v
    monkeypatch.setattr(tool, "_assess_recall", fake_assess)

    out = tool._search_agent("挖沙要办什么手续", 8)
    assert calls["n"] == 3
    assert out["search_meta"]["rounds"] == 3
    assert out["search_meta"]["queries_tried"] == [
        "挖沙要办什么手续", "变体A", "变体B", "改写后的检索词"]
    assert out["search_meta"]["strategy"] == "agent_search"
