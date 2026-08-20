import React, { useState } from 'react';

// 常见缺参的输入占位提示
const PLACEHOLDERS = {
  distance: '如：500米',
  feature_name: '如：某水库',
  table_name: '如：cai_sha_table',
  demand: '请描述你的具体需求',
  coordinates: '如：113.5, 34.7',
};

/**
 * ParameterFormCard — 缺参补参卡片（阶段5）。
 *
 * 按后端返回的 missing 列表渲染输入框，提交时组装为"参数名: 值"文本
 * （后端 parse_supplied_from_message 规则解析），随 X-Pending-Run-ID 头发送
 * 即从 checkpoint 断点 resume。
 */
const ParameterFormCard = ({ pending, onSubmit, onCancel }) => {
  const [values, setValues] = useState({});
  const missing = (pending?.missing || []).filter(m => m?.param || m?.label);

  const setValue = (param, value) => setValues(prev => ({ ...prev, [param]: value }));

  const handleSubmit = () => {
    const parts = missing
      .map(m => {
        const param = m.param || m.label;
        const value = (values[param] || '').trim();
        return value ? `${param}: ${value}` : '';
      })
      .filter(Boolean);
    if (parts.length === 0) return;
    onSubmit?.(parts.join('\n'));
  };

  const allFilled = missing.every(m => ((values[m.param || m.label] || '').trim() !== ''));

  return (
    <div className="param-form-card">
      <div className="cc-header">
        <span className="cc-icon">✍️</span>
        <span>等待补充信息</span>
      </div>
      {pending?.question && <div className="cc-question">{pending.question}</div>}
      {missing.map((m, i) => {
        const param = m.param || m.label;
        return (
          <div className="pf-field" key={param || i}>
            <label className="pf-label">{m.label || param}</label>
            <input
              type="text"
              className="pf-input"
              placeholder={PLACEHOLDERS[param] || `请输入${m.label || param}`}
              value={values[param] || ''}
              onChange={(e) => setValue(param, e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSubmit();
              }}
            />
          </div>
        );
      })}
      <div className="cc-actions">
        <button
          type="button"
          className="cc-confirm-btn"
          onClick={handleSubmit}
          disabled={!allFilled}
        >
          提交并继续
        </button>
        <button type="button" className="cc-cancel-btn" onClick={() => onCancel?.()}>
          取消任务
        </button>
      </div>
    </div>
  );
};

export default ParameterFormCard;
