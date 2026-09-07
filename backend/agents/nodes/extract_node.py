"""提取节点：工具结果提取/摘要/来源标注/参数归一化（自 task_executor.py 机械搬移，方法体逐字保留）。
P1 拆分：以 Mixin 形式挂载到 TaskExecutor，self 语义与运行时行为完全不变。
"""
import asyncio
import httpx
import json
import logging
import operator
import os
import re
from typing import Annotated, List, Dict, Any, Optional, TypedDict, AsyncGenerator
from urllib.parse import quote

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from ..intent_types import IntentType, IntentResult, TaskStep
from ..qgis_workflows import match_recipe, extract_params, substitute_params, RECIPES
from ..state import AgentState, QwenToolAdapter

logger = logging.getLogger(__name__)


class ExtractNodeMixin:
    def _extract_node(self, state: AgentState) -> AgentState:
        """节点3a：从 tool_results 中提取结构化输出（map_commands / cesium_commands / charts / report_url），
        并构建 tool_summaries 供后续 summarize_node 使用。

        本节点不调用 LLM，仅做纯数据提取和整理。
        """
        intent_result: IntentResult = state["intent_result"]
        tool_results: List[Dict] = state.get("tool_results", [])

        # 从工具结果中提取各类命令
        map_commands: List[Dict] = []
        cesium_commands: List[Dict] = []
        charts: List[Dict] = []
        report_url: Optional[str] = None
        tool_summaries: List[str] = []

        for item in tool_results:
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})

            # map_command
            if result.get("map_command"):
                map_commands.append(result["map_command"])

            # cesium_command
            if result.get("cesium_command"):
                cesium_commands.append(result["cesium_command"])

            # 图表
            if tool_name == "data_visualizer_tool" and result.get("success"):
                charts.append({
                    "chart_type": result.get("chart_type"),
                    "config": result.get("config"),
                    "summary": result.get("content"),
                })

            # 报告（通用报告工具 / 采砂监测报告工具）
            if tool_name in ("report_generator_tool", "caisha_report_tool") and result.get("success"):
                report_url = result.get("report_url") or result.get("download_url")
                logger.info(f"[extract_node] Report generated: report_url={report_url} (from tool_name={tool_name}, result keys={list(result.keys())})")

            # 收集摘要，供 LLM 生成文本回答
            content = result.get("content") or result.get("message") or result.get("error", "")
            data = result.get("data")

            # 知识库检索工具：将 data（chunks 列表）转为可读摘要
            if tool_name == "knowledge_base_tool" and result.get("success"):
                kb_summary_parts = []
                kb_count = result.get("count", 0)
                if isinstance(data, list) and data:
                    kb_summary_parts.append(f"知识库检索到 {kb_count} 条相关内容：")
                    for i, chunk in enumerate(data[:5]):
                        chunk_title = chunk.get("title", "")
                        chunk_content = (chunk.get("content") or "")[:300]
                        chunk_relevance = chunk.get("relevance", 0)
                        kb_summary_parts.append(
                            f"  [{i+1}] {chunk_title} (相关度:{chunk_relevance:.2f})\n     {chunk_content}"
                        )
                    summary = "\n".join(kb_summary_parts)
                elif content:
                    summary = f"[{tool_name}]: {str(content)[:500]}"
                else:
                    summary = f"[{tool_name}]: 检索完成，但未找到相关内容"
                tool_summaries.append(summary)
            elif tool_name == "weather_tool" and result.get("success"):
                # 天气查询：qweather 走 current/forecasts/aqi；web-search 降级走 answer+来源
                w_parts = [f"城市：{result.get('city', '未知')}"]
                answer = str(result.get("answer") or "").strip()
                if answer:
                    w_parts.append(f"天气实况（联网搜索）：{answer[:1200]}")
                    for i, sr in enumerate((result.get("search_results") or [])[:5]):
                        w_parts.append(f"  [{i+1}] {sr.get('title') or sr.get('url', '')}")
                else:
                    current = result.get("current") or {}
                    if current:
                        w_parts.append(
                            f"当前天气：{current.get('weather', '未知')}，气温 {current.get('temp', '?')}℃"
                            f"（体感 {current.get('feels_like', '?')}℃），湿度 {current.get('humidity', '?')}%，"
                            f"{current.get('wind_direction', '')}风 {current.get('wind_scale', '')} 级"
                        )
                    forecasts = result.get("forecasts") or []
                    if forecasts:
                        w_parts.append("未来预报：")
                        for d in forecasts[:7]:
                            w_parts.append(
                                f"  {d.get('date', '')}: {d.get('text_day', '')}转{d.get('text_night', '')}，"
                                f"{d.get('temp_min', '?')}~{d.get('temp_max', '?')}℃，降水 {d.get('precip', '?')}mm"
                            )
                    aqi = result.get("aqi")
                    if isinstance(aqi, dict) and aqi:
                        w_parts.append(f"空气质量：AQI {aqi.get('aqi', '?')}（{aqi.get('category', '?')}），PM2.5 {aqi.get('pm2p5', '?')}")
                    if result.get("source") == "mock":
                        w_parts.append("（注意：以上为模拟数据，真实天气服务不可用）")
                tool_summaries.append(f"[{tool_name}]:\n" + "\n".join(w_parts))
            elif tool_name == "web_search_tool" and result.get("success"):
                # 联网搜索：answer 已含引用标记的总结，附上来源列表供标注出处
                ws_parts = [f"搜索问题：{result.get('query', '')}"]
                answer = str(result.get("answer") or "").strip()
                if answer:
                    ws_parts.append(f"搜索总结：{answer[:1500]}")
                for i, sr in enumerate((result.get("search_results") or [])[:5]):
                    ws_parts.append(f"  [{i+1}] {sr.get('title', '')} ({sr.get('url', '')})")
                tool_summaries.append(f"[{tool_name}]:\n" + "\n".join(ws_parts))
            elif content or (isinstance(data, list) and data):
                summary = f"[{tool_name}]: {str(content)[:500]}"
                # 对数据库查询，把 data 的 JSON 摘要也加进来
                if tool_name in ("postgresql_tool", "mcp_postgres_tool") and result.get("success"):
                    if data is not None:
                        data_text = json.dumps(data[:10], ensure_ascii=False, default=str)
                        summary += f"\n数据结果（前 10 条）: {data_text[:800]}"
                tool_summaries.append(summary)

        # 当地图操作意图但无工具被调用（execution_plan 中 tool 为空），不应让 LLM 凭空回答
        if not tool_summaries and intent_result.primary_intent in (
            IntentType.MAP_DISPLAY, IntentType.LOCATION_SEARCH, IntentType.COORDINATE_MARKER
        ):
            all_steps = intent_result.execution_plan
            if all_steps:
                logger.warning(
                    f"[extract_node] Map intent '{intent_result.primary_intent}' "
                    f"has {len(all_steps)} steps but no tool results. "
                    f"Possible cause: execution_plan steps have tool=None."
                )
                # 用实际生成的地图命令构造回复，避免所有地图操作都返回同一句模板
                # （前端按内容去重，固定模板会被当成重复消息丢弃，导致用户看不到任何回复）
                if map_commands:
                    loaded = "、".join(
                        f"「{cmd.get('name') or cmd.get('type', '图层')}」" for cmd in map_commands
                    )
                    state["response"] = f"地图指令已执行，已加载图层 {loaded}。"
                else:
                    state["response"] = "地图操作指令已解析，正在执行中。"
            else:
                state["response"] = "已接收到地图操作请求，但未生成执行步骤，请重新描述需求。"
            state["map_commands"] = map_commands
            state["cesium_commands"] = cesium_commands
            state["charts"] = charts
            state["report_url"] = report_url
            state["tool_summaries"] = tool_summaries
            return state

        # ── 知识库回退检测 + 补偿检索（响应阶段兜底）──
        has_kb_results = any(
            item.get("tool_name") == "knowledge_base_tool"
            and isinstance(item.get("result"), dict)
            and item["result"].get("success")
            and item["result"].get("count", 0) > 0
            for item in tool_results
        )
        db_tool_names = ("postgresql_tool", "mcp_postgres_tool")
        has_db = any(item.get("tool_name") in db_tool_names for item in tool_results)

        if has_db and not has_kb_results:
            need_fallback = False
            for item in tool_results:
                if item.get("tool_name") not in db_tool_names:
                    continue
                r = item.get("result", {})
                if isinstance(r, dict):
                    data = r.get("data")
                    if data is None or (isinstance(data, list) and len(data) == 0):
                        need_fallback = True
                        break
                    if data is not None and (isinstance(data, list) and data):
                        if self._is_empty_aggregate(data):
                            need_fallback = True
                            break
                        for row in data:
                            if isinstance(row, dict):
                                for v in row.values():
                                    if isinstance(v, (int, float)) and v > 100:
                                        logger.warning(
                                            f"[extract_node] Suspicious large count {v}, "
                                            f"likely COUNT(*) instead of COUNT(DISTINCT)"
                                        )
                                        need_fallback = True
                                        break
                            if need_fallback:
                                break
            if need_fallback:
                logger.warning("[extract_node] DB result unreliable — running sync fallback")
                kb_summary = self._sync_kb_search(state["user_message"])
                if kb_summary:
                    tool_summaries = [
                        s for s in tool_summaries
                        if not s.startswith("[postgresql_tool]") and not s.startswith("[mcp_postgres_tool]")
                    ]
                    tool_summaries.insert(0, kb_summary)

        elif has_db and has_kb_results:
            tool_summaries = [
                s for s in tool_summaries
                if not s.startswith("[postgresql_tool]") and not s.startswith("[mcp_postgres_tool]")
            ]
            tool_summaries.insert(0, (
                "【重要】数据库中没有该地区的砂场数据（数据库仅覆盖固始县），"
                "请完全依据以下知识库检索结果回答用户问题，不要提及数据库结果。"
            ))

        state["map_commands"] = map_commands
        state["cesium_commands"] = cesium_commands
        state["charts"] = charts
        state["report_url"] = report_url
        state["tool_summaries"] = tool_summaries
        return state

    def _try_extract_rich_response(self, tool_results: List[Dict]) -> Optional[str]:
        """如果工具已产出高质量摘要，返回可直接用作 response 的文本，否则返回 None。"""
        parts: List[str] = []
        for item in tool_results:
            tool_name = item.get("tool_name", "")
            result = item.get("result", {})
            if not isinstance(result, dict):
                continue
            # data_visualizer_tool 返回的 content 通常是完整的 Markdown 摘要（含表格+分析）
            if tool_name == "data_visualizer_tool" and result.get("success"):
                content = result.get("content") or ""
                if len(content) > 50:
                    parts.append(content)
            # report_generator_tool 成功时简单告知
            if tool_name == "report_generator_tool" and result.get("success"):
                url = result.get("report_url") or result.get("download_url") or ""
                msg = result.get("message") or "报告已生成"
                parts.append(f"{msg}。" + (f"下载地址：{url}" if url else ""))
            # spatial_processing_tool 成功时直接使用其 message
            if tool_name == "spatial_processing_tool" and result.get("success"):
                msg = result.get("message") or ""
                if msg:
                    parts.append(msg)
            # qgis_mcp_tool 缓冲区工作流成功时直接使用其 message
            if tool_name == "qgis_mcp_tool" and result.get("success"):
                msg = result.get("message") or ""
                if msg:
                    parts.append(msg)
        # 当 data_visualizer_tool 已提供完整分析时，postgresql_tool 的结果也视为已覆盖
        # （因为 viz tool 的 SQL 查询结果已包含数据和分析）
        covered_tools = {"data_visualizer_tool", "report_generator_tool", "spatial_processing_tool", "qgis_mcp_tool"}
        has_viz = any(
            item.get("tool_name") == "data_visualizer_tool"
            and isinstance(item.get("result"), dict)
            and item["result"].get("success")
            for item in tool_results
        )
        if has_viz:
            covered_tools.add("postgresql_tool")
        uncovered = [
            item for item in tool_results
            if item.get("tool_name") not in covered_tools
            and isinstance(item.get("result"), dict)
            and item["result"].get("success") is not False
            and (item["result"].get("content") or item["result"].get("message"))
        ]
        if parts and not uncovered:
            return "\n\n".join(parts)
        return None

    def _append_kb_sources(self, response_text: str, tool_results: List[Dict]) -> str:
        """知识库回答的来源标注兜底：提取 KB 结果中的文档名，去重后追加到回复末尾。

        LLM 已在正文中标注（出现任一文档名）则不重复追加；最多列 3 个，超出折叠计数。
        """
        titles: List[str] = []
        for item in tool_results:
            if item.get("tool_name") != "knowledge_base_tool":
                continue
            result = item.get("result", {})
            if not isinstance(result, dict) or not result.get("success"):
                continue
            data = result.get("data")
            if not isinstance(data, list):
                continue
            for chunk in data:
                if not isinstance(chunk, dict):
                    continue
                t = str(chunk.get("title") or "").strip()
                # 清洗内部文件名：去掉 section_5_0007_ 前缀与 .txt 等扩展名
                t = re.sub(r"^section_\d+_", "", t)
                t = re.sub(r"\.(txt|md|docx?|pdf)$", "", t, flags=re.IGNORECASE)
                if t and t != "未命名文档" and t not in titles:
                    titles.append(t)
        if not titles:
            return response_text
        if any(t in response_text for t in titles):
            return response_text
        shown = "、".join(f"《{t}》" for t in titles[:3])
        if len(titles) > 3:
            shown += f" 等 {len(titles)} 篇文档"
        return f"{response_text}\n\n📎 来源：{shown}"

    def _append_web_sources(self, response_text: str, tool_results: List[Dict]) -> str:
        """联网搜索回答的来源标注兜底：提取搜索结果标题+域名，去重后追加到回复末尾。

        LLM 已在正文中标注任一来源标题/域名则不重复追加；最多列 3 条。
        """
        sources: List[str] = []
        for item in tool_results:
            is_web = item.get("tool_name") == "web_search_tool" or (
                item.get("tool_name") == "weather_tool"
                and isinstance(item.get("result"), dict)
                and item["result"].get("provider") == "web-search"
            )
            if not is_web:
                continue
            result = item.get("result", {})
            if not isinstance(result, dict) or not result.get("success"):
                continue
            for sr in result.get("search_results") or []:
                if not isinstance(sr, dict):
                    continue
                title = str(sr.get("title") or "").strip()
                url = str(sr.get("url") or "").strip()
                if not title and not url:
                    continue
                domain = url.split("//", 1)[-1].split("/", 1)[0] if url else ""
                if title and domain:
                    label = f"{title}（{domain}）"
                else:
                    label = title or domain
                if label and label not in sources:
                    sources.append(label)
        if not sources:
            return response_text
        # 正文已标注任一来源（标题或域名出现）则不重复追加
        plain = [s.split("（")[0] for s in sources] + [
            s.split("（")[1].rstrip("）") for s in sources if "（" in s
        ]
        if any(p in response_text for p in plain if p):
            return response_text
        shown = "；".join(sources[:3])
        if len(sources) > 3:
            shown += f" 等 {len(sources)} 条来源"
        return f"{response_text}\n\n📎 网络来源：{shown}"

    def _summarize_tool_result(self, tool_name: str, result: Dict[str, Any]) -> str:
        readable_tool_name = self._humanize_tool_name(tool_name)

        if not isinstance(result, dict):
            return f"{readable_tool_name}已返回结果。"

        if result.get("success") is False:
            error = result.get("error") or result.get("message") or "未知错误"
            return f"{readable_tool_name}执行失败：{error}"

        if tool_name == "data_visualizer_tool":
            chart_type = result.get("chart_type") or "图表"
            return f"{readable_tool_name}已完成，生成结果类型：{chart_type}。"

        if tool_name == "report_generator_tool":
            return f"{readable_tool_name}已完成，报告文件已生成。"

        if result.get("map_command"):
            cmd_type = result["map_command"].get("type", "地图命令")
            return f"{readable_tool_name}已完成，生成地图动作：{cmd_type}。"

        if result.get("cesium_command"):
            cmd_type = result["cesium_command"].get("action") or result["cesium_command"].get("type", "三维命令")
            return f"{readable_tool_name}已完成，生成三维动作：{cmd_type}。"

        content = result.get("content") or result.get("message")
        if content:
            content = str(content).strip().replace("\n", " ")
            return f"{readable_tool_name}已完成：{content[:80]}"

        return f"{readable_tool_name}已完成。"

    # 天气关键词停用词：从用户消息中剥离后剩余部分视为城市名
    _WEATHER_CITY_STRIP_RE = re.compile(
        r"查询|查一下|查下|看看|帮我|麻烦|请问|今天|明天|后天|现在|当前|这几天|未来三天|未来七天"
        r"|的|天气|气温|温度|降雨|下雨|下雪|风力|风速|湿度|空气质量|AQI|雾霾"
        r"|怎么样|如何|预报|几度|带伞|紫外线|情况|冷吗|热吗|有雨吗"
    )

    @classmethod
    def _extract_city_from_message(cls, user_message: str) -> str:
        """从用户消息中兜底抽取城市名：剥离天气相关词后剩余中文片段。"""
        if not user_message:
            return ""
        candidate = cls._WEATHER_CITY_STRIP_RE.sub("", user_message)
        candidate = re.sub(r"[？?！!。，,．\s_\-—~]|吗$|呢$", "", candidate)
        return candidate.strip()

    def _normalize_tool_params(self, tool_name: str, params: Dict[str, Any], user_message: str, step: Any) -> Dict[str, Any]:
        """根据工具类型规范化参数，确保必需参数存在且格式正确。"""
        if not isinstance(params, dict):
            return {}

        normalized = dict(params)

        # postgresql_tool / mcp_postgres_tool：确保包含 operation
        if tool_name in {"postgresql_tool", "mcp_postgres_tool"}:
            if "operation" not in normalized:
                sql = normalized.get("sql", "")
                if sql:
                    normalized["operation"] = "query"
            if "params" not in normalized:
                normalized["params"] = []
            return normalized

        # map_tool：补参 resume 只带 table_name 时，默认视为加载矢量图层
        if tool_name == "map_tool":
            if "table_name" in normalized and "action" not in normalized:
                normalized["action"] = "load_vector_layer"
            return normalized

        # data_visualizer_tool：确保包含 demand
        if tool_name == "data_visualizer_tool":
            if "demand" not in normalized or not normalized["demand"]:
                normalized["demand"] = user_message
            return normalized

        # weather_tool：city 缺失时从用户消息兜底抽取（LLM 漏填 params 时避免反问死循环）
        if tool_name == "weather_tool":
            if not normalized.get("city"):
                extracted = self._extract_city_from_message(user_message)
                if extracted:
                    normalized["city"] = extracted
                    logger.info(f"[tool_node] weather_tool city fallback: '{extracted}'")
            return normalized

        # report_generator_tool：确保 variables 是字典
        if tool_name == "report_generator_tool":
            if "variables" not in normalized:
                normalized["variables"] = {}
            if not isinstance(normalized["variables"], dict):
                normalized["variables"] = {}
            return normalized

        # cesium_tool：标准化 action/type 字段
        if tool_name == "cesium_tool":
            if "action" not in normalized and "type" not in normalized:
                normalized["action"] = "unknown"
            return normalized

        return normalized
