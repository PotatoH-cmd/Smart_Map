import React, { useState, useEffect, useRef, useCallback } from 'react';
import L from 'leaflet';

const SAMPanel = ({ mapManager }) => {
  const [drawMode, setDrawMode] = useState('rectangle');
  const [prompt, setPrompt] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [isDrawing, setIsDrawing] = useState(false);
  const [progress, setProgress] = useState([]);
  const [result, setResult] = useState(null);
  const [resultLayer, setResultLayer] = useState(null);
  const [drawCount, setDrawCount] = useState(0);
  // 实时进度条状态
  const [progressPct, setProgressPct] = useState(0);
  const [progressMsg, setProgressMsg] = useState('');
  const [taskId, setTaskId] = useState('');
  const [fastMode, setFastMode] = useState(true);
  const [demoMode, setDemoMode] = useState(true);  // Demo 模式：超快速（默认开启）
  const [searchLevel, setSearchLevel] = useState('16');
  const [contextMode, setContextMode] = useState(false);
  // SAM 阈值（从后端获取/设置）
  const [classifyThd, setClassifyThd] = useState(0.45);
  const [boxThd, setBoxThd] = useState(0.65);
  const [verifyThd, setVerifyThd] = useState(0.75);
  const [autoVerifyThd, setAutoVerifyThd] = useState(0.90);
  const [classifyMaxSide, setClassifyMaxSide] = useState(640);
  const [searchRunning, setSearchRunning] = useState(false);
  const [searchStats, setSearchStats] = useState({ total: 0, current: 0, found: 0 });
  const [drawnBounds, setDrawnBounds] = useState(null);
  // 变化检测状态
  const [changeMode, setChangeMode] = useState(false);
  const [changeYearA, setChangeYearA] = useState(2023);
  const [changeYearB, setChangeYearB] = useState(2025);
  const [changeResult, setChangeResult] = useState(null);
  const [changeRunning, setChangeRunning] = useState(false);
  const changeLayerRef = useRef(null);
  const pollRef = useRef(null);

  const drawnItemsRef = useRef(null);
  const cleanupDrawRef = useRef(null);
  const logRef = useRef(null);
  const currentSearchLayerRef = useRef(null);
  const searchResultLayerRef = useRef(null);
  const searchCancelRef = useRef(false);
  const searchAbortControllersRef = useRef([]);
  const svgRenderersRef = useRef({});

  const stopNativeEvent = useCallback((event) => {
    if (!event) return;
    if (typeof event.preventDefault === 'function' && event.cancelable !== false) event.preventDefault();
    if (typeof event.stopPropagation === 'function') event.stopPropagation();
    if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
  }, []);

  const getNativeEventLatLng = useCallback((map, event) => {
    if (!map || !map.getContainer) return null;
    const container = map.getContainer();
    if (!container) return null;
    const rect = container.getBoundingClientRect();
    const point = L.point(event.clientX - rect.left, event.clientY - rect.top);
    return map.containerPointToLatLng(point);
  }, []);

  const ensurePane = useCallback((map, name, zIndex, pointerEvents = 'auto') => {
    if (!map.getPane(name)) {
      const pane = map.createPane(name);
      pane.style.zIndex = String(zIndex);
      pane.style.pointerEvents = pointerEvents;
    }
  }, []);

  const getSvgRenderer = useCallback((map, paneName) => {
    if (!svgRenderersRef.current[paneName]) {
      svgRenderersRef.current[paneName] = L.svg({ pane: paneName });
    }
    return svgRenderersRef.current[paneName];
  }, []);

  const clearSvgRenderers = useCallback((map) => {
    Object.values(svgRenderersRef.current).forEach(renderer => {
      try {
        if (map?.hasLayer?.(renderer)) map.removeLayer(renderer);
        else renderer.remove?.();
      } catch (_) {}
    });
    svgRenderersRef.current = {};
  }, []);

  const safelyRemoveLayer = useCallback((map, layer) => {
    if (!map || !layer) return;
    try {
      if (typeof map.hasLayer === 'function' && map.hasLayer(layer)) {
        map.removeLayer(layer);
      }
    } catch (_) {}
  }, []);

  const addLog = useCallback((text, tone = 'info') => {
    setProgress(prev => [...prev.slice(-80), { text, tone, time: new Date().toLocaleTimeString() }]);
  }, []);

  const plainBounds = useCallback((bounds) => ({
    south: bounds.getSouth(),
    west: bounds.getWest(),
    north: bounds.getNorth(),
    east: bounds.getEast(),
  }), []);

  const boundsToGeometry = useCallback((bounds) => ({
    type: 'Polygon',
    coordinates: [[
      [bounds.getWest(), bounds.getSouth()],
      [bounds.getWest(), bounds.getNorth()],
      [bounds.getEast(), bounds.getNorth()],
      [bounds.getEast(), bounds.getSouth()],
      [bounds.getWest(), bounds.getSouth()],
    ]],
  }), []);

  const lngLatToTile = useCallback((lng, lat, z) => {
    const n = Math.pow(2, z);
    const x = Math.floor((lng + 180) / 360 * n);
    const latRad = lat * Math.PI / 180;
    const y = Math.floor((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n);
    return { x: Math.max(0, Math.min(n - 1, x)), y: Math.max(0, Math.min(n - 1, y)) };
  }, []);

  const tileBounds4326 = useCallback((z, x, y) => {
    const n = Math.pow(2, z);
    const west = x / n * 360 - 180;
    const east = (x + 1) / n * 360 - 180;
    const latRadN = Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n)));
    const latRadS = Math.atan(Math.sinh(Math.PI * (1 - 2 * (y + 1) / n)));
    return L.latLngBounds(
      [latRadS * 180 / Math.PI, west],
      [latRadN * 180 / Math.PI, east]
    );
  }, []);

  const generateXYZTiles = useCallback((bounds, level) => {
    const z = Number(level);
    const tl = lngLatToTile(bounds.getWest(), bounds.getNorth(), z);
    const br = lngLatToTile(bounds.getEast(), bounds.getSouth(), z);
    const tiles = [];
    for (let tx = tl.x; tx <= br.x; tx++) {
      for (let ty = tl.y; ty <= br.y; ty++) {
        tiles.push({ z, x: tx, y: ty, bounds: tileBounds4326(z, tx, ty) });
      }
    }
    return tiles;
  }, [lngLatToTile, tileBounds4326]);

  const mergeNearbyBboxFeatures = useCallback((features, level) => {
    const boxes = (features || []).map((feature) => {
      const ring = feature?.geometry?.coordinates?.[0] || [];
      const xs = ring.map(p => p[0]).filter(Number.isFinite);
      const ys = ring.map(p => p[1]).filter(Number.isFinite);
      if (!xs.length || !ys.length) return null;
      return {
        minX: Math.min(...xs),
        minY: Math.min(...ys),
        maxX: Math.max(...xs),
        maxY: Math.max(...ys),
        label: feature?.properties?.label || prompt.trim(),
        confidence: feature?.properties?.confidence || 0.8,
        count: 1,
      };
    }).filter(Boolean);
    const z = Number(level);
    const tol = (360 / Math.pow(2, z)) * 0.02;
    const overlaps = (a, b) => (
      a.minX <= b.maxX + tol && a.maxX + tol >= b.minX &&
      a.minY <= b.maxY + tol && a.maxY + tol >= b.minY
    );
    let changed = true;
    while (changed) {
      changed = false;
      outer: for (let i = 0; i < boxes.length; i += 1) {
        for (let j = i + 1; j < boxes.length; j += 1) {
          if (boxes[i].label === boxes[j].label && overlaps(boxes[i], boxes[j])) {
            boxes[i] = {
              minX: Math.min(boxes[i].minX, boxes[j].minX),
              minY: Math.min(boxes[i].minY, boxes[j].minY),
              maxX: Math.max(boxes[i].maxX, boxes[j].maxX),
              maxY: Math.max(boxes[i].maxY, boxes[j].maxY),
              label: boxes[i].label,
              confidence: Math.max(boxes[i].confidence, boxes[j].confidence),
              count: boxes[i].count + boxes[j].count,
            };
            boxes.splice(j, 1);
            changed = true;
            break outer;
          }
        }
      }
    }
    return boxes.map((box) => ({
      type: 'Feature',
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [box.minX, box.minY],
          [box.maxX, box.minY],
          [box.maxX, box.maxY],
          [box.minX, box.maxY],
          [box.minX, box.minY],
        ]],
      },
      properties: {
        label: box.label,
        confidence: Number(box.confidence.toFixed(2)),
        merged_count: box.count,
      },
    }));
  }, [prompt]);

  const drawSearchResults = useCallback((map, geojson) => {
    if (!searchResultLayerRef.current) {
      searchResultLayerRef.current = L.featureGroup().addTo(map);
    }
    searchResultLayerRef.current.clearLayers?.();
    L.geoJSON(geojson, {
      pane: 'samSearchPane',
      renderer: getSvgRenderer(map, 'samSearchPane'),
      interactive: false,
      bubblingMouseEvents: false,
      style: {
        color: '#10b981',
        weight: 2,
        fillColor: '#10b981',
        fillOpacity: 0.18,
      },
    }).addTo(searchResultLayerRef.current);
  }, [getSvgRenderer]);

  useEffect(() => {
    logRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' });
  }, [progress]);

  // 加载 SAM 阈值配置
  useEffect(() => {
    fetch('/api/sam-thresholds')
      .then(r => r.json())
      .then(data => {
        if (data.classify_conf_thd !== undefined) setClassifyThd(data.classify_conf_thd);
        if (data.box_conf_thd !== undefined) setBoxThd(data.box_conf_thd);
        if (data.verify_conf_thd !== undefined) setVerifyThd(data.verify_conf_thd);
        if (data.auto_verify_thd !== undefined) setAutoVerifyThd(data.auto_verify_thd);
        if (data.classify_max_side !== undefined) setClassifyMaxSide(data.classify_max_side);
      })
      .catch(() => {});
  }, []);

  // 组件卸载或 map 变化时，强制中止绘制操作，清理残留 DOM 事件监听
  useEffect(() => {
    return () => {
      if (cleanupDrawRef.current) {
        try { cleanupDrawRef.current(); } catch (_) {}
        cleanupDrawRef.current = null;
      }
    };
  }, [mapManager]);

  // 初始化绘制图层组
  useEffect(() => {
    if (!mapManager || !mapManager.map) return;
    const fg = new L.FeatureGroup();
    mapManager.map.addLayer(fg);
    drawnItemsRef.current = fg;
    return () => {
      safelyRemoveLayer(mapManager?.map, fg);
    };
  }, [mapManager, safelyRemoveLayer]);

  // 矩形绘制
  const startRectDraw = useCallback(() => {
    if (!mapManager?.map) return;
    const map = mapManager.map;
    const container = map.getContainer();
    ensurePane(map, 'samDrawPane', 900, 'none');
    setIsDrawing(true);
    const prevCursor = container.style.cursor;
    const prevTouchAction = container.style.touchAction;
    const wasDragging = !!map.dragging?.enabled?.();
    container.style.cursor = 'crosshair';
    container.style.touchAction = 'none';
    let startLL = null, preview = null, active = false, finished = false;

    const done = () => {
      if (finished) return;
      finished = true;
      container.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('pointermove', onMove, true);
      document.removeEventListener('pointerup', onUp, true);
      document.removeEventListener('pointercancel', onCancel, true);
      container.style.cursor = prevCursor;
      container.style.touchAction = prevTouchAction;
      if (wasDragging) map.dragging.enable();
      setIsDrawing(false);
      cleanupDrawRef.current = null;
    };
    const onDown = (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      startLL = getNativeEventLatLng(map, e);
      if (!startLL) { done(); return; }
      active = true;
      try { container.setPointerCapture?.(e.pointerId); } catch (_) {}
      stopNativeEvent(e);
    };
    const onMove = (e) => {
      if (!active || !startLL) return;
      const b = L.latLngBounds(startLL, getNativeEventLatLng(map, e));
      if (preview) preview.setBounds(b);
      else preview = L.rectangle(b, { color: '#ff6b00', weight: 2, fillOpacity: 0.1, dashArray: '6', pane: 'samDrawPane', renderer: getSvgRenderer(map, 'samDrawPane'), interactive: false, bubblingMouseEvents: false }).addTo(map);
      stopNativeEvent(e);
    };
    const onUp = (e) => {
      if (!active || !startLL) return;
      const endLL = getNativeEventLatLng(map, e);
      active = false;
      done();
      const b = L.latLngBounds(startLL, endLL);
      if (preview) map.removeLayer(preview);
      const sz = map.latLngToContainerPoint(b.getNorthEast()).subtract(map.latLngToContainerPoint(b.getSouthWest()));
      if (Math.abs(sz.x) < 10 || Math.abs(sz.y) < 10) return;
      drawnItemsRef.current.addLayer(L.rectangle(b, { color: '#ff6b00', weight: 2, fillOpacity: 0.15, pane: 'samDrawPane', renderer: getSvgRenderer(map, 'samDrawPane'), interactive: false, bubblingMouseEvents: false }));
      setDrawnBounds(plainBounds(b));
      setDrawCount(drawnItemsRef.current.getLayers().length);
      stopNativeEvent(e);
    };
    const onCancel = (e) => {
      if (preview) map.removeLayer(preview);
      active = false;
      done();
      stopNativeEvent(e);
    };
    map.dragging.disable();
    container.addEventListener('pointerdown', onDown, true);
    document.addEventListener('pointermove', onMove, true);
    document.addEventListener('pointerup', onUp, true);
    document.addEventListener('pointercancel', onCancel, true);
    cleanupDrawRef.current = () => { done(); if (preview) map.removeLayer(preview); };
  }, [ensurePane, getNativeEventLatLng, getSvgRenderer, mapManager, plainBounds, stopNativeEvent]);

  // 多边形绘制
  const startPolyDraw = useCallback(() => {
    if (!mapManager?.map) return;
    const map = mapManager.map;
    const container = map.getContainer();
    ensurePane(map, 'samDrawPane', 900, 'none');
    setIsDrawing(true);
    const prevCursor = container.style.cursor;
    const wasDragging = !!map.dragging?.enabled?.();
    const wasDoubleClickZoom = !!map.doubleClickZoom?.enabled?.();
    container.style.cursor = 'crosshair';
    const pts = []; let line = null, finished = false;

    const onClick = (e) => {
      if (finished) return;
      const ll = getNativeEventLatLng(map, e);
      if (!ll) { done(); return; }
      pts.push(ll);
      if (line) map.removeLayer(line);
      line = L.polyline(pts, { color: '#ff6b00', weight: 2, dashArray: '6', pane: 'samDrawPane', renderer: getSvgRenderer(map, 'samDrawPane'), interactive: false, bubblingMouseEvents: false }).addTo(map);
      stopNativeEvent(e);
    };
    const onDbl = (e) => {
      stopNativeEvent(e);
      done(); if (line) map.removeLayer(line);
      if (pts.length < 3) return;
      const poly = L.polygon(pts, { color: '#ff6b00', weight: 2, fillOpacity: 0.15, pane: 'samDrawPane', renderer: getSvgRenderer(map, 'samDrawPane'), interactive: false, bubblingMouseEvents: false });
      drawnItemsRef.current.addLayer(poly);
      setDrawnBounds(plainBounds(poly.getBounds()));
      setDrawCount(drawnItemsRef.current.getLayers().length);
    };
    const onKey = (e) => { if (e.key === 'Escape') { done(); if (line) map.removeLayer(line); } };
    const done = () => {
      if (finished) return;
      finished = true;
      container.removeEventListener('click', onClick, true);
      container.removeEventListener('dblclick', onDbl, true);
      document.removeEventListener('keydown', onKey);
      container.style.cursor = prevCursor;
      if (wasDragging) map.dragging.enable();
      if (wasDoubleClickZoom) map.doubleClickZoom.enable();
      setIsDrawing(false);
      cleanupDrawRef.current = null;
    };
    map.dragging.disable();
    map.doubleClickZoom.disable();
    container.addEventListener('click', onClick, true);
    container.addEventListener('dblclick', onDbl, true);
    document.addEventListener('keydown', onKey);
    cleanupDrawRef.current = () => { done(); if (line) map.removeLayer(line); };
  }, [ensurePane, getNativeEventLatLng, getSvgRenderer, mapManager, plainBounds, stopNativeEvent]);

  // 开始绘制
  const startDraw = () => {
    if (cleanupDrawRef.current) cleanupDrawRef.current();
    if (drawMode === 'rectangle') startRectDraw(); else startPolyDraw();
  };

  // 清除绘制区域
  const clearDrawn = useCallback(() => {
    if (cleanupDrawRef.current) { cleanupDrawRef.current(); cleanupDrawRef.current = null; }
    if (drawnItemsRef.current) { drawnItemsRef.current.clearLayers(); setDrawCount(0); setDrawnBounds(null); }
  }, []);

  // 清除结果
  const clearResult = useCallback(() => {
    if (resultLayer && mapManager) {
      safelyRemoveLayer(mapManager?.map, resultLayer);
      setResultLayer(null);
    }
    if (currentSearchLayerRef.current && mapManager?.map) {
      safelyRemoveLayer(mapManager.map, currentSearchLayerRef.current);
      currentSearchLayerRef.current = null;
    }
    if (searchResultLayerRef.current && mapManager?.map) {
      safelyRemoveLayer(mapManager.map, searchResultLayerRef.current);
      searchResultLayerRef.current = null;
    }
    clearSvgRenderers(mapManager?.map);
    setResult(null);
    setProgress([]);
    setProgressPct(0);
    setProgressMsg('');
  }, [resultLayer, mapManager, safelyRemoveLayer, clearSvgRenderers]);

  // 轮询进度
  const startProgressPoll = (id) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(`/api/sam-progress/${id}`);
        if (!r.ok) return;
        const d = await r.json();
        let pct = d.total > 0 ? Math.round((d.current / Math.max(d.total, 1)) * 100) : 0;
        // 推理阶段映射到更合理的百分比
        if (d.stage === 'inference') pct = Math.min(pct, 85);
        else if (d.stage === 'inference_done') pct = 90;
        else if (d.stage === 'done') { pct = 100; clearInterval(pollRef.current); }
        else if (d.stage === 'error') { clearInterval(pollRef.current); }
        setProgressPct(pct);
        setProgressMsg(d.message || '');
      } catch {}
    }, 1500); // 每1.5秒轮询
  };

  // 停止轮询
  const stopPoll = () => { if (pollRef.current) clearInterval(pollRef.current); };

  const stopSearch = () => {
    searchCancelRef.current = true;
    searchAbortControllersRef.current.forEach(controller => {
      try { controller.abort(); } catch (_) {}
    });
    searchAbortControllersRef.current = [];
    setSearchRunning(false);
    setProgressMsg('已停止');
    addLog('已请求停止：不再提交新瓦片，正在等待的请求已中断', 'error');
  };

  const runTileSearch = async () => {
    if (!mapManager?.map || !drawnItemsRef.current || drawnItemsRef.current.getLayers().length === 0) {
      alert('请先在地图上绘制搜索范围');
      return;
    }
    if (!prompt.trim()) {
      alert('请输入搜索目标');
      return;
    }
    const baseLayer = drawnItemsRef.current.getLayers()[0];
    const baseBounds = baseLayer.getBounds?.();
    if (!baseBounds || !baseBounds.isValid?.()) {
      alert('搜索范围无效');
      return;
    }
    const map = mapManager.map;
    ensurePane(map, 'samSearchPane', 910, 'none');
    searchCancelRef.current = false;
    searchAbortControllersRef.current = [];
    setSearchRunning(true);
    setIsRunning(false);
    setProgress([]);
    setProgressPct(0);
    setProgressMsg('计算 XYZ 瓦片...');
    setResult(null);
    setDrawnBounds(plainBounds(baseBounds));
    if (currentSearchLayerRef.current) safelyRemoveLayer(map, currentSearchLayerRef.current);
    if (searchResultLayerRef.current) safelyRemoveLayer(map, searchResultLayerRef.current);
    searchResultLayerRef.current = L.featureGroup().addTo(map);
    const tiles = generateXYZTiles(baseBounds, searchLevel);
    const recommendedMaxTiles = 80;
    if (tiles.length > recommendedMaxTiles) {
      const ok = window.confirm(
        `当前 Z${searchLevel} 会搜索 ${tiles.length} 个瓦片，预计耗时很长。\n\n` +
        `建议改用 Z16/Z17、缩小范围，或关闭上下文增强；继续可能看起来像卡住。\n\n` +
        `是否仍要继续？`
      );
      if (!ok) {
        setSearchRunning(false);
        setProgressMsg('已取消：瓦片数量过多');
        addLog(`已取消：Z${searchLevel} 共 ${tiles.length} 个瓦片，建议降低级别或缩小范围`, 'error');
        return;
      }
    }
    const merged = { type: 'FeatureCollection', features: [] };
    setSearchStats({ total: tiles.length, current: 0, found: 0 });
    const concurrency = Math.min(4, Math.max(1, tiles.length));
    let nextTileIndex = 0;
    let completedTiles = 0;
    addLog(`开始搜索：Z${searchLevel} 共 ${tiles.length} 个瓦片，目标：${prompt.trim()}，并发：${concurrency}，上下文：${contextMode ? '开启' : '关闭'}`, 'info');
    const processTile = async (tile, i) => {
      if (searchCancelRef.current) {
        return;
      }
      if (currentSearchLayerRef.current) safelyRemoveLayer(map, currentSearchLayerRef.current);
      currentSearchLayerRef.current = L.rectangle(tile.bounds, {
        color: '#ef4444',
        weight: 2,
        fillColor: '#ef4444',
        fillOpacity: 0.08,
        pane: 'samSearchPane',
        renderer: getSvgRenderer(map, 'samSearchPane'),
        interactive: false,
        bubblingMouseEvents: false,
      }).addTo(map);
      setProgressMsg(`检测瓦片 ${tile.z}/${tile.x}/${tile.y} (${i + 1}/${tiles.length})`);
      let controller = null;
      try {
        controller = new AbortController();
        searchAbortControllersRef.current.push(controller);
        const res = await fetch('/api/sam-tile-detect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({
            z: tile.z,
            x: tile.x,
            y: tile.y,
            prompt: prompt.trim(),
            tile_source: 'arcgis_gf',
            context_radius: contextMode ? 1 : 0,
            classify_conf_thd: classifyThd,
            box_conf_thd: boxThd,
            verify_conf_thd: verifyThd,
            auto_verify_thd: autoVerifyThd,
            classify_max_side: classifyMaxSide,
          }),
        });
        if (!res.ok) {
          const err = await res.text();
          throw new Error(`HTTP ${res.status}: ${err}`);
        }
        const data = await res.json();
        const meta = data._meta || {};
        const features = data.features || [];
        if (features.length > 0) {
          features.forEach((feature) => {
            merged.features.push({
              ...feature,
              properties: {
                ...(feature.properties || {}),
                tile_index: i + 1,
              },
            });
          });
          L.geoJSON(data, {
            pane: 'samSearchPane',
            renderer: getSvgRenderer(map, 'samSearchPane'),
            interactive: false,
            bubblingMouseEvents: false,
            style: {
              color: '#10b981',
              weight: 2,
              fillColor: '#10b981',
              fillOpacity: 0.18,
            },
          }).addTo(searchResultLayerRef.current);
          setSearchStats(prev => ({ ...prev, found: prev.found + features.length }));
          const drillInfo = meta.drilled ? ` [下钻${meta.drilled}]` : '';
          const contextInfo = meta.context ? ` [上下文${meta.context_tiles || 0}片]` : '';
          addLog(`${tile.z}/${tile.x}/${tile.y}: ${features.length} 个目标${contextInfo}${drillInfo} (${meta.duration || '?'}s)`, 'success');
        } else {
          const reason = meta.stage === 'classify' ? '无目标' : (meta.stage === 'detect' ? '无bbox' : '空瓦片');
          if ((i + 1) % 5 === 0 || i === 0) {
            addLog(`${tile.z}/${tile.x}/${tile.y}: ${reason} (${meta.duration || '?'}s)`, 'info');
          }
        }
      } catch (err) {
        if (err.name === 'AbortError') {
          addLog(`${tile.z}/${tile.x}/${tile.y} 已中断`, 'error');
        } else {
          addLog(`${tile.z}/${tile.x}/${tile.y} 错误：${err.message}`, 'error');
        }
      } finally {
        if (controller) {
          searchAbortControllersRef.current = searchAbortControllersRef.current.filter(item => item !== controller);
        }
        completedTiles += 1;
        setSearchStats(prev => ({ ...prev, current: completedTiles }));
        setProgressPct(Math.round((completedTiles / tiles.length) * 100));
        setProgressMsg(`已完成 ${completedTiles}/${tiles.length} 个瓦片`);
      }
    };
    const workers = Array.from({ length: concurrency }, async () => {
      while (!searchCancelRef.current && nextTileIndex < tiles.length) {
        const i = nextTileIndex;
        nextTileIndex += 1;
        await processTile(tiles[i], i);
      }
    });
    await Promise.all(workers);
    if (searchCancelRef.current) {
      addLog('搜索已停止', 'error');
    }
    if (!searchCancelRef.current) {
      const mergedFeatures = mergeNearbyBboxFeatures(merged.features, searchLevel);
      const finalMerged = { type: 'FeatureCollection', features: mergedFeatures };
      drawSearchResults(map, finalMerged);
      setProgressPct(100);
      setProgressMsg(`完成，发现 ${mergedFeatures.length} 个目标`);
      addLog(`搜索完成！原始 ${merged.features.length} 个，合并后 ${mergedFeatures.length} 个目标`, 'success');
      setResult(mergedFeatures.length > 0 ? finalMerged : null);
    } else {
      setResult(merged.features.length > 0 ? merged : null);
    }
    setSearchRunning(false);
  };

  // 运行 SAM 检测
  const runDetect = async () => {
    if (!drawnItemsRef.current || drawnItemsRef.current.getLayers().length === 0) {
      alert('请先在地图上绘制识别区域');
      return;
    }
    if (!prompt.trim()) {
      alert('请输入识别目标提示词');
      return;
    }

    setIsRunning(true);
    setProgress([]);
    clearResult();

    const addLog = (text, tone = 'info') => {
      setProgress(prev => [...prev, { text, tone, time: new Date().toLocaleTimeString() }]);
    };

    addLog('开始 SAM 目标识别...');
    setProgressPct(2);
    setProgressMsg('准备中...');

    try {
      const layers = drawnItemsRef.current.getLayers();
      const geojsons = layers.map(l => l.toGeoJSON());

      const allCoords = [];
      geojsons.forEach(gj => {
        const coords = gj.geometry.type === 'Polygon'
          ? gj.geometry.coordinates[0]
          : [];
        coords.forEach(c => allCoords.push(c));
      });

      if (allCoords.length < 3) {
        addLog('绘制区域几何无效', 'error');
        setIsRunning(false);
        return;
      }

      addLog(`检测到 ${geojsons.length} 个绘制区域，${allCoords.length} 个坐标点`);
      addLog(`识别模式：${demoMode ? 'Demo模式（极速）' : fastMode ? '快速模式（速度优先）' : '标准模式（精度优先）'}`);
      setProgressMsg('正在发送请求...');

      const res = await fetch('/api/sam-detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          geometry: geojsons[0].geometry,
          prompt: prompt.trim(),
          mode: drawMode,
          fast_mode: fastMode,
          demo_mode: demoMode,
          classify_conf_thd: classifyThd,
          box_conf_thd: boxThd,
          verify_conf_thd: verifyThd,
          auto_verify_thd: autoVerifyThd,
          classify_max_side: classifyMaxSide,
        }),
      });

      if (!res.ok) {
        const err = await res.text();
        throw new Error(`HTTP ${res.status}: ${err}`);
      }

      const data = await res.json();
      // 获取 task_id 并开始轮询进度（如果还在进行中）
      const tid = data._task_id || '';
      if (tid) { setTaskId(tid); startProgressPoll(tid); }
      stopPoll();  // 已返回则停止轮询
      setProgressPct(100);
      addLog(`推理完成！检测到 ${data.features?.length || 0} 个目标`, 'success');

      if (data.features && data.features.length > 0) {
        if (!mapManager?.map) {
          addLog('地图已卸载，结果未渲染到地图', 'info');
          setResult(data);
          return;
        }
        // 加载结果到地图
        const layer = L.geoJSON(data, {
          renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
          interactive: true,
          bubblingMouseEvents: false,
          style: {
            color: '#e53e3e',
            weight: 2,
            fillColor: '#fc8181',
            fillOpacity: 0.4,
          },
          onEachFeature: (feature, l) => {
            const p = feature.properties || {};
            const areaMu = p.area_mu ? `${p.area_mu.toFixed(2)} 亩` : '';
            const areaM2 = p.area_m2 ? `${p.area_m2.toFixed(1)} m²` : '';
            l.bindPopup(`
              <b>SAM 识别结果</b><br/>
              目标: ${prompt}<br/>
              面积: ${areaM2}${areaMu ? ` (${areaMu})` : ''}
            `);
          },
        });
        layer.addTo(mapManager.map);
        setResultLayer(layer);
        setResult(data);

        // 自动缩放到结果
        mapManager.map.fitBounds(layer.getBounds().pad(0.1));
        addLog('结果已加载到地图', 'success');
      } else {
        addLog('未检测到目标', 'info');
      }
    } catch (err) {
      addLog(`错误: ${err.message}`, 'error');
      setProgressMsg('出错: ' + err.message);
    } finally {
      stopPoll();
      setIsRunning(false);
    }
  };

  // 下载 SHP
  const downloadSHP = async () => {
    if (!result) return;
    try {
      const res = await fetch('/api/sam-download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ geojson: result }),
      });
      if (!res.ok) throw new Error('下载失败');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `sam_result_${new Date().toISOString().slice(0, 19).replace(/[:]/g, '-')}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`下载失败: ${err.message}`);
    }
  };

  // 变化检测
  const runChangeDetect = async () => {
    if (!drawnItemsRef.current || drawnItemsRef.current.getLayers().length === 0) {
      alert('请先在地图上绘制检测区域');
      return;
    }
    if (!prompt.trim()) {
      alert('请输入检测目标提示词');
      return;
    }
    if (changeYearA === changeYearB) {
      alert('请选择不同的年份进行对比');
      return;
    }

    setChangeRunning(true);
    setProgress([]);
    clearResult();
    if (changeLayerRef.current && mapManager?.map) {
      mapManager.map.removeLayer(changeLayerRef.current);
      changeLayerRef.current = null;
    }

    const addLog = (text, tone = 'info') => {
      setProgress(prev => [...prev, { text, tone, time: new Date().toLocaleTimeString() }]);
    };

    addLog(`开始变化检测: ${changeYearA} vs ${changeYearB}`);
    setProgressPct(5);
    setProgressMsg('获取影像中...');

    try {
      const layers = drawnItemsRef.current.getLayers();
      const gj = layers[0].toGeoJSON();

      const res = await fetch('/api/sam-change-detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          geometry: gj.geometry,
          prompt: prompt.trim(),
          year_a: changeYearA,
          year_b: changeYearB,
          fast_mode: true,
          demo_mode: demoMode,
        }),
      });

      if (!res.ok) {
        const err = await res.text();
        throw new Error(`HTTP ${res.status}: ${err}`);
      }

      const data = await res.json();
      const tid = data._task_id || '';
      if (tid) { setTaskId(tid); startProgressPoll(tid); }
      stopPoll();
      setProgressPct(100);

      const meta = data.metadata || {};
      addLog(`检测完成！${changeYearA}年: ${meta.features_a || 0}个, ${changeYearB}年: ${meta.features_b || 0}个, 变化: ${meta.features_change || 0}个`, 'success');

      if (data.features && data.features.length > 0 && mapManager?.map) {
        // 清除旧图层
        if (changeLayerRef.current) {
          mapManager.map.removeLayer(changeLayerRef.current);
        }
        // 按变化类型分层渲染
        const addedLayer = L.geoJSON(
          { type: 'FeatureCollection', features: data.features.filter(f => f.properties.change_type === 'added') },
          { style: { color: '#ef4444', weight: 2, fillColor: '#fca5a5', fillOpacity: 0.35 } }
        );
        const removedLayer = L.geoJSON(
          { type: 'FeatureCollection', features: data.features.filter(f => f.properties.change_type === 'removed') },
          { style: { color: '#3b82f6', weight: 2, fillColor: '#93c5fd', fillOpacity: 0.35 } }
        );
        const unchangedLayer = L.geoJSON(
          { type: 'FeatureCollection', features: data.features.filter(f => f.properties.change_type === 'unchanged') },
          { style: { color: '#9ca3af', weight: 1, fillColor: '#d1d5db', fillOpacity: 0.2 } }
        );

        const fg = L.featureGroup([addedLayer, removedLayer, unchangedLayer]);
        fg.addTo(mapManager.map);
        changeLayerRef.current = fg;
        setChangeResult(data);

        // 图例面板
        const legend = L.control({ position: 'bottomright' });
        legend.onAdd = () => {
          const div = L.DomUtil.create('div', 'change-legend');
          div.innerHTML = `<div style="background:#fff;padding:8px 12px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.15);font-size:12px;line-height:1.8">`
            + `<b>变化检测</b> ${changeYearA} vs ${changeYearB}<br>`
            + `<span style="color:#ef4444">■</span> 新增 ${data.features.filter(f => f.properties.change_type === 'added').length}<br>`
            + `<span style="color:#3b82f6">■</span> 消失 ${data.features.filter(f => f.properties.change_type === 'removed').length}<br>`
            + `<span style="color:#9ca3af">■</span> 无变化 ${data.features.filter(f => f.properties.change_type === 'unchanged').length}</div>`;
          return div;
        };
        legend.addTo(mapManager.map);
        // 5秒后自动移除图例
        setTimeout(() => { try { mapManager.map.removeControl(legend); } catch(_) {} }, 15000);

        addLog('变化图斑已加载到地图（红=新增，蓝=消失，灰=无变化）', 'info');
      } else {
        addLog('未检测到变化', 'info');
      }
    } catch (err) {
      addLog(`变化检测错误: ${err.message}`, 'error');
      setProgressMsg('出错: ' + err.message);
    } finally {
      stopPoll();
      setChangeRunning(false);
    }
  };

  return (
    <div style={{
      position: 'relative',
      zIndex: 1000,
      width: '100%',
      height: '100%',
      maxHeight: '100%',
      overflowY: 'auto',
      background: 'transparent',
      color: '#1e293b',
      border: 'none',
      borderRadius: 0,
      boxShadow: 'none',
      fontSize: 12,
    }}>
      <div style={{ padding: '16px 16px 12px', borderBottom: '1px solid rgba(180,160,130,0.2)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <div style={{ fontWeight: 700, color: '#1e293b', fontSize: 15 }}>✥ 任务配置</div>
          <button onClick={clearResult} style={{
            border: 'none',
            background: 'rgba(239,68,68,0.08)',
            color: '#ef4444',
            borderRadius: 6,
            cursor: 'pointer',
            fontSize: 16,
            width: 26,
            height: 26,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            transition: 'background 0.15s',
          }}>×</button>
        </div>
        <div style={{ color: '#64748b', fontSize: 10 }}>SAM / Rule-based progressive tile search</div>
      </div>

      <div style={{ padding: 14, display: 'grid', gap: 10, gridTemplateRows: 'auto' }}>
        <div>
          <div style={{ color: '#64748b', fontSize: 11, marginBottom: 5, fontWeight: 500 }}>检测目标</div>
          <textarea
            placeholder="A construction site surrounded by farmland / 建筑物 / 厂房"
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            rows={2}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              resize: 'vertical',
              background: '#ffffff',
              color: '#1e293b',
              border: '1px solid rgba(180,160,130,0.25)',
              borderRadius: 8,
              padding: '10px 12px',
              outline: 'none',
              fontSize: 12,
              fontFamily: 'inherit',
              transition: 'border-color 0.15s',
              boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            }}
          />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <button
            onClick={() => setDrawMode('rectangle')}
            style={{
              padding: '8px 0',
              border: drawMode === 'rectangle' ? 'none' : '1px solid rgba(180,160,130,0.25)',
              borderRadius: 8,
              background: drawMode === 'rectangle' ? 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)' : '#ffffff',
              color: drawMode === 'rectangle' ? '#ffffff' : '#475569',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: 13,
              boxShadow: drawMode === 'rectangle' ? '0 2px 8px rgba(14,165,233,0.3)' : '0 1px 2px rgba(0,0,0,0.03)',
              transition: 'all 0.15s',
            }}
          >
            矩形范围
          </button>
          <button
            onClick={() => setDrawMode('polygon')}
            style={{
              padding: '8px 0',
              border: drawMode === 'polygon' ? 'none' : '1px solid rgba(180,160,130,0.25)',
              borderRadius: 8,
              background: drawMode === 'polygon' ? 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)' : '#ffffff',
              color: drawMode === 'polygon' ? '#ffffff' : '#475569',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: 13,
              boxShadow: drawMode === 'polygon' ? '0 2px 8px rgba(14,165,233,0.3)' : '0 1px 2px rgba(0,0,0,0.03)',
              transition: 'all 0.15s',
            }}
          >
            多边形
          </button>
        </div>

        <button
          onClick={startDraw}
          disabled={isDrawing || searchRunning || isRunning}
          style={{
            padding: '10px 0',
            border: isDrawing ? '1px dashed rgba(234,179,8,0.5)' : '1px dashed rgba(14,165,233,0.35)',
            borderRadius: 8,
            background: isDrawing ? '#fefce8' : '#f8fafc',
            color: isDrawing ? '#92400e' : '#0284c7',
            cursor: isDrawing || searchRunning || isRunning ? 'not-allowed' : 'pointer',
            fontWeight: 600,
            fontSize: 13,
            transition: 'all 0.15s',
          }}
        >
          {isDrawing ? '正在绘制范围...' : '+ 添加搜索/识别范围'}
        </button>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <div style={{ background: '#f8f7f4', borderRadius: 8, padding: '8px 10px', border: '1px solid rgba(180,160,130,0.12)' }}>
            <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>北</div>
            <div style={{ color: '#1e293b', fontFamily: 'monospace', fontSize: 13, fontWeight: 500 }}>{drawnBounds ? drawnBounds.north.toFixed(6) : '-'}</div>
          </div>
          <div style={{ background: '#f8f7f4', borderRadius: 8, padding: '8px 10px', border: '1px solid rgba(180,160,130,0.12)' }}>
            <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>东</div>
            <div style={{ color: '#1e293b', fontFamily: 'monospace', fontSize: 13, fontWeight: 500 }}>{drawnBounds ? drawnBounds.east.toFixed(6) : '-'}</div>
          </div>
          <div style={{ background: '#f8f7f4', borderRadius: 8, padding: '8px 10px', border: '1px solid rgba(180,160,130,0.12)' }}>
            <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>南</div>
            <div style={{ color: '#1e293b', fontFamily: 'monospace', fontSize: 13, fontWeight: 500 }}>{drawnBounds ? drawnBounds.south.toFixed(6) : '-'}</div>
          </div>
          <div style={{ background: '#f8f7f4', borderRadius: 8, padding: '8px 10px', border: '1px solid rgba(180,160,130,0.12)' }}>
            <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>西</div>
            <div style={{ color: '#1e293b', fontFamily: 'monospace', fontSize: 13, fontWeight: 500 }}>{drawnBounds ? drawnBounds.west.toFixed(6) : '-'}</div>
          </div>
        </div>

        <div>
          <div style={{ color: '#64748b', fontSize: 11, marginBottom: 5, fontWeight: 500 }}>搜索瓦片级别</div>
          <select
            value={searchLevel}
            onChange={e => setSearchLevel(e.target.value)}
            disabled={searchRunning}
            style={{
              width: '100%',
              background: '#ffffff',
              color: '#1e293b',
              border: '1px solid rgba(180,160,130,0.25)',
              borderRadius: 8,
              padding: '9px 12px',
              fontSize: 12,
              outline: 'none',
              cursor: searchRunning ? 'not-allowed' : 'pointer',
              boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            }}
          >
            <option value="12">Z12 概览（~150m/瓦片）</option>
            <option value="14">Z14 中等（~40m/瓦片）</option>
            <option value="16">Z16 推荐（~10m/瓦片）</option>
            <option value="18">Z18 精细（~2.5m/瓦片）</option>
          </select>
        </div>

        <button
          onClick={() => setContextMode(prev => !prev)}
          disabled={searchRunning}
          style={{
            padding: '9px 12px',
            border: contextMode ? '1px solid rgba(234,179,8,0.35)' : '1px solid rgba(180,160,130,0.25)',
            borderRadius: 8,
            background: contextMode ? '#fefce8' : '#ffffff',
            color: contextMode ? '#92400e' : '#475569',
            cursor: searchRunning ? 'not-allowed' : 'pointer',
            fontWeight: 600,
            fontSize: 12,
            textAlign: 'left',
            boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            transition: 'all 0.15s',
          }}
        >
          {contextMode ? '✓ 上下文增强：开启（3×3，较慢，适合大目标）' : '○ 上下文增强：关闭（默认，速度优先）'}
        </button>

        {/* === SAM 检测阈值调节 === */}
        <div style={{ borderTop: '1px solid rgba(180,160,130,0.2)', paddingTop: 10, marginTop: 2 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
            <span style={{ color: '#64748b', fontSize: 11, fontWeight: 500 }}>⚙ 检测阈值</span>
            <button
              onClick={() => {
                fetch('/api/sam-thresholds', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({
                    classify_conf_thd: classifyThd,
                    box_conf_thd: boxThd,
                    verify_conf_thd: verifyThd,
                    auto_verify_thd: autoVerifyThd,
                    classify_max_side: classifyMaxSide,
                  }),
                }).catch(() => {});
              }}
              disabled={isRunning || searchRunning}
              style={{
                border: '1px solid rgba(14,165,233,0.25)',
                borderRadius: 6,
                background: '#f0f9ff',
                color: '#0284c7',
                cursor: isRunning || searchRunning ? 'not-allowed' : 'pointer',
                fontSize: 11,
                padding: '4px 10px',
                fontWeight: 500,
                transition: 'all 0.15s',
              }}
            >保存</button>
          </div>
          {/* 分类阈值 */}
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#64748b' }}>
              <span>分类阈值</span>
              <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{classifyThd.toFixed(2)}</span>
            </div>
            <input
              type="range" min="0" max="1" step="0.01"
              value={classifyThd}
              onChange={e => setClassifyThd(parseFloat(e.target.value))}
              disabled={isRunning || searchRunning}
              style={{ width: '100%', accentColor: '#0ea5e9', height: 6 }}
            />
          </div>
          {/* 候选框阈值 */}
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#64748b' }}>
              <span>候选框阈值</span>
              <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{boxThd.toFixed(2)}</span>
            </div>
            <input
              type="range" min="0" max="1" step="0.01"
              value={boxThd}
              onChange={e => setBoxThd(parseFloat(e.target.value))}
              disabled={isRunning || searchRunning}
              style={{ width: '100%', accentColor: '#0ea5e9', height: 6 }}
            />
          </div>
          {/* 二次确认阈值 */}
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#64748b' }}>
              <span>二次确认阈值</span>
              <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{verifyThd.toFixed(2)}</span>
            </div>
            <input
              type="range" min="0" max="1" step="0.01"
              value={verifyThd}
              onChange={e => setVerifyThd(parseFloat(e.target.value))}
              disabled={isRunning || searchRunning}
              style={{ width: '100%', accentColor: '#0ea5e9', height: 6 }}
            />
          </div>
          {/* 自动放行阈值 */}
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#64748b' }}>
              <span>自动放行阈值</span>
              <span style={{ color: '#059669', fontWeight: 600 }}>{autoVerifyThd.toFixed(2)}</span>
            </div>
            <input
              type="range" min="0" max="1" step="0.01"
              value={autoVerifyThd}
              onChange={e => setAutoVerifyThd(parseFloat(e.target.value))}
              disabled={isRunning || searchRunning}
              style={{ width: '100%', accentColor: '#059669', height: 6 }}
            />
            <div style={{ color: '#94a3b8', fontSize: 9, marginTop: 1 }}>高于此值 + 高于二次确认阈值时跳过验证</div>
          </div>
          {/* 分类图片尺寸 */}
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: '#64748b' }}>
              <span>分类图最大边长</span>
              <span style={{ color: '#64748b', fontWeight: 600 }}>{classifyMaxSide}px</span>
            </div>
            <input
              type="range" min="128" max="2048" step="64"
              value={classifyMaxSide}
              onChange={e => setClassifyMaxSide(parseInt(e.target.value))}
              disabled={isRunning || searchRunning}
              style={{ width: '100%', accentColor: '#64748b', height: 6 }}
            />
            <div style={{ color: '#94a3b8', fontSize: 9, marginTop: 1 }}>越小越快，越大越准（默认 640）</div>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <button
            onClick={runTileSearch}
            disabled={searchRunning || isRunning}
            style={{
              padding: '10px 0',
              border: 'none',
              borderRadius: 8,
              background: searchRunning || isRunning ? '#e2e8f0' : 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)',
              color: searchRunning || isRunning ? '#94a3b8' : '#ffffff',
              cursor: searchRunning || isRunning ? 'not-allowed' : 'pointer',
              fontWeight: 700,
              fontSize: 13,
              boxShadow: (searchRunning || isRunning) ? 'none' : '0 2px 8px rgba(14,165,233,0.3)',
              transition: 'all 0.15s',
            }}
          >
            ▶ 开始搜索
          </button>
          <button
            onClick={searchRunning ? stopSearch : runDetect}
            disabled={isRunning}
            style={{
              padding: '10px 0',
              border: searchRunning ? '1px solid rgba(239,68,68,0.3)' : '1px solid rgba(180,160,130,0.25)',
              borderRadius: 8,
              background: searchRunning ? '#fef2f2' : '#ffffff',
              color: searchRunning ? '#dc2626' : '#475569',
              cursor: isRunning ? 'not-allowed' : 'pointer',
              fontWeight: 700,
              fontSize: 13,
              boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
              transition: 'all 0.15s',
            }}
          >
            {searchRunning ? '■ 停止' : '单块识别'}
          </button>
        </div>

        <button
          type="button"
          onClick={() => {
            if (demoMode) { setDemoMode(false); setFastMode(true); }
            else if (fastMode) { setFastMode(false); }
            else { setDemoMode(true); setFastMode(true); }
          }}
          disabled={isRunning || searchRunning}
          style={{
            padding: '8px 12px',
            border: demoMode ? '1px solid rgba(99,102,241,0.40)' : fastMode ? '1px solid rgba(34,197,94,0.35)' : '1px solid rgba(180,160,130,0.25)',
            borderRadius: 8,
            background: demoMode ? '#eef2ff' : fastMode ? '#f0fdf4' : '#ffffff',
            color: demoMode ? '#3730a3' : fastMode ? '#166534' : '#64748b',
            cursor: isRunning || searchRunning ? 'not-allowed' : 'pointer',
            fontWeight: 600,
            fontSize: 12,
            textAlign: 'left',
            boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            transition: 'all 0.15s',
          }}
        >
          识别模式：{demoMode ? '⚡ Demo极速' : fastMode ? '🚀 快速优先' : '🎯 精度优先'}
        </button>

        {/* === 变化检测模式 === */}
        <div style={{ borderTop: '1px solid rgba(180,160,130,0.2)', paddingTop: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ color: '#64748b', fontWeight: 500, fontSize: 11 }}>变化检测</span>
            <button
              onClick={() => setChangeMode(v => !v)}
              disabled={isRunning || searchRunning || changeRunning}
              style={{
                border: changeMode ? '1px solid rgba(234,179,8,0.35)' : '1px solid rgba(180,160,130,0.25)',
                borderRadius: 6,
                background: changeMode ? '#fefce8' : '#ffffff',
                color: changeMode ? '#92400e' : '#64748b',
                cursor: 'pointer',
                fontSize: 11,
                padding: '3px 10px',
                fontWeight: 600,
              }}
            >
              {changeMode ? '✓ 开启' : '○ 关闭'}
            </button>
          </div>
          {changeMode && (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr', gap: 6, alignItems: 'center', marginBottom: 8 }}>
                <select
                  value={changeYearA}
                  onChange={e => setChangeYearA(parseInt(e.target.value))}
                  disabled={changeRunning}
                  style={{
                    background: '#fff', border: '1px solid rgba(180,160,130,0.25)', borderRadius: 6,
                    padding: '6px 8px', fontSize: 12, color: '#1e293b', outline: 'none', cursor: 'pointer',
                  }}
                >
                  <option value={2023}>2023年</option>
                  <option value={2024}>2024年</option>
                  <option value={2025}>2025年</option>
                </select>
                <span style={{ color: '#94a3b8', fontSize: 11, textAlign: 'center' }}>vs</span>
                <select
                  value={changeYearB}
                  onChange={e => setChangeYearB(parseInt(e.target.value))}
                  disabled={changeRunning}
                  style={{
                    background: '#fff', border: '1px solid rgba(180,160,130,0.25)', borderRadius: 6,
                    padding: '6px 8px', fontSize: 12, color: '#1e293b', outline: 'none', cursor: 'pointer',
                  }}
                >
                  <option value={2023}>2023年</option>
                  <option value={2024}>2024年</option>
                  <option value={2025}>2025年</option>
                </select>
              </div>
              <button
                onClick={runChangeDetect}
                disabled={changeRunning || isRunning || searchRunning}
                style={{
                  width: '100%', padding: '10px 0',
                  border: 'none', borderRadius: 8,
                  background: changeRunning ? '#e2e8f0' : 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
                  color: changeRunning ? '#94a3b8' : '#ffffff',
                  cursor: changeRunning ? 'not-allowed' : 'pointer',
                  fontWeight: 700, fontSize: 13,
                  boxShadow: changeRunning ? 'none' : '0 2px 8px rgba(245,158,11,0.3)',
                  transition: 'all 0.15s',
                }}
              >
                {changeRunning ? '检测中...' : `变化检测 ${changeYearA} vs ${changeYearB}`}
              </button>
              <div style={{ color: '#ef4444', fontSize: 9, marginTop: 4 }}>红色=新增, 蓝色=消失, 灰色=无变化</div>
            </>
          )}
        </div>

        <div style={{ borderTop: '1px solid rgba(180,160,130,0.2)', paddingTop: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ color: '#64748b', fontWeight: 500 }}>搜索进度</span>
            <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{progressPct}%</span>
          </div>
          <div style={{ height: 7, background: '#e2e8f0', borderRadius: 999, overflow: 'hidden' }}>
            <div style={{
              width: `${Math.min(progressPct, 100)}%`,
              height: '100%',
              background: 'linear-gradient(90deg,#0ea5e9,#22c55e)',
              transition: 'width 0.35s ease',
            }} />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 10 }}>
            <div style={{ background: '#f8f7f4', padding: '8px 10px', borderRadius: 8, border: '1px solid rgba(180,160,130,0.12)' }}>
              <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>总计</div>
              <div style={{ color: '#1e293b', fontWeight: 700, fontSize: 14 }}>{searchStats.total}</div>
            </div>
            <div style={{ background: '#f8f7f4', padding: '8px 10px', borderRadius: 8, border: '1px solid rgba(180,160,130,0.12)' }}>
              <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>已搜索</div>
              <div style={{ color: '#0ea5e9', fontWeight: 700, fontSize: 14 }}>{searchStats.current}</div>
            </div>
            <div style={{ background: '#f8f7f4', padding: '8px 10px', borderRadius: 8, border: '1px solid rgba(180,160,130,0.12)' }}>
              <div style={{ color: '#94a3b8', fontSize: 10, marginBottom: 2 }}>发现</div>
              <div style={{ color: '#059669', fontWeight: 700, fontSize: 14 }}>{searchStats.found}</div>
            </div>
          </div>
          <div style={{ color: '#94a3b8', fontSize: 10, marginTop: 7 }}>{progressMsg || `已绘制 ${drawCount} 个范围`}</div>
          {taskId && <div style={{ color: '#cbd5e1', fontSize: 10, marginTop: 3 }}>task: {taskId}</div>}
        </div>

        <div style={{ borderTop: '1px solid rgba(180,160,130,0.2)', paddingTop: 10 }}>
          <div style={{ color: '#64748b', marginBottom: 6, fontWeight: 500 }}>当前推理</div>
          <div style={{ maxHeight: 220, overflowY: 'auto', background: '#f8f7f4', borderRadius: 8, padding: 8, border: '1px solid rgba(180,160,130,0.12)' }}>
            {progress.length === 0 && <div style={{ color: '#94a3b8' }}>等待任务开始...</div>}
            {progress.map((p, i) => (
              <div key={i} style={{
                color: p.tone === 'error' ? '#ef4444' : p.tone === 'success' ? '#059669' : '#334155',
                lineHeight: 1.45,
                marginBottom: 5,
              }}>
                <span style={{ color: '#94a3b8', marginRight: 5 }}>[{p.time}]</span>{p.text}
              </div>
            ))}
            <div ref={logRef} />
          </div>
        </div>

        <div style={{ borderTop: '1px solid rgba(180,160,130,0.2)', paddingTop: 10, paddingBottom: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ color: '#64748b', fontWeight: 500 }}>发现目标</span>
            <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{result?.features?.length || 0}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <button
              onClick={downloadSHP}
              disabled={!result}
              style={{
                padding: '9px 0',
                border: 'none',
                borderRadius: 8,
                background: result ? 'linear-gradient(135deg, #059669 0%, #10b981 100%)' : '#e2e8f0',
                color: result ? '#ffffff' : '#94a3b8',
                cursor: result ? 'pointer' : 'not-allowed',
                fontWeight: 700,
                fontSize: 13,
                boxShadow: result ? '0 2px 8px rgba(5,150,105,0.3)' : 'none',
                transition: 'all 0.15s',
              }}
            >
              下载 SHP
            </button>
            <button
              onClick={() => { clearDrawn(); clearResult(); }}
              style={{
                padding: '9px 0',
                border: '1px solid rgba(180,160,130,0.25)',
                borderRadius: 8,
                background: '#ffffff',
                color: '#475569',
                cursor: 'pointer',
                fontWeight: 700,
                fontSize: 13,
                boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
                transition: 'all 0.15s',
              }}
            >
              清除全部
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SAMPanel;
