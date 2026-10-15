import { useState, useEffect, useRef, useCallback } from 'react';
import './App.css';
import MapComponent from './components/MapComponent';
import CesiumComponent from './components/CesiumComponent';
import KnowledgeBaseManager from './components/KnowledgeBaseManager';
import FalconPanel from './components/FalconPanel';
import TileManager from './components/TileManager';
import GisPipeline from './components/GisPipeline';
import ToolHubConsole from './components/ToolHubConsole';
import ObservabilityConsole from './components/ObservabilityConsole';
import ChatMessage from './components/ChatMessage';
import ChatInput from './components/ChatInput';
import SessionPanel from './components/SessionPanel';
import RunStatusBar from './components/RunStatusBar';
import ConfirmationCard from './components/ConfirmationCard';
import ParameterFormCard from './components/ParameterFormCard';
import ProgressTimeline from './components/ProgressTimeline';
import useAgentChat from './hooks/useAgentChat';
import { buildChartMessages, getMessageKey, generateSessionId } from './utils/chart';
import { executeMapCommands } from './utils/mapCommands';
import { captureMapToServer } from './utils/capture';

// 离线模式的默认建议
const DEFAULT_SUGGESTIONS = [
  '在地图上标记北京天安门广场的位置',
  '统计数据库中各个砂场的平均超深，并以柱状图展示',
  '分析最近一月的采砂量趋势，生成折线图',
  '清除地图上的所有标记',
  '切换到卫星图层',
];

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
  const handleDeleteSession = (sid) => {
    if (sid === sessionId) {
      setMessages([]);
      setSessionId(generateSessionId());
    }
  };

  // 重命名会话（面板内部已处理，此处保留为空占位）
  // eslint-disable-next-line no-unused-vars
  const handleRenameSession = (_sid, _title) => {};

  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState(DEFAULT_SUGGESTIONS);
  const [mapManager, setMapManager] = useState(null);
  const [falconMapManager, setFalconMapManager] = useState(null);
  const [isPanelCollapsed, setIsPanelCollapsed] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [activeView, setActiveView] = useState('map');
  const [isCapturing, setIsCapturing] = useState(false);
  const [lastMapScreenshotPath, setLastMapScreenshotPath] = useState(null);
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [uploadedUrls, setUploadedUrls] = useState([]);
  const fileInputRef = useRef(null);
  const [shpUploadInfo, setShpUploadInfo] = useState(null);
  // eslint-disable-next-line no-unused-vars
  const [shpUploading, setShpUploading] = useState(false);
  const shpInputRef = useRef(null);
  const messagesEndRef = useRef(null);
  const progressEndRef = useRef(null);
  const mapWrapperRef = useRef(null);
  const cesiumViewerRef = useRef(null);
  const pendingCesiumCmds = useRef([]);

  useEffect(() => {
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

  // 切换回 2D 地图时，通知 Leaflet 重新计算尺寸
  useEffect(() => {
    if (activeView === 'map' && mapManager?.map) {
      const timer = setTimeout(() => {
        mapManager.map.invalidateSize();
      }, 100);
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
    if (pendingCesiumCmds.current.length > 0) {
      console.log('[App] 执行暂存的 Cesium 命令:', pendingCesiumCmds.current.length, '条');
      setTimeout(() => {
        pendingCesiumCmds.current.forEach(cmd => {
          window.dispatchEvent(new CustomEvent('cesium_execute_command', { detail: cmd }));
        });
        pendingCesiumCmds.current = [];
      }, 800);
    }
  }, []);

  const handleLayerStatus = useCallback((payload) => {
    const { status, layerName, message } = payload || {};
    if (!status || !layerName) return;
    let content = '';
    if (status === 'empty') {
      const detail = message ? `（${message}）` : '';
      content = `图层"${layerName}"无可用要素，未更新视图${detail}。`;
    } else if (status === 'error') {
      content = `图层"${layerName}"加载失败：${message || '未知错误'}。`;
    }
    if (!content) return;
    setMessages(prev => [...prev, { role: 'assistant', content }]);
  }, []);

  const handleScreenshot = async () => {
    if (!mapWrapperRef.current || isCapturing) return;
    setIsCapturing(true);
    try {
      const html2canvas = (await import('html2canvas')).default;
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

  const handlePreviewReport = (filename) => {
    const previewUrl = `/api/preview/report?filename=${encodeURIComponent(filename)}`;
    window.open(previewUrl, '_blank');
  };

  // ---- 文件上传 ----
  const handleFileSelect = (e) => {
    const files = Array.from(e.target.files);
    if (files.length === 0) return;
    const newFiles = files.map(file => ({
      file,
      preview: URL.createObjectURL(file),
      uploading: false,
    }));
    setSelectedFiles(prev => [...prev, ...newFiles]);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const removeSelectedFile = (index) => {
    setSelectedFiles(prev => {
      const updated = [...prev];
      if (updated[index]?.preview) URL.revokeObjectURL(updated[index].preview);
      updated.splice(index, 1);
      return updated;
    });
  };

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

  const clearShpUpload = () => {
    setShpUploadInfo(null);
    if (shpInputRef.current) shpInputRef.current.value = '';
  };

  // ---- 应用服务器响应 ----
  const applyServerResponse = useCallback((data) => {
    const responseMessages = Array.isArray(data.messages) ? data.messages : [];
    const normalizedMessages = [...responseMessages];

    if (data.response && !normalizedMessages.some(m => m.role === 'assistant' && m.content === data.response)) {
      normalizedMessages.push({ role: 'assistant', content: data.response, mid: `final-${Date.now()}` });
    }

    // 结构化溯源引用：挂到最终回答消息上，由 ChatMessage 渲染可点击来源
    if (Array.isArray(data.sources) && data.sources.length > 0) {
      for (let i = normalizedMessages.length - 1; i >= 0; i--) {
        const m = normalizedMessages[i];
        if (m.role === 'assistant' && m.content === data.response) {
          normalizedMessages[i] = { ...m, sources: data.sources };
          break;
        }
      }
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

    const existingChartKeys = new Set(responseMessages.map(m => {
      try {
        const raw = typeof m.content === 'string' ? JSON.parse(m.content) : m.content;
        return raw?.config ? `${raw.chart_type}_${raw.config?.title?.text}` : null;
      } catch { return null; }
    }).filter(Boolean));
    const chartMessages = buildChartMessages(data.charts).filter(chartMessage => {
      try {
        const raw = typeof chartMessage.content === 'string' ? JSON.parse(chartMessage.content) : chartMessage.content;
        const key = `${raw.chart_type}_${raw.config?.title?.text}`;
        return key && !existingChartKeys.has(key);
      } catch { return true; }
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
      executeMapCommands(mapManager, data.map_commands);
      if (activeView !== 'cesium' && activeView !== 'kb') {
        setTimeout(() => {
          captureMapToServer('auto_map', {}, mapWrapperRef, mapManager, setLastMapScreenshotPath).catch(() => {});
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
  }, [activeView, mapManager]);

  // ---- 前端状态机接入 ----
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

  useEffect(() => {
    scrollToBottom();
  }, [messages, isBusy]);

  useEffect(() => {
    if (isBusy && progressEndRef.current) {
      progressEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [streamProgress, isBusy]);

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

  // ---- 发送消息 ----
  const sendMessage = async (content) => {
    const hasText = content && content.trim().length > 0;
    const hasImages = selectedFiles.length > 0 || uploadedUrls.length > 0;
    const hasShp = shpUploadInfo !== null;
    if ((!hasText && !hasImages && !hasShp) || isBusy) return;

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
        mapImageServerPath = await captureMapToServer('report_map', {}, mapWrapperRef, mapManager, setLastMapScreenshotPath);
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
    const currentShpInfo = shpUploadInfo;
    if (currentShpInfo) {
      userMessage.content = `${userMessage.content}\n\n[已上传SHP数据] 图层名: ${currentShpInfo.layer_name}, 容器内路径: ${currentShpInfo.container_path}, 要素数: ${currentShpInfo.feature_count}, 几何类型: ${currentShpInfo.geometry_type}。进行空间分析时请使用容器内路径。`;
      setShpUploadInfo(null);
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
    setUploadedUrls([]);

    await agentSend({
      messages: newMessages,
      activeView,
      sessionId,
    });
  };

  const handleSubmit = (content) => {
    sendMessage(content);
  };

  const handleSuggestionClick = (suggestion) => {
    sendMessage(suggestion);
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
              className={`nav-button ${activeView === 'falcon' ? 'active' : ''}`}
              onClick={() => setActiveView('falcon')}
            >
              🎯 Falcon识别
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
            <button
              className={`nav-button ${activeView === 'toolhub' ? 'active' : ''}`}
              onClick={() => setActiveView('toolhub')}
            >
              🧰 工具中台
            </button>
            <button
              className={`nav-button ${activeView === 'obs' ? 'active' : ''}`}
              onClick={() => setActiveView('obs')}
            >
              📈 观测
            </button>
          </div>
        </div>
      </div>

      <div className={`main-content ${(activeView === 'falcon' || activeView === 'kb' || activeView === 'tiles' || activeView === 'toolhub' || activeView === 'gis' || activeView === 'obs') ? 'falcon-page-mode' : ''}`}>
        <div className="view-container">
          {/* 2D 地图 */}
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
          {/* 3D 视图 */}
          <div className="map-wrapper cesium-wrapper" style={{ display: activeView === 'cesium' ? 'block' : 'none' }}>
            <CesiumComponent onViewerReady={handleCesiumReady} visible={activeView === 'cesium'} />
          </div>
          {/* 知识库管理 */}
          <div style={{ display: activeView === 'kb' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <KnowledgeBaseManager />
          </div>
          {/* Falcon识别 */}
          <div className="falcon-workspace" style={{ display: activeView === 'falcon' ? 'flex' : 'none' }}>
            <aside className="falcon-workspace-sidebar">
              <FalconPanel mapManager={falconMapManager} />
            </aside>
            <section className="falcon-workspace-map">
              <MapComponent onMapReady={setFalconMapManager} onLayerStatus={handleLayerStatus} visible={activeView === 'falcon'} />
            </section>
          </div>
          {/* 切片管理 */}
          <div style={{ display: activeView === 'tiles' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <TileManager onSwitchTo3D={() => setActiveView('cesium')} />
          </div>
          {/* GIS工具 */}
          <div style={{ display: activeView === 'gis' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <GisPipeline />
          </div>
          {/* 工具中台 */}
          <div style={{ display: activeView === 'toolhub' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <ToolHubConsole />
          </div>
          {/* 智能体观测台 */}
          <div style={{ display: activeView === 'obs' ? 'block' : 'none', height: '100%', overflow: 'hidden' }}>
            <ObservabilityConsole />
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
          <SessionPanel
            show={showSessionPanel}
            sessionId={sessionId}
            onSwitchSession={switchSession}
            onDeleteSession={handleDeleteSession}
            onRenameSession={handleRenameSession}
          />

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

              {messages.map((message, index) => (
                <ChatMessage
                  key={index}
                  message={message}
                  index={index}
                  onPreviewReport={handlePreviewReport}
                />
              ))}

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

            <ChatInput
              inputValue={inputValue}
              setInputValue={setInputValue}
              isBusy={isBusy}
              connectionError={connectionError}
              selectedFiles={selectedFiles}
              removeSelectedFile={removeSelectedFile}
              shpUploadInfo={shpUploadInfo}
              shpUploading={shpUploading}
              clearShpUpload={clearShpUpload}
              fileInputRef={fileInputRef}
              onFileSelect={handleFileSelect}
              onSubmit={handleSubmit}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;