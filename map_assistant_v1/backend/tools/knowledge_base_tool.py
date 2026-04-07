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
    知识库管理工具，外挂 Dify 知识库，支持对业务文档进行检索和管理。
    """

    description = '''
        知识库管理工具，用于检索和维护业务知识（基于 Dify 外挂知识库）。
        你可以使用此工具：
        1. 搜索知识 (search)：从 Dify 知识库中检索相关信息。
        2. 添加知识 (add)：向 Dify 知识库中存入新的文档。
        3. 列出主题 (list_topics)：查看当前知识库中的文档列表。
        4. 获取内容 (get_content)：查看特定文档的具体内容。
        '''
    parameters = [
        {
            'name': 'operation',
            'type': 'string',
            'description': '操作类型',
            'enum': ['search', 'add', 'list_topics', 'get_content'],
            'required': True
        },
        {
            'name': 'query',
            'type': 'string',
            'description': '搜索关键词（仅 search 时需要）',
            'required': False
        },
        {
            'name': 'title',
            'type': 'string',
            'description': '知识标题（仅 add 时需要）',
            'required': False
        },
        {
            'name': 'content',
            'type': 'string',
            'description': '知识内容（仅 add 时需要）',
            'required': False
        },
        {
            'name': 'document_id',
            'type': 'string',
            'description': '文档 ID（仅 get_content 时需要）',
            'required': False
        }
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        # Dify 相关配置
        self.api_key = os.environ.get('DIFY_KNOWLEDGE_API_KEY', 'dataset-ihY2ckUpZezVmydpw8Tix4l1')
        self.api_base = os.environ.get('DIFY_API_BASE', 'http://172.136.16.52:83/v1')
        self.dataset_id = os.environ.get('DIFY_DATASET_ID', '5ec0a57c-21be-43b4-8cd3-c34f657c3efe')

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if isinstance(params, str):
            params = json.loads(params)

        operation = params.get('operation')
        if operation == 'search':
            return self._search(params.get('query', ''))
        elif operation == 'add':
            return self._add(params.get('title', ''), params.get('content', ''))
        elif operation == 'list_topics':
            return self._list_topics()
        elif operation == 'get_content':
            return self._get_content(params.get('document_id', ''))
        else:
            return {'success': False, 'error': f'不支持的操作: {operation}'}

    def _search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """调用 Dify 检索接口"""
        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/retrieve"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            payload_primary = {
                "query": query,
                # Dify 接口参数调整：暂时移除 retrieval_model 以避免 500 错误
                # "retrieval_model": {
                #     "search_method": "hybrid_search",
                #     "reranking_enable": True,
                #     "top_k": limit
                # }
            }

            response = requests.post(url, headers=headers, json=payload_primary, timeout=10)
            response.raise_for_status()
            data = response.json()

            results = []
            for record in data.get('records', []):
                segment = record.get('segment', {})
                results.append({
                    'id': segment.get('id'),
                    'title': segment.get('document', {}).get('name', '未命名文档'),
                    'content': segment.get('content', ''),
                    'relevance': record.get('score', 0)
                })

            return {
                'success': True,
                'data': results,
                'count': len(results),
                'method': 'Dify Dataset Retrieval'
            }
        except requests.HTTPError as he:
            status = getattr(he.response, 'status_code', None)
            body = None
            try:
                body = he.response.text
            except:
                body = str(he)
            logger.error(f"Dify search failed: HTTP {status}, body={body}")
            # 二次降级尝试：不带 retrieval_model 的最简请求
            try:
                url = f"{self.api_base}/datasets/{self.dataset_id}/retrieve"
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                payload_fallback = {"query": query}
                resp2 = requests.post(url, headers=headers, json=payload_fallback, timeout=10)
                resp2.raise_for_status()
                data2 = resp2.json()
                results2 = []
                for record in data2.get('records', []):
                    segment = record.get('segment', {})
                    results2.append({
                        'id': segment.get('id'),
                        'title': segment.get('document', {}).get('name', '未命名文档'),
                        'content': segment.get('content', ''),
                        'relevance': record.get('score', 0)
                    })
                return {
                    'success': True,
                    'data': results2,
                    'count': len(results2),
                    'method': 'Dify Dataset Retrieval (fallback)'
                }
            except Exception as e2:
                logger.error(f"Dify fallback search failed: {e2}")
                return {
                    'success': False,
                    'status_code': status,
                    'error': f"Dify 检索失败: HTTP {status}. {body}"
                }
        except Exception as e:
            logger.error(f"Dify search failed: {e}")
            return {'success': False, 'error': f"Dify 检索失败: {str(e)}"}

    def _add(self, title: str, content: str) -> Dict[str, Any]:
        """调用 Dify 创建文档接口"""
        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/document/create-by-text"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "name": title,
                "text": content,
                "indexing_technique": "high_quality",
                "process_rule": {
                    "mode": "automatic"
                }
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            return {
                'success': True,
                'document_id': data.get('document', {}).get('id'),
                'message': f'知识点 "{title}" 已成功添加到 Dify 知识库'
            }
        except Exception as e:
            logger.error(f"Dify add document failed: {e}")
            return {'success': False, 'error': f"Dify 添加文档失败: {str(e)}"}

    def _list_topics(self) -> Dict[str, Any]:
        """获取 Dify 知识库文档列表"""
        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            params = {
                "page": 1,
                "limit": 50
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            topics = [{"id": doc.get('id'), "name": doc.get('name')} for doc in data.get('data', [])]
            return {
                'success': True,
                'data': topics
            }
        except Exception as e:
            logger.error(f"Dify list documents failed: {e}")
            return {'success': False, 'error': f"Dify 获取列表失败: {str(e)}"}

    def _get_content(self, document_id: str) -> Dict[str, Any]:
        """获取文档具体内容（通过获取所有分段）"""
        if not document_id:
            return {'success': False, 'error': '未提供文档 ID'}

        try:
            url = f"{self.api_base}/datasets/{self.dataset_id}/documents/{document_id}/segments"
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }
            segments = []
            page = 1
            limit = 100

            while True:
                params = {
                    "status": "completed",
                    "page": page,
                    "limit": limit
                }
                response = requests.get(url, headers=headers, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                batch = data.get('data', [])
                segments.extend(batch)
                has_more = data.get('has_more')
                if has_more is False or not batch or (has_more is None and len(batch) < limit):
                    break
                page += 1

            segments.sort(key=lambda x: x.get('position', 0))
            full_content = "\n".join([seg.get('content', '') for seg in segments])

            return {
                'success': True,
                'content': full_content,
                'document_id': document_id
            }
        except Exception as e:
            logger.error(f"Dify get segments failed: {e}")
            return {'success': False, 'error': f"获取文档内容失败: {str(e)}"}
