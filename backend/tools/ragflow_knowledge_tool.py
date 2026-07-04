#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RagFlow 知识库工具
替换原有的 Dify 知识库实现，接入 RagFlow 框架进行知识检索和管理。
"""

import json
import logging
import os
import requests
from typing import Dict, List, Any, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool('knowledge_base_tool')
class KnowledgeBaseTool(BaseTool):
    """
    知识库管理工具，接入 RagFlow 知识库框架，支持对业务文档进行检索和管理。
    
    支持的操作：
    1. search: 从 RagFlow 知识库中检索相关信息
    2. list_topics: 查看当前知识库中的文档列表
    3. get_content: 查看特定文档的具体内容
    """

    description = '''
        知识库管理工具，用于检索和维护业务知识（基于 RagFlow 知识库框架）。
        你可以使用此工具：
        1. 搜索知识 (search)：从 RagFlow 知识库中检索相关信息。
        2. 列出主题 (list_topics)：查看当前知识库中的文档列表。
        3. 获取内容 (get_content)：查看特定文档的具体内容。
        4. 添加文档 (add_document)：向知识库中添加新的文本文档。
        5. 删除文档 (delete_document)：从知识库中删除指定文档。
        6. 上传文件 (upload_file)：上传 PDF/Word/TXT 文件到知识库。
        '''
    
    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '操作类型',
            'enum': ['search', 'list_topics', 'get_content', 'add_document', 'delete_document', 'upload_file'],
            'required': True
        },
        {
            'name': 'query',
            'type': 'string',
            'description': '搜索关键词（仅 search 时需要）',
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
        # RagFlow 配置
        self.api_key = os.environ.get(
            'RAGFLOW_API_KEY', 
            'ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0'
        )
        self.api_base = os.environ.get(
            'RAGFLOW_API_BASE', 
            'http://172.136.16.14:8080/api/v1'
        )
        # 数据集 ID（信阳市智慧巡河报告）
        self.dataset_id = os.environ.get(
            'RAGFLOW_DATASET_ID', 
            '538b0a5c36ff11f18e7d3d43671e73e4'
        )

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """执行工具调用"""
        if isinstance(params, str):
            params = json.loads(params)

        operation = params.get('operation')
        
        if operation == 'search':
            return self._search(
                query=params.get('query', ''),
                top_k=params.get('top_k', 8)
            )
        elif operation == 'list_topics':
            return self._list_topics()
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

    def _search(self, query: str, top_k: int = 8) -> Dict[str, Any]:
        """
        从 RagFlow 知识库检索信息
        
        使用 RagFlow 的检索 API，支持向量检索和混合检索。
        """
        try:
            url = f"{self.api_base}/retrieval"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "question": query,
                "dataset_ids": [self.dataset_id],
                "top_k": top_k,
                "similarity_threshold": 0.3,
                "vector_similarity_weight": 0.5,
                "keyword": False,
                "use_kg": False
            }
            
            response = requests.post(
                url, 
                headers=headers, 
                json=payload, 
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            
            # 检查返回码
            if data.get("code") != 0:
                logger.error(f"RagFlow retrieval error: {data.get('message')}")
                return {
                    'success': False,
                    'error': f"RagFlow 检索失败: {data.get('message', '未知错误')}"
                }
            
            # 解析结果
            results = []
            chunks = data.get("data", {}).get("chunks", [])
            
            for chunk in chunks:
                results.append({
                    'id': chunk.get("chunk_id", ""),
                    'document_id': chunk.get("document_id", ""),
                    'title': chunk.get("docnm_kwd", "未命名文档"),
                    'content': chunk.get("content", ""),
                    'relevance': chunk.get("similarity", 0),
                    'metadata': chunk.get("meta_fields", {})
                })
            
            return {
                'success': True,
                'data': results,
                'count': len(results),
                'method': 'RagFlow Retrieval'
            }
            
        except requests.HTTPError as he:
            status = getattr(he.response, 'status_code', None)
            body = ""
            try:
                body = he.response.text
            except:
                body = str(he)
            
            logger.error(f"RagFlow search failed: HTTP {status}, body={body}")
            return {
                'success': False,
                'status_code': status,
                'error': f"RagFlow 检索失败: HTTP {status}. {body}"
            }
        except Exception as e:
            logger.error(f"RagFlow search failed: {e}")
            return {
                'success': False,
                'error': f"RagFlow 检索失败: {str(e)}"
            }

    def _list_topics(self) -> Dict[str, Any]:
        """
        获取 RagFlow 知识库文档列表
        
        列出数据集中所有已上传的文档。
        """
        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            # 分页获取所有文档
            all_docs = []
            page = 1
            page_size = 100
            
            while True:
                params = {
                    "page": page,
                    "page_size": page_size,
                    "orderby": "create_time",
                    "desc": "true"
                }
                
                response = requests.get(
                    url, 
                    headers=headers, 
                    params=params, 
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                
                if data.get("code") != 0:
                    logger.error(f"RagFlow list documents error: {data.get('message')}")
                    return {
                        'success': False,
                        'error': f"RagFlow 获取文档列表失败: {data.get('message')}"
                    }
                
                docs = data.get("data", {}).get("docs", [])
                all_docs.extend(docs)
                
                # 检查是否还有更多
                total = data.get("data", {}).get("total", 0)
                if len(all_docs) >= total or not docs:
                    break
                
                page += 1
            
            # 格式化输出
            topics = []
            for doc in all_docs:
                topics.append({
                    "id": doc.get("id"),
                    "name": doc.get("name"),
                    "status": doc.get("run", "unknown"),
                    "created_at": doc.get("create_time", ""),
                    "size": doc.get("size", 0)
                })
            
            return {
                'success': True,
                'data': topics,
                'total': len(topics)
            }
            
        except Exception as e:
            logger.error(f"RagFlow list documents failed: {e}")
            return {
                'success': False,
                'error': f"RagFlow 获取文档列表失败: {str(e)}"
            }

    def _get_content(self, document_id: str) -> Dict[str, Any]:
        """
        获取文档具体内容
        
        通过 RagFlow API 获取文档的所有 chunk 内容。
        """
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        try:
            # RagFlow 的正确 API 路径
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents/{document_id}/chunks"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            # 分页获取所有 chunks
            all_chunks = []
            page = 1
            page_size = 100
            
            while True:
                params = {
                    "page": page,
                    "page_size": page_size
                }
                
                response = requests.get(
                    url, 
                    headers=headers, 
                    params=params, 
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                
                if data.get("code") != 0:
                    logger.error(f"RagFlow get chunks error: {data.get('message')}")
                    return {
                        'success': False,
                        'error': f"RagFlow 获取文档内容失败: {data.get('message')}"
                    }
                
                chunks = data.get("data", {}).get("chunks", [])
                all_chunks.extend(chunks)
                
                # 检查是否还有更多
                total = data.get("data", {}).get("total", 0)
                if len(all_chunks) >= total or not chunks:
                    break
                
                page += 1
            
            # 拼接内容
            full_content = "\n\n".join([
                chunk.get("content", "") 
                for chunk in all_chunks 
                if chunk.get("content")
            ])
            
            if not full_content:
                return {
                    'success': False,
                    'error': '文档内容为空，可能尚未解析完成'
                }
            
            return {
                'success': True,
                'content': full_content,
                'document_id': document_id,
                'chunk_count': len(all_chunks)
            }
            
        except Exception as e:
            logger.error(f"RagFlow get content failed: {e}")
            return {
                'success': False,
                'error': f"获取文档内容失败: {str(e)}"
            }

    def _add_document(self, name: str, content: str) -> Dict[str, Any]:
        """
        向 RagFlow 知识库添加文本文档
        
        POST /api/v1/datasets/{dataset_id}/documents
        """
        if not name or not content:
            return {'success': False, 'error': '文档名称和内容不能为空'}

        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "name": name,
                "content": content,
                "parser_method": "manual"
            }
            
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") != 0:
                logger.error(f"RagFlow add document error: {data.get('message')}")
                return {
                    'success': False,
                    'error': f"RagFlow 添加文档失败: {data.get('message', '未知错误')}"
                }
            
            doc_info = data.get("data", {})
            # RagFlow may return data as a list; extract first element
            if isinstance(doc_info, list) and len(doc_info) > 0:
                doc_info = doc_info[0]
            elif not isinstance(doc_info, dict):
                doc_info = {}
            return {
                'success': True,
                'document_id': doc_info.get("id", ""),
                'name': name,
                'message': f"文档 '{name}' 已提交到 RagFlow，稍后完成解析"
            }
            
        except Exception as e:
            logger.error(f"RagFlow add document failed: {e}")
            return {
                'success': False,
                'error': f"添加文档失败: {str(e)}"
            }

    def _delete_document(self, document_id: str) -> Dict[str, Any]:
        """
        从 RagFlow 知识库删除文档
        
        DELETE /api/v1/datasets/{dataset_id}/documents
        """
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {"ids": [document_id]}
            
            response = requests.delete(
                url,
                headers=headers,
                json=payload,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") != 0:
                logger.error(f"RagFlow delete document error: {data.get('message')}")
                return {
                    'success': False,
                    'error': f"RagFlow 删除文档失败: {data.get('message', '未知错误')}"
                }
            
            return {
                'success': True,
                'document_id': document_id,
                'message': '文档已从 RagFlow 中删除'
            }
            
        except Exception as e:
            logger.error(f"RagFlow delete document failed: {e}")
            return {
                'success': False,
                'error': f"删除文档失败: {str(e)}"
            }

    def _upload_file(self, file_path: str) -> Dict[str, Any]:
        """
        上传文件到 RagFlow 知识库
        
        POST /api/v1/datasets/{dataset_id}/documents (multipart/form-data)
        """
        import os as _os
        
        if not file_path or not _os.path.isfile(file_path):
            return {'success': False, 'error': f'文件不存在: {file_path}'}

        file_name = _os.path.basename(file_path)
        
        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            
            with open(file_path, 'rb') as f:
                files = {
                    'file': (file_name, f, 'application/octet-stream')
                }
                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    timeout=60
                )
            
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") != 0:
                logger.error(f"RagFlow upload file error: {data.get('message')}")
                return {
                    'success': False,
                    'error': f"RagFlow 上传文件失败: {data.get('message', '未知错误')}"
                }
            
            doc_info = data.get("data", {}) or {}
            # RagFlow may return data as a list; extract first element
            if isinstance(doc_info, list) and len(doc_info) > 0:
                doc_info = doc_info[0]
            elif not isinstance(doc_info, dict):
                doc_info = {}
            return {
                'success': True,
                'document_id': doc_info.get("id", ""),
                'name': file_name,
                'message': f"文件 '{file_name}' 已上传到 RagFlow，稍后完成解析"
            }
            
        except Exception as e:
            logger.error(f"RagFlow upload file failed: {e}")
            return {
                'success': False,
                'error': f"上传文件失败: {str(e)}"
            }
