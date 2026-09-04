import json
import logging
import os
from typing import Dict, Any, List, Union

import httpx
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool("web_search_tool")
class WebSearchTool(BaseTool):
    description = """
联网搜索工具：接入阿里云百炼服务端联网搜索，获取互联网实时信息。
适用于：新闻热点、时效性问题（"今天/最新/现在"）、天气兜底、价格行情、
知识库与数据库覆盖不到的常识问题。
入参：query（必填，完整的自然语言搜索问题）。
返回：answer（总结）、search_results（标题/链接列表）。
"""

    parameters = [
        {
            "name": "query",
            "type": "string",
            "description": "搜索查询语句，用完整的中文自然语言描述，如 '郑州市今天的天气'、'河南省2026年采砂管理最新政策'",
            "required": True,
        },
        {
            "name": "max_results",
            "type": "integer",
            "description": "返回的搜索结果条数上限，默认 5",
            "required": False,
            "default": 5,
        },
    ]

    DEFAULT_MODEL = "qwen-flash"
    TIMEOUT = 60

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        query = str(params.get("query", "")).strip()
        max_results = int(params.get("max_results", 5) or 5)
        if not query:
            return {"success": False, "error": "搜索内容不能为空"}

        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            return {"success": False, "error": "DASHSCOPE_API_KEY 未配置，无法联网搜索"}

        base_url = os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
        model = os.environ.get("WEB_SEARCH_MODEL", self.DEFAULT_MODEL)

        headers = {"Authorization": f"Bearer {api_key}"}

        # 首选 Responses API：服务端 web_search 工具，返回带 URL 的来源明细
        try:
            with httpx.Client(timeout=self.TIMEOUT) as client:
                r = client.post(
                    f"{base_url}/responses",
                    headers=headers,
                    json={
                        "model": model,
                        "input": query,
                        "tools": [{"type": "web_search"}],
                    },
                )
            if r.status_code == 200:
                return self._parse_responses_api(r.json(), model, query, max_results)
            logger.warning(f"Responses API returned {r.status_code}, falling back to enable_search")
        except Exception as e:
            logger.warning(f"Responses API failed, falling back to enable_search: {e}")

        # 兜底：Chat Completions + enable_search 插件（答案可靠，但无来源明细）
        try:
            with httpx.Client(timeout=self.TIMEOUT) as client:
                r = client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": query}],
                        "enable_search": True,
                        "search_options": {"forced_search": True},
                    },
                )
            if r.status_code != 200:
                return {"success": False, "error": f"搜索服务返回 {r.status_code}: {r.text[:200]}"}
            data = r.json()
            answer = data["choices"][0]["message"]["content"] or ""
            return {
                "success": True,
                "provider": "dashscope-enable-search",
                "model": model,
                "query": query,
                "answer": answer,
                "search_results": [],
                "total_results": 0,
            }
        except Exception as e:
            logger.warning(f"Web search request failed: {e}")
            return {"success": False, "error": f"联网搜索请求失败: {e}"}

    def _parse_responses_api(self, data: Dict[str, Any], model: str, query: str, max_results: int) -> Dict[str, Any]:
        answer_parts: List[str] = []
        sources: List[Dict[str, str]] = []
        seen_urls = set()

        for item in data.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "web_search_call":
                for src in (item.get("action") or {}).get("sources") or []:
                    if isinstance(src, dict) and src.get("url"):
                        url = str(src["url"])
                        if url not in seen_urls:
                            seen_urls.add(url)
                            sources.append({"title": str(src.get("title") or ""), "url": url})
            elif item_type == "message":
                for c in item.get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        if c.get("text"):
                            answer_parts.append(c["text"])
                        for ann in c.get("annotations") or []:
                            if isinstance(ann, dict) and ann.get("url") and ann["url"] not in seen_urls:
                                seen_urls.add(ann["url"])
                                sources.append({
                                    "title": str(ann.get("title") or ""),
                                    "url": str(ann["url"]),
                                })

        search_results = sources[:max_results]
        for sr in search_results:
            sr["snippet"] = ""
        return {
            "success": True,
            "provider": "dashscope-responses-web-search",
            "model": model,
            "query": query,
            "answer": "\n".join(answer_parts),
            "search_results": search_results,
            "total_results": len(search_results),
        }
