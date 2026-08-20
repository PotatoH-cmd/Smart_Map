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
from typing import Dict, List, Any, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

# 知识库后端自动选择（与 main.py 保持一致）
_kb_backend = os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
if _kb_backend == "llamaindex":
    from .llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from .ragflow_knowledge_tool import KnowledgeBaseTool

logger = logging.getLogger(__name__)


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
        self._kg_tool = None  # 延迟加载知识图谱工具
        model_name = os.environ.get("QA_LLM_MODEL", "qwen-plus")
        self.llm = ChatOpenAI(
            model=model_name,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=os.environ.get("DASHSCOPE_API_KEY", "sk-e4990da94bfb4037be1f755fa586d048"),
            temperature=0.1,
        )

    def _get_kg(self):
        """延迟加载知识图谱工具"""
        if self._kg_tool is None:
            try:
                from .knowledge_graph_tool import get_kg
                self._kg_tool = get_kg()
            except Exception as e:
                logger.warning(f"[QA Agent] 知识图谱工具加载失败: {e}")
        return self._kg_tool

    # ─────────────────────────────────────────────
    # 公开入口
    # ─────────────────────────────────────────────

    def answer(self, question: str, top_k: int = 5) -> dict:
        """
        处理用户问题，返回答案 + 来源 + 思考过程

        双通道架构：
        - 实体统计问题 → 知识图谱精确查询
        - 文档理解问题 → 向量检索语义匹配
        """
        thinking_steps = []

        # Stage 1: 查询理解
        step1 = self._understand_query(question)
        thinking_steps.append({"stage": 1, "label": "查询理解", "data": step1})

        # Stage 2: 多路检索（向量通道）
        all_chunks, retrieval_details = self._multi_pass_retrieval(question, step1, top_k)
        thinking_steps.append({
            "stage": 2,
            "label": "多路检索",
            "data": {
                "total_chunks": len(all_chunks),
                "passes": retrieval_details,
            }
        })

        # Stage 2.5: 知识图谱检索（图谱通道）
        kg_result = None
        is_entity_q = self._detect_entity_query(question, step1)
        if is_entity_q:
            kg_result = self._graph_retrieval(question)
            thinking_steps.append({
                "stage": "2.5",
                "label": "知识图谱检索",
                "data": {
                    "is_entity_query": True,
                    "success": kg_result.get("success", False) if kg_result else False,
                    "answer": (kg_result.get("answer", "") or "")[:150] if kg_result else "",
                }
            })

        # Stage 3: 结构化提取
        extracted_items = self._extract_structured(all_chunks, question)
        thinking_steps.append({
            "stage": 3,
            "label": "结构化提取",
            "data": {
                "extracted_count": len(extracted_items),
                "items": extracted_items[:8],
            }
        })

        # Stage 4: 推理生成（合并图谱结果）
        answer_text = self._reason_and_generate(question, extracted_items, all_chunks, kg_result)
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
            "method": f"KnowledgeQAAgent ({_kb_backend.upper()} + Qwen{' + KG' if kg_result and kg_result.get('success') else ''})",
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
        chunks1 = self._kb_search(question, top_k)
        retrieval_details.append({
            "query": question[:60],
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
            chunks2 = self._kb_search(kw_query, top_k)
            retrieval_details.append({
                "query": kw_query[:60],
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
                chunks3 = self._kb_search(broad_query, top_k)
                retrieval_details.append({
                    "query": broad_query[:60],
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

    def _kb_search(self, query: str, top_k: int = 5) -> List[dict]:
        """使用 self.kb_tool 统一检索（自动适配 LlamaIndex / RagFlow 后端）"""
        try:
            result = self.kb_tool._search(query=query, top_k=top_k)
            if result.get("success"):
                chunks = result.get("data", [])
                # 统一字段名
                normalized = []
                for c in chunks:
                    normalized.append({
                        "id": c.get("id", ""),
                        "document_id": c.get("document_id", ""),
                        "title": c.get("title", "未命名文档"),
                        "content": c.get("content", ""),
                        "relevance": c.get("relevance", 0),
                    })
                logger.info(
                    f"[QA Agent] KB search '{query[:60]}' → {len(normalized)} results "
                    f"(method: {result.get('method', 'unknown')})"
                )
                return normalized
            else:
                logger.warning(f"[QA Agent] KB search failed: {result.get('error')}")
                return []
        except Exception as e:
            logger.error(f"[QA Agent] KB search exception: {e}")
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
        self, question: str, extracted_items: list, all_chunks: list,
        kg_result: dict = None
    ) -> str:
        """
        用 Qwen 结合结构化提取结果、知识图谱结果和原始 chunk，生成最终答案
        """
        # 知识图谱结果
        kg_section = ""
        if kg_result and kg_result.get("success"):
            kg_section = (
                f"【知识图谱精确查询结果】\n"
                f"{kg_result.get('answer', '')}\n"
                f"(Cypher 查询: {kg_result.get('cypher', '')})\n"
            )

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

{kg_section}
【结构化数据分析】
{data_summary}

【参考资料原文】
{context}

【回答要求】
1. 如果知识图谱有精确查询结果（非"未找到"或"0条记录"），优先使用图谱数据作为权威答案。
2. 如果图谱显示"未找到"或"0条记录"，说明该数据不在图谱数据库范围内，请忽略图谱结果，以文档检索为准。
3. 如果结构化数据中有匹配的信息，直接给出具体数值并引用来源。
4. 如果结构化数据不足，检查参考资料原文中是否有隐含信息。
5. 如果确实不包含相关信息，如实告知，并列出参考资料中可能相关的内容摘要。
6. 回答要简洁、准确，用中文。
7. 每引用一个来源，标注 [来源N]。

请回答："""

        try:
            resp = self.llm.invoke([HumanMessage(content=prompt)])
            answer = resp.content.strip()
            logger.info(f"[QA Agent] Stage 4 生成回答: {len(answer)} 字")
            return answer
        except Exception as e:
            logger.error(f"[QA Agent] Stage 4 失败: {e}")
            return "抱歉，回答生成失败，请稍后重试。"

    # ─────────────────────────────────────────────
    # 知识图谱通道
    # ─────────────────────────────────────────────

    def _detect_entity_query(self, question: str, entities: dict) -> bool:
        """
        检测是否为实体统计类问题（适合用知识图谱精确查询）

        判定规则：
        1. 包含计数词：几个、多少个、几条、多少、数量、统计
        2. 包含列举词：哪些、哪几个、列出
        3. 包含实体 + 比较/属性词
        """
        # 计数关键词
        count_patterns = [
            r'几个', r'多少个', r'多少', r'几条',
            r'数量', r'个数', r'总计', r'统计',
            r'一共有', r'共有',
        ]
        for pat in count_patterns:
            if re.search(pat, question):
                logger.info(f"[QA Agent] 检测到实体统计问题 (计数词: {pat})")
                return True

        # 列举关键词
        list_patterns = [r'哪些', r'哪几个', r'列出', r'分别是']
        for pat in list_patterns:
            if re.search(pat, question):
                logger.info(f"[QA Agent] 检测到实体列举问题 (列举词: {pat})")
                return True

        # 如果提取到了地理实体且有属性查询
        ents = entities.get("entities", [])
        attrs = entities.get("attributes", [])
        if ents and attrs:
            logger.info(f"[QA Agent] 检测到实体+属性查询: entities={ents}, attrs={attrs}")
            return True

        return False

    def _graph_retrieval(self, question: str) -> Optional[dict]:
        """
        调用知识图谱工具进行精确查询
        """
        kg = self._get_kg()
        if kg is None:
            return None

        try:
            result = kg.query(question)
            logger.info(
                f"[QA Agent] 图谱检索: success={result.get('success')}, "
                f"answer={str(result.get('answer', ''))[:80]}"
            )
            return result
        except Exception as e:
            logger.error(f"[QA Agent] 图谱检索失败: {e}")
            return None
