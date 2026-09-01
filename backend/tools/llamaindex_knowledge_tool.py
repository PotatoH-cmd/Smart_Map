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

        # BM25 检索器缓存（(文档数, retriever)，文档增删后自动重建）
        self._bm25_cache = None

        # RagFlow fallback 配置（用于降级）
        self._ragflow_api_key = os.environ.get(
            "RAGFLOW_API_KEY", "ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
        )
        self._ragflow_api_base = os.environ.get(
            "RAGFLOW_API_BASE", "http://172.136.16.14:8080/api/v1"
        )
        self._ragflow_dataset_id = os.environ.get(
            "RAGFLOW_DATASET_ID", "538b0a5c36ff11f18e7d3d43671e73e4"
        )

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

            # 向量后端：milvus（默认，不可用自动回退 simple）/ simple
            vector_backend = os.environ.get("KB_VECTOR_BACKEND", "milvus").lower()
            storage_context = None
            if vector_backend == "milvus":
                try:
                    from llama_index.vector_stores.milvus import MilvusVectorStore
                    milvus_store = MilvusVectorStore(
                        uri=os.environ.get("MILVUS_URI", "http://127.0.0.1:19530"),
                        collection=os.environ.get("KB_MILVUS_COLLECTION", "map_kb"),
                        dim=int(os.environ.get("KB_EMBED_DIM", "1024")),
                        overwrite=False,
                    )
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
                    self._initialized = True
                    return True
                except Exception as e:
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
        """
        if not query:
            return {'success': False, 'error': '搜索关键词不能为空'}

        if self._ensure_initialized():
            try:
                # 需要过滤项目时适当多召回，过滤后再精排截断到 top_k
                fetch_k = max(top_k * 5, 30) if project else max(top_k * 2, 12)
                nodes = self._retrieve_hybrid(query, fetch_k)

                if project:
                    nodes = [n for n in nodes if (n.metadata or {}).get('project') == project]

                results = []
                for node in nodes:
                    results.append({
                        'id': node.node_id,
                        'document_id': node.metadata.get('document_id', ''),
                        'title': node.metadata.get('title', '未命名文档'),
                        'content': node.text,
                        'relevance': round(node.score or 0, 4),
                        'metadata': node.metadata,
                    })

                # 精排截断：rerank 失败/关闭时保持融合原序
                if len(results) > top_k:
                    reranked = self._rerank(query, results, top_k)
                    results = reranked if reranked is not None else results[:top_k]

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
        """BM25 检索器（jieba 分词），按文档数缓存，增删文档后自动重建；不可用返回 None。"""
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

    def _retrieve_hybrid(self, query: str, fetch_k: int):
        """向量 + BM25(jieba) RRF 融合召回；BM25 不可用时降级纯向量。

        不用 QueryFusionRetriever：其构造会 resolve Settings.llm（默认 OpenAI），
        无 OPENAI_API_KEY 时报错退化。RRF 即其 SIMPLE 融合模式的标准算法。
        """
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

        # Reciprocal Rank Fusion（k=60，与 QueryFusionRetriever SIMPLE 模式一致）
        rrf_k = 60
        scores: Dict[str, float] = {}
        nodes_by_id: Dict[str, Any] = {}
        for nodes in (vec_nodes, bm_nodes):
            for rank, node in enumerate(nodes):
                nid = node.node_id
                scores[nid] = scores.get(nid, 0.0) + 1.0 / (rrf_k + rank + 1)
                if nid not in nodes_by_id:
                    nodes_by_id[nid] = node
        ranked = sorted(nodes_by_id.values(), key=lambda n: scores.get(n.node_id, 0.0), reverse=True)
        return ranked[:fetch_k]

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
                try:
                    self._index.delete_ref_doc(same_name["doc_id"], delete_from_docstore=True)
                except Exception:
                    pass
                self._kb_delete(same_name["doc_id"])
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
            self._index.storage_context.persist(persist_dir=self._persist_dir)
            self._kb_record(doc_id, name, content_hash, chunk_count=1, source="add_document")

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

    def _delete_document(self, document_id: str) -> Dict[str, Any]:
        """从本地 LlamaIndex 删除文档"""
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        if not self._ensure_initialized():
            return self._ragflow_fallback_delete_document(document_id)

        try:
            self._index.delete_ref_doc(document_id, delete_from_docstore=True)
            self._index.storage_context.persist(persist_dir=self._persist_dir)
            self._kb_delete(document_id)

            logger.info(f"[LlamaIndex] 删除文档: {document_id}")
            return {
                'success': True,
                'document_id': document_id,
                'message': '文档已从 LlamaIndex 中删除'
            }
        except Exception as e:
            logger.error(f"[LlamaIndex] 删除文档失败: {e}")
            return self._ragflow_fallback_delete_document(document_id)

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

    def _kb_lookup(self, field: str, value: str) -> Optional[Dict[str, Any]]:
        """按字段查 kb_documents 清单；表不存在时自动建表。"""
        import sqlite3
        try:
            with sqlite3.connect(self._kb_db_path()) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS kb_documents (
                        doc_id TEXT PRIMARY KEY,
                        name TEXT,
                        content_hash TEXT,
                        chunk_count INTEGER DEFAULT 0,
                        source TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_documents_hash ON kb_documents(content_hash)")
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    f"SELECT * FROM kb_documents WHERE {field}=? LIMIT 1", (value,)
                ).fetchone()
                conn.commit()
                return dict(row) if row else None
        except Exception as e:
            logger.warning(f"[LlamaIndex] kb_documents 查询失败: {e}")
            return None

    def _kb_record(self, doc_id: str, name: str, content_hash: str,
                   chunk_count: int, source: str) -> None:
        import sqlite3
        from datetime import datetime as _dt
        now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with sqlite3.connect(self._kb_db_path()) as conn:
                conn.execute(
                    "INSERT INTO kb_documents (doc_id, name, content_hash, chunk_count, source, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET name=excluded.name, content_hash=excluded.content_hash, "
                    "chunk_count=excluded.chunk_count, source=excluded.source, updated_at=excluded.updated_at",
                    (doc_id, name, content_hash, chunk_count, source, now, now),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[LlamaIndex] kb_documents 写入失败: {e}")

    def _kb_delete(self, doc_id: str) -> None:
        import sqlite3
        try:
            with sqlite3.connect(self._kb_db_path()) as conn:
                conn.execute("DELETE FROM kb_documents WHERE doc_id=?", (doc_id,))
                conn.commit()
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
