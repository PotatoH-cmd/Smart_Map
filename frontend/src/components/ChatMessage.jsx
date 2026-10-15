// ChatMessage.jsx — 消息渲染组件（从 App.jsx renderMessage 抽离）
import React from 'react';
import ReactECharts from 'echarts-for-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { normalizeChartOption } from '../utils/chart';

const emojiMap = {
  bar: '📊',
  line: '📈',
  pie: '🥧',
  scatter: '🌌',
};

const typeNameMap = {
  bar: '柱状图',
  line: '折线图',
  pie: '饼图',
  scatter: '散点图',
};

const ChatMessage = ({ message, index, onPreviewReport }) => {
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
              onClick={() => onPreviewReport?.(filename)}
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

      if (result && result.success === false) {
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
        const emoji = emojiMap[result.chart_type] || '📊';
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

    if (message.name === 'data_visualizer_tool') {
      return null;
    }
  }

  // 过滤掉没有内容的消息
  if (!isUser && (!message.content || !message.content.trim())) {
    return null;
  }

  if (!isUser && !isAssistant) return null;

  const kbSources = Array.isArray(message.sources) ? message.sources.filter(s => s && (s.source_url || s.document_id)) : [];

  return (
    <div key={index} className={`message ${isUser ? 'user' : 'assistant'}`}>
      <div className="message-content">
        <div className="message-role">{isUser ? '用户' : '豫水地图助手'}</div>
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
              a: ({ node, ...props }) => {
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
                        onPreviewReport?.(filename);
                      }}
                    >
                      📄 {props.children || '预览报告'}
                    </a>
                  );
                }
                return <a {...props} target="_blank" rel="noopener noreferrer" />;
              },
              table: ({ node, ...props }) => (
                <div style={{ overflowX: 'auto', width: '100%', marginBottom: 4 }}>
                  <table {...props} />
                </div>
              ),
            }}
          >
            {message.content}
          </ReactMarkdown>
        </div>
        {kbSources.length > 0 && (
          <div className="kb-sources">
            <div className="kb-sources-title">📎 溯源来源（点击查看原文）</div>
            {kbSources.map((s, sIdx) => {
              const href = s.source_url || `/api/knowledge/preview/${s.document_id}`;
              return (
                <a
                  key={sIdx}
                  className="kb-source-item"
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={s.section || s.title}
                >
                  <span className="kb-source-doc">📄 {s.title}</span>
                  {s.section && <span className="kb-source-sec">{s.section}</span>}
                </a>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};

export default ChatMessage;