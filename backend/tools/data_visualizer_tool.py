import json
import logging
import asyncio
import os
import re
from typing import Dict, List, Any, Optional, Union
from qwen_agent.tools.base import BaseTool, register_tool
import dashscope

# 导入 PostgreSQLTool 以复用其逻辑
from .postgresql_tool import PostgreSQLTool
from .schema_manager import SchemaManager

logger = logging.getLogger(__name__)

@register_tool('data_visualizer_tool')
class DataVisualizerTool(BaseTool):
    """
    数据统计分析可视化工具。
    第一步：根据用户输入需求转换为数据库 SQL 并查询得到数据。
    第二步：将处理好的数据跟制图需求发送给阿里的 mcp-server-chart 调用相关函数，返回图表。
    """

    description = '数据统计分析可视化工具。输入用户需求，自动生成 SQL 查询数据库并直接生成交互式 ECharts 图表。'
    
    parameters = [
        {
            'name': 'demand',
            'type': 'string',
            'description': '用户的数据统计或制图需求，例如“统计各采区的可采量并用柱状图展示”',
            'required': True
        }
    ]

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        # 从配置或环境变量获取 API Key
        self.api_key = os.environ.get('DASHSCOPE_API_KEY', 'sk-e4990da94bfb4037be1f755fa586d048')
        self.model = 'qwen-flash-2025-07-28'
        
        # 初始化 PostgreSQLTool
        self.pg_tool = PostgreSQLTool({
            'host': '172.136.16.52',
            'port': 5432,
            'database': 'postgres',
            'user': 'postgres',
            'password': '8720622'
        })

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {'success': False, 'error': '无效的参数格式'}
        
        demand = params.get('demand')
        if not demand:
            return {'success': False, 'error': '未提供需求内容'}

        pre_sql = params.get('sql', '') or ''
        pre_chart_type = params.get('chart_type', '') or ''
        logger.info(f"DataVisualizerTool called with demand: {demand}, pre_sql={bool(pre_sql)}, pre_chart_type={pre_chart_type or 'auto'}")

        try:
            # 解决 asyncio.run() 不能在已运行的事件循环中调用的问题
            import threading
            from concurrent.futures import ThreadPoolExecutor

            def _run_async_in_thread(coro):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_async_in_thread, self._async_call(demand, pre_sql, pre_chart_type))
                return future.result()
        except Exception as e:
            logger.exception(f"DataVisualizerTool execution failed: {e}")
            return {'success': False, 'error': str(e)}

    async def _async_call(self, demand: str, pre_sql: str = '', pre_chart_type: str = '') -> Dict[str, Any]:
        # 1. 获取数据库 Schema（从集中缓存）
        sm = SchemaManager.instance()
        schema_text = sm.get_formatted_schema()
        if not schema_text:
            sm.refresh()
            schema_text = sm.get_formatted_schema()
        if not schema_text:
            return {'success': False, 'error': '获取数据库结构失败：Schema 为空'}

        rule_sql = self._generate_rule_based_sql(demand)
        if rule_sql:
            sql = rule_sql
            logger.info(f"[DataVisualizerTool] Using rule-based SQL: {sql}")
        elif pre_sql.strip():
            sql = pre_sql.strip()
            logger.info(f"[DataVisualizerTool] Using pre-generated SQL from IntentAgent: {sql}")
        else:
            sql = self._generate_sql(demand, schema_text)
            logger.info(f"Generated SQL (initial): {sql}")

        # 3. 执行 SQL 获取数据（失败时基于报错自动修复 SQL 并重试）
        max_attempts = 3
        query_res = None
        last_error = ''
        for attempt in range(1, max_attempts + 1):
            logger.info(f"[DataVisualizerTool] SQL attempt {attempt}/{max_attempts}: {sql}")
            query_res = self.pg_tool._query(sql, [])
            if query_res.get('success'):
                break

            last_error = str(query_res.get('error') or '未知错误')
            logger.warning(f"[DataVisualizerTool] SQL attempt {attempt} failed: {last_error}")

            if attempt >= max_attempts:
                return {
                    'success': False,
                    'error': f"数据查询失败（已重试{max_attempts}次）: {last_error}",
                    'sql': sql,
                }

            repaired_sql = self._repair_sql_with_error(
                demand=demand,
                schema=schema_text,
                failed_sql=sql,
                error_text=last_error,
            )
            if repaired_sql and repaired_sql.strip():
                sql = repaired_sql.strip()
            else:
                # 兜底：重新生成一次
                sql = self._generate_sql(demand, schema_text)
            logger.info(f"[DataVisualizerTool] SQL repaired for next attempt: {sql}")

        if not query_res or not query_res.get('success'):
            return {'success': False, 'error': f"数据查询失败: {last_error}"}
        
        data = query_res['data']
        if not data:
            return {'success': False, 'error': '查询结果为空，无法生成图表'}

        # 4. 针对“超深深度”需求额外提供标准化文本答案，但不再提前返回，避免丢失图表
        formatted = None
        if any(k in (demand or "") for k in ["超深", "超深深度", "平均超深"]):
            formatted = self._format_over_depth_answer(demand, data)

        # 5. 生成 ECharts 图表配置（传入 IntentAgent 预生成的 chart_type）
        chart_result = self._generate_echarts_chart(demand, data, pre_chart_type)
        if chart_result.get('success'):
            if formatted:
                chart_result['formatted_answer'] = formatted
            return chart_result

        # 图表生成失败时，至少返回标准化文本答案，避免完全无结果
        if formatted:
            return {
                'success': True,
                'formatted_answer': formatted,
                'content': formatted
            }

        return chart_result

    def _escape_sql_literal(self, value: str) -> str:
        return str(value).replace("'", "''")

    def _extract_county_name(self, text: str) -> Optional[str]:
        matches = re.findall(r'[\u4e00-\u9fa5]{1,10}(?:县|区|市)', text or "")
        for raw in reversed(matches):
            candidate = re.sub(r'^(统计|查询|分析|对|在|年|按|请|帮我|计算)+', '', raw)
            if not candidate:
                continue
            if len(candidate) <= 4:
                return candidate
            for n in (4, 3, 2):
                tail = candidate[-n:]
                if tail.endswith(('县', '区', '市')) and not any(s in tail[:-1] for s in ('县', '区', '市')):
                    return tail
        return None

    def _generate_rule_based_sql(self, demand: str) -> str:
        text = demand or ""
        if not self._is_over_depth_demand(text):
            return ""
        if not any(k in text for k in ["各个", "各采砂场", "各采区", "各可采区", "每个", "分别", "所有采砂场", "所有采区"]):
            return ""

        filters = [
            '"Control_Elevation" IS NOT NULL',
            '"Measured_Depth" IS NOT NULL',
            '"Mineable_Area_Name" IS NOT NULL',
        ]
        county_name = self._extract_county_name(text)
        if county_name:
            county = self._escape_sql_literal(county_name)
            filters.append(f'"County_District" = \'{county}\'')
        year_match = re.search(r'(20\d{2})\s*年?', text)
        if year_match:
            filters.append(f'"Year" = {int(year_match.group(1))}')

        where_clause = " AND ".join(filters)
        diff_expr = '("Control_Elevation" - "Measured_Depth")::numeric'
        return (
            'SELECT "Mineable_Area_Name", '
            'COUNT(*) AS point_count, '
            f'ROUND(AVG({diff_expr}), 3) AS avg_over_depth, '
            f'ROUND(MAX({diff_expr}), 3) AS max_over_depth, '
            f'ROUND(MIN({diff_expr}), 3) AS min_over_depth, '
            f"CASE WHEN AVG({diff_expr}) > 2 THEN '超深度开采' ELSE '未构成超深度开采' END AS over_depth_status "
            'FROM "ceshen" '
            f'WHERE {where_clause} '
            'GROUP BY "Mineable_Area_Name" '
            'ORDER BY avg_over_depth DESC;'
        )

    def _generate_sql(self, demand: str, schema: str) -> str:
        """使用 LLM 将用户需求转换为 SQL 语句"""
        
        # 加载 SQL 模板语料库
        examples_context = ""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            template_path = os.path.join(current_dir, 'sql_templates.json')
            if os.path.exists(template_path):
                with open(template_path, 'r', encoding='utf-8') as f:
                    templates = json.load(f)
                    examples_list = []
                    for t in templates:
                        examples_list.append(f"Q: {t['question']}\nSQL: {t['sql']}")
                    if examples_list:
                        examples_context = "参考以下相似问题的 SQL 写法：\n" + "\n\n".join(examples_list)
        except Exception as e:
            logger.warning(f"Failed to load SQL templates: {e}")

        extra_rules = ""
        if any(k in (demand or "") for k in ["超深", "超深深度", "平均超深"]):
            extra_rules = """
业务规则：
1. “超深深度”严格定义为整个区域或砂场的平均差值。
   - 平均超深：AVG("Control_Elevation" - "Measured_Depth")
   - 违规判定标准：只有当 AVG("Control_Elevation" - "Measured_Depth") > 2 时，整个区域才算作“超深度开采”。
   - 最大超深：MAX("Control_Elevation" - "Measured_Depth")（作为辅助参考，但不作为超深判定标准）
2. 请在 SELECT 中同时包含平均超深和最大超深指标，以便进行全面分析。
3. 对计算结果进行两位小数的四舍五入。
4. 仅统计非空记录：需要添加 "Control_Elevation" IS NOT NULL AND "Measured_Depth" IS NOT NULL 的过滤条件。
5. 如用户指定地区（如县区），使用 "County_District" 精确匹配；如指定采区名称，使用 "Mineable_Area_Name" 精确匹配。
"""
        prompt = f"""你是一个专业的 SQL 生成专家。请根据提供的数据库结构、参考案例和用户需求，生成一个标准的 PostgreSQL SQL 查询语句。

数据库结构信息：
{schema}

{examples_context}

用户需求：
{demand}

严格遵守以下规则：
1. 只返回 SQL 语句本身，不要包含任何解释、注释或 Markdown 代码块标记（如 ```sql）。
2. 字段名如果包含大写字母，必须使用双引号包裹，例如 "Mineable_Area_Name"。
3. 业务数据表通常为 'ceshen'。
4. 确保生成的 SQL 能够直接在 PostgreSQL 中运行。
5. 除非用户明确要求 TOP-N（如"前10名"），否则不要使用 LIMIT，应返回全部满足条件的数据。
{extra_rules}
"""
        response = dashscope.Generation.call(
            model=self.model,
            api_key=self.api_key,
            messages=[{'role': 'user', 'content': prompt}],
            result_format='message'
        )
        
        if response.status_code == 200:
            content = response.output.choices[0].message.content.strip()
            # 清理可能的 Markdown 标记
            sql = content.replace('```sql', '').replace('```', '').strip()
            return sql
        else:
            raise Exception(f"SQL 生成失败: {response.message}")

    def _repair_sql_with_error(self, demand: str, schema: str, failed_sql: str, error_text: str) -> str:
        """根据数据库报错修复 SQL（例如字段不存在、列名大小写错误）。"""
        prompt = f"""你是 PostgreSQL SQL 修复专家。请根据数据库结构和报错，修复失败 SQL。

数据库结构信息：
{schema}

用户需求：
{demand}

失败 SQL：
{failed_sql}

数据库报错：
{error_text}

修复要求：
1. 只输出修复后的 SQL，不要解释。
2. 必须严格使用数据库中真实存在的字段名与表名。
3. 字段名若含大写字母，必须使用双引号。
4. 若报错为“column does not exist/字段不存在”，请替换为 schema 中最匹配字段。
5. 默认查询表为 ceshen，除非 schema 显示应使用其他表。
6. 保持与原需求一致，且 SQL 可直接执行。
"""
        response = dashscope.Generation.call(
            model=self.model,
            api_key=self.api_key,
            messages=[{'role': 'user', 'content': prompt}],
            result_format='message'
        )

        if response.status_code == 200:
            content = response.output.choices[0].message.content.strip()
            return content.replace('```sql', '').replace('```', '').strip()

        logger.warning(f"SQL 修复失败，回退重生 SQL: {response.message}")
        return ''

    def _extract_requested_chart_type(self, demand: str) -> Optional[str]:
        """优先遵从用户在需求中显式指定的图表类型。"""
        if not demand:
            return None
        if '折线图' in demand or '折线' in demand:
            return 'line'
        if '柱状图' in demand or '柱形图' in demand or '柱状' in demand:
            return 'bar'
        if '饼图' in demand or '饼状图' in demand:
            return 'pie'
        if '散点图' in demand or '散点' in demand:
            return 'scatter'
        return None

    def _infer_chart_type_from_data(self, data: List[Dict[str, Any]], demand: str) -> Optional[str]:
        """规则推断图表类型，避免 LLM 调用。返回 None 表示无法确定。"""
        if not data or not isinstance(data[0], dict):
            return None
        # 用户显式指定优先
        explicit = self._extract_requested_chart_type(demand)
        if explicit:
            return explicit
        # 占比/比例 → pie
        if any(k in (demand or '') for k in ['占比', '比例', '构成', '分布']):
            return 'pie'
        # 趋势/变化/时间 → line
        if any(k in (demand or '') for k in ['趋势', '变化', '时间', '年份', '月份']):
            return 'line'
        # 多行 + 有分类字段 + 有数值字段 → bar
        row = data[0]
        str_fields = [k for k, v in row.items() if isinstance(v, str)]
        num_fields = [k for k, v in row.items() if isinstance(v, (int, float))]
        if len(data) > 1 and str_fields and num_fields:
            return 'bar'
        return None

    def _rule_based_chart_data(self, data: List[Dict[str, Any]], chart_type: str) -> Optional[List[Dict]]:
        """规则推断数据映射，避免 LLM 调用。返回 None 表示无法确定。"""
        if not data or not isinstance(data[0], dict):
            return None
        row = data[0]
        str_fields = [k for k, v in row.items() if isinstance(v, str)]
        num_fields = [k for k, v in row.items() if isinstance(v, (int, float))]
        if not num_fields:
            return None
        cat_field = str_fields[0] if str_fields else None
        val_field = num_fields[0]
        if chart_type in ('bar', 'line'):
            if cat_field:
                return [{'category': str(r.get(cat_field, '')), 'value': round(float(r.get(val_field, 0) or 0), 3)} for r in data]
            return [{'category': f'记录{i+1}', 'value': round(float(r.get(val_field, 0) or 0), 3)} for i, r in enumerate(data)]
        if chart_type == 'pie':
            if cat_field:
                return [{'name': str(r.get(cat_field, '')), 'value': round(float(r.get(val_field, 0) or 0), 3)} for r in data]
            return None
        return None

    def _is_over_depth_demand(self, demand: str) -> bool:
        if not demand:
            return False
        keywords = ['超深', '超深深度', '平均超深', '平均差值', '控制高程', 'Measured_Depth', 'Control_Elevation']
        return any(k in demand for k in keywords)

    def _pick_numeric_field(self, row: Dict[str, Any], aliases: List[str]) -> Optional[float]:
        if not isinstance(row, dict):
            return None
        lower_map = {str(k).lower(): v for k, v in row.items()}
        for alias in aliases:
            key = alias.lower()
            if key in lower_map:
                try:
                    value = lower_map[key]
                    if value is None:
                        continue
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _extract_area_name(self, demand: str) -> Optional[str]:
        if not demand:
            return None
        m = re.search(r'([\u4e00-\u9fa5A-Za-z0-9\-]+可采区)', demand)
        if m:
            return m.group(1).strip()
        return None

    def _find_field(self, row: Dict[str, Any], aliases: List[str]) -> Optional[str]:
        lower_map = {str(k).lower(): k for k in row.keys()}
        for alias in aliases:
            key = alias.lower()
            if key in lower_map:
                return lower_map[key]
        return None

    def _build_over_depth_grouped_chart(self, demand: str, data: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not data or len(data) <= 1 or not isinstance(data[0], dict):
            return None

        row = data[0]
        category_field = self._find_field(row, [
            'Mineable_Area_Name', 'mineable_area_name', 'area_name', 'name', '采区名称', '砂场名称'
        ])
        avg_field = self._find_field(row, [
            'avg_over_depth', 'avg_depth_diff', 'avg_diff', 'avg_difference',
            'avg_control_minus_measured', 'average_over_depth', 'average_diff', 'avg'
        ])
        if not category_field or not avg_field:
            return None

        point_field = self._find_field(row, ['point_count', 'count', 'total_count', '记录数', '点位数'])
        max_field = self._find_field(row, ['max_over_depth', 'max_depth_diff', 'max_diff', 'max_difference', 'max'])
        min_field = self._find_field(row, ['min_over_depth', 'min_depth_diff', 'min_diff', 'min_difference', 'min'])
        status_field = self._find_field(row, ['over_depth_status', 'status', '判定'])
        threshold = 2.0
        chart_data = []
        rows = []
        for item in data:
            name = str(item.get(category_field) or '').strip()
            if not name:
                continue
            try:
                avg_val = round(float(item.get(avg_field) or 0), 3)
            except (TypeError, ValueError):
                continue
            max_val = item.get(max_field) if max_field else None
            min_val = item.get(min_field) if min_field else None
            point_count = item.get(point_field) if point_field else None
            status = str(item.get(status_field) or ('超深度开采' if avg_val > threshold else '未构成超深度开采'))
            chart_data.append({'category': name, 'value': avg_val})
            rows.append({
                'name': name,
                'avg': avg_val,
                'max': round(float(max_val), 3) if isinstance(max_val, (int, float)) else '',
                'min': round(float(min_val), 3) if isinstance(min_val, (int, float)) else '',
                'point_count': point_count if point_count is not None else '',
                'status': status,
            })

        if not chart_data:
            return None

        year_match = re.search(r'(20\d{2})\s*年?', demand or "")
        county_name = self._extract_county_name(demand or "")
        area_scope = county_name if county_name else "各采砂场/可采区"
        year_scope = f"{year_match.group(1)}年" if year_match else ""
        title = f"{area_scope}{year_scope}各采砂场超深度开采情况"
        echarts_config = self._convert_to_echarts({
            'type': 'bar',
            'data': chart_data,
            'title': title,
        })
        if echarts_config.get('series'):
            echarts_config['series'][0]['data'] = [
                {
                    'value': item['value'],
                    'itemStyle': {'color': '#ef4444' if item['value'] > threshold else '#3b82f6'}
                }
                for item in chart_data
            ]
            echarts_config['series'][0]['markLine'] = {
                'silent': True,
                'lineStyle': {'color': '#ef4444', 'type': 'dashed'},
                'data': [{'yAxis': threshold, 'name': '超深阈值2m'}]
            }
        if echarts_config.get('yAxis'):
            echarts_config['yAxis']['name'] = '平均差值(m)'

        over_rows = [r for r in rows if r['avg'] > threshold]
        summary = (
            f"### 📊 **{title}**\n\n"
            f"判定标准：以各采砂场/可采区的平均差值 `AVG(Control_Elevation - Measured_Depth)` 为准，阈值为 2m。\n\n"
            f"本次从数据库检索到 **{len(rows)} 个** 采砂场/可采区，其中 **{len(over_rows)} 个** 判定为超深度开采。"
        )
        if over_rows:
            summary += "超深对象：" + "、".join(r['name'] for r in over_rows) + "。\n\n"
        else:
            summary += "\n\n"
        summary += "| 采砂场/可采区 | 点位数 | 平均差值(m) | 最大值(m) | 最小值(m) | 判定 |\n"
        summary += "|---|---:|---:|---:|---:|---|\n"
        for r in rows:
            summary += f"| {r['name']} | {r['point_count']} | {r['avg']} | {r['max']} | {r['min']} | {r['status']} |\n"

        return {
            'success': True,
            'chart_type': 'bar',
            'config': echarts_config,
            'message': '图表生成成功',
            'content': summary,
        }

    def _build_over_depth_triplet_chart(self, demand: str, data: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """为超深判定类需求构建固定三指标图：平均差值、最大值、最小值。"""
        if not data or not isinstance(data[0], dict):
            return None

        row = data[0]
        avg_val = self._pick_numeric_field(row, [
            'avg_over_depth', 'avg_depth_diff', 'avg_diff', 'avg_difference',
            'avg_control_minus_measured', 'average_over_depth', 'average_diff', 'avg'
        ])
        max_val = self._pick_numeric_field(row, [
            'max_over_depth', 'max_depth_diff', 'max_diff', 'max_difference', 'max'
        ])
        min_val = self._pick_numeric_field(row, [
            'min_over_depth', 'min_depth_diff', 'min_diff', 'min_difference', 'min'
        ])

        numeric_values = []
        for v in row.values():
            try:
                if v is not None:
                    numeric_values.append(float(v))
            except (TypeError, ValueError):
                continue

        if avg_val is None and numeric_values:
            avg_val = numeric_values[0]
        if max_val is None and numeric_values:
            max_val = max(numeric_values)
        if min_val is None and numeric_values:
            min_val = min(numeric_values)

        if avg_val is None:
            return None
        if max_val is None:
            max_val = avg_val
        if min_val is None:
            min_val = avg_val

        area_name = self._extract_area_name(demand) or '区域'
        chart_data = [
            {'category': '平均差值', 'value': round(float(avg_val), 3)},
            {'category': '最大值', 'value': round(float(max_val), 3)},
            {'category': '最小值', 'value': round(float(min_val), 3)},
        ]
        threshold = 2.0
        verdict = '超深度开采' if avg_val > threshold else '未构成超深度开采'
        title = f'{area_name}超深度关键指标对比'

        echarts_config = self._convert_to_echarts({
            'type': 'bar',
            'data': chart_data,
            'title': title,
        })

        summary = (
            f"### 📊 **{title}**\n\n"
            f"判定标准：以平均差值（AVG(Control_Elevation - Measured_Depth)）为准，阈值 2m。"
            f"当前平均差值为 {round(float(avg_val), 3)}m，判定为：**{verdict}**。\n\n"
            f"- 平均差值：{round(float(avg_val), 3)}m（用于判定）\n"
            f"- 最大值：{round(float(max_val), 3)}m\n"
            f"- 最小值：{round(float(min_val), 3)}m\n"
        )

        return {
            'success': True,
            'chart_type': 'bar',
            'config': echarts_config,
            'message': '图表生成成功',
            'content': summary,
        }

    def _generate_echarts_chart(self, demand: str, data: List[Dict[str, Any]], pre_chart_type: str = '') -> Dict[str, Any]:
        """生成 ECharts 配置。优先用规则推断，仅在无法确定时才调用 LLM。"""
        try:
            if self._is_over_depth_demand(demand):
                grouped_chart = self._build_over_depth_grouped_chart(demand, data)
                if grouped_chart:
                    return grouped_chart
                fixed_chart = self._build_over_depth_triplet_chart(demand, data)
                if fixed_chart:
                    return fixed_chart

            # 尝试规则推断（零 LLM 调用）
            chart_type = pre_chart_type or self._infer_chart_type_from_data(data, demand)
            chart_data = None
            title = demand[:30] if demand else '数据统计分析'
            analysis_text = None

            if chart_type:
                chart_data = self._rule_based_chart_data(data, chart_type)

            if chart_type and chart_data:
                logger.info(f"[DataVisualizerTool] Rule-based chart inference: type={chart_type}, data_points={len(chart_data)}")
            else:
                # 规则无法确定，合并调用 LLM（图表配置 + 数据分析 合并为 1 次）
                decision = self._decide_chart_and_analyze(demand, data)
                chart_type = self._extract_requested_chart_type(demand) or decision.get('chart_type', 'bar')
                chart_data = decision.get('data', [])
                title = decision.get('title', title)
                analysis_text = decision.get('analysis', None)

            if not chart_data:
                raise Exception("未能提取出有效的图表数据")

            mcp_style_config = {
                'type': chart_type,
                'data': chart_data,
                'title': title
            }
            echarts_config = self._convert_to_echarts(mcp_style_config)
            summary = self._generate_detailed_summary(data, chart_type, title, demand, analysis_text)

            return {
                'success': True,
                'chart_type': chart_type,
                'config': echarts_config,
                'message': '图表生成成功',
                'content': summary
            }
        except Exception as e:
            logger.exception(f"Chart generation failed: {e}")
            return {'success': False, 'error': f"图表生成失败: {str(e)}"}

    def _decide_chart_and_analyze(self, demand: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """合并图表决策 + 数据分析为单次 LLM 调用（原来需要 2 次）。"""
        prompt = f"""你是一个数据分析与可视化专家。请根据用户需求和查询到的数据，完成以下两件事：
1. 决定最合适的图表类型并提取转换数据
2. 提供一段简短的数据分析总结（约 100-200 字）

用户原始需求：
{demand}

数据样本（共 {len(data)} 条，展示前 10 条）：
{json.dumps(data[:10], ensure_ascii=False)}

请返回一个 JSON 对象，严格包含以下字段：
1. 'chart_type': 字符串，可选值：'bar' (柱状图), 'line' (折线图), 'pie' (饼图), 'scatter' (散点图)。
2. 'title': 简洁的图表标题。
3. 'data': 转换后的数据列表。
   - 如果是 'bar' 或 'line'，列表项格式为: {{"category": "类别名称", "value": 数值}}
   - 如果是 'pie'，列表项格式为: {{"name": "项目名称", "value": 数值}}
   - 如果是 'scatter'，列表项格式为: {{"x": 数值1, "y": 数值2}}
4. 'analysis': 字符串，简短的数据分析总结（100-200字）。

重要规则：
- 只返回 JSON 对象，不要有任何 Markdown 代码块标记或解释文字。
- 必须包含所有查询到的数据条目（当前共 {len(data)} 条），不要只提取样本。
- 确保数值(value/x/y)是纯数字，保留三位小数。
- 分析总结中：如涉及超深度，判定标准为平均差值 > 2m。
"""
        response = dashscope.Generation.call(
            model=self.model,
            api_key=self.api_key,
            messages=[{'role': 'user', 'content': prompt}],
            result_format='message'
        )

        if response.status_code == 200:
            content = response.output.choices[0].message.content.strip()
            content = content.replace('```json', '').replace('```', '').strip()
            return json.loads(content)
        else:
            raise Exception(f"图表决策失败: {response.message}")

    def _convert_to_echarts(self, mcp_config: Dict[str, Any]) -> Dict[str, Any]:
        """将 MCP Chart 配置转换为 ECharts 配置"""
        chart_type = mcp_config.get('type', 'bar')
        data = mcp_config.get('data', [])
        title = mcp_config.get('title', '')
        
        # 基础配置，设置默认中文字体
        font_family = 'Noto Sans CJK SC, WenQuanYi Micro Hei, sans-serif'
        base_option = {
            'title': {
                'text': title,
                'left': 'center',
                'textStyle': {
                    'fontFamily': font_family,
                    'fontSize': 14,
                    'overflow': 'break',    # 超出宽度自动换行
                    'width': 320           # 标题最大宽度（px），超出则换行
                }
            },
            'tooltip': {'trigger': 'axis' if chart_type in ['bar', 'line'] else 'item'},
            'legend': {'orient': 'horizontal', 'bottom': 10, 'textStyle': {'fontFamily': font_family}},
            'grid': {'left': '3%', 'right': '4%', 'top': '18%', 'bottom': '15%', 'containLabel': True},
            'textStyle': {'fontFamily': font_family}
        }

        if chart_type in ['bar', 'line']:
            categories = [str(item.get('category', '')) for item in data]
            values = []
            for item in data:
                val = item.get('value', 0)
                # 所有数值保留三位小数
                if isinstance(val, (int, float)):
                    val = round(float(val), 3)
                values.append(val)
            
            base_option.update({
                'xAxis': {
                    'type': 'category',
                    'data': categories,
                    'axisLabel': {
                        'interval': 0, 
                        'rotate': 30 if len(categories) > 5 else 0,
                        'textStyle': {'fontFamily': font_family}
                    }
                },
                'yAxis': {
                    'type': 'value',
                    'axisLabel': {'textStyle': {'fontFamily': font_family}}
                },
                'series': [{
                    'name': title,
                    'type': chart_type,
                    'data': values,
                    'label': {
                        'show': True, 
                        'position': 'top',
                        'formatter': '{c}',
                        'textStyle': {'fontFamily': font_family}
                    }
                }]
            })
        elif chart_type == 'pie':
            series_data = []
            for item in data:
                val = item.get('value', 0)
                if isinstance(val, (int, float)):
                    val = round(float(val), 3)
                series_data.append({'name': str(item.get('name', '')), 'value': val})
            
            base_option.update({
                'tooltip': {'trigger': 'item', 'formatter': '{a} <br/>{b} : {c} ({d}%)', 'textStyle': {'fontFamily': font_family}},
                'series': [{
                    'name': title,
                    'type': 'pie',
                    'radius': '55%',
                    'center': ['50%', '50%'],
                    'data': series_data,
                    'label': {
                        'formatter': '{b}: {c} ({d}%)',
                        'textStyle': {'fontFamily': font_family}
                    },
                    'emphasis': {
                        'itemStyle': {
                            'shadowBlur': 10,
                            'shadowOffsetX': 0,
                            'shadowColor': 'rgba(0, 0, 0, 0.5)'
                        }
                    }
                }]
            })
        elif chart_type == 'scatter':
            series_data = []
            for item in data:
                x = item.get('x', 0)
                y = item.get('y', 0)
                if isinstance(x, (int, float)): x = round(float(x), 3)
                if isinstance(y, (int, float)): y = round(float(y), 3)
                series_data.append([x, y])
            
            base_option.update({
                'xAxis': {},
                'yAxis': {},
                'series': [{
                    'symbolSize': 20,
                    'data': series_data,
                    'type': 'scatter'
                }]
            })
        
        return base_option

    def _format_over_depth_answer(self, demand: str, data: List[Dict[str, Any]]) -> Optional[str]:
        """对平均超深深度生成标准化答案：不使用负值，提供方向说明"""
        try:
            # 提取采区名称
            area = None
            m = re.search(r'([\u4e00-\u9fa5\-]+可采区)', demand or "")
            if m:
                area = m.group(1).strip()
            
            # 如果数据列表为空
            if not data:
                return None

            # 仅处理单行数据（单个采区或总体统计）的情况，多行数据交由图表分析处理
            if len(data) != 1:
                return None

            row = data[0]
            
            # 获取统计值
            avg_val = 0.0
            max_val = 0.0
            violation_count = 0
            
            # 尝试获取平均值
            if 'avg_over_depth' in row:
                avg_val = float(row['avg_over_depth'] or 0)
            else:
                 # 回退旧逻辑
                 nums = [float(v) for v in row.values() if isinstance(v, (int, float))]
                 if len(nums) == 1:
                     avg_val = nums[0]
            
            # 获取最大值和违规数（如果 SQL 返回了这些字段）
            if 'max_over_depth' in row:
                 max_val = float(row['max_over_depth'] or 0)
 
            name_text = area or "该区域"
            abs_avg = round(abs(avg_val), 2)
            direction = "低了" if avg_val >= 0 else "高了" # val > 0 implies Measured < Control (Lower)
 
             # 构造回答：根据平均差值判定是否为超深度开采
            if avg_val > 2:
                 return f"{name_text}的监测结果显示存在超深度开采情况。该区域所有测深点的平均高程比控制高程低 {abs_avg} 米（平均差值超过 2m 的违规标准）。区域内最大局部超深为 {max_val} 米。"
            else:
                 return f"{name_text}的监测结果显示未构成超深度开采（整体平均差值未超过 2m）。该区域平均偏差为 {abs_avg} 米（实测深度平均比控制高程{direction} {abs_avg} 米）。"
 
        except Exception as e:
            logger.error(f"Format answer error: {e}")
            return None

    def _generate_detailed_summary(self, data: List[Dict[str, Any]], chart_type: str, title: str, demand: str, analysis_text: Optional[str] = None) -> str:
        """生成包含数据表格和分析的 Markdown 摘要。
        如果 analysis_text 已由合并 LLM 调用提供，直接使用，不再额外调 LLM。"""
        chart_emoji = {
            'bar': '📊',
            'line': '📈',
            'pie': '🥧',
            'scatter': '🌌'
        }.get(chart_type, '📊')

        md = f"### {chart_emoji} **{title}**\n\n"
        md += f"已根据您的需求“{demand}”生成了可视化图表。共分析了 {len(data)} 条数据记录。\n\n"

        # 添加数据表格
        if data:
            headers = list(data[0].keys())
            md += "#### 📋 数据表格\n\n"
            md += "| " + " | ".join(headers) + " |\n"
            md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
            for row in data[:15]:
                row_values = []
                for h in headers:
                    val = row.get(h, '')
                    if isinstance(val, (int, float)):
                        val = round(float(val), 3)
                    row_values.append(str(val))
                md += "| " + " | ".join(row_values) + " |\n"

            if len(data) > 15:
                md += f"\n*注：仅展示前 15 条数据（共 {len(data)} 条）。*\n"

        md += "\n#### 💡 数据分析\n\n"
        if analysis_text and analysis_text.strip():
            md += analysis_text.strip()
        else:
            md += "数据分析完成，请查看上方图表和表格。"

        return md
