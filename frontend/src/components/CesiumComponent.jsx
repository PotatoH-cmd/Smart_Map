// ============================================================
// CesiumComponent React 壳（自拆分后保留，路径与 default export 不变）
//
// 说明：底图预设/命令执行器/工具函数/测量绘制工具已拆至 ./cesium/ 目录。
// window.CESIUM_BASE_URL 设置与 Cesium CSS import 保留在本文件顶部。
// executeCesiumCommand 命名导出保留，供外部命令分发使用。
// ============================================================
import React, { useEffect, useRef, useCallback, useState } from 'react';
import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import { BASEMAP_PRESETS, OVERLAY_PRESETS, OBLIQUE_TILESETS, loadBasemap } from './cesium/presets';
import { executeCesiumCommand, setModuleLocalDatasets } from './cesium/commands';
import { clearToolEntities, startToolSession } from './cesium/measureTools';
import { getClickLngLat, overlayFeatureName } from './cesium/geoUtils';

window.CESIUM_BASE_URL = '/cesium/';

// ==================== React 组件 ====================

const CesiumComponent = ({ onViewerReady, className = '', visible = true }) => {
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
      // 视角操控阻尼，让拖动/缩放/旋转更顺滑
      const camCtrl = scene.screenSpaceCameraController;
      camCtrl.inertiaSpin = 0.92;           // 旋转惯性（越高越滑）
      camCtrl.inertiaTranslate = 0.92;      // 平移惯性
      camCtrl.inertiaZoom = 0.85;           // 缩放惯性（提高以匹配其他轴）
      camCtrl.minimumZoomDistance = 50;     // 最近缩放距离（米）
      camCtrl.maximumZoomDistance = 5000000; // 最远缩放距离
      camCtrl.enableCollisionDetection = false; // 关闭碰撞检测，让相机更自由
      try { camCtrl.zoomFactor = 2.5; } catch (_) {}  // 缩放速度倍率

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
          setModuleLocalDatasets(d.datasets);
        }
      })
      .catch(() => {});
  }, []);

  // 仅在容器可见时才初始化 Viewer（避免 display:none 时创建损坏的 WebGL 上下文）
  useEffect(() => {
    if (!visible) return;
    initViewer();
    connectWebSocket();
  }, [visible, initViewer, connectWebSocket]);

  // 全局事件监听 + 组件卸载时清理
  useEffect(() => {
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
  }, [refreshLayers]);

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
