# 项目总览（CLAUDE）

本项目是“地图助手”与“知识检索/数据分析”的一体化系统，提供地图展示、数据库查询、知识库检索、数据可视化与报告生成。后端采用 FastAPI，前端采用 React + Leaflet，辅以工具式能力（map_tool、postgresql_tool、knowledge_base_tool、data_visualizer_tool、report_generator_tool）由助手统一编排。

## 技术栈
- 后端：FastAPI，JSON/StreamingResponse，GZip 压缩，CORS
- 数据库：PostgreSQL + PostGIS（矢量数据）
- 地图：Leaflet（底图 Esri/OSM），支持 GeoJSON 与矢量瓦片 MVT
- 前端：React 组件化，Leaflet 控件与图层管理
- AI 能力：Qwen Agent 工具编排；DashScope 用于 SQL/图表决策与摘要

## 目录与关键文件
- 后端入口：[main.py](file:///home/server/python/map_assistant_v1/backend/main.py)
- 工具层：
  - 地图工具：[map_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/map_tool.py)
  - 数据库工具：[postgresql_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/postgresql_tool.py)
  - 知识库工具：[knowledge_base_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/knowledge_base_tool.py)
  - 数据可视化工具：[data_visualizer_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/data_visualizer_tool.py)
  - 报告工具（如存在）：[report_generator_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/report_generator_tool.py)
- 前端地图组件：[MapComponent.jsx](file:///home/server/python/map_assistant_v1/frontend/src/components/MapComponent.jsx)
- 前端知识库组件：[KnowledgeBaseManager.jsx](file:///home/server/python/map_assistant_v1/frontend/src/components/KnowledgeBaseManager.jsx)
- 配置/缓存：
  - 数据库结构缓存：[db_schema.json](file:///home/server/python/map_assistant_v1/backend/config/db_schema.json)
  - 后端日志：[backend.log](file:///home/server/python/map_assistant_v1/backend/backend.log)

## 后端 API 概览
- 健康与建议：
  - GET /：运行状态
  - GET /suggestions：常用指令建议
- 矢量数据：
  - GET /api/vector-data：按表/字段/过滤条件返回 GeoJSON
    - 支持 properties、filter、color_expression
    - 返回 JSON，附带 Cache-Control
  - GET /api/mvt/{z}/{x}/{y}：返回 MVT（pbf）矢量瓦片
    - 支持 table_name、geom_col、properties、filter
    - Content-Type: application/x-protobuf
- 知识库：
  - GET /api/knowledge：文档列表
  - GET /api/knowledge/{document_id}：文档内容（拼接段）
  - POST /api/knowledge：新增文档（高质量向量化）
- 对话：
  - POST /chat：助手对话，返回 response、messages、map_commands、charts
  - POST /chat/stream：SSE 流式输出

## 地图与可视化工作流
- 地图加载（前端）
  - 通过 MapManager.addVectorLayerFromAPI 加载 GeoJSON
  - 通过 MapManager.addVectorTileLayerFromAPI 加载 MVT（Leaflet.VectorGrid）
  - 支持底图切换、图层组、弹窗/注记、fitBounds 自动缩放
- 数据可视化（后端工具）
  - data_visualizer_tool 将自然语言需求转换为 SQL
  - 执行查询后生成 ECharts 配置与 Markdown 摘要
  - 后端 /chat 汇聚 charts 字段，前端可直接渲染

## 助手行为规范（提示词要点）
- 需求分析与任务拆分优先：明确问题类型、目标输出、约束条件，列出子任务与对应工具
- 工具顺序：
  - 文档/流程：优先 knowledge_base_tool 搜索并结构化输出
  - 数据分析：postgresql_tool 获取数据 → data_visualizer_tool 生成图表
  - 地图展示：map_tool 加载矢量图层（含筛选/颜色表达式）
- 严禁误调用：
  - 未请求报告时不得调用报告工具
  - 政策/流程类问题不得查询数据库或上图
- 输出格式：
  - 使用结构化小标题与要点列表；首段直给结论与关键信息
  - 图表任务必须返回可视化（charts）与数据表摘要

## 部署与运行
- 启动后端：
  - 环境：确保 psycopg2-binary、fastapi、uvicorn、httpx 可用
  - 命令：`python backend/main.py` 或通过你的环境启动脚本
  - 端口：默认 8006
- 前端（示例）：
  - 以项目现有前端为准；组件在 src/components 目录
  - 地图容器需初始化 MapComponent，并按需要触发 map_commands

## 环境变量与安全
- DASHSCOPE_API_KEY：用于 SQL 生成与摘要生成
- DIFY_API_BASE / DIFY_KNOWLEDGE_API_KEY / DIFY_DATASET_ID：用于知识库检索与入库
- PostgreSQL 连接信息请通过安全方式传入，避免硬编码
- 切记不要在仓库中提交任何真实密钥或密码

## 性能优化与故障排查
- 地图平滑度：
  - Leaflet 参数优化：zoomAnimation、preferCanvas、updateWhenZooming、keepBuffer
  - 后端压缩与缓存：GZipMiddleware、Cache-Control 头
  - 大数据改 MVT；前端使用 VectorGrid Canvas 渲染
- 知识库检索异常：
  - 工具内置降级策略：检索失败时尝试最简请求，并返回详细错误
  - 检查 API Base、Key 与 Dataset 权限
- 数据库错误：
  - 统一返回空 FeatureCollection；避免越界错误
  - 对名称过滤进行规范化（例如“童庙-老李集可采区”）

## 开发规范
- 字段名大小写敏感：SQL 中需使用双引号包裹（例："Mineable_Area_Name"）
- 前端与后端协定：
  - 后端 /chat 返回 map_commands 与 charts，前端分别按地图/图表渲染
  - 知识库文档内容通过 /api/knowledge/{id} 拉取
- 输出规范：
  - 文档类问题结构化输出（标题+要点）
  - 数据分析返回图表配置与简明摘要

## 示例任务拆分
- 需求：筛选固始县采砂场，统计各砂场平均深度，表格 + 折线图展示
  - 子任务：
    - 用 postgresql_tool 获取 schema → 生成并执行 SQL（筛选“固始县”，分组计算平均深度）
    - 用 data_visualizer_tool 按“折线图 + 表格”生成配置与摘要
    - 前端读取 charts 渲染图表，并在文本区展示摘要表格

## 重要文件索引
- 后端入口与路由：[main.py](file:///home/server/python/map_assistant_v1/backend/main.py)
- 地图工具与命令：[map_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/map_tool.py)
- 数据库工具（查询/结构/缓存）：[postgresql_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/postgresql_tool.py)
- 知识库工具（检索/入库/内容）：[knowledge_base_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/knowledge_base_tool.py)
- 可视化工具（SQL 生成/图表配置）：[data_visualizer_tool.py](file:///home/server/python/map_assistant_v1/backend/tools/data_visualizer_tool.py)
- 前端地图组件：[MapComponent.jsx](file:///home/server/python/map_assistant_v1/frontend/src/components/MapComponent.jsx)
- 前端知识库组件：[KnowledgeBaseManager.jsx](file:///home/server/python/map_assistant_v1/frontend/src/components/KnowledgeBaseManager.jsx)

