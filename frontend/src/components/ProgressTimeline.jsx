import React from 'react';

/**
 * ProgressTimeline — 流式进度卡片列表（阶段5）。
 *
 * 替代 App.jsx 内散落的 streamProgress 渲染；纯展示组件，
 * 数据由 useAgentChat 的 progress 数组提供（含兜底/重连提示条目）。
 */
const ProgressTimeline = ({ items = [], showSpinner = true, endRef }) => {
  if (!items || items.length === 0) return null;
  const running = showSpinner;
  return (
    <div className={'stream-progress-list' + (running ? ' is-running' : '')}>
      {items.map((item, idx) => {
        const isLast = idx === items.length - 1;
        const state = running && isLast && item.tone === 'info' ? 'running' : item.tone || 'info';
        return (
          <div
            key={item.id}
            className={
              `stream-progress-card tone-${item.tone || 'info'} state-${state}`
              + (item.source === 'fallback' ? ' fallback' : '')
              + (item.source === 'reconnect' ? ' reconnect' : '')
            }
          >
            <span className="spc-node" aria-hidden="true">
              {item.tone === 'error' ? '✕'
                : item.tone === 'success' ? '✓'
                : item.tone === 'warning' ? '!'
                : ''}
            </span>
            <div className="spc-body">
              <div className="spc-header">
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
          </div>
        );
      })}
      {showSpinner && (
        <div className="spc-thinking">
          <span className="spc-thinking-track"><span className="spc-thinking-bar" /></span>
          <span className="spc-thinking-text">正在思考</span>
        </div>
      )}
      {endRef && <div ref={endRef} />}
    </div>
  );
};

export default ProgressTimeline;
