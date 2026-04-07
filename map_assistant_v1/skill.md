# 智能一张图 (Map Assistant) Skill

## 简介
Map Assistant 是一个基于 AI 的智能地图助手，允许用户通过自然语言指令控制地图展示、查询地理位置、绘制形状以及加载矢量数据。该 Skill 封装了地图操作和位置查询的核心能力。

## 核心工具 (Tools)

### 1. 地图操作工具 (`map_tool`)
用于直接控制地图的前端展示和交互。支持的操作 (`action`) 包括：

*   **标记与覆盖物**
    *   `add_marker`: 在指定坐标添加标记 (参数: `lat`, `lng`, `title`, `popup`, `color`)
    *   `add_circle`: 添加圆形区域 (参数: `lat`, `lng`, `radius`, `color`) n'j
    *   `add_circles`: 批量添加圆形
    *   `add_polygon`: 绘制多边形 (参数: `coordinates` list of [lat, lng], `color`)
    *   `add_polyline`: 绘制折线 (参数: `coordinates`, `color`, `weight`)
    *   `clear_markers`: 清除地图上的所有标记和覆盖物

*   **视图控制**
    *   `set_view`: 设置地图中心点和缩放级别 (参数: `lat`, `lng`, `zoom`)
    *   `fit_markers`: 自动调整视图以适应当前所有标记的范围
    *   `switch_layer`: 切换底图图层 (参数: `layer`: 'satellite' | 'osm')

*   **高级数据加载**
    *   `load_vector_layer`: 从数据库加载矢量图层数据
        *   参数: `table_name` (表名), `layer_name` (显示名称), `filter` (SQL过滤条件), `color_expression` (颜色规则), `geom_col` (几何列名)

### 2. 位置搜索工具 (`location_search`)
用于将自然语言地名转换为地理坐标。
*   功能: 根据地名查找经纬度。
*   参数: `location_name` (地名或地标名称)
*   数据源: 内置常见城市和地标数据库 (如北京、上海、天安门、东方明珠等)。

## 使用示例

### 基础控制
*   "将地图移动到北京 (39.9042, 116.4074)" -> `map_tool(action="set_view", lat=39.9042, lng=116.4074)`
*   "切换到卫星地图" -> `map_tool(action="switch_layer", layer="satellite")`

### 标记与绘图
*   "在天安门广场标记一个点" -> 先调用 `location_search` 获取坐标，再调用 `map_tool(action="add_marker", ...)`
*   "在当前位置画一个半径500米的红色圆" -> `map_tool(action="add_circle", radius=500, color="red", ...)`
*   "连接北京和上海" -> `map_tool(action="add_polyline", coordinates=[[39.9, 116.4], [31.2, 121.5]])`

### 数据分析
*   "加载 mining_areas 表的数据，显示可采区" -> `map_tool(action="load_vector_layer", table_name="mining_areas", ...)`

## 项目结构参考
*   后端逻辑: `backend/tools/map_tool.py`
*   API 服务: `backend/main.py`
*   前端组件: `frontend/src/components/MapComponent.jsx`
