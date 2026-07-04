"""
report_builder.py
─────────────────────────────────────────────────────────────────
报告内容智能构建器。

职责：
  1. 从多个工具结果（数据库、知识库、可视化等）中提取并预聚合数据
  2. 根据业务规则（超深度开采判断等）做语义标注
  3. 调用 LLM 生成各章节专业文字
  4. 返回可直接填入 Word 模板占位符的变量字典

模板占位符说明（report_template.docx）:
  {{report_title}}   — 报告标题
  {{summary}}        — 概述（多段，用 \n 分隔）
  {{details}}        — 详细数据（多段）
  {{conclusion}}     — 结论与建议（多段）
  {{generated_date}} — 由工具自动填充，无需传入
"""

import json
import logging
import re
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# ── 业务常量 ──────────────────────────────────────────────────────
OVERDEPTH_THRESHOLD = 2.0          # 超深度开采判定阈值（米）
DEPTH_FIELD_ALIASES = [            # 实测高程字段候选名
    "measured_depth", "measureddepth", "实测高程", "测深", "depth"
]
CONTROL_FIELD_ALIASES = [          # 控制高程字段候选名
    "control_elevation", "controlelevation", "控制高程", "control"
]
AREA_FIELD_ALIASES = [             # 可采区名称字段候选名
    "mineable_area_name", "mineableareaname", "可采区名称", "area_name", "name"
]


# ─────────────────────────────────────────────────────────────────
# 公共入口
# ─────────────────────────────────────────────────────────────────

def check_data_sufficiency(prior_results: List[Tuple]) -> Tuple[bool, str]:
    """
    检查前置工具结果中是否包含足够的有效数据来生成报告。
    返回 (is_sufficient, reason)。
    """
    db_summary = _extract_db_summary(prior_results)

    # 情况1：完全没有查到数据库结果
    if not db_summary["found"]:
        return False, "未检索到任何数据库记录，无法生成报告。"

    # 情况2：查到记录但记录数为0
    if db_summary["record_count"] == 0:
        return False, "数据库查询结果为空（0条记录），无法生成报告。"

    # 情况3：有记录但所有关键业务字段均缺失（深度和控制高程都没有有效值）
    has_depth = bool(db_summary.get("depth_stats"))
    has_control = bool(db_summary.get("control_stats"))
    has_extra = bool(db_summary.get("extra_content", "").strip())
    raw_rows = db_summary.get("raw_rows", [])

    # 如果有数据行，检查是否所有行的关键字段都是 None/空
    if raw_rows and not has_depth and not has_control:
        # 进一步检查：是否至少有一些非空的业务字段值
        non_empty_field_count = 0
        for row in raw_rows[:50]:  # 抽样检查前50条
            if not isinstance(row, dict):
                continue
            for v in row.values():
                if v is not None and str(v).strip() != "":
                    non_empty_field_count += 1
                    break
        if non_empty_field_count == 0:
            return False, "查询到的记录中所有字段均为空值，无有效数据可供分析。"

        # 关键分析字段全部缺失时，判定为数据不足
        return False, "查询到的记录中缺少关键分析字段（实测高程、控制高程），数据不足以生成有意义的分析报告。建议补充完整测深数据后重新生成。"

    return True, ""


def build_report_variables(
    prior_results: List[Tuple],
    user_message: str,
    task_context: str,
    llm: ChatOpenAI,
) -> Dict[str, Any]:
    """
    主入口。先做数据预处理，再调 LLM 生成报告文字。
    返回格式：{"report_title": ..., "summary": ..., "details": ..., "conclusion": ...}
    """
    # 1. 提取并结构化各工具结果
    db_summary = _extract_db_summary(prior_results)
    kb_summary = _extract_kb_summary(prior_results)
    chart_summary = _extract_chart_summary(prior_results)

    # 2. 业务规则分析
    compliance = _analyze_compliance(db_summary)

    # 3. 组装上下文给 LLM
    context_block = _build_context_block(
        db_summary, kb_summary, chart_summary, compliance, user_message, task_context
    )

    # 4. LLM 生成报告文字
    variables = _llm_generate(context_block, user_message, task_context, llm)

    # 5. 追加结构化图表数据（供 report_generator_tool 自动绘图插入 Word）
    variables["_report_charts"] = _build_report_charts_payload(db_summary, compliance)

    logger.info(
        f"[ReportBuilder] title={variables.get('report_title','')!r} "
        f"summary={len(variables.get('summary',''))}chars "
        f"details={len(variables.get('details',''))}chars"
    )
    return variables


def _build_report_charts_payload(db_summary: Dict, compliance: Dict) -> List[Dict[str, Any]]:
    """构造报告图表载荷：统计柱状图 + 合规占比图。"""
    charts: List[Dict[str, Any]] = []

    depth_stats = db_summary.get("depth_stats") or {}
    if depth_stats:
        values = [
            float(depth_stats.get("mean", 0) or 0),
            float(depth_stats.get("max", 0) or 0),
            float(depth_stats.get("min", 0) or 0),
            float(depth_stats.get("stdev", 0) or 0),
        ]
        charts.append({
            "type": "bar",
            "title": "实测高程统计特征",
            "labels": ["均值", "最大值", "最小值", "标准差"],
            "values": values,
            "unit": "m",
        })

    total = int(db_summary.get("record_count") or 0)
    overdepth = int(len(db_summary.get("overdepth_records") or []))
    if total > 0:
        normal = max(total - overdepth, 0)
        charts.append({
            "type": "doughnut",
            "title": "超深度记录占比",
            "labels": ["超深度记录", "非超深度记录"],
            "values": [overdepth, normal],
            "unit": "条",
            "note": f"风险等级：{compliance.get('risk_level', '未知')}",
        })

    return charts


# ─────────────────────────────────────────────────────────────────
# 数据提取层
# ─────────────────────────────────────────────────────────────────

def _find_field(row: Dict, aliases: List[str]) -> Optional[Any]:
    """在一条记录里按候选字段名查找值（不区分大小写）。"""
    lower_row = {k.lower(): v for k, v in row.items()}
    for alias in aliases:
        if alias.lower() in lower_row:
            return lower_row[alias.lower()]
    return None


def _extract_db_summary(prior_results: List[Tuple]) -> Dict:
    """从数据库工具结果中提取统计摘要。"""
    summary = {
        "found": False,
        "tool_name": "",
        "record_count": 0,
        "fields": [],
        "area_names": [],
        "depth_stats": {},          # measured_depth 统计
        "control_stats": {},        # control_elevation 统计
        "overdepth_records": [],    # 超深度记录
        "raw_rows": [],             # 前20条原始数据
        "extra_content": "",        # content/message 文字
    }

    for _, item in prior_results:
        tool_name = item.get("tool_name", "")
        if tool_name not in {"postgresql_tool", "mcp_postgres_tool"}:
            continue
        result = item.get("result", {})
        if not isinstance(result, dict) or result.get("success") is False:
            continue

        data = result.get("data")
        content = result.get("content") or result.get("message") or ""

        summary["found"] = True
        summary["tool_name"] = tool_name
        summary["extra_content"] = str(content)[:600] if content else ""

        if isinstance(data, list) and data:
            rows = data
            summary["record_count"] = len(rows)
            # 传递更多原始数据给 LLM（原20条太少导致建议"加载全部"）
            summary["raw_rows"] = rows[:200] if len(rows) > 200 else rows
            if rows and isinstance(rows[0], dict):
                summary["fields"] = list(rows[0].keys())

            # 提取可采区名称（去重）
            areas = set()
            for row in rows:
                if isinstance(row, dict):
                    area = _find_field(row, AREA_FIELD_ALIASES)
                    if area:
                        areas.add(str(area))
            summary["area_names"] = sorted(areas)[:20]

            # 计算实测高程统计
            depths = []
            controls = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                d = _find_field(row, DEPTH_FIELD_ALIASES)
                c = _find_field(row, CONTROL_FIELD_ALIASES)
                try:
                    if d is not None:
                        depths.append(float(d))
                    if c is not None:
                        controls.append(float(c))
                except (ValueError, TypeError):
                    pass

            if depths:
                summary["depth_stats"] = {
                    "count": len(depths),
                    "mean": round(statistics.mean(depths), 3),
                    "max": round(max(depths), 3),
                    "min": round(min(depths), 3),
                    "stdev": round(statistics.stdev(depths), 3) if len(depths) > 1 else 0,
                }
            if controls:
                summary["control_stats"] = {
                    "count": len(controls),
                    "mean": round(statistics.mean(controls), 3),
                    "max": round(max(controls), 3),
                    "min": round(min(controls), 3),
                }

            # 判断超深度记录
            for row in rows:
                if not isinstance(row, dict):
                    continue
                d = _find_field(row, DEPTH_FIELD_ALIASES)
                c = _find_field(row, CONTROL_FIELD_ALIASES)
                try:
                    if d is not None and c is not None:
                        diff = float(c) - float(d)
                        if diff > OVERDEPTH_THRESHOLD:
                            summary["overdepth_records"].append({
                                "area": _find_field(row, AREA_FIELD_ALIASES),
                                "measured": float(d),
                                "control": float(c),
                                "diff": round(diff, 3),
                            })
                except (ValueError, TypeError):
                    pass

        elif isinstance(data, dict):
            # 聚合查询结果（单行 dict）
            summary["found"] = True
            summary["extra_content"] += "\n聚合统计：" + json.dumps(data, ensure_ascii=False)[:400]

    return summary


def _extract_kb_summary(prior_results: List[Tuple]) -> str:
    """从知识库结果中提取关键政策/规范文字。"""
    parts = []
    for _, item in prior_results:
        if item.get("tool_name") != "knowledge_base_tool":
            continue
        result = item.get("result", {})
        if not isinstance(result, dict) or result.get("success") is False:
            continue
        content = result.get("content") or result.get("message") or ""
        if content:
            parts.append(str(content)[:600])
    return "\n\n".join(parts)


def _extract_chart_summary(prior_results: List[Tuple]) -> str:
    """从可视化工具结果中提取图表描述。"""
    parts = []
    for _, item in prior_results:
        if item.get("tool_name") != "data_visualizer_tool":
            continue
        result = item.get("result", {})
        if not isinstance(result, dict) or result.get("success") is False:
            continue
        chart_type = result.get("chart_type", "")
        config = result.get("config") or {}
        title = (config.get("title") or {}).get("text", "")
        content = result.get("content") or ""
        parts.append(f"图表类型：{chart_type}，标题：{title}。{str(content)[:200]}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────
# 业务规则分析层
# ─────────────────────────────────────────────────────────────────

def _analyze_compliance(db_summary: Dict) -> Dict:
    """根据超深度开采规则判断合规性。"""
    result = {
        "has_overdepth": False,
        "overdepth_count": 0,
        "overdepth_rate": 0.0,
        "max_overdepth": 0.0,
        "compliance_text": "数据不足，无法判断合规性。",
        "risk_level": "未知",
    }

    if not db_summary.get("found"):
        return result

    overdepth = db_summary.get("overdepth_records", [])
    total = db_summary.get("record_count", 0)

    result["overdepth_count"] = len(overdepth)
    result["has_overdepth"] = len(overdepth) > 0

    if total > 0:
        result["overdepth_rate"] = round(len(overdepth) / total * 100, 1)

    if overdepth:
        result["max_overdepth"] = max(r["diff"] for r in overdepth)

    # 判断风险等级
    rate = result["overdepth_rate"]
    if len(overdepth) == 0:
        result["risk_level"] = "合规"
        result["compliance_text"] = (
            f"经本次检测，共核查 {total} 条测深记录，"
            f"未发现超深度开采情况（判定标准：控制高程与实测高程差值 > {OVERDEPTH_THRESHOLD}m），"
            "本次采砂活动符合规范要求。"
        )
    elif rate < 10:
        result["risk_level"] = "低风险"
        result["compliance_text"] = (
            f"经检测，{total} 条记录中发现 {len(overdepth)} 条疑似超深度开采记录"
            f"（占比 {rate}%），最大超采深度 {result['max_overdepth']}m，"
            "风险较低，建议加强重点区域监测。"
        )
    elif rate < 30:
        result["risk_level"] = "中等风险"
        result["compliance_text"] = (
            f"经检测，{total} 条记录中发现 {len(overdepth)} 条超深度开采记录"
            f"（占比 {rate}%），最大超采深度 {result['max_overdepth']}m，"
            "存在中等程度违规风险，建议责令相关采砂单位整改并复测。"
        )
    else:
        result["risk_level"] = "高风险"
        result["compliance_text"] = (
            f"经检测，{total} 条记录中发现 {len(overdepth)} 条超深度开采记录"
            f"（占比 {rate}%），最大超采深度 {result['max_overdepth']}m，"
            "超深度开采情况严重，建议立即暂停采砂作业并启动执法程序。"
        )

    return result


# ─────────────────────────────────────────────────────────────────
# 上下文组装层
# ─────────────────────────────────────────────────────────────────

def _build_context_block(
    db: Dict, kb: str, chart: str, compliance: Dict,
    user_message: str, task_context: str,
) -> str:
    """将所有来源的数据整理成结构化文本块，供 LLM 阅读。"""
    lines = []

    lines.append(f"【用户需求】{user_message}")
    lines.append(f"【任务背景】{task_context}")
    lines.append("")

    # 数据库部分
    if db["found"]:
        lines.append("【数据库查询结果】")
        lines.append(f"  总记录数：{db['record_count']} 条")

        if db["area_names"]:
            lines.append(f"  涉及可采区：{', '.join(db['area_names'][:10])}")

        if db["depth_stats"]:
            s = db["depth_stats"]
            lines.append(
                f"  实测高程统计：均值 {s['mean']}m，最大 {s['max']}m，"
                f"最小 {s['min']}m，标准差 {s['stdev']}m"
            )

        if db["control_stats"]:
            s = db["control_stats"]
            lines.append(
                f"  控制高程统计：均值 {s['mean']}m，最大 {s['max']}m，最小 {s['min']}m"
            )

        lines.append(f"  合规性判断：{compliance['compliance_text']}")
        lines.append(f"  风险等级：{compliance['risk_level']}")

        if db["overdepth_records"]:
            lines.append(f"  超深度明细（前5条）：")
            for r in db["overdepth_records"][:5]:
                lines.append(
                    f"    - 区域：{r['area']}，实测 {r['measured']}m，"
                    f"控制 {r['control']}m，超采 {r['diff']}m"
                )

        # 原始测深数据明细（让 LLM 基于完整数据分析，而非猜测"只有20条"）
        raw_rows = db.get("raw_rows", [])
        if raw_rows:
            lines.append(f"  测深数据明细（共{len(raw_rows)}条，已全部传入分析）：")
            # 展示每条的关键字段
            for i, row in enumerate(raw_rows):
                if not isinstance(row, dict):
                    continue
                area = _find_field(row, AREA_FIELD_ALIASES) or ""
                depth = _find_field(row, DEPTH_FIELD_ALIASES)
                control = _find_field(row, CONTROL_FIELD_ALIASES)
                diff = ""
                if depth is not None and control is not None:
                    try:
                        diff = f" 差值{round(float(control)-float(depth), 3)}m"
                    except (ValueError, TypeError):
                        pass
                line = f"    {i+1}. "
                if area: line += f"区域:{area} "
                if depth is not None: line += f"实测:{depth}m "
                if control is not None: line += f"控制:{control}m"
                line += diff
                lines.append(line)

        if db["extra_content"]:
            lines.append(f"  工具返回摘要：{db['extra_content'][:800]}")  # 原300→800

        lines.append("")

    # 知识库部分
    if kb:
        lines.append("【知识库检索结果】")
        lines.append(kb[:500])
        lines.append("")

    # 图表部分
    if chart:
        lines.append("【可视化图表】")
        lines.append(chart[:300])
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# LLM 生成层
# ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一位专业的河道采砂监管技术报告撰写专家，熟悉水利行业规范和采砂管理法规。

你将收到结构化的数据分析结果，请据此生成一份完整的技术报告各章节正文。

写作要求：
1. 语言正式、专业，使用第三人称，适合政府技术文件
2. 数据引用精确，保留原始数值（不得篡改）
3. 段落间用空行分隔，绝对禁止使用 Markdown 符号（**、##、-、|）
4. 每段 80-120 字，整体字数：summary 200-300字，details 300-500字，conclusion 150-250字
5. conclusion 必须包含：合规性结论 + 具体建议措施

【绝对禁止 - 违反将导致报告不合格】
- 禁止在正文中提及任何工具名称（如 map_tool、postgresql_tool、knowledge_base_tool 等）
- 禁止输出函数调用语法（如 load_vector_layer、set_view、action='xxx' 等）
- 禁止建议用户"调用接口"、"使用工具"、"通过xx加载"
- 只输出业务结论和建议措施，不暴露后台技术实现细节
- 报告读者是政府领导/监管人员，不是开发人员

严格按以下 JSON 格式输出，不输出任何其他内容：
{
  "report_title": "报告标题（含地点+内容+时间，如：XX河XX段采砂深度监测报告（2026年4月））",
  "summary": "概述正文（多段，段落间用\\n\\n分隔）",
  "details": "详细数据正文（多段，引用具体数值）",
  "conclusion": "结论与建议正文（多段）"
}"""

_FALLBACK_VARIABLES_TEMPLATE = {
    "report_title": "{title}",
    "summary": "本报告根据现场采砂深度监测数据编制，数据来源于实时采集系统。{extra}",
    "details": "详细数据见附件。",
    "conclusion": "建议加强现场监管，确保采砂活动在批准范围内进行。",
}


def _llm_generate(
    context_block: str,
    user_message: str,
    task_context: str,
    llm: ChatOpenAI,
) -> Dict[str, Any]:
    """调用 LLM 生成报告变量，失败时用规则降级。"""
    user_prompt = f"""{context_block}

请根据以上数据，生成报告各章节内容（JSON格式）："""

    try:
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]
        ai_msg = llm.invoke(messages)
        response_text = ai_msg.content if hasattr(ai_msg, "content") else str(ai_msg)

        # 提取 JSON（容忍 LLM 在 JSON 前后输出多余文字）
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if not json_match:
            raise ValueError("LLM 未返回有效 JSON")

        variables = json.loads(json_match.group())

        # 校验必需字段
        required = {"report_title", "summary", "details", "conclusion"}
        missing = required - set(variables.keys())
        if missing:
            raise ValueError(f"LLM 返回 JSON 缺少字段：{missing}")

        # 清理残余 Markdown（以防 LLM 不遵守约束）
        for key in required:
            variables[key] = _strip_markdown(str(variables[key]))

        # 后处理：去除工具/接口调用描述（防止 LLM 泄露技术细节）
        for key in required:
            sanitized = _sanitize_tool_references(variables[key])
            if sanitized != variables[key]:
                logger.info(f"[ReportBuilder] 清洗工具引用: {key} ({len(variables[key])}→{len(sanitized)}字)")
                variables[key] = sanitized

        return variables

    except Exception as e:
        logger.warning(f"[ReportBuilder] LLM 生成失败，启用规则降级: {e}")
        return _rule_based_fallback(context_block, task_context)


def _rule_based_fallback(context_block: str, task_context: str) -> Dict[str, Any]:
    """LLM 失败时的规则降级，确保报告仍有基本内容。"""
    year_month = datetime.now().strftime("%Y年%m月")
    title = f"{task_context[:30]}监测报告（{year_month}）" if task_context else f"采砂监测报告（{year_month}）"

    clean_context = _strip_markdown(context_block)
    lines = [l.strip() for l in clean_context.split("\n") if l.strip()]
    body = "\n\n".join(lines[:6])

    return {
        "report_title": title,
        "summary": f"本报告依据{year_month}采砂深度监测数据编制，{task_context[:80]}。\n\n{body[:300]}",
        "details": body[300:700] or "详细数据见附件。",
        "conclusion": f"综合以上数据，建议加强现场监管，定期复测，确保采砂活动在批准范围内进行。",
    }


def _strip_markdown(text: str) -> str:
    """清理 Markdown 格式符，输出适合 Word 的纯文字。"""
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\|[^\n]*\|', '', text)   # 表格行
    text = re.sub(r'[-]{3,}', '', text)       # 分隔线
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_tool_references(text: str) -> str:
    """后处理：去除 LLM 可能输出的工具/接口调用描述。
    
    只清除明确的技术实现细节，保留业务含义。
    """
    patterns = [
        # 精确匹配：通过/使用/调用/借助 xxx_tool 调用/加载/执行...
        r'(?:通过|使用|调用|借助|建议|可以)\s*[（(]?\s*\b\w+_?tool\b[^\u3002\n]{0,60}[。，]',
        # 函数名调用: load_vector_layer / set_view / fit_bounds 等
        r"(?:load_vector_layer|loadvectorlayer|set_view|setview|fit_bounds|add_layer|addlayer|query_data|querydata)\s*[(\s][^。\n]*[。，]?",
        # action='xxx' / tool='xxx' 参数
        r"(?:action|tool)\s*=\s*'[^']*'[^。\n]{0,30}[。，]?",
        # "使用xx接口/功能/API" 技术性表述
        r'(?:\u4f7f\u7528|\u8c03\u7528)\s*[a-zA-Z_]{2,20}\s*(?:\u63a5\u53e3|\u529f\u80fd|API|\u65b9\u6cd5|\u6a21\u5757)[^。\n]*[。，]',
        # 括号内技术参数
        r'[（(]\s*(?:action|tool|method|func|endpoint)\s*[:=]\s*["\'][^"\']*[\'"][）)]',
        # maptool 这类非标准工具名
        r'\bmaptool\b[^。\n]{0,50}',
        # 独立的 xxx_tool 引用（前后有中文或空格）
        r'(?:^|[\u4e00-\u9fff\s])\b([a-zA-Z]+_tool)\b(?=[\s\u4e00-\u9fff,，。])',
    ]
    
    original = text
    for p in patterns:
        text = re.sub(p, '', text, flags=re.IGNORECASE)
    
    # 清理删除后的语法残留
    text = re.sub(r'(?:通过|使用|调用|借助)\s*(?:maptool|[a-z_]*\s*)?(?:工具)?[，,]?\s*', '', text, flags=re.IGNORECASE)
    # "建议此举/建议此操作/建议该方法" → 直接删掉整个片段
    text = re.sub(r'建议(?:此(?:举|操作|方法|功能|系统))?[^。\n]{0,15}(?:有助于|以便|用于|来)', '', text)
    # 开头的孤立"可以/能够"
    text = re.sub(r'^(?:可以|能够)\s*(?=[\u4e00-\u9fff])', '', text.strip())
    text = re.sub(r'\s{2,}', ' ', text)
    
    if text != original:
        logger.info(f"[Sanitize] 清洗工具引用: {len(original)}→{len(text)}字")
    
    return text.strip()
