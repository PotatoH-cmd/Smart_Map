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

        logger.info(f"DataVisualizerTool called with demand: {demand}")

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
                future = executor.submit(_run_async_in_thread, self._async_call(demand))
                return future.result()
        except Exception as e:
            logger.exception(f"DataVisualizerTool execution failed: {e}")
            return {'success': False, 'error': str(e)}

    async def _async_call(self, demand: str) -> Dict[str, Any]:
        # 1. 获取数据库 Schema，用于辅助生成 SQL
        schema_res = self.pg_tool._get_db_schema()
        if not schema_res['success']:
            return {'success': False, 'error': f"获取数据库结构失败: {schema_res.get('error')}"}
        schema_text = schema_res['data']['formatted_schema']

        # 2. 将需求转换为 SQL
        sql = self._generate_sql(demand, schema_text)
        logger.info(f"Generated SQL: {sql}")

        # 3. 执行 SQL 获取数据
        query_res = self.pg_tool._query(sql, [])
        if not query_res['success']:
            return {'success': False, 'error': f"数据查询失败: {query_res.get('error')}"}
        
        data = query_res['data']
        if not data:
            return {'success': False, 'error': '查询结果为空，无法生成图表'}

        # 4. 针对“超深深度”需求提供标准化文本答案（不使用负值）
        if any(k in (demand or "") for k in ["超深", "超深深度", "平均超深"]):
            formatted = self._format_over_depth_answer(demand, data)
            if formatted:
                return {
                    'success': True,
                    'formatted_answer': formatted
                }
        
        # 5. 默认生成 ECharts 图表配置
        return self._generate_echarts_chart(demand, data)

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
5. 限制返回条数，除非用户有特殊要求，否则最多返回 50 条。
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

    def _generate_echarts_chart(self, demand: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """直接生成 ECharts 配置"""
        try:
            # 使用 LLM 决定图表类型和数据映射
            decision = self._decide_chart_config(demand, data)
            chart_type = decision.get('chart_type', 'bar')
            chart_data = decision.get('data', [])
            title = decision.get('title', '数据统计分析')
            
            if not chart_data:
                raise Exception("LLM 未能提取出有效的图表数据")

            # 构造统一的配置对象，模拟之前 MCP 的返回结构，以便复用 _convert_to_echarts
            mcp_style_config = {
                'type': chart_type,
                'data': chart_data,
                'title': title
            }
            
            # 转换为前端可用的 ECharts 配置
            echarts_config = self._convert_to_echarts(mcp_style_config)
            
            # 构造摘要文本（包含数据表格）
            summary = self._generate_detailed_summary(data, chart_type, title, demand)
            
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

    def _decide_chart_config(self, demand: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """使用 LLM 决定图表类型、标题以及如何映射数据"""
        prompt = f"""你是一个数据分析与可视化专家。请根据用户需求和查询到的数据，决定最合适的图表类型，并提取转换数据。

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

注意：
- 只返回 JSON 对象，不要有任何 Markdown 代码块标记或解释文字。
- 必须包含所有查询到的数据条目（当前共 {len(data)} 条），不要只提取样本。
- 确保数值(value/x/y)是纯数字。
- 特别注意：请确保所有数值(value, x, y等)均保留三位小数。
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
                'textStyle': {'fontFamily': font_family}
            },
            'tooltip': {'trigger': 'axis' if chart_type in ['bar', 'line'] else 'item'},
            'legend': {'orient': 'horizontal', 'bottom': 10, 'textStyle': {'fontFamily': font_family}},
            'grid': {'left': '3%', 'right': '4%', 'bottom': '15%', 'containLabel': True},
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

    def _generate_detailed_summary(self, data: List[Dict[str, Any]], chart_type: str, title: str, demand: str) -> str:
        """生成包含数据表格和分析的 Markdown 摘要"""
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
            for row in data[:15]:  # 最多展示15行
                row_values = []
                for h in headers:
                    val = row.get(h, '')
                    # 只要是数值类型，就保留三位小数
                    if isinstance(val, (int, float)):
                        val = round(float(val), 3)
                    row_values.append(str(val))
                md += "| " + " | ".join(row_values) + " |\n"
            
            if len(data) > 15:
                md += f"\n*注：仅展示前 15 条数据（共 {len(data)} 条）。*\n"
        
        md += "\n#### 💡 数据分析\n\n"
        # 使用 LLM 生成简短的数据分析描述
        analysis = self._generate_data_analysis(data, demand)
        md += analysis
        
        return md

    def _generate_data_analysis(self, data: List[Dict[str, Any]], demand: str) -> str:
        """使用 LLM 生成简短的数据分析描述"""
        prompt = f"""请根据以下数据和用户需求，提供一段简短的分析总结（约 100-200 字）。
        需求：{demand}
        数据样本：{json.dumps(data[:10], ensure_ascii=False)}
        
        重要规则：
        1. 重点关注平均超深（avg_over_depth）。
        2. 判断标准更新：只有当整个区域的平均差值 (Control_Elevation - Measured_Depth 的平均值) > 2米 时，才定义为“超深度开采”。
        3. 如果 avg_over_depth <= 2，即使存在个别点位局部超深（max_over_depth > 2），也不能将整个区域定性为“超深度开采”，应表述为“整体未超深，但存在局部偏低”。
        4. 请对比平均情况与极端情况（最大超深）。
        """
        try:
            response = dashscope.Generation.call(
                model=self.model,
                api_key=self.api_key,
                messages=[{'role': 'user', 'content': prompt}],
                result_format='message'
            )
            if response.status_code == 200:
                return response.output.choices[0].message.content.strip()
        except:
            pass
        return "数据分析完成，请查看上方图表和表格。"
