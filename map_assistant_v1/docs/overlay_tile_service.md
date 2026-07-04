# 红线与采区栅格切片服务实现文档

## 1. 概述

在 `map_assistant_v1` 项目中，**河道红线（hx）** 和 **采区边界（caiqu）** 两个矢量图层以 **XYZ 栅格瓦片** 的形式提供给前端（Leaflet 2D 地图和 Cesium 3D 地图）。

**为什么不直接加载 GeoJSON？**
原始 GeoJSON 文件体积庞大（如采区 caiqu.geojson 达 75 MB 级），直接在浏览器中解析和渲染会导致严重的内存和性能问题。改为后端按需渲染栅格瓦片后，前端只需像加载普通底图一样逐片请求 256×256 PNG 图片，性能开销大幅降低。

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────┐
│  浏览器（Leaflet / Cesium）                          │
│    URL: /api/overlay_tile/{layer}/{z}/{x}/{y}.png    │
└────────────────────┬────────────────────────────────┘
                     │ HTTP GET
                     ▼
┌─────────────────────────────────────────────────────┐
│  FastAPI 后端 (main.py)                              │
│    @app.get("/api/overlay_tile/{layer}/{z}/{x}/{y}") │
│           ↓                                          │
│  overlay_tile_service.py                             │
│    ┌──────────┐   ┌──────────┐   ┌──────────┐       │
│    │ LRU 缓存  │→ │ 空间查询  │→ │ PIL 渲染  │       │
│    └──────────┘   └──────────┘   └──────────┘       │
│         ↑                ↑                           │
│    (key,z,x,y)     STRtree 索引                      │
│                 (启动时加载 GeoJSON)                   │
└─────────────────────────────────────────────────────┘
```

### 请求链路

1. 前端通过 `UrlTemplateImageryProvider`（Cesium）或 `L.tileLayer`（Leaflet）发起标准 XYZ 瓦片请求
2. FastAPI 路由 `GET /api/overlay_tile/{layer}/{z}/{x}/{y}.png` 接收请求
3. 调用 `overlay_tile_service.get_tile_png(layer, z, x, y)` → 先查 LRU 缓存 → 未命中则实时渲染
4. 返回 256×256 透明 PNG，设置 `Cache-Control: public, max-age=86400`

---

## 3. 后端核心实现

核心代码位于 `backend/tools/overlay_tile_service.py`。

### 3.1 数据加载与空间索引 — `OverlayLayer` 类

```python
class OverlayLayer:
    def __init__(self, key, path, style):
        ...
    def load(self):
        # 1. 读取 GeoJSON 文件
        # 2. 逐要素用 shapely.geometry.shape() 解析几何
        # 3. 用 pyproj 将 WGS84(EPSG:4326) → Web Mercator(EPSG:3857)
        # 4. 构建 shapely STRtree 空间索引
```

**关键设计：**

| 设计点 | 说明 |
|--------|------|
| **惰性加载** | `load()` 仅在首次请求时执行，使用 `threading.Lock` 保证线程安全 |
| **坐标转换** | 加载时即把所有几何从 EPSG:4326 转为 EPSG:3857（Web Mercator 米），与瓦片坐标系一致，渲染时无需再转 |
| **STRtree 空间索引** | Shapely 2.x 的 R-Tree 索引，支持快速包围盒查询，避免逐个要素遍历 |
| **属性存储** | 同时保存 `properties` 列表，用于后续点击查询返回要素属性 |

### 3.2 瓦片数学 — `tile_bounds_3857()`

将 XYZ 瓦片编号转为 Web Mercator 坐标范围（米）：

```python
def tile_bounds_3857(z, x, y):
    n = 2 ** z
    tile_size = (2 * 20037508.342789244) / n   # 整个地球宽度 / 瓦片数量
    minx = -MERC_HALF + x * tile_size
    maxx = -MERC_HALF + (x + 1) * tile_size
    maxy =  MERC_HALF - y * tile_size
    miny =  MERC_HALF - (y + 1) * tile_size
    return (minx, miny, maxx, maxy)
```

标准 Web Mercator 半轴长 `20037508.342789244` 米，Y 轴从上到下递增。

### 3.3 渲染流程 — `render_tile()`

```python
def render_tile(layer, z, x, y):
    # 1. 计算瓦片的 Web Mercator 边界
    minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)

    # 2. STRtree 空间查询：只取与瓦片 bbox 相交的要素
    candidates = layer.query(minx, miny, maxx, maxy)
    if not candidates:
        return empty_tile_png()   # 透明空瓦片

    # 3. 创建 256×256 RGBA 透明画布
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    # 4. 计算「米 → 像素」缩放系数
    scale = 256 / (maxx - minx)

    # 5. 遍历候选要素，逐个绘制
    for geom, gtype in candidates:
        _draw_geom(draw, gtype, geom, project_xy, style, z)

    # 6. 导出 PNG 字节
    img.save(buf, format="PNG", optimize=True)
```

### 3.4 几何绘制 — `_draw_geom()`

支持的几何类型及绘制方式：

| 几何类型 | 绘制方式 |
|----------|----------|
| `Point` / `MultiPoint` | 圆形点标记 (`draw.ellipse`) |
| `LineString` / `MultiLineString` | 线条 (`draw.line`，曲线连接) |
| `Polygon` / `MultiPolygon` | 半透明填充 + 描边 (`draw.polygon` + `draw.line`) |
| `GeometryCollection` | 递归处理每个子几何 |

**样式参数：**

- `stroke` — 描边颜色（hex），红线为 `#ef4444`，采区为 `#facc15`
- `fill` — 填充颜色，默认同描边
- `fillAlpha` — 填充透明度，红线 0.05（接近透明），采区 0.18（微黄色填充）
- `strokeWidth` — 基础线宽，会根据缩放级别自适应调整

**线宽自适应规则** (`_line_width`)：

```
z ≤ 6  → 1px （远视图极细）
z ≤ 10 → base - 1（中等视图略细）
z > 10 → base （近视图完整线宽）
```

### 3.5 LRU 缓存

```python
@lru_cache(maxsize=4096)
def _cached_tile(key, z, x, y):
    return render_tile(get_layer(key), z, x, y)
```

以 `(图层名, z, x, y)` 为 key，进程内缓存最近 **4096** 张瓦片。常用视野范围的瓦片可以直接命中缓存，避免重复 PIL 渲染。

### 3.6 图层注册

模块加载时自动注册两个默认图层：

```python
register_layer("hx",
    ".../frontend/public/data/hx.geojson",
    {"stroke": "#ef4444", "fill": "#ef4444", "fillAlpha": 0.05, "strokeWidth": 3})

register_layer("caiqu",
    ".../frontend/public/data/caiqu.geojson",
    {"stroke": "#facc15", "fill": "#facc15", "fillAlpha": 0.18, "strokeWidth": 2})
```

数据目录可通过环境变量 `MAP_OVERLAY_DATA_DIR` 覆盖。

---

## 4. 后端 API 接口

### 4.1 瓦片接口

```
GET /api/overlay_tile/{layer}/{z}/{x}/{y}.png
```

| 参数 | 说明 |
|------|------|
| `layer` | 图层 key：`hx` 或 `caiqu` |
| `z` | 缩放级别 0-22 |
| `x` | 瓦片列号 |
| `y` | 瓦片行号 |

**响应：** `image/png`，256×256 透明 PNG。

**校验逻辑：**
- 未知图层 → 404
- z 超出 0-22 → 400
- x/y 超出 `[0, 2^z)` → 400
- 渲染失败 → 500

### 4.2 要素查询接口（点击查属性）

```
GET /api/overlay_feature/{layer}?lng={lng}&lat={lat}&tolerance_m={tolerance}
```

| 参数 | 说明 |
|------|------|
| `layer` | 图层 key |
| `lng` / `lat` | 点击位置（WGS84 经纬度） |
| `tolerance_m` | 搜索容差（米），默认 120 |

**响应示例：**
```json
{
  "found": true,
  "feature": {
    "layer": "hx",
    "geometry_type": "MultiPolygon",
    "properties": { "HHMC": "滚水河", "HHDM": "...", "AB": "..." },
    "distance_m": 35.7
  }
}
```

此接口利用同一份 STRtree 索引 + 几何距离计算，返回容差范围内最近的要素及其属性。因为瓦片是栅格图片，Cesium 无法直接 pick 矢量要素，所以需要此接口来补充属性查询能力。

---

## 5. 前端集成

### 5.1 Cesium 3D 地图 (`CesiumComponent.jsx`)

**图层预设：**
```javascript
const OVERLAY_PRESETS = {
  hx:    { label: '河道红线', url: '/api/overlay_tile/hx/{z}/{x}/{y}.png',    swatch: '#ef4444' },
  caiqu: { label: '采区边界', url: '/api/overlay_tile/caiqu/{z}/{x}/{y}.png', swatch: '#facc15' },
};
```

**开关控制** (`toggleOverlay`)：
- 使用 `Cesium.UrlTemplateImageryProvider` 加载瓦片 URL
- `WebMercatorTilingScheme` 匹配后端生成的 EPSG:3857 瓦片
- `alpha = 0.95` 保持高不透明度
- 再次点击同一按钮 → `viewer.imageryLayers.remove()` 关闭

**点击查询** (`handleOverlayFeatureClick`)：
1. 检测当前有哪些叠加图层处于开启状态
2. 将屏幕坐标转为 WGS84 经纬度
3. 根据相机高度计算动态容差 `tolerance_m = clamp(cameraH / 2500, 80, 800)`
4. 依次请求 `/api/overlay_feature/{key}` 获取最近要素属性
5. 在点击位置创建临时 Cesium Entity（黄色圆点），弹出属性面板

### 5.2 Leaflet 2D 地图 (`MapComponent.jsx`)

```javascript
// 采区
L.tileLayer('/api/overlay_tile/caiqu/{z}/{x}/{y}.png', {
  maxZoom: 22, maxNativeZoom: 18, opacity: 0.95,
  updateWhenIdle: true, updateWhenZooming: false, keepBuffer: 4,
})

// 红线
L.tileLayer('/api/overlay_tile/hx/{z}/{x}/{y}.png', { ... })
```

Leaflet 侧使用标准 `L.tileLayer` 加载，设置 `keepBuffer: 4` 提前缓冲周边瓦片以减少滚动白闪。

---

## 6. 性能优化总结

| 优化手段 | 效果 |
|----------|------|
| **STRtree 空间索引** | 每个瓦片只检索 bbox 内的要素，不遍历全量数据 |
| **坐标预转换** | 加载时一次性转为 EPSG:3857，渲染时零转换开销 |
| **LRU 缓存（4096 张）** | 常用视野瓦片命中缓存，避免重复 PIL 绘制 |
| **空瓦片单例** | 无要素的瓦片返回全局共享的透明 PNG bytes，不创建新 Image |
| **惰性加载** | 首次请求才读取 GeoJSON，不影响服务启动速度 |
| **浏览器缓存** | 响应头 `Cache-Control: public, max-age=86400`（24h） |
| **Leaflet keepBuffer** | 提前加载周边 4 格瓦片，减少平移时白闪 |

---

## 7. 依赖库

| 库 | 用途 |
|----|------|
| `Pillow (PIL)` | 创建 RGBA 画布、绘制几何、导出 PNG |
| `Shapely` | GeoJSON 解析、STRtree 空间索引、几何距离计算 |
| `pyproj` | EPSG:4326 → EPSG:3857 坐标转换 |
| `FastAPI` | HTTP 路由 |

---

## 8. 文件清单

| 文件 | 角色 |
|------|------|
| `backend/tools/overlay_tile_service.py` | 核心：数据加载、空间索引、瓦片渲染、缓存 |
| `backend/main.py` | API 路由：`/api/overlay_tile/` 和 `/api/overlay_feature/` |
| `frontend/public/data/hx.geojson` | 河道红线原始数据 |
| `frontend/public/data/caiqu.geojson` | 采区边界原始数据 |
| `frontend/src/components/CesiumComponent.jsx` | Cesium 3D 集成：叠加图层开关 + 点击查属性 |
| `frontend/src/components/MapComponent.jsx` | Leaflet 2D 集成：`L.tileLayer` 加载 |
