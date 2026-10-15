// ============================================================
// MapComponent React 壳（自拆分后保留，路径与 default export 不变）
//
// 说明：LabelManager 已拆至 ./LabelManager.js；MapManager 命令式控制器及其
// 直接依赖的模块级副作用（Leaflet monkey-patch / marker 图标 CDN 路径 / risk
// 色标工具等，模块加载即执行）已随 ./MapManager.js 一并迁出。因此 import
// 本文件时，上述副作用随 ./MapManager.js 模块顶层立即执行，与原文件
// “模块加载即执行”的语义一致。MapManager/LabelManager 类与相关工具函数
// 不再在本文件内定义。
// ============================================================
import React, { useEffect, useRef, useCallback } from 'react';
import { MapManager } from './MapManager';

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
