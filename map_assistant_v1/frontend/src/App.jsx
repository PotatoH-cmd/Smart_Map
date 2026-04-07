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

const API_BASE_URL = ''; // 使用相对路径以启用 CRA 代理或同源部署

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

  const clearChat = () => {
    if (window.confirm('确定要清空所有对话记录吗？')) {
      setMessages([]);
      localStorage.removeItem('chat_history');
    }
  };
  const [inputValue, setInputValue] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [suggestions, setSuggestions] = useState(DEFAULT_SUGGESTIONS);
  const [mapManager, setMapManager] = useState(null);
  const [isPanelCollapsed, setIsPanelCollapsed] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [activeView, setActiveView] = useState('map'); // 'map' | 'cesium' | 'kb'
  const [isCapturing, setIsCapturing] = useState(false);
  const messagesEndRef = useRef(null);
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

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

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
            mapManager.setView(viewLat, viewLng, zoom);
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
              mapManager.addVectorLayerFromAPI(command.url, command.name);
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

  const sendMessage = async (content) => {
    if (!content.trim() || isLoading) return;

    const userMessage = { role: 'user', content };
    const newMessages = [...messages, userMessage];
    setMessages(newMessages);
    setInputValue('');
    setIsLoading(true);

    try {
      const response = await fetch(`/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          messages: newMessages,
          active_view: activeView,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        const errorMessage = errorData.detail || `Server error: ${response.status}`;
        throw new Error(errorMessage);
      }

      const data = await response.json();
      
      // 修复：不要直接用 data.messages 覆盖，而是将新产生的消息追加到当前消息列表中
      // 后端返回的 data.messages 通常包含本次对话产生的 assistant 和 function 消息
      if (data.messages && data.messages.length > 0) {
        setMessages(prev => {
          // 过滤掉已经在 prev 中存在的消息（通过内容和角色简单判断）
          const existingContent = new Set(prev.map(m => `${m.role}_${m.content}`));
          const newUniqueMessages = data.messages.filter(m => !existingContent.has(`${m.role}_${m.content}`));
          return [...prev, ...newUniqueMessages];
        });
      }
      console.log('收到服务器响应:', data);
      if (data.map_commands && data.map_commands.length > 0) {
        console.log('执行地图命令:', data.map_commands);
        data.map_commands.forEach((cmd, idx) => {
          console.log(`[MapCmd ${idx}] type=${cmd.type} url=${cmd.url || ''} name=${cmd.name || ''}`);
        });
        executeMapCommands(data.map_commands);
      }
      // 处理 Cesium 3D 命令
      if (data.cesium_commands && data.cesium_commands.length > 0) {
        console.log('[App] 收到 Cesium 命令:', data.cesium_commands);
        if (activeView === 'cesium') {
          // 已在 3D 视图：直接派发
          data.cesium_commands.forEach(cmd => {
            window.dispatchEvent(new CustomEvent('cesium_execute_command', { detail: cmd }));
          });
        }
        // 如果在 2D 视图收到 cesium 命令，不自动切换（应该由后端根据 active_view 返回正确的命令类型）
      }
      setConnectionError(false);
    } catch (error) {
      console.error('Error:', error);
      let errorMessage = '抱歉，发生了错误。请稍后重试。';
      if (error.message.includes('Failed to fetch') || error.message.includes('ERR_CONNECTION_REFUSED')) {
        errorMessage = '无法连接到服务器，请确保后端服务正在运行 (http://localhost:8006)';
        setConnectionError(true);
      } else if (error.message.includes('Messages cannot be empty')) {
        errorMessage = '请输入消息内容。';
      } else if (error.message.includes('Invalid role')) {
        errorMessage = '消息格式错误。';
      } else if (error.message.includes('Network')) {
        errorMessage = '网络连接错误，请检查网络连接。';
      }
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: errorMessage
      }]);
    } finally {
      setIsLoading(false);
    }
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
        backgroundColor: '#f0ede8',
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

  const renderMessage = (message, index) => {
    const isUser = message.role === 'user';
    const isAssistant = message.role === 'assistant';
    const isFunction = message.role === 'function';

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
                <div className="chart-container" style={{ height: '350px', marginTop: '10px', width: '100%' }}>
                  <ReactECharts 
                    option={result.config} 
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
          <div className="message-role">{isUser ? '用户' : 'Qwen3 地图助手'}</div>
          <div className="message-text markdown-body">
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({node, ...props}) => {
                  // 拦截链接点击，处理报告下载
                  const href = props.href || '';
                  if (href.startsWith('/static/reports/')) {
                    const downloadUrl = `${href}`;
                    return (
                      <a 
                        {...props} 
                        href={downloadUrl}
                        target="_blank" 
                        rel="noopener noreferrer"
                        className="download-link"
                      >
                        📄 {props.children || '下载报告'}
                      </a>
                    );
                  }
                  return <a {...props} target="_blank" rel="noopener noreferrer" />;
                }
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
            <div className="header-subtitle">AI驱动的智能地图助手</div>
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
          </div>
        </div>
      </div>

      <div className="main-content">
        <div className="view-container">
          {activeView === 'map' ? (
            <div className="map-wrapper" ref={mapWrapperRef}>
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
          ) : activeView === 'cesium' ? (
            <div className="map-wrapper cesium-wrapper" ref={mapWrapperRef}>
              <CesiumComponent onViewerReady={handleCesiumReady} />
              <div className="map-toolbar">
                <button
                  className="map-tool-button"
                  onClick={() => {
                    if (cesiumViewerRef.current) {
                      cesiumViewerRef.current.entities.removeAll();
                      cesiumViewerRef.current.dataSources.removeAll();
                      setMessages(prev => [...prev, { role: 'assistant', content: '已清除3D地图上的所有实体和图层。' }]);
                    }
                  }}
                  title="清除3D地图"
                >
                  🗑️ 清除3D
                </button>
              </div>
            </div>
          ) : (
            <KnowledgeBaseManager />
          )}
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
          <h2>Qwen3 地图助手</h2>
          <div className="header-actions">
            <button 
              className="action-button clear-button"
              onClick={clearChat}
              title="开启新对话"
              disabled={messages.length === 0}
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
            
            {isLoading && (
              <div className="message assistant">
                <div className="message-content">
                  <div className="message-role">Qwen3 地图助手</div>
                  <div className="loading">正在思考中...</div>
                </div>
              </div>
            )}
            
            <div ref={messagesEndRef} />
          </div>

          <form onSubmit={handleSubmit} className="input-form">
            <div className="input-container">
              <input
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder={connectionError ? "后端服务未连接..." : "请输入您的问题..."}
                disabled={isLoading}
                className="message-input"
              />
              <button 
                type="submit" 
                disabled={isLoading || !inputValue.trim() || connectionError}
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
