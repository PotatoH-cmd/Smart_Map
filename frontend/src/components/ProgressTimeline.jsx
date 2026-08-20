import React from 'react';

/**
 * ProgressTimeline — 流式进度卡片列表（阶段5）。
 *
 * 替代 App.jsx 内散落的 streamProgress 渲染；纯展示组件，
 * 数据由 useAgentChat 的 progress 数组提供（含兜底/重连提示条目）。
 */
const ProgressTimeline = ({ items = [], showSpinner = true, endRef }) => {
  if (!items || items.length === 0) return null;
  return (
    <div className="stream-progress-list">
      {items.map((item) => (
        <div
          key={item.id}
          className={
            `stream-progress-card tone-${item.tone || 'info'}`
            + (item.source === 'fallback' ? ' fallback' : '')
            + (item.source === 'reconnect' ? ' reconnect' : '')
          }
        >
          <div className="spc-header">
            <span className={`spc-icon tone-${item.tone || 'info'}`}>
              {item.tone === 'error' ? '✕'
                : item.tone === 'success' ? '✓'
                : item.tone === 'warning' ? '!'
                : '…'}
            </span>
            <span className="spc-title">{item.title}</span>
            {item.badge && <span className="spc-badge">{item.badge}</span>}
          </div>
          {item.details && item.details.length > 0 && (
            <ul className="spc-details">
              {item.details.map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      {showSpinner && (
        <div className="spc-spinner">
          <span /><span /><span />
        </div>
      )}
      {endRef && <div ref={endRef} />}
    </div>
  );
};

export default ProgressTimeline;
