import React, { useState, useEffect, useRef, useCallback } from 'react';
import L from 'leaflet';
import useLeafletDraw from '../hooks/useLeafletDraw';

const ANNOTATION_LABELS = ['建筑', '厂房', '道路', '水体', '农田', '裸地', '其他'];
// 更多可选类别建议（SAM 模型支持任意文本，这里只是快捷提示）
const LABEL_SUGGESTIONS = [...ANNOTATION_LABELS, '烟囱', '光伏板', '水闸', '桥梁', '围墙', '大棚', '操场', '坑塘', '林地'];
const COLORS = ['#ef4444', '#f59e0b', '#3b82f6', '#06b6d4', '#10b981', '#8b5cf6', '#ec4899'];

// 根据标签名取颜色（class_id < 0 的自定义标签用灰色）
function _colorForLabel(label) {
  const idx = ANNOTATION_LABELS.indexOf(label);
  if (idx >= 0) return COLORS[idx % COLORS.length];
  // 自定义标签：用 hash 取色
  let hash = 0;
  for (let i = 0; i < (label || '').length; i++) hash = ((hash << 5) - hash) + label.charCodeAt(i);
  return COLORS[Math.abs(hash) % COLORS.length];
}

const AnnotationPanel = ({ mapManager }) => {
  // Session
  const [sessionId, setSessionId] = useState(() => `session_${Date.now()}`);
  const [annotations, setAnnotations] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [undoStack, setUndoStack] = useState([]);

  // Drawing
  const [activeTool, setActiveTool] = useState('select'); // select | rect | poly | sam_point | sam_neg
  const [prompt, setPrompt] = useState('');
  const [isSamRunning, setIsSamRunning] = useState(false);
  const drawHandlerRef = useRef(null);

  // ── 识别模式切换 ──
  const [samMode, setSamMode] = useState('text'); // 'text' | 'point'
  const [pointPrompts, setPointPrompts] = useState([]); // [{x, y, label: 1|0}]
  const pointMarkersGroupRef = useRef(null); // 跟踪点提示模式的标记图层
  const [regionBounds, setRegionBounds] = useState(null); // 点击模式下绘制的范围矩形 {west, east, south, north}

  // ── 训练相关状态 ──
  const [trainTaskId, setTrainTaskId] = useState('');
  const [trainStatus, setTrainStatus] = useState(null);

  // ── 模型管理状态 ──
  const [checkpoints, setCheckpoints] = useState([]);
  const [baseCheckpoint, setBaseCheckpoint] = useState('');  // 训练基底模型路径
  const [showModelManager, setShowModelManager] = useState(false);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');

  // ── 共享绘制 Hook（替代内联绘制逻辑） ──
  const {
    drawnItemsRef,
    startRectDraw: hookStartRectDraw,
    startPolyDraw: hookStartPolyDraw,
    clearDrawings,
    getDrawnGeoJSON,
    getDrawnBounds,
    cleanupHandler,
  } = useLeafletDraw(mapManager, {
    paneZIndex: 650,
    style: {
      color: '#0ea5e9',
      fillColor: '#0ea5e9',
      polyColor: '#f59e0b',
      polyFillColor: '#f59e0b',
      polyDashArray: '5 5',
    },
  });

  // Map layers
  const annotLayerRef = useRef(null);
  const editMarkersRef = useRef([]);   // 选中标注的可拖拽顶点

  // ── 初始化地图图层 ──
  useEffect(() => {
    if (!mapManager?.map) return;
    const map = mapManager.map;

    if (!annotLayerRef.current) {
      annotLayerRef.current = new L.FeatureGroup();
      map.addLayer(annotLayerRef.current);
    }

    return () => {
      if (annotLayerRef.current && map.hasLayer(annotLayerRef.current)) {
        map.removeLayer(annotLayerRef.current);
      }
      // 清理编辑顶点
      editMarkersRef.current.forEach(m => map.removeLayer(m));
      editMarkersRef.current = [];
    };
  }, [mapManager]);

  // ── 标注 CRUD（必须在绘制 useEffect 之前声明）──
  const addAnnotation = useCallback((geometry, label) => {
    const newAnn = {
      id: `ann_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
      session_id: sessionId,
      image_path: '',
      label,
      class_id: ANNOTATION_LABELS.indexOf(label),
      geometry,
      source: 'manual',
      confidence: 1.0,
      iteration: 0,
      created_at: new Date().toISOString(),
    };
    setUndoStack(prev => [...prev.slice(-49), annotations]);
    setAnnotations(prev => [...prev, newAnn]);
    return newAnn;
  }, [sessionId, annotations]);

  const updateAnnotation = useCallback((id, updates) => {
    setUndoStack(prev => [...prev.slice(-49), annotations]);
    setAnnotations(prev => prev.map(a => a.id === id ? { ...a, ...updates, updated_at: new Date().toISOString() } : a));
  }, [annotations]);

  const deleteAnnotation = useCallback((id) => {
    setUndoStack(prev => [...prev.slice(-49), annotations]);
    setAnnotations(prev => prev.filter(a => a.id !== id));
    if (selectedId === id) setSelectedId(null);
  }, [annotations, selectedId]);

  const undo = useCallback(() => {
    setUndoStack(prev => {
      if (prev.length === 0) return prev;
      const restored = prev[prev.length - 1];
      setAnnotations(restored);
      return prev.slice(0, -1);
    });
  }, []);

  const clearDrawing = useCallback(() => {
    if (drawnItemsRef.current) drawnItemsRef.current.clearLayers();
  }, []);

  const clearEditMarkers = useCallback(() => {
    if (!mapManager?.map) return;
    editMarkersRef.current.forEach(m => mapManager.map.removeLayer(m));
    editMarkersRef.current = [];
  }, [mapManager]);

  // ── 从 Leaflet 图层创建标注 ──
  // 使用 ref 保持稳定的回调引用，避免 useEffect 依赖循环
  const addAnnotationRef = useRef(addAnnotation);
  addAnnotationRef.current = addAnnotation;

  const addAnnotationFromLayer = useCallback((layer) => {
    try {
      const gj = layer.toGeoJSON();
      const geometry = gj?.geometry;
      if (!geometry) {
        console.warn('addAnnotationFromLayer: 无法从图层提取几何');
        return;
      }
      addAnnotationRef.current(geometry, '未分类');
    } catch (err) {
      console.error('addAnnotationFromLayer 失败:', err);
    }
  }, []); // 稳定引用，通过 ref 访问最新的 addAnnotation

  // ── 绘制工具切换 ──
  useEffect(() => {
    if (!mapManager?.map) return;
    const map = mapManager.map;

    // 清除之前的绘制 handler
    if (drawHandlerRef.current) {
      drawHandlerRef.current();
      drawHandlerRef.current = null;
    }

    if (activeTool === 'rect') {
      hookStartRectDraw((layer) => {
        setActiveTool('select');
        addAnnotationFromLayer(layer);
      });
      drawHandlerRef.current = cleanupHandler;
    }

    if (activeTool === 'poly') {
      hookStartPolyDraw((layer) => {
        setActiveTool('select');
        addAnnotationFromLayer(layer);
      });
      drawHandlerRef.current = cleanupHandler;
    }

    if (activeTool === 'sam_point' || activeTool === 'sam_neg') {
      const onClick = (e) => {
        const color = activeTool === 'sam_point' ? '#10b981' : '#ef4444';
        const marker = L.circleMarker(e.latlng, {
          radius: 6, color, fillColor: color, fillOpacity: 0.7,
        }).addTo(map);

        // 点标注模式：点击后自动创建一个小范围
        const offset = 0.0001; // ~11m at equator
        const bounds = L.latLngBounds(
          L.latLng(e.latlng.lat - offset, e.latlng.lng - offset),
          L.latLng(e.latlng.lat + offset, e.latlng.lng + offset)
        );
        drawnItemsRef.current.addLayer(L.rectangle(bounds, {
          color, weight: 2, fillColor: color, fillOpacity: 0.1,
          _samPoint: activeTool === 'sam_point',
        }));

        setTimeout(() => map.removeLayer(marker), 500);
        setActiveTool('select');
      };
      map.on('click', onClick);
      drawHandlerRef.current = () => map.off('click', onClick);
    }

  }, [activeTool, mapManager, hookStartRectDraw, hookStartPolyDraw, addAnnotationFromLayer]);

  // ── 快捷键 ──
  useEffect(() => {
    const handler = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      const key = e.key.toLowerCase();

      if (e.ctrlKey && key === 'z') { e.preventDefault(); undo(); return; }
      if (key === 'b') { setActiveTool('rect'); return; }
      if (key === 'p') { setActiveTool('poly'); return; }
      if (key === 'v') { setActiveTool('select'); return; }
      if (key === '1') { setActiveTool('sam_point'); return; }
      if (key === '2') { setActiveTool('sam_neg'); return; }
      if (key === 'delete' && selectedId) { deleteAnnotation(selectedId); return; }
      if (key === 'escape') { setSelectedId(null); clearDrawing(); clearEditMarkers(); return; }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [selectedId, activeTool]);

  // ── SAM 预标注 ──
  const runSamPreAnnotate = async () => {
    if (!drawnItemsRef.current || drawnItemsRef.current.getLayers().length === 0) {
      alert('请先绘制 ROI 区域');
      return;
    }
    if (!prompt.trim()) {
      alert('请输入识别目标提示词');
      return;
    }

    setIsSamRunning(true);
    try {
      const layers = drawnItemsRef.current.getLayers();
      const gj = layers[0].toGeoJSON();

      const res = await fetch('/api/sam-detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          geometry: gj.geometry,
          prompt: prompt.trim(),
          mode: layers[0] instanceof L.Rectangle ? 'rectangle' : 'polygon',
          fast_mode: true,
          demo_mode: false,
        }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.features && data.features.length > 0) {
        const newAnns = data.features.map(f => ({
          id: `sam_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          session_id: sessionId,
          image_path: '',
          label: prompt.trim(),
          class_id: ANNOTATION_LABELS.indexOf(prompt.trim()),
          geometry: f.geometry,
          source: 'sam_preannotate',
          confidence: f.properties?.confidence || 0.7,
          iteration: 0,
          created_at: new Date().toISOString(),
        }));
        setAnnotations(prev => [...prev, ...newAnns]);
      } else {
        alert('SAM 未检测到目标，请尝试调整 ROI 区域或提示词');
      }
    } catch (err) {
      alert(`SAM 预标注失败: ${err.message}`);
    } finally {
      setIsSamRunning(false);
    }
  };

  // ── 点击提示模式：调用后端分割 ──
  const runPointPredict = async () => {
    if (pointPrompts.length === 0) return;
    setIsSamRunning(true);
    try {
      const points = pointPrompts.map(p => [p.x, p.y]);
      const labels = pointPrompts.map(p => p.label);
      const textPrompt = prompt.trim() || 'object';
      const res = await fetch('/api/sam-predict-point', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          points, labels, session_id: sessionId, prompt: textPrompt,
          image_bounds: regionBounds || undefined,  // 如果有绘制范围则传入
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.polygons && data.polygons.length > 0) {
        const newAnns = data.polygons.map(f => ({
          id: `ann_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          label: prompt.trim() || '目标',
          geometry: f.geometry,
          confidence: f.properties?.confidence || 0.8,
          source: 'point_prompt',
        }));
        setAnnotations(prev => [...prev, ...newAnns]);
        clearPointPrompts();
      } else {
        alert('未检测到目标');
      }
    } catch (err) {
      alert(`点提示分割失败: ${err.message}`);
    } finally {
      setIsSamRunning(false);
    }
  };

  // ── 清除点提示 ──
  const clearPointPrompts = () => {
    setPointPrompts([]);
    // 清除地图上的点标记
    if (pointMarkersGroupRef.current) {
      pointMarkersGroupRef.current.clearLayers();
    }
  };

  // ── 点击模式地图交互 ──
  useEffect(() => {
    console.log('[SAM-Point] useEffect 触发, mapManager.map:', !!mapManager?.map, 'samMode:', samMode);
    if (!mapManager?.map || samMode !== 'point') return;
    const map = mapManager.map;
    console.log('[SAM-Point] 开始绑定 click 事件, container:', !!map.getContainer());

    // 创建点标记图层（如果尚未创建）
    if (!pointMarkersGroupRef.current) {
      pointMarkersGroupRef.current = L.featureGroup().addTo(map);
      console.log('[SAM-Point] 创建 pointMarkersGroupRef');
    }

    // 点击模式下的光标样式
    map.getContainer().style.cursor = 'crosshair';

    // 安全措施：确保地图事件处理器已启用（可能被之前的绘制工具禁用）
    if (map._handlers) {
      map._handlers.forEach(h => { try { h.enable(); } catch (_) {} });
      console.log('[SAM-Point] 已重新启用 map._handlers');
    }
    map.dragging.enable();

    // 禁用已绘制 ROI 图层的交互，避免拦截点击
    if (drawnItemsRef.current) {
      let count = 0;
      drawnItemsRef.current.eachLayer(l => {
        if (l.setStyle) { l.setStyle({ interactive: false }); count++; }
      });
      console.log('[SAM-Point] 禁用 drawnItems 交互:', count, '个图层');
    }

    const container = map.getContainer();

    // 原生 DOM 左键点击（绕过 Leaflet Canvas 瓦片的事件拦截）
    const domClickHandler = (e) => {
      // 只处理左键
      if (e.button !== 0) return;
      const rect = container.getBoundingClientRect();
      const latlng = map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
      console.log('[SAM-Point] 原生 click → 打点! latlng:', latlng);
      const newPoint = { x: latlng.lng, y: latlng.lat, label: 1 };
      setPointPrompts(prev => [...prev, newPoint]);
      L.circleMarker([latlng.lat, latlng.lng], {
        radius: 7, color: '#10b981', fillColor: '#10b981', fillOpacity: 0.8, weight: 2,
      }).addTo(pointMarkersGroupRef.current).bindTooltip('正', { permanent: true, direction: 'top', offset: [0, -8], className: 'point-tooltip' });
    };

    // 原生 DOM 右键菜单（负提示）
    const domContextHandler = (e) => {
      e.preventDefault();
      const rect = container.getBoundingClientRect();
      const latlng = map.containerPointToLatLng([e.clientX - rect.left, e.clientY - rect.top]);
      console.log('[SAM-Point] 原生 contextmenu → 打负点! latlng:', latlng);
      const newPoint = { x: latlng.lng, y: latlng.lat, label: 0 };
      setPointPrompts(prev => [...prev, newPoint]);
      L.circleMarker([latlng.lat, latlng.lng], {
        radius: 7, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 0.8, weight: 2,
      }).addTo(pointMarkersGroupRef.current).bindTooltip('负', { permanent: true, direction: 'top', offset: [0, -8], className: 'point-tooltip' });
    };

    container.addEventListener('click', domClickHandler);
    container.addEventListener('contextmenu', domContextHandler);
    console.log('[SAM-Point] 原生 DOM click/contextmenu 事件已绑定');

    return () => {
      container.removeEventListener('click', domClickHandler);
      container.removeEventListener('contextmenu', domContextHandler);
      console.log('[SAM-Point] cleanup - 移除事件绑定');
      map.getContainer().style.cursor = '';
      // 恢复绘制图层的交互性
      if (drawnItemsRef.current) {
        drawnItemsRef.current.eachLayer(l => {
          if (l.setStyle) l.setStyle({ interactive: true });
        });
      }
      // 清理点标记图层
      if (pointMarkersGroupRef.current) {
        map.removeLayer(pointMarkersGroupRef.current);
        pointMarkersGroupRef.current = null;
      }
    };
  }, [mapManager, samMode]);

  // ── 渲染标注图层（含选中状态的可编辑顶点）──
  useEffect(() => {
    if (!annotLayerRef.current || !mapManager?.map) return;
    const map = mapManager.map;

    // 先清除旧顶点标记
    editMarkersRef.current.forEach(m => map.removeLayer(m));
    editMarkersRef.current = [];
    annotLayerRef.current.clearLayers();

    annotations.forEach(ann => {
      const isSelected = ann.id === selectedId;
      const color = _colorForLabel(ann.label);

      try {
        if (isSelected) {
          // ── 选中标注：直接创建可编辑多边形 + 顶点标记 ──
          const coords = _extractPolyCoords(ann.geometry);
          if (!coords || coords.length < 3) return;

          const polyLayer = L.polygon(coords, {
            color: '#fff', weight: 3,
            fillColor: color, fillOpacity: 0.5,
          }).addTo(annotLayerRef.current);

          // 创建可拖拽顶点（L.marker + divIcon，原生支持 draggable）
          const vertexIcon = L.divIcon({
            className: 'vertex-edit-marker',
            html: '<div style="width:14px;height:14px;border-radius:50%;background:#f59e0b;border:2px solid #fff;cursor:grab;pointer-events:auto;"></div>',
            iconSize: [14, 14],
            iconAnchor: [7, 7],
          });

          const markers = coords.map((c, i) => {
            const marker = L.marker(c, {
              icon: vertexIcon,
              draggable: true,
              pmIgnore: true,
              keyboard: false,
            });
            marker._vertexIndex = i;
            marker._polyLayer = polyLayer;

            marker.on('dragstart', () => {
              marker._icon.style.cursor = 'grabbing';
            });

            marker.on('drag', (e) => {
              // 拖动时实时更新多边形形状
              const newLatLngs = [...polyLayer.getLatLngs()[0]];
              newLatLngs[i] = e.latlng;
              polyLayer.setLatLngs([newLatLngs]);
            });

            marker.on('dragend', () => {
              marker._icon.style.cursor = 'grab';
              // 保存几何到标注状态
              const newLatLngs = polyLayer.getLatLngs()[0];
              const newCoords = newLatLngs.map(ll => [ll.lng, ll.lat]);
              if (newCoords.length > 0) {
                newCoords.push([...newCoords[0]]);
              }
              updateAnnotation(ann.id, {
                geometry: { type: 'Polygon', coordinates: [newCoords] },
              });
            });

            marker.addTo(map);
            return marker;
          });

          editMarkersRef.current = markers;

          // tooltip
          const area = _estimateArea(ann.geometry);
          polyLayer.bindTooltip(`${ann.label}${area ? ` (${area})` : ''}`, { sticky: true });
          // 点击已选中的多边形 → 完成编辑（比 dblclick 更可靠）
          polyLayer.on('click', () => setSelectedId(null));

        } else {
          // ── 普通标注：静态 GeoJSON ──
          const layer = L.geoJSON(ann.geometry, {
            style: {
              color: color, weight: 2,
              fillColor: color, fillOpacity: 0.25,
            },
            onEachFeature: (_, l) => {
              l.on('click', () => setSelectedId(ann.id));
              const area = _estimateArea(ann.geometry);
              l.bindTooltip(`${ann.label}${area ? ` (${area})` : ''}`, { sticky: true });
            },
          });
          annotLayerRef.current.addLayer(layer);
        }
      } catch (_) {}
    });
  }, [annotations, selectedId, mapManager, updateAnnotation]);

  // ── 保存到后端 ──
  const saveToBackend = async () => {
    try {
      const res = await fetch('/api/annotations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          annotations: annotations.map(a => ({
            ...a,
            session_id: sessionId,
            iteration: a.iteration || 0,
          })),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      alert('标注已保存');
      return true;
    } catch (err) {
      alert(`保存失败: ${err.message}`);
      return false;
    }
  };

  // ── 提交 SSA 训练 ──
  const handleSubmitTrain = async () => {
    if (annotations.length === 0) {
      alert('请先创建标注后再训练');
      return;
    }
    // 先保存标注（后端训练时需要从数据库读取）
    const saved = await saveToBackend();
    if (!saved) return;

    try {
      const res = await fetch('/api/sam-train', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_ids: [sessionId],
          epochs: 20,
          checkpoint_name: `ann_${sessionId.slice(0, 12)}`,
          base_checkpoint: baseCheckpoint || '',
          training_method: 'resam',
          lora_rank: 4,
        }),
      });
      if (!res.ok) throw new Error((await res.text()).slice(0, 200));
      const data = await res.json();
      setTrainTaskId(data.task_id);
      setTrainStatus({ status: 'running', message: '训练已提交...' });

      // 轮询训练进度
      const poll = setInterval(async () => {
        try {
          const sr = await fetch(`/api/sam-train/${data.task_id}/status`);
          if (!sr.ok) return;
          const status = await sr.json();
          setTrainStatus(status);
          if (status.status === 'complete' || status.status === 'failed') {
            clearInterval(poll);
          }
        } catch {}
      }, 3000);
    } catch (err) {
      alert(`训练提交失败: ${err.message}`);
    }
  };

  // ── 从后端加载 ──
  const loadFromBackend = async () => {
    try {
      const res = await fetch(`/api/annotations?session_id=${sessionId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.annotations) setAnnotations(data.annotations);
    } catch (err) {
      console.error('Load annotations error:', err);
    }
  };

  // ── 导出 ──
  const exportData = async (format) => {
    try {
      const res = await fetch(`/api/annotations/export?session_id=${sessionId}&format=${format}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `annotations_${sessionId}.${format === 'coco' ? 'coco' : 'geo'}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`导出失败: ${err.message}`);
    }
  };

  // Auto-load on mount
  useEffect(() => { loadFromBackend(); }, [sessionId]);

  // 加载模型列表
  const loadCheckpoints = async () => {
    try {
      const res = await fetch('/api/sam-checkpoints');
      if (!res.ok) return;
      const data = await res.json();
      setCheckpoints(data.checkpoints || []);
    } catch {}
  };
  useEffect(() => { loadCheckpoints(); }, []);

  // 删除模型
  const handleDeleteCheckpoint = async (cp) => {
    if (!window.confirm(`确定删除模型 "${cp.name}"？\n删除后不可恢复。`)) return;
    try {
      const res = await fetch(`/api/sam-checkpoints?path=${encodeURIComponent(cp.path)}`, { method: 'DELETE' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '删除失败' }));
        alert(err.detail || '删除失败');
        return;
      }
      loadCheckpoints();
    } catch (e) { alert(`删除失败: ${e.message}`); }
  };

  // 重命名模型
  const handleRenameCheckpoint = async (cp) => {
    if (!renameValue.trim()) return;
    try {
      const res = await fetch('/api/sam-checkpoints/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: cp.path, new_name: renameValue.trim() }),
      });
      if (!res.ok) {
        alert('重命名失败');
        return;
      }
      setRenamingId(null);
      setRenameValue('');
      loadCheckpoints();
    } catch (e) { alert(`重命名失败: ${e.message}`); }
  };

  // 接收 SAM 预标注：切换 session 并自动加载
  useEffect(() => {
    const handler = (e) => {
      const { sessionId: newSessionId } = e.detail || {};
      if (newSessionId) {
        console.log('[AnnotationPanel] 收到 SAM 预标注, sessionId:', newSessionId);
        setSessionId(newSessionId);
      }
    };
    window.addEventListener('sam_annotations_sent', handler);
    return () => window.removeEventListener('sam_annotations_sent', handler);
  }, []);

  const selectedAnn = annotations.find(a => a.id === selectedId);

  return (
    <div style={{ display: 'flex', height: '100%', width: '100%', overflow: 'hidden' }}>
      {/* ── 左侧工具面板 ── */}
      <div style={{
        width: 260, minWidth: 260, background: '#f8fafc', borderRight: '1px solid #e2e8f0',
        padding: 12, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8,
      }}>
        <div style={{ fontWeight: 700, fontSize: 14, color: '#1e293b' }}>标注工具箱</div>

        {/* 工具按钮 */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
          {[
            ['select', '选择 V', '#64748b'],
            ['rect', '矩形 B', '#0ea5e9'],
            ['poly', '多边形 P', '#f59e0b'],
            ['sam_point', '正样本 1', '#10b981'],
            ['sam_neg', '负样本 2', '#ef4444'],
          ].map(([tool, label, color]) => (
            <button key={tool} onClick={() => setActiveTool(tool)}
              style={{
                padding: '8px 4px', border: activeTool === tool ? `2px solid ${color}` : '1px solid #e2e8f0',
                borderRadius: 6, background: activeTool === tool ? `${color}15` : '#fff',
                color: activeTool === tool ? color : '#64748b', cursor: 'pointer',
                fontSize: 11, fontWeight: 600, transition: 'all 0.1s',
              }}>
              {label}
            </button>
          ))}
        </div>

        {/* SAM 预标注 */}
        <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 8 }}>
          <div style={{ fontSize: 11, color: '#64748b', marginBottom: 4, fontWeight: 500 }}>SAM 预标注</div>

          {/* 模式切换 Tab */}
          <div style={{ display: 'flex', marginBottom: 6, borderRadius: 6, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
            <button onClick={() => { setSamMode('text'); setActiveTool('select'); setRegionBounds(null); clearDrawings(); }} style={{
              flex: 1, padding: '5px 0', border: 'none', fontSize: 11, fontWeight: 600, cursor: 'pointer',
              background: samMode === 'text' ? '#0ea5e9' : '#f8fafc',
              color: samMode === 'text' ? '#fff' : '#64748b',
            }}>文字提示</button>
            <button onClick={() => { setSamMode('point'); setActiveTool('select'); setRegionBounds(null); clearDrawings(); }} style={{
              flex: 1, padding: '5px 0', border: 'none', fontSize: 11, fontWeight: 600, cursor: 'pointer',
              background: samMode === 'point' ? '#0ea5e9' : '#f8fafc',
              color: samMode === 'point' ? '#fff' : '#64748b',
            }}>点击提示</button>
          </div>

          {/* 文字模式 */}
          {samMode === 'text' && (<>
          <input
            placeholder="目标提示词，如：建筑"
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            style={{
              width: '100%', boxSizing: 'border-box', padding: '8px 10px',
              border: '1px solid #e2e8f0', borderRadius: 6, fontSize: 12, outline: 'none',
              marginBottom: 6,
            }}
          />
          <button onClick={runSamPreAnnotate} disabled={isSamRunning}
            style={{
              width: '100%', padding: '8px 0', border: 'none', borderRadius: 6,
              background: isSamRunning ? '#e2e8f0' : 'linear-gradient(135deg, #0ea5e9, #06b6d4)',
              color: isSamRunning ? '#94a3b8' : '#fff', cursor: isSamRunning ? 'not-allowed' : 'pointer',
              fontWeight: 600, fontSize: 12,
            }}>
            {isSamRunning ? '识别中...' : '▶ SAM 预标注'}
          </button>
          </>)}

          {/* 点击模式 */}
          {samMode === 'point' && (<>
          <input
            placeholder="目标提示词，如：建筑"
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            style={{
              width: '100%', boxSizing: 'border-box', padding: '8px 10px',
              border: '1px solid #e2e8f0', borderRadius: 6, fontSize: 12, outline: 'none',
              marginBottom: 6,
            }}
          />
          {/* 绘制搜索范围 */}
          <div style={{ display: 'flex', gap: 4, marginBottom: 6 }}>
            <button onClick={() => {
              hookStartRectDraw((rect) => {
                const b = rect.getBounds();
                setRegionBounds({ west: b.getWest(), east: b.getEast(), south: b.getSouth(), north: b.getNorth() });
                // 矩形绘制完成后恢复地图交互（点模式依赖原生 DOM 事件）
                setTimeout(() => {
                  if (mapManager?.map?._handlers) {
                    mapManager.map._handlers.forEach(h => { try { h.enable(); } catch (_) {} });
                  }
                  mapManager?.map?.dragging?.enable();
                }, 100);
              });
            }}
              style={{
                flex: 1, padding: '6px 0', border: '1px dashed #0ea5e9', borderRadius: 6,
                background: regionBounds ? '#e0f2fe' : '#fff', color: '#0ea5e9',
                cursor: 'pointer', fontWeight: 600, fontSize: 11,
              }}>
              {regionBounds ? '↺ 重绘范围' : '✥ 绘制搜索范围'}
            </button>
            {regionBounds && (
              <button onClick={() => { setRegionBounds(null); clearDrawings(); }}
                style={{
                  padding: '6px 10px', border: '1px solid #fca5a5', borderRadius: 6,
                  background: '#fef2f2', color: '#dc2626', cursor: 'pointer',
                  fontWeight: 600, fontSize: 11,
                }}>✕</button>
            )}
          </div>
          {regionBounds && (
            <div style={{ fontSize: 10, color: '#0ea5e9', marginBottom: 6, lineHeight: 1.3 }}>
              范围: {regionBounds.west.toFixed(5)}°~{regionBounds.east.toFixed(5)}°, {regionBounds.south.toFixed(5)}°~{regionBounds.north.toFixed(5)}°
            </div>
          )}
          <div style={{ fontSize: 11, color: '#475569', marginBottom: 4, lineHeight: 1.4 }}>
            🟢 左键 = 正提示（目标）&nbsp;&nbsp;🔴 右键 = 负提示（非目标）
          </div>
          <div style={{ fontSize: 11, color: '#64748b', marginBottom: 6 }}>
            已标记 {pointPrompts.filter(p => p.label === 1).length} 正 / {pointPrompts.filter(p => p.label === 0).length} 负
          </div>
          <div style={{ display: 'flex', gap: 4 }}>
            <button onClick={runPointPredict} disabled={isSamRunning || pointPrompts.length === 0}
              style={{
                flex: 1, padding: '8px 0', border: 'none', borderRadius: 6,
                background: (isSamRunning || pointPrompts.length === 0) ? '#e2e8f0' : 'linear-gradient(135deg, #10b981, #059669)',
                color: (isSamRunning || pointPrompts.length === 0) ? '#94a3b8' : '#fff',
                cursor: (isSamRunning || pointPrompts.length === 0) ? 'not-allowed' : 'pointer',
                fontWeight: 600, fontSize: 12,
              }}>
              {isSamRunning ? '分割中...' : '✔ 确认分割'}
            </button>
            <button onClick={clearPointPrompts}
              style={{
                padding: '8px 12px', border: 'none', borderRadius: 6,
                background: '#fee2e2', color: '#dc2626', cursor: 'pointer',
                fontWeight: 600, fontSize: 12,
              }}>
              清除
            </button>
          </div>
          </>)}
        </div>

        {/* 操作按钮 */}
        <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <button onClick={saveToBackend} style={btnStyle('#10b981')}>保存标注</button>
          <button onClick={() => exportData('geojson')} style={btnStyle('#3b82f6')}>导出 GeoJSON</button>
          <button onClick={() => exportData('coco')} style={btnStyle('#8b5cf6')}>导出 COCO</button>

          {/* ── 模型选择与管理 ── */}
          <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: 8, marginTop: 4 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <span style={{ fontWeight: 600, fontSize: 11, color: '#1e293b' }}>🧠 基底模型</span>
              <button
                onClick={() => setShowModelManager(!showModelManager)}
                style={{ fontSize: 9, color: '#6366f1', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 600 }}>
                {showModelManager ? '✖ 关闭管理' : '⚙ 模型管理'}
              </button>
            </div>
            <select
              value={baseCheckpoint}
              onChange={e => setBaseCheckpoint(e.target.value)}
              style={{
                width: '100%', padding: '6px 8px', fontSize: 10,
                border: '1px solid #d4d4d8', borderRadius: 4, background: '#fff',
              }}>
              <option value="">从零开始训练（无基底模型）</option>
              {checkpoints.filter(cp => cp.path).map(cp => (
                <option key={cp.path} value={cp.path}>
                  {cp.name}{cp.classes?.length > 0 ? ` [类别: ${cp.classes.join(', ')}]` : ''}
                  {cp.epochs > 0 ? ` (${cp.epochs}轮)` : ''}
                </option>
              ))}
            </select>
            {baseCheckpoint && (
              <div style={{ fontSize: 9, color: '#64748b', marginTop: 2 }}>
                将在此模型基础上继续训练（迁移学习）
              </div>
            )}

            {/* 模型管理面板 */}
            {showModelManager && (
              <div style={{
                marginTop: 6, padding: 8, background: '#f1f5f9', borderRadius: 6,
                border: '1px solid #e2e8f0', maxHeight: 200, overflowY: 'auto',
              }}>
                <div style={{ fontWeight: 600, fontSize: 10, color: '#475569', marginBottom: 6 }}>模型列表</div>
                {checkpoints.filter(cp => cp.path).length === 0 && (
                  <div style={{ fontSize: 10, color: '#94a3b8' }}>暂无训练模型</div>
                )}
                {checkpoints.filter(cp => cp.path).map(cp => (
                  <div key={cp.path} style={{
                    padding: '5px 6px', background: '#fff', borderRadius: 4,
                    border: '1px solid #e2e8f0', marginBottom: 4, fontSize: 10,
                  }}>
                    {renamingId === cp.path ? (
                      <div style={{ display: 'flex', gap: 3 }}>
                        <input
                          value={renameValue}
                          onChange={e => setRenameValue(e.target.value)}
                          onKeyDown={e => e.key === 'Enter' && handleRenameCheckpoint(cp)}
                          style={{ flex: 1, padding: '2px 4px', fontSize: 10, border: '1px solid #c7d2fe', borderRadius: 3 }}
                          autoFocus
                        />
                        <button onClick={() => handleRenameCheckpoint(cp)}
                          style={{ fontSize: 9, padding: '2px 5px', background: '#dbeafe', border: '1px solid #93c5fd', borderRadius: 3, cursor: 'pointer' }}>✓</button>
                        <button onClick={() => { setRenamingId(null); setRenameValue(''); }}
                          style={{ fontSize: 9, padding: '2px 5px', background: '#f1f5f9', border: '1px solid #d4d4d8', borderRadius: 3, cursor: 'pointer' }}>✖</button>
                      </div>
                    ) : (
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontWeight: 600, color: '#1e293b', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {cp.active && '● '}{cp.name}
                          </div>
                          <div style={{ color: '#94a3b8', fontSize: 9 }}>
                            {cp.classes?.join(', ') || '未分类'} | {cp.epochs || 0}轮
                            {cp.annotation_count > 0 && ` | ${cp.annotation_count}条`}
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: 2, marginLeft: 4 }}>
                          <button
                            onClick={() => { setRenamingId(cp.path); setRenameValue(cp.name); }}
                            style={{ fontSize: 9, padding: '1px 4px', background: '#eef2ff', border: '1px solid #c7d2fe', borderRadius: 3, cursor: 'pointer', color: '#4f46e5' }}
                            title="重命名">✏</button>
                          <button
                            onClick={() => handleDeleteCheckpoint(cp)}
                            style={{ fontSize: 9, padding: '1px 4px', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 3, cursor: 'pointer', color: '#dc2626' }}
                            title="删除">✖</button>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* ── SSA 训练按钮 ── */}
          <button
            onClick={handleSubmitTrain}
            disabled={!!trainTaskId || annotations.length === 0}
            style={{
              padding: '10px 0', border: 'none', borderRadius: 6,
              background: (trainTaskId || annotations.length === 0)
                ? '#e2e8f0'
                : 'linear-gradient(135deg, #f59e0b 0%, #f97316 100%)',
              color: (trainTaskId || annotations.length === 0) ? '#94a3b8' : '#fff',
              cursor: (trainTaskId || annotations.length === 0) ? 'not-allowed' : 'pointer',
              fontWeight: 700, fontSize: 12,
              boxShadow: (trainTaskId || annotations.length === 0) ? 'none' : '0 2px 8px rgba(245,158,11,0.3)',
              transition: 'all 0.15s',
            }}
          >
            {trainTaskId
              ? `⏳ 训练中 (${trainStatus?.progress_pct || 0}%)`
              : `🧠 提交训练 (${annotations.length} 条标注)`}
          </button>
          {trainStatus && trainStatus.status !== 'idle' && (
            <div style={{ fontSize: 10, color: '#64748b', padding: '4px 6px', background: '#f8fafc', borderRadius: 4 }}>
              {trainStatus.status === 'running' && <>🔄 {trainStatus.message || '训练中...'}</>}
              {trainStatus.status === 'complete' && <>✅ 训练完成！{trainStatus.output_path ? ` 模型: ${trainStatus.output_path.split('/').pop()}` : ''}</>}
              {trainStatus.status === 'failed' && <>❌ {trainStatus.error || '训练失败'}</>}
            </div>
          )}

          {/* 训练完成后显示训练曲线图 */}
          {trainStatus?.status === 'complete' && trainStatus?.output_path && (() => {
            const fname = trainStatus.output_path.split('/').pop().replace('sam3_ssa_', '').replace('.pt', '');
            const chartUrl = `/api/sam-checkpoints/${fname}/chart`;
            return (
              <div style={{ marginTop: 6 }}>
                <div style={{ fontSize: 10, fontWeight: 600, color: '#166534', marginBottom: 3 }}>📈 训练曲线</div>
                <img
                  src={chartUrl}
                  alt="训练曲线"
                  style={{
                    width: '100%', borderRadius: 6,
                    border: '1px solid #d4d4d8',
                    cursor: 'pointer',
                  }}
                  onClick={() => window.open(chartUrl, '_blank')}
                  onError={e => { e.target.style.display = 'none'; }}
                />
              </div>
            );
          })()}
        </div>

        <div style={{ color: '#94a3b8', fontSize: 10, marginTop: 'auto' }}>
          Ctrl+Z 撤销 | Del 删除 | Esc 取消
        </div>
      </div>

      {/* ── 右侧标注列表 ── */}
      <div style={{
        flex: 1, minWidth: 0, background: '#fff', borderLeft: '1px solid #e2e8f0',
        overflowY: 'auto', display: 'flex', flexDirection: 'column',
      }}>
        <div style={{
          padding: '10px 12px', fontWeight: 700, fontSize: 13, color: '#1e293b',
          borderBottom: '1px solid #e2e8f0', display: 'flex', justifyContent: 'space-between',
        }}>
          <span>标注列表 ({annotations.length})</span>
          <span style={{ fontSize: 10, color: '#94a3b8', fontWeight: 400 }}>{sessionId.slice(0, 12)}...</span>
        </div>

        {annotations.length === 0 && (
          <div style={{ padding: 20, textAlign: 'center', color: '#94a3b8', fontSize: 12 }}>
            暂无标注，使用左侧工具开始标注
          </div>
        )}

        {annotations.map(ann => (
          <div key={ann.id} onClick={() => {
            setSelectedId(ann.id);
            // 点击跳转到该标注位置
            if (mapManager?.map && ann.geometry) {
              try {
                const layer = L.geoJSON(ann.geometry);
                const bounds = layer.getBounds();
                if (bounds.isValid()) {
                  mapManager.map.flyToBounds(bounds, { padding: [60, 60], maxZoom: 19, duration: 0.5 });
                }
              } catch (_) {}
            }
          }}
            style={{
              padding: '8px 12px', borderBottom: '1px solid #f1f5f9',
              background: ann.id === selectedId ? '#f0f9ff' : 'transparent',
              cursor: 'pointer', transition: 'background 0.1s',
            }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{
                fontWeight: 600, fontSize: 12,
                color: _colorForLabel(ann.label),
              }}>
                {ann.label || '未分类'}
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 9, color: '#94a3b8' }}>
                  {ann.source === 'sam_preannotate' ? '🤖 SAM' : '✋ 手动'}
                </span>
                <button onClick={(e) => { e.stopPropagation(); deleteAnnotation(ann.id); }}
                  style={{ padding: '2px 5px', border: '1px solid #fecaca', borderRadius: 3, background: '#fef2f2', color: '#dc2626', cursor: 'pointer', fontSize: 9, lineHeight: 1 }}
                  title="删除此标注">
                  ✖
                </button>
              </div>
            </div>
            <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 2 }}>
              置信度: {ann.confidence != null ? (ann.confidence * 100).toFixed(0) + '%' : '-'}
            </div>
            {ann.id === selectedId && (
              <div style={{ marginTop: 6, display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
                <button onClick={(e) => { e.stopPropagation(); setSelectedId(null); }}
                  style={{ padding: '4px 8px', border: '1px solid #bbf7d0', borderRadius: 4, background: '#f0fdf4', color: '#16a34a', cursor: 'pointer', fontSize: 10, fontWeight: 600 }}>
                  ✓ 完成编辑
                </button>
                <input
                  list={`labels-${ann.id}`}
                  value={ann.label}
                  onChange={e => {
                    const label = e.target.value;
                    updateAnnotation(ann.id, { label, class_id: ANNOTATION_LABELS.indexOf(label) });
                  }}
                  onClick={e => e.stopPropagation()}
                  placeholder="修改标签"
                  style={{ flex: 1, padding: '4px 6px', border: '1px solid #e2e8f0', borderRadius: 4, fontSize: 11 }}
                />
                <datalist id={`labels-${ann.id}`}>
                  {LABEL_SUGGESTIONS.map(l => <option key={l} value={l} />)}
                </datalist>
              </div>
            )}
          </div>
        ))}

        {selectedAnn && (
          <div style={{
            marginTop: 'auto', borderTop: '2px solid #e2e8f0', padding: 12,
            background: '#f8fafc',
          }}>
            <div style={{ fontWeight: 600, fontSize: 12, color: '#1e293b', marginBottom: 8 }}>属性编辑</div>
            <div style={{ display: 'grid', gap: 6, fontSize: 11 }}>
              <div>
                <div style={{ color: '#94a3b8' }}>标签</div>
                <input
                  list="labels-editor"
                  value={selectedAnn.label}
                  onChange={e => {
                    const label = e.target.value;
                    updateAnnotation(selectedAnn.id, { label, class_id: ANNOTATION_LABELS.indexOf(label) });
                  }}
                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #e2e8f0', borderRadius: 4, fontSize: 11, boxSizing: 'border-box' }}
                />
                <datalist id="labels-editor">
                  {LABEL_SUGGESTIONS.map(l => <option key={l} value={l} />)}
                </datalist>
              </div>
              <div>
                <div style={{ color: '#94a3b8' }}>置信度</div>
                <input type="range" min="0" max="1" step="0.05"
                  value={selectedAnn.confidence || 1}
                  onChange={e => updateAnnotation(selectedAnn.id, { confidence: parseFloat(e.target.value) })}
                  style={{ width: '100%' }} />
                <span style={{ color: '#64748b' }}>{((selectedAnn.confidence || 1) * 100).toFixed(0)}%</span>
              </div>
              <div>
                <div style={{ color: '#94a3b8' }}>来源</div>
                <select value={selectedAnn.source} onChange={e => updateAnnotation(selectedAnn.id, { source: e.target.value })}
                  style={{ width: '100%', padding: '6px 8px', border: '1px solid #e2e8f0', borderRadius: 4, fontSize: 11 }}>
                  <option value="manual">手动标注</option>
                  <option value="sam_preannotate">SAM 预标注</option>
                  <option value="sam_refine">SAM 精修</option>
                </select>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

// ── 辅助函数 ──
function _extractPolyCoords(geometry) {
  /** 从 GeoJSON geometry 提取 Leaflet [lat, lng] 坐标数组 */
  if (!geometry) return null;
  try {
    let ring = null;
    if (geometry.type === 'Polygon') {
      ring = geometry.coordinates?.[0];
    } else if (geometry.type === 'MultiPolygon') {
      ring = geometry.coordinates?.[0]?.[0];
    }
    if (!ring || ring.length < 3) return null;
    // GeoJSON [lng, lat] → Leaflet [lat, lng]，去闭合点
    const pts = ring.slice(0, -1).map(c => [c[1], c[0]]);
    return pts.length >= 3 ? pts : null;
  } catch (_) {
    return null;
  }
}

function btnStyle(color) {
  return {
    padding: '8px 0', border: 'none', borderRadius: 6,
    background: color, color: '#fff', cursor: 'pointer',
    fontWeight: 600, fontSize: 12, transition: 'opacity 0.15s',
  };
}

function _estimateArea(geometry) {
  try {
    if (!geometry || geometry.type !== 'Polygon') return '';
    const coords = geometry.coordinates?.[0];
    if (!coords || coords.length < 3) return '';
    // 简单球面面积估算（近似）
    const xs = coords.map(p => p[0]);
    const ys = coords.map(p => p[1]);
    const midLat = (Math.min(...ys) + Math.max(...ys)) / 2;
    const mPerDegLat = 111320;
    const mPerDegLng = 111320 * Math.cos(midLat * Math.PI / 180);
    const areaM2 = Math.abs(
      coords.reduce((sum, p, i) => {
        const next = coords[(i + 1) % coords.length];
        return sum + (p[0] * mPerDegLng) * (next[1] * mPerDegLat) - (next[0] * mPerDegLng) * (p[1] * mPerDegLat);
      }, 0) / 2
    );
    if (areaM2 > 1000000) return `${(areaM2 / 1000000).toFixed(1)} km²`;
    return `${(areaM2).toFixed(0)} m²`;
  } catch (_) {
    return '';
  }
}

export default AnnotationPanel;
