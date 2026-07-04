# GeoServer 并行集成与切片管理操作说明

本文档说明 `map_assistant_v1` 中 GeoServer 并行集成的使用方式：现有 FastAPI 自研切片服务保持不变，GeoServer 作为 OGC 标准服务补充，用于向 QGIS、ArcGIS Pro 等第三方 GIS 客户端提供 WMS / WMTS / WFS。

## 1. 环境变量

后端通过 `/api/geoserver/**` 代理访问 GeoServer REST API，前端不会接触 admin 密码。请基于 `backend/.env.template` 配置：

```bash
GEOSERVER_URL=http://127.0.0.1:8088/geoserver
GEOSERVER_USER=admin
GEOSERVER_PASSWORD=change_me
GEOSERVER_WORKSPACE=map_assistant

GEOSERVER_PG_HOST=172.136.16.52
GEOSERVER_PG_PORT=5432
GEOSERVER_PG_DB=postgres
GEOSERVER_PG_USER=postgres
GEOSERVER_PG_PASSWORD=8720622
GEOSERVER_PG_SCHEMA_OVERLAY=overlay
GEOSERVER_PG_SCHEMA_BUSINESS=public
```

后端启动时会自动读取 `backend/.env`；首次部署可执行：

```bash
cp backend/.env.template backend/.env
```

然后按实际 GeoServer 密码、宿主机端口和 PostGIS 地址修改 `backend/.env`。

发布 `hx` / `caiqu` / `ceshen` 时，后端会优先按上述 schema 配置查找 PostGIS 表；如果 `overlay` 中不存在 `hx/caiqu`，会自动回退到 `public`。

端口说明：服务器宿主机 `8080` 已被占用，部署 GeoServer 时不要映射到 `8080:8080`。建议使用：

```yaml
ports:
  - "8088:8080"
```

此时后端应配置 `GEOSERVER_URL=http://127.0.0.1:8088/geoserver`。如果后端和 GeoServer 在同一个 Docker Compose 网络内，也可以使用容器服务名访问：`http://geoserver:8080/geoserver`，但对宿主机暴露端口仍建议使用 `8088` 或其他空闲端口。

项目已提供独立部署文件：`deploy/docker-compose.geoserver.yml`。

启动 GeoServer：

```bash
docker compose -f deploy/docker-compose.geoserver.yml up -d
```

指定管理员密码与宿主机端口：

```bash
GEOSERVER_PASSWORD='your_strong_password' GEOSERVER_HOST_PORT=8088 docker compose -f deploy/docker-compose.geoserver.yml up -d
```

启动后访问：

- Web 控制台：`http://127.0.0.1:8088/geoserver/web/`
- REST/OGC 根地址：`http://127.0.0.1:8088/geoserver`
- 数据目录：`deploy/geoserver_data/`

后端 `.env` 中的 `GEOSERVER_PASSWORD` 需要与 compose 启动时的 `GEOSERVER_PASSWORD` 保持一致。

## 2. 切片管理页面新增模块

### GeoServer 状态面板

位置：切片管理顶部说明卡片下方。

功能：

- 显示 GeoServer 是否可用、版本号、workspace、layer 数
- 一键复制 WMS / WMTS / WFS Capabilities URL
- 展开详情后查看完整 OGC 接入地址
- GeoServer 不可用时仅禁用相关按钮，不影响现有切片管理功能

### 图层列表 GeoServer 操作列

图层列表新增两列：

- `GeoServer`：显示当前 layer 是否已发布到 GeoServer
- `Geo 操作`：支持发布、重同步、取消发布、复制 GeoServer 图层 URL

支持的 layer 范围：

- `hx`：河道红线
- `caiqu`：采区边界
- `ceshen`：PostGIS 业务测深表（前端虚拟行）
- 自定义上传 GeoJSON 图层
- 无人机影像图层（优先使用保留的 `_3857.tif` 发布为 GeoTIFF coveragestore）

### GeoWebCache 预切任务面板

位置：图层列表下方，默认折叠。

功能：

- 选择已发布到 GeoServer 的图层
- 可选填写 BBOX；留空表示全范围
- 设置 `min_zoom` / `max_zoom`
- 设置输出格式：`image/png`、`image/jpeg`、MVT
- 提交 GWC seed 任务
- 查询 seed 进度
- truncate 清空指定图层缓存

## 3. 后端接口速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/geoserver/status` | GeoServer 健康状态与 Capabilities URL |
| GET | `/api/geoserver/layers` | 已发布图层列表 |
| GET | `/api/geoserver/capabilities` | OGC Capabilities URL 集合 |
| POST | `/api/geoserver/publish` | 发布 layer key |
| POST | `/api/geoserver/unpublish` | 取消发布 layer key |
| POST | `/api/geoserver/seed` | 提交 GWC seed |
| GET | `/api/geoserver/seed/{layer}` | 查询 GWC 任务状态 |
| POST | `/api/geoserver/truncate` | 清空 GWC 缓存 |

## 4. QGIS / ArcGIS Pro 接入

从切片管理顶部状态面板复制：

- WMS：`/geoserver/map_assistant/wms?service=WMS&version=1.3.0&request=GetCapabilities`
- WMTS：`/geoserver/gwc/service/wmts?REQUEST=GetCapabilities`
- WFS：`/geoserver/map_assistant/wfs?service=WFS&version=2.0.0&request=GetCapabilities`

QGIS 中使用：

1. `图层` → `添加图层` → `添加 WMS/WMTS 图层` 或 `添加 WFS 图层`
2. 新建连接，粘贴对应 Capabilities URL
3. 选择 `map_assistant:<layer>` 图层加载

## 5. 注意事项

- `hx/caiqu/自定义矢量` 发布前需要已在 PostGIS `overlay` schema 中存在同名表；后续可用 bootstrap 脚本自动同步 GeoJSON 到 PostGIS。
- `ceshen` 直接从业务 PostGIS schema 发布。
- 无人机影像若只保留 MBTiles 而没有 `_3857.tif`，GeoServer 发布可能失败；建议构建 MBTiles 时保留中间 GeoTIFF。
- GeoServer REST 凭据仅保存在后端环境变量中，浏览器 Network 不会出现 admin 密码。
