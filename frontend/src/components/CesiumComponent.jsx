/**
 * CesiumComponent.jsx
 * Cesium 3D 地图组件（天地图底图，国内可用）
 */
import React, { useEffect, useRef, useCallback, useState } from 'react';
import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';

window.CESIUM_BASE_URL = '/cesium/';

// ==================== 底图加载（Esri World Imagery）====================

const BASEMAP_PRESETS = {
  satellite: {
    label: '卫星影像',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Imagery',
    }],
  },
  street: {
    label: '街道地图',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Street Map',
    }],
  },
  hybrid: {
    label: '影像注记',
    providers: [
      {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        credit: 'Esri World Imagery',
      },
      {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
        credit: 'Esri Reference',
      },
    ],
  },
  topo: {
    label: '地形图',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Topo',
    }],
  },
  jcdt: {
    label: '基础底图',
    type: 'arcgisMapServer',
    url: 'http://gis95.yskc.com/server/rest/services/河南省基础底图/HNS_JCDT_SL/MapServer',
    credit: '基础底图',
  },
  gf2024: {
    label: '2024年高分影像',
    providers: [{
      url: '/proxy/gf-tiles/{z}/{y}/{x}',
      credit: '2024年高分影像',
    }],
  },
  gf2025: {
    label: '2025年高分影像',
    providers: [{
      url: '/proxy/gf2025-tiles/{z}/{y}/{x}',
      credit: '2025年高分影像',
    }],
  },
};

const OVERLAY_PRESETS = {
  hx: {
    label: '河道红线',
    name: 'overlay-hx',
    url: '/api/overlay_tile/hx/{z}/{x}/{y}.png',
    swatch: '#ef4444',
    minLevel: 0,
    maxLevel: 18,
  },
  caiqu: {
    label: '采区边界',
    name: 'overlay-caiqu',
    url: '/api/overlay_tile/caiqu/{z}/{x}/{y}.png',
    swatch: '#38bdf8',
    minLevel: 0,
    maxLevel: 18,
  },
};

const OBLIQUE_TILESETS = {
  'qx-dyt': {
    label: '大云台倾斜影像',
    url: 'http://data.mars3d.cn/3dtiles/qx-dyt/tileset.json',
    name: '大云台倾斜影像',
    autoGroundClamp: false,
  },
};

function loadBasemap(viewer, type) {
  if (!viewer || viewer.isDestroyed()) return;
  const preset = BASEMAP_PRESETS[type] || BASEMAP_PRESETS.satellite;
  viewer.imageryLayers.removeAll();

  if (preset.type === 'arcgisMapServer') {
    // ArcGIS MapServer 底图：使用 ArcGisMapServerImageryProvider.fromUrl 异步加载
    Cesium.ArcGisMapServerImageryProvider.fromUrl(preset.url, {
      credit: preset.credit || '',
    }).then(provider => {
      if (!viewer.isDestroyed()) {
        viewer.imageryLayers.addImageryProvider(provider);
      }
    }).catch(err => {
      console.error('[CesiumComponent] ArcGIS MapServer 底图加载失败:', preset.url, err);
    });
  } else {
    preset.providers.forEach(p => {
      viewer.imageryLayers.addImageryProvider(
        new Cesium.UrlTemplateImageryProvider({
          url: p.url,
          tilingScheme: new Cesium.WebMercatorTilingScheme(),
          maximumLevel: 19,
          credit: p.credit,
        })
      );
    });
  }
  console.log('[CesiumComponent] 底图已加载:', type || 'satellite');
}

// ==================== 命令执行器 ====================

// 模块级变量，供 executeCesiumCommand 访问本地数据集
let _moduleLocalDatasets = [];

function executeCesiumCommand(viewer, command) {
  if (!viewer || !command || !command.type) return;
  const { type } = command;
  try {
    switch (type) {
      case 'flyTo': {
        const { lat, lng, height = 100000, duration = 3 } = command;
        viewer.camera.flyTo({
          destination: Cesium.Cartesian3.fromDegrees(lng, lat, height),
          duration,
          easingFunction: Cesium.EasingFunction.QUADRATIC_IN_OUT,
          orientation: {
            heading: Cesium.Math.toRadians(0),
            pitch: Cesium.Math.toRadians(-45),
            roll: 0,
          },
        });
        break;
      }
      case 'setView': {
        const { lat, lng, height = 50000 } = command;
        viewer.camera.setView({
          destination: Cesium.Cartesian3.fromDegrees(lng, lat, height),
          orientation: {
            heading: Cesium.Math.toRadians(0),
            pitch: Cesium.Math.toRadians(-45),
            roll: 0,
          },
        });
        break;
      }
      case 'addMarker': {
        const { lat, lng, title = '标注点', popup = '', color = '#ff4444' } = command;
        const cesiumColor = parseCesiumColor(color);
        const entity = viewer.entities.add({
          name: title,
          position: Cesium.Cartesian3.fromDegrees(lng, lat),
          billboard: {
            image: createPinImage(cesiumColor),
            verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            scale: 1.0,
          },
          description: popup || title,
        });
        viewer.flyTo(entity, { duration: 2 });
        break;
      }
      case 'addGeoJsonLayer': {
        const { url, name = 'GeoJSON图层', color = '#2773d7' } = command;
        let fullUrl = url;
        if (url && url.startsWith('/')) {
          fullUrl = url;  // 使用相对路径，由 CRA proxy 或 Nginx 反代转发到后端
        }
        const existing = viewer.dataSources.getByName(name);
        existing.forEach(ds => viewer.dataSources.remove(ds));
        removeDepthColumnGroup(viewer, name);
        if (isDepthGeoJsonCommand({ ...command, url: fullUrl, name })) {
          addDepthColumns(viewer, {
            ...command,
            type: 'addDepthColumns',
            url: fullUrl,
            name,
            threshold: command.threshold ?? 2,
            heightScale: command.heightScale ?? 45,
          });
          break;
        }
        Cesium.GeoJsonDataSource.load(fullUrl, {
          stroke: parseCesiumColor(color),
          fill: parseCesiumColor(color, 0.4),
          strokeWidth: 2,
          clampToGround: true,
        }).then(ds => {
          ds.name = name;
          viewer.dataSources.add(ds);
          viewer.flyTo(ds, { duration: 2 }).catch(() => {});
        }).catch(err => {
          console.error('[CesiumComponent] GeoJSON加载失败:', err, url);
        });
        break;
      }
      case 'addDepthColumns': {
        addDepthColumns(viewer, command);
        break;
      }
      case 'removeLayer': {
        viewer.dataSources.getByName(command.name).forEach(ds => viewer.dataSources.remove(ds));
        removeDepthColumnGroup(viewer, command.name);
        break;
      }
      case 'clearAll': {
        viewer.entities.removeAll();
        viewer.dataSources.removeAll();
        clearDepthColumnGroups(viewer);
        break;
      }
      case 'setBasemap': {
        loadBasemap(viewer, command.basemap);
        break;
      }
      case 'screenshot': {
        viewer.render();
        const imageData = viewer.canvas.toDataURL('image/png');
        window.dispatchEvent(new CustomEvent('cesium_screenshot', { detail: { imageData } }));
        const link = document.createElement('a');
        link.download = 'cesium_' + Date.now() + '.png';
        link.href = imageData;
        link.click();
        break;
      }
      case 'addPolygon': {
        const { coordinates, color = '#2773d7', name: polyName = '多边形' } = command;
        if (!coordinates || coordinates.length < 3) break;
        viewer.entities.add({
          name: polyName,
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(
              coordinates.map(([lng, lat]) => Cesium.Cartesian3.fromDegrees(lng, lat))
            ),
            material: parseCesiumColor(color, 0.5),
            outline: true,
            outlineColor: parseCesiumColor(color),
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          },
        });
        break;
      }
      case 'addPolyline': {
        const { coordinates, color = '#ff4444', name: lineName = '折线' } = command;
        if (!coordinates || coordinates.length < 2) break;
        viewer.entities.add({
          name: lineName,
          polyline: {
            positions: coordinates.map(([lng, lat]) => Cesium.Cartesian3.fromDegrees(lng, lat)),
            width: 3,
            material: parseCesiumColor(color),
            clampToGround: true,
          },
        });
        break;
      }
      case 'load3dTiles': {
        const preset = command.preset ? OBLIQUE_TILESETS[command.preset] : null;
        // 也尝试从 localDatasets 中查找 URL（当 preset 不存在但有 tileset key 时）
        const localDs = (!preset && command.key)
          ? (_moduleLocalDatasets || []).find(ds => ds.key === command.key)
          : null;
        const merged = preset
          ? { ...preset, ...command, url: command.url || preset.url, name: command.name || preset.name }
          : localDs
            ? { ...command, url: command.url || `/api/3dtiles/${encodeURIComponent(localDs.key)}/tileset.json`, name: command.name || localDs.label || localDs.key, autoGroundClamp: command.autoGroundClamp ?? (localDs.auto_ground_clamp !== false), altOffset: command.altOffset ?? (localDs.alt_offset || 0) }
            : command;
        const { url: tilesUrl, name = '3D Tiles' } = merged;
        if (!tilesUrl) break;
        if (!viewer._mapAssistantTilesets) viewer._mapAssistantTilesets = {};
        if (viewer._mapAssistantTilesets[name]) {
          try { viewer.scene.primitives.remove(viewer._mapAssistantTilesets[name]); } catch (_) {}
          delete viewer._mapAssistantTilesets[name];
        }
        window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'loading' } }));
        const tilesetOptions = {
          maximumScreenSpaceError: merged.maximumScreenSpaceError ?? 8,
          dynamicScreenSpaceError: merged.dynamicScreenSpaceError ?? true,
          dynamicScreenSpaceErrorDensity: merged.dynamicScreenSpaceErrorDensity ?? 0.00278,
          dynamicScreenSpaceErrorFactor: merged.dynamicScreenSpaceErrorFactor ?? 4,
          foveatedScreenSpaceError: merged.foveatedScreenSpaceError ?? true,
          foveatedConeSize: merged.foveatedConeSize ?? 0.3,
          foveatedMinimumScreenSpaceErrorRelaxation: merged.foveatedMinimumScreenSpaceErrorRelaxation ?? 2,
          foveatedTimeDelay: merged.foveatedTimeDelay ?? 0.2,
          skipLevelOfDetail: merged.skipLevelOfDetail ?? false,
          cullWithChildrenBounds: merged.cullWithChildrenBounds ?? true,
          maximumCacheOverflowBytes: merged.maximumCacheOverflowBytes ?? 512 * 1024 * 1024,
        };
        Cesium.Cesium3DTileset.fromUrl(tilesUrl, tilesetOptions).then(tileset => {
          tileset.name = name;
          // (altitude correction is applied below after primitive is ready)
          // ---- 高程修正：将模型贴合到平坦地形(0m)上 ----
          const _applyGroundClamp = () => {
            const center = tileset.boundingSphere.center;
            const carto = Cesium.Cartographic.fromCartesian(center);
            const centerAlt = carto.height;
            if (merged.autoGroundClamp && centerAlt > 50) {
              // 用包围球半径的15%估算垂直半高，将模型底部贴地
              const vertHalf = tileset.boundingSphere.radius * 0.15;
              const bottomAlt = centerAlt - vertHalf;
              const delta = -bottomAlt;
              const from = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0);
              const to = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, delta);
              const translation = Cesium.Cartesian3.subtract(to, from, new Cesium.Cartesian3());
              tileset.modelMatrix = Cesium.Matrix4.fromTranslation(translation);
            } else if (Number.isFinite(Number(merged.altOffset)) && Number(merged.altOffset) !== 0) {
              const surface = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0.0);
              const offset = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, Number(merged.altOffset));
              const translation = Cesium.Cartesian3.subtract(offset, surface, new Cesium.Cartesian3());
              tileset.modelMatrix = Cesium.Matrix4.fromTranslation(translation);
            }
          };
          _applyGroundClamp();
          viewer.scene.primitives.add(tileset);
          viewer._mapAssistantTilesets[name] = tileset;
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'ready' } }));
          if (merged.flyTo === false) {
            viewer.scene.requestRender();
          } else if (merged.center) {
            viewer.camera.flyTo({
              destination: Cesium.Cartesian3.fromDegrees(merged.center.lng, merged.center.lat, merged.center.height || 900),
              orientation: {
                heading: Cesium.Math.toRadians(merged.center.heading || 0),
                pitch: Cesium.Math.toRadians(merged.center.pitch || -37),
                roll: 0,
              },
              duration: merged.duration ?? 2,
            });
          } else {
            // 自动贴合 tileset 包围球，正上方俯视
            const bs = tileset.boundingSphere;
            const range = Math.max(bs.radius * 2.5, 300);
            viewer.camera.flyToBoundingSphere(bs, {
              offset: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_TWO, range),
              duration: 2,
            });
          }
        }).catch(err => {
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'error' } }));
          console.error('[CesiumComponent] 3DTiles加载失败:', err);
        });
        break;
      }
      case 'loadTerrain': {
        const { url: terrainUrl } = command;
        if (!terrainUrl) break;
        Cesium.CesiumTerrainProvider.fromUrl(terrainUrl).then(tp => {
          if (!viewer.isDestroyed()) viewer.terrainProvider = tp;
        }).catch(err => console.error('[CesiumComponent] 地形加载失败:', err));
        break;
      }
      default:
        console.warn('[CesiumComponent] 未知命令:', type);
    }
  } catch (err) {
    console.error('[CesiumComponent] 执行命令出错:', type, err);
  }
}

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
    hx: {
      HHMC: '河流名称',
      HHDM: '河流代码',
      FWXLB: '范围线类别',
      AB: '岸别',
      XZQDM: '行政区代码',
      SJLY: '数据来源',
      'Shape.STLe': '长度',
      OBJECTID: 'OBJECTID',
    },
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

function riskDiffToRGB(diff) {
  if (diff <= -1) return [34, 197, 94];
  if (diff <= 0) {
    const s = diff + 1;
    return [Math.round(34 + (74 - 34) * s), Math.round(197 + (222 - 197) * s), Math.round(94 + (128 - 94) * s)];
  }
  const t = Math.min(diff / 4, 1);
  if (t < 0.4) {
    const s = t / 0.4;
    return [Math.round(74 + (250 - 74) * s), Math.round(222 + (204 - 222) * s), Math.round(128 + (21 - 128) * s)];
  } else if (t < 0.7) {
    const s = (t - 0.4) / 0.3;
    return [Math.round(250 + (249 - 250) * s), Math.round(204 + (115 - 204) * s), Math.round(21 + (22 - 21) * s)];
  }
  const s = (t - 0.7) / 0.3;
  return [Math.round(249 + (220 - 249) * s), Math.round(115 + (38 - 115) * s), Math.round(22 + (38 - 22) * s)];
}

// 凸包（Graham Scan）
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

function buildIDWCanvas(pts, west, south, east, north, W = 128, H = 128) {
  // 自适应衰减半径：根据点密度估算典型间距
  const areaM2 = (east - west) * 111320 * Math.cos((north + south) / 2 * Math.PI / 180)
               * (north - south) * 111320;
  const typicalSpacing = Math.sqrt(areaM2 / Math.max(pts.length, 1));
  const FULL_R  = typicalSpacing * 0.8;   // 80% 间距内全不透明
  const FADE_R  = typicalSpacing * 2.0;   // 2× 间距外完全透明

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
      // 距离衰减 alpha
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
    threshold = 2,
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

    // 按采区分组
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
      console.warn('[CesiumComponent] 测深数据不足');
      return;
    }

    // 为每个采区独立生成渐变面
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

      // 极小点保留点击交互
      pts.forEach((pt, idx) => {
        const color = getDepthRiskColor(pt.diff, threshold);
        const riskLabel = getDepthRiskLabel(pt.diff, threshold);
        const entity = viewer.entities.add({
          name: `${areaName} - ${riskLabel}`,
          position: Cesium.Cartesian3.fromDegrees(pt.lng, pt.lat, 2),
          point: {
            pixelSize: 5,
            color: color.withAlpha(0.55),
            outlineColor: Cesium.Color.WHITE.withAlpha(0.5),
            outlineWidth: 0.8,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 200000),
          },
          description: `
            <table class="cesium-infoBox-defaultTable">
              <tbody>
                <tr><th>采区</th><td>${pt.props.Mineable_Area_Name || '-'}</td></tr>
                <tr><th>县区</th><td>${pt.props.County_District || '-'}</td></tr>
                <tr><th>年份</th><td>${pt.props.Year || '-'}</td></tr>
                <tr><th>实测深度</th><td>${Number.isFinite(pt.measuredDepth) ? pt.measuredDepth.toFixed(2) + ' m' : '-'}</td></tr>
                <tr><th>控制高程</th><td>${Number.isFinite(pt.controlElevation) ? pt.controlElevation.toFixed(2) + ' m' : '-'}</td></tr>
                <tr><th>差值</th><td>${pt.diff.toFixed(2)} m</td></tr>
                <tr><th>风险等级</th><td>${riskLabel}</td></tr>
              </tbody>
            </table>
          `,
        });
        entity.depthColumnGroup = groupName;
        entity.depthColumnIndex = idx;
      });
    });

    addDepthContextLayers(viewer, globalBounds);

    // 飞到整体区域
    const pLng = Math.max((globalBounds.east - globalBounds.west) * 0.08, 0.002);
    const pLat = Math.max((globalBounds.north - globalBounds.south) * 0.08, 0.002);
    viewer.camera.flyTo({
      destination: Cesium.Rectangle.fromDegrees(
        globalBounds.west - pLng, globalBounds.south - pLat,
        globalBounds.east + pLng, globalBounds.north + pLat
      ),
      duration: 2,
    });

    console.log('[CesiumComponent] 测深风险渐变面已加载:', name, areaMap.size, '个采区');
  } catch (err) {
    console.error('[CesiumComponent] 测深风险面加载失败:', err, url);
  }
}

// ==================== 测量 / 绘制 工具 ====================

const TOOL_GROUP = 'tool-draw-measure';

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

function clearToolEntities(viewer) {
  if (!viewer || viewer.isDestroyed()) return;
  const toRemove = viewer.entities.values.filter(e => e.toolGroup === TOOL_GROUP);
  toRemove.forEach(e => viewer.entities.remove(e));
}

function tagToolEntity(entity, sub = '') {
  entity.toolGroup = TOOL_GROUP;
  entity.toolSub = sub;
  return entity;
}

function formatLength(meters) {
  if (!Number.isFinite(meters)) return '-';
  if (meters >= 1000) return `${(meters / 1000).toFixed(2)} km`;
  return `${meters.toFixed(2)} m`;
}

function formatArea(sqm) {
  if (!Number.isFinite(sqm)) return '-';
  if (sqm >= 1_000_000) return `${(sqm / 1_000_000).toFixed(3)} km²`;
  if (sqm >= 10_000) return `${(sqm / 10_000).toFixed(3)} ha`;
  return `${sqm.toFixed(2)} m²`;
}

function totalDistance(positions) {
  let total = 0;
  for (let i = 1; i < positions.length; i++) {
    total += Cesium.Cartesian3.distance(positions[i - 1], positions[i]);
  }
  return total;
}

function polygonAreaFromPositions(positions) {
  if (!positions || positions.length < 3) return 0;
  const carto = positions.map(p => Cesium.Cartographic.fromCartesian(p));
  const R = 6378137;
  let total = 0;
  for (let i = 0; i < carto.length; i++) {
    const a = carto[i];
    const b = carto[(i + 1) % carto.length];
    total += (b.longitude - a.longitude) * (2 + Math.sin(a.latitude) + Math.sin(b.latitude));
  }
  return Math.abs(total * R * R / 2);
}

/**
 * 启动一个工具会话（测量或绘制）。返回销毁函数。
 * mode: 'distance' | 'area' | 'height' | 'point' | 'polyline' | 'polygon' | 'circle'
 */
function startToolSession(viewer, mode, onFinish) {
  if (!viewer || viewer.isDestroyed()) return () => {};
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
  const positions = [];
  let movePosition = null;
  const tempEntities = [];
  let labelEntity = null;

  const dynamicPositions = new Cesium.CallbackProperty(() => {
    const arr = positions.slice();
    if (movePosition) arr.push(movePosition);
    return arr;
  }, false);

  const dynamicHierarchy = new Cesium.CallbackProperty(() => {
    const arr = positions.slice();
    if (movePosition && arr.length >= 1) arr.push(movePosition);
    return new Cesium.PolygonHierarchy(arr.length >= 3 ? arr : []);
  }, false);

  // 预览图形
  if (mode === 'distance' || mode === 'polyline') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      polyline: {
        positions: dynamicPositions,
        width: 3,
        material: Cesium.Color.YELLOW,
        clampToGround: true,
      },
    }, mode)));
  } else if (mode === 'area' || mode === 'polygon') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      polyline: {
        positions: new Cesium.CallbackProperty(() => {
          const arr = positions.slice();
          if (movePosition) arr.push(movePosition);
          if (arr.length >= 2) arr.push(arr[0]);
          return arr;
        }, false),
        width: 2,
        material: Cesium.Color.YELLOW,
        clampToGround: true,
      },
      polygon: {
        hierarchy: dynamicHierarchy,
        material: Cesium.Color.YELLOW.withAlpha(0.3),
        classificationType: Cesium.ClassificationType.BOTH,
      },
    }, mode)));
  } else if (mode === 'circle') {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      position: new Cesium.CallbackProperty(() => positions[0], false),
      ellipse: {
        semiMajorAxis: new Cesium.CallbackProperty(() => {
          if (positions.length === 0) return 0;
          const ref = movePosition || positions[1];
          if (!ref) return 0;
          return Math.max(Cesium.Cartesian3.distance(positions[0], ref), 1);
        }, false),
        semiMinorAxis: new Cesium.CallbackProperty(() => {
          if (positions.length === 0) return 0;
          const ref = movePosition || positions[1];
          if (!ref) return 0;
          return Math.max(Cesium.Cartesian3.distance(positions[0], ref), 1);
        }, false),
        material: Cesium.Color.YELLOW.withAlpha(0.3),
        outline: true,
        outlineColor: Cesium.Color.YELLOW,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    }, mode)));
  }

  function addVertex(pos) {
    tempEntities.push(viewer.entities.add(tagToolEntity({
      position: pos,
      point: {
        pixelSize: 8,
        color: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.YELLOW,
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    }, mode)));
  }

  function updateLabel() {
    let text = '';
    if (mode === 'distance' && positions.length >= 1) {
      const arr = movePosition ? positions.concat([movePosition]) : positions;
      text = `距离: ${formatLength(totalDistance(arr))}`;
    } else if (mode === 'area' && positions.length >= 2) {
      const arr = movePosition ? positions.concat([movePosition]) : positions;
      text = `面积: ${formatArea(polygonAreaFromPositions(arr))}\n周长: ${formatLength(totalDistance(arr) + (arr.length >= 3 ? Cesium.Cartesian3.distance(arr[arr.length - 1], arr[0]) : 0))}`;
    } else if (mode === 'circle' && positions.length >= 1) {
      const ref = movePosition || positions[1];
      if (ref) {
        const r = Cesium.Cartesian3.distance(positions[0], ref);
        text = `半径: ${formatLength(r)}\n面积: ${formatArea(Math.PI * r * r)}`;
      }
    }
    if (!text) {
      if (labelEntity) { viewer.entities.remove(labelEntity); labelEntity = null; }
      return;
    }
    const anchor = movePosition || positions[positions.length - 1];
    if (!anchor) return;
    if (!labelEntity) {
      labelEntity = viewer.entities.add(tagToolEntity({
        position: new Cesium.CallbackProperty(() => movePosition || positions[positions.length - 1], false),
        label: {
          text,
          font: '13px sans-serif',
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString('#1f2937').withAlpha(0.85),
          pixelOffset: new Cesium.Cartesian2(12, -12),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'label'));
    } else {
      labelEntity.label.text = text;
    }
  }

  function finish(commit = true) {
    handler.destroy();
    if (!commit) {
      tempEntities.forEach(e => viewer.entities.remove(e));
      if (labelEntity) viewer.entities.remove(labelEntity);
    } else if (mode === 'height' && positions.length === 1) {
      // 高度测量：在终点显示
    }
    movePosition = null;
    if (typeof onFinish === 'function') onFinish({ mode, positions: positions.slice() });
  }

  handler.setInputAction(event => {
    const pos = pickGroundCartesian(viewer, event.position);
    if (!pos) return;
    if (mode === 'point') {
      const carto = Cesium.Cartographic.fromCartesian(pos);
      const lng = Cesium.Math.toDegrees(carto.longitude);
      const lat = Cesium.Math.toDegrees(carto.latitude);
      viewer.entities.add(tagToolEntity({
        position: pos,
        point: {
          pixelSize: 10, color: Cesium.Color.CYAN,
          outlineColor: Cesium.Color.WHITE, outlineWidth: 2,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
        label: {
          text: `${lng.toFixed(5)}, ${lat.toFixed(5)}`,
          font: '12px sans-serif', fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -16),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'point'));
      finish(true);
      return;
    }
    if (mode === 'height') {
      const carto = Cesium.Cartographic.fromCartesian(pos);
      const groundHeight = viewer.scene.globe.getHeight(carto) || carto.height || 0;
      const top = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, groundHeight + 1);
      viewer.entities.add(tagToolEntity({
        position: pos,
        point: { pixelSize: 8, color: Cesium.Color.CYAN, heightReference: Cesium.HeightReference.CLAMP_TO_GROUND },
        label: {
          text: `高程: ${groundHeight.toFixed(2)} m`,
          font: '13px sans-serif', fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString('#1f2937').withAlpha(0.85),
          pixelOffset: new Cesium.Cartesian2(0, -16),
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        },
      }, 'height'));
      void top;
      finish(true);
      return;
    }
    positions.push(pos);
    addVertex(pos);
    if (mode === 'circle' && positions.length >= 2) {
      updateLabel();
      finish(true);
      return;
    }
    updateLabel();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  handler.setInputAction(event => {
    movePosition = pickGroundCartesian(viewer, event.endPosition);
    updateLabel();
    viewer.scene.requestRender();
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  handler.setInputAction(() => {
    if (mode === 'distance' || mode === 'polyline') {
      if (positions.length >= 2) { movePosition = null; updateLabel(); finish(true); }
      else finish(false);
    } else if (mode === 'area' || mode === 'polygon') {
      if (positions.length >= 3) { movePosition = null; updateLabel(); finish(true); }
      else finish(false);
    } else {
      finish(false);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  handler.setInputAction(() => {
    finish(false);
  }, Cesium.ScreenSpaceEventType.RIGHT_CLICK);

  return () => { if (!handler.isDestroyed()) finish(false); };
}

// ==================== React 组件 ====================

const CesiumComponent = ({ onViewerReady, className = '' }) => {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const wsRef = useRef(null);
  const isInitializedRef = useRef(false);
  const toolCancelRef = useRef(null);

  const [basemap, setBasemap] = useState('satellite');
  const [activeTool, setActiveTool] = useState(null);
  const [layers, setLayers] = useState([]);
  const [layersOpen, setLayersOpen] = useState(false);
  const [overlays, setOverlays] = useState({ hx: false, caiqu: false });
  const [, forceTick] = useState(0);
  const [featurePopup, setFeaturePopup] = useState(null);
  const [tilesetStatus, setTilesetStatus] = useState({});
  const [localDatasets, setLocalDatasets] = useState([]);

  const refreshLayers = useCallback(() => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    const items = [];
    for (let i = 0; i < viewer.dataSources.length; i++) {
      const ds = viewer.dataSources.get(i);
      items.push({ key: `ds:${i}:${ds.name || ''}`, name: ds.name || `数据源${i + 1}`, type: 'dataSource', show: ds.show });
    }
    Object.entries(viewer._mapAssistantTilesets || {}).forEach(([name, tileset]) => {
      if (tileset) items.push({ key: `ts:${name}`, name, type: 'tileset', show: tileset.show !== false, tileset });
    });
    const groupMap = new Map();
    viewer.entities.values.forEach(e => {
      const g = e.depthColumnGroup || e.toolGroup || (e.name ? '其他标注' : null);
      if (!g) return;
      if (!groupMap.has(g)) groupMap.set(g, []);
      groupMap.get(g).push(e);
    });
    groupMap.forEach((arr, key) => {
      const allShown = arr.every(e => e.show !== false);
      items.push({ key: `eg:${key}`, name: key, type: 'entityGroup', show: allShown, entities: arr });
    });
    setLayers(items);
  }, []);

  const stopTool = useCallback(() => {
    if (toolCancelRef.current) {
      toolCancelRef.current();
      toolCancelRef.current = null;
    }
    setActiveTool(null);
  }, []);

  const startTool = useCallback((mode) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    if (toolCancelRef.current) toolCancelRef.current();
    setActiveTool(mode);
    toolCancelRef.current = startToolSession(viewer, mode, () => {
      toolCancelRef.current = null;
      setActiveTool(null);
      refreshLayers();
    });
  }, [refreshLayers]);

  const handleBasemap = useCallback((type) => {
    setBasemap(type);
    if (viewerRef.current) loadBasemap(viewerRef.current, type);
  }, []);

  const overlayLayersRef = useRef({});
  const overlayPickEntityRef = useRef(null);
  const localDatasetsRef = useRef([]);

  const toggleOverlay = useCallback((key) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    const cfg = OVERLAY_PRESETS[key];
    if (!cfg) return;
    const existing = overlayLayersRef.current[key];
    if (existing) {
      try { viewer.imageryLayers.remove(existing, true); } catch (_) {}
      delete overlayLayersRef.current[key];
      setOverlays(prev => ({ ...prev, [key]: false }));
      return;
    }
    try {
      const provider = new Cesium.UrlTemplateImageryProvider({
        url: cfg.url,
        tilingScheme: new Cesium.WebMercatorTilingScheme(),
        minimumLevel: cfg.minLevel ?? 0,
        maximumLevel: cfg.maxLevel ?? 18,
        credit: cfg.label,
      });
      const layer = viewer.imageryLayers.addImageryProvider(provider);
      layer.alpha = 0.95;
      overlayLayersRef.current[key] = layer;
      setOverlays(prev => ({ ...prev, [key]: true }));
    } catch (err) {
      console.error('[CesiumComponent] 叠加图层加载失败:', key, err);
    }
  }, []);

  const toggleLayer = useCallback((item) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    if (item.type === 'dataSource') {
      const idx = Number(item.key.split(':')[1]);
      const ds = viewer.dataSources.get(idx);
      if (ds) ds.show = !ds.show;
    } else if (item.type === 'entityGroup') {
      const next = !item.show;
      item.entities.forEach(e => { e.show = next; });
    } else if (item.type === 'tileset' && item.tileset) {
      item.tileset.show = !item.show;
      viewer.scene.requestRender();
    }
    forceTick(t => t + 1);
    refreshLayers();
  }, [refreshLayers]);

  const removeLayer = useCallback((item) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    if (item.type === 'dataSource') {
      const idx = Number(item.key.split(':')[1]);
      const ds = viewer.dataSources.get(idx);
      if (ds) viewer.dataSources.remove(ds);
    } else if (item.type === 'entityGroup') {
      item.entities.forEach(e => viewer.entities.remove(e));
    } else if (item.type === 'tileset' && item.tileset) {
      try { viewer.scene.primitives.remove(item.tileset); } catch (_) {}
      if (viewer._mapAssistantTilesets) delete viewer._mapAssistantTilesets[item.name];
    }
    refreshLayers();
  }, [refreshLayers]);

  const removeTilesetByName = useCallback((name) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    const ts = viewer._mapAssistantTilesets && viewer._mapAssistantTilesets[name];
    if (ts) {
      try { viewer.scene.primitives.remove(ts); } catch (_) {}
      delete viewer._mapAssistantTilesets[name];
    }
    setTilesetStatus(prev => { const next = { ...prev }; delete next[name]; return next; });
    forceTick(t => t + 1);
    refreshLayers();
    viewer.scene.requestRender();
  }, [refreshLayers]);

  const loadObliqueTileset = useCallback((key) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed()) return;
    const preset = OBLIQUE_TILESETS[key];
    if (!preset) return;
    const name = preset.name;
    const isLoaded = !!(viewer._mapAssistantTilesets || {})[name];
    if (isLoaded) { removeTilesetByName(name); return; }
    // 如果已在 loading/error 状态，先清除再重新加载
    setTilesetStatus(prev => { const next = { ...prev }; delete next[name]; return next; });

    // Google Photorealistic 3D Tiles
    if (preset.isGoogle) {
      if (!viewer._mapAssistantTilesets) viewer._mapAssistantTilesets = {};
      window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'loading' } }));
      Cesium.createGooglePhotorealistic3DTileset({ showCreditsOnScreen: true, onlyUsingWithGoogleGeocoder: true })
        .then(tileset => {
          tileset.name = name;
          viewer.scene.primitives.add(tileset);
          viewer._mapAssistantTilesets[name] = tileset;
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'ready' } }));
          viewer.scene.requestRender();
        })
        .catch(err => {
          console.error('[Google 3D Tiles] 加载失败:', err);
          window.dispatchEvent(new CustomEvent('cesium_tileset_status', { detail: { name, status: 'error' } }));
        });
      return;
    }

    executeCesiumCommand(viewer, {
      type: 'load3dTiles',
      preset: key,
      url: preset.url,
      name,
      autoGroundClamp: false,
      altOffset: preset.altOffset ?? 0,
      center: preset.center,
      flyTo: true,
    });
  }, [removeTilesetByName]);

  const loadAnyTileset = useCallback((cfg) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed() || !cfg) return;
    const st = (viewer._mapAssistantTilesets || {})[cfg.name] ? 'ready' : undefined;
    if (st) { removeTilesetByName(cfg.name); return; }
    executeCesiumCommand(viewer, {
      type: 'load3dTiles',
      url: cfg.url,
      name: cfg.name,
      autoGroundClamp: false,
      altOffset: cfg.altOffset ?? 0,
      center: cfg.center,
      flyTo: true,
    });
  }, [removeTilesetByName]);

  const clearTools = useCallback(() => {
    if (viewerRef.current) clearToolEntities(viewerRef.current);
    refreshLayers();
  }, [refreshLayers]);

  const handleOverlayFeatureClick = useCallback(async (event) => {
    const viewer = viewerRef.current;
    if (!viewer || viewer.isDestroyed() || toolCancelRef.current) return;
    const activeOverlayKeys = ['hx', 'caiqu'].filter(key => overlayLayersRef.current[key]);
    if (activeOverlayKeys.length === 0) return;
    const pickedEntity = viewer.scene.pick(event.position);
    if (
      Cesium.defined(pickedEntity)
      && Cesium.defined(pickedEntity.id)
      && pickedEntity.id !== overlayPickEntityRef.current
    ) return;
    const click = getClickLngLat(viewer, event.position);
    if (!click) return;
    const cameraHeight = viewer.camera.positionCartographic?.height || 200000;
    const toleranceM = Math.min(800, Math.max(80, cameraHeight / 2500));
    for (const key of activeOverlayKeys) {
      try {
        const url = `/api/overlay_feature/${key}?lng=${encodeURIComponent(click.lng)}&lat=${encodeURIComponent(click.lat)}&tolerance_m=${encodeURIComponent(toleranceM)}`;
        const res = await fetch(url);
        if (!res.ok) continue;
        const data = await res.json();
        if (!data?.found || !data.feature) continue;
        if (overlayPickEntityRef.current) {
          viewer.entities.remove(overlayPickEntityRef.current);
        }
        const entity = viewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(click.lng, click.lat),
          point: {
            pixelSize: 10,
            color: Cesium.Color.CYAN,
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 2,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          },
        });
        overlayPickEntityRef.current = entity;
        viewer.scene.requestRender();
        const properties = data.feature.properties || {};
        setFeaturePopup({
          layerKey: key,
          title: overlayFeatureName(key, properties),
          properties,
          distanceM: data.feature.distance_m,
        });
        return;
      } catch (err) {
        console.error('[CesiumComponent] 叠加图层属性查询失败:', key, err);
      }
    }
    setFeaturePopup(null);
  }, []);

  const initViewer = useCallback(() => {
    if (!containerRef.current || isInitializedRef.current) return;
    isInitializedRef.current = true;
    try {
      const ionToken = process.env.REACT_APP_CESIUM_ION_TOKEN;
      if (ionToken) Cesium.Ion.defaultAccessToken = ionToken;

      const viewer = new Cesium.Viewer(containerRef.current, {
        terrainProvider: new Cesium.EllipsoidTerrainProvider(),
        baseLayerPicker: false,
        navigationHelpButton: false,
        animation: false,
        timeline: false,
        homeButton: true,
        sceneModePicker: true,
        fullscreenButton: false,
        geocoder: false,
        infoBox: false,
        selectionIndicator: false,
        // —— 性能优化 ——
        requestRenderMode: true,
        maximumRenderTimeChange: Infinity,
        contextOptions: {
          webgl: { powerPreference: 'high-performance', antialias: true },
        },
      });

      viewerRef.current = viewer;

      // 渲染/瓦片性能调优
      const scene = viewer.scene;
      scene.fog.enabled = false;
      scene.skyAtmosphere.show = false;
      scene.globe.showGroundAtmosphere = false;
      scene.globe.maximumScreenSpaceError = 2.5;   // 默认 2，提高一点减少瓦片请求量
      scene.globe.tileCacheSize = 1000;            // 默认 100，扩大缓存避免重复下载
      scene.globe.preloadSiblings = true;          // 提前加载相邻瓦片，减少边缘空白
      scene.globe.depthTestAgainstTerrain = false; // 简化深度测试
      try { scene.globe.skipLevelOfDetail = true; } catch (_) {}
      scene.postProcessStages.fxaa.enabled = false; // 关闭后处理抗锯齿（用 MSAA 已够）
      if (typeof scene.msaaSamples !== 'undefined') scene.msaaSamples = 2;
      // 视角操控阻尼，让拖动更顺滑
      scene.screenSpaceCameraController.inertiaSpin = 0.85;
      scene.screenSpaceCameraController.inertiaTranslate = 0.85;
      scene.screenSpaceCameraController.inertiaZoom = 0.7;

      // 加载高德卫星影像底图（国内可用，无需Token）
      loadBasemap(viewer, 'satellite');

      // 异步升级为 Cesium World Terrain 真实地形（不阻塞初始化）
      if (ionToken) {
        Cesium.CesiumTerrainProvider.fromIonAssetId(1, { requestWaterMask: false })
          .then(tp => { if (viewer && !viewer.isDestroyed()) viewer.terrainProvider = tp; })
          .catch(() => {/* 保持平坦地形 */});
      }

      // 初始视角：河南信阳区域
      viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(114.3, 32.0, 800000),
        orientation: {
          heading: Cesium.Math.toRadians(0),
          pitch: Cesium.Math.toRadians(-45),
          roll: 0,
        },
      });

      viewer.dataSources.dataSourceAdded.addEventListener(refreshLayers);
      viewer.dataSources.dataSourceRemoved.addEventListener(refreshLayers);
      viewer.entities.collectionChanged.addEventListener(refreshLayers);

      // 双击拾取实体并显示属性（requestRenderMode 下显式触发）
      const dblClickHandler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
      dblClickHandler.setInputAction((event) => {
        const picked = viewer.scene.pick(event.position);
        if (Cesium.defined(picked) && Cesium.defined(picked.id)) {
          viewer.selectedEntity = picked.id;
          viewer.scene.requestRender();
        } else if (Cesium.defined(picked) && Cesium.defined(picked.primitive) && Cesium.defined(picked.primitive.id)) {
          viewer.selectedEntity = picked.primitive.id;
          viewer.scene.requestRender();
        }
      }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
      const overlayClickHandler = new Cesium.ScreenSpaceEventHandler(viewer.canvas);
      overlayClickHandler.setInputAction(handleOverlayFeatureClick, Cesium.ScreenSpaceEventType.LEFT_CLICK);
      viewer.selectedEntityChanged.addEventListener(() => {
        viewer.scene.requestRender();
      });

      if (onViewerReady) onViewerReady(viewer);
      console.log('[CesiumComponent] Viewer 初始化完成');
    } catch (err) {
      console.error('[CesiumComponent] Viewer 初始化失败:', err);
      isInitializedRef.current = false;
    }
  }, [onViewerReady, refreshLayers, handleOverlayFeatureClick]);

  const connectWebSocket = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = protocol + '//' + window.location.host + '/ws/cesium';
    console.log('[CesiumComponent] 连接 WebSocket:', wsUrl);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    ws.onopen = () => console.log('[CesiumComponent] WebSocket 已连接');
    ws.onmessage = (event) => {
      try {
        const command = JSON.parse(event.data);
        if (command.type === 'pong') return;
        if (viewerRef.current) executeCesiumCommand(viewerRef.current, command);
      } catch (err) {
        console.error('[CesiumComponent] 命令解析失败:', err);
      }
    };
    ws.onerror = () => {};
    ws.onclose = () => {
      wsRef.current = null;
      setTimeout(() => {
        if (isInitializedRef.current) connectWebSocket();
      }, 5000);
    };
  }, []);

  useEffect(() => {
    fetch('/api/tile_manager/3dtiles')
      .then(r => r.json())
      .then(d => {
        if (d.success && Array.isArray(d.datasets)) {
          setLocalDatasets(d.datasets);
          localDatasetsRef.current = d.datasets;
          _moduleLocalDatasets = d.datasets;
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    initViewer();
    connectWebSocket();
    const handleDirectCommand = (event) => {
      if (viewerRef.current) executeCesiumCommand(viewerRef.current, event.detail);
    };
    const handleTilesetStatus = (event) => {
      const { name, status } = event.detail || {};
      if (!name) return;
      setTilesetStatus(prev => ({ ...prev, [name]: status }));
      forceTick(t => t + 1);
      refreshLayers();
    };
    window.addEventListener('cesium_execute_command', handleDirectCommand);
    window.addEventListener('cesium_tileset_status', handleTilesetStatus);
    return () => {
      window.removeEventListener('cesium_execute_command', handleDirectCommand);
      window.removeEventListener('cesium_tileset_status', handleTilesetStatus);
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
      if (viewerRef.current && !viewerRef.current.isDestroyed()) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
      isInitializedRef.current = false;
    };
  }, [initViewer, connectWebSocket, refreshLayers]);

  const toolButtons = [
    { mode: 'distance', label: '测距', icon: '📏' },
    { mode: 'area', label: '测面', icon: '⬛' },
    { mode: 'height', label: '测高', icon: '⛰️' },
    { mode: 'point', label: '点', icon: '📍' },
    { mode: 'polyline', label: '线', icon: '➿' },
    { mode: 'polygon', label: '面', icon: '⬢' },
    { mode: 'circle', label: '圆', icon: '⭕' },
  ];

  return (
    <div
      className={'cesium-container ' + className}
      style={{ width: '100%', height: '100%', position: 'relative' }}
    >
      <div ref={containerRef} style={{ width: '100%', height: '100%' }} />

      {/* 右侧自定义属性弹窗 */}
      {featurePopup && (
        <div style={panelStyles.popup}>
          <div style={panelStyles.popupHeader}>
            <span style={panelStyles.popupTitle}>{featurePopup.title}</span>
            <button
              style={panelStyles.popupClose}
              onClick={() => {
                setFeaturePopup(null);
                if (overlayPickEntityRef.current && viewerRef.current && !viewerRef.current.isDestroyed()) {
                  viewerRef.current.entities.remove(overlayPickEntityRef.current);
                  overlayPickEntityRef.current = null;
                  viewerRef.current.scene.requestRender();
                }
              }}
            >
              ×
            </button>
          </div>
          <div style={panelStyles.popupBody}>
            {(() => {
              const labels = {
                hx: { HHMC: '河流名称', HHDM: '河流代码', FWXLB: '范围线类别', AB: '岸别', XZQDM: '行政区代码', SJLY: '数据来源', 'Shape.STLe': '长度(m)', OBJECTID: 'ID' },
                caiqu: { 'alow_area_': '采区名称', '所属县': '所属县', '许可证': '许可证号', '类型': '类型', '状态': '状态', 'ID': 'ID', '名称': '名称' },
              };
              const lbl = labels[featurePopup.layerKey] || {};
              const props = featurePopup.properties || {};
              const entries = Object.keys(props).map(k => [lbl[k] || k, props[k]]);
              if (Number.isFinite(featurePopup.distanceM)) entries.push(['点击距离', featurePopup.distanceM.toFixed(1) + ' m']);
              return entries.map(([label, value], i) => (
                <div key={i} style={panelStyles.popupRow}>
                  <span style={panelStyles.popupLabel}>{label}</span>
                  <span style={panelStyles.popupValue}>{value === null || value === undefined || value === '' ? '-' : typeof value === 'number' && Number.isFinite(value) ? (Number.isInteger(value) ? String(value) : value.toFixed(2)) : String(value)}</span>
                </div>
              ));
            })()}
          </div>
        </div>
      )}

      {/* 左侧统一控制面板 */}
      <div style={panelStyles.sidePanel}>
        {/* 测量 / 绘制 */}
        <div style={panelStyles.section}>
          <div style={panelStyles.boxTitle}>测量 / 绘制</div>
          <div style={panelStyles.btnGroup}>
            {toolButtons.map(b => (
              <button
                key={b.mode}
                onClick={() => (activeTool === b.mode ? stopTool() : startTool(b.mode))}
                style={{
                  ...panelStyles.btn,
                  ...(activeTool === b.mode ? panelStyles.btnActive : null),
                }}
                title={b.label}
              >
                <span style={{ marginRight: 4 }}>{b.icon}</span>{b.label}
              </button>
            ))}
            <button onClick={clearTools} style={{ ...panelStyles.btn, ...panelStyles.btnDanger }}>清除</button>
          </div>
          {activeTool && (
            <div style={panelStyles.hint}>
              {activeTool === 'point' || activeTool === 'height'
                ? '左键单击拾取一个点 · 右键取消'
                : activeTool === 'circle'
                  ? '左键点击中心，再点击边缘 · 右键取消'
                  : '左键依次添加点 · 双击结束 · 右键取消'}
            </div>
          )}
        </div>

        {/* 底图 */}
        <div style={panelStyles.section}>
          <div style={panelStyles.boxTitle}>底图</div>
          <div style={panelStyles.btnGroup}>
            {Object.keys(BASEMAP_PRESETS).map(key => (
              <button
                key={key}
                onClick={() => handleBasemap(key)}
                style={{
                  ...panelStyles.btn,
                  ...(basemap === key ? panelStyles.btnActive : null),
                }}
              >
                {BASEMAP_PRESETS[key].label}
              </button>
            ))}
          </div>
          <div style={{ ...panelStyles.btnGroup, marginTop: 4 }}>
            {Object.keys(OVERLAY_PRESETS).map(key => {
              const cfg = OVERLAY_PRESETS[key];
              const on = !!overlays[key];
              return (
                <button
                  key={key}
                  onClick={() => toggleOverlay(key)}
                  style={{
                    ...panelStyles.btn,
                    ...(on ? panelStyles.btnActive : null),
                  }}
                  title={on ? '点击关闭' : '点击叠加显示'}
                >
                  <span style={{
                    display: 'inline-block', width: 8, height: 8, borderRadius: 2,
                    background: cfg.swatch, marginRight: 6, verticalAlign: 'middle',
                  }} />
                  {cfg.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* 3D Tiles */}
        <div style={panelStyles.section}>
          <div style={panelStyles.boxTitle}>3D Tiles</div>
          {/* 在线示例 */}
          {Object.keys(OBLIQUE_TILESETS).length > 0 && (
            <div style={panelStyles.btnGroup}>
              {Object.keys(OBLIQUE_TILESETS).map(key => {
                const preset = OBLIQUE_TILESETS[key];
                const isLoaded = !!(viewerRef.current?._mapAssistantTilesets?.[preset.name]);
                const status = tilesetStatus[preset.name];
                return (
                  <button
                    key={key}
                    onClick={() => loadObliqueTileset(key)}
                    title={
                      isLoaded ? '点击卸载 ' + preset.name
                      : preset.isGoogle ? 'Google Photorealistic 3D Tiles\n需在 cesium.com/ion 账户里开启 Google Maps Platform 集成'
                      : preset.url
                    }
                    disabled={false}
                    style={{
                      ...panelStyles.btn,
                      ...(isLoaded ? panelStyles.btnActive : null),
                      ...(preset.isGoogle && !isLoaded ? { background: '#1e40af', color: '#fff', border: '1px solid #1e40af' } : null),
                      ...(status === 'error' ? { border: '1px solid #ef4444', color: '#ef4444' } : null),
                    }}
                  >
                    {status === 'loading' ? '加载中...' : status === 'error' ? '⚠ 失败(重试)' : isLoaded ? '✓ ' + preset.label : preset.label}
                  </button>
                );
              })}
            </div>
          )}
          {/* 本地数据集 */}
          {localDatasets.length > 0 && (
            <div style={panelStyles.btnGroup}>
              <div style={{ ...panelStyles.boxTitle, fontSize: 11, color: '#722ed1', marginBottom: 4, paddingTop: 4 }}>本地数据集</div>
              {localDatasets.map(ds => {
                const name = ds.label || ds.key;
                const isLoaded = !!(viewerRef.current?._mapAssistantTilesets?.[name]);
                const status = tilesetStatus[name];
                return (
                  <button
                    key={`local-${ds.key}`}
                    onClick={() => loadAnyTileset({
                      url: `/api/3dtiles/${encodeURIComponent(ds.key)}/tileset.json`,
                      name,
                      altOffset: ds.alt_offset || 0,
                      autoGroundClamp: ds.auto_ground_clamp !== false,
                      center: ds.center || null,
                    })}
                    title={isLoaded ? '点击卸载 ' + name : `加载 ${name}\n目录: ${ds.directory}`}
                    disabled={false}
                    style={{
                      ...panelStyles.btn,
                      ...(isLoaded ? panelStyles.btnActive : null),
                      ...(status === 'error' ? { border: '1px solid #ef4444', color: '#ef4444' } : null),
                      borderColor: '#d3adf7',
                    }}
                  >
                    {status === 'loading' ? '加载中...' : status === 'error' ? '⚠ 失败(重试)' : isLoaded ? '✓ ' + name : '📦 ' + name}
                  </button>
                );
              })}
            </div>
          )}
          <div style={panelStyles.hint}>已启用动态误差、视锥剔除和中心优先加载优化。支持在线示例和本地数据集。</div>
        </div>

        {/* 图层 */}
        <div style={panelStyles.section}>
          <button
            style={panelStyles.collapseBtn}
            onClick={() => { setLayersOpen(o => !o); refreshLayers(); }}
          >
            <span>{layersOpen ? '▾' : '▸'} 图层管理</span>
            <span style={panelStyles.badge}>{layers.length}</span>
          </button>
          {layersOpen && (
            <div style={panelStyles.layersBox}>
              {layers.length === 0 && (
                <div style={{ color: '#94a3b8', fontSize: 12, padding: 6 }}>暂无图层</div>
              )}
              {layers.map(item => (
                <div key={item.key} style={panelStyles.layerRow}>
                  <input
                    type="checkbox"
                    checked={!!item.show}
                    onChange={() => toggleLayer(item)}
                  />
                  <span style={panelStyles.layerName} title={item.name}>{item.name}</span>
                  <button onClick={() => removeLayer(item)} style={panelStyles.btnSmall} title="删除图层">×</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

const panelStyles = {
  sidePanel: {
    position: 'absolute', top: 12, left: 12, zIndex: 5,
    width: 240,
    display: 'flex', flexDirection: 'column', gap: 10,
    padding: 10,
    background: 'rgba(255,255,255,0.94)',
    color: '#1e293b',
    borderRadius: 10,
    border: '1px solid rgba(148,163,184,0.35)',
    boxShadow: '0 6px 18px rgba(15,23,42,0.12)',
    backdropFilter: 'blur(8px)',
    fontSize: 12,
    fontFamily: 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  section: {
    display: 'flex', flexDirection: 'column', gap: 6,
  },
  boxTitle: {
    fontWeight: 600, fontSize: 12, color: '#475569',
    letterSpacing: '0.3px',
  },
  btnGroup: {
    display: 'flex', gap: 6, flexWrap: 'wrap',
  },
  btn: {
    background: '#ffffff',
    color: '#334155',
    border: '1px solid #cbd5e1',
    borderRadius: 6,
    padding: '5px 10px',
    fontSize: 12,
    cursor: 'pointer',
    transition: 'all 0.15s',
    lineHeight: 1.3,
  },
  btnActive: {
    background: '#2563eb',
    borderColor: '#2563eb',
    color: '#ffffff',
    boxShadow: '0 2px 6px rgba(37,99,235,0.35)',
  },
  btnDanger: {
    background: '#fff1f2',
    borderColor: '#fecaca',
    color: '#dc2626',
  },
  btnSmall: {
    background: 'transparent',
    color: '#dc2626',
    border: 'none',
    fontSize: 16,
    lineHeight: 1,
    cursor: 'pointer',
    padding: '0 4px',
  },
  collapseBtn: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    width: '100%',
    background: '#f1f5f9',
    color: '#334155',
    border: '1px solid #e2e8f0',
    borderRadius: 6,
    padding: '6px 10px',
    fontSize: 12,
    fontWeight: 500,
    cursor: 'pointer',
  },
  badge: {
    background: '#2563eb', color: '#fff',
    borderRadius: 10, padding: '1px 8px',
    fontSize: 11, fontWeight: 600,
  },
  layersBox: {
    marginTop: 4,
    maxHeight: 220, overflowY: 'auto',
    background: '#f8fafc',
    border: '1px solid #e2e8f0',
    borderRadius: 6,
    padding: '4px 6px',
  },
  layerRow: {
    display: 'flex', alignItems: 'center', gap: 6,
    padding: '3px 0',
  },
  layerName: {
    flex: 1, whiteSpace: 'nowrap', overflow: 'hidden',
    textOverflow: 'ellipsis', fontSize: 12,
    color: '#334155',
  },
  hint: {
    marginTop: 2,
    padding: '5px 8px',
    fontSize: 11,
    color: '#92400e',
    background: '#fef3c7',
    border: '1px solid #fde68a',
    borderRadius: 6,
    lineHeight: 1.5,
  },
  popup: {
    position: 'absolute',
    top: 16,
    right: 16,
    zIndex: 10,
    width: 320,
    background: 'rgba(255,255,255,0.96)',
    border: '1px solid rgba(148,163,184,0.3)',
    borderRadius: 14,
    boxShadow: '0 16px 40px rgba(15,23,42,0.16), 0 2px 8px rgba(15,23,42,0.06)',
    backdropFilter: 'blur(12px)',
    fontFamily: 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
    overflow: 'hidden',
  },
  popupHeader: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '10px 12px 10px 14px',
    background: 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)',
  },
  popupTitle: {
    fontSize: 14,
    fontWeight: 700,
    color: '#ffffff',
    letterSpacing: '-0.2px',
  },
  popupClose: {
    width: 24,
    height: 24,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'rgba(255,255,255,0.2)',
    color: '#ffffff',
    border: 'none',
    borderRadius: 6,
    fontSize: 16,
    fontWeight: 700,
    cursor: 'pointer',
    lineHeight: 1,
  },
  popupBody: {
    padding: '6px 0',
    maxHeight: 340,
    overflowY: 'auto',
  },
  popupRow: {
    display: 'flex',
    alignItems: 'baseline',
    padding: '7px 14px',
    borderBottom: '1px solid #f1f5f9',
  },
  popupLabel: {
    width: '38%',
    flexShrink: 0,
    fontSize: 12,
    fontWeight: 500,
    color: '#64748b',
    textAlign: 'right',
    paddingRight: 10,
  },
  popupValue: {
    flex: 1,
    fontSize: 13,
    fontWeight: 600,
    color: '#0f172a',
    wordBreak: 'break-word',
  },
};

export default CesiumComponent;
export { executeCesiumCommand };
