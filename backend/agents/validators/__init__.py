"""
validators/ — 按工具拆分的校验器（阶段2）。

- db_validators：postgresql_tool / knowledge_base_tool 数据校验
- gis_validators：qgis_mcp_tool 的 CRS 与 GeoJSON 校验
- report_validators：report_generator_tool / data_visualizer_tool 产出物校验

校验函数签名统一为 fn(arg: Dict, state: Dict) -> List[Check]，由
rules_gateway.RulesGateway 按工具名注册执行。
"""
