import os
import time
import json
import logging
from typing import Dict, Any, Union
from docx import Document
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)

@register_tool('report_generator_tool')
class ReportGeneratorTool(BaseTool):
    description = '''
    报告生成工具。用于根据提供的变量数据，基于固定模板生成 Word 文档报告。
    使用场景：当用户请求生成报告、文档或总结时使用。
    
    使用步骤：
    1. 你的上游步骤（如 PostgreSQLTool）应该已经获取了必要的数据。
    2. 你需要整理这些数据，并生成用于填充模板的变量文本（如 summary, details, conclusion 等）。
    3. 调用此工具，传入 variables 字典。
    '''
    
    parameters = [
        {
            'name': 'variables',
            'type': 'object',
            'description': '用于替换模板中占位符的变量字典。Key为占位符名称（不含花括号），Value为替换文本。例如：{"report_title": "xx分析报告", "summary": "..."}',
            'required': True
        },
        {
            'name': 'template_name',
            'type': 'string',
            'description': '模板文件名（默认为 report_template.docx）',
            'required': False
        }
    ]

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {'success': False, 'error': 'Invalid JSON parameters'}

        variables = params.get('variables', {})
        template_name = params.get('template_name', 'report_template.docx')
        
        # 路径配置
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template_path = os.path.join(base_dir, 'templates', template_name)
        output_dir = os.path.join(base_dir, 'static', 'reports')
        
        if not os.path.exists(template_path):
            return {'success': False, 'error': f'Template {template_name} not found at {template_path}'}
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        try:
            doc = Document(template_path)
            
            # 替换段落中的文本
            for paragraph in doc.paragraphs:
                self._replace_text_in_paragraph(paragraph, variables)
            
            # 替换表格中的文本
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            self._replace_text_in_paragraph(paragraph, variables)

            # 生成唯一文件名
            timestamp = int(time.time())
            filename = f"report_{timestamp}.docx"
            output_path = os.path.join(output_dir, filename)
            
            doc.save(output_path)
            
            # 返回可访问的 URL（假设后端挂载了 static 目录）
            # 注意：这里的 IP/端口 可能需要根据实际情况调整，或者只返回相对路径
            download_url = f"/static/reports/{filename}"
            
            return {
                'success': True,
                'message': '报告生成成功',
                'file_path': output_path,
                'download_url': download_url,
                'filename': filename
            }
            
        except Exception as e:
            logger.error(f"Error generating report: {e}")
            return {'success': False, 'error': str(e)}

    def _replace_text_in_paragraph(self, paragraph, variables):
        """简单替换段落中的占位符"""
        if '{{' not in paragraph.text:
            return
            
        # 这是一个简化的替换逻辑，可能不支持跨 run 的占位符
        # 对于复杂情况，可以使用 python-docx-template
        for key, value in variables.items():
            placeholder = f"{{{{{key}}}}}"
            if placeholder in paragraph.text:
                # 简单替换：直接替换 paragraph.text 会丢失样式，但最简单
                # 为了保留样式，最好是在 run 级别操作，但这里为了简单起见先这样
                # 如果要保留样式，需要遍历 runs
                
                # 尝试保留样式的替换
                for run in paragraph.runs:
                    if placeholder in run.text:
                        run.text = run.text.replace(placeholder, str(value))
                
                # 如果上面的 run 级替换没生效（说明占位符跨 run 了），则回退到段落级替换
                if placeholder in paragraph.text:
                     paragraph.text = paragraph.text.replace(placeholder, str(value))
