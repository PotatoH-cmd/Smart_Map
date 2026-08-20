import React from 'react';

// 状态机阶段 → 展示元信息（浅色系徽章，样式进 App.css）
const STATUS_META = {
  idle: { label: '空闲', cls: 'idle' },
  sending: { label: '提交中', cls: 'running' },
  streaming: { label: '进行中', cls: 'running' },
  reconnecting: { label: '重连中', cls: 'warning' },
  awaiting_confirmation: { label: '等待确认', cls: 'warning' },
  awaiting_input: { label: '等待补充信息', cls: 'warning' },
  done: { label: '已完成', cls: 'success' },
  error: { label: '失败', cls: 'error' },
  cancelled: { label: '已取消', cls: 'error' },
};

// 非终态且可取消的阶段（取消按钮"全程可用"）
const CANCELLABLE_PHASES = new Set([
  'sending',
  'streaming',
  'reconnecting',
  'awaiting_confirmation',
  'awaiting_input',
]);

/**
 * RunStatusBar — run_id 徽章 + 状态徽章 + 取消按钮（阶段5）。
 *
 * 展示当前 run 的生命周期状态；取消语义为"步骤间停止"
 * （后端 RunEngine 在每个步骤中断点响应取消标记）。
 */
const RunStatusBar = ({ phase, runId, onCancel, canCancel = true }) => {
  if (!phase || phase === 'idle') return null;
  const meta = STATUS_META[phase] || { label: phase, cls: 'idle' };
  const cancellable = canCancel && !!runId && CANCELLABLE_PHASES.has(phase) && typeof onCancel === 'function';

  return (
    <div className={`run-status-bar rs-${meta.cls}`}>
      {runId && (
        <span className="run-id-badge" title={`run_id: ${runId}`}>
          run:{runId.slice(0, 8)}
        </span>
      )}
      <span className="run-status-badge">{meta.label}</span>
      {cancellable && (
        <button type="button" className="run-cancel-btn" onClick={onCancel}>
          取消任务
        </button>
      )}
    </div>
  );
};

export default RunStatusBar;
