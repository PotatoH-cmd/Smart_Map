import React, { useEffect, useRef, useCallback } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import 'leaflet.vectorgrid';
import * as EL from 'esri-leaflet';

if (L.DomEvent && typeof L.DomEvent.fakeStop !== 'function') {
  L.DomEvent.fakeStop = (e) => {
    L.DomEvent.stopPropagation(e);
  };
}

const guardCanvasClickWhenDetached = (CanvasClass) => {
  const proto = CanvasClass && CanvasClass.prototype;
  if (!proto || typeof proto._onClick !== 'function' || Object.prototype.hasOwnProperty.call(proto, '_onClickDetachedGuarded')) return;
  const originalOnClick = proto._onClick;
  proto._onClick = function guardedCanvasClick(e) {
    if (!this._map || typeof this._map.mouseEventToLayerPoint !== 'function') return;
    return originalOnClick.call(this, e);
  };
  proto._onClickDetachedGuarded = true;
};

guardCanvasClickWhenDetached(L.Canvas);
guardCanvasClickWhenDetached(L.Canvas && L.Canvas.Tile);

if (L.VectorGrid && L.VectorGrid.Protobuf && L.VectorGrid.Protobuf.prototype) {
  const proto = L.VectorGrid.Protobuf.prototype;
  if (typeof proto._getVectorTilePromise === 'function' && !Object.prototype.hasOwnProperty.call(proto, '_getVectorTilePromiseGuarded')) {
    const originalGetVectorTilePromise = proto._getVectorTilePromise;
    proto._getVectorTilePromise = function guardedVectorTilePromise(coords) {
      return originalGetVectorTilePromise.call(this, coords).catch(() => ({ layers: {} }));
    };
    proto._getVectorTilePromiseGuarded = true;
  }
}

// ============================================================
// 风险渐变面：IDW 插值 + Canvas 渲染
// ============================================================
function riskDiffToRGB(diff) {
  // diff = Control_Elevation - Measured_Depth（正值=超深/风险，负值=安全）
  if (diff <= -1) return [34, 197, 94];
  if (diff <= 0) {
    const s = (diff + 1) / 1;
    return [Math.round(34 + (74 - 34) * s), Math.round(197 + (222 - 197) * s), Math.round(94 + (128 - 94) * s)];
  }
  const t = Math.min(diff / 4, 1);
  if (t < 0.4) {
    const s = t / 0.4;
    return [Math.round(74 + (250 - 74) * s), Math.round(222 + (204 - 222) * s), Math.round(128 + (21 - 128) * s)];
  } else if (t < 0.7) {
    const s = (t - 0.4) / 0.3;
    return [Math.round(250 + (249 - 250) * s), Math.round(204 + (115 - 204) * s), Math.round(21 + (22 - 21) * s)];
  } else {
    const s = (t - 0.7) / 0.3;
    return [Math.round(249 + (220 - 249) * s), Math.round(115 + (38 - 115) * s), Math.round(22 + (38 - 22) * s)];
  }
}

// ✅ 修复 Leaflet 默认 marker 图标路径（移除多余空格）
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
});

// ============================================================
// LabelManager：智能标注管理器（缩放级别控制 + 像素碰撞避让）
// 使用 Leaflet Tooltip，在 openTooltip 后用 JS 内联样式强制覆盖默认样式
// 碰撞检测用空间网格(Grid)优化，避免 O(n²) 导致卡顿
// ============================================================
class LabelManager {
  constructor(map) {
    this.map = map;
    this.entries = []; // { latlng, layer, depthText }
    this._refreshTimer = null; // 防抖定时器
  }

  // 对 Tooltip DOM 元素强制注入内联样式（覆盖 Leaflet 默认白色背景）
  _applyStyle(layer) {
    const tooltip = layer.getTooltip();
    if (!tooltip) return;
    const el = tooltip.getElement ? tooltip.getElement() : null;
    if (!el) return;
    el.style.setProperty('background', 'transparent', 'important');
    el.style.setProperty('background-color', 'transparent', 'important');
    el.style.setProperty('border', 'none', 'important');
    el.style.setProperty('box-shadow', 'none', 'important');
    el.style.setProperty('padding', '0', 'important');
    el.style.setProperty('color', '#ef4444', 'important');
    el.style.setProperty('font-size', '14px', 'important');
    el.style.setProperty('font-weight', 'bold', 'important');
    el.style.setProperty('font-family', "'Times New Roman', Times, serif", 'important');
    el.style.setProperty('text-shadow',
      '-2px -2px 0 #fff, 2px -2px 0 #fff, -2px 2px 0 #fff, 2px 2px 0 #fff,' +
      '-2px 0 0 #fff, 2px 0 0 #fff, 0 -2px 0 #fff, 0 2px 0 #fff', 'important');
    el.style.setProperty('white-space', 'nowrap', 'important');
    el.classList.add('map-label-depth');
  }

  register(latlng, layer, depthText) {
    this.entries.push({ latlng, layer, depthText });
  }

  clear() {
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    // 关闭所有已显示的 Tooltip
    this.entries.forEach(({ layer }) => {
      try { layer.closeTooltip(); } catch (_) {}
    });
    this.entries = [];
  }

  // 防抖封装：快速连续缩放时只执行最后一次
  scheduleRefresh(delay = 200) {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this._refreshTimer = setTimeout(() => {
      this._refreshTimer = null;
      this.refresh();
    }, delay);
  }

  refresh() {
    if (!this.map || this.entries.length === 0) return;
    const zoom = this.map.getZoom();
    // === 修复：增大最小像素间距，减少注记密度 ===
    // 原值: zoom>=16→28, 14-15→50, 12-13→80, <12→120
    // 新值: zoom>=17→40, 15-16→65, 13-14→100, 11-12→140, <11→180
    const minPxGap = zoom >= 17 ? 40 : zoom >= 15 ? 65 : zoom >= 13 ? 100 : zoom >= 11 ? 140 : 180;

    // 获取当前视口范围（地理坐标）
    const bounds = this.map.getBounds().pad(0.1); // 稍微扩展一些，避免边缘闪烁

    // 空间网格碰撞检测（O(n) 复杂度）
    const cellSize = minPxGap;
    const grid = new Map();
    const cellKey = (cx, cy) => `${cx},${cy}`;

    const hasClash = (px, py) => {
      const cx = Math.floor(px / cellSize);
      const cy = Math.floor(py / cellSize);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          const neighbors = grid.get(cellKey(cx + dx, cy + dy));
          if (!neighbors) continue;
          for (const [ox, oy] of neighbors) {
            if (Math.hypot(px - ox, py - oy) < minPxGap) return true;
          }
        }
      }
      return false;
    };

    const addToGrid = (px, py) => {
      const key = cellKey(Math.floor(px / cellSize), Math.floor(py / cellSize));
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push([px, py]);
    };

    // 批量 DOM 操作：先全部关闭，再开启视口内符合条件的
    // 这样只需遍历两次，避免每个点都要判断 open/close 导致大量混杂 DOM 操作
    this.entries.forEach(({ layer }) => {
      try { layer.closeTooltip(); } catch (_) {}
    });

    this.entries.forEach(({ latlng, layer, depthText }) => {
      if (!layer.getTooltip()) return;
      // 忽略视口外的点（这是最关键的优化）
      if (!bounds.contains(latlng)) return;
      try {
        const pt = this.map.latLngToContainerPoint(latlng);
        if (!hasClash(pt.x, pt.y)) {
          layer.setTooltipContent(depthText);
          layer.openTooltip();
          this._applyStyle(layer);
          addToGrid(pt.x, pt.y);
        }
      } catch (_) {}
    });
  }

  destroy() {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this.entries = [];
  }
}

class MapManager {
  constructor(containerId, options = {}) {
    this.containerId = containerId;
    this.map = null;
    this.markers = [];
    this.layers = {};
    this.overlays = {};
    this._riskCanvases = {};
    this.droneImageryLayers = {};
    this.currentBaseLayer = null;
    this.layerControl = null;
    this.options = {
      center: [32.1, 115.5],
      zoom: 10,
      ...options
    };
    this.visibilityThresholds = {
      caiqu: 11,
      henanBaseMap: 12
    };
    this.markingMode = false;
    this.manualMarkers = L.layerGroup();
    this._featureClickHandled = false;
    this.onLayerStatus = typeof this.options.onLayerStatus === 'function' ? this.options.onLayerStatus : null;
    this.labelManager = null; // 智能标注管理器，initMap 后初始化
  }

  safelyRemoveLayer(layer) {
    if (!this.map || !layer) return;
    try {
      if (typeof this.map.hasLayer === 'function' && this.map.hasLayer(layer)) {
        this.map.removeLayer(layer);
      }
    } catch (_) {}
  }

  _isRiskLayer(features) {
    if (!features || features.length === 0) return false;
    return features.slice(0, 8).some(f => {
      const p = f.properties || {};
      return (p.Measured_Depth !== undefined || p.measured_depth !== undefined) &&
             (p.Control_Elevation !== undefined || p.control_elevation !== undefined);
    });
  }

  _removeRiskCanvas(layerName) {
    const c = this._riskCanvases[layerName];
    if (!c) return;
    try { if (c.canvas && c.canvas.parentNode) c.canvas.parentNode.removeChild(c.canvas); } catch (_) {}
    if (this.map && c.draw) this.map.off('moveend zoomend resize', c.draw);
    delete this._riskCanvases[layerName];
  }

  _addRiskGradientCanvas(layerName, features) {
    this._removeRiskCanvas(layerName);
    if (!this.map) return;

    const pts = features.map(f => {
      const p = f.properties || {};
      const coords = f.geometry && f.geometry.coordinates;
      const lng = Number(p.Lon_4326 !== undefined ? p.Lon_4326 : p.lon_4326 !== undefined ? p.lon_4326 : (coords && coords[0]));
      const lat = Number(p.Lat_4326 !== undefined ? p.Lat_4326 : p.lat_4326 !== undefined ? p.lat_4326 : (coords && coords[1]));
      const depth = Number(p.Measured_Depth !== undefined ? p.Measured_Depth : p.measured_depth);
      const ctrl  = Number(p.Control_Elevation !== undefined ? p.Control_Elevation : p.control_elevation);
      if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
      return { lng, lat, diff: Number.isFinite(ctrl - depth) ? ctrl - depth : 0 };
    }).filter(Boolean);

    if (pts.length < 2) return;

    const canvas = document.createElement('canvas');
    canvas.className = 'risk-gradient-canvas';
    canvas.style.cssText = 'position:absolute;pointer-events:none;z-index:450;opacity:0;transition:opacity 0.5s;filter:blur(20px);';
    this.map.getPanes().overlayPane.appendChild(canvas);

    const draw = () => {
      if (!this.map) return;
      const size = this.map.getSize();
      canvas.width = size.x;
      canvas.height = size.y;
      const off = this.map.containerPointToLayerPoint([0, 0]);
      canvas.style.left = off.x + 'px';
      canvas.style.top  = off.y + 'px';

      const ctx = canvas.getContext('2d');
      const projected = pts.map(pt => {
        const px = this.map.latLngToContainerPoint(L.latLng(pt.lat, pt.lng));
        return { x: px.x, y: px.y, diff: pt.diff };
      });

      // 自适应衰减半径：根据投影后像素间距决定渲染范围
      // 避免整个地图都被 IDW 插值色覆盖（修复底图变绿问题）
      const W = size.x, H = size.y;
      let avgNNDist = Infinity;
      if (projected.length > 1) {
        let totalNN = 0;
        const sampleCount = Math.min(projected.length, 60);
        for (let i = 0; i < sampleCount; i++) {
          let minD = Infinity;
          for (let j = 0; j < projected.length; j++) {
            if (i === j) continue;
            const dx = projected[i].x - projected[j].x;
            const dy = projected[i].y - projected[j].y;
            const d = Math.sqrt(dx * dx + dy * dy);
            if (d < minD) minD = d;
          }
          if (minD < Infinity) totalNN += minD;
        }
        avgNNDist = totalNN / sampleCount;
      }
      const FULL_R = Math.max(Math.min(avgNNDist * 0.8, 150), 30);  // 全不透明范围（像素）
      const FADE_R = Math.max(FULL_R * 2.5, 60);                     // 完全透明范围（像素）

      const step = 14;
      const N = projected.length;
      const imageData = ctx.createImageData(W, H);
      const data = imageData.data;

      for (let py = 0; py < H; py += step) {
        for (let px = 0; px < W; px += step) {
          let sumW = 0, sumWV = 0, minDist = Infinity;
          for (let i = 0; i < N; i++) {
            const pt = projected[i];
            const dx = px - pt.x, dy = py - pt.y;
            const d2 = dx * dx + dy * dy;
            const d = Math.sqrt(d2);
            if (d < minDist) minDist = d;
            if (d2 < 1) { sumWV = pt.diff; sumW = 1; break; }
            const w = 1 / (d2 * d2);
            sumW += w;
            sumWV += w * pt.diff;
          }

          // 距离衰减 alpha：只在数据点附近渲染，远离数据区域保持透明
          let alpha = 0;
          if (minDist <= FULL_R) {
            alpha = 160;
          } else if (minDist < FADE_R) {
            alpha = Math.round(160 * (1 - (minDist - FULL_R) / (FADE_R - FULL_R)));
          }
          if (alpha === 0) continue; // 超出影响范围的像素跳过，露出底图

          const val = sumW > 0 ? sumWV / sumW : 0;
          const [r, g, b] = riskDiffToRGB(val);
          const endY = Math.min(py + step, H), endX = Math.min(px + step, W);
          for (let ry = py; ry < endY; ry++) {
            for (let rx = px; rx < endX; rx++) {
              const idx = (ry * W + rx) * 4;
              data[idx] = r; data[idx+1] = g; data[idx+2] = b; data[idx+3] = alpha;
            }
          }
        }
      }
      ctx.putImageData(imageData, 0, 0);
      canvas.style.opacity = '0.6';
    };

    this.map.on('moveend zoomend resize', draw);
    requestAnimationFrame(draw);
    this._riskCanvases[layerName] = { canvas, draw };
  }

  initMap() {
    if (this.map) {
      try { this.map.off(); } catch (_) {}
      try { this.map.remove(); } catch (_) {}
    }

    this.map = L.map(this.containerId, {
      center: this.options.center,
      zoom: this.options.zoom,
      zoomControl: true,
      zoomAnimation: true,
      markerZoomAnimation: true,
      // —— 平滑交互 ——
      zoomSnap: 0.25,               // 分数级缩放，滚动更顺滑
      zoomDelta: 0.5,               // 每次滚轮缩放幅度减半，手感更细腻
      wheelPxPerZoomLevel: 120,     // 需要更多滚轮行程，缩放更受控
      bounceAtZoomLimits: false,    // 到达缩放极限不弹跳
      easeLinearity: 0.2,           // 惯性缓动曲线
      inertia: true,                // 拖拽松手惯性滑动
      inertiaDeceleration: 2000,    // 惯性停止速度（越小越快停）
      keyboardPanDelta: 80,         // 键盘平移步长
      // —— 性能优化 ——
      preferCanvas: true,           // 用 Canvas 渲染矢量，远快于 SVG
      updateWhenZooming: false,     // 缩放动画期间不重绘瓦片（动画结束再补）
      updateWhenIdle: true,         // 拖动过程中不刷新瓦片，停下后再加载
      fadeAnimation: false,         // 瓦片淡入由 CSS transition 接管，避免双动画
      wheelDebounceTime: 40,        // 滚轮缩放节流，避免过度触发
    });
    this.initLayerPanes();

    this.layers = {};
    this.overlays = {};

    // 初始化图层组
    this.manualMarkers.addTo(this.map);
    
    // 添加底图（仅高分影像 ArcGIS，国内可用）
    this.addArcGis3857Layer();
    // 添加基础底图（河南省基础底图）
    this.addJCDTBaseLayer();
    // 添加高分影像底图
    this.addGF2024Layer();
    this.addGF2025Layer();
    this.addGF2026Layer();
    // 添加 Esri 全球卫星底图（用于高分影像覆盖范围外的区域，如潢川 118°E 以东）
    this.addSatelliteLayer();

    // 初始化业务图层组
    this.overlays.caiqu = L.layerGroup();
    this.overlays.caiqu2026 = L.layerGroup();
    this.overlays.henanBaseMap = L.layerGroup(); // 河道红线
    this.overlays.jcdtOverlay = L.layerGroup();   // 基础底图（叠加到影像上）
    
    this.addLocalGeoJSONLayer();
    this.addCaiqu2026Layer();
    this.addHXLayer();

    // 将基础底图放入叠加图层组（可在影像上叠加显示）
    if (this.layers.jcdt && this.overlays.jcdtOverlay) {
      this.overlays.jcdtOverlay.addLayer(this.layers.jcdt);
    }

    // 1. 先设置并添加底图（最底层）
    this.currentBaseLayer = this.layers.arcgis;
    this.currentBaseLayer.addTo(this.map);
    // 默认也加入基础底图（但不显示，在图层控制中手动切换）
    // 基础底图作为可选底图已注册到 baseLayers 中

    // 添加交互工具
    this.addTaggingControl();
    // GeoLibre 图层工作台入口（右上角切换按钮）
    this.addGeoLibreControl();

    // 添加点击事件
    this.map.on('click', (e) => {
      if (this.markingMode) {
        const { lat, lng } = e.latlng;
        const marker = L.marker([lat, lng]).addTo(this.manualMarkers);
        const popupContent = `
          <div style="font-family: sans-serif;">
            <b style="color: #2c3e50;">手动标记点</b><br/>
            <span style="color: #7f8c8d;">纬度:</span> ${lat.toFixed(6)}<br/>
            <span style="color: #7f8c8d;">经度:</span> ${lng.toFixed(6)}
          </div>
        `;
        marker.bindPopup(popupContent).openPopup();
        return;
      }
    });

    // 添加缩放/拖拽事件的轻量防抖示例（可用于更新轻量 UI）
    const debounce = (fn, wait = 150) => {
      let t = null;
      return (...args) => {
        if (t) clearTimeout(t);
        t = setTimeout(() => fn(...args), wait);
      };
    };
    const onMoveEnd = debounce(() => {
      // 预留位置：此处仅作为节流示例，不做重逻辑
    }, 150);
    const onZoomEnd = debounce(() => {
      // 预留位置：此处仅作为节流示例，不做重逻辑
    }, 150);
    this.map.on('moveend', onMoveEnd);
    this.map.on('zoomend', onZoomEnd);

    // 2. 添加图层组到地图
    this.overlays.henanBaseMap.addTo(this.map);
    this.overlays.caiqu.addTo(this.map);
    this.overlays.caiqu2026.addTo(this.map);

    const baseLayers = {
      '2023年高分影像': this.layers.arcgis,
      '2024年高分影像': this.layers.gf2024,
      '2025年高分影像': this.layers.gf2025,
      '2026年Q1高分影像': this.layers.gf2026,
      'Esri卫星影像': this.layers.satellite
    };

    const overlays = {
      '2025年采区边界': this.overlays.caiqu,
      '2026年采区': this.overlays.caiqu2026,
      '基础底图': this.overlays.jcdtOverlay,
      '红线底图': this.overlays.henanBaseMap,
    };

    this.layerControl = L.control.layers(baseLayers, overlays, { position: 'topleft' }).addTo(this.map);
    this.loadDroneImageryLayers();

    const xinyangBounds = L.latLngBounds([31.5, 114.5], [32.7, 116.5]);
    this.map.fitBounds(xinyangBounds.pad(0.05));

    // leaflet.vectorgrid 已作为 npm 依赖引入，无需 CDN 加载

    // 初始化智能标注管理器，缩放/移动结束后防抖刷新（200ms）
    this.labelManager = new LabelManager(this.map);
    this.map.on('zoomend moveend', () => {
      if (this.labelManager) this.labelManager.scheduleRefresh(200);
    });

    return this.map;
  }

  emitLayerStatus(status, detail = {}) {
    if (this.onLayerStatus) {
      this.onLayerStatus({ status, ...detail });
    }
  }

  initLayerPanes() {
    if (!this.map) return;
    if (!this.map.getPane('droneImageryPane')) {
      const pane = this.map.createPane('droneImageryPane');
      pane.style.zIndex = 390;
      pane.style.pointerEvents = 'none';
    }
    if (!this.map.getPane('businessOverlayPane')) {
      const pane = this.map.createPane('businessOverlayPane');
      pane.style.zIndex = 450;
    }
    if (!this.map.getPane('hxPane')) {
      const pane = this.map.createPane('hxPane');
      pane.style.zIndex = 460;
    }
    if (!this.map.getPane('caiquPane')) {
      const pane = this.map.createPane('caiquPane');
      pane.style.zIndex = 470;
    }
    if (!this.map.getPane('pointDataPane')) {
      const pane = this.map.createPane('pointDataPane');
      pane.style.zIndex = 620;
    }
  }

  shouldShowLayer(layerKey, zoomLevel) {
    const threshold = this.visibilityThresholds?.[layerKey];
    if (typeof threshold !== 'number') {
      return true;
    }
    return zoomLevel >= threshold;
  }

  escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  getHXLabelMap() {
    return {
      HHMC: '河流名称',
      HHDM: '河流代码',
      AB: '岸别',
      XZQDM: '行政区代码',
      FWXLB: '范围线类别',
      SJLY: '数据来源',
    };
  }

  getCaiquLabelMap() {
    return {
      'alow_area_': '采区名称',
      '所属县': '所属县',
      '许可证': '许可证',
      '类型': '类型',
      '状态': '状态',
      '名称': '名称',
      'SHAPE': '几何类型',
      'ID': '编号',
      'Name': '名称',
      'name': '名称',
    };
  }

  buildFeatureRows(properties, labelMap = {}) {
    const entries = Object.entries(properties || {}).filter(([key, value]) => (
      key !== 'description' && value !== null && value !== undefined && value !== ''
    ));
    return entries
      .map(([key, value]) => `<tr><th style="text-align:left;white-space:nowrap;color:#595959;padding:4px 8px 4px 0;border-bottom:1px solid #f0f0f0;">${this.escapeHtml(labelMap[key] || key)}</th><td style="color:#262626;padding:4px 0;border-bottom:1px solid #f0f0f0;">${this.escapeHtml(value)}</td></tr>`)
      .join('');
  }

  openFeaturePopup(title, titleColor, latlng, properties, labelMap = {}, distanceM = null) {
    const rows = this.buildFeatureRows(properties, labelMap);
    const distanceRow = typeof distanceM === 'number'
      ? `<tr><th style="text-align:left;white-space:nowrap;color:#595959;padding:4px 8px 4px 0;">点击距离</th><td style="color:#262626;padding:4px 0;">${distanceM.toFixed(1)} 米</td></tr>`
      : '';
    L.popup({ maxWidth: 360 })
      .setLatLng(latlng)
      .setContent(`
        <div style="min-width:240px;max-height:260px;overflow:auto;font-size:13px;">
          <div style="font-weight:700;color:${titleColor};margin-bottom:8px;">${this.escapeHtml(title)}</div>
          <table style="border-collapse:collapse;width:100%;">
            ${rows || '<tr><td>无属性</td></tr>'}
            ${distanceRow}
          </table>
        </div>
      `)
      .openOn(this.map);
  }

  openHXPopup(latlng, properties, distanceM = null) {
    this.openFeaturePopup('河道红线信息', '#ef4444', latlng, properties, this.getHXLabelMap(), distanceM);
  }

  openCaiquPopup(latlng, properties, distanceM = null) {
    this.openFeaturePopup('采区边界信息', '#0ea5e9', latlng, properties, this.getCaiquLabelMap(), distanceM);
  }

  getApiUrlCandidates(path) {
    const candidates = [path];
    if (typeof window !== 'undefined' && window.location?.hostname) {
      candidates.push(`${window.location.protocol}//${window.location.hostname}:8006${path}`);
    }
    candidates.push(`http://127.0.0.1:8006${path}`);
    return [...new Set(candidates)];
  }

  async fetchFirstAvailableJson(path) {
    const headers = { 'Accept': 'application/json' };
    for (const url of this.getApiUrlCandidates(path)) {
      try {
        const response = await fetch(url, { headers });
        if (response.ok) {
          const contentType = response.headers.get('content-type') || '';
          if (!contentType.includes('json')) continue;
          return await response.json();
        }
      } catch (_) {}
    }
    return null;
  }

  async queryOverlayFeatureAt(layerKey, overlayGroup, latlng, openPopup, warningLabel) {
    if (!this.map || !overlayGroup || !this.map.hasLayer(overlayGroup)) return false;
    try {
      const zoom = this.map.getZoom();
      const tolerance = Math.max(20, Math.min(120, 160 - zoom * 6));
      const path = `/api/overlay_feature/${layerKey}?lng=${encodeURIComponent(latlng.lng)}&lat=${encodeURIComponent(latlng.lat)}&tolerance_m=${encodeURIComponent(tolerance)}`;
      const data = await this.fetchFirstAvailableJson(path);
      if (!data?.found || !data.feature?.properties) return false;
      openPopup(data.feature.properties, data.feature.distance_m);
      return true;
    } catch (err) {
      console.warn(`查询${warningLabel}属性失败:`, err);
      return false;
    }
  }

  async queryHXFeatureAt(latlng) {
    return this.queryOverlayFeatureAt(
      'hx',
      this.overlays.henanBaseMap,
      latlng,
      (properties, distanceM) => this.openHXPopup(latlng, properties, distanceM),
      '河道红线'
    );
  }

  addSatelliteLayer() {
    const satelliteLayer = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {
        attribution: '&copy; <a href="https://www.esri.com/">Esri</a>',
        maxZoom: 18,
        maxNativeZoom: 18,
        updateWhenIdle: true,
        updateWhenZooming: false,
        keepBuffer: 4,
        crossOrigin: 'anonymous'
      }
    );
    this.layers.satellite = satelliteLayer;
  }


  addOSMLayer() {
    if (this.layers.osm) return;
    const osmLayer = L.tileLayer(
      'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 18,
        maxNativeZoom: 18,
        updateWhenIdle: true,
        updateWhenZooming: false,
        keepBuffer: 4,
        crossOrigin: 'anonymous'
      }
    );
    this.layers.osm = osmLayer;
  }

  addArcGis3857Layer() {
    const base = this.options.arcgisBaseUrl || 'http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202308_cache';
    const tpl = base.replace(/\/+$/, '') + '/MapServer/tile/{z}/{y}/{x}';
    const layer = L.tileLayer(tpl, {
      attribution: '2023年高分影像',
      maxZoom: 23,
      maxNativeZoom: 23,
      updateWhenIdle: true,
      updateWhenZooming: false,
      keepBuffer: 4,
      crossOrigin: 'anonymous'
    });
    layer.on('tileerror', (e) => {
      console.warn('ArcGIS瓦片加载失败:', e.url);
    });
    this.layers.arcgis = layer;
    try {
      const infoUrl = base.replace(/\/+$/, '') + '/MapServer?f=json';
      fetch(infoUrl)
        .then(r => r.ok ? r.json() : null)
        .then(j => {
          if (!j) return;
          const lods = (j.tileInfo && j.tileInfo.lods) || [];
          if (Array.isArray(lods) && lods.length > 0) {
            const mz = lods.length - 1;
            layer.options.maxZoom = mz;
            layer.options.maxNativeZoom = mz;
          }
          // 不在底图元数据回调里二次改视角，避免初始化先定位信阳后又跳远
        })
        .catch(() => {});
    } catch (_) {}
  }

  /** 基础底图：河南省基础底图 HNS_JCDT_SL (ArcGIS MapServer) */
  addJCDTBaseLayer() {
    const url = 'http://gis95.yskc.com/server/rest/services/%E6%B2%B3%E5%8D%97%E7%9C%81%E5%9F%BA%E7%A1%80%E5%BA%95%E5%9B%BE/HNS_JCDT_SL/MapServer';
    const layer = EL.tiledMapLayer({
      url: url,
      maxZoom: 18,
      attribution: '基础底图',
      crossOrigin: 'anonymous',
    });
    layer.on('tileerror', (e) => {
      console.warn('基础底图瓦片加载失败:', e.url);
    });
    this.layers.jcdt = layer;
  }

  /** 2024年高分影像 GF_2024_YM (ArcGIS 瓦片服务，通过后端代理) */
  addGF2024Layer() {
    const tpl = '/proxy/gf-tiles/{z}/{y}/{x}';
    const layer = L.tileLayer(tpl, {
      attribution: '2024年高分影像',
      maxZoom: 18,
      maxNativeZoom: 18,
      updateWhenIdle: true,
      updateWhenZooming: false,
      keepBuffer: 4,
      crossOrigin: 'anonymous',
    });
    layer.on('tileerror', (e) => {
      console.warn('2024高分影像瓦片加载失败:', e.tile?.src);
    });
    this.layers.gf2024 = layer;
  }

  /** 2025年高分影像 GF_202509_cache (ArcGIS 瓦片服务，通过后端代理) */
  addGF2025Layer() {
    const tpl = '/proxy/gf2025-tiles/{z}/{y}/{x}';
    const layer = L.tileLayer(tpl, {
      attribution: '2025年高分影像',
      maxZoom: 18,
      maxNativeZoom: 18,
      updateWhenIdle: true,
      updateWhenZooming: false,
      keepBuffer: 4,
      crossOrigin: 'anonymous',
    });
    layer.on('tileerror', (e) => {
      console.warn('2025高分影像瓦片加载失败:', e.tile?.src);
    });
    this.layers.gf2025 = layer;
  }

  /** 2026年第一季度高分影像 (本地 GeoTIFF → serve_tile.py → 后端代理) */
  addGF2026Layer() {
    const tpl = '/proxy/gf2026-tiles/{z}/{y}/{x}';
    const layer = L.tileLayer(tpl, {
      attribution: '2026年Q1高分影像',
      maxZoom: 18,
      maxNativeZoom: 18,
      updateWhenIdle: true,
      updateWhenZooming: false,
      keepBuffer: 4,
      crossOrigin: 'anonymous',
    });
    layer.on('tileerror', (e) => {
      console.warn('2026高分影像瓦片加载失败:', e.tile?.src);
    });
    this.layers.gf2026 = layer;
  }

  parseCaiquDescription(html) {
    if (!html || typeof html !== 'string' || !html.includes('<t')) return null;
    const result = {};
    const regex = /<tr[^>]*>\s*<td[^>]*>([^<]*)<\/td>\s*<td[^>]*>([^<]*)<\/td>\s*<\/tr>/gi;
    let match;
    while ((match = regex.exec(html)) !== null) {
      const key = match[1].trim();
      const value = match[2].trim();
      if (key && value) result[key] = value;
    }
    return Object.keys(result).length > 0 ? result : null;
  }

  addLocalGeoJSONLayer() {
    if (this.overlays.caiqu) {
      this.overlays.caiqu.clearLayers();
    }
    fetch('/data/caiqu.geojson')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data || !data.features) {
          console.warn('[采区] geojson数据为空');
          return;
        }
        console.log('[采区] 加载成功，要素数:', data.features.length);
        const self = this;
        const svgRenderer = L.svg({ pane: 'caiquPane' });
        const geoLayer = L.geoJSON(data, {
          pane: 'caiquPane',
          renderer: svgRenderer,
          interactive: true,
          bubblingMouseEvents: false,
          style: () => ({
            color: '#38bdf8',
            weight: 2,
            fillColor: '#38bdf8',
            fillOpacity: 0.18,
          }),
          onEachFeature: (feature, layer) => {
            const rawProps = feature.properties || {};
            const parsed = self.parseCaiquDescription(rawProps.description) || {};
            const displayProps = Object.keys(parsed).length > 0 ? parsed : rawProps;
            const rows = Object.entries(displayProps)
              .filter(([k, v]) => k !== 'description' && v)
              .map(([k, v]) => `<tr><td style="padding:2px 6px;color:#595959;">${k}</td><td style="padding:2px 6px;">${v}</td></tr>`)
              .join('');
            layer.bindPopup(
              `<div style="min-width:200px;font-size:13px;"><b style="color:#0ea5e9;">采区边界信息</b><table>${rows || '<tr><td>无属性</td></tr>'}</table></div>`,
              { maxWidth: 360 }
            );
          },
        });
        this.overlays.caiqu.addLayer(geoLayer);
        console.log('[采区] 图层已添加到地图, layers:', geoLayer.getLayers().length);
      })
      .catch(err => console.warn('加载采区GeoJSON失败:', err));
  }

  addCaiqu2026Layer() {
    if (this.overlays.caiqu2026) {
      this.overlays.caiqu2026.clearLayers();
    }
    fetch('/data/caiqu2026.geojson')
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data || !data.features) {
          console.warn('[2026年采区] geojson数据为空');
          return;
        }
        console.log('[2026年采区] 加载成功，要素数:', data.features.length);
        const self = this;
        const svgRenderer = L.svg({ pane: 'caiquPane' });
        const geoLayer = L.geoJSON(data, {
          pane: 'caiquPane',
          renderer: svgRenderer,
          interactive: true,
          bubblingMouseEvents: false,
          style: () => ({
            color: '#f59e0b',
            weight: 2,
            fillColor: '#f59e0b',
            fillOpacity: 0.18,
          }),
          onEachFeature: (feature, layer) => {
            const props = feature.properties || {};
            const displayName = props.Name || '未命名';
            const desc = props.descriptio || '';
            layer.bindPopup(
              `<div style="min-width:200px;font-size:13px;"><b style="color:#f59e0b;">2026年采区</b><br/><b>名称:</b> ${self.escapeHtml(displayName)}${desc ? '<br/><b>描述:</b> ' + self.escapeHtml(desc.substring(0, 200)) : ''}</div>`,
              { maxWidth: 360 }
            );
          },
        });
        this.overlays.caiqu2026.addLayer(geoLayer);
        console.log('[2026年采区] 图层已添加到地图');
      })
      .catch(err => console.warn('加载2026年采区GeoJSON失败:', err));
  }

  addHXLayer() {
    if (this.overlays.henanBaseMap) {
      this.overlays.henanBaseMap.clearLayers();
    }
    const vtLayer = L.vectorGrid.protobuf('/api/vector_tile/hx/{z}/{x}/{y}.pbf', {
      maxZoom: 22,
      maxNativeZoom: 18,
      minZoom: 0,
      pane: 'hxPane',
      zIndex: 460,
      rendererFactory: L.canvas.tile,
      interactive: true,
      getFeatureId: (f) => f.properties.OBJECTID || f.properties.HHMC || f.id,
      vectorTileLayerStyles: {
        hx: {
          weight: 3,
          color: '#ef4444',
          fillColor: '#ef4444',
          fillOpacity: 0.05,
          fill: true,
        },
      },
      attribution: '河道红线',
    });
    vtLayer.on('click', (e) => {
      const props = e.layer?.properties;
      if (props) {
        this.openHXPopup(e.latlng, props);
      } else {
        this.queryHXFeatureAt(e.latlng);
      }
    });
    this.overlays.henanBaseMap.addLayer(vtLayer);
  }

  loadDroneImageryLayers() {
    if (!this.map || !this.layerControl) return;
    fetch('/api/drone_imagery/layers')
      .then(res => (res.ok ? res.json() : null))
      .then(data => {
        // 二次检查：防止 map/layerControl 在 fetch 期间被销毁（React Strict Mode 双重渲染）
        if (!this.map || !this.layerControl) return;
        if (!data || !data.success || !Array.isArray(data.layers)) return;
        const readyLayers = data.layers.filter(layer => layer.status === 'ready');
        if (readyLayers.length === 0) return;

        // 添加无人机影像分组标题（虚拟图层，仅作视觉分隔）
        const droneHeader = L.layerGroup();
        this.layerControl.addOverlay(droneHeader,
          '<span class="drone-group-header">2026年无人机影像 <span class="drone-group-arrow">▼</span></span>'
        );

        readyLayers.forEach(layer => {
          if (this.droneImageryLayers[layer.key]) return;
          // minZoom 设为 0 避免 Leaflet 因当前 zoom < 实际瓦片 minZoom 而禁用 checkbox
          // 勾选后由 'add' 事件触发 flyToBounds 自动定位，maxZoom=14 避免一次性放大过多
          const tileLayer = L.tileLayer(layer.api_url, {
            minZoom: 0,
            maxZoom: 22,
            maxNativeZoom: Number(layer.max_native_zoom ?? layer.max_zoom ?? 22),
            opacity: Number(layer.style?.opacity ?? 0.9),
            pane: 'droneImageryPane',
            zIndex: 680,
            updateWhenIdle: true,
            updateWhenZooming: false,
            keepBuffer: 2,
            attribution: layer.label || '无人机影像',
          });
          tileLayer.on('tileerror', (e) => {
            console.warn('无人机影像瓦片加载失败:', e.url);
          });
          // 被勾选显示时自动定位到影像范围（maxZoom=14 只缩放到可辨识范围，不一次性放大过多）
          tileLayer.on('add', () => {
            if (this._droneAutoLocated?.[layer.key]) return;
            if (Array.isArray(layer.bounds) && layer.bounds.length === 4) {
              const [minX, minY, maxX, maxY] = layer.bounds;
              const latLngBounds = L.latLngBounds([minY, minX], [maxY, maxX]);
              this.map.flyToBounds(latLngBounds.pad(0.1), {
                duration: 0.6,
                maxZoom: 14,
              });
              this._droneAutoLocated = this._droneAutoLocated || {};
              this._droneAutoLocated[layer.key] = true;
            }
          });
          this.droneImageryLayers[layer.key] = tileLayer;
          this.layerControl.addOverlay(tileLayer,
            `<span class="drone-layer-item">${layer.label || layer.key}</span><button class="drone-delete-btn" data-key="${layer.key}" title="删除此影像">×</button>`
          );
        });

        // 为无人机分组条目添加 CSS 标记类
        this._applyDroneGroupStyles();
        // 绑定删除按钮点击事件
        this._setupDroneDeleteHandler();
      })
      .catch(err => {
        console.warn('加载无人机影像图层列表失败:', err);
      });
  }

  /** 给 Leaflet 图层控制面板中的无人机条目打上 CSS 类，以实现视觉分组 */
  _applyDroneGroupStyles() {
    requestAnimationFrame(() => {
      const controlEl = document.querySelector('.leaflet-control-layers');
      if (!controlEl) return;
      // 在 overlays section 中查找所有 label
      const overlaysSection = controlEl.querySelector('.leaflet-control-layers-overlays')
        || controlEl.querySelector('section');
      if (!overlaysSection) return;
      const labels = overlaysSection.querySelectorAll('label');
      let groupLabel = null;
      labels.forEach(label => {
        if (label.querySelector('.drone-group-header')) {
          label.classList.add('drone-group-label');
          groupLabel = label;
        } else if (label.querySelector('.drone-layer-item')) {
          label.classList.add('drone-group-child');
        }
      });
      // 点击分组标题折叠/展开子图层
      if (groupLabel && !groupLabel._droneToggleBound) {
        groupLabel._droneToggleBound = true;
        groupLabel.style.cursor = 'pointer';
        groupLabel.addEventListener('click', (e) => {
          e.stopPropagation();
          e.preventDefault();
          const arrow = groupLabel.querySelector('.drone-group-arrow');
          const children = overlaysSection.querySelectorAll('.drone-group-child');
          const isCollapsed = groupLabel.classList.toggle('drone-group-collapsed');
          children.forEach(child => {
            child.style.display = isCollapsed ? 'none' : '';
          });
          if (arrow) {
            arrow.textContent = isCollapsed ? '▶' : '▼';
          }
        });
        // 默认折叠
        groupLabel.classList.add('drone-group-collapsed');
        const arrow = groupLabel.querySelector('.drone-group-arrow');
        if (arrow) arrow.textContent = '▶';
        // 延迟隐藏子条目（等 DOM 更新）
        setTimeout(() => {
          const children = overlaysSection.querySelectorAll('.drone-group-child');
          children.forEach(child => { child.style.display = 'none'; });
        }, 50);
      }
    });
  }

  /** 在图层控制面板上委托无人机影像删除按钮的点击事件 */
  _setupDroneDeleteHandler() {
    requestAnimationFrame(() => {
      const controlEl = document.querySelector('.leaflet-control-layers');
      if (!controlEl) return;
      const overlaysSection = controlEl.querySelector('.leaflet-control-layers-overlays')
        || controlEl.querySelector('section');
      if (!overlaysSection) return;
      // 事件委托：拦截删除按钮点击，阻止冒泡到 checkbox
      overlaysSection.addEventListener('click', (e) => {
        const btn = e.target.closest('.drone-delete-btn');
        if (!btn) return;
        e.stopPropagation();
        e.preventDefault();
        const key = btn.dataset.key;
        if (key) this._handleDroneLayerDelete(key);
      }, true);  // capture phase，优先于 Leaflet 的 input click 处理
    });
  }

  /** 从地图图层控制面板中删除指定无人机影像 */
  async _handleDroneLayerDelete(key) {
    if (!window.confirm('确定要删除此无人机影像吗？')) return;
    try {
      const resp = await fetch(`/api/drone_imagery/${encodeURIComponent(key)}?delete_files=false`, {
        method: 'DELETE',
      });
      if (!resp.ok) {
        console.warn('删除无人机影像失败:', resp.status);
        return;
      }
      // 从地图移除图层
      const tileLayer = this.droneImageryLayers[key];
      if (tileLayer) {
        if (this.layerControl) this.layerControl.removeLayer(tileLayer);
        if (this.map && this.map.hasLayer(tileLayer)) this.map.removeLayer(tileLayer);
        delete this.droneImageryLayers[key];
      }
      // 刷新分组样式
      this._applyDroneGroupStyles();
    } catch (err) {
      console.warn('删除无人机影像失败:', err);
    }
  }

  addTaggingControl() {
    const TaggingControl = L.Control.extend({
      options: {
        position: 'topleft'
      },
      onAdd: (map) => {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control leaflet-tagging-control');
        container.style.backgroundColor = 'white';
        container.style.display = 'flex';
        container.style.flexDirection = 'column';

        const toggleBtn = L.DomUtil.create('a', '', container);
        toggleBtn.innerHTML = '📍';
        toggleBtn.href = '#';
        toggleBtn.title = '打点标记工具';
        toggleBtn.style.fontSize = '18px';
        toggleBtn.style.display = 'flex';
        toggleBtn.style.alignItems = 'center';
        toggleBtn.style.justifyContent = 'center';

        const clearBtn = L.DomUtil.create('a', '', container);
        clearBtn.innerHTML = '🗑️';
        clearBtn.href = '#';
        clearBtn.title = '清除所有标记';
        clearBtn.style.fontSize = '16px';
        clearBtn.style.display = 'flex';
        clearBtn.style.alignItems = 'center';
        clearBtn.style.justifyContent = 'center';

        L.DomEvent.disableClickPropagation(container);

        L.DomEvent.on(toggleBtn, 'click', (e) => {
          L.DomEvent.stop(e);
          this.markingMode = !this.markingMode;
          toggleBtn.style.backgroundColor = this.markingMode ? '#e1f5fe' : 'white';
          toggleBtn.style.color = this.markingMode ? '#03a9f4' : 'black';
          map.getContainer().style.cursor = this.markingMode ? 'crosshair' : '';
        });

        L.DomEvent.on(clearBtn, 'click', (e) => {
          L.DomEvent.stop(e);
          this.manualMarkers.clearLayers();
        });

        return container;
      }
    });

    new TaggingControl().addTo(this.map);
  }

  // GeoLibre 图层工作台：左上角切换按钮 + iframe 覆盖层（自部署 GeoLibre，端口 8090）
  addGeoLibreControl() {
    const GeoLibreControl = L.Control.extend({
      options: { position: 'topleft' },
      onAdd: () => {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        const btn = L.DomUtil.create('a', '', container);
        btn.innerHTML = '🌍';
        btn.href = '#';
        btn.title = 'GeoLibre 图层工作台';
        btn.style.fontSize = '17px';
        btn.style.display = 'flex';
        btn.style.alignItems = 'center';
        btn.style.justifyContent = 'center';
        btn.style.width = '34px';
        btn.style.height = '34px';
        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(btn, 'click', (e) => {
          L.DomEvent.stop(e);
          this.toggleGeoLibre();
        });
        this._geolibreBtn = btn;
        return container;
      }
    });
    new GeoLibreControl().addTo(this.map);
  }

  getGeoLibreUrl() {
    const host = (typeof window !== 'undefined' && window.location?.hostname) || '127.0.0.1';
    // 通过 ?url= 深链接让 GeoLibre 加载后端动态生成的项目（含高分影像/河道红线/采区/3DTiles）
    const projectUrl = `http://${host}:8006/api/geolibre/project`;
    return `http://${host}:8090/?url=${encodeURIComponent(projectUrl)}`;
  }

  toggleGeoLibre() {
    if (!this.map) return;
    const container = this.map.getContainer();
    let overlay = this._geolibreOverlay;
    if (!overlay || !overlay.parentNode) {
      overlay = document.createElement('div');
      overlay.className = 'geolibre-overlay';
      Object.assign(overlay.style, {
        position: 'absolute',
        inset: '0',
        zIndex: '1100',
        background: '#ffffff',
        display: 'none',
        flexDirection: 'column',
      });
      const bar = document.createElement('div');
      Object.assign(bar.style, {
        height: '40px',
        flex: '0 0 40px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 12px',
        background: '#1f2937',
        color: '#ffffff',
        fontSize: '13px',
        fontFamily: 'inherit',
        boxSizing: 'border-box',
      });
      const title = document.createElement('span');
      title.textContent = 'GeoLibre 图层工作台（添加图层 → 选择数据源类型）';
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.textContent = '✕ 关闭';
      Object.assign(closeBtn.style, {
        border: 'none',
        background: 'rgba(255,255,255,0.15)',
        color: '#ffffff',
        padding: '4px 10px',
        borderRadius: '4px',
        cursor: 'pointer',
        fontSize: '13px',
      });
      closeBtn.addEventListener('click', () => this.toggleGeoLibre());
      bar.appendChild(title);
      bar.appendChild(closeBtn);
      const frame = document.createElement('iframe');
      frame.src = this.getGeoLibreUrl();
      frame.allow = 'geolocation; fullscreen';
      Object.assign(frame.style, { border: '0', width: '100%', flex: '1 1 auto' });
      overlay.appendChild(bar);
      overlay.appendChild(frame);
      container.appendChild(overlay);
      this._geolibreOverlay = overlay;
    }
    const show = overlay.style.display === 'none';
    overlay.style.display = show ? 'flex' : 'none';
    // GeoLibre 打开时隐藏上层地图工具按钮（截图/清除），关闭时恢复
    const wrapper = container.closest('.map-wrapper');
    if (wrapper) wrapper.classList.toggle('geolibre-open', show);
    if (this._geolibreBtn) {
      this._geolibreBtn.style.backgroundColor = show ? '#e1f5fe' : 'white';
    }
  }

  executeMapCommand(command) {
    if (!this.map || !command) return;

    const cmdType = command.type || command.command;

    switch (cmdType) {
      case 'load_vector_layer':
        this.addVectorLayerFromAPI(command.url, command.name);
        break;
      case 'load_vector_tile_layer':
        this.addVectorTileLayerFromAPI(command.url, command.name, command.style);
        break;
      case 'add_marker':
        this.addMarkerWithPopup(
          command.lat,
          command.lng,
          command.popup || command.title || '标记点'
        );
        break;
      case 'clear_markers':
        this.clearMarkers();
        break;
      case 'set_view':
        this.flyTo(command.lat, command.lng, command.zoom || this.options.zoom);
        break;
      case 'fit_markers':
        this.fitMarkers();
        break;
      case 'switch_layer':
        // 基础底图是叠加图层，不是底图——直接添加/显示
        if (command.layer === 'jcdt') {
          if (this.overlays.jcdtOverlay && !this.map.hasLayer(this.overlays.jcdtOverlay)) {
            this.map.addLayer(this.overlays.jcdtOverlay);
          }
          break;
        }

        // 以下为影像底图切换
        if (this.currentBaseLayer) {
          this.safelyRemoveLayer(this.currentBaseLayer);
        }
        let newLayer;
        if (command.layer === 'arcgis') {
          newLayer = this.layers.arcgis;
        } else if (command.layer === 'gf2024') {
          newLayer = this.layers.gf2024;
        } else if (command.layer === 'gf2025') {
          newLayer = this.layers.gf2025;
        } else if (command.layer === 'gf2026') {
          newLayer = this.layers.gf2026;
        } else {
          newLayer = this.layers.arcgis;
        }

        if (newLayer) {
          newLayer.addTo(this.map);
          this.currentBaseLayer = newLayer;
        }
        break;
      default:
        console.warn('未知的地图命令:', cmdType);
    }
  }

  addVectorLayerFromAPI(url, layerName = '矢量数据', renderConfig = {}) {
    if (!this.map) return;
    if (this.overlays[layerName]) {
      this._removeRiskCanvas(layerName);
      this.safelyRemoveLayer(this.overlays[layerName]);
    }

    const { style: renderStyle, view: renderView } = renderConfig;

    let fullUrl = url;
    if (url.startsWith('/')) {
      fullUrl = url;
    } else if (!/^https?:\/\//.test(url)) {
      const origin = typeof window !== 'undefined' ? window.location.origin : '';
      fullUrl = origin ? origin.replace(/\/+$/, '') + '/' + url.replace(/^\/+/, '') : '/' + url.replace(/^\/+/, '');
    }

    console.log('【调试】加载矢量图层 URL:', fullUrl);
    this.emitLayerStatus('loading', { layerName, url: fullUrl });

    // 每次加载新图层前，清空旧点位注册（避免标注残留）
    if (this.labelManager) this.labelManager.clear();

    fetch(fullUrl)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        return res.json();
      })
      .then(geojson => {
        let normalized = geojson;
        if (typeof normalized === 'string') {
          try {
            normalized = JSON.parse(normalized);
          } catch (e) {
            normalized = null;
          }
        }
        if (!normalized || typeof normalized !== 'object') {
          this.emitLayerStatus('error', { layerName, url: fullUrl, message: '返回数据格式异常' });
          throw new Error('返回数据格式异常');
        }
        if (!Array.isArray(normalized.features)) {
          normalized.features = [];
        }
        if (geojson && (geojson._error || geojson._debug)) {
          const dbg = geojson._debug || {};
          const err = geojson._error ? `错误: ${geojson._error}` : '';
          const stats = (dbg.matched_total !== undefined || dbg.geom_valid_total !== undefined || dbg.lonlat_total !== undefined)
            ? `匹配数=${dbg.matched_total ?? 'N/A'}，有效几何数=${dbg.geom_valid_total ?? 'N/A'}，经纬度数=${dbg.lonlat_total ?? 'N/A'}`
            : '';
          const applied = dbg.applied_filter ? `过滤条件: ${dbg.applied_filter}` : '';
          const msg = [err, stats, applied].filter(Boolean).join('\n');
          if (msg) {
            console.warn(msg);
          }
        }
        const featureCount = normalized.features.length;
        // 标注显示点数上限：超过此数量则按采样率稀疏注册，避免海量标注引起卡顿
        // === 修复：降低上限从2000→800，并使用更激进的采样率 ===
        const LABEL_MAX = 800;
        const labelSampleRate = featureCount > LABEL_MAX ? Math.max(Math.ceil(featureCount / LABEL_MAX), 3) : (featureCount > 400 ? 2 : 1);
        let labelIndex = 0;
        if (normalized._error) {
          this.emitLayerStatus('error', { layerName, url: fullUrl, message: normalized._error });
        } else if (featureCount === 0) {
          try {
            const hasRetried = /[?&]retry=1(&|$)/.test(fullUrl);
            if (!hasRetried) {
              const u = new URL(fullUrl, window.location.origin);
              const f = u.searchParams.get('filter') || '';
              const eqMatch = f.match(/\"Mineable_Area_Name\"\\s*=\\s*'([^']+)'/);
              const simpleName = eqMatch ? eqMatch[1] : layerName;
              u.searchParams.set('filter', `"Mineable_Area_Name"='${simpleName}'`);
              u.searchParams.set('retry', '1');
              const retryUrl = u.pathname + '?' + u.searchParams.toString();
              this.addVectorLayerFromAPI(retryUrl, layerName, renderConfig);
              return;
            }
          } catch (_) {}
          const meta = normalized.meta || {};
          this.emitLayerStatus('empty', {
            layerName,
            url: fullUrl,
            featureCount,
            message: meta.message || `图层 ${layerName} 无可用要素`
          });
        } else {
          this.emitLayerStatus('success', { layerName, url: fullUrl, featureCount });
        }
        // ── 样式驱动渲染：优先使用 recipe render 配置，否则走业务默认逻辑 ──
        const hasRecipeStyle = !!(renderStyle && (renderStyle.point || renderStyle.polygon));
        const layer = L.geoJSON(normalized, {
          pane: hasRecipeStyle ? 'markerPane' : 'pointDataPane',
          pointToLayer: (feature, latlng) => {
            const props = feature.properties || {};
            
            // ── recipe 配置的 point 样式 ──
            if (renderStyle && renderStyle.point) {
              return L.circleMarker(latlng, {
                pane: 'markerPane',
                ...renderStyle.point,
                interactive: true,
              });
            }
            
            // 优先用实测高程 vs 控制高程判断颜色（业务逻辑优先，不依赖后端 _style_color）
            const depth = props.Measured_Depth || props.measured_depth || props.h || props.depth || props.value;
            const control = props.Control_Elevation || props.control_elevation || props.control;
            
            let fillColor;
            if (depth !== undefined && control !== undefined) {
              // 业务逻辑：实测高程 < 控制高程 → 已超深（红色）；实测高程 >= 控制高程 → 未超深（绿色）
              fillColor = Number(depth) < Number(control) ? "red" : "green";
            } else if (props._style_color) {
              // 没有高程对比数据时才回退到后端提供的颜色
              fillColor = props._style_color;
            } else {
              fillColor = "#cc0000"; // 最终兜底：红色
            }
            
            const isRisk = this._isRiskLayer ? this._isRiskLayer(normalized.features) : false;
            return L.circleMarker(latlng, {
              pane: 'pointDataPane',
              radius: isRisk ? 5 : 6,
              fillColor: fillColor,
              color: isRisk ? fillColor : '#fff',
              weight: isRisk ? 0 : 1,
              fillOpacity: isRisk ? 0.45 : 0.8,
              interactive: true,
            });
          },
          // ── recipe 配置的 polygon 样式 ──
          style: (feature) => {
            if (renderStyle && renderStyle.polygon) {
              return renderStyle.polygon;
            }
            return {};
          },
          onEachFeature: (feature, layer) => {
            const props = feature.properties || {};
            // 移除调试日志（大量点位时 console.log 也会显著拖慢）
            
            // 动态识别坐标字段（兼容大小写）
            const lon = props.Lon_4326 || props.lon_4326 || props.lon || props.x || 0;
            const lat = props.Lat_4326 || props.lat_4326 || props.lat || props.y || 0;
            const coordText = `(${Number(lon).toFixed(6)}, ${Number(lat).toFixed(6)})`;
            
            // 动态识别深度/高程字段（兼容大小写）
            const depth = props.Measured_Depth || props.measured_depth || props.h || props.depth || props.value;
            const control = props.Control_Elevation || props.control_elevation || props.control;
            
            let depthText = '';
            if (depth !== undefined) {
              // 业务逻辑：测量值（高程） < 控制高程 -> 已超深(红色)；测量值 >= 控制高程 -> 未超深(绿色)
              const isOverDeep = control !== undefined && Number(depth) < Number(control);
              const status = control !== undefined 
                ? (isOverDeep ? ' <span style="color:red; font-weight:bold;">(已超深)</span>' : ' <span style="color:green; font-weight:bold;">(未超深)</span>')
                : '';
              depthText = `<br><b>实测高程:</b> ${Number(depth).toFixed(3)} m${status}`;
            }
            
            if (control !== undefined) {
              depthText += `<br><b>红线标准(控制高程):</b> ${Number(control).toFixed(3)} m`;
            }
            
            // 其他常用字段
            const areaName = props.Mineable_Area_Name || props.mineable_area_name || props.name || layerName;
            const areaId = props.Mineable_Area_ID || props.mineable_area_id || '';
            const county = props.County_District || props.county_district || '';
            const year = (props.Year || props.year) ? `<br><b>年份:</b> ${props.Year || props.year}` : '';
            
            const idText = areaId ? `<br><b>许可证号:</b> ${areaId}` : '';
            const countyText = county ? `<br><b>所属县区:</b> ${county}` : '';
            
            const popupContent = `<b>${areaName}</b>${idText}${countyText}<br><b>坐标:</b> ${coordText}${depthText}${year}`;
            layer.bindPopup(popupContent);
            
            // 标注：按采样率稀疏注册，超过上限的点不绑定 Tooltip
            labelIndex++;
            if (labelIndex % labelSampleRate !== 0) return;

            // 标注文字：优先显示实测高程，无高程数据时展示采区名称
            const depthLabel = depth !== undefined
              ? `${Number(depth).toFixed(2)}`
              : areaName;

            // 获取点位的地理坐标（供 LabelManager 像素计算）
            const coords = feature.geometry?.coordinates;
            const featureLatlng = coords ? L.latLng(coords[1], coords[0]) : null;

            // 绑定 Tooltip（初始关闭，由 LabelManager 统一调度开关）
            layer.bindTooltip(depthLabel, {
              permanent: true,
              direction: 'top',
              offset: [0, -8],
              className: 'map-label-depth'
            });
            layer.closeTooltip();

            // 向 LabelManager 注册当前点位
            if (featureLatlng && this.labelManager) {
              this.labelManager.register(featureLatlng, layer, depthLabel);
            }
          }
        });

        layer.addTo(this.map);
        this.overlays[layerName] = layer;

        // 风险点 → 渐变插值面
        if (this._isRiskLayer(normalized.features)) {
          this._addRiskGradientCanvas(layerName, normalized.features);
        }

        // 图层渲染完成后，预留 300ms 再触发首次智能标注调度
        requestAnimationFrame(() => {
          if (this.labelManager) this.labelManager.scheduleRefresh(300);
        });

        // 自动缩放（由 recipe render.view 配置驱动）
        if (normalized.features?.length > 0) {
          if (renderView && renderView.strategy === 'fly_to_centroid' && normalized.features.length === 1) {
            const coords = normalized.features[0].geometry?.coordinates;
            if (coords && coords.length >= 2) {
              this.map.flyTo([coords[1], coords[0]], renderView.zoom || 15, { duration: 1 });
            }
          } else if (renderView && renderView.strategy === 'fit_bounds') {
            const bounds = L.geoJSON(normalized).getBounds();
            this.map.fitBounds(bounds.pad(0.1));
          } else if (!renderView) {
            // 无 view 配置 → 保持现有行为：自适应边界
            const bounds = L.geoJSON(normalized).getBounds();
            this.map.fitBounds(bounds.pad(0.1));
          }
        } else {
          console.warn(`图层 ${layerName} 无可用要素，保持当前视图。`);
        }
      })
      .catch(err => {
        console.error('加载矢量数据失败:', err);
        this.emitLayerStatus('error', { layerName, url: fullUrl, message: err.message });
      });
  }

  addVectorTileLayerFromAPI(urlTemplate, layerName = 'mvt', style = {}) {
    if (!this.map || !L.vectorGrid || !L.vectorGrid.protobuf) return;
    if (this.overlays[layerName]) {
      this.safelyRemoveLayer(this.overlays[layerName]);
    }
    const BACKEND_BASE = '';  // 使用相对路径，由 CRA proxy 或 Nginx 反代转发到后端
    let tpl = urlTemplate;
    if (urlTemplate.startsWith('/')) {
      tpl = BACKEND_BASE + urlTemplate;
    } else if (!urlTemplate.startsWith('http')) {
      tpl = BACKEND_BASE + '/' + urlTemplate;
    }
    const defaultStyle = {
      weight: 1,
      color: '#2773d7',
      fillColor: '#fee8c8',
      fillOpacity: 0.6
    };
    const layer = L.vectorGrid.protobuf(tpl, {
      maxNativeZoom: 18,
      rendererFactory: L.canvas.tile,
      vectorTileLayerStyles: {
        [layerName]: Object.assign({}, defaultStyle, style),
        '*': Object.assign({}, defaultStyle, style)
      },
      interactive: true,
    });
    layer.addTo(this.map);
    this.overlays[layerName] = layer;
  }

  clearMarkers() {
    if (!this.map) {
      this.markers = [];
      return;
    }
    this.markers.forEach(marker => this.safelyRemoveLayer(marker));
    this.markers = [];
  }

  clearAllLayers() {
    if (!this.map) {
      this.markers = [];
      this.overlays = {};
      return;
    }
    this.markers.forEach(marker => this.safelyRemoveLayer(marker));
    this.markers = [];
    Object.keys(this.overlays).forEach(key => {
      if (this.overlays[key]) {
        this.overlays[key].clearLayers();
        if (this.map.hasLayer(this.overlays[key])) {
          this.safelyRemoveLayer(this.overlays[key]);
        }
      }
    });
  }

  addMarker(lat, lng, options = {}) {
    const marker = L.marker([lat, lng], options).addTo(this.map);
    this.markers.push(marker);
    return marker;
  }

  addMarkerWithPopup(lat, lng, popupContent, options = {}) {
    const marker = this.addMarker(lat, lng, options);
    marker.bindPopup(popupContent);
    return marker;
  }

  setView(lat, lng, zoom = this.options.zoom) {
    if (this.map) this.map.setView([lat, lng], zoom);
  }

  flyTo(lat, lng, zoom = this.options.zoom) {
    if (this.map) {
      this.map.flyTo([lat, lng], zoom, {
        duration: 1.2,           // 飞行时长（秒）
        easeLinearity: 0.15,     // 缓动曲线（越小越平滑）
        noMoveStart: true,       // 不触发 movestart 事件
      });
    }
  }

  fitMarkers() {
    if (this.markers.length > 0) {
      const group = L.featureGroup(this.markers);
      this.map.fitBounds(group.getBounds().pad(0.1));
    }
  }

  getMap() {
    return this.map;
  }

  destroy() {
    if (this.labelManager) {
      this.labelManager.destroy();
      this.labelManager = null;
    }
    // 清理 layerControl 引用，防止已销毁实例的异步回调误操作
    this.layerControl = null;
    // 清理无人机影像记录，确保下次初始化时可重新加载
    this.droneImageryLayers = {};
    if (this.map) {
      // 先移除所有 Leaflet 事件监听，避免 map.remove() 后残留 handler 触发
      try { this.map.off(); } catch (_) {}
      try { this.map.remove(); } catch (_) {}
      this.map = null;
    }
  }
}

const MapComponent = ({ onMapReady, onLayerStatus, className = '', options = {}, visible = true }) => {
  const mapRef = useRef(null);
  const mapManagerRef = useRef(null);
  const isInitializedRef = useRef(false);

  const stableOnMapReady = useCallback(
    (mapManagerInstance) => {
      if (onMapReady && typeof onMapReady === 'function') {
        onMapReady(mapManagerInstance);
      }
    },
    [onMapReady]
  );

  // 初始化地图（只在容器可见时，避免 display:none 时用 0x0 尺寸创建地图）
  useEffect(() => {
    if (!visible || !mapRef.current || isInitializedRef.current) return;
    isInitializedRef.current = true;
    try {
      const manager = new MapManager(mapRef.current, { ...options, onLayerStatus });
      mapManagerRef.current = manager;
      manager.initMap();
      stableOnMapReady(manager);

      // 监听容器尺寸变化（CSS display:none → block 切换时自动重算地图尺寸）
      let lastW = 0, lastH = 0;
      const resizeObserver = new ResizeObserver(() => {
        const w = mapRef.current.clientWidth;
        const h = mapRef.current.clientHeight;
        if (w > 0 && h > 0 && (w !== lastW || h !== lastH)) {
          lastW = w;
          lastH = h;
          try { manager.map.invalidateSize(); } catch (_) {}
        }
      });
      resizeObserver.observe(mapRef.current);
      manager._resizeObserver = resizeObserver;
    } catch (error) {
      console.error('地图初始化失败:', error);
      isInitializedRef.current = false;
    }
  }, [visible, options, onLayerStatus, stableOnMapReady]);

  // 组件卸载时清理地图
  useEffect(() => {
    return () => {
      if (mapManagerRef.current) {
        try {
          if (mapManagerRef.current._resizeObserver) {
            mapManagerRef.current._resizeObserver.disconnect();
          }
          mapManagerRef.current.destroy();
        } catch (error) {
          console.error('地图销毁出错:', error);
        }
        mapManagerRef.current = null;
        isInitializedRef.current = false;
      }
    };
  }, []);

  return (
    <div
      ref={mapRef}
      className={`map-container ${className}`}
      style={{ width: '100%', height: '100%' }}
    />
  );
};

export default MapComponent;
export { MapManager };
