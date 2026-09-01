import React, { useState, useEffect, useRef, useCallback } from 'react';
import axios from 'axios';
import ReactECharts from 'echarts-for-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import html2canvas from 'html2canvas';
import './App.css';
import MapComponent from './components/MapComponent';
import CesiumComponent from './components/CesiumComponent';
import KnowledgeBaseManager from './components/KnowledgeBaseManager';
import SAMPanel from './components/SAMPanel';
import AnnotationPanel from './components/AnnotationPanel';
import TileManager from './components/TileManager';
import GisPipeline from './components/GisPipeline';
import RunStatusBar from './components/RunStatusBar';
import ConfirmationCard from './components/ConfirmationCard';
import ParameterFormCard from './components/ParameterFormCard';
import ProgressTimeline from './components/ProgressTimeline';
import useAgentChat from './hooks/useAgentChat';

const API_BASE_URL = ''; // 使用相对路径以启用 CRA 代理或同源部署

// 离线模式的默认建议
const DEFAULT_SUGGESTIONS = [
  '在地图上标记北京天安门广场的位置',
  '统计数据库中各个砂场的平均超深，并以柱状图展示',
  '分析最近一月的采砂量趋势，生成折线图',
  '清除地图上的所有标记',
  '切换到卫星图层',
];

const formatChartNumber = (value) => {
  if (typeof value !== 'number' || Number.isNaN(value)) return value;
  const abs = Math.abs(value);
  if (abs >= 10000) return `${(value / 10000).toFixed(2)}万`;
  if (abs >= 1000) return value.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  if (abs >= 1) return value.toFixed(2).replace(/\.00$/, '');
  return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
};

const CHART_FONT_FAMILY = "'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', 'Source Han Sans SC', 'SimHei', Arial, sans-serif";

const wrapChartText = (text, maxLength) => {
  if (!text || typeof text !== 'string') return text;
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength || normalized.includes('\n')) return normalized;
  const chunks = [];
  for (let i = 0; i < normalized.length; i += maxLength) {
    chunks.push(normalized.slice(i, i + maxLength));
  }
  return chunks.join('\n');
};

const flattenChartText = (text) => {
  if (!text || typeof text !== 'string') return '';
  return text.replace(/\s*\n\s*/g, '').trim();
};

const normalizeChartOption = (rawOption, chartType = 'bar') => {
  if (!rawOption || typeof rawOption !== 'object') return rawOption;

  let option;
  try {
    option = JSON.parse(JSON.stringify(rawOption));
  } catch {
    option = { ...rawOption };
  }

  option.backgroundColor = 'transparent';
  option.animationDuration = option.animationDuration ?? 600;
  option.animationEasing = option.animationEasing || 'cubicOut';
  option.textStyle = {
    color: '#334155',
    fontFamily: CHART_FONT_FAMILY,
    ...(option.textStyle || {}),
  };

  option.tooltip = {
    trigger: option.tooltip?.trigger || (chartType === 'pie' ? 'item' : 'axis'),
    backgroundColor: 'rgba(15, 23, 42, 0.92)',
    borderWidth: 0,
    padding: [10, 12],
    textStyle: {
      color: '#e2e8f0',
      fontSize: 12,
      lineHeight: 18,
      ...(option.tooltip?.textStyle || {}),
    },
    axisPointer: {
      type: 'shadow',
      shadowStyle: { color: 'rgba(59, 130, 246, 0.12)' },
      ...(option.tooltip?.axisPointer || {}),
    },
    ...option.tooltip,
  };

  if (option.title) {
    const rawTitle = option.title;
    const titleText = wrapChartText(rawTitle.text, 18);
    const subtext = wrapChartText(rawTitle.subtext, 30);
    option.title = {
      left: 'center',
      top: 8,
      itemGap: 8,
      textStyle: {
        color: '#0f172a',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 14,
        fontWeight: 700,
        lineHeight: 20,
        width: 300,
        overflow: 'break',
        ...(option.title.textStyle || {}),
      },
      subtextStyle: {
        color: '#64748b',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 12,
        lineHeight: 18,
        width: 320,
        overflow: 'break',
        ...(option.title.subtextStyle || {}),
      },
      ...option.title,
      text: titleText,
      subtext,
    };
  }

  if (option.legend) {
    option.legend = {
      top: 48,
      left: 'center',
      icon: 'roundRect',
      itemWidth: 12,
      itemHeight: 8,
      textStyle: {
        color: '#475569',
        fontFamily: CHART_FONT_FAMILY,
        fontSize: 12,
        ...(option.legend.textStyle || {}),
      },
      ...option.legend,
    };
  }

  if (chartType !== 'pie' && chartType !== 'card') {
    option.grid = {
      left: 56,
      right: 24,
      top: 28,
      bottom: 62,
      containLabel: true,
      ...(option.grid || {}),
    };

    const normalizeAxis = (axis) => {
      if (!axis) return axis;
      const next = {
        axisLine: { show: false, ...(axis.axisLine || {}) },
        axisTick: { show: false, ...(axis.axisTick || {}) },
        axisLabel: {
          color: '#64748b',
          fontSize: 12,
          hideOverlap: true,
          margin: 12,
          ...(axis.axisLabel || {}),
        },
        splitLine: {
          show: axis.type === 'value' || axis.type === 'log',
          lineStyle: {
            color: '#e2e8f0',
            width: 1,
            type: 'solid',
            ...(axis.splitLine?.lineStyle || {}),
          },
          ...(axis.splitLine || {}),
        },
        ...axis,
      };

      if (next.type === 'value' || next.type === 'log') {
        next.axisLabel = {
          ...next.axisLabel,
          formatter: next.axisLabel?.formatter || ((v) => formatChartNumber(v)),
        };
      }
      return next;
    };

    if (Array.isArray(option.xAxis)) option.xAxis = option.xAxis.map(normalizeAxis);
    else option.xAxis = normalizeAxis(option.xAxis);

    if (Array.isArray(option.yAxis)) option.yAxis = option.yAxis.map(normalizeAxis);
    else option.yAxis = normalizeAxis(option.yAxis);
  }

  if (Array.isArray(option.series)) {
    option.series = option.series.map((series) => {
      if (!series || typeof series !== 'object') return series;
      const seriesValues = Array.isArray(series.data)
        ? series.data
            .map((item) => {
              if (typeof item === 'number') return item;
              if (Array.isArray(item)) return Number(item[item.length - 1]);
              if (item && typeof item === 'object') {
                if (typeof item.value === 'number') return item.value;
                if (Array.isArray(item.value)) return Number(item.value[item.value.length - 1]);
              }
              return NaN;
            })
            .filter((v) => typeof v === 'number' && !Number.isNaN(v))
        : [];
      const nextSeries = {
        ...series,
        emphasis: {
          focus: 'series',
          ...(series.emphasis || {}),
        },
      };

      if (series.type === 'bar') {
        nextSeries.barMaxWidth = series.barMaxWidth || 42;
        nextSeries.clip = false;

        const titleText = `${option?.title?.text || ''} ${series.name || ''}`;
        const categories = Array.isArray(option?.xAxis?.data)
          ? option.xAxis.data.map((c) => String(c || ''))
          : [];
        const avgIdx = categories.findIndex((c) => c.includes('平均'));
        const hasTripletMetrics = categories.some((c) => c.includes('最大')) && categories.some((c) => c.includes('最小'));
        const isDepthThresholdChart = /超深|阈值|判定/.test(titleText) && (seriesValues.length === 1 || hasTripletMetrics || avgIdx >= 0);
        const threshold = 2;
        const currentValue = avgIdx >= 0 ? seriesValues[avgIdx] : seriesValues[0];
        const isExceeded = isDepthThresholdChart && typeof currentValue === 'number' && currentValue > threshold;

        nextSeries.itemStyle = {
          borderRadius: [8, 8, 0, 0],
          color: (params) => {
            if (isDepthThresholdChart && typeof params?.value === 'number') {
              const name = String(params?.name || '');
              if (name.includes('平均')) {
                return params.value > threshold ? '#ef4444' : '#22c55e';
              }
              return '#3b82f6';
            }
            const v = params?.value;
            if (typeof v === 'number' && v < 0) return '#f97316';
            return '#3b82f6';
          },
          ...(series.itemStyle || {}),
        };
        nextSeries.label = {
          show: true,
          position: (params) => {
            const v = Array.isArray(params?.value) ? params.value[1] : params?.value;
            return typeof v === 'number' && v < 0 ? 'bottom' : 'top';
          },
          distance: 8,
          color: '#1e293b',
          fontSize: 12,
          formatter: ({ value }) => formatChartNumber(value),
          hideOverlap: true,
          ...(series.label || {}),
        };
        nextSeries.labelLayout = {
          hideOverlap: true,
          moveOverlap: 'shiftY',
          ...(series.labelLayout || {}),
        };

        if (isDepthThresholdChart) {
          nextSeries.markLine = {
            symbol: 'none',
            silent: true,
            label: {
              show: true,
              formatter: `阈值 ${threshold}m`,
              color: '#64748b',
              fontSize: 11,
              backgroundColor: '#f8fafc',
              padding: [2, 6],
              borderRadius: 4,
            },
            lineStyle: {
              color: '#94a3b8',
              type: 'dashed',
              width: 1.5,
            },
            data: [{ yAxis: threshold }],
            ...(series.markLine || {}),
          };

          option.title = {
            ...(option.title || {}),
            subtext: wrapChartText(`判定依据：平均差值 ${formatChartNumber(currentValue)} m ｜ 阈值 ${threshold} m ｜ ${isExceeded ? '超标' : '未超标'}（最大/最小仅作参考）`, 30),
            subtextStyle: {
              color: isExceeded ? '#dc2626' : '#16a34a',
              fontFamily: CHART_FONT_FAMILY,
              fontSize: 12,
              lineHeight: 18,
              fontWeight: 600,
              width: 320,
              overflow: 'break',
              ...((option.title && option.title.subtextStyle) || {}),
            },
          };
        }
      } else if (series.type === 'line') {
        nextSeries.symbol = series.symbol || 'circle';
        nextSeries.symbolSize = series.symbolSize || 8;
        nextSeries.smooth = series.smooth ?? true;
        nextSeries.clip = false;
        nextSeries.lineStyle = {
          width: 3,
          ...(series.lineStyle || {}),
        };
        nextSeries.labelLayout = {
          hideOverlap: true,
          moveOverlap: 'shiftY',
          ...(series.labelLayout || {}),
        };
      } else if (series.type === 'pie') {
        nextSeries.radius = series.radius || ['38%', '66%'];
        nextSeries.center = series.center || ['50%', '58%'];
        nextSeries.label = {
          color: '#334155',
          fontSize: 12,
          formatter: series.label?.formatter || '{b}\n{d}%',
          ...(series.label || {}),
        };
      }
      return nextSeries;
    });
  }

  if (option.title) {
    const headerTitle = flattenChartText(option.title.text);
    const headerSubtext = flattenChartText(option.title.subtext);
    option.__chartHeader = {
      title: headerTitle,
      subtext: headerSubtext,
      subtextColor: option.title.subtextStyle?.color,
    };
    option.title = {
      ...option.title,
      show: false,
      text: '',
      subtext: '',
    };
  }

  if (option.legend) {
    const seriesCount = Array.isArray(option.series) ? option.series.length : 0;
    option.legend = {
      ...option.legend,
      show: seriesCount > 1 && option.legend.show !== false,
      bottom: 4,
      left: 'center',
    };
    delete option.legend.top;
  }

  return option;
};

const generateSessionId = () => {
  const cryptoObj = typeof window !== 'undefined' ? window.crypto : undefined;

  if (cryptoObj?.randomUUID) {
    return cryptoObj.randomUUID();
  }

  if (cryptoObj?.getRandomValues) {
    const bytes = cryptoObj.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  return `session-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
};

function App() {
  const [messages, setMessages] = useState(() => {
    const saved = localStorage.getItem('chat_history');
    return saved ? JSON.parse(saved) : [];
  });

  useEffect(() => {
    localStorage.setItem('chat_history', JSON.stringify(messages));
  }, [messages]);

  // ---- 会话管理 state ----
  const [sessionId, setSessionId] = useState(() => generateSessionId());
  const [showSessionPanel, setShowSessionPanel] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');

  // 加载会话列表
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) setSessions(await res.json());
    } catch (e) {
      console.error('Load sessions failed:', e);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  // 展开/收起时加载
  useEffect(() => {
    if (showSessionPanel) loadSessions();
  }, [showSessionPanel, loadSessions]);

  // 新对话（同时在后端创建会话）
  const clearChat = async () => {
    if (!window.confirm('确定要开启新对话吗？')) return;
    try {
      const res = await fetch('/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: '新对话' }) });
      if (res.ok) {
        const data = await res.json();
        setSessionId(data.id);
      } else {
        setSessionId(generateSessionId());
      }
    } catch {
      setSessionId(generateSessionId());
    }
    setMessages([]);
    localStorage.removeItem('chat_history');
    if (showSessionPanel) loadSessions();
  };

  // 切换到某个历史会话
  const switchSession = async (sid) => {
    try {
      const res = await fetch(`/api/sessions/${sid}/messages`);
      if (!res.ok) return;
      const hist = await res.json();
      setMessages(hist.map(m => ({ role: m.role, content: m.content })));
      setSessionId(sid);
      setShowSessionPanel(false);
    } catch (e) {
      console.error('Switch session failed:', e);
    }
  };

  // 删除会话
  const deleteSession = async (e, sid) => {
    e.stopPropagation();
    if (!window.confirm('确定删除该会话及所有消息？')) return;
    try {
      await fetch(`/api/sessions/${sid}`, { method: 'DELETE' });
      setSessions(prev => prev.filter(s => s.id !== sid));
      if (sid === sessionId) {
        setMessages([]);
        setSessionId(generateSessionId());
      }
    } catch (e) {
      console.error('Delete session failed:', e);
    }
  };

  // 开始重命名
  const startRename = (e, s) => {
    e.stopPropagation();
    setEditingSessionId(s.id);
    setEditingTitle(s.title);
  };

  // 提交重命名
  const commitRename = async (sid) => {
    if (!editingTitle.trim()) { setEditingSessionId(null); return; }
    try {
      await fetch(`/api/sessions/${sid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: editingTitle.trim() }),
      });
      setSessions(prev => prev.map(s => s.id === sid ? { ...s, title: editingTitle.trim() } : s));
    } catch (e) {
      console.error('Rename session failed:', e);
    }
    setEditingSessionId(null);
  };

  // 格式化时间
  const formatTime = (iso) => {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now - d;
    if (diff < 86400000 && now.getDate() === d.getDate()) return '今天';
    if (diff < 172800000) return '昨天';
    return `${d.getMonth() + 1}/${d.getDate()}`;
  };

  const getChartMessageKey = useCallback((message) => {
    if (!message) return null;

    try {
      const raw = typeof message.content === 'string' ? JSON.parse(message.content) : message.content;
      if (!raw?.config) return null;
      const title = raw.config?.title?.text || raw.content || raw.formatted_answer || '';
      return `${raw.chart_type || 'chart'}_${title}_${JSON.stringify(raw.config)}`;
    } catch {
      return null;
    }
  }, []);

  const buildChartMessages = useCallback((charts = []) => {
    if (!Array.isArray(charts)) return [];

    return charts
      .filter(chart => chart && chart.config)
      .map((chart, index) => ({
        role: 'function',
        name: 'data_visualizer_tool',
        content: JSON.stringify({
          success: true,
          chart_type: chart.chart_type || 'bar',
          config: chart.config,
          content: chart.summary || '',
          __chart_id: `${chart.chart_type || 'chart'}_${chart.config?.title?.text || index}`,
        }),
      }));
  }, []);

  const getMessageKey = useCallback((message) => {
    const chartKey = getChartMessageKey(message);
    if (chartKey) return `chart_${chartKey}`;
    // 携带唯一 mid 的消息不参与内容去重（同一句回复在不同轮次合法重复）
    if (message?.mid) return `${message.role || ''}_${message.mid}`;
    return `${message?.role || ''}_${message?.name || ''}_${message?.content || ''}`;
  }, [getChartMessageKey]);

  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState(DEFAULT_SUGGESTIONS);
  const [mapManager, setMapManager] = useState(null);     // 主 2D 地图
  const [samMapManager, setSamMapManager] = useState(null);     // SAM 模块的地图
  const [annotateMapManager, setAnnotateMapManager] = useState(null); // 标注模块的地图
  const [isPanelCollapsed, setIsPanelCollapsed] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [activeView, setActiveView] = useState('map'); // 'map' | 'cesium' | 'kb'
  const [isCapturing, setIsCapturing] = useState(false);
  const [lastMapScreenshotPath, setLastMapScreenshotPath] = useState(null); // 最近一次地图截图服务器路径
  // 图片上传相关
  const [selectedFiles, setSelectedFiles] = useState([]); // { file, preview, uploading }
  const [uploadedUrls, setUploadedUrls] = useState([]); // 已上传的图片 URL 列表
  const fileInputRef = useRef(null);
  // SHP 文件上传相关
  const [shpUploadInfo, setShpUploadInfo] = useState(null); // { layer_name, container_path, feature_count, geometry_type }
  const [shpUploading, setShpUploading] = useState(false);
  const shpInputRef = useRef(null);
  const messagesEndRef = useRef(null);
  const progressEndRef = useRef(null);   // 状态卡片列表底部锚点
  const mapWrapperRef = useRef(null);
  const cesiumViewerRef = useRef(null); // 存储 Cesium Viewer 实例
  const pendingCesiumCmds = useRef([]); // 待执行的 Cesium 命令队列（视图切换中暂存）
  useEffect(() => {
    // Load suggestions with error handling
    const loadSuggestions = async () => {
      try {
        const response = await fetch(`/suggestions`);
        if (response.ok) {
          const data = await response.json();
          setSuggestions(data.suggestions);
          setConnectionError(false);
        } else {
          throw new Error(`HTTP ${response.status}`);
        }
      } catch (err) {
        console.warn('Failed to load suggestions from server, using defaults:', err.message);
        setConnectionError(true);
        setSuggestions(DEFAULT_SUGGESTIONS);
      }
    };

    loadSuggestions();
  }, []);

  // 切换回 2D 地图时，通知 Leaflet 重新计算尺寸（display:none → block 后容器尺寸从 0 恢复）
  useEffect(() => {
    if (activeView === 'map' && mapManager?.map) {
      const timer = setTimeout(() => {
        mapManager.map.invalidateSize();
      }, 100); // 等 CSS display 切换完成后再重算
      return () => clearTimeout(timer);
    }
  }, [activeView, mapManager]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleMapReady = useCallback((mapManagerInstance) => {
    setMapManager(mapManagerInstance);
    console.log('地图已初始化');
  }, []);

  const handleCesiumReady = useCallback((viewer) => {
    cesiumViewerRef.current = viewer;
    console.log('[App] Cesium Viewer 已初始化');
    // 消费切换视图时暂存的待执行命令
    if (pendingCesiumCmds.current.length > 0) {
      console.log('[App] 执行暂存的 Cesium 命令:', pendingCesiumCmds.current.length, '条');
      setTimeout(() => {
        pendingCesiumCmds.current.forEach(cmd => {
          window.dispatchEvent(new CustomEvent('cesium_execute_command', { detail: cmd }));
        });
        pendingCesiumCmds.current = [];
      }, 800); // 等待 Viewer 完全初始化再执行
    }
  }, []);

  const handleLayerStatus = useCallback((payload) => {
    const { status, layerName, message, featureCount } = payload || {};
    if (!status || !layerName) return;
    let content = '';
    if (status === 'empty') {
      const detail = message ? `（${message}）` : '';
      content = `图层“${layerName}”无可用要素，未更新视图${detail}。`;
    } else if (status === 'error') {
      content = `图层“${layerName}”加载失败：${message || '未知错误'}。`;
    }
    if (!content) return;
    setMessages(prev => [...prev, { role: 'assistant', content }]);
  }, []);

  // 执行地图命令 - 已修复：支持 load_vector_layer
  const executeMapCommands = useCallback((commands) => {
    if (!mapManager || !commands || commands.length === 0) {
      return;
    }

    commands.forEach(command => {
      if (!command || !command.type) {
        console.warn('Invalid or missing command type:', command);
        return;
      }

      try {
        switch (command.type) {
          case 'add_marker':
            const { lat, lng, title, popup } = command;
            if (typeof lat === 'undefined' || typeof lng === 'undefined') {
              console.warn('Missing coordinates for add_marker command:', command);
              break;
            }
            if (popup) {
              mapManager.addMarkerWithPopup(lat, lng, popup, { title });
            } else {
              mapManager.addMarker(lat, lng, { title });
            }
            break;

          case 'set_view':
            const { lat: viewLat, lng: viewLng, zoom } = command;
            if (typeof viewLat === 'undefined' || typeof viewLng === 'undefined') {
              console.warn('Missing coordinates for set_view command:', command);
              break;
            }
            mapManager.flyTo(viewLat, viewLng, zoom);
            break;

          case 'clear_markers':
            mapManager.clearMarkers();
            break;

          case 'switch_layer':
            if (command.layer) {
              // 注意：MapManager 中没有 switchLayer 方法，应直接操作
              // 但根据 MapManager 实现，它通过 executeMapCommand 内部处理 switch_layer
              // 所以这里应调用 executeMapCommand（但当前设计是 App 调用 MapManager 方法）
              // 为保持一致性，我们假设 MapManager 有 switchLayer 方法，或直接复用逻辑
              // 实际上，更合理的做法是让 MapManager 暴露 switchLayer，但为快速修复，我们模拟调用
              mapManager.executeMapCommand(command); // ✅ 委托给 MapManager 处理
            } else {
              console.warn('Missing layer for switch_layer command:', command);
            }
            break;

          case 'add_circle':
            const { lat: circleLat, lng: circleLng, radius, color } = command;
            if (typeof circleLat === 'undefined' || typeof circleLng === 'undefined' || typeof radius === 'undefined') {
              console.warn('Missing required data for add_circle command:', command);
              break;
            }
            mapManager.addCircle?.(circleLat, circleLng, radius, {
              color: color || 'blue',
              fillColor: color || 'blue',
              fillOpacity: 0.2
            });
            break;

          case 'add_circles':
            const { circles } = command;
            if (circles && Array.isArray(circles)) {
              mapManager.addCircles?.(circles);
            } else {
              console.warn('Missing or invalid circles data for add_circles command:', command);
            }
            break;

          case 'add_polygon':
            const { coordinates, color: polygonColor } = command;
            if (!coordinates || !Array.isArray(coordinates)) {
              console.warn('Missing or invalid coordinates for add_polygon command:', command);
              break;
            }
            const polygonCoords = coordinates.map(coord => [coord[1], coord[0]]);
            mapManager.addPolygon?.(polygonCoords, {
              color: polygonColor || 'red',
              fillColor: polygonColor || 'red',
              fillOpacity: 0.2
            });
            break;

          case 'add_polyline':
            const { coordinates: lineCoords, color: lineColor, weight } = command;
            if (!lineCoords || !Array.isArray(lineCoords)) {
              console.warn('Missing or invalid coordinates for add_polyline command:', command);
              break;
            }
            const leafletCoords = lineCoords.map(coord => [coord[1], coord[0]]);
            mapManager.addPolyline?.(leafletCoords, {
              color: lineColor || 'red',
              weight: weight || 3,
              opacity: 0.8
            });
            break;

          case 'fit_markers':
            mapManager.fitMarkers();
            break;

          // ✅ 新增：加载矢量图层
          case 'load_vector_layer':
            if (command.url && command.name) {
              mapManager.addVectorLayerFromAPI(command.url, command.name, {
                style: command.style,
                view: command.view,
              });
            } else {
              console.warn('Missing url or name for load_vector_layer command:', command);
            }
            break;

          default:
            console.warn('未知的地图命令类型:', command.type, command);
        }
      } catch (error) {
        console.error('执行地图命令失败:', error, command);
      }
    });
  }, [mapManager]);

  // 裁剪 Canvas 空白边缘（基于背景色检测，去除四周多余空白区域）
  const _trimCanvasWhitespace = (sourceCanvas) => {
    const ctx = sourceCanvas.getContext('2d');
    const w = sourceCanvas.width;
    const h = sourceCanvas.height;
    if (w === 0 || h === 0) return sourceCanvas;
    
    const imageData = ctx.getImageData(0, 0, w, h);
    const data = imageData.data; // RGBA

    // 目标背景色（#f0f2f5 的 RGB）
    const bgR = 240, bgG = 242, bgB = 245;
    const threshold = 25; // 颜色容差

    const isBg = (idx) => {
      const dr = Math.abs(data[idx] - bgR);
      const dg = Math.abs(data[idx + 1] - bgG);
      const db = Math.abs(data[idx + 2] - bgB);
      return dr < threshold && dg < threshold && db < threshold;
    };

    let top = h, bottom = 0, left = w, right = 0;

    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const idx = (y * w + x) * 4;
        if (!isBg(idx)) {
          if (y < top) top = y;
          if (y > bottom) bottom = y;
          if (x < left) left = x;
          if (x > right) right = x;
        }
      }
    }

    // 如果全是背景色或几乎无内容，返回原 canvas
    if (top >= bottom || left >= right) {
      console.warn('[Screenshot] 裁剪检测：画布几乎全为背景色，跳过裁剪');
      return sourceCanvas;
    }

    // 添加小边距避免太紧
    const margin = 8;
    const cropW = right - left + margin * 2;
    const cropH = bottom - top + margin * 2;
    const cropX = Math.max(0, left - margin);
    const cropY = Math.max(0, top - margin);

    console.log(`[Screenshot] 裁剪: 原${w}x${h} → ${cropW}x${cropH} (节省 ${(1-cropW*cropH/(w*h)*100).toFixed(0)}%面积)`);

    const trimmed = document.createElement('canvas');
    trimmed.width = cropW;
    trimmed.height = cropH;
    const tCtx = trimmed.getContext('2d');
    tCtx.drawImage(sourceCanvas, cropX, cropY, cropW, cropH, 0, 0, cropW, cropH);
    return trimmed;
  };

  // 截取地图并保存到服务器，返回服务器端路径
  const captureMapToServer = async (filePrefix = 'report_map', options = {}) => {
    const target = mapWrapperRef.current;
    if (!target) return null;
    try {
      // === 修复1: 截图前紧贴数据范围（去除左右空白）+ 放大两级 ===
      if (mapManager && options.zoomIn !== false) {
        const mapObj = mapManager.map;
        if (mapObj) {
          // 先用 fitBounds 紧贴当前所有图层数据，padding 从默认0.1减到0.02
          const allLayers = mapObj._layers || {};
          let hasVisibleData = false;
          // 检查 overlay layers 是否有数据
          try {
            if (mapManager.overlays) {
              Object.values(mapManager.overlays).forEach(og => {
                if (og && og.getLayers && og.getLayers().length > 0) {
                  hasVisibleData = true;
                }
              });
            }
            // 也检查 markers
            if (mapManager.markers && mapManager.markers.length > 0) {
              hasVisibleData = true;
            }
          } catch (_) {}

          if (hasVisibleData) {
            // 用更小的 padding 紧贴数据
            try { mapObj.fitBounds(mapObj.getBounds(), { padding: [20, 20], maxZoom: 18 }); } catch (_) {}
          }

          // 再放大两级（原+1不够）
          const currentZoom = mapObj.getZoom();
          if (currentZoom < 18) {
            const newZoom = Math.min(Math.round(currentZoom) + 2, 18);
            mapObj.setZoom(newZoom);
          }
        }
      }

      // === 修复2: 等待底图瓦片加载完成 + 放大后的渲染 ===
      const waitForTiles = options.tileWaitMs || 4000;
      await new Promise(resolve => setTimeout(resolve, waitForTiles));

      let canvas = await html2canvas(target, {
        useCORS: true,
        allowTaint: true,
        logging: false,
        backgroundColor: '#f0f2f5',
        scale: 2,
        imageTimeout: 15000,
        onclone: (clonedDoc) => {
          const tiles = clonedDoc.querySelectorAll('.leaflet-tile-container img, .leaflet-layer img');
          tiles.forEach(img => {
            img.style.display = 'block';
            img.style.visibility = 'visible';
            if (!img.crossOrigin) {
              img.crossOrigin = 'anonymous';
            }
          });
        },
      });

      // === 修复3: 裁剪画布空白边缘（去除左右/上下多余空白）===
      canvas = _trimCanvasWhitespace(canvas);
      const dataUrl = canvas.toDataURL('image/png');
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const res = await fetch('/api/save-screenshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_data: dataUrl,
          file_name: `${filePrefix}_${timestamp}.png`,
        }),
      });
      if (!res.ok) return null;
      const data = await res.json();
      const serverPath = data.file_path || null;
      if (serverPath) setLastMapScreenshotPath(serverPath);
      return serverPath;
    } catch (e) {
      console.warn('地图截图保存失败:', e);
      return null;
    }
  };

  const applyServerResponse = useCallback((data) => {
    const responseMessages = Array.isArray(data.messages) ? data.messages : [];
    const normalizedMessages = [...responseMessages];

    if (data.response && !normalizedMessages.some(m => m.role === 'assistant' && m.content === data.response)) {
      // mid: 最终回复每轮唯一，防止被历史同文消息去重吞掉
      normalizedMessages.push({ role: 'assistant', content: data.response, mid: `final-${Date.now()}` });
    }

    if (data.report_url) {
      const filename = data.report_url.split('/').pop();
      console.log('[applyServerResponse] 收到报告下载链接:', data.report_url, '文件名:', filename);
      normalizedMessages.push({
        role: 'assistant',
        content: `[报告已生成，点击下载](${data.report_url})`,
        report_url: data.report_url,
        report_filename: filename,
      });
    } else {
      console.warn('[applyServerResponse] report_url 为空，不会显示下载卡片');
    }

    const existingChartKeys = new Set(responseMessages.map(getChartMessageKey).filter(Boolean));
    const chartMessages = buildChartMessages(data.charts).filter(chartMessage => {
      const chartKey = getChartMessageKey(chartMessage);
      return chartKey && !existingChartKeys.has(chartKey);
    });
    normalizedMessages.push(...chartMessages);

    const dedupedNormalizedMessages = [];
    const normalizedSeen = new Set();
    normalizedMessages.forEach(message => {
      const key = getMessageKey(message);
      if (normalizedSeen.has(key)) return;
      normalizedSeen.add(key);
      dedupedNormalizedMessages.push(message);
    });

    if (dedupedNormalizedMessages.length > 0) {
      setMessages(prev => {
        const existingContent = new Set(prev.map(getMessageKey));
        const newUniqueMessages = dedupedNormalizedMessages.filter(
          m => !existingContent.has(getMessageKey(m))
        );
        return [...prev, ...newUniqueMessages];
      });
    }

    if (data.map_commands && data.map_commands.length > 0) {
      console.log('执行地图命令:', data.map_commands);
      data.map_commands.forEach((cmd, idx) => {
        console.log(`[MapCmd ${idx}] type=${cmd.type} url=${cmd.url || ''} name=${cmd.name || ''}`);
      });
      executeMapCommands(data.map_commands);
      if (activeView !== 'cesium' && activeView !== 'kb') {
        setTimeout(() => {
          captureMapToServer('auto_map').catch(() => {});
        }, 2500);
      }
    }

    if (data.cesium_commands && data.cesium_commands.length > 0) {
      console.log('[App] 收到 Cesium 命令:', data.cesium_commands);
      if (activeView === 'cesium') {
        data.cesium_commands.forEach(cmd => {
          window.dispatchEvent(new CustomEvent('cesium_execute_command', { detail: cmd }));
        });
      }
    }

    setConnectionError(false);
  }, [activeView, buildChartMessages, captureMapToServer, executeMapCommands, getChartMessageKey, getMessageKey]);

  // ── 阶段5：前端状态机接入（SSE 消费 / 断线重连 / 错误兜底均在 hook 内）──
  const {
    phase: agentPhase,
    runId: agentRunId,
    pendingInfo,
    progress: streamProgress,
    isBusy,
    send: agentSend,
    cancelRun: agentCancelRun,
  } = useAgentChat({
    onFinal: applyServerResponse,
    onAssistantMessage: (text) => {
      setMessages(prev => [...prev, { role: 'assistant', content: text }]);
    },
    onConnectionError: setConnectionError,
  });

  // 消息或 busy 状态变化时滚到底部
  useEffect(() => {
    scrollToBottom();
  }, [messages, isBusy]);

  // 每当新卡片推入，自动滚到卡片列表底部
  useEffect(() => {
    if (isBusy && progressEndRef.current) {
      progressEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [streamProgress, isBusy]);

  // pending 恢复：确认 / 补参（hook 内携带 X-Pending-Run-ID 头 resume）
  const resumePendingRun = useCallback((resumeText) => {
    if (!agentRunId) return;
    const text = (resumeText || '').trim() || '__confirm__';
    const userMessage = { role: 'user', content: text };
    const newMessages = [...messages, userMessage];
    setMessages(newMessages);
    agentSend({
      messages: newMessages,
      activeView,
      sessionId,
      pendingRunId: agentRunId,
    });
  }, [agentRunId, messages, activeView, sessionId, agentSend]);

  // 处理文件选择
  const handleFileSelect = (e) => {
    const files = Array.from(e.target.files);
    if (files.length === 0) return;
    
    const newFiles = files.map(file => ({
      file,
      preview: URL.createObjectURL(file),
      uploading: false,
    }));
    setSelectedFiles(prev => [...prev, ...newFiles]);
    // 重置 input 以便可以再次选择同一文件
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  // 移除已选图片
  const removeSelectedFile = (index) => {
    setSelectedFiles(prev => {
      const updated = [...prev];
      if (updated[index]?.preview) URL.revokeObjectURL(updated[index].preview);
      updated.splice(index, 1);
      return updated;
    });
  };

  // 上传单张图片到后端
  const uploadImage = async (file) => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch('/api/upload_image', {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `上传失败 (${res.status})`);
    }
    const data = await res.json();
    return data.url;
  };

  // 上传 SHP 文件（ZIP 包）到后端
  const uploadShp = async (file) => {
    setShpUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch('/api/upload/shp', {
        method: 'POST',
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `SHP 上传失败 (${res.status})`);
      }
      const data = await res.json();
      setShpUploadInfo(data);
      // 自动在输入框填入提示文本
      const hint = `已上传图层「${data.layer_name}」（${data.feature_count}个${data.geometry_type || '要素'}），`;
      return data;
    } finally {
      setShpUploading(false);
    }
  };

  // 清除已上传的 SHP
  const clearShpUpload = () => {
    setShpUploadInfo(null);
    if (shpInputRef.current) shpInputRef.current.value = '';
  };

  const sendMessage = async (content) => {
    // 允许纯文本、纯图片、纯 SHP 或混合发送
    const hasText = content && content.trim().length > 0;
    const hasImages = selectedFiles.length > 0 || uploadedUrls.length > 0;
    const hasShp = shpUploadInfo !== null;
    if ((!hasText && !hasImages && !hasShp) || isBusy) return;

    // 上传待发送的图片
    const currentFiles = [...selectedFiles];
    let imageUrls = [...uploadedUrls];
    if (currentFiles.length > 0) {
      setSelectedFiles(prev => prev.map(f => ({ ...f, uploading: true })));
      try {
        const uploadPromises = currentFiles
          .filter(f => !f.uploading)
          .map(async (f) => {
            const url = await uploadImage(f.file);
            return url;
          });
        const newUrls = await Promise.all(uploadPromises);
        imageUrls = [...imageUrls, ...newUrls];
        setUploadedUrls(imageUrls);
      } catch (err) {
        console.error('图片上传失败:', err);
        setMessages(prev => [...prev, { role: 'assistant', content: `图片上传失败：${err.message}` }]);
        setSelectedFiles(prev => prev.map(f => ({ ...f, uploading: false })));
        return;
      }
      // 清理已上传文件的预览
      currentFiles.forEach(f => {
        if (f.preview) URL.revokeObjectURL(f.preview);
      });
      setSelectedFiles([]);
    }

    const reportKeywords = [
      '生成报告', '出具报告', '形成文档', '生成分析报告', '导出报告',
      '出报告', '写报告', '做报告', '制作报告', '输出报告', '报告生成',
      '生成文档', '导出文档', '形成报告', '分析报告', '汇总报告',
    ];
    const isReportIntent = reportKeywords.some(k => content.includes(k));

    let mapImageServerPath = null;
    if (isReportIntent && activeView !== 'kb') {
      if (lastMapScreenshotPath) {
        mapImageServerPath = lastMapScreenshotPath;
        console.log('[Report] 使用已保存截图:', mapImageServerPath);
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: '正在截取地图快照，请稍候……' }]);
        mapImageServerPath = await captureMapToServer('report_map');
      }
    }

    const userMessage = { role: 'user', content, images: imageUrls.length > 0 ? imageUrls : undefined };
    const messagesWithCtx = [...messages, userMessage];
    if (mapImageServerPath) {
      messagesWithCtx.push({
        role: 'system',
        content: `[地图截图已保存] 当前地图截图服务器路径为: ${mapImageServerPath}。生成报告时，必须将此路径传入 report_generator_tool 的 map_image_path 参数。`,
      });
    }
    // 注入 SHP 上传信息到消息上下文
    const currentShpInfo = shpUploadInfo;
    if (currentShpInfo) {
      // 将用户消息补充上 SHP 路径说明
      userMessage.content = `${userMessage.content}\n\n[已上传SHP数据] 图层名: ${currentShpInfo.layer_name}, 容器内路径: ${currentShpInfo.container_path}, 要素数: ${currentShpInfo.feature_count}, 几何类型: ${currentShpInfo.geometry_type}。进行空间分析时请使用容器内路径。`;
      setShpUploadInfo(null);  // 发送后清除状态
      if (shpInputRef.current) shpInputRef.current.value = '';
    }
    const newMessages = messagesWithCtx;

    setMessages(prev => {
      const cleaned = isReportIntent
        ? prev.filter(m => m.content !== '正在截取地图快照，请稍候……')
        : prev;
      return [...cleaned, userMessage];
    });
    setInputValue('');
    setUploadedUrls([]);  // 清除已上传的图片 URL（已随消息发送）

    // 流式执行交由 useAgentChat 状态机处理（SSE 消费 / 断线重连 / 错误兜底均在 hook 内）
    await agentSend({
      messages: newMessages,
      activeView,
      sessionId,
    });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    sendMessage(inputValue);
  };

  const handleSuggestionClick = (suggestion) => {
    sendMessage(suggestion);
  };

  const handleScreenshot = async () => {
    if (!mapWrapperRef.current || isCapturing) return;
    setIsCapturing(true);
    try {
      const canvas = await html2canvas(mapWrapperRef.current, {
        useCORS: true,
        backgroundColor: '#f0f2f5',
        scale: window.devicePixelRatio || 1
      });
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const filename = `map_screenshot_${timestamp}.png`;
      const dataUrl = canvas.toDataURL('image/png');
      const link = document.createElement('a');
      link.href = dataUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setMessages(prev => [...prev, { role: 'assistant', content: `截图已保存为 ${filename}` }]);
    } catch (error) {
      console.error('截图失败:', error);
      setMessages(prev => [...prev, { role: 'assistant', content: `截图失败：${error.message}` }]);
    } finally {
      setIsCapturing(false);
    }
  };

  const handleClearMap = () => {
    if (mapManager) {
      mapManager.clearAllLayers();
      setMessages(prev => [...prev, { role: 'assistant', content: '已清除地图上的所有标记和矢量数据。' }]);
    }
  };

  // 预览报告：新标签页打开 HTML 预览
  const handlePreviewReport = (filename) => {
    const previewUrl = `/api/preview/report?filename=${encodeURIComponent(filename)}`;
    window.open(previewUrl, '_blank');
  };

  const renderMessage = (message, index) => {
    const isUser = message.role === 'user';
    const isAssistant = message.role === 'assistant';
    const isFunction = message.role === 'function';

    // 渲染报告预览卡片
    if (isAssistant && message.report_url) {
      const filename = message.report_filename || message.report_url.split('/').pop();
      return (
        <div key={index} className="message assistant">
          <div className="message-content">
            <div className="message-role">豫水地图助手</div>
            <div className="report-download-card">
              <span className="report-icon">📄</span>
              <div className="report-info">
                <div className="report-name">{filename}</div>
                <div className="report-hint">报告已生成</div>
              </div>
              <button
                className="report-download-btn"
                onClick={() => handlePreviewReport(filename)}
              >
                👁 预览报告
              </button>
            </div>
          </div>
        </div>
      );
    }

    // 渲染图表
    if (isFunction && (message.name === 'data_visualizer_tool' || (typeof message.content === 'string' && message.content.includes('"config":')))) {
      try {
        let result;
        if (typeof message.content === 'string') {
          result = JSON.parse(message.content);
        } else {
          result = message.content;
        }

        // 如果工具调用失败（例如回滚后的字段检查失败），显示美化的数值卡片而不是空图表
        if (result && result.success === false) {
          // 尝试从错误信息或原始数据中提取数值进行展示
          const data = result.raw_data || [];
          if (data.length === 1) {
            const entry = data[0];
            const keys = Object.keys(entry);
            const value = entry[keys[0]];
            const label = keys[0];
            
            return (
              <div key={index} className="message chart-message">
                <div className="message-content value-card">
                  <div className="value-card-header">📇 统计结果</div>
                  <div className="value-card-body">
                    <div className="value-label">{label}</div>
                    <div className="value-number">{typeof value === 'number' ? value.toFixed(2) : value}</div>
                  </div>
                  <div className="value-card-footer">注：单一数值不适合生成图表，已为您转化为数据卡片展示。</div>
                </div>
              </div>
            );
          }
          console.log("Chart tool returned failure, skipping render:", result.error);
          return null;
        }

        if (result && result.success && result.chart_type === 'card') {
          const { title, value, unit, label } = result.config;
          return (
            <div key={index} className="message chart-message">
              <div className="message-content value-card">
                <div className="value-card-header">📇 {title || '统计指标'}</div>
                <div className="value-card-body">
                  <div className="value-label">{label}</div>
                  <div className="value-number">
                    {typeof value === 'number' ? value.toFixed(2) : value}
                    {unit && <span style={{ fontSize: '18px', marginLeft: '5px', opacity: 0.8 }}>{unit}</span>}
                  </div>
                </div>
                <div className="value-card-footer">Qwen3 智能数据分析</div>
              </div>
            </div>
          );
        }

        if (result && result.success && result.config && result.chart_type !== 'card') {
          const prettyOption = normalizeChartOption(result.config, result.chart_type);
          const chartHeader = prettyOption.__chartHeader || {};
          delete prettyOption.__chartHeader;
          const emojiMap = {
            'bar': '📊',
            'line': '📈',
            'pie': '🥧',
            'scatter': '🌌'
          };
          const emoji = emojiMap[result.chart_type] || '📊';
          const typeNameMap = {
            'bar': '柱状图',
            'line': '折线图',
            'pie': '饼图',
            'scatter': '散点图'
          };
          const typeName = typeNameMap[result.chart_type] || result.chart_type || '图表';

          return (
            <div key={index} className="message chart-message">
              <div className="message-content">
                <div className="message-role">{emoji} 统计{typeName}</div>
                {(chartHeader.title || chartHeader.subtext) && (
                  <div className="chart-header">
                    {chartHeader.title && <div className="chart-title">{chartHeader.title}</div>}
                    {chartHeader.subtext && (
                      <div className="chart-subtitle" style={{ color: chartHeader.subtextColor || undefined }}>
                        {chartHeader.subtext}
                      </div>
                    )}
                  </div>
                )}
                <div className="chart-container" style={{ height: '300px', width: '100%' }}>
                  <ReactECharts 
                    option={prettyOption}
                    style={{ height: '100%', width: '100%' }}
                    notMerge={true}
                    lazyUpdate={true}
                  />
                </div>
              </div>
            </div>
          );
        }
      } catch (e) {
        console.error("Failed to parse chart message:", e);
      }
      
      // 对于 data_visualizer_tool，如果没成功生成配置，就直接不显示，防止弹出空框
      if (message.name === 'data_visualizer_tool') {
        return null;
      }
    }

    // 过滤掉没有内容的消息（如工具调用过程中的中间消息），但保留用户消息
    if (!isUser && (!message.content || !message.content.trim())) {
      return null;
    }

    if (!isUser && !isAssistant) return null;

    return (
      <div key={index} className={`message ${isUser ? 'user' : 'assistant'}`}>
        <div className="message-content">
          <div className="message-role">{isUser ? '用户' : '豫水地图助手'}</div>
          {/* 用户消息中的图片展示 */}
          {isUser && message.images && message.images.length > 0 && (
            <div className="message-images">
              {message.images.map((imgUrl, imgIdx) => (
                <img
                  key={imgIdx}
                  src={imgUrl}
                  alt={`上传图片 ${imgIdx + 1}`}
                  className="message-image"
                  onClick={() => window.open(imgUrl, '_blank')}
                  style={{ cursor: 'pointer' }}
                />
              ))}
            </div>
          )}
          <div className="message-text markdown-body">
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({node, ...props}) => {
                  // 报告链接→新标签页预览
                  const href = props.href || '';
                  if (href.startsWith('/static/reports/') || href.startsWith('/api/download/report/')) {
                    const filename = href.split('/').pop();
                    const previewUrl = `/api/preview/report?filename=${encodeURIComponent(filename)}`;
                    return (
                      <a
                        href={previewUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="download-link"
                        style={{ cursor: 'pointer' }}
                        onClick={(e) => {
                          e.preventDefault();
                          handlePreviewReport(filename);
                        }}
                      >
                        📄 {props.children || '预览报告'}
                      </a>
                    );
                  }
                  return <a {...props} target="_blank" rel="noopener noreferrer" />;
                },
                // 表格加横向滚动容器，防止超出气泡宽度
                table: ({node, ...props}) => (
                  <div style={{ overflowX: 'auto', width: '100%', marginBottom: 4 }}>
                    <table {...props} />
                  </div>
                ),
              }}
            >
              {message.content}
            </ReactMarkdown>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="App">
      <div className="top-header">
        <div className="header-content">
          <div className="header-left">
            <div className="header-logo">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21"/>
                <line x1="9" y1="3" x2="9" y2="18"/>
                <line x1="15" y1="6" x2="15" y2="21"/>
              </svg>
            </div>
            <h1 className="app-title">豫水智能一张图</h1>
            <div className="header-subtitle">豫水地图助手</div>
          </div>
          <div className="header-nav">
            <button 
              className={`nav-button ${activeView === 'map' ? 'active' : ''}`}
              onClick={() => setActiveView('map')}
            >
              🗺️ 2D 地图
            </button>
            <button 
              className={`nav-button ${activeView === 'cesium' ? 'active' : ''}`}
              onClick={() => setActiveView('cesium')}
            >
              🌐 3D 视图
            </button>
            <button 
              className={`nav-button ${activeView === 'kb' ? 'active' : ''}`}
              onClick={() => setActiveView('kb')}
            >
              📚 知识库管理
            </button>
            <button 
              className={`nav-button ${activeView === 'sam' ? 'active' : ''}`}
              onClick={() => setActiveView('sam')}
            >
              🎯 SAM识别
            </button>
            <button 
              className={`nav-button ${activeView === 'annotate' ? 'active' : ''}`}
              onClick={() => setActiveView('annotate')}
            >
              ✏️ 标注
            </button>
            <button 
              className={`nav-button ${activeView === 'gis' ? 'active' : ''}`}
              onClick={() => setActiveView('gis')}
            >
              🛠 GIS工具
            </button>
            <button 
              className={`nav-button ${activeView === 'tiles' ? 'active' : ''}`}
              onClick={() => setActiveView('tiles')}
            >
              🗂️ 切片管理
            </button>
          </div>
        </div>
      </div>

      <div className={`main-content ${activeView === 'sam' ? 'sam-page-mode' : ''}`}>
        <div className="view-container">
          {/* 2D 地图 — CSS 显隐，切换不销毁 */}
          <div className="map-wrapper" ref={mapWrapperRef} style={{ display: activeView === 'map' ? 'block' : 'none' }}>
              <MapComponent onMapReady={handleMapReady} onLayerStatus={handleLayerStatus} />
              <div className="map-toolbar">
                <button
                  className="map-tool-button"
                  onClick={handleScreenshot}
                  disabled={isCapturing}
                  title="截图保存"
                >
                  {isCapturing ? '正在截图...' : '📸 截图'}
                </button>
                <button
                  className="map-tool-button"
                  onClick={handleClearMap}
                  title="清除地图"
                >
                  🗑️ 清除
                </button>
              </div>
            </div>
          {/* 3D 视图 — CSS 显隐，切换不销毁场景 */}
          <div className="map-wrapper cesium-wrapper" style={{ display: activeView === 'cesium' ? 'block' : 'none' }}>
              <CesiumComponent onViewerReady={handleCesiumReady} visible={activeView === 'cesium'} />
            </div>
          {/* 知识库管理 — CSS 显隐 */}
          <div style={{ display: activeView === 'kb' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <KnowledgeBaseManager />
          </div>
          {/* SAM识别 — CSS 显隐，识别结果不丢失 */}
          <div className="sam-workspace" style={{ display: activeView === 'sam' ? 'flex' : 'none' }}>
            <aside className="sam-workspace-sidebar">
              <SAMPanel mapManager={samMapManager} />
            </aside>
            <section className="sam-workspace-map">
              <MapComponent onMapReady={setSamMapManager} onLayerStatus={handleLayerStatus} visible={activeView === 'sam'} />
            </section>
          </div>
          {/* 标注 — CSS 显隐，标注数据不丢失 */}
          <div className="annotate-workspace" style={{ display: activeView === 'annotate' ? 'flex' : 'none' }}>
            <aside className="annotate-workspace-sidebar">
              <AnnotationPanel mapManager={annotateMapManager} />
            </aside>
            <section className="annotate-workspace-map">
              <MapComponent onMapReady={setAnnotateMapManager} onLayerStatus={handleLayerStatus} visible={activeView === 'annotate'} />
            </section>
          </div>
          {/* 切片管理 — CSS 显隐 */}
          <div style={{ display: activeView === 'tiles' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <TileManager onSwitchTo3D={() => setActiveView('cesium')} />
          </div>
          {/* GIS工具 — CSS 显隐 */}
          <div style={{ display: activeView === 'gis' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <GisPipeline />
          </div>
        </div>

        <div className={`chat-panel ${isPanelCollapsed ? 'collapsed' : ''}`}>
        <div className="panel-header">
          <button 
            className="collapse-button"
            onClick={() => setIsPanelCollapsed(!isPanelCollapsed)}
            title={isPanelCollapsed ? '展开面板' : '收起面板'}
          >
            {isPanelCollapsed ? '◀' : '▶'}
          </button>
          <h2>豫水地图助手</h2>
          <div className="header-actions">
            <button
              className={`action-button session-button ${showSessionPanel ? 'active' : ''}`}
              onClick={() => setShowSessionPanel(v => !v)}
              title="会话管理"
            >
              💬 会话
            </button>
            <button 
              className="action-button clear-button"
              onClick={clearChat}
              title="开启新对话"
            >
              🗑️ 新对话
            </button>
            {connectionError && (
              <div className="connection-status error" title="服务器连接失败">
                ⚠️
              </div>
            )}
          </div>
        </div>

        {/* 会话管理面板 */}
        <div className={`session-panel ${showSessionPanel ? 'open' : ''}`}>
          <div className="session-panel-inner">
            {sessionsLoading ? (
              <div className="session-loading">
                <span className="session-loading-dot" /><span className="session-loading-dot" /><span className="session-loading-dot" />
              </div>
            ) : sessions.length === 0 ? (
              <div className="session-empty">暂无历史会话</div>
            ) : (
              sessions.map(s => (
                <div
                  key={s.id}
                  className={`session-item ${s.id === sessionId ? 'active' : ''}`}
                  onClick={() => switchSession(s.id)}
                >
                  <div className="session-item-left">
                    {editingSessionId === s.id ? (
                      <input
                        className="session-edit-input"
                        value={editingTitle}
                        autoFocus
                        onChange={e => setEditingTitle(e.target.value)}
                        onBlur={() => commitRename(s.id)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') commitRename(s.id);
                          if (e.key === 'Escape') setEditingSessionId(null);
                        }}
                        onClick={e => e.stopPropagation()}
                      />
                    ) : (
                      <span
                        className="session-title"
                        onDoubleClick={e => startRename(e, s)}
                        title="双击重命名"
                      >
                        {s.title}
                      </span>
                    )}
                    <span className="session-time">{formatTime(s.updated_at)}</span>
                  </div>
                  <button
                    className="session-delete-btn"
                    onClick={e => deleteSession(e, s.id)}
                    title="删除会话"
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="chat-content">
          <div className="messages">
            {messages.length === 0 && (
              <div className="welcome-message">
                <h3>欢迎使用智能一张图！</h3>
                {connectionError && (
                  <div className="connection-warning">
                    ⚠️ 无法连接到后端服务器，请确保后端服务正在运行在 http://172.136.16.14:8006
                  </div>
                )}
                <p>您可以尝试以下功能：</p>
                <div className="suggestions-grid">
                  {suggestions.map((suggestion, index) => (
                    <button
                      key={index}
                      className="suggestion-button"
                      onClick={() => handleSuggestionClick(suggestion)}
                      disabled={connectionError}
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            )}
            
            {messages.map((message, index) => renderMessage(message, index))}
            
            {(isBusy || (streamProgress.length > 0 && !pendingInfo)) && (
              <div className="message assistant loading-message">
                <div className="message-content">
                  <div className="message-role">豫水地图助手</div>
                  <RunStatusBar phase={agentPhase} runId={agentRunId} onCancel={agentCancelRun} />
                  {streamProgress.length === 0 ? (
                    <div className="loading-text">
                      <span className="loading-dots"><span /><span /><span /></span>
                      正在连接后端...
                    </div>
                  ) : (
                    <ProgressTimeline
                      items={streamProgress}
                      showSpinner={isBusy && agentPhase !== 'reconnecting'}
                      endRef={progressEndRef}
                    />
                  )}
                </div>
              </div>
            )}

            {/* pending 交互卡片：等待确认 / 补充参数（busy 结束后可交互） */}
            {pendingInfo && !isBusy && (
              <div className="message assistant loading-message">
                <div className="message-content">
                  <div className="message-role">豫水地图助手</div>
                  <RunStatusBar phase={agentPhase} runId={agentRunId} onCancel={agentCancelRun} />
                  {agentPhase === 'awaiting_confirmation' ? (
                    <ConfirmationCard
                      pending={pendingInfo}
                      onConfirm={resumePendingRun}
                      onCancel={agentCancelRun}
                    />
                  ) : (
                    <ParameterFormCard
                      pending={pendingInfo}
                      onSubmit={resumePendingRun}
                      onCancel={agentCancelRun}
                    />
                  )}
                </div>
              </div>
            )}
            
            <div ref={messagesEndRef} />
          </div>

          <form onSubmit={handleSubmit} className="input-form">
            {/* 已选图片预览 */}
            {selectedFiles.length > 0 && (
              <div className="image-preview-bar">
                {selectedFiles.map((item, idx) => (
                  <div key={idx} className={`image-preview-item${item.uploading ? ' uploading' : ''}`}>
                    <img src={item.preview} alt={`预览 ${idx + 1}`} />
                    {item.uploading && <div className="image-upload-overlay">上传中...</div>}
                    <button
                      type="button"
                      className="image-remove-btn"
                      onClick={() => removeSelectedFile(idx)}
                      disabled={item.uploading}
                      title="移除图片"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
            {/* 已上传 SHP 文件标签 */}
            {shpUploadInfo && (
              <div className="shp-upload-chip-bar">
                <div className="shp-upload-chip">
                  <span className="shp-chip-icon">🗂️</span>
                  <span className="shp-chip-name">{shpUploadInfo.layer_name}</span>
                  <span className="shp-chip-meta">
                    {shpUploadInfo.feature_count}个{shpUploadInfo.geometry_type || '要素'}
                  </span>
                  {shpUploading && <span className="shp-chip-loading">上传中...</span>}
                  <button
                    type="button"
                    className="shp-chip-remove"
                    onClick={clearShpUpload}
                    disabled={shpUploading}
                    title="移除图层"
                  >
                    ✕
                  </button>
                </div>
              </div>
            )}
            <div className="input-container">
              {/* 上传图片按钮 */}
              <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileSelect}
                accept="image/*,.xlsx,.xls"
                multiple
                style={{ display: 'none' }}
              />
              <button
                type="button"
                className="upload-image-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={isBusy}
                title="上传图片"
              >
                📎
              </button>
              <input
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder={connectionError ? "后端服务未连接..." : "请输入您的问题..."}
                disabled={isBusy}
                className="message-input"
              />
              <button 
                type="submit" 
                disabled={isBusy || (!inputValue.trim() && selectedFiles.length === 0 && !shpUploadInfo) || connectionError}
                className="send-button"
              >
                发送
              </button>
            </div>
            {connectionError && (
              <div className="connection-help">
                请确保后端服务正在运行：<code>cd backend && python main.py</code>
              </div>
            )}
          </form>
        </div>
      </div>
    </div>
  </div>
);
}

export default App;
