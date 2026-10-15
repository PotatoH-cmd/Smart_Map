#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LlamaIndex 知识库工具
基于 LlamaIndex 框架实现本地知识检索和管理，替代 RagFlow 远程 HTTP 调用。

通过环境变量 KNOWLEDGE_BACKEND=llamaindex 激活。
默认使用 DashScope text-embedding-v3 做向量化，JSON 文件持久化索引。
"""

import json
import logging
import os
from typing import Dict, List, Any, Optional, Union

from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool('knowledge_base_tool')
class KnowledgeBaseTool(BaseTool):
    """
    知识库管理工具，接入 LlamaIndex 本地知识库框架，支持对业务文档进行检索和管理。

    支持的操作：
    1. search: 从本地 LlamaIndex 中检索相关信息（向量检索）
    2. list_topics: 查看当前知识库中的文档列表
    3. get_content: 查看特定文档的具体内容
    4. add_document: 向知识库中添加新的文本文档
    5. delete_document: 从知识库中删除指定文档
    6. upload_file: 上传 PDF/Word/TXT 文件到知识库
    """

    description = '''
        知识库管理工具，用于检索和维护业务知识（基于 LlamaIndex 本地知识库框架）。
        你可以使用此工具：
        1. 搜索知识 (search)：从 LlamaIndex 知识库中检索相关信息。
        2. 列出主题 (list_topics)：查看当前知识库中的文档列表，可用 project 限定某个文件夹。
        3. 列出文件夹 (list_folders)：查看知识库中有哪些项目文件夹及各自文档数。
        4. 获取内容 (get_content)：查看特定文档的具体内容。
        5. 添加文档 (add_document)：向知识库中添加新的文本文档。
        6. 删除文档 (delete_document)：从知识库中删除指定文档。
        7. 上传文件 (upload_file)：上传 PDF/Word/TXT 文件到知识库。
        '''

    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '操作类型',
            'enum': ['search', 'list_topics', 'list_folders', 'get_content', 'add_document', 'delete_document', 'upload_file'],
            'required': True
        },
        {
            'name': 'query',
            'type': 'string',
            'description': '搜索关键词（仅 search 时需要）',
            'required': False
        },
        {
            'name': 'top_k',
            'type': 'integer',
            'description': '返回结果数量（仅 search 时需要，默认 8）',
            'required': False
        },
        {
            'name': 'project',
            'type': 'string',
            'description': '项目文件夹过滤（适用于 search / list_topics，可选）：shishifangan=实施方案，chengguobaogao=采砂成果报告，xianchangjiance=现场监测报告；不传则涵盖全部。可先用 list_folders 查看可选文件夹。',
            'required': False
        },
        {
            'name': 'document_id',
            'type': 'string',
            'description': '文档 ID（get_content / delete_document 时需要）',
            'required': False
        },
        {
            'name': 'name',
            'type': 'string',
            'description': '文档名称（仅 add_document 时需要）',
            'required': False
        },
        {
            'name': 'content',
            'type': 'string',
            'description': '文档正文内容（仅 add_document 时需要）',
            'required': False
        },
        {
            'name': 'file_path',
            'type': 'string',
            'description': '要上传的本地文件路径（仅 upload_file 时需要）',
            'required': False
        }
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)

        # ── LlamaIndex 配置 ──
        self._persist_dir = os.environ.get(
            "LLAMAINDEX_PERSIST_DIR",
            os.path.join(os.path.dirname(__file__), "..", "llama_index_storage")
        )
        self._embed_model_name = os.environ.get(
            "LLAMAINDEX_EMBED_MODEL", "text-embedding-v3"
        )
        self._api_key = os.environ.get("DASHSCOPE_API_KEY", "")

        # 延迟初始化 LlamaIndex 组件（首次调用时加载，避免启动时阻塞）
        self._index = None
        self._initialized = False
        self._milvus_store = None  # Milvus 适配器（KB_VECTOR_BACKEND=milvus 时挂载）

        # BM25 检索器缓存（(文档数, retriever)，文档增删后自动重建）
        self._bm25_cache = None

        # RagFlow fallback 配置（用于降级）
        self._ragflow_api_key = os.environ.get("RAGFLOW_API_KEY", "")
        self._ragflow_api_base = os.environ.get("RAGFLOW_API_BASE", "")
        self._ragflow_dataset_id = os.environ.get("RAGFLOW_DATASET_ID", "")

    # ─────────────────────────────────────────────
    # 延迟初始化
    # ─────────────────────────────────────────────

    def _ensure_initialized(self):
        """延迟初始化 LlamaIndex 组件"""
        if self._initialized:
            return True
        try:
            from llama_index.core import Settings, VectorStoreIndex, StorageContext, load_index_from_storage
            from llama_index.embeddings.dashscope import DashScopeEmbedding

            Settings.embed_model = DashScopeEmbedding(
                model_name=self._embed_model_name,
                api_key=self._api_key,
                embed_batch_size=10,  # DashScope API 限制每次最多 10 条
            )
            # DashScope text-embedding-v3 限制每次最多 10 条
            Settings.embed_batch_size = 10
            # 分块参数：每块 512 字，重叠 64 字
            Settings.chunk_size = 512
            Settings.chunk_overlap = 64

            os.makedirs(self._persist_dir, exist_ok=True)

            has_local = os.path.exists(os.path.join(self._persist_dir, "docstore.json"))

            # 向量后端：simple（默认，向量落本地 default__vector_store.json）/ milvus
            #
            # milvus 模式挂载 kb_milvus.LlamaIndexMilvusVectorStore：
            #   - stores_text=False → VectorStoreIndex 照常写 docstore，保持
            #     「docstore 为文本真相来源、Milvus 为向量副本」的架构（官方
            #     MilvusVectorStore.stores_text=True 会跳过 docstore 写入，
            #     曾致「报成功但查不到」）；
            #   - 检索走 Milvus 服务端混合检索（稠密 + jieba/BM25 稀疏 RRF 融合），
            #     project 过滤下推为 expr 前置过滤（partition key 剪枝）。
            vector_backend = os.environ.get("KB_VECTOR_BACKEND", "simple").lower()
            storage_context = None
            if vector_backend == "milvus":
                try:
                    from kb_milvus import LlamaIndexMilvusVectorStore

                    milvus_store = LlamaIndexMilvusVectorStore.from_env()
                    milvus_store.milvus.ensure_collection()
                    if has_local:
                        # docstore/index_store 仍取本地 JSON，向量走 Milvus
                        storage_context = StorageContext.from_defaults(
                            persist_dir=self._persist_dir, vector_store=milvus_store
                        )
                        self._index = load_index_from_storage(storage_context)
                        logger.info(f"[LlamaIndex] 已加载索引（向量后端=Milvus, persist={self._persist_dir}）")
                    else:
                        storage_context = StorageContext.from_defaults(vector_store=milvus_store)
                        self._index = VectorStoreIndex.from_documents(
                            [], storage_context=storage_context
                        )
                        self._index.storage_context.persist(persist_dir=self._persist_dir)
                        logger.info(f"[LlamaIndex] 已创建空索引（向量后端=Milvus）")
                    self._milvus_store = milvus_store
                    self._maybe_autosync_milvus()
                    self._initialized = True
                    return True
                except Exception as e:
                    self._milvus_store = None
                    logger.warning(f"[LlamaIndex] Milvus 后端不可用（{e}），回退 simple 后端")

            # simple 后端（默认行为）
            if has_local:
                try:
                    storage_context = StorageContext.from_defaults(
                        persist_dir=self._persist_dir
                    )
                    self._index = load_index_from_storage(storage_context)
                    logger.info(f"[LlamaIndex] 已从 {self._persist_dir} 加载索引")
                except Exception as e:
                    logger.warning(f"[LlamaIndex] 加载已有索引失败: {e}，将创建空索引")
                    self._index = VectorStoreIndex.from_documents(
                        [], storage_context=StorageContext.from_defaults()
                    )
            else:
                # 首次运行，创建空索引
                from llama_index.core import Document
                self._index = VectorStoreIndex.from_documents(
                    [Document(text="__placeholder__", doc_id="placeholder")],
                )
                # 删除占位文档
                self._index.delete_ref_doc("placeholder", delete_from_docstore=True)
                self._index.storage_context.persist(persist_dir=self._persist_dir)
                logger.info(f"[LlamaIndex] 已在 {self._persist_dir} 创建空索引")

            self._initialized = True
            return True
        except ImportError as e:
            logger.error(f"[LlamaIndex] 依赖未安装: {e}")
            return False
        except Exception as e:
            logger.error(f"[LlamaIndex] 初始化失败: {e}")
            return False

    # ─────────────────────────────────────────────
    # Milvus 自愈与检索
    # ─────────────────────────────────────────────

    def _embed_text(self, text: str) -> list:
        """用当前 embedding 模型计算单条文本向量（自愈路径现算向量用）。"""
        from llama_index.core import Settings

        return list(Settings.embed_model.get_text_embedding(text))

    def _maybe_autosync_milvus(self):
        """启动自愈：docstore 与 Milvus 数量/指纹出现差异时增量修复。

        正常写入路径 insert_nodes/delete_nodes 都会实时同步 Milvus，
        此机制兜底异常中断留下的差异。KB_MILVUS_AUTOSYNC：
          1（默认）count 不一致时全量 diff；full 总是 diff；0 关闭。
        """
        mode = os.environ.get("KB_MILVUS_AUTOSYNC", "1").lower()
        if mode == "0" or self._milvus_store is None:
            return
        try:
            store = self._milvus_store.milvus
            if mode != "full" and store.count() == len(self._index.docstore.docs):
                return
            from kb_milvus import repair_from_docstore

            stats = repair_from_docstore(
                self._index.docstore,
                store,
                encoder=self._milvus_store.bm25_encoder,
                persist_dir=self._persist_dir,
                embed_fn=self._embed_text,
            )
            if stats.get("changed"):
                logger.info(f"[LlamaIndex] Milvus 自愈完成: {stats}")
        except Exception as e:
            logger.warning(f"[LlamaIndex] Milvus 自愈检查失败（不影响检索）: {e}")

    def _retrieve_milvus(self, query: str, fetch_k: int, project: Optional[str] = None):
        """Milvus 服务端混合检索：稠密 + 稀疏（jieba/BM25）双路 → RRFRanker 融合。"""
        from llama_index.core import Settings
        from llama_index.core.schema import NodeWithScore
        from llama_index.core.vector_stores.types import (
            MetadataFilter,
            MetadataFilters,
            VectorStoreQuery,
        )

        filters = None
        if project:
            filters = MetadataFilters(filters=[MetadataFilter(key="project", value=project)])
        vs_query = VectorStoreQuery(
            query_embedding=Settings.embed_model.get_query_embedding(query),
            query_str=query,
            similarity_top_k=fetch_k,
            filters=filters,
        )
        res = self._milvus_store.query(vs_query)
        return [
            NodeWithScore(node=node, score=score)
            for node, score in zip(res.nodes or [], res.similarities or [])
        ]

    # ─────────────────────────────────────────────
    # 公开入口
    # ─────────────────────────────────────────────

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """执行工具调用"""
        if isinstance(params, str):
            params = json.loads(params)

        operation = params.get('operation')

        if operation == 'search':
            return self._search(
                query=params.get('query', ''),
                top_k=params.get('top_k', 8),
                project=params.get('project')
            )
        elif operation == 'list_topics':
            return self._list_topics(project=params.get('project'))
        elif operation == 'list_folders':
            return self._list_folders()
        elif operation == 'get_content':
            return self._get_content(params.get('document_id', ''))
        elif operation == 'get_document_nodes':
            return self._get_document_nodes(params.get('document_id', ''))
        elif operation == 'add_document':
            return self._add_document(
                name=params.get('name', ''),
                content=params.get('content', '')
            )
        elif operation == 'delete_document':
            return self._delete_document(params.get('document_id', ''))
        elif operation == 'upload_file':
            return self._upload_file(params.get('file_path', ''))
        else:
            return {'success': False, 'error': f'不支持的操作: {operation}'}

    # ─────────────────────────────────────────────
    # 核心检索
    # ─────────────────────────────────────────────

    def _search(self, query: str, top_k: int = 8, project: Optional[str] = None) -> Dict[str, Any]:
        """
        从 LlamaIndex 知识库检索信息，失败时降级到 RagFlow HTTP。
        project 不为空时，仅返回该项目文件夹内的结果（按节点 metadata.project 过滤）。

        KB_DETERMINISTIC=1（默认）时走纯混合检索（向量+BM25 RRF → 精排），
        消除 Agent Search 的 LLM 变体生成/评估随机性，保证同 query 多次运行结果一致。
        KB_DETERMINISTIC=0 时走 Agent Search 分级迭代检索（变体生成 → 多路 RRF
        融合 → 启发式/LLM 分级评估 → 不足时改写重查），召回率更高但存在随机性。
        """
        if not query:
            return {'success': False, 'error': '搜索关键词不能为空'}

        if self._ensure_initialized():
            # 确定性模式：纯混合检索，无 LLM 参与（默认）
            if os.environ.get("KB_DETERMINISTIC", "1") == "1":
                try:
                    return self._search_deterministic(query, top_k, project)
                except Exception as e:
                    logger.warning(f"[LlamaIndex] 确定性检索失败，降级单轮混合检索: {e}")
            # Agent Search 分级迭代检索（异常自动降级单轮）
            elif os.environ.get("KB_AGENT_SEARCH", "1") == "1":
                try:
                    return self._search_agent(query, top_k, project)
                except Exception as e:
                    logger.warning(f"[LlamaIndex] agent search 失败，降级单轮检索: {e}")
            try:
                # 需要过滤项目时适当多召回，过滤后再精排截断到 top_k
                fetch_k = max(top_k * 5, 30) if project else max(top_k * 2, 12)
                nodes = self._retrieve_hybrid(query, fetch_k, project=project)

                if project:
                    nodes = [n for n in nodes if (n.metadata or {}).get('project') == project]

                results = self._nodes_to_results(nodes)
                # 通用增强：父片回溯 → 精排 → 同文档配额 → 截断
                results = self._expand_parents(results)
                results = self._rerank_and_trim(query, results, top_k)

                logger.info(f"[LlamaIndex] 检索 '{query[:60]}'（project={project or '全部'}）→ {len(results)} 条结果")
                return {
                    'success': True,
                    'data': results,
                    'count': len(results),
                    'method': 'LlamaIndex Hybrid Retrieval'
                }
            except Exception as e:
                logger.error(f"[LlamaIndex] 检索失败: {e}，降级到 RagFlow")
        else:
            logger.warning("[LlamaIndex] 未初始化，降级到 RagFlow")

        # LlamaIndex 不可用时降级到 RagFlow HTTP
        return self._ragflow_fallback_search(query)

    # ─────────────────────────────────────────────
    # 混合检索（向量 + BM25）与重排
    # ─────────────────────────────────────────────

    def _get_bm25_retriever(self):
        """BM25 检索器（jieba 分词），按文档数缓存，增删文档后自动重建；不可用返回 None。

        仅 simple 后端（客户端融合）走此路径；Milvus 后端用服务端稀疏向量检索。
        """
        try:
            from llama_index.retrievers.bm25 import BM25Retriever
            import jieba
        except Exception as e:
            logger.warning(f"[LlamaIndex] BM25 依赖不可用: {e}")
            return None
        try:
            doc_count = len(self._index.docstore.docs)
            if self._bm25_cache and self._bm25_cache[0] == doc_count:
                return self._bm25_cache[1]
            retriever = BM25Retriever.from_defaults(
                docstore=self._index.docstore,
                tokenizer=jieba.lcut,
                similarity_top_k=50,  # 多召回供融合层截断
            )
            self._bm25_cache = (doc_count, retriever)
            logger.info(f"[LlamaIndex] BM25 索引已构建（{doc_count} 个节点，jieba 分词）")
            return retriever
        except Exception as e:
            logger.warning(f"[LlamaIndex] BM25 索引构建失败: {e}")
            return None

    def _retrieve_hybrid(self, query: str, fetch_k: int, project: Optional[str] = None):
        """向量 + 关键词双路 RRF 融合召回。

        Milvus 模式：服务端混合检索（稠密 + SPARSE_FLOAT_VECTOR 稀疏向量，
        RRFRanker 融合），project 过滤下推为 expr 前置过滤（partition 剪枝）；
        否则回退本地「向量 + BM25(jieba) 客户端 RRF」（BM25 不可用时降级纯向量）。

        不用 QueryFusionRetriever：其构造会 resolve Settings.llm（默认 OpenAI），
        无 OPENAI_API_KEY 时报错退化。RRF 即其 SIMPLE 融合模式的标准算法。
        """
        if self._milvus_store is not None:
            return self._retrieve_milvus(query, fetch_k, project)
        vector_retriever = self._index.as_retriever(similarity_top_k=fetch_k)
        bm25_retriever = self._get_bm25_retriever()
        if bm25_retriever is None:
            return vector_retriever.retrieve(query)
        try:
            vec_nodes = vector_retriever.retrieve(query)
            bm_nodes = bm25_retriever.retrieve(query)
        except Exception as e:
            logger.warning(f"[LlamaIndex] 双路召回失败，退化为纯向量: {e}")
            return vector_retriever.retrieve(query)
        return self._rrf_fuse([vec_nodes, bm_nodes], fetch_k)

    @staticmethod
    def _rrf_fuse(node_lists: List[List], fetch_k: int) -> List:
        """多路节点列表 Reciprocal Rank Fusion（k=60，与 QueryFusionRetriever SIMPLE 模式一致）。

        供单 query 双路召回（向量+BM25）与多 query 跨路融合复用。
        """
        rrf_k = 60
        scores: Dict[str, float] = {}
        nodes_by_id: Dict[str, Any] = {}
        for nodes in node_lists:
            for rank, node in enumerate(nodes):
                nid = node.node_id
                scores[nid] = scores.get(nid, 0.0) + 1.0 / (rrf_k + rank + 1)
                if nid not in nodes_by_id:
                    nodes_by_id[nid] = node
        ranked = sorted(nodes_by_id.values(), key=lambda n: scores.get(n.node_id, 0.0), reverse=True)
        return ranked[:fetch_k]

    # ─────────────────────────────────────────────
    # Agent Search：变体生成 / 分级评估 / 迭代检索
    # ─────────────────────────────────────────────

    def _qwen_call(self, prompt: str, timeout: int = 8) -> str:
        """qwen-flash 单轮调用（同步，复用 falcon_detect 模式），失败抛异常由调用方降级。"""
        import dashscope
        resp = dashscope.Generation.call(
            model=os.environ.get("KB_VARIANT_MODEL", "qwen-flash"),
            api_key=self._api_key,
            messages=[{"role": "user", "content": prompt}],
            result_format="message",
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"qwen-flash 调用失败: {getattr(resp, 'message', resp)}")
        return resp.output.choices[0].message.content.strip()

    def _gen_query_variants(self, query: str) -> List[str]:
        """qwen-flash 生成检索变体（关键词版/同义改写/领域术语），失败返回 []（降级单 query）。"""
        try:
            out = self._qwen_call(
                "你是知识库检索助手。为下面的检索问题生成最多 3 个检索变体，用分号分隔：\n"
                "- 第 1 个：提取核心关键词（去掉口语化表述）\n"
                "- 第 2 个：用政策/规范文件的正式用语改写\n"
                "- 第 3 个：补充领域标准术语或同义词\n"
                "每个变体不超过 20 个字；只输出变体本身，不要解释、编号和标点（分号除外）。\n"
                f"检索问题：{query[:500]}"
            )
            import re as _re
            parts = [p.strip() for p in _re.split(r"[;；、\n]", out)]
            seen, variants = set(), []
            for p in parts:
                if p and p != query and p not in seen:
                    seen.add(p)
                    variants.append(p)
            return variants[:3]
        except Exception as e:
            logger.warning(f"[LlamaIndex] query 变体生成失败（降级单 query）: {e}")
            return []

    def _assess_recall(self, query: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分级评估召回质量：L0 启发式（零成本）→ L1 LLM（L0 不达标才触发）。

        返回 {"sufficient": bool, "level": 0|1, "reason": str}。
        L1 解析失败按 sufficient=True 处理（避免无谓重查）。
        """
        # L0 启发式：top1 分数 + 命中数
        threshold = float(os.environ.get("KB_RECALL_TOP1_THRESHOLD", "0.55"))
        if results and results[0].get("relevance", 0) >= threshold and len(results) >= 3:
            return {"sufficient": True, "level": 0, "reason": f"top1分数 {results[0]['relevance']} ≥ {threshold} 且命中 {len(results)} 条"}

        # L1 LLM 评估：结果能否回答 query
        try:
            import json as _json
            top5 = "\n".join(
                f"{i + 1}. [{r.get('title', '')}] {str(r.get('content', ''))[:200]}"
                for i, r in enumerate(results[:5])
            ) or "（无检索结果）"
            out = self._qwen_call(
                "你是检索质量评估员。判断以下检索结果能否充分回答用户问题。\n"
                '只输出 JSON：{"sufficient": true/false, "gaps": "缺失信息一句话描述，无缺失则空串"}\n'
                f"用户问题：{query[:500]}\n\n检索结果：\n{top5}"
            )
            out = out.strip()
            if out.startswith("```"):
                out = out.strip("`").lstrip("json").strip()
            verdict = _json.loads(out)
            return {
                "sufficient": bool(verdict.get("sufficient", True)),
                "level": 1,
                "reason": str(verdict.get("gaps", "")) or ("LLM判定充分" if verdict.get("sufficient") else "LLM判定不充分"),
            }
        except Exception as e:
            logger.warning(f"[LlamaIndex] L1 评估失败（按充分处理）: {e}")
            return {"sufficient": True, "level": 1, "reason": f"评估失败按充分处理: {e}"}

    @staticmethod
    def _nodes_to_results(nodes: List) -> List[Dict[str, Any]]:
        return [
            {
                'id': node.node_id,
                'document_id': node.metadata.get('document_id', ''),
                'title': node.metadata.get('title', '未命名文档'),
                'content': node.text,
                'relevance': round(node.score or 0, 4),
                'metadata': node.metadata,
            }
            for node in nodes
        ]

    # ─────────────────────────────────────────────
    # 通用检索增强（L1，与领域无关；规则读自 config/kb_rules.yaml）
    # ─────────────────────────────────────────────

    @staticmethod
    def _find_ancestor(sec_map: Dict[str, Any], section_no: str) -> Optional[Any]:
        """按章节编号上溯最近的祖先切片（"3.2.1" → "3.2" → "3"）。

        从**父级**开始上溯（严格祖先，不含自身）；顶级章节（无 "."）无父片返回 None。
        纯结构操作：仅依赖 section_no，不认识任何领域术语。
        """
        sec = (section_no or "").strip().rstrip(".")
        if "." not in sec:
            return None
        sec = sec.rsplit(".", 1)[0]
        while sec:
            node = sec_map.get(sec)
            if node is not None:
                return node
            if "." not in sec:
                return None
            sec = sec.rsplit(".", 1)[0]
        return None

    def _expand_parents(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """small-to-big：命中细粒度切片后回溯其祖先切片，补齐上下文与汇总/总数表述。

        病根场景：答案（如"共涉及 X 个…"）在上级章节切片，而检索只召回了下属细粒度切片。
        """
        try:
            from core.kb_rules import parent_expand_settings
        except Exception:
            return results
        enabled, top = parent_expand_settings()
        if not enabled or not results or top <= 0:
            return results
        docs = getattr(getattr(self._index, "docstore", None), "docs", None) or {}
        if not docs:
            return results
        by_doc_sec: Dict[str, Dict[str, Any]] = {}
        for node in docs.values():
            meta = getattr(node, "metadata", {}) or {}
            did, sec = str(meta.get("document_id") or ""), str(meta.get("section_no") or "")
            if did and sec:
                by_doc_sec.setdefault(did, {}).setdefault(sec, node)
        if not by_doc_sec:
            return results
        seen_ids = {str(r.get("id") or "") for r in results}
        added: List[Dict[str, Any]] = []
        for r in results:
            if len(added) >= top:
                break
            meta = r.get("metadata") or {}
            did, sec = str(meta.get("document_id") or ""), str(meta.get("section_no") or "")
            if not did or not sec:
                continue
            # 已是粗粒度切片（pipeline 约定 chunk_type=parent）无需再上溯，避免冗余与噪声
            if str(meta.get("chunk_type") or "") == "parent":
                continue
            node = self._find_ancestor(by_doc_sec.get(did, {}), sec)
            if node is None:
                continue
            nid = str(getattr(node, "node_id", "") or "")
            if not nid or nid in seen_ids:
                continue
            seen_ids.add(nid)
            added.append({
                'id': nid,
                'document_id': (getattr(node, "metadata", {}) or {}).get('document_id', ''),
                'title': (getattr(node, "metadata", {}) or {}).get('title', '未命名文档'),
                'content': getattr(node, "text", "") or "",
                'relevance': None,
                'metadata': getattr(node, "metadata", {}) or {},
                'expanded_from': r.get('id'),
            })
        if added:
            logger.info(f"[LlamaIndex] 父片回溯补充 {len(added)} 条（small-to-big）")
        return results + added

    def _rerank_and_trim(self, query: str, results: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        """通用后处理：精排 → 同文档配额去冗余 → 截断 top_k。"""
        if not results:
            return results
        reranked = self._rerank(query, results, len(results))
        if reranked:
            results = reranked
        try:
            from core.kb_rules import doc_quota, quota_by_doc
            results = quota_by_doc(results, doc_quota(),
                                   lambda r: (r.get("metadata") or {}).get("document_id"))
        except Exception:
            pass
        return results[:top_k]

    def _retrieve_multi_query(self, query: str, fetch_k: int,
                              project: Optional[str] = None) -> tuple:
        """聚合类问题派生互补 query 并 RRF 融合（python 侧无 LLM，模板来自规则文件）。"""
        try:
            from core.kb_rules import aggregation_queries
            extra = aggregation_queries(query)
        except Exception:
            extra = []
        queries = [query] + [q for q in extra if q != query]
        node_lists = []
        for q in queries[:3]:
            try:
                nodes = self._retrieve_hybrid(q, fetch_k, project=project)
                if nodes:
                    node_lists.append(nodes)
            except Exception as e:
                logger.warning(f"[LlamaIndex] 互补 query 召回失败（{q[:30]}）: {e}")
        if not node_lists:
            return [], queries
        nodes = node_lists[0] if len(node_lists) == 1 else self._rrf_fuse(node_lists, fetch_k)
        return nodes, queries

    def _search_deterministic(self, query: str, top_k: int, project: Optional[str] = None) -> Dict[str, Any]:
        """确定性检索：向量+BM25 RRF 融合 → project 过滤 → 精排截断。

        与 Agent Search 的区别：无 LLM 变体生成、无 LLM 评估、无改写重查，
        同 query 多次运行结果完全一致。向量检索与 BM25 均为确定性算法，
        RRF 融合与精排（gte-rerank-v2）也是确定性的。
        """
        fetch_k = max(top_k * 5, 30) if project else max(top_k * 2, 12)
        # 聚合类问题：派生互补 query 并 RRF 融合（通用机制，模板见 config/kb_rules.yaml）
        nodes, queries = self._retrieve_multi_query(query, fetch_k, project=project)

        if project:
            nodes = [n for n in nodes if (n.metadata or {}).get('project') == project]

        results = self._nodes_to_results(nodes)
        # small-to-big：回溯祖先切片补齐汇总/总数表述，再精排 + 同文档配额 + 截断
        results = self._expand_parents(results)
        results = self._rerank_and_trim(query, results, top_k)

        logger.info(
            f"[LlamaIndex] 确定性检索 '{query[:60]}'（project={project or '全部'}, "
            f"queries={len(queries)}）→ {len(results)} 条结果"
        )
        return {
            'success': True,
            'data': results,
            'count': len(results),
            'method': 'LlamaIndex Deterministic Hybrid Retrieval',
            'search_meta': {
                'strategy': 'deterministic_hybrid',
                'fetch_k': fetch_k,
                'top_k': top_k,
                'queries': queries,
            },
        }

    def _search_agent(self, query: str, top_k: int, project: Optional[str] = None) -> Dict[str, Any]:
        """Agent Search 主流程：分级迭代检索。

        轮1: 原 query 双路召回 → L0 启发式评估（充分即停）
        轮2: 生成变体 → 多 query 召回 RRF 融合 → L1 LLM 评估
        轮3: 基于 L1 缺口改写重查
        """
        max_rounds = int(os.environ.get("KB_AGENT_SEARCH_MAX_ROUNDS", "3"))
        fetch_k = max(top_k * 5, 30) if project else max(top_k * 2, 12)

        queries_tried: List[str] = [query]
        assessment = {"sufficient": False, "level": 0, "reason": ""}
        nodes: List = []

        # 轮1：原 query + L0 启发式评估
        nodes = self._retrieve_hybrid(query, fetch_k, project=project)
        results = self._nodes_to_results(nodes)
        assessment = self._assess_recall(query, results)

        # 轮2+：不充分时变体扩展 / 改写重查
        round_no = 1
        while not assessment["sufficient"] and round_no < max_rounds:
            round_no += 1
            if round_no == 2:
                # 变体扩展：多 query 召回 + 跨 query RRF 融合
                variants = self._gen_query_variants(query)
                if not variants:
                    break  # 变体生成失败，无法继续迭代
                queries_tried.extend(variants)
                node_lists = [nodes]  # 复用轮1结果参与融合
                for v in variants:
                    try:
                        node_lists.append(self._retrieve_hybrid(v, fetch_k, project=project))
                    except Exception as e:
                        logger.warning(f"[LlamaIndex] 变体 '{v[:30]}' 召回失败: {e}")
                nodes = self._rrf_fuse(node_lists, fetch_k)
            else:
                # 轮3：基于 L1 缺口改写重查
                gaps = assessment.get("reason", "")
                try:
                    rewritten = self._qwen_call(
                        "你是知识库检索助手。已有检索结果存在信息缺口，请改写检索词以补齐缺口。\n"
                        "只输出一个改写后的检索词（不超过 25 字），不要解释。\n"
                        f"原始问题：{query[:300]}\n信息缺口：{gaps[:200]}"
                    )
                    if rewritten and rewritten not in queries_tried:
                        queries_tried.append(rewritten)
                        nodes = self._rrf_fuse(
                            [nodes, self._retrieve_hybrid(rewritten, fetch_k, project=project)], fetch_k
                        )
                    else:
                        break
                except Exception as e:
                    logger.warning(f"[LlamaIndex] 缺口改写失败（终止迭代）: {e}")
                    break
            results = self._nodes_to_results(nodes)
            assessment = self._assess_recall(query, results)

        # project 过滤 + 精排截断（与单轮路径一致）
        if project:
            filtered = [n for n in nodes if (n.metadata or {}).get('project') == project]
            nodes = filtered
        results = self._nodes_to_results(nodes)
        if len(results) > top_k:
            reranked = self._rerank(query, results, top_k)
            results = reranked if reranked is not None else results[:top_k]

        logger.info(
            f"[LlamaIndex] agent search '{query[:60]}' → {len(results)} 条 "
            f"(rounds={round_no}, queries={len(queries_tried)}, "
            f"assessment=L{assessment['level']}:{'充分' if assessment['sufficient'] else '不充分'})"
        )
        return {
            'success': True,
            'data': results,
            'count': len(results),
            'method': 'Agent Search (Hybrid + Iterative)',
            'search_meta': {
                'rounds': round_no,
                'queries_tried': queries_tried,
                'assessment': f"L{assessment['level']}:{'充分' if assessment['sufficient'] else '不充分'}",
                'reason': assessment['reason'],
                'strategy': 'agent_search',
            },
        }

    def _rerank(self, query: str, results: List[Dict[str, Any]], top_k: int) -> Optional[List[Dict[str, Any]]]:
        """DashScope gte-rerank-v2 精排；关闭/失败返回 None（调用方保持原序截断）。"""
        if os.environ.get("KB_RERANK", "1") != "1":
            return None
        if not self._api_key or not results:
            return None
        try:
            import httpx
            resp = httpx.post(
                "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": "gte-rerank-v2",
                    "input": {
                        "query": query[:2000],
                        "documents": [r["content"][:1000] for r in results],
                    },
                    "parameters": {"return_documents": False, "top_n": top_k},
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = ((resp.json().get("output") or {}).get("results")) or []
            reranked = []
            for item in items:
                idx = item.get("index")
                if idx is None or not (0 <= idx < len(results)):
                    continue
                row = dict(results[idx])
                row["relevance"] = round(float(item.get("relevance_score", row["relevance"])), 4)
                row["reranked"] = True
                reranked.append(row)
            return reranked or None
        except Exception as e:
            logger.warning(f"[LlamaIndex] rerank 失败（保持原序）: {e}")
            return None

    # ─────────────────────────────────────────────
    # 文档列表
    # ─────────────────────────────────────────────

    def _list_topics(self, project: Optional[str] = None) -> Dict[str, Any]:
        """
        获取 LlamaIndex 知识库中的文档列表。
        每项附带 project / project_name（项目文件夹）；
        传入 project 时仅返回该文件夹内的文档；
        返回中的 folders 始终基于全量节点统计，便于前端渲染文件夹入口。
        """
        if not self._ensure_initialized():
            return self._ragflow_fallback_list_topics()

        try:
            docs = self._index.docstore.docs
            topics = []
            folder_map = {}  # project -> {project, project_name, count}
            for doc_id, doc in docs.items():
                # 跳过占位文档
                if doc_id == "placeholder":
                    continue
                metadata = doc.metadata or {}
                proj = metadata.get("project", "") or ""
                proj_name = metadata.get("project_name", "") or (proj if proj else "未分组")

                # 文件夹统计（基于全量，不受 project 过滤影响）
                if proj not in folder_map:
                    folder_map[proj] = {"project": proj, "project_name": proj_name, "count": 0}
                folder_map[proj]["count"] += 1

                # 项目过滤
                if project and proj != project:
                    continue

                topics.append({
                    "id": doc_id,
                    "name": metadata.get("title", doc_id[:30]),
                    "status": "completed",
                    "created_at": metadata.get("created_at", ""),
                    "size": len(doc.text) if doc.text else 0,
                    "project": proj,
                    "project_name": proj_name,
                    "document_id": metadata.get("document_id", ""),
                })

            folders = sorted(folder_map.values(), key=lambda x: -x["count"])

            return {
                'success': True,
                'data': topics,
                'total': len(topics),
                'folders': folders,
                'project': project or ''
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 列表获取失败: {e}")
            return self._ragflow_fallback_list_topics()

    def _list_folders(self) -> Dict[str, Any]:
        """列出知识库中的项目文件夹及各自文档数。"""
        res = self._list_topics()
        if not res.get('success'):
            return res
        folders = res.get('folders', [])
        return {
            'success': True,
            'folders': folders,
            'total_documents': res.get('total', 0),
            'total_folders': len(folders)
        }

    # ─────────────────────────────────────────────
    # 文档内容
    # ─────────────────────────────────────────────

    def _get_content(self, document_id: str) -> Dict[str, Any]:
        """获取文档完整内容"""
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        if not self._ensure_initialized():
            return self._ragflow_fallback_get_content(document_id)

        try:
            doc = self._index.docstore.get_document(document_id)
            if doc is None:
                return {'success': False, 'error': f'文档 {document_id} 不存在'}

            return {
                'success': True,
                'content': doc.text,
                'document_id': document_id,
                'title': (doc.metadata or {}).get('title', ''),
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 获取内容失败: {e}")
            return self._ragflow_fallback_get_content(document_id)

    def _get_document_nodes(self, document_id: str) -> Dict[str, Any]:
        """按 document_id 取全部节点（溯源预览页用），按章节路径排序。

        优先遍历 docstore（含所有已入库节点），按 breadcrumb 升序还原文档结构。
        """
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}
        if not self._ensure_initialized():
            return {'success': False, 'error': '知识库未初始化'}

        try:
            items = []
            title = ''
            for node_id, doc in self._index.docstore.docs.items():
                meta = doc.metadata or {}
                if meta.get('document_id') != document_id:
                    continue
                if not title:
                    title = str(meta.get('title', '')).split('｜')[0]
                items.append({
                    'node_id': node_id,
                    'title': meta.get('title', ''),
                    'breadcrumb': meta.get('breadcrumb', ''),
                    'section_no': meta.get('section_no', ''),
                    'chunk_type': meta.get('chunk_type', ''),
                    'text': doc.text,
                    'source_url': meta.get('source_url', ''),
                    'image_paths': meta.get('image_paths', ''),
                })
            if not items:
                return {'success': False, 'error': f'文档 {document_id} 不存在或无节点'}

            def _sort_key(it):
                no = str(it.get('section_no') or '')
                return [int(p) if p.isdigit() else 0 for p in no.split('.') if p.strip()] or [9999]

            items.sort(key=_sort_key)
            return {'success': True, 'document_id': document_id, 'title': title, 'sections': items}
        except Exception as e:
            logger.error(f"[LlamaIndex] 获取文档节点失败: {e}")
            return {'success': False, 'error': str(e)}

    # ─────────────────────────────────────────────
    # 添加文档
    # ─────────────────────────────────────────────

    def _add_document(self, name: str, content: str) -> Dict[str, Any]:
        """向本地 LlamaIndex 添加文本文档"""
        if not name or not content:
            return {'success': False, 'error': '文档名称和内容不能为空'}

        if not self._ensure_initialized():
            return self._ragflow_fallback_add_document(name, content)

        try:
            from llama_index.core import Document
            from datetime import datetime as dt

            # 增量更新：内容相同直接跳过；同名不同内容按替换语义删旧插新
            content_hash = self._content_hash(content)
            existing = self._kb_lookup("content_hash", content_hash)
            if existing:
                return {
                    'success': True,
                    'document_id': existing["doc_id"],
                    'name': existing["name"],
                    'dedup': True,
                    'message': f"内容与已有文档「{existing['name']}」相同，已跳过（doc_id={existing['doc_id']}）"
                }
            same_name = self._kb_lookup("name", name)
            if same_name and same_name.get("content_hash") != content_hash:
                # 走 _delete_document 而不是 delete_ref_doc：后者依赖 docstore 的
                # ref_doc_info（本库为空），既不报错也不删任何东西，会把旧文档的
                # 切片永久留在库里 —— 检索得到、文档列表里却看不到的孤儿
                replaced = self._delete_document(same_name["doc_id"])
                if not replaced.get("success"):
                    logger.warning(
                        f"[LlamaIndex] 同名替换: 旧文档删除失败 {same_name['doc_id']}: "
                        f"{replaced.get('error')}"
                    )
                else:
                    logger.info(f"[LlamaIndex] 同名替换: 删除旧文档 '{name}' ({same_name['doc_id']})")

            doc_id = f"doc_{abs(hash(name + content))}_{int(dt.now().timestamp())}"
            doc = Document(
                text=content,
                doc_id=doc_id,
                metadata={
                    "title": name,
                    "document_id": doc_id,
                    "created_at": dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

            self._index.insert(doc)
            # insert() 没有返回值，落库成功的唯一标志是切片真的进了 docstore：
            # 向量后端 stores_text=True（如 Milvus）时它会静默跳过 docstore 写入，
            # 只在 docstore/metadata 留一条 doc_hash，成为查不到也删不掉的幽灵记录
            fp_to_nodes = self._scan_doc_fingerprints(doc_id)
            if not fp_to_nodes:
                logger.error(f"[LlamaIndex] 添加文档 '{name}' 未产生切片，已中止")
                return {'success': False, 'error': '文档未产生任何切片，未写入知识库'}

            self._index.storage_context.persist(persist_dir=self._persist_dir)
            self._kb_record(doc_id, name, content_hash,
                            chunk_count=len(fp_to_nodes), source="add_document")

            logger.info(f"[LlamaIndex] 添加文档 '{name}' → {doc_id}")
            return {
                'success': True,
                'document_id': doc_id,
                'name': name,
                'message': f"文档 '{name}' 已添加到 LlamaIndex 知识库"
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 添加文档失败: {e}")
            return self._ragflow_fallback_add_document(name, content)

    # ─────────────────────────────────────────────
    # 删除文档
    # ─────────────────────────────────────────────

    def _resolve_doc_identity(self, given_id: str) -> Dict[str, Any]:
        """判断传入 id 属于文档级还是切片级。

        文档管理页历史上把切片节点 id 当文档 id 传给删除接口，
        `delete_ref_doc` 对切片 id 既不报错也不动作，导致「假删除」：
        接口报成功但数据完好。这里先做身份判定，明确拒绝切片 id。
        """
        docs = self._index.docstore.docs
        if given_id in docs:
            meta = docs[given_id].metadata or {}
            did = meta.get("document_id") or ""
            count = sum(
                1 for n in docs.values()
                if (n.metadata or {}).get("document_id") == did
            )
            return {"kind": "chunk", "doc_id": did, "node_count": count}
        count = sum(
            1 for n in docs.values()
            if (n.metadata or {}).get("document_id") == given_id
        )
        if count:
            return {"kind": "document", "doc_id": given_id, "node_count": count}
        return {"kind": "unknown", "doc_id": given_id, "node_count": 0}

    def _delete_document(self, document_id: str) -> Dict[str, Any]:
        """从本地 LlamaIndex 删除文档（含其全部切片）。

        不做 RagFlow 回退：本地删除失败即返回失败并说明原因，
        静默回退会掩盖「什么都没删但报成功」的问题。
        """
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        if not self._ensure_initialized():
            return {'success': False, 'error': '知识库未初始化，无法删除'}

        try:
            ident = self._resolve_doc_identity(document_id)
            if ident['kind'] == 'chunk':
                return {
                    'success': False,
                    'error': (
                        f'传入的是切片节点 id（属于文档 {ident["doc_id"]}），'
                        f'删除需要文档级 document_id'
                    ),
                    'hint': 'chunk_id_given',
                    'document_id': ident['doc_id'],
                }
            if ident['kind'] == 'unknown':
                # docstore 中无该文档的节点，但可能只登记在清单里
                # （如 add_document 未真正落盘节点的历史遗留），
                # 这种情况应允许从清单中清除，否则会成为无法删除的幽灵文档。
                if self._kb_lookup("doc_id", document_id):
                    self._kb_delete(document_id)
                    logger.info(f"[LlamaIndex] 清除无切片的清单记录: {document_id}")
                    return {
                        'success': True,
                        'document_id': document_id,
                        'removed_chunks': 0,
                        'message': '该文档在库中无切片，已从清单中移除',
                    }
                return {
                    'success': False,
                    'error': f'文档不存在: {document_id}',
                    'hint': 'not_found',
                }

            before = ident['node_count']
            # 不用 delete_ref_doc：它依赖 docstore 的 ref_doc_info，
            # 而本库 ref_doc_info 为空（0/90 篇），调用它既不报错也不删任何东西
            # ——这正是原先「删除报成功但数据完好」的深层原因。
            # 直接按 document_id 元数据取节点 id 删除，不依赖任何旁支索引。
            node_ids = [
                nid for nid, n in self._index.docstore.docs.items()
                if (n.metadata or {}).get('document_id') == document_id
            ]
            if node_ids:
                try:
                    self._index.delete_nodes(node_ids, delete_from_docstore=True)
                except Exception as e:
                    logger.warning(f"[LlamaIndex] 向量库删除异常（继续清理本地节点）: {e}")
                # 补删 docstore/index_struct 作为幂等防御：LlamaIndex 的
                # delete_nodes 仅在 `not vector_store.stores_text` 时才清理
                # docstore（kb_milvus 适配器声明 stores_text=False，正常会清理；
                # 若换回 stores_text=True 的后端，此处保证 docstore 真相来源
                # 仍被删除——官方 MilvusVectorStore 曾因该分支报「删除未生效」）。
                # 本库的真相来源是 docstore，故必须显式补删。
                for nid in node_ids:
                    try:
                        self._index.index_struct.delete(nid)
                    except Exception:
                        pass
                    self._index.docstore.delete_document(nid, raise_error=False)
            after = sum(
                1 for n in self._index.docstore.docs.values()
                if (n.metadata or {}).get('document_id') == document_id
            )
            if after > 0:
                logger.error(
                    f"[LlamaIndex] 删除未生效: {document_id} 仍残留 {after}/{before} 个切片"
                )
                return {
                    'success': False,
                    'error': f'删除未生效：仍有 {after}/{before} 个切片留在库中',
                    'remaining': after,
                }

            self._index.storage_context.persist(persist_dir=self._persist_dir)
            self._kb_delete(document_id)

            logger.info(f"[LlamaIndex] 删除文档: {document_id}（{before} 个切片）")
            return {
                'success': True,
                'document_id': document_id,
                'removed_chunks': before,
                'message': f'已删除文档及其 {before} 个切片',
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 删除文档失败: {e}")
            return {'success': False, 'error': f'删除失败: {e}'}

    # ─────────────────────────────────────────────
    # 文件上传
    # ─────────────────────────────────────────────

    def _upload_file(self, file_path: str) -> Dict[str, Any]:
        """上传本地文件到 LlamaIndex"""
        if not file_path or not os.path.isfile(file_path):
            return {'success': False, 'error': f'文件不存在: {file_path}'}

        if not self._ensure_initialized():
            return self._ragflow_fallback_upload_file(file_path)

        try:
            from llama_index.core import SimpleDirectoryReader
            from datetime import datetime as dt

            file_name = os.path.basename(file_path)

            # 用 SimpleDirectoryReader 加载文件
            documents = SimpleDirectoryReader(
                input_files=[file_path]
            ).load_data()

            if not documents:
                return {'success': False, 'error': f'无法解析文件: {file_path}'}

            # 增量更新：按解析出的正文 hash 去重；同名不同内容按替换语义清理旧节点
            content_text = "\n".join(d.get_content() for d in documents)
            content_hash = self._content_hash(content_text)
            existing = self._kb_lookup("content_hash", content_hash)
            if existing:
                return {
                    'success': True,
                    'document_id': existing["doc_id"],
                    'name': existing["name"],
                    'dedup': True,
                    'message': f"文件内容与已有文档「{existing['name']}」相同，已跳过"
                }
            same_name = self._kb_lookup("name", file_name)
            if same_name:
                removed = self._delete_file_nodes_by_source(file_path)
                self._kb_delete(same_name["doc_id"])
                if removed:
                    logger.info(f"[LlamaIndex] 同名替换: 清理旧文件节点 {removed} 个")

            # 为每个文档节点添加元数据
            base_id = f"file_{abs(hash(file_path))}_{int(dt.now().timestamp())}"
            for i, doc in enumerate(documents):
                doc.doc_id = f"{base_id}_{i}"
                if not doc.metadata:
                    doc.metadata = {}
                doc.metadata["title"] = file_name
                doc.metadata["document_id"] = doc.doc_id
                doc.metadata["source_file"] = file_path
                doc.metadata["created_at"] = dt.now().strftime("%Y-%m-%d %H:%M:%S")

            # 插入索引
            for doc in documents:
                self._index.insert(doc)
            self._index.storage_context.persist(persist_dir=self._persist_dir)
            self._kb_record(base_id, file_name, content_hash,
                            chunk_count=len(documents), source="upload_file")

            logger.info(f"[LlamaIndex] 上传文件 '{file_name}' → {len(documents)} 个节点")
            return {
                'success': True,
                'document_id': base_id,
                'name': file_name,
                'message': f"文件 '{file_name}' 已上传到 LlamaIndex（{len(documents)} 个分段）"
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 上传文件失败: {e}")
            return self._ragflow_fallback_upload_file(file_path)

    # ─────────────────────────────────────────────
    # kb_documents 文档清单（去重 / 增量更新）
    # ─────────────────────────────────────────────

    @staticmethod
    def _content_hash(text: str) -> str:
        import hashlib
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def _kb_db_path(self) -> str:
        return os.environ.get(
            "MAPASSIST_DB_PATH",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.db"),
        )

    def _manifest(self):
        """惰性构造切片清单（kb_documents + kb_chunks）。"""
        if getattr(self, "_manifest_obj", None) is None:
            from kb_pipeline import Manifest
            self._manifest_obj = Manifest(self._kb_db_path())
        return self._manifest_obj

    def _kb_lookup(self, field: str, value: str) -> Optional[Dict[str, Any]]:
        """按字段查 kb_documents 清单；表不存在时自动建表。"""
        try:
            return self._manifest().lookup(field, value)
        except Exception as e:
            logger.warning(f"[LlamaIndex] kb_documents 查询失败: {e}")
            return None

    def _kb_record(self, doc_id: str, name: str, content_hash: str,
                   chunk_count: int, source: str) -> None:
        """写文档级记录，并把该文档当前切片的指纹登记进 kb_chunks。"""
        try:
            m = self._manifest()
            # 以 docstore 里实际的切片数为准：调用方常只能给出估算值
            fp_to_nodes = self._scan_doc_fingerprints(doc_id)
            if fp_to_nodes:
                chunk_count = len(fp_to_nodes)
            m.upsert_document(
                doc_id=doc_id, name=name, content_hash=content_hash,
                chunk_count=chunk_count, source=source,
            )
            # 登记切片指纹，使后续增量能精确到片
            if fp_to_nodes:
                m.replace_document_chunks(doc_id, fp_to_nodes)
        except Exception as e:
            logger.warning(f"[LlamaIndex] kb_documents 写入失败: {e}")

    def _scan_doc_fingerprints(self, doc_id: str) -> Dict[str, Any]:
        """从 docstore 扫出某文档的 chunk_fp -> [node_id]。"""
        from kb_pipeline.fingerprint import fingerprint_of_meta
        out: Dict[str, Any] = {}
        try:
            docs = getattr(self._index.docstore, "docs", {}) or {}
            for nid, node in docs.items():
                meta = getattr(node, "metadata", {}) or {}
                if meta.get("document_id") != doc_id:
                    continue
                fp = meta.get("chunk_fp") or fingerprint_of_meta(
                    meta, getattr(node, "text", "") or node.get_content()
                )
                out.setdefault(fp, []).append(nid)
        except Exception as e:
            logger.warning(f"[LlamaIndex] 扫描切片指纹失败: {e}")
        return out

    def _kb_delete(self, doc_id: str) -> None:
        try:
            self._manifest().delete_document(doc_id)
        except Exception:
            pass


    def _delete_file_nodes_by_source(self, file_path: str) -> int:
        """按 source_file 元数据删除文件对应的所有节点（同名替换语义）。"""
        removed = 0
        try:
            docs = getattr(self._index.docstore, "docs", {}) or {}
            stale = [
                did for did, d in docs.items()
                if (getattr(d, "metadata", {}) or {}).get("source_file") == file_path
            ]
            for did in stale:
                try:
                    self._index.delete_ref_doc(did, delete_from_docstore=True)
                    removed += 1
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[LlamaIndex] 按 source_file 清理旧节点失败: {e}")
        return removed

    # ─────────────────────────────────────────────
    # RagFlow Fallback 降级方法
    # ─────────────────────────────────────────────

    def _ragflow_fallback_search(self, query: str) -> Dict[str, Any]:
        """RagFlow HTTP 降级检索"""
        import requests
        try:
            url = f"{self._ragflow_api_base}/retrieval"
            headers = {
                "Authorization": f"Bearer {self._ragflow_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "question": query,
                "dataset_ids": [self._ragflow_dataset_id],
                "top_k": 8,
                "similarity_threshold": 0.3,
                "vector_similarity_weight": 0.5,
                "keyword": False,
                "use_kg": False
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                return {'success': False, 'error': f"RagFlow fallback 失败: {data.get('message')}"}

            chunks = data.get("data", {}).get("chunks", [])
            results = []
            for chunk in chunks:
                results.append({
                    'id': chunk.get("chunk_id", ""),
                    'document_id': chunk.get("document_id", ""),
                    'title': chunk.get("docnm_kwd", "未命名文档"),
                    'content': chunk.get("content", ""),
                    'relevance': round(chunk.get("similarity", 0), 4),
                })

            return {
                'success': True,
                'data': results,
                'count': len(results),
                'method': 'RagFlow Retrieval (LlamaIndex fallback)'
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 检索失败: {e}")
            return {'success': False, 'error': f"知识库检索失败（LlamaIndex 和 RagFlow fallback 均不可用）: {str(e)}"}

    def _ragflow_fallback_list_topics(self) -> Dict[str, Any]:
        """RagFlow HTTP 降级列表"""
        import requests
        try:
            url = f"{self._ragflow_api_base}/datasets/{self._ragflow_dataset_id}/documents"
            headers = {"Authorization": f"Bearer {self._ragflow_api_key}"}
            params = {"page": 1, "page_size": 100, "orderby": "create_time", "desc": "true"}
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                return {'success': False, 'error': data.get('message')}
            docs = data.get("data", {}).get("docs", [])
            topics = [{"id": d.get("id"), "name": d.get("name"), "status": d.get("run", "unknown")} for d in docs]
            return {'success': True, 'data': topics, 'total': len(topics)}
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 列表失败: {e}")
            return {'success': False, 'error': str(e)}

    def _ragflow_fallback_get_content(self, document_id: str) -> Dict[str, Any]:
        """RagFlow HTTP 降级获取内容"""
        import requests
        try:
            url = f"{self._ragflow_api_base}/datasets/{self._ragflow_dataset_id}/documents/{document_id}/chunks"
            headers = {"Authorization": f"Bearer {self._ragflow_api_key}"}
            chunks = []
            page = 1
            while True:
                params = {"page": page, "page_size": 100}
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    break
                batch = data.get("data", {}).get("chunks", [])
                chunks.extend(batch)
                total = data.get("data", {}).get("total", 0)
                if len(chunks) >= total or not batch:
                    break
                page += 1

            full_content = "\n\n".join([c.get("content", "") for c in chunks if c.get("content")])
            return {'success': True, 'content': full_content, 'document_id': document_id}
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 获取内容失败: {e}")
            return {'success': False, 'error': str(e)}

    def _ragflow_fallback_add_document(self, name: str, content: str) -> Dict[str, Any]:
        """RagFlow HTTP 降级添加"""
        import requests
        try:
            url = f"{self._ragflow_api_base}/datasets/{self._ragflow_dataset_id}/documents"
            headers = {"Authorization": f"Bearer {self._ragflow_api_key}", "Content-Type": "application/json"}
            payload = {"name": name, "content": content, "parser_method": "manual"}
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return {'success': False, 'error': data.get('message')}
            doc_info = data.get("data", {}) or {}
            if isinstance(doc_info, list) and doc_info:
                doc_info = doc_info[0]
            return {'success': True, 'document_id': doc_info.get("id", ""), 'name': name, 'message': f"文档 '{name}' 已提交到 RagFlow（fallback）"}
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 添加失败: {e}")
            return {'success': False, 'error': str(e)}

    def _ragflow_fallback_delete_document(self, document_id: str) -> Dict[str, Any]:
        """RagFlow HTTP 降级删除"""
        import requests
        try:
            url = f"{self._ragflow_api_base}/datasets/{self._ragflow_dataset_id}/documents"
            headers = {"Authorization": f"Bearer {self._ragflow_api_key}", "Content-Type": "application/json"}
            payload = {"ids": [document_id]}
            resp = requests.delete(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return {'success': False, 'error': data.get('message')}
            return {'success': True, 'document_id': document_id, 'message': '文档已从 RagFlow 中删除（fallback）'}
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 删除失败: {e}")
            return {'success': False, 'error': str(e)}

    def _ragflow_fallback_upload_file(self, file_path: str) -> Dict[str, Any]:
        """RagFlow HTTP 降级上传"""
        import requests
        file_name = os.path.basename(file_path)
        try:
            url = f"{self._ragflow_api_base}/datasets/{self._ragflow_dataset_id}/documents"
            headers = {"Authorization": f"Bearer {self._ragflow_api_key}"}
            with open(file_path, 'rb') as f:
                files = {'file': (file_name, f, 'application/octet-stream')}
                resp = requests.post(url, headers=headers, files=files, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return {'success': False, 'error': data.get('message')}
            doc_info = data.get("data", {}) or {}
            if isinstance(doc_info, list) and doc_info:
                doc_info = doc_info[0]
            return {'success': True, 'document_id': doc_info.get("id", ""), 'name': file_name, 'message': f"文件已上传到 RagFlow（fallback）"}
        except Exception as e:
            logger.error(f"[LlamaIndex] RagFlow fallback 上传失败: {e}")
            return {'success': False, 'error': str(e)}
