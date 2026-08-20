"""
MapAgent — 地图操作专职 Agent。
覆盖：MAP_DISPLAY, LOCATION_SEARCH, COORDINATE_MARKER, SPATIAL_PROCESSING, SPATIAL_REFERENCE, SPATIAL_ANALYSIS
"""
from typing import List

from .intent_types import IntentType
from .base_agent import BaseAgent


class MapAgent(BaseAgent):
    """地图操作 Agent：2D/3D 视图切换、矢量加载、空间坐标处理。"""

    @property
    def intent(self) -> IntentType:
        return IntentType.MAP_DISPLAY

    @property
    def tool_names(self) -> List[str]:
        return [
            "map_tool",
            "cesium_tool",
            "location_search",
            "coordinate_marker",
            "spatial_processing_tool",
            "spatial_reference_tool",
            "qgis_mcp_tool",
        ]

    def build_system_prompt(self, schema_text: str = "") -> str:
        return f"""## 地图操作领域规则

### 工具参数约束
1. `map_tool`（2D地图）的 action 类型：
   - 切换底图：action="switch_layer"，layer="satellite"（卫星影像）、"arcgis"（高分影像）或 "osm"（街道地图）
   - 加载矢量图层：action="load_vector_layer"，需提供 table_name、filter、layer_name
   - 添加标记：action="add_marker"，需提供 lat、lng
   - 清除标记：action="clear_markers"
   - 设置视图：action="set_view"，需提供 lat、lng、zoom

2. `cesium_tool`（3D地图）的 action 类型：
   - 切换底图：action="setBasemap"，basemap="satellite"（卫星）、"osm"（街道）或 "tianditu"（天地图）
   - 飞行到位置：action="flyTo"，需提供 lat、lng
   - 添加GeoJSON图层：action="addGeoJsonLayer"，需提供 table_name
   - 清除所有：action="clearAll"

3. `spatial_processing_tool`（空间数据处理）的参数：
   - 生成面：{{"action": "generate_polygon", "coordinates": [[x1,y1],[x2,y2],...], "source_crs": "4526"}}
   - 生成线：{{"action": "generate_polyline", ...}}
   - 生成点：{{"action": "generate_points", ...}}
   - XY交换：当用户说"XY相反"时，设置 "swap_xy": true
   - 坐标系对照：CGCS2000 Zone 38N = EPSG:4526（推荐），也可用 EPSG:4547；WGS84 = EPSG:4326
   - ⚠️ EPSG:4497 是 6 度带第 19 带（CM 111°E），**不是** 3 度带第 38 带，切勿混淆
   - 当用户提到"38带号"、"CGCS2000"、"2000国家大地坐标系"、"投影坐标"时，source_crs 应为 "4526"
   - ⚠️ 坐标顺序：coordinates 应为 [[easting, northing], ...] 即 [[Y含带号, X], ...]
   - ⚠️ 从 OCR 文本/Excel 中提取坐标时：必须提取**全部**坐标点，不要遗漏角点
   - ⚠️ 如果坐标提取不完整或转换失败，**不要猜测替代方案**（如 cesium_tool），直接告知用户错误

4. `spatial_reference_tool`（空间参考数据查询）：
   - 当用户说"红线""河道红线""红线范围内""红线附近"时，意图应为 spatial_reference，关联 hx 图层
   - 当用户说"采区""可采区""采区范围"时，意图应为 spatial_reference，关联 caiqu 图层
   - 关键词映射：
     - "红线""河道红线""管理红线" → layer='hx'
     - "采区""可采区""许可范围" → layer='caiqu'
   - 执行计划：① spatial_reference_tool 获取几何 → ② 将几何传给 postgresql_tool(spatial_query) 做空间筛选
   - **极其重要的区分**：上述规则仅适用于"获取边界/做空间筛选"场景；
     若用户问"XX可采区/砂场的（控制开采）高程/深度/数量是多少"，这是数值数据查询，
     必须判为 data_query 并用 postgresql_tool 查 ceshen 表，严禁直接编造数值作答。

5. ⭐ `qgis_mcp_tool`（QGIS空间分析引擎）—— 处理已有图层的空间运算：
   参数格式：{{"category": "analysis", "action": "buffer", "params": {{"distance": 100, "output_path": "/output/result.geojson"}}}}
   
   必须使用的场景（关键词触发）：
   - "缓冲区/buffer" → {{"category": "analysis", "action": "buffer", "params": {{"distance": 100, "output_path": "/output/buffer.geojson"}}}}
   - "裁剪/clip" → {{"category": "analysis", "action": "clip", "params": {{"clip_layer": "边界"}}}}
   - "面积计算/面积" → 先用 layer/get_statistics 或直接算
   - "叠加分析/相交/intersection" → {{"category": "analysis", "action": "intersection", "params": {{"overlay_layer": "other"}}}}
   - "分区统计/zonal" → {{"category": "analysis", "action": "zonal_statistics", "params": {{"zones": "redline"}}}}
   - "空间关联/空间连接" → {{"category": "analysis", "action": "spatial_join", "params": {{"join_layer": "other"}}}}

   常用操作流程：
   ① 先加载数据：{{"category": "layer", "action": "add_vector", "params": {{"path": "/gis_data/xxx.shp", "layer_name": "xxx"}}}}
   ② 再执行分析：{{"category": "analysis", "action": "buffer", "params": {{"layer_name": "xxx", "distance": 100, "output_path": "/output/buffer.geojson"}}}}
   ③ 结果会自动导出到 /output/ 目录

   ⚠️ spatial_processing_tool 用于"从原始坐标生成矢量"，qgis_mcp_tool 用于"对已有图层做空间运算"，不要混淆！
   ⚠️ 如果用户提供了SHP路径（如 /gis_data/xxx.shp、/uploads/xxx.shp），必须用 qgis_mcp_tool 而非 spatial_processing_tool
   ⚠️ 缓冲区、裁剪、叠加、面积计算、分区统计 → 全部是 qgis_mcp_tool 的职责
   ⚠️ 结果文件路径填 /output/xxx.geojson，后端可自动读取

### execution_plan 规范
- execution_plan 中每一步的 tool 字段**必须**填写对应工具名称字符串，**绝对不能**留空或为 null
- 注意：spatial_processing_tool 会自动生成 GeoJSON 并返回 load_vector_layer 指令，使用后**不要再调用 map_tool 执行 load_vector_layer**，避免重复加载
- qgis_mcp_tool 的结果已写入 /output/ 目录，可直接用 map_tool 加载到地图

### 常见任务速查
- "切换到卫星图层"：map_tool（2D）或 cesium_tool（3D）
- "加载SHP/加载图层"：qgis_mcp_tool（layer/add_vector），然后 map_tool 显示
- "缓冲区/裁剪/叠加/面积计算"：⭐ qgis_mcp_tool（analysis/...）
- "查找某地位置"：location_search
- "清除地图"：map_tool，action="clear_markers"
- "XY相反/投影坐标/带号/生成矢量"：spatial_processing_tool
- "红线/采区附近的数据"：spatial_reference_tool → postgresql_tool
"""

    def build_response_prompt(self) -> str:
        return (
            "你是地图分析助手，正在汇总地图操作结果。\n"
            "规则：\n"
            "- 只描述实际执行成功的操作结果\n"
            "- 如果工具返回错误(含'success':false)，必须如实告知用户失败，不要编造成功结果\n"
            "- 如果执行计划中的工具全部失败，回复格式：'抱歉，[操作名]失败了：{错误原因}'\n"
            "- 回答要简洁明了，统计信息用数字量化"
        )
