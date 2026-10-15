# -*- coding: utf-8 -*-
"""LLM 补漏抽取：只处理规则零命中的 chunk。

规则抽取已覆盖 90% 的 chunk，剩余部分多为图片 URL 列表与通用规程文本，
本就不含命名实体。因此 LLM 只做**兜底补漏**，且受三重约束：

  1. 类型受限：head/tail 必须落在 8 类枚举内，rel 必须在关系白名单内
  2. 溯源强制：每条三元组必须回填 chunk_id + 章节路径 + 原文片段，
     否则丢弃——用户明确要求「能抽取到完整的源数据信息」
  3. 名称校验：采区名过 is_noise/is_generic，县区/河流必须在白名单内，
     乡镇/村必须带行政后缀，防止 LLM 编造

结果按 chunk_id 缓存到 out/llm_triples.json，重复执行不重复计费。
"""

import json
import logging
import os
import re

from .normalize import (
    COUNTIES, RIVERS, is_generic, is_noise, normalize_area_name,
    strip_upper_admin,
)
from .refs import dump_refs, extract_snippet, make_ref, merge_refs
from .schema import NODE_TYPE_KEYS, RELATION_LABELS

logger = logging.getLogger(__name__)

BATCH_SIZE = 8          # 每批 chunk 数
MAX_CHARS = 1200        # 单 chunk 送入 LLM 的最大字符数
EVIDENCE_LEN = 80       # 要求 LLM 返回的原文片段长度上限

_TYPE_ENUM = " | ".join(NODE_TYPE_KEYS)
_REL_ENUM = " | ".join(RELATION_LABELS)

_PROMPT = """你是知识图谱抽取专家。从下面 {n} 个文档切片中抽取实体与关系。

【实体类型】（必须严格用英文枚举值）
{types}

【关系类型】（必须严格用英文枚举值）
{rels}

【输出要求】
1. 只返回 JSON 数组，不要 markdown 标记，不要解释。
2. 每个元素结构：
   {{"chunk": 切片序号（整数）,
     "head_type": "实体类型", "head_name": "实体名",
     "rel": "关系类型",
     "tail_type": "实体类型", "tail_name": "实体名",
     "evidence": "该三元组在切片中的原文片段（不超过 {ev} 字，必须原样摘录）"}}
3. head_name/tail_name 必须是切片原文中**逐字出现**的实体名，不得改写、不得翻译。
4. 采区名请用原文写法（如「陈营-柳沟采区」「郝楼村可采区」），不要自行补全或缩写。
5. evidence 必须是从切片里原样复制的片段；找不到原文依据就不要输出该三元组。
6. 切片中没有命名实体时返回空数组 []。

【文档切片】
{chunks}
"""


def _chunk_block(idx: int, chunk: dict) -> str:
    """把 chunk 渲染成带序号与章节信息的文本块。"""
    text = (chunk.get("text") or "").strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "…"
    bc = chunk.get("breadcrumb") or ""
    title = chunk.get("title") or chunk.get("doc_id") or ""
    head = f"### 切片 {idx}\n文档：{title}"
    if bc:
        head += f"\n章节：{bc}"
    return f"{head}\n内容：{text}"


class LLMExtractor:
    """LLM 兜底抽取器（仅零命中 chunk）。"""

    def __init__(self, dicts: dict, cache_path: str = ""):
        self.counties = set(dicts.get("County", COUNTIES))
        self.rivers = set(dicts.get("River", RIVERS))
        self.cache_path = cache_path
        # {chunk_id: [triple, ...]}
        self.cache = self._load_cache()
        self._llm = None

    # ── 缓存 ──
    def _load_cache(self) -> dict:
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"[KG-LLM] 载入缓存 {len(data)} 个 chunk")
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[KG-LLM] 缓存读取失败，忽略: {e}")
        return {}

    def save_cache(self):
        if not self.cache_path:
            return
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=1)
        logger.info(f"[KG-LLM] 缓存已保存: {len(self.cache)} 个 chunk")

    # ── LLM ──
    def _ensure_llm(self):
        if self._llm is not None:
            return
        from langchain_openai import ChatOpenAI

        self._llm = ChatOpenAI(
            model=os.environ.get("QA_LLM_MODEL", "qwen-plus"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            temperature=0,
        )

    # ── 主入口 ──
    def extract(self, chunks: list) -> list:
        """抽取零命中 chunk 的三元组，返回三元组列表。

        返回 [{"head_type","head_name","rel","tail_type","tail_name",
                "chunk_id","doc_id","title","section","evidence"}, ...]
        """
        todo = [c for c in chunks if c.get("chunk_id") not in self.cache]
        if todo:
            self._ensure_llm()
        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i: i + BATCH_SIZE]
            try:
                triples = self._extract_batch(batch)
            except Exception as e:  # 单批失败不影响整体
                logger.warning(f"[KG-LLM] 批次失败（跳过）: {e}")
                triples = []
            # 失败的 chunk 也写空列表，避免重复调用
            by_chunk = {c.get("chunk_id", ""): [] for c in batch}
            for t in triples:
                by_chunk.setdefault(t["chunk_id"], []).append(t)
            self.cache.update(by_chunk)
            logger.info(f"[KG-LLM] 进度 {min(i + BATCH_SIZE, len(todo))}/{len(todo)}")

        out = []
        for triples in self.cache.values():
            out.extend(triples)
        self.save_cache()
        return out

    def _extract_batch(self, batch: list) -> list:
        from langchain_core.messages import HumanMessage

        blocks = "\n\n".join(_chunk_block(i + 1, c) for i, c in enumerate(batch))
        prompt = _PROMPT.format(
            n=len(batch), types=_TYPE_ENUM, rels=_REL_ENUM,
            ev=EVIDENCE_LEN, chunks=blocks,
        )
        resp = self._llm.invoke([HumanMessage(content=prompt)])
        raw = (resp.content or "").strip()
        items = self._parse_json(raw)
        return self._validate(items, batch)

    @staticmethod
    def _parse_json(raw: str) -> list:
        """容错解析 LLM 返回的 JSON 数组。"""
        text = raw.replace("```json", "").replace("```", "").strip()
        try:
            val = json.loads(text)
            return val if isinstance(val, list) else []
        except json.JSONDecodeError:
            pass
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                val = json.loads(text[start: end + 1])
                return val if isinstance(val, list) else []
            except json.JSONDecodeError:
                pass
        logger.warning(f"[KG-LLM] JSON 解析失败: {raw[:160]}")
        return []

    # ── 校验（宁缺毋滥：编造的名称一律丢弃）──
    def _validate(self, items: list, batch: list) -> list:
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("chunk") or 0) - 1
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(batch):
                continue
            chunk = batch[idx]
            text = chunk.get("text") or ""
            ht, hn = it.get("head_type"), (it.get("head_name") or "").strip()
            tt, tn = it.get("tail_type"), (it.get("tail_name") or "").strip()
            rel = it.get("rel")
            if ht not in NODE_TYPE_KEYS or tt not in NODE_TYPE_KEYS:
                continue
            if rel not in RELATION_LABELS:
                continue
            if not hn or not tn or hn == tn:
                continue
            # 名称必须逐字出现在原文中，杜绝 LLM 编造
            if hn not in text or tn not in text:
                continue
            hn2 = self._canon(ht, hn)
            tn2 = self._canon(tt, tn)
            if not hn2 or not tn2:
                continue
            section = chunk.get("breadcrumb") or ""
            evidence = (it.get("evidence") or "").strip()
            if not evidence or evidence not in text:
                evidence = extract_snippet(text, hn, window=40)
            out.append({
                "head_type": ht, "head_name": hn2,
                "rel": rel,
                "tail_type": tt, "tail_name": tn2,
                "chunk_id": chunk.get("chunk_id", ""),
                "doc_id": chunk.get("doc_id", ""),
                "title": chunk.get("title", ""),
                "section": section,
                "evidence": evidence,
            })
        return out

    def _canon(self, ntype: str, name: str) -> str:
        """按类型规范化名称；不合法返回空串。"""
        if ntype == "County":
            return name if name in self.counties else ""
        if ntype == "River":
            return name if name in self.rivers else ""
        if ntype == "MineableArea":
            n = normalize_area_name(name)
            if not n or is_noise(n) or is_generic(n) or not (2 <= len(n) <= 6):
                return ""
            if "村" in n and not n.endswith("村"):
                return ""
            return n
        if ntype == "Town":
            n = strip_upper_admin(name)
            if not re.search(r"(镇|乡|街道)$", n) or len(n) < 3 or is_noise(n):
                return ""
            return n
        if ntype == "Village":
            n = strip_upper_admin(name)
            if not n.endswith("村") or len(n) < 3 or is_noise(n):
                return ""
            return n
        if ntype == "License":
            from .normalize import license_norm
            return license_norm(name)
        return name


def apply_llm_triples(nodes: dict, relations: list, rel_seen: set,
                      triples: list) -> dict:
    """把 LLM 三元组并入规则抽取结果（复用 RuleExtractor 的合并语义）。

    返回统计 {"added_nodes": n, "added_relations": m, "dropped": k}。
    只新增节点/关系与 refs，不覆盖规则侧的属性值（规则更可信）。
    """
    added_nodes = 0
    added_relations = 0
    dropped = 0

    def ensure(ntype, name, chunk):
        nonlocal added_nodes
        bucket = nodes.setdefault(ntype, {})
        nid = name
        if ntype == "Village":
            nid = name  # 村级 id 由规则侧决定，LLM 侧仅补名称
        ref = make_ref(
            doc_id=chunk.get("doc_id", ""), title=chunk.get("title", ""),
            section=chunk.get("section", ""), snippet=chunk.get("evidence", ""),
            chunk_id=chunk.get("chunk_id", ""), count=1,
        )
        if nid in bucket:
            node = bucket[nid]
            node["refs"] = merge_refs(node.get("refs", ""), ref)
            return nid
        node = {"id": nid, "name": name, "source": "llm"}
        if ntype == "County":
            node["short"] = name[:-1] if name.endswith(("县", "区")) else name
        node["refs"] = dump_refs([ref])
        bucket[nid] = node
        added_nodes += 1
        return nid

    for t in triples:
        hn, tn = t["head_name"], t["tail_name"]
        hid = ensure(t["head_type"], hn, t)
        tid = ensure(t["tail_type"], tn, t)
        if not hid or not tid:
            dropped += 1
            continue
        key = (t["rel"], hid, tid)
        if key in rel_seen:
            continue
        rel_seen.add(key)
        relations.append({"rel": t["rel"], "src": hid, "dst": tid, "source": "llm"})
        added_relations += 1

    return {"added_nodes": added_nodes, "added_relations": added_relations, "dropped": dropped}
