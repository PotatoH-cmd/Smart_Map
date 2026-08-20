import React, { useState } from 'react';

/**
 * ConfirmationCard — 计划确认卡片（阶段5）。
 *
 * 展示执行计划步骤 + 「确认执行 / 取消任务」按钮 + 可选参数补充框。
 * 确认时由上层携带 X-Pending-Run-ID 头，并发送带 __confirm__ 标记的消息
 * （后端 is_confirm_message 规则判定），run 从 checkpoint 断点继续执行。
 */
const ConfirmationCard = ({ pending, onConfirm, onCancel }) => {
  const [extra, setExtra] = useState('');
  const plan = pending?.plan || {};
  const steps = Array.isArray(plan.execution_plan) ? plan.execution_plan : [];

  const handleConfirm = () => {
    const note = extra.trim();
    onConfirm?.(note ? `__confirm__\n补充说明：${note}` : '__confirm__');
  };

  return (
    <div className="confirmation-card">
      <div className="cc-header">
        <span className="cc-icon">⏳</span>
        <span>等待确认执行计划</span>
      </div>
      {pending?.question && <div className="cc-question">{pending.question}</div>}
      {steps.length > 0 && (
        <ul className="cc-steps">
          {steps.map((s, i) => (
            <li key={s.step_id || i}>
              <span className="cc-step-id">{s.step_id || i + 1}</span>
              <span className="cc-step-action">{s.action || s.tool}</span>
              {s.tool && <span className="cc-step-tool">{s.tool}</span>}
            </li>
          ))}
        </ul>
      )}
      <textarea
        className="cc-extra"
        placeholder="可选：补充说明或对计划的调整意见（随确认一并提交）"
        value={extra}
        onChange={(e) => setExtra(e.target.value)}
        rows={2}
      />
      <div className="cc-actions">
        <button type="button" className="cc-confirm-btn" onClick={handleConfirm}>
          确认执行
        </button>
        <button type="button" className="cc-cancel-btn" onClick={() => onCancel?.()}>
          取消任务
        </button>
      </div>
    </div>
  );
};

export default ConfirmationCard;
