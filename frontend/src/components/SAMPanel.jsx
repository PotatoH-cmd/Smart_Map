import React, { useState, useEffect, useRef, useCallback } from 'react';
import L from 'leaflet';
import useLeafletDraw from '../hooks/useLeafletDraw';
import { useReSAMWorkflow } from '../hooks/useReSAMWorkflow';
import WorkflowStepper from './WorkflowStepper';

const SAMPanel = ({ mapManager }) => {
  const [drawMode, setDrawMode] = useState('rectangle');
  const [prompt, setPrompt] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [isDrawing, setIsDrawing] = useState(false);
  const [progress, setProgress] = useState([]);
  const [result, setResult] = useState(null);
  const [resultLayer, setResultLayer] = useState(null);
  const [coarseResultLayer, setCoarseResultLayer] = useState(null);  // 粗检测图层（精修后保留作对比）
  const [highlightLayer, setHighlightLayer] = useState(null);        // 点击高亮图层
  const [drawCount, setDrawCount] = useState(0);
  // 实时进度条状态
  const [progressPct, setProgressPct] = useState(0);
  const [progressMsg, setProgressMsg] = useState('');
  const [taskId, setTaskId] = useState('');
  const [fastMode, setFastMode] = useState(true);
  const [preciseMode, setPreciseMode] = useState(false);  // 精度模式：默认关闭（快速优先）
  const [drawnBounds, setDrawnBounds] = useState(null);
  const pollRef = useRef(null);
  const [requeryRunning, setRequeryRunning] = useState(false);
  const [selectedFeatIdx, setSelectedFeatIdx] = useState(null);
  const [workflowMode, setWorkflowMode] = useState(false);

  // ── ReSAM 工作流 Hook（串联 SAM→标注→训练闭环） ──
  const workflow = useReSAMWorkflow();

  // ── 共享绘制 Hook（替代内联绘制逻辑） ──
  const {
    drawnItemsRef,
    startRectDraw: hookStartRectDraw,
    startPolyDraw: hookStartPolyDraw,
    clearDrawings,
    getDrawnGeoJSON,
    getDrawnBounds,
    getDrawCount,
  } = useLeafletDraw(mapManager, {
    paneName: 'samDrawPane',
    paneZIndex: 900,
    style: {
      color: '#ff6b00',
      fillColor: '#ff6b00',
      polyColor: '#ff6b00',
      polyFillColor: '#ff6b00',
      polyDashArray: '6',
    },
  });

  const logRef = useRef(null);
  const svgRenderersRef = useRef({});

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

  useEffect(() => {
    logRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' });
  }, [progress]);

  // ── 绘制操作（委托给 useLeafletDraw hook） ──

  const plainBounds = useCallback((bounds) => ({
    south: bounds.getSouth(),
    west: bounds.getWest(),
    north: bounds.getNorth(),
    east: bounds.getEast(),
  }), []);

  // 矩形绘制 — 通过 hook
  const startRectDraw = useCallback(() => {
    hookStartRectDraw((layer) => {
      setIsDrawing(false);
      setDrawnBounds(plainBounds(layer.getBounds()));
      setDrawCount(getDrawCount());
    });
  }, [hookStartRectDraw, plainBounds, getDrawCount]);

  // 多边形绘制 — 通过 hook
  const startPolyDraw = useCallback(() => {
    hookStartPolyDraw((layer) => {
      setIsDrawing(false);
      setDrawnBounds(plainBounds(layer.getBounds()));
      setDrawCount(getDrawCount());
    });
  }, [hookStartPolyDraw, plainBounds, getDrawCount]);

  // 开始绘制
  const startDraw = () => {
    setIsDrawing(true);
    if (drawMode === 'rectangle') startRectDraw();
    else startPolyDraw();
  };

  // 清除绘制区域
  const clearDrawn = useCallback(() => {
    clearDrawings();
    setDrawCount(0);
    setDrawnBounds(null);
    setIsDrawing(false);
  }, [clearDrawings]);

  // 清除结果（含粗检测、精修、高亮图层）
  const clearResult = useCallback(() => {
    if (resultLayer && mapManager) {
      safelyRemoveLayer(mapManager?.map, resultLayer);
      setResultLayer(null);
    }
    if (coarseResultLayer && mapManager) {
      safelyRemoveLayer(mapManager?.map, coarseResultLayer);
      setCoarseResultLayer(null);
    }
    if (highlightLayer && mapManager) {
      safelyRemoveLayer(mapManager?.map, highlightLayer);
      setHighlightLayer(null);
    }
    clearSvgRenderers(mapManager?.map);
    setResult(null);
    setSelectedFeatIdx(null);
    setProgress([]);
    setProgressPct(0);
    setProgressMsg('');
  }, [resultLayer, coarseResultLayer, highlightLayer, mapManager, safelyRemoveLayer, clearSvgRenderers]);

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

  // 加载预计算的测试结果
  const loadTestResult = async () => {
    setIsRunning(true);
    setProgress([]);
    clearResult();
    const addLog = (text, tone = 'info') => {
      setProgress(prev => [...prev, { text, tone, time: new Date().toLocaleTimeString() }]);
    };
    addLog('加载测试结果...');
    try {
      const res = await fetch('/api/sam-test-result');
      const data = await res.json();
      if (!data.features || data.features.length === 0) {
        addLog('测试数据不可用', 'error');
        return;
      }
      addLog(`✅ 加载成功！检测到 ${data.features.length} 个建筑图斑`, 'success');
      if (mapManager?.map) {
        const layer = L.geoJSON(data, {
          renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
          interactive: true, bubblingMouseEvents: false,
          style: { color: '#e53e3e', weight: 2, fillColor: '#fc8181', fillOpacity: 0.4 },
          onEachFeature: (feature, l) => {
            const p = feature.properties || {};
            l.bindPopup(`<b>SAM 识别结果</b><br/>目标: 建筑<br/>面积: ${p.area_m2 || ''} m² (${p.area_mu || ''} 亩)`);
          },
        });
        layer.addTo(mapManager.map);
        setResultLayer(layer);
        setResult(data);
        mapManager.map.fitBounds(layer.getBounds().pad(0.1));
        addLog('结果已加载到地图', 'success');
      }
    } catch(err) {
      addLog(`错误: ${err.message}`, 'error');
    } finally {
      setIsRunning(false);
    }
  };

  // 运行 SAM 检测（SSE 流式）
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
    setProgressPct(2);
    setProgressMsg('准备中...');

    const addLog = (text, tone = 'info') => {
      setProgress(prev => [...prev, { text, tone, time: new Date().toLocaleTimeString() }]);
    };

    addLog('开始 SAM 目标识别...');

    try {
      const layers = drawnItemsRef.current.getLayers();
      const geojsons = layers.map(l => l.toGeoJSON());
      const allCoords = [];
      geojsons.forEach(gj => {
        const coords = gj.geometry.type === 'Polygon' ? gj.geometry.coordinates[0] : [];
        coords.forEach(c => allCoords.push(c));
      });

      if (allCoords.length < 3) {
        addLog('绘制区域几何无效', 'error');
        setIsRunning(false);
        return;
      }

      addLog(`检测到 ${geojsons.length} 个绘制区域`);
      addLog(`识别模式：${preciseMode ? '🎯 精度优先（瓦片推理）' : '⚡ 快速模式（单次推理）'}`);

      // ── SSE 流式请求 ──
      const res = await fetch('/api/sam-detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
        body: JSON.stringify({
          geometry: geojsons[0].geometry,
          prompt: prompt.trim(),
          mode: drawMode,
          fast_mode: fastMode,
          precise_mode: preciseMode,
        }),
      });

      if (!res.ok) {
        const err = await res.text().catch(() => `HTTP ${res.status}`);
        throw new Error(err.slice(0, 300));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let finalData = null;

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';

        for (const chunk of chunks) {
          const lines = chunk.split('\n').filter(Boolean);
          for (const line of lines) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;

            try {
              const event = JSON.parse(payload);

              if (event.type === 'progress') {
                if (event.percent >= 0) {
                  setProgressPct(event.percent);
                  setProgressMsg(event.message || '');
                }
                if (event.stage === 'inference' || event.stage === 'inference_done') {
                  addLog(event.message || event.stage);
                }
              } else if (event.type === 'final') {
                finalData = event.result;
              } else if (event.type === 'error') {
                throw new Error(event.message);
              }
            } catch (parseErr) {
              // 非 JSON 行忽略
            }
          }
        }

        if (done) break;
      }

      if (!finalData) {
        throw new Error('未收到识别结果');
      }

      // ── 处理最终结果 ──
      setProgressPct(100);
      setProgressMsg('推理完成');
      setTaskId(finalData._task_id || '');

      const nFeatures = finalData.features?.length || 0;
      addLog(`推理完成！检测到 ${nFeatures} 个目标`, 'success');

      if (nFeatures > 0 && mapManager?.map) {
        const layer = L.geoJSON(finalData, {
          renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
          interactive: true,
          bubblingMouseEvents: false,
          style: {
            color: '#e53e3e', weight: 2,
            fillColor: '#fc8181', fillOpacity: 0.4,
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
        setResult(finalData);

        workflow.startDetect(finalData);
        mapManager.map.fitBounds(layer.getBounds().pad(0.1));
        addLog('结果已加载到地图', 'success');
      } else if (nFeatures === 0) {
        setResult(finalData);
        addLog('未检测到目标', 'info');
        addLog('提示：尝试缩小绘制区域或换一个位置', 'info');
      }
    } catch (err) {
      addLog(`错误: ${err.message}`, 'error');
      setProgressMsg('出错: ' + err.message);
      setProgressPct(0);
    } finally {
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

  // Requery 精修
  const runRequery = async () => {
    if (!result?.features?.length) return;
    const layers = drawnItemsRef.current?.getLayers?.() || [];
    if (layers.length === 0) return;
    const geometry = layers[0].toGeoJSON().geometry;

    setRequeryRunning(true);
    addLog('▶ Requery 精修启动...');
    try {
      const res = await fetch('/api/sam-requery', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          geometry,
          prompt: prompt.trim(),
          coarse_geojson: result,
        }),
      });
      if (!res.ok) {
        const err = await res.text();
        throw new Error(err.slice(0, 200));
      }
      const refined = await res.json();
      const nRefined = refined.features?.filter(f => f.properties?.refined)?.length || 0;
      addLog(`✓ Requery 完成: ${refined.features?.length || 0} 个特征 (${nRefined} 精修)`, 'success');

      if (mapManager?.map && refined.features?.length) {
        // ── 保留粗检测图层（改为红色虚线，稍淡）──
        if (resultLayer) {
          // 改变粗检测图层样式：红色虚线 + 透明填充
          try {
            resultLayer.setStyle?.({
              color: '#ef4444', weight: 1.5, dashArray: '6 4',
              fillColor: '#fca5a5', fillOpacity: 0.15,
            });
          } catch (_) {
            // setStyle 不可用时重建图层
            safelyRemoveLayer(mapManager.map, resultLayer);
            const coarseGeo = resultLayer.toGeoJSON?.() || result;
            const coarseL = L.geoJSON(coarseGeo, {
              renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
              interactive: true, bubblingMouseEvents: false,
              style: { color: '#ef4444', weight: 1.5, dashArray: '6 4', fillColor: '#fca5a5', fillOpacity: 0.15 },
              onEachFeature: (f, l) => {
                const p = f.properties || {};
                l.bindPopup(`<b>SAM 粗检测</b><br/>目标: ${prompt}<br/>面积: ${p.area_m2 ? p.area_m2.toFixed(1)+' m²' : ''}`);
              },
            });
            coarseL.addTo(mapManager.map);
            setCoarseResultLayer(coarseL);
          }
          // 保存粗检测引用（如果还没存）
          if (!coarseResultLayer) setCoarseResultLayer(resultLayer);
          setResultLayer(null);
        }

        // ── 精修图层（绿色实线 + 明显填充）──
        const layer = L.geoJSON(refined, {
          renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
          interactive: true,
          bubblingMouseEvents: false,
          style: (feature) => ({
            color: feature?.properties?.refined ? '#059669' : '#e53e3e',
            weight: feature?.properties?.refined ? 3 : 2,
            fillColor: feature?.properties?.refined ? '#34d399' : '#fc8181',
            fillOpacity: 0.45,
          }),
          onEachFeature: (feature, l) => {
            const p = feature.properties || {};
            const source = p.source === 'sam_requery' ? ' (精修)' : '';
            l.bindPopup(`<b>SAM${source}</b><br/>目标: ${prompt}<br/>面积: ${p.area_m2 ? p.area_m2.toFixed(1)+' m²' : ''}`);
          },
        });
        layer.addTo(mapManager.map);
        setResultLayer(layer);
      }
      setResult(refined);
      workflow.startRequery(refined);
    } catch (err) {
      addLog(`✗ Requery 失败: ${err.message}`, 'error');
    } finally {
      setRequeryRunning(false);
    }
  };

  // 发送当前结果到标注面板
  const sendToAnnotation = async () => {
    if (!result?.features?.length) return;
    const sessionId = `sam_${Date.now()}`;
    try {
      const items = result.features.map((f) => ({
        session_id: sessionId,
        image_path: '',
        label: prompt.trim(),
        class_id: null,
        geometry: f.geometry,
        source: f.properties?.source || 'sam_preannotate',
        confidence: f.properties?.requery_confidence || 0.8,
      }));
      const res = await fetch('/api/annotations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ annotations: items }),
      });
      if (!res.ok) throw new Error((await res.text()).slice(0, 200));
      addLog(`✓ 已发送 ${items.length} 条预标注到标注面板`, 'success');

      // 刷新工作流标注计数
      workflow.refreshAnnotationCount(sessionId);
      // 记录 sessionId 以便后续训练
      if (!_trainSessionIds.includes(sessionId)) {
        _trainSessionIds.push(sessionId);
      }
    } catch (err) {
      addLog(`✗ 发送失败: ${err.message}`, 'error');
    }
  };

  // 累积的训练 session ID 列表（模块级，避免闭包问题）
  const _trainSessionIdsRef = useRef([]);
  const _trainSessionIds = _trainSessionIdsRef.current;

  // 高亮地图上的单个特征
  const highlightFeatureOnMap = useCallback((feature, idx) => {
    if (!mapManager?.map) return;
    // 移除旧高亮
    if (highlightLayer) safelyRemoveLayer(mapManager.map, highlightLayer);

    const featGeojson = {
      type: 'FeatureCollection',
      features: [feature],
    };
    const hl = L.geoJSON(featGeojson, {
      renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
      interactive: false, bubblingMouseEvents: false,
      style: {
        color: '#f59e0b', weight: 4,
        fillColor: '#fbbf24', fillOpacity: 0.35,
      },
    });
    hl.addTo(mapManager.map);
    setHighlightLayer(hl);

    // 飞到该特征
    try {
      const layer = L.geoJSON(feature);
      const b = layer.getBounds();
      if (b.isValid()) mapManager.map.flyToBounds(b, { padding: [60, 60], maxZoom: 17 });
    } catch (_) {}
  }, [mapManager, highlightLayer, safelyRemoveLayer, getSvgRenderer]);

  const clearHighlight = useCallback(() => {
    if (highlightLayer && mapManager) {
      safelyRemoveLayer(mapManager?.map, highlightLayer);
      setHighlightLayer(null);
    }
  }, [highlightLayer, mapManager, safelyRemoveLayer]);

  // 删除单个检测结果特征
  const deleteFeature = (idx) => {
    if (!result?.features) return;
    const newFeatures = result.features.filter((_, i) => i !== idx);
    const newResult = { ...result, features: newFeatures };
    setResult(newResult);
    if (mapManager?.map && resultLayer) {
      safelyRemoveLayer(mapManager.map, resultLayer);
      if (newFeatures.length > 0) {
        const layer = L.geoJSON(newResult, {
          renderer: getSvgRenderer(mapManager.map, 'overlayPane'),
          interactive: true,
          bubblingMouseEvents: false,
          style: { color: '#e53e3e', weight: 2, fillColor: '#fc8181', fillOpacity: 0.4 },
          onEachFeature: (feature, l) => {
            const p = feature.properties || {};
            l.bindPopup(`<b>SAM</b><br/>目标: ${p.prompt || prompt}<br/>面积: ${p.area_m2 ? p.area_m2.toFixed(1)+' m²' : ''}`);
          },
        });
        layer.addTo(mapManager.map);
        setResultLayer(layer);
      } else {
        setResultLayer(null);
        setResult(null);
      }
    }
  };

  // 提交 SSA 训练
  const handleSubmitTrain = async () => {
    if (_trainSessionIds.length === 0) {
      addLog('✗ 无标注 session 可供训练', 'error');
      return;
    }
    addLog('▶ 提交 SSA 训练任务...');
    const tid = await workflow.submitTrain([..._trainSessionIds], {
      epochs: 10,
      checkpointName: `sam_${prompt.trim().slice(0, 20).replace(/\s+/g, '_')}_v1`,
    });
    if (tid) {
      addLog(`✓ 训练任务已提交: ${tid.slice(0, 8)}`, 'success');
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
        <div style={{ color: '#64748b', fontSize: 10 }}>
          SAM / Rule-based progressive tile search
          <button
            onClick={() => setWorkflowMode(v => !v)}
            style={{
              marginLeft: 8, border: workflowMode ? '1px solid #7c3aed' : '1px solid #e2e8f0',
              borderRadius: 4, background: workflowMode ? '#f5f3ff' : '#fff',
              color: workflowMode ? '#7c3aed' : '#94a3b8', cursor: 'pointer',
              fontSize: 10, padding: '1px 6px', fontWeight: 600, verticalAlign: 'middle',
            }}
          >
            {workflowMode ? '● 工作流' : '○ 工作流'}
          </button>
        </div>
      </div>

      {/* ── ReSAM 工作流步骤可视化 ── */}
      {workflowMode && (
        <div style={{ padding: '0 14px' }}>
          <WorkflowStepper
            currentPhase={workflow.phase}
            completedPhases={
              new Set(
                workflow.PHASE_ORDER.slice(0, workflow.phaseIndex)
              )
            }
            onPhaseClick={(phase) => workflow.goToPhase(phase)}
            iteration={workflow.iteration}
            annotationCount={workflow.annotationCount}
          />
        </div>
      )}

      <div style={{ padding: 14, display: 'grid', gap: 10, gridTemplateRows: 'auto' }}>
        <div>
          <div style={{ color: '#64748b', fontSize: 11, marginBottom: 5, fontWeight: 500 }}>检测目标</div>
          <textarea
            placeholder="输入任意目标描述，如：建筑、水、桥梁、烟囱、厂房..."
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
          disabled={isDrawing || isRunning}
          style={{
            padding: '10px 0',
            border: isDrawing ? '1px dashed rgba(234,179,8,0.5)' : '1px dashed rgba(14,165,233,0.35)',
            borderRadius: 8,
            background: isDrawing ? '#fefce8' : '#f8fafc',
            color: isDrawing ? '#92400e' : '#0284c7',
            cursor: isDrawing || isRunning ? 'not-allowed' : 'pointer',
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

        <button
          type="button"
          onClick={() => setPreciseMode(v => !v)}
          disabled={isRunning}
          style={{
            padding: '8px 12px',
            border: preciseMode ? '1px solid rgba(180,160,130,0.25)' : '1px solid rgba(99,102,241,0.40)',
            borderRadius: 8,
            background: preciseMode ? '#ffffff' : '#eef2ff',
            color: preciseMode ? '#64748b' : '#3730a3',
            cursor: isRunning ? 'not-allowed' : 'pointer',
            fontWeight: 600,
            fontSize: 12,
            textAlign: 'left',
            boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
            transition: 'all 0.15s',
          }}
        >
          识别模式：{preciseMode ? '🎯 精度优先' : '⚡ 快速模式'}
        </button>

        <button
          onClick={runDetect}
          disabled={isRunning}
          style={{
            padding: '10px 0',
            border: 'none',
            borderRadius: 8,
            background: isRunning ? '#e2e8f0' : 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)',
            color: isRunning ? '#94a3b8' : '#ffffff',
            cursor: isRunning ? 'not-allowed' : 'pointer',
            fontWeight: 700,
            fontSize: 13,
            boxShadow: isRunning ? 'none' : '0 2px 8px rgba(14,165,233,0.3)',
            transition: 'all 0.15s',
          }}
        >
          {isRunning ? '识别中...' : '▶ 开始识别'}
        </button>

        <button
          onClick={loadTestResult}
          disabled={isRunning}
          style={{
            padding: '8px 0',
            border: '1px solid #e2e8f0',
            borderRadius: 8,
            background: '#f8fafc',
            color: isRunning ? '#94a3b8' : '#64748b',
            cursor: isRunning ? 'not-allowed' : 'pointer',
            fontWeight: 600,
            fontSize: 12,
            marginTop: 6,
            transition: 'all 0.15s',
          }}
        >
          加载测试结果（南阳市区建筑）
        </button>

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
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ color: '#64748b', fontWeight: 500 }}>发现目标</span>
            <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{result?.features?.length || 0}</span>
          </div>

          {/* Requery 精修按钮 */}
          {result?.features?.length > 0 && (
            <button
              onClick={runRequery}
              disabled={requeryRunning}
              style={{
                width: '100%', padding: '9px 0', marginBottom: 8,
                border: 'none', borderRadius: 8,
                background: requeryRunning ? '#e2e8f0' : 'linear-gradient(135deg, #7c3aed 0%, #a855f7 100%)',
                color: requeryRunning ? '#94a3b8' : '#ffffff',
                cursor: requeryRunning ? 'not-allowed' : 'pointer',
                fontWeight: 700, fontSize: 13,
                boxShadow: requeryRunning ? 'none' : '0 2px 8px rgba(124,58,237,0.3)',
                transition: 'all 0.15s',
              }}
            >
              {requeryRunning ? '精修中...' : '🔍 Requery 精修'}
            </button>
          )}

          {/* 结果特征列表 */}
          {result?.features?.length > 0 && (
            <div style={{ maxHeight: 200, overflowY: 'auto', background: '#f8f7f4', borderRadius: 8, padding: 6, marginBottom: 8, border: '1px solid rgba(180,160,130,0.12)' }}>
              {result.features.map((f, i) => {
                const p = f.properties || {};
                const isRefined = p.source === 'sam_requery';
                const areaMu = p.area_mu ? `${p.area_mu.toFixed(2)}亩` : '';
                return (
                  <div key={i} onClick={() => {
                    const newIdx = selectedFeatIdx === i ? null : i;
                    setSelectedFeatIdx(newIdx);
                    if (newIdx !== null) {
                      highlightFeatureOnMap(f, i);
                    } else {
                      clearHighlight();
                    }
                  }} style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '5px 6px', marginBottom: 3, borderRadius: 6,
                    background: selectedFeatIdx === i ? '#e0f2fe' : (isRefined ? '#ecfdf5' : '#ffffff'),
                    border: selectedFeatIdx === i ? '1px solid #0ea5e9' : '1px solid transparent',
                    cursor: 'pointer', fontSize: 11, transition: 'all 0.1s',
                  }}>
                    <div style={{ flex: 1, overflow: 'hidden' }}>
                      <div style={{ color: '#1e293b', fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {isRefined && <span style={{ color: '#059669', marginRight: 3 }}>✦</span>}
                        目标 #{i + 1}
                      </div>
                      {areaMu && <div style={{ color: '#94a3b8', fontSize: 10 }}>{areaMu}</div>}
                    </div>
                    <button onClick={(e) => { e.stopPropagation(); deleteFeature(i); }} title="删除此目标" style={{
                      border: 'none', background: 'rgba(239,68,68,0.08)', color: '#ef4444',
                      borderRadius: 4, cursor: 'pointer', fontSize: 14, width: 22, height: 22,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      flexShrink: 0, marginLeft: 4,
                    }}>×</button>
                  </div>
                );
              })}
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6 }}>
            <button
              onClick={sendToAnnotation}
              disabled={!result?.features?.length}
              style={{
                padding: '9px 0', border: 'none', borderRadius: 8,
                background: result?.features?.length ? 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)' : '#e2e8f0',
                color: result?.features?.length ? '#ffffff' : '#94a3b8',
                cursor: result?.features?.length ? 'pointer' : 'not-allowed',
                fontWeight: 700, fontSize: 11,
                boxShadow: result?.features?.length ? '0 2px 8px rgba(14,165,233,0.3)' : 'none',
                transition: 'all 0.15s',
              }}
            >
              📋 发送标注
            </button>
            <button
              onClick={downloadSHP}
              disabled={!result}
              style={{
                padding: '9px 0', border: 'none', borderRadius: 8,
                background: result ? 'linear-gradient(135deg, #059669 0%, #10b981 100%)' : '#e2e8f0',
                color: result ? '#ffffff' : '#94a3b8',
                cursor: result ? 'pointer' : 'not-allowed',
                fontWeight: 700, fontSize: 11,
                boxShadow: result ? '0 2px 8px rgba(5,150,105,0.3)' : 'none',
                transition: 'all 0.15s',
              }}
            >
              下载 SHP
            </button>
            <button
              onClick={() => { clearDrawn(); clearResult(); }}
              style={{
                padding: '9px 0', border: '1px solid rgba(180,160,130,0.25)', borderRadius: 8,
                background: '#ffffff', color: '#475569',
                cursor: 'pointer', fontWeight: 700, fontSize: 11,
                boxShadow: '0 1px 2px rgba(0,0,0,0.03)', transition: 'all 0.15s',
              }}
            >
              清除
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SAMPanel;
