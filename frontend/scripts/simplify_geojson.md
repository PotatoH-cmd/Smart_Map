# GeoJSON 数据简化指引

## 使用 Mapshaper（推荐）

### 红线（线要素）
```
mapshaper hx.geojson \
  -simplify 15% keep-shapes prevent-crossing \
  -o hx_simplified.geojson
```

### 采区（面要素）
```
mapshaper caiqu.geojson \
  -simplify 20% keep-shapes \
  -o caiqu_simplified.geojson
```

> 说明：百分比为保留形状的比例，请根据实际视觉效果与顶点规模调优，可将顶点减少 ≥80%。

## 使用 Turf.js（前端或预处理）
```js
import simplify from '@turf/simplify';
const simplified = simplify(feature, { tolerance: 0.0003, highQuality: false });
```

> 注意：前端简化建议置于 Web Worker 中，避免阻塞主线程。
