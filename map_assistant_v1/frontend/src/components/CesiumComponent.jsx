/**
 * CesiumComponent.jsx
 * Cesium 3D 地图组件（天地图底图，国内可用）
 */
import React, { useEffect, useRef, useCallback } from 'react';
import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';

window.CESIUM_BASE_URL = '/cesium/';

// ==================== 底图加载（Esri World Imagery）====================

function loadBasemap(viewer, type) {
  if (!viewer || viewer.isDestroyed()) return;
  viewer.imageryLayers.removeAll();

  if (type === 'street') {
    // Esri 街道图
    viewer.imageryLayers.addImageryProvider(
      new Cesium.UrlTemplateImageryProvider({
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
        tilingScheme: new Cesium.WebMercatorTilingScheme(),
        maximumLevel: 19,
        credit: 'Esri World Street Map',
      })
    );
  } else {
    // 默认：Esri 卫星影像（World Imagery）
    viewer.imageryLayers.addImageryProvider(
      new Cesium.UrlTemplateImageryProvider({
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        tilingScheme: new Cesium.WebMercatorTilingScheme(),
        maximumLevel: 19,
        credit: 'Esri World Imagery',
      })
    );
  }
  console.log('[CesiumComponent] 底图已加载:', type || 'satellite(esri)');
}

// ==================== 命令执行器 ====================

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
          fullUrl = (process.env.NODE_ENV === 'development' ? 'http://localhost:8006' : '') + url;
        }
        const existing = viewer.dataSources.getByName(name);
        existing.forEach(ds => viewer.dataSources.remove(ds));
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
      case 'removeLayer': {
        viewer.dataSources.getByName(command.name).forEach(ds => viewer.dataSources.remove(ds));
        break;
      }
      case 'clearAll': {
        viewer.entities.removeAll();
        viewer.dataSources.removeAll();
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
        const { url: tilesUrl } = command;
        if (!tilesUrl) break;
        Cesium.Cesium3DTileset.fromUrl(tilesUrl).then(tileset => {
          viewer.scene.primitives.add(tileset);
          viewer.zoomTo(tileset).catch(() => {});
        }).catch(err => console.error('[CesiumComponent] 3DTiles加载失败:', err));
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

// ==================== React 组件 ====================

const CesiumComponent = ({ onViewerReady, className = '' }) => {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const wsRef = useRef(null);
  const isInitializedRef = useRef(false);

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
        infoBox: true,
        selectionIndicator: true,
      });

      viewerRef.current = viewer;

      // 加载高德卫星影像底图（国内可用，无需Token）
      loadBasemap(viewer, 'satellite');

      // 初始视角：河南信阳区域
      viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(114.3, 32.0, 800000),
        orientation: {
          heading: Cesium.Math.toRadians(0),
          pitch: Cesium.Math.toRadians(-45),
          roll: 0,
        },
      });

      if (onViewerReady) onViewerReady(viewer);
      console.log('[CesiumComponent] Viewer 初始化完成');
    } catch (err) {
      console.error('[CesiumComponent] Viewer 初始化失败:', err);
      isInitializedRef.current = false;
    }
  }, [onViewerReady]);

  const connectWebSocket = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    const isDev = process.env.NODE_ENV === 'development';
    const wsUrl = isDev
      ? 'ws://localhost:8006/ws/cesium'
      : 'ws://' + window.location.host + '/ws/cesium';
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
    initViewer();
    connectWebSocket();
    const handleDirectCommand = (event) => {
      if (viewerRef.current) executeCesiumCommand(viewerRef.current, event.detail);
    };
    window.addEventListener('cesium_execute_command', handleDirectCommand);
    return () => {
      window.removeEventListener('cesium_execute_command', handleDirectCommand);
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
      if (viewerRef.current && !viewerRef.current.isDestroyed()) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
      isInitializedRef.current = false;
    };
  }, [initViewer, connectWebSocket]);

  return (
    <div
      ref={containerRef}
      className={'cesium-container ' + className}
      style={{ width: '100%', height: '100%', position: 'relative' }}
    />
  );
};

export default CesiumComponent;
export { executeCesiumCommand };
