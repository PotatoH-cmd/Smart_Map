#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识库智能问答 Agent
四阶段管道：查询理解 → 多路检索 → 结构化提取 → 推理生成

使用 DashScope Qwen API 驱动推理，替代原 gemma4:31b 的简单 prompt 模式。
"""

import json
import logging
import os
import re
import requests
from typing import Dict, List, Any, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

# 知识库后端自动选择（与 main.py 保持一致）
_kb_backend = os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
if _kb_backend == "llamaindex":
    from .llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from .ragflow_knowledge_tool import KnowledgeBaseTool

logger = logging.getLogger(__name__)

# RagFlow 配置（与 ragflow_knowledge_tool.py 保持一致）
RAGFLOW_API_KEY = os.environ.get(
    'RAGFLOW_API_KEY',
    'ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0'
)
RAGFLOW_API_BASE = os.environ.get(
    'RAGFLOW_API_BASE',
    'http://172.136.16.14:8080/api/v1'
)
RAGFLOW_DATASET_ID = os.environ.get(
    'RAGFLOW_DATASET_ID',
    '538b0a5c36ff11f18e7d3d43671e73e4'
)


class KnowledgeQAAgent:
    """
    知识库智能问答 Agent

    四阶段管道：
    1. 查询理解：提取实体、属性、时间约束
    2. 多路检索：原问题 + 关键词 + 宽泛查询，合并去重
    3. 结构化提取：从 chunk 中提取 JSON 结构化数据
    4. 推理生成：合成最终答案 + 来源引用
    """

    def __init__(self):
        self.kb_tool = KnowledgeBaseTool()
        model_name = os.environ.get("QA_LLM_MODEL", "qwen-plus")
        self.llm = ChatOpenAI(
            model=model_name,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048"),
            temperature=0.1,
        )

    # ─────────────────────────────────────────────
    # 公开入口
    # ─────────────────────────────────────────────

    def answer(self, question: str, top_k: int = 5) -> dict:
        """
        处理用户问题，返回答案 + 来源 + 思考过程
        """
        thinking_steps = []

        # Stage 1: 查询理解
        step1 = self._understand_query(question)
        thinking_steps.append({"stage": 1, "label": "查询理解", "data": step1})

        # Stage 2: 多路检索
        all_chunks, retrieval_details = self._multi_pass_retrieval(question, step1, top_k)
        thinking_steps.append({
            "stage": 2,
            "label": "多路检索",
            "data": {
                "total_chunks": len(all_chunks),
                "passes": retrieval_details,
            }
        })

        # Stage 3: 结构化提取
        extracted_items = self._extract_structured(all_chunks, question)
        thinking_steps.append({
            "stage": 3,
            "label": "结构化提取",
            "data": {
                "extracted_count": len(extracted_items),
                "items": extracted_items[:8],  # 最多展示 8 条
            }
        })

        # Stage 4: 推理生成
        answer_text = self._reason_and_generate(question, extracted_items, all_chunks)
        thinking_steps.append({
            "stage": 4,
            "label": "推理生成",
            "data": {"answer_length": len(answer_text)}
        })

        # 组装来源信息
        sources = []
        for chunk in all_chunks[:10]:
            sources.append({
                "document_id": chunk.get("document_id", ""),
                "title": chunk.get("title", "未知文档"),
                "relevance": round(chunk.get("relevance", 0), 4),
                "snippet": (chunk.get("content", "") or "")[:200],
            })

        return {
            "success": True,
            "answer": answer_text,
            "sources": sources,
            "method": "KnowledgeQAAgent (RagFlow + Qwen)",
            "thinking_steps": thinking_steps,
        }

    # ─────────────────────────────────────────────
    # Stage 1: 查询理解
    # ─────────────────────────────────────────────

    def _understand_query(self, question: str) -> dict:
        """
        用 Qwen 提取问题中的实体、属性、时间约束
        """
        prompt = f"""分析以下用户问题，提取关键信息，返回纯 JSON。

用户问题：{question}

请提取：
- entities: 问题涉及的地理实体/场所名称（如"种子场可采区""潢河"）
- attributes: 问题询问的属性（如"控制高程""面积""深度"）
- time_constraints: 时间相关约束（如"2023年""2024年"）
- keywords: 用于检索的关键词列表（3-5个，去停用词）

只返回 JSON，不要任何解释：
{{"entities":[],"attributes":[],"time_constraints":[],"keywords":[]}}"""

        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            text = resp.content.strip()
            # 清理可能的 markdown 标记
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            logger.info(f"[QA Agent] Stage 1 查询理解: {result}")
            return result
        except Exception as e:
            logger.warning(f"[QA Agent] Stage 1 失败，使用降级策略: {e}")
            return {
                "entities": [],
                "attributes": [],
                "time_constraints": [],
                "keywords": question.split()
            }

    # ─────────────────────────────────────────────
    # Stage 2: 多路检索
    # ─────────────────────────────────────────────

    def _multi_pass_retrieval(
        self, question: str, entities: dict, top_k: int
    ) -> tuple:
        """
        多路检索：原问题 + 关键词 + 宽泛查询 → 合并去重
        返回 (all_chunks, retrieval_details)
        """
        all_chunks = []
        seen_ids = set()
        retrieval_details = []

        # Pass 1: 原问题检索
        chunks1 = self._ragflow_search(question, top_k, similarity_threshold=0.3)
        retrieval_details.append({
            "query": question[:60],
            "threshold": 0.3,
            "results": len(chunks1),
        })
        for c in chunks1:
            cid = c.get("id") or c.get("document_id", "") + "_" + (c.get("content", "") or "")[:40]
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_chunks.append(c)

        # Pass 2: 关键词检索（从提取实体拼关键词）
        keywords = entities.get("keywords", [])
        if keywords:
            kw_query = " ".join(keywords[:5])
            chunks2 = self._ragflow_search(kw_query, top_k, similarity_threshold=0.2)
            retrieval_details.append({
                "query": kw_query[:60],
                "threshold": 0.2,
                "results": len(chunks2),
            })
            for c in chunks2:
                cid = c.get("id") or c.get("document_id", "") + "_" + (c.get("content", "") or "")[:40]
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    all_chunks.append(c)

        # Pass 3: 宽泛查询（用实体名 + 属性名）
        ents = entities.get("entities", [])
        attrs = entities.get("attributes", [])
        if ents or attrs:
            broad_query = " ".join(ents[:2] + attrs[:2])
            if broad_query.strip():
                chunks3 = self._ragflow_search(broad_query, top_k, similarity_threshold=0.2)
                retrieval_details.append({
                    "query": broad_query[:60],
                    "threshold": 0.2,
                    "results": len(chunks3),
                })
                for c in chunks3:
                    cid = c.get("id") or c.get("document_id", "") + "_" + (c.get("content", "") or "")[:40]
                    if cid and cid not in seen_ids:
                        seen_ids.add(cid)
                        all_chunks.append(c)

        # 按 relevance 降序排序，去重后取 top 15
        all_chunks.sort(key=lambda c: c.get("relevance", 0), reverse=True)
        all_chunks = all_chunks[:15]

        logger.info(
            f"[QA Agent] Stage 2 多路检索: 共 {len(all_chunks)} 条去重结果 "
            f"(原始: {len(seen_ids)} 条)"
        )
        return all_chunks, retrieval_details

    def _ragflow_search(
        self, query: str, top_k: int = 5, similarity_threshold: float = 0.3
    ) -> List[dict]:
        """直接调用 RagFlow 检索 API"""
        try:
            url = f"{RAGFLOW_API_BASE}/retrieval"
            headers = {
                "Authorization": f"Bearer {RAGFLOW_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "question": query,
                "dataset_ids": [RAGFLOW_DATASET_ID],
                "top_k": top_k,
                "similarity_threshold": similarity_threshold,
                "vector_similarity_weight": 0.5,
                "rerank_id": "gte-rerank",
                "keyword": False,
                "use_kg": False,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                logger.warning(f"RagFlow search error: {data.get('message')}")
                return []

            chunks = data.get("data", {}).get("chunks", [])
            results = []
            for chunk in chunks:
                results.append({
                    "id": chunk.get("chunk_id", ""),
                    "document_id": chunk.get("document_id", ""),
                    "title": chunk.get("docnm_kwd", "未命名文档"),
                    "content": chunk.get("content_with_weight", ""),
                    "relevance": chunk.get("similarity", 0),
                })
            return results
        except Exception as e:
            logger.error(f"[QA Agent] RagFlow search failed: {e}")
            return []

    # ─────────────────────────────────────────────
    # Stage 3: 结构化提取
    # ─────────────────────────────────────────────

    def _extract_structured(self, chunks: list, question: str) -> list:
        """
        用 Qwen 从每个 chunk 中提取结构化数据

        不再截断 800 字，每个 chunk 截取前 2000 字供提取
        """
        if not chunks:
            return []

        extracted_items = []

        # 分组合并 chunks 避免单次 LLM 调用过大
        group_size = 5
        for group_start in range(0, min(len(chunks), 15), group_size):
            group = chunks[group_start:group_start + group_size]
            group_text_parts = []
            for i, chunk in enumerate(group):
                idx = group_start + i + 1
                content = (chunk.get("content", "") or "")[:2000]
                group_text_parts.append(
                    f"[文档{idx}] 标题: {chunk.get('title', '未知')}\n"
                    f"内容: {content}"
                )
            group_text = "\n\n---\n\n".join(group_text_parts)

            prompt = f"""请从以下参考资料中提取与用户问题相关的结构化数据。

用户问题：{question}

参考资料：
{group_text}

请提取每一条相关数据，返回 JSON 数组。每条数据包含：
- location: 地点/可采区名称
- year: 年份
- attribute: 属性名称（如"控制高程""面积""深度"）
- value: 数值
- unit: 单位（如"m""km²"）
- source_idx: 来源文档编号

如果某条参考资料不包含与问题相关的结构化数据，则不要为它生成条目。

只返回 JSON 数组，不要任何解释："""

            try:
                resp = self.llm.invoke([HumanMessage(content=prompt)])
                text = resp.content.strip()
                text = text.replace("```json", "").replace("```", "").strip()
                items = json.loads(text)
                if isinstance(items, list):
                    for item in items:
                        item["_source_title"] = (
                            group[item.get("source_idx", 1) - 1].get("title", "")
                            if 1 <= item.get("source_idx", 1) <= len(group)
                            else ""
                        )
                    extracted_items.extend(items)
                logger.info(
                    f"[QA Agent] Stage 3 提取 {len(items)} 条数据 "
                    f"from group {group_start}-{group_start + group_size}"
                )
            except Exception as e:
                logger.warning(f"[QA Agent] Stage 3 提取失败 (group {group_start}): {e}")

        return extracted_items

    # ─────────────────────────────────────────────
    # Stage 4: 推理生成
    # ─────────────────────────────────────────────

    def _reason_and_generate(
        self, question: str, extracted_items: list, all_chunks: list
    ) -> str:
        """
        用 Qwen 结合结构化提取结果和原始 chunk，生成最终答案
        """
        # 构建结构化数据摘要
        if extracted_items:
            data_summary = "已提取到的结构化数据：\n"
            for item in extracted_items:
                location = item.get("location", "未知")
                attr = item.get("attribute", "未知属性")
                value = item.get("value", "?")
                unit = item.get("unit", "")
                year = item.get("year", "")
                year_str = f" ({year}年)" if year else ""
                data_summary += (
                    f"  - {location}{year_str}: {attr} = {value}{unit}\n"
                )
        else:
            data_summary = "未能从参考资料中提取到结构化数据。"

        # 构建参考资料摘要（原始 chunk 的前 500 字）
        context_parts = []
        for i, chunk in enumerate(all_chunks[:10]):
            title = chunk.get("title", "未知文档")
            content = (chunk.get("content", "") or "")[:500]
            context_parts.append(f"[来源{i+1}] {title}:\n{content}")
        context = "\n\n".join(context_parts)

        prompt = f"""你是一个知识库智能助手。请根据参考资料回答用户问题。

【用户问题】
{question}

【结构化数据分析】
{data_summary}

【参考资料原文】
{context}

【回答要求】
1. 如果结构化数据中有匹配的信息，直接给出具体数值并引用来源。
2. 如果结构化数据不足，检查参考资料原文中是否有隐含信息。
3. 如果确实不包含相关信息，如实告知，并列出参考资料中可能相关的内容摘要。
4. 回答要简洁、准确，用中文。
5. 每引用一个来源，标注 [来源N]。

请回答："""

        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            answer = resp.content.strip()
            logger.info(f"[QA Agent] Stage 4 生成回答: {len(answer)} 字")
            return answer
        except Exception as e:
            logger.error(f"[QA Agent] Stage 4 失败: {e}")
            return "抱歉，回答生成失败，请稍后重试。"
