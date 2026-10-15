// ============================================================
// Cesium 工具函数（自 CesiumComponent.jsx 拆分）：颜色/覆盖层描述/几何/IDW 渐变面
// ============================================================
import * as Cesium from 'cesium';
import { riskDiffToRGB } from '../../utils/geo';
import { OVERLAY_PRESETS } from './presets';

// ==================== 工具函数 ====================

function parseCesiumColor(colorStr, alpha) {
  const a = alpha !== undefined ? alpha : 1.0;
  try {
    if (!colorStr) return Cesium.Color.fromCssColorString('#2773d7').withAlpha(a);
    return Cesium.Color.fromCssColorString(colorStr).withAlpha(a);
  } catch (_) {
    return Cesium.Color.BLUE.withAlpha(a);
  }
}

function createPinImage(color) {
  const canvas = document.createElement('canvas');
  canvas.width = 32;
  canvas.height = 42;
  const ctx = canvas.getContext('2d');
  ctx.beginPath();
  ctx.arc(16, 16, 14, 0, Math.PI * 2);
  ctx.fillStyle = color.toCssColorString();
  ctx.fill();
  ctx.strokeStyle = 'white';
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(9, 26);
  ctx.lineTo(16, 42);
  ctx.lineTo(23, 26);
  ctx.fillStyle = color.toCssColorString();
  ctx.fill();
  return canvas.toDataURL();
}

function getFeatureLngLat(feature) {
  const props = feature?.properties || {};
  const lon = Number(props.Lon_4326 ?? props.lon ?? props.lng);
  const lat = Number(props.Lat_4326 ?? props.lat);
  if (Number.isFinite(lon) && Number.isFinite(lat)) return { lng: lon, lat };
  const geom = feature?.geometry;
  if (geom?.type === 'Point' && Array.isArray(geom.coordinates)) {
    const [geomLng, geomLat] = geom.coordinates;
    if (Number.isFinite(Number(geomLng)) && Number.isFinite(Number(geomLat))) {
      return { lng: Number(geomLng), lat: Number(geomLat) };
    }
  }
  return null;
}

function getDepthRiskColor(diff, threshold) {
  if (!Number.isFinite(diff)) return Cesium.Color.fromCssColorString('#94a3b8');
  if (diff > threshold) return Cesium.Color.fromCssColorString('#ef4444');
  if (diff > 0) return Cesium.Color.fromCssColorString('#f59e0b');
  return Cesium.Color.fromCssColorString('#22c55e');
}

function getDepthRiskLabel(diff, threshold) {
  if (!Number.isFinite(diff)) return '数据不完整';
  if (diff > threshold) return '超深风险';
  if (diff > 0) return '接近/轻微超深';
  return '正常';
}

function getDepthColumnGroupName(name) {
  return `depth-columns:${name || '测深风险柱'}`;
}

function removeDepthColumnGroup(viewer, name) {
  const groupName = getDepthColumnGroupName(name);
  const toRemove = viewer.entities.values.filter(entity => entity.depthColumnGroup === groupName);
  toRemove.forEach(entity => viewer.entities.remove(entity));
}

function clearDepthColumnGroups(viewer) {
  const toRemove = viewer.entities.values.filter(entity => entity.depthColumnGroup);
  toRemove.forEach(entity => viewer.entities.remove(entity));
}

function removeDataSourceByName(viewer, name) {
  viewer.dataSources.getByName(name).forEach(ds => viewer.dataSources.remove(ds));
}

function isDepthGeoJsonCommand(command) {
  const url = command?.url || '';
  const name = command?.name || '';
  return /[?&]table_name=ceshen(&|$)/.test(url) || /测深|超深|风险柱/.test(name);
}

function includeBoundsPoint(bounds, lng, lat) {
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return bounds;
  if (!bounds) return { west: lng, south: lat, east: lng, north: lat };
  bounds.west = Math.min(bounds.west, lng);
  bounds.south = Math.min(bounds.south, lat);
  bounds.east = Math.max(bounds.east, lng);
  bounds.north = Math.max(bounds.north, lat);
  return bounds;
}

function expandBounds(bounds, buffer = 0.025) {
  if (!bounds) return null;
  return {
    west: bounds.west - buffer,
    south: bounds.south - buffer,
    east: bounds.east + buffer,
    north: bounds.north + buffer,
  };
}

function forEachGeoJsonCoordinate(coords, callback) {
  if (!Array.isArray(coords)) return;
  if (typeof coords[0] === 'number' && typeof coords[1] === 'number') {
    callback(Number(coords[0]), Number(coords[1]));
    return;
  }
  coords.forEach(item => forEachGeoJsonCoordinate(item, callback));
}

function getFeatureBounds(feature) {
  let bounds = null;
  forEachGeoJsonCoordinate(feature?.geometry?.coordinates, (lng, lat) => {
    bounds = includeBoundsPoint(bounds, lng, lat);
  });
  return bounds;
}

function intersectsBounds(a, b) {
  if (!a || !b) return true;
  return !(a.east < b.west || a.west > b.east || a.north < b.south || a.south > b.north);
}

async function addFilteredGeoJsonOverlay(viewer, { url, name, stroke, fill, fillAlpha, strokeWidth, bounds }) {
  removeDataSourceByName(viewer, name);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const geojson = await res.json();
  const rawFeatures = Array.isArray(geojson.features) ? geojson.features : [];
  const features = bounds ? rawFeatures.filter(feature => intersectsBounds(getFeatureBounds(feature), bounds)) : rawFeatures;
  if (features.length === 0) return null;
  const strokeColor = parseCesiumColor(stroke);
  const fillColor = parseCesiumColor(fill, fillAlpha);
  const ds = await Cesium.GeoJsonDataSource.load({ ...geojson, features }, {
    stroke: strokeColor,
    fill: fillColor,
    strokeWidth,
    clampToGround: true,
  });
  ds.name = name;
  ds.entities.values.forEach(entity => {
    if (entity.polyline) {
      entity.polyline.width = strokeWidth;
      entity.polyline.material = strokeColor;
      entity.polyline.clampToGround = true;
    }
    if (entity.polygon) {
      entity.polygon.material = fillColor;
      entity.polygon.outline = true;
      entity.polygon.outlineColor = strokeColor;
    }
  });
  viewer.dataSources.add(ds);
  return ds;
}

async function addDepthContextLayers(viewer, bounds) {
  const contextBounds = expandBounds(bounds);
  try {
    await addFilteredGeoJsonOverlay(viewer, {
      url: '/data/caiqu.geojson',
      name: '采区范围边界',
      stroke: '#facc15',
      fill: '#facc15',
      fillAlpha: 0.14,
      strokeWidth: 3,
      bounds: contextBounds,
    });
    await addFilteredGeoJsonOverlay(viewer, {
      url: '/data/hx.geojson',
      name: '河道红线',
      stroke: '#ef4444',
      fill: '#ef4444',
      fillAlpha: 0.05,
      strokeWidth: 4,
      bounds: contextBounds,
    });
    console.log('[CesiumComponent] 红线与采区边界已加载');
  } catch (err) {
    console.error('[CesiumComponent] 红线/采区边界加载失败:', err);
  }
}

function convexHull(points) {
  if (points.length < 3) return points.slice();
  const sorted = points.slice().sort((a, b) => a.lng - b.lng || a.lat - b.lat);
  const cross = (O, A, B) => (A.lng - O.lng) * (B.lat - O.lat) - (A.lat - O.lat) * (B.lng - O.lng);
  const lower = [];
  for (const p of sorted) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i--) { const p = sorted[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
  lower.pop(); upper.pop();
  return lower.concat(upper);
}

// 点是否在多边形内（射线法）
function pointInPolygon(lng, lat, hull) {
  let inside = false;
  for (let i = 0, j = hull.length - 1; i < hull.length; j = i++) {
    const xi = hull[i].lng, yi = hull[i].lat, xj = hull[j].lng, yj = hull[j].lat;
    if ((yi > lat) !== (yj > lat) && lng < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// 凸包外扩缓冲
function bufferHull(hull, bufMeters) {
  if (hull.length < 3) return hull;
  const cx = hull.reduce((s, p) => s + p.lng, 0) / hull.length;
  const cy = hull.reduce((s, p) => s + p.lat, 0) / hull.length;
  const degLng = bufMeters / (111320 * Math.cos(cy * Math.PI / 180));
  const degLat = bufMeters / 111320;
  return hull.map(p => {
    const dx = p.lng - cx, dy = p.lat - cy;
    const d = Math.sqrt(dx * dx + dy * dy) || 1e-9;
    return { lng: p.lng + (dx / d) * degLng, lat: p.lat + (dy / d) * degLat };
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatOverlayValue(value) {
  if (value === null || value === undefined || value === '') return '-';
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  return String(value);
}

function buildOverlayDescription(layerKey, properties = {}, distanceM) {
  const labels = {
    hx: { HHMC: '河流名称', HHDM: '河流代码', FWXLB: '范围线类别', AB: '岸别', XZQDM: '行政区代码', SJLY: '数据来源', 'Shape.STLe': '长度', OBJECTID: 'OBJECTID' },
    caiqu: {},
  };
  const layerLabels = labels[layerKey] || {};
  const rows = Object.keys(properties).map(key => {
    const title = layerLabels[key] || key;
    return `<tr><th>${escapeHtml(title)}</th><td>${escapeHtml(formatOverlayValue(properties[key]))}</td></tr>`;
  });
  if (Number.isFinite(distanceM)) {
    rows.push(`<tr><th>点击距离</th><td>${distanceM.toFixed(1)} m</td></tr>`);
  }
  return `<div class="cesium-feature-card"><table class="cesium-feature-table"><tbody>${rows.join('')}</tbody></table></div>`;
}

function overlayFeatureName(layerKey, properties = {}) {
  if (layerKey === 'hx') return properties.HHMC ? `河道红线：${properties.HHMC}` : '河道红线';
  if (layerKey === 'caiqu') return properties['alow_area_'] || properties['名称'] || properties.name || properties.Name || '采区边界';
  return OVERLAY_PRESETS[layerKey]?.label || '图层属性';
}

function getClickLngLat(viewer, screenPosition) {
  const cartesian = pickGroundCartesian(viewer, screenPosition)
    || viewer.camera.pickEllipsoid(screenPosition, viewer.scene.globe.ellipsoid);
  if (!cartesian) return null;
  const carto = Cesium.Cartographic.fromCartesian(cartesian);
  return {
    lng: Cesium.Math.toDegrees(carto.longitude),
    lat: Cesium.Math.toDegrees(carto.latitude),
    position: cartesian,
  };
}

function pickGroundCartesian(viewer, screenPosition) {
  if (!screenPosition) return null;
  const scene = viewer.scene;
  if (scene.pickPositionSupported) {
    const p = scene.pickPosition(screenPosition);
    if (p) return p;
  }
  const ray = viewer.camera.getPickRay(screenPosition);
  if (!ray) return null;
  return scene.globe.pick(ray, scene) || null;
}

function buildIDWCanvas(pts, west, south, east, north, W = 128, H = 128) {
  // 自适应衰减半径：根据点密度估算典型间距
  const areaM2 = (east - west) * 111320 * Math.cos((north + south) / 2 * Math.PI / 180)
               * (north - south) * 111320;
  const typicalSpacing = Math.sqrt(areaM2 / Math.max(pts.length, 1));
  const FULL_R  = typicalSpacing * 0.8;
  const FADE_R  = typicalSpacing * 2.0;

  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  const imageData = ctx.createImageData(W, H);
  const data = imageData.data;
  for (let py = 0; py < H; py++) {
    const lat = north - (py / H) * (north - south);
    const cosLat = Math.cos(lat * Math.PI / 180);
    for (let px = 0; px < W; px++) {
      const lng = west + (px / W) * (east - west);
      let sumW = 0, sumWV = 0, minDist = Infinity;
      for (let i = 0; i < pts.length; i++) {
        const pt = pts[i];
        const dx = (lng - pt.lng) * 111320 * cosLat;
        const dy = (lat - pt.lat) * 111320;
        const d2 = dx * dx + dy * dy;
        const d  = Math.sqrt(d2);
        if (d < minDist) minDist = d;
        if (d2 < 1) { sumWV = pt.diff; sumW = 1; }
        else { const w = 1 / (d2 * d2); sumW += w; sumWV += w * pt.diff; }
      }
      let alpha = 0;
      if (minDist <= FULL_R) {
        alpha = 210;
      } else if (minDist < FADE_R) {
        alpha = Math.round(210 * (1 - (minDist - FULL_R) / (FADE_R - FULL_R)));
      }
      if (alpha === 0) continue;
      const val = sumW > 0 ? sumWV / sumW : 0;
      const [r, g, b] = riskDiffToRGB(val);
      const idx = (py * W + px) * 4;
      data[idx] = r; data[idx + 1] = g; data[idx + 2] = b; data[idx + 3] = alpha;
    }
  }
  ctx.putImageData(imageData, 0, 0);
  return canvas;
}

async function addDepthColumns(viewer, command) {
  const {
    url,
    name = '测深风险面',
    maxPoints = 600,
  } = command;
  if (!url) return;
  removeDepthColumnGroup(viewer, name);
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const geojson = await res.json();
    const rawFeatures = Array.isArray(geojson.features) ? geojson.features : [];
    const features = rawFeatures.length > maxPoints
      ? rawFeatures.filter((_, index) => index % Math.ceil(rawFeatures.length / maxPoints) === 0)
      : rawFeatures;
    const groupName = getDepthColumnGroupName(name);

    const areaMap = new Map();
    let globalBounds = null;
    features.forEach(feature => {
      const lngLat = getFeatureLngLat(feature);
      if (!lngLat) return;
      const props = feature.properties || {};
      const measuredDepth = Number(props.Measured_Depth);
      const controlElevation = Number(props.Control_Elevation);
      const diff = controlElevation - measuredDepth;
      if (!Number.isFinite(diff)) return;
      globalBounds = includeBoundsPoint(globalBounds, lngLat.lng, lngLat.lat);
      const areaKey = props.Mineable_Area_Name || name;
      if (!areaMap.has(areaKey)) areaMap.set(areaKey, []);
      areaMap.get(areaKey).push({ lng: lngLat.lng, lat: lngLat.lat, diff, props, measuredDepth, controlElevation });
    });

    if (areaMap.size === 0 || !globalBounds) {
      console.warn('[Cesium] 测深数据不足');
      return;
    }

    areaMap.forEach((pts, areaName) => {
      if (pts.length < 2) return;
      let bounds = null;
      pts.forEach(pt => { bounds = includeBoundsPoint(bounds, pt.lng, pt.lat); });

      const padLng = Math.max((bounds.east - bounds.west) * 0.08, 0.001);
      const padLat = Math.max((bounds.north - bounds.south) * 0.08, 0.001);
      const west  = bounds.west  - padLng;
      const east  = bounds.east  + padLng;
      const south = bounds.south - padLat;
      const north = bounds.north + padLat;

      const canvas = buildIDWCanvas(pts, west, south, east, north, 128, 128);
      const surface = viewer.entities.add({
        name: areaName + ' 风险面',
        rectangle: {
          coordinates: Cesium.Rectangle.fromDegrees(west, south, east, north),
          material: new Cesium.ImageMaterialProperty({ image: canvas, transparent: true }),
          classificationType: Cesium.ClassificationType.TERRAIN,
        },
      });
      surface.depthColumnGroup = groupName;
    });

    addDepthContextLayers(viewer, globalBounds);
  } catch (err) {
    console.error('[Cesium] addDepthColumns failed:', url, err);
  }
}

export {
  parseCesiumColor, createPinImage, getFeatureLngLat, getDepthRiskColor, getDepthRiskLabel,
  getDepthColumnGroupName, removeDepthColumnGroup, clearDepthColumnGroups, removeDataSourceByName,
  isDepthGeoJsonCommand, includeBoundsPoint, expandBounds, forEachGeoJsonCoordinate, getFeatureBounds,
  intersectsBounds, addFilteredGeoJsonOverlay, addDepthContextLayers, addDepthColumns,
  escapeHtml, formatOverlayValue, buildOverlayDescription, overlayFeatureName, getClickLngLat, pickGroundCartesian,
  convexHull, pointInPolygon, bufferHull, buildIDWCanvas
};
