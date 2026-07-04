# Map Assistant 后端 API 说明文档

本文档基于 `backend/main.py`（FastAPI 1.0.0）整理，覆盖目前对外暴露的全部 HTTP/SSE/WebSocket 接口。

- 默认监听端口：`PORT` / `APP_PORT` 环境变量，未设置时为 `8006`
- 跨域：CORS `*`，允许所有方法/头
- 压缩：除 `/chat/stream`（SSE）外全局启用 GZip
- 静态资源：`/static/**` 映射到 `backend/static/`（截图、报告等）

---

## 1. 通用与首页

### GET `/`
健康检查。
- 返回：`{"message": "Map Assistant API is running"}`

### GET `/suggestions`
获取首页推荐问句。
- 返回：`{"suggestions": [string, ...]}`

---

## 2. 智能体对话 (Chat)

### POST `/chat`
非流式对话接口，返回完整结果。

请求体（`ChatRequest`）：
```json
{
  "messages": [{"role": "user|assistant|system|function", "content": "..."}],
  "active_view": "map | cesium | kb",
  "session_id": "可选，会话ID，用于 MemorySaver thread_id 隔离"
}
```
请求头可选：`X-Session-ID: <uuid>`，优先级高于 body.session_id。

响应（`ChatResponse`）：
```json
{
  "response": "助手最终文本",
  "messages": [...],            // 完整工具调用与回复轨迹
  "map_commands": [...],        // 2D Leaflet 指令（已去重/优化）
  "cesium_commands": [...],     // 3D Cesium 指令
  "charts": [...],              // ECharts 配置
  "intent_info": { ... } | null // IntentAgent 结果（启用时）
}
```

行为说明：
- 自动注入"实测高程/控制高程"业务规则补丁（关键词：超深/高程/深度…）
- 根据 `active_view` 自动注入系统提示，引导使用 `map_tool` 或 `cesium_tool`
- 通过环境变量 `USE_INTENT_AGENT=true|false` 切换 IntentAgent 路径

### POST `/chat/stream`
SSE 流式对话。响应 `Content-Type: text/event-stream`。

事件载荷类型：
- `{"type":"stage", "stage":"...", ...}`：阶段进度
- `{"type":"delta", "content":"..."}`：增量文本
- `{"type":"final", "result": ChatResponse}`：最终结构化结果
- `{"type":"error", "stage":"error", "message":"..."}`：错误

最后以 `data: [DONE]` 结束。

### WebSocket `/ws/cesium`
3D Cesium 桥接长连接，由 `cesium_bridge_server.cesium_ws_endpoint` 实现，用于浏览器与后端推送 Cesium 命令。

---

## 3. 矢量数据接口

### GET `/api/vector-data`
按表名动态拉取 GeoJSON。

Query 参数：
| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `table_name` | 是 |  | PostgreSQL 表名（`mineable_areas` 自动重定向到 `ceshen`） |
| `geom_col` | 否 | `geom` | 几何列名 |
| `properties` | 否 | 全字段 | 逗号分隔的属性字段；关键字段（`Mineable_Area_Name`、`Measured_Depth` 等）会强制保留 |
| `filter` | 否 |  | SQL `WHERE` 子句片段 |
| `color_expression` | 否 |  | SQL CASE 表达式生成 `_style_color` |
| `debug` | 否 | `false` | 返回 `_debug` 调试信息 |

返回：标准 GeoJSON `FeatureCollection`，附 `meta`（含 `feature_count`、空集时的诊断字段）。

### GET `/api/mvt/{z}/{x}/{y}`
PostGIS `ST_AsMVT` 实时矢量瓦片。

Query：`table_name`（默认 `ceshen`）、`geom_col`（默认 `geom`）、`properties`、`filter`。

返回：`application/x-protobuf` MVT。

---

## 4. 切片管理（自定义图层）

注册表存储于 `backend/tile_layers.json`，原始 GeoJSON 存于 `backend/uploaded_tile_data/`。
内置图层：`hx`（河道红线）、`caiqu`（采区边界）。

### GET `/api/tile_manager/layers`
列出全部图层（矢量切片 + 实时栅格 + 无人机正射），含 `tile_count`、`size_bytes`、`api_url`、`style`、`status`（`ready|missing`）等元信息。

### POST `/api/tile_manager/build`
`multipart/form-data` 上传 GeoJSON 并构建切片。

字段：
| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `file` | File | 必填 | GeoJSON 文件 |
| `layer_key` | str |  | 图层键（不传则用文件名） |
| `label` | str |  | 显示名 |
| `build_type` | str | `both` | `vector` / `raster` / `both` |
| `stroke` | str | `#2773d7` | 描边颜色（hex） |
| `fill` | str | `#2773d7` | 填充颜色 |
| `fill_alpha` | float | `0.18` | 填充透明度 0-1 |
| `stroke_width` | float | `2` | 描边宽度 1-20 |
| `min_zoom` | int | `0` | 矢量切片最小级 |
| `max_zoom` | int | `18` | 矢量切片最大级 |

矢量走 `tippecanoe` 输出到 `backend/vector_tiles/{key}/`；栅格通过 `overlay_tile_service` 实时渲染。

### POST `/api/tile_manager/regenerate`
重建已注册图层的矢量切片。
请求体：`{"layer": "<key>"}`。

### GET `/api/vector_tile/{layer}/{z}/{x}/{y}.pbf`
读取预生成的 tippecanoe `.pbf`。`layer` 仅允许 `[A-Za-z0-9_-]+`；缺失瓦片返回 204。
缓存：`Cache-Control: public, max-age=604800`。

### GET `/api/overlay_tile/{layer}/{z}/{x}/{y}.png`
矢量图层的实时栅格瓦片（hx / caiqu / 自定义）。
`z` 限定 `0–22`，缓存 1 天。

### GET `/api/overlay_feature/{layer}`
3D 视图点击时根据经纬度在 STRtree 中查找最近要素。

Query：`lng`、`lat`（必填，WGS84）、`tolerance_m`（默认 `120`）。
返回：`{"found": bool, "feature": {...}|null}`。

### GeoServer OGC 发布代理 `/api/geoserver/**`

用于切片管理页面与 GeoServer REST API 交互，前端不直接接触 GeoServer admin 凭据。配置来源为 `GEOSERVER_URL`、`GEOSERVER_USER`、`GEOSERVER_PASSWORD`、`GEOSERVER_WORKSPACE` 以及 `GEOSERVER_PG_*` 环境变量。

| 方法+路径 | 说明 |
| --- | --- |
| `GET /api/geoserver/status` | GeoServer 健康状态、版本、workspace 数、layer 数、Capabilities URL |
| `GET /api/geoserver/layers` | 当前 workspace 已发布图层列表，含 WMS/WMTS/WFS 示例 URL |
| `GET /api/geoserver/capabilities` | 返回 WMS 1.3.0、WMS 1.1.1、WMTS、WFS、WCS GetCapabilities URL |
| `POST /api/geoserver/publish` | 按 TileManager layer key 发布到 GeoServer，例如 `{"layer":"hx"}` |
| `POST /api/geoserver/unpublish` | 从 GeoServer 取消发布指定图层 |
| `POST /api/geoserver/seed` | 触发 GeoWebCache seed 预切任务 |
| `GET /api/geoserver/seed/{layer}` | 查询指定图层的 GWC seed 任务状态 |
| `POST /api/geoserver/truncate` | 清空指定图层的 GWC 缓存 |

`publish` 会根据 layer key 自动识别发布路径：`hx/caiqu/自定义矢量` 使用 PostGIS `overlay` schema，`ceshen` 使用业务 PostGIS schema，`drone` 图层使用保留的 `_3857.tif` 作为 GeoTIFF coveragestore。

GWC seed/truncate 请求体：
```json
{
  "layer": "hx",
  "bounds": [minX, minY, maxX, maxY],
  "min_zoom": 0,
  "max_zoom": 14,
  "format": "image/png",
  "threads": 1
}
```

---

## 5. 无人机正射 (MBTiles)

注册表存储于 `backend/drone_imagery/registry.json`，MBTiles 文件位于 `backend/drone_imagery/mbtiles/`。

### GET `/api/drone_imagery/layers`
列出已注册无人机图层。

### POST `/api/drone_imagery/register`
注册一个已存在的 MBTiles 文件。
```json
{
  "layer_key": "yuxin_gusha_2023_03",
  "name": "豫信固砂 2023-03",
  "path": "/abs/path/to/file.mbtiles",
  "area_key": "",
  "year": 2023,
  "min_zoom": 0,
  "max_zoom": 22,
  "max_native_zoom": null,
  "bounds": [minLng, minLat, maxLng, maxLat],
  "opacity": 0.9,
  "scheme": "tms"
}
```

### POST `/api/drone_imagery/build`
将 GeoTIFF 通过 `gdalwarp` + `gdal_translate` + `gdaladdo` 构建成 MBTiles 并自动注册。
```json
{
  "source_path": "/abs/path/to/source.tif",
  "layer_key": "",
  "name": "",
  "area_key": "",
  "year": null,
  "min_zoom": 0,
  "max_zoom": 22,
  "opacity": 0.9,
  "tile_format": "PNG",   // PNG | PNG8 | JPEG
  "quality": 85,
  "overwrite": true
}
```
依赖 GDAL CLI；超时 7200s。

### GET `/api/drone_imagery/tile/{layer}/{z}/{x}/{y}.png`
按 TMS（默认）或 XYZ scheme 读取 MBTiles tile，缺失返回 204。

---

## 6. 截图与报告

### POST `/api/save-screenshot`
保存前端 base64 截图。
```json
{
  "image_data": "data:image/png;base64,...",
  "file_name": "可选"
}
```
返回：`{"url": "/static/screenshots/xxx.png", "filename": "...", "file_path": "..."}`。

### GET `/api/download/report/{filename}`
强制下载 `backend/static/reports/{filename}`，自动加 `Content-Disposition: attachment`，仅允许字母数字与 `._-`。

---

## 7. 第三方瓦片代理

### GET `/proxy/gf-tiles/{z}/{y}/{x}`
代理高分影像服务 `123.149.20.94:60805/...GF_2024_YM/MapServer/tile`，HTTPS 失败自动回退 HTTP，缓存 10 分钟。

---

## 8. 会话管理（SQLite 持久化）

数据库：`backend/sessions.db`，包含 `sessions` 与 `messages` 两表。

### GET `/api/sessions`
列出所有会话（按 `updated_at desc`）。

### POST `/api/sessions`
创建会话。
```json
{"title": "可选，最长 50"}
```
返回：`{"id": uuid, "title": "...", "created_at": "...", "updated_at": "..."}`。

### PATCH `/api/sessions/{session_id}`
重命名。请求体：`{"title": "新标题"}`。

### DELETE `/api/sessions/{session_id}`
级联删除会话及消息。

### GET `/api/sessions/{session_id}/messages`
返回历史消息：`[{"role": "...", "content": "...", "created_at": "..."}]`。

> `/chat` 与 `/chat/stream` 只在 `session_id` 是数据库中存在的有效 UUID 时才会写入 `messages` 表，并在前两条消息时使用用户输入前 20 字自动生成会话标题。

---

## 9. 知识库（RagFlow）

### GET `/api/knowledge`
列出 RagFlow 知识库文档（调用 `KnowledgeBaseTool.list_topics`）。

### GET `/api/knowledge/{document_id}`
读取文档内容。

### POST `/api/knowledge`
新增条目：
```json
{"title": "...", "content": "...", "tags": []}
```

### DELETE `/api/knowledge/{kb_id}`
当前返回固定提示：RagFlow 模式下不支持通过该接口删除，请使用 RagFlow 控制台。

---

## 10. SAM 目标识别

依赖 conda 环境 `/home/server/miniconda3/envs/sam` 中的 `tools/sam_predict.py`。

### POST `/api/sam-detect`
```json
{
  "geometry": { "type": "Polygon", "coordinates": [...] },
  "prompt": "river | road | ...",
  "mode": "rectangle | polygon",
  "fast_mode": false
}
```
- 异步进度文件：`/tmp/sam_progress/{task_id}.json`
- 返回 GeoJSON `FeatureCollection`，附加 `_task_id` 字段
- 推理超时 600s

### GET `/api/sam-progress/{task_id}`
轮询推理进度：
```json
{"stage": "init|loading|inference|done|error", "current": int, "total": int, "message": "..."}
```

---

## 11. 静态资源

### GET `/static/**`
直接返回 `backend/static/` 下文件，包括：
- `screenshots/` 用户截图
- `reports/` 报告生成器输出（DOCX）
- 模板与示意图等

---

## 数据模型速查（Pydantic）

| 模型 | 字段 |
| --- | --- |
| `ChatMessage` | `role`, `content` |
| `ChatRequest` | `messages`, `active_view`(默认 `map`), `session_id?` |
| `ChatResponse` | `response`, `messages`, `map_commands`, `cesium_commands`, `charts`, `intent_info?` |
| `ScreenshotRequest` | `image_data`, `file_name?` |
| `KnowledgeItem` | `title`, `content`, `tags=[]` |
| `SessionCreate` | `title?` |
| `SessionRename` | `title` |
| `TileRegenerateRequest` | `layer` |
| `DroneImageryRegisterRequest` | `layer_key`, `name`, `path`, `area_key`, `year?`, `min_zoom`, `max_zoom`, `max_native_zoom?`, `bounds?`, `opacity`, `scheme` |
| `DroneImageryBuildRequest` | `source_path`, `layer_key`, `name`, `area_key`, `year?`, `min_zoom`, `max_zoom`, `opacity`, `tile_format`, `quality`, `overwrite` |
| `SAMDetectRequest` | `geometry`, `prompt`, `mode`, `fast_mode` |

---

## 通用错误约定

- `400` 参数非法（表名、坐标、GeoJSON 解析失败等）
- `404` 资源不存在（layer / session / 报告文件 / MBTiles）
- `409` MBTiles 已存在且 `overwrite=false`
- `500` 内部错误（数据库、GDAL、tippecanoe、SAM 等子进程失败）
- `502` 上游瓦片代理不可达
- `504` tippecanoe / SAM 超时

所有错误返回 `{"detail": "..."}` 标准 FastAPI 格式。
