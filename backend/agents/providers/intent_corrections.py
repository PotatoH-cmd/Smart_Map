"""intent_node 各业务主题的"确定性兑底"计划纠正 Provider。

自 intent_node.py 迁移而来，逐字保留行为。注册顺序 = 原节点内执行顺序
（后纠正覆盖前纠正）：报告裁剪 → 红线距离 → 测深加载 → 缓冲区 →
坐标转矢量 → 数值查询 → 砂场统计。
"""
import logging
import re
from typing import Dict, List, Optional

from core.kb_rules import fill_template, match_profiles
from core import db_schema as DB

from ..intent_types import IntentType, TaskStep, extract_county_xuke
from .plan_correction import PlanCorrectionProvider, register_correction

logger = logging.getLogger(__name__)


# ════════════════════════ 报告意图裁剪 ════════════════════════
@register_correction
class ReportTrimProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        if intent_result.primary_intent != IntentType.REPORT_GENERATION:
            return None
        caisha = [s for s in intent_result.execution_plan if s.tool == "caisha_report_tool"]
        if not caisha:
            return None
        return {
            "execution_plan": caisha,
            "description": "Report intent: trimmed plan to caisha_report_tool only",
        }


# ════════════════════════ 红线距离分析 ════════════════════════
@register_correction
class RedlineDistanceProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """红线距离分析挂底：LLM 常规划错误的 postgresql/spatial_reference 步骤，
        确定性替换/裁剪为 qgis_mcp_tool 单步。"""
        _redline_hit = ("红线" in user_msg) and any(
            k in user_msg for k in ("距离", "多远", "最近", "离", "距"))
        if _redline_hit and intent_result.primary_intent in (
                IntentType.SPATIAL_ANALYSIS, IntentType.SPATIAL_REFERENCE):
            qgis = [s for s in intent_result.execution_plan if s.tool == "qgis_mcp_tool"]
            if qgis:
                return {
                    "execution_plan": qgis,
                    "description": "Redline-distance: trimmed plan to qgis_mcp_tool only",
                }
            return {
                "primary_intent": IntentType.SPATIAL_ANALYSIS,
                "execution_plan": [self._build_redline_distance_step()],
                "task_context": f"红线距离分析兜底：{user_msg}",
                "confidence": 0.98,
                "description": "Redline-distance fallback: plan replaced with qgis_mcp_tool step",
            }
        return None

    @staticmethod
    def _build_redline_distance_step() -> TaskStep:
        return TaskStep(
            step_id=1,
            action="计算采区到最近河道红线的距离",
            tool="qgis_mcp_tool",
            params={"operation": "distance_to_redline"},
            reasoning="红线距离分析兜底：qgis 工作流 distance_to_redline 计算最短距离并生成连线图层",
            expected_output="最短距离（米）与最近红线连线图层",
        )


# ════════════════════════ 测深数据加载 ════════════════════════
@register_correction
class DepthLoadProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """测深数据加载兜底："加载XX测深数据到地图" → map_tool load_vector_layer 单步。"""
        _depth_load_hit = ("测深" in user_msg) and any(
            k in user_msg for k in ("加载", "显示", "展示", "地图", "绘制", "叠加"))
        if _depth_load_hit and intent_result.primary_intent == IntentType.MAP_DISPLAY:
            return {
                "execution_plan": [self._build_depth_load_step(user_msg)],
                "task_context": f"测深数据加载兜底：{user_msg}",
                "confidence": 0.98,
                "description": "Depth-load fallback: plan replaced with map_tool load_vector_layer step",
            }
        return None

    @staticmethod
    def _build_depth_load_step(user_msg: str) -> TaskStep:
        """动作词前缀（"加载种子场可采区" → "种子场可采区"）需剥离，避免吞入过滤条件。"""
        site_m = re.search(
            r"(?:加载|显示|展示|绘制|查看|查询|叠加)?([\u4e00-\u9fa5]{2,12}?(?:可采区|砂场|采区|采点))",
            user_msg or "")
        site = site_m.group(1) if site_m else ""
        filter_expr = ""
        layer_name = "测深点"
        if site:
            filter_expr = f'{DB.quote(DB.CESHEN_AREA_NAME)} LIKE \'%{site}%\''
            layer_name = f"{site}测深点"
        return TaskStep(
            step_id=1,
            action=f"加载{site or '采区'}测深数据到地图",
            tool="map_tool",
            params={
                "action": "load_vector_layer",
                "table_name": DB.TABLE_CESHEN,
                "geom_col": "geom",
                "filter": filter_expr,
                "layer_name": layer_name,
                "color_expression": f"CASE WHEN ({DB.quote(DB.CESHEN_CONTROL_ELEVATION)} - {DB.quote(DB.CESHEN_MEASURED_DEPTH)}) > 2 THEN 'red' ELSE 'green' END",
            },
            reasoning=f"测深数据加载兜底：map_tool load_vector_layer 从 {DB.TABLE_CESHEN} 表读取测深点位",
            expected_output="地图加载测深点位图层",
        )


# ════════════════════════ 缓冲区分析 ════════════════════════
@register_correction
class BufferProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """缓冲区分析兜底：确定性替换/裁剪为 qgis_mcp_tool 单步（buffer recipe 自动完成）。"""
        _buffer_hit = ("缓冲" in user_msg) and (
            "米" in user_msg or re.search(r"\d+", user_msg) is not None)
        if _buffer_hit and intent_result.primary_intent in (
                IntentType.SPATIAL_ANALYSIS, IntentType.SPATIAL_REFERENCE,
                IntentType.MAP_DISPLAY, IntentType.UNKNOWN):
            qgis = [s for s in intent_result.execution_plan if s.tool == "qgis_mcp_tool"]
            if qgis:
                return {
                    "execution_plan": qgis,
                    "description": "Buffer fallback: trimmed plan to qgis_mcp_tool only",
                }
            return {
                "primary_intent": IntentType.SPATIAL_ANALYSIS,
                "execution_plan": [self._build_buffer_step()],
                "task_context": f"缓冲区分析兜底：{user_msg}",
                "confidence": 0.98,
                "description": "Buffer fallback: plan replaced with qgis_mcp_tool step",
            }
        return None

    @staticmethod
    def _build_buffer_step() -> TaskStep:
        return TaskStep(
            step_id=1,
            action="创建缓冲区并加载到地图",
            tool="qgis_mcp_tool",
            params={"operation": "buffer"},
            reasoning="缓冲区分析兜底：qgis 工作流 buffer 完成筛选、缓冲、导出合并与地图加载",
            expected_output="缓冲区图层（含原始边界）加载到地图",
        )


# ════════════════════════ 坐标转矢量（空间处理） ════════════════════════
@register_correction
class SpatialProcessingProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """坐标转矢量兜底：确定性替换为 spatial_processing_tool 单步。"""
        _spatial_hit = (
            ("坐标" in user_msg and any(k in user_msg for k in ("矢量", "转换", "生成", "范围", "面", "加载", "点")))
            or any(k in user_msg.lower() for k in ("cgcs2000", "epsg", "带号", "投影"))
            or re.search(r"(?<!\d)45(?:2[5-8]|4[4-8]|49[0-9])(?!\d)", user_msg)
        )
        if _spatial_hit and intent_result.primary_intent in (
                IntentType.SPATIAL_PROCESSING, IntentType.SPATIAL_REFERENCE,
                IntentType.MAP_DISPLAY, IntentType.SPATIAL_ANALYSIS, IntentType.UNKNOWN):
            return {
                "primary_intent": IntentType.SPATIAL_PROCESSING,
                "execution_plan": [self._build_spatial_step(user_msg)],
                "task_context": f"坐标转矢量兜底：{user_msg}",
                "confidence": 0.98,
                "description": "Spatial-processing fallback: plan replaced with spatial_processing_tool step",
            }
        return None

    def _build_spatial_step(self, user_msg: str) -> TaskStep:
        coords = self._extract_coord_pairs(user_msg or "")
        cnt_m = re.search(r"([一二三四五六七八九十两\d]+)\s*个\s*(?:坐标)?点", user_msg or "")
        if cnt_m and coords:
            cn_map = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            raw_cnt = cnt_m.group(1)
            expect = int(raw_cnt) if raw_cnt.isdigit() else cn_map.get(raw_cnt)
            if expect and len(coords) != expect:
                coords = []
        src_crs = "4326"
        crs_m = re.search(r"(?<!\d)(45\d{2}|4326|3857|4214|4610)(?!\d)", user_msg or "")
        if crs_m:
            src_crs = crs_m.group(1)
        action = ("generate_polyline"
                  if "线" in (user_msg or "") and "范围" not in (user_msg or "")
                  else "generate_polygon")
        return TaskStep(
            step_id=1,
            action="坐标转换生成矢量并加载到地图",
            tool="spatial_processing_tool",
            params={
                "action": action,
                "coordinates": coords,
                "source_crs": src_crs,
                "layer_name": "坐标矢量范围",
            },
            reasoning="坐标转矢量兜底：spatial_processing_tool 完成投影转换、GeoJSON 生成并自动加载到地图",
            expected_output="矢量范围图层加载到地图",
        )

    def _extract_coord_pairs(self, text: str) -> List[List[float]]:
        pairs = _extract_coord_pairs_structured(text)
        if pairs and len(pairs) >= 3:
            return pairs
        return _extract_coord_pairs_flat(text)


def _classify_northing_easting(a: float, b: float):
    """按数值量级把一对数归一化为 [X(北坐标), Y(东坐标)]；无法分类返回 None。"""
    def _is_northing(v):
        return 3_200_000 <= abs(v) <= 4_500_000

    def _is_easting(v):
        return 18_000_000 <= abs(v) <= 40_000_000

    if _is_northing(a) and _is_easting(b):
        return [a, b]
    if _is_easting(a) and _is_northing(b):
        return [b, a]
    return None


def _extract_coord_pairs_structured(text: str) -> List[List[float]]:
    """结构化行解析：「点号 X值 Y值」每行一个拐点。"""
    rows: List[tuple] = []
    last_pair = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        bigs = re.findall(r"\d{6,9}(?:\.\d+)?", line)
        if len(bigs) != 2:
            continue
        try:
            pair = _classify_northing_easting(float(bigs[0]), float(bigs[1]))
        except ValueError:
            continue
        if pair is None:
            continue
        if pair == last_pair:
            continue  # 连续重复（VLM 复读最后一行）
        last_pair = pair
        remainder = re.sub(r"\d{6,9}(?:\.\d+)?", " ", line)
        smalls = [int(x) for x in re.findall(r"(?<![\d.])\d{1,3}(?![\d.])", remainder)]
        rows.append((smalls[0] if smalls else None, pair))

    if not rows:
        return []
    numbered = {no: pair for no, pair in rows if no is not None}
    unnumbered = [pair for no, pair in rows if no is None]
    if unnumbered:
        total = len(rows)
        missing = [i for i in range(1, total + 1) if i not in numbered]
        for pair, no in zip(unnumbered, missing):
            numbered.setdefault(no, pair)
    return [numbered[no] for no in sorted(numbered)]


def _extract_coord_pairs_flat(text: str) -> List[List[float]]:
    """回退：相邻大数配对 + 量级分类过滤（适用于手输坐标串）。"""
    pairs: List[List[float]] = []
    for m in re.finditer(
            r"(\d{6,9}(?:\.\d+)?)[,，;；\s\t]+(\d{6,9}(?:\.\d+)?)",
            text or ""):
        try:
            pair = _classify_northing_easting(
                float(m.group(1)), float(m.group(2)))
        except ValueError:
            continue
        if pair and (not pairs or pairs[-1] != pair):
            pairs.append(pair)
    return pairs


# ════════════════════════ 数值查询（spatial_reference → data_query） ════════════════════════
@register_correction
class NumericQueryProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """数值查询兜底："XX采区2025年控制开采高程范围"被误判为 spatial_reference
        时，改判 data_query 并按模板构造 postgresql_tool 查询。"""
        if intent_result.primary_intent != IntentType.SPATIAL_REFERENCE:
            return None
        numeric_hit = any(k in user_msg for k in ("高程", "深度", "超深", "多少", "几个", "数量"))
        spatial_hit = any(k in user_msg for k in ("范围内", "附近", "周边", "离", "距"))
        if numeric_hit and not spatial_hit:
            fallback = self._build_numeric_fallback_step(user_msg)
            if fallback is not None:
                return {
                    "primary_intent": IntentType.DATA_QUERY,
                    "execution_plan": [fallback],
                    "task_context": f"数值查询兜底：{user_msg}",
                    "confidence": 0.98,
                    "description": "Numeric-query fallback: spatial_reference → data_query",
                }
        return None

    @staticmethod
    def _build_numeric_fallback_step(user_msg: str) -> Optional[TaskStep]:
        site_m = re.search(r"([\u4e00-\u9fa5]{2,20}?(?:可采区|砂场|采区))", user_msg or "")
        if not site_m:
            return None
        site = site_m.group(1).replace("的", "").strip()
        year_m = re.search(r"(20\d{2})", user_msg or "")

        cols: List[str]
        if "控制" in user_msg and "高程" in user_msg:
            cols = [f'MIN({DB.quote(DB.CESHEN_CONTROL_ELEVATION)}) AS min_ctrl',
                    f'MAX({DB.quote(DB.CESHEN_CONTROL_ELEVATION)}) AS max_ctrl']
        elif "实测" in user_msg and ("高程" in user_msg or "深度" in user_msg):
            cols = [f'MIN({DB.quote(DB.CESHEN_MEASURED_DEPTH)}) AS min_measured',
                    f'MAX({DB.quote(DB.CESHEN_MEASURED_DEPTH)}) AS max_measured']
        elif "超深" in user_msg:
            cols = [f'ROUND(AVG({DB.quote(DB.CESHEN_CONTROL_ELEVATION)} - {DB.quote(DB.CESHEN_MEASURED_DEPTH)})::numeric, 3) AS avg_diff_m']
        elif "高程" in user_msg or "深度" in user_msg:
            cols = [
                f'MIN({DB.quote(DB.CESHEN_MEASURED_DEPTH)}) AS min_measured',
                f'MAX({DB.quote(DB.CESHEN_MEASURED_DEPTH)}) AS max_measured',
                f'MIN({DB.quote(DB.CESHEN_CONTROL_ELEVATION)}) AS min_ctrl',
                f'MAX({DB.quote(DB.CESHEN_CONTROL_ELEVATION)}) AS max_ctrl',
            ]
        elif "多少" in user_msg or "几个" in user_msg or "数量" in user_msg:
            cols = [f'COUNT(DISTINCT {DB.quote(DB.CESHEN_AREA_NAME)}) AS site_count',
                    'COUNT(*) AS point_count']
        else:
            return None

        where = f'{DB.quote(DB.CESHEN_AREA_NAME)} LIKE \'%{site}%\''
        if year_m:
            where += f' AND "Year" = {year_m.group(1)}'
        sql = f"SELECT {', '.join(cols)} FROM {DB.TABLE_CESHEN} WHERE {where}"
        return TaskStep(
            step_id=1,
            action=f"查询 {site} 的数值信息",
            tool="postgresql_tool",
            params={"operation": "query", "sql": sql, "params": []},
            reasoning=f"数值查询兜底：从 {DB.TABLE_CESHEN} 表查询真实数据",
            expected_output="返回查询结果",
        )


# ════════════════════════ 砂场/采区数量统计 ════════════════════════
@register_correction
class SandfieldCountPlanProvider(PlanCorrectionProvider):
    def correct(self, user_msg: str, intent_result) -> Optional[Dict]:
        """砂场/采区数量统计兜底：DB 去重统计（+ 许可口径 KB 检索）双步计划。

        触发词与 KB 检索词模板均来自规则档案（config/kb_rules.yaml），代码不含领域词表；
        许可口径仅在「许可停发年份之前」适用（档案 fallback_query_max_year），
        更晚年度不再发无效的许可文档检索步骤（其权威口径由年度方案检索路径承担）。
        """
        if intent_result.primary_intent in (
                IntentType.DATA_QUERY, IntentType.KNOWLEDGE_SEARCH,
                IntentType.SPATIAL_REFERENCE, IntentType.UNKNOWN):
            if not match_profiles(user_msg or ""):
                return None
            stat_plan = self._build_sandfield_count_steps(user_msg)
            if stat_plan:
                return {
                    "primary_intent": IntentType.DATA_QUERY,
                    "execution_plan": stat_plan,
                    "task_context": f"砂场数量统计兜底：{user_msg}",
                    "confidence": 0.98,
                    "description": (
                        f"Sandfield-count fallback: dual DB+KB plan "
                        f"({len(stat_plan)} steps) for {user_msg[:40]}"
                    ),
                }
        return None

    @staticmethod
    def _build_sandfield_count_steps(user_msg: str) -> Optional[List[TaskStep]]:
        extracted = extract_county_xuke(user_msg)
        if not extracted:
            return None
        county_full, abbr, year = extracted
        sql = (
            f'SELECT COUNT(DISTINCT {DB.quote(DB.CESHEN_AREA_NAME)}) AS "砂场数量" '
            f"FROM {DB.TABLE_CESHEN} WHERE {DB.quote(DB.CESHEN_COUNTY)} = '{county_full}' "
            f"AND {DB.quote(DB.CESHEN_YEAR)} = {year}"
        )
        profiles = match_profiles(user_msg or "", year)
        profile = profiles[0] if profiles else {}
        steps = [
            TaskStep(
                step_id=1,
                action=f"统计 {county_full} {year} 年砂场数量",
                tool="postgresql_tool",
                params={"operation": "query", "sql": sql, "params": []},
                reasoning=f"砂场数量统计兜底：{DB.TABLE_CESHEN} 表按采区名去重统计",
                expected_output=f"{county_full} {year} 年砂场（采区）数量",
            ),
        ]
        # 许可口径检索步骤：检索词模板与适用年度上限均来自规则档案
        tpl = profile.get("fallback_query_template")
        try:
            cap = profile.get("fallback_query_max_year")
            cap = int(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        if not tpl or (cap is not None and int(year) > cap):
            logger.info(
                f"[intent_node] 砂场统计：{year} 年不适用许可口径检索（上限={cap}），跳过该步骤"
            )
            return steps
        kb_query = fill_template(tpl, abbr=abbr, year=year)
        steps.append(TaskStep(
            step_id=len(steps) + 1,
            action=f"检索 {county_full} {year} 年采砂许可文档",
            tool="knowledge_base_tool",
            params={"operation": "search", "query": kb_query, "top_k": 20},
            reasoning="砂场数量统计兜底：按许可号模板确定性检索知识库采砂许可文档",
            expected_output=f"{county_full} {year} 年采砂许可文档列表",
        ))
        return steps
