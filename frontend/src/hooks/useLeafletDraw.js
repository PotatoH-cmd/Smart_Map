import { useRef, useCallback, useEffect } from 'react';
import L from 'leaflet';

/**
 * 共享 Leaflet 绘制 Hook
 * 提供矩形/多边形绘制、清理、GeoJSON 输出等能力
 * 被 SAMPanel 和 AnnotationPanel 共同使用
 *
 * @param {object} mapManager - 地图管理器 { map, ... }
 * @param {object} [opts] - 可选配置
 * @param {string} [opts.paneName] - 自定义 pane 名称（默认使用 overlayPane）
 * @param {number} [opts.paneZIndex] - pane z-index
 * @param {object} [opts.style] - 自定义绘制样式 { color, fillColor, fillOpacity, weight }
 * @param {L.Renderer} [opts.renderer] - 自定义渲染器（如 L.svg）
 */
export default function useLeafletDraw(mapManager, opts = {}) {
  const {
    paneName = null,
    paneZIndex = 650,
    style = {},
    renderer = null,
  } = opts;

  const defaultStyle = {
    color: style.color || '#ff6b00',
    weight: style.weight || 2,
    fillColor: style.fillColor || '#ff6b00',
    fillOpacity: style.fillOpacity != null ? style.fillOpacity : 0.15,
  };
  const polyStyle = {
    color: style.polyColor || defaultStyle.color,
    weight: style.polyWeight || 2,
    fillColor: style.polyFillColor || defaultStyle.fillColor,
    fillOpacity: style.polyFillOpacity != null ? style.polyFillOpacity : 0.15,
    dashArray: style.polyDashArray || '6',
  };

  const drawnItemsRef = useRef(null);
  const currentHandlerRef = useRef(null);

  // 初始化 FeatureGroup + 自定义 pane
  useEffect(() => {
    if (!mapManager?.map) return;
    const map = mapManager.map;

    // 创建自定义 pane（如果需要）
    let pane = null;
    if (paneName) {
      if (!map.getPane(paneName)) {
        pane = map.createPane(paneName);
        pane.style.zIndex = String(paneZIndex);
        pane.style.pointerEvents = 'auto';
      }
    }

    if (!drawnItemsRef.current) {
      const fgOpts = {};
      if (paneName) fgOpts.pane = paneName;
      if (renderer) fgOpts.renderer = renderer;
      drawnItemsRef.current = new L.FeatureGroup();
      map.addLayer(drawnItemsRef.current);
    }
    return () => {
      if (drawnItemsRef.current && map.hasLayer(drawnItemsRef.current)) {
        map.removeLayer(drawnItemsRef.current);
      }
    };
  }, [mapManager, paneName, paneZIndex, renderer]);

  // 清理当前绘制 handler
  const cleanupHandler = useCallback(() => {
    if (currentHandlerRef.current) {
      currentHandlerRef.current();
      currentHandlerRef.current = null;
    }
  }, []);

  // 矩形绘制 — 直接绑定 DOM 事件，绕过 Leaflet 拖拽干扰
  const startRectDraw = useCallback((onComplete) => {
    if (!mapManager?.map) return;
    const map = mapManager.map;
    cleanupHandler();

    const container = map.getContainer();
    if (!container) return;

    // 立即禁用地图拖拽
    map.dragging.disable();
    // 防止 Leaflet 也处理这些事件
    map._handlers.forEach(h => h.disable());

    let startLatLng = null;
    let tempRect = null;

    const getLatLng = (e) => {
      const rect = container.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      return map.containerPointToLatLng([x, y]);
    };

    const onMouseDown = (e) => {
      if (e.button !== 0) return; // 只响应左键
      e.preventDefault();
      e.stopPropagation();
      startLatLng = getLatLng(e);
    };

    const onMouseMove = (e) => {
      if (!startLatLng) return;
      e.preventDefault();
      e.stopPropagation();
      if (tempRect) map.removeLayer(tempRect);
      const latlng = getLatLng(e);
      const rectOpts = { ...defaultStyle, fillOpacity: 0.1, dashArray: '6' };
      if (paneName) rectOpts.pane = paneName;
      if (renderer) rectOpts.renderer = renderer;
      tempRect = L.rectangle(L.latLngBounds(startLatLng, latlng), rectOpts).addTo(map);
    };

    const onMouseUp = (e) => {
      if (!startLatLng) return;
      e.preventDefault();
      e.stopPropagation();
      if (tempRect) map.removeLayer(tempRect);
      const latlng = getLatLng(e);

      // 检查最小拖拽距离（至少 5 像素，避免点击误触产生零面积矩形）
      const pixelDist = Math.sqrt(
        Math.pow(e.clientX - (container.getBoundingClientRect().left + map.latLngToContainerPoint(startLatLng).x), 2) +
        Math.pow(e.clientY - (container.getBoundingClientRect().top + map.latLngToContainerPoint(startLatLng).y), 2)
      );
      if (pixelDist < 5) {
        console.log('[Draw] 矩形拖拽距离过小（<5px），已跳过');
        startLatLng = null;
        // 拖拽距离太小但也要清理，否则地图一直锁着
        cleanupHandler();
        return;
      }

      const bounds = L.latLngBounds(startLatLng, latlng);
      if (bounds.isValid() && Math.abs(bounds.getNorth() - bounds.getSouth()) > 0.000001
          && Math.abs(bounds.getEast() - bounds.getWest()) > 0.000001) {
        const rectOpts = { ...defaultStyle };
        if (paneName) rectOpts.pane = paneName;
        if (renderer) rectOpts.renderer = renderer;
        const rect = L.rectangle(bounds, rectOpts);
        drawnItemsRef.current.addLayer(rect);
        if (onComplete) onComplete(rect);
      }
      startLatLng = null;
      // 绘制完成，恢复地图交互
      cleanupHandler();
    };

    container.addEventListener('mousedown', onMouseDown, true);
    container.addEventListener('mousemove', onMouseMove, true);
    container.addEventListener('mouseup', onMouseUp, true);

    currentHandlerRef.current = () => {
      container.removeEventListener('mousedown', onMouseDown, true);
      container.removeEventListener('mousemove', onMouseMove, true);
      container.removeEventListener('mouseup', onMouseUp, true);
      map.dragging.enable();
      map._handlers.forEach(h => h.enable());
    };
  }, [mapManager, cleanupHandler, paneName, renderer, defaultStyle]);

  // 多边形绘制 — 直接绑定 DOM 事件
  const startPolyDraw = useCallback((onComplete) => {
    if (!mapManager?.map) return;
    const map = mapManager.map;
    cleanupHandler();

    const container = map.getContainer();
    if (!container) return;

    map.dragging.disable();

    const points = [];
    let tempLine = null;
    const markers = [];

    const getLatLng = (e) => {
      const rect = container.getBoundingClientRect();
      return map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
    };

    const onClick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const latlng = getLatLng(e);
      points.push(latlng);
      if (tempLine) map.removeLayer(tempLine);
      const m = L.circleMarker(latlng, {
        radius: 4, color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: 0.8,
      }).addTo(map);
      markers.push(m);
      if (points.length >= 2) {
        tempLine = L.polyline(points, {
          color: '#f59e0b', weight: 2, dashArray: '5 5',
        }).addTo(map);
      }
    };

    const onDblClick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (points.length >= 3) {
        markers.forEach(m => map.removeLayer(m));
        if (tempLine) map.removeLayer(tempLine);
        const polyOpts = { ...polyStyle };
        if (paneName) polyOpts.pane = paneName;
        if (renderer) polyOpts.renderer = renderer;
        const poly = L.polygon(points, polyOpts);
        drawnItemsRef.current.addLayer(poly);
        if (onComplete) onComplete(poly);
      }
      cleanup();
    };

    const cleanup = () => {
      container.removeEventListener('click', onClick, true);
      container.removeEventListener('dblclick', onDblClick, true);
      markers.forEach(m => map.removeLayer(m));
      if (tempLine) map.removeLayer(tempLine);
      map.dragging.enable();
    };

    container.addEventListener('click', onClick, true);
    container.addEventListener('dblclick', onDblClick, true);

    currentHandlerRef.current = cleanup;
  }, [mapManager, cleanupHandler, paneName, renderer, polyStyle]);

  // 清除所有绘制
  const clearDrawings = useCallback(() => {
    cleanupHandler();
    if (drawnItemsRef.current) {
      drawnItemsRef.current.clearLayers();
    }
  }, [cleanupHandler]);

  // 获取当前绘制 GeoJSON
  const getDrawnGeoJSON = useCallback(() => {
    if (!drawnItemsRef.current || drawnItemsRef.current.getLayers().length === 0) {
      return null;
    }
    const layers = drawnItemsRef.current.getLayers();
    return layers.map(l => l.toGeoJSON());
  }, []);

  // 获取绘制区域 bounds
  const getDrawnBounds = useCallback(() => {
    if (!drawnItemsRef.current) return null;
    const bounds = drawnItemsRef.current.getBounds();
    if (!bounds || !bounds.isValid()) return null;
    return {
      north: bounds.getNorth(),
      south: bounds.getSouth(),
      east: bounds.getEast(),
      west: bounds.getWest(),
    };
  }, []);

  // 获取绘制数量
  const getDrawCount = useCallback(() => {
    return drawnItemsRef.current ? drawnItemsRef.current.getLayers().length : 0;
  }, []);

  return {
    drawnItemsRef,
    startRectDraw,
    startPolyDraw,
    clearDrawings,
    getDrawnGeoJSON,
    getDrawnBounds,
    getDrawCount,
    cleanupHandler,
  };
}
