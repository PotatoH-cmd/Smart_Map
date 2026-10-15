// 知识库通用工具函数和子组件
import { useState } from 'react';
import { styles } from './styles';

// ── 工具函数 ──

export const formatDate = (ts) => {
  if (!ts) return '';
  const d = new Date(ts * 1000 || ts);
  if (isNaN(d.getTime())) return String(ts);
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};

export const formatSize = (bytes) => {
  if (!bytes) return '';
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
  return (bytes / (1024 * 1024)).toFixed(1) + 'MB';
};

export const statusLabel = (s) => {
  const map = { '1': '已解析', '2': '解析中', '0': '待处理' };
  return map[String(s)] || s || '未知';
};

export const statusColor = (s) => {
  const map = { '1': '#10b981', '2': '#f59e0b', '0': '#94a3b8' };
  return map[String(s)] || '#94a3b8';
};

/** 清理文档标题：去掉内部前缀（section_5_0007_ / section_0006_ / table_0001_）与文档后缀。
 *  入库时名称会带上内部序号前缀，直接展示会看到「section_6_0002_6.2_建议」这类脏数据。 */
export const cleanDocName = (name) => {
  let s = String(name || '').trim();
  if (!s) return '';
  s = s.replace(/^(?:section|table|figure|img)_\d+(?:_\d+)*_/i, '');
  s = s.replace(/\.(txt|md|docx?|pdf)$/i, '');
  return s || String(name || '').trim();
};

// ── 子组件 ──

// Thinking steps sub-component
export const ThinkingPanel = ({ steps }) => {
  const [expanded, setExpanded] = useState(false);
  if (!steps || steps.length === 0) return null;
  const stageIcons = ['?', '?', '?', '?'];
  return (
    <div style={styles.qaSources}>
      <button style={styles.qaSourcesToggle} onClick={() => setExpanded(!expanded)}>
        <span>分析过程 (4 阶段)</span>
        <span style={{ transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}>&#x25BC;</span>
      </button>
      {expanded && steps.map((step, i) => (
        <div key={i} style={styles.qaSourceItem}>
          <div style={{ fontWeight: 600, color: '#06b6d4', marginBottom: 4 }}>
            [{stageIcons[step.stage - 1] || '?'}] Stage {step.stage}: {step.label}
          </div>
          {step.stage === 1 && step.data && (
            <div style={{ fontSize: 12, color: '#64748b' }}>
              实体: {(step.data.entities || []).join(', ') || '无'}
              &nbsp;| 属性: {(step.data.attributes || []).join(', ') || '无'}
              &nbsp;| 时间: {(step.data.time_constraints || []).join(', ') || '无'}
            </div>
          )}
          {step.stage === 2 && step.data && (
            <div style={{ fontSize: 12, color: '#64748b' }}>
              总结果: {step.data.total_chunks} 条 | 检索批次: {step.data.passes?.length || 0}
            </div>
          )}
          {step.stage === 3 && step.data && (
            <div style={{ fontSize: 12, color: '#64748b' }}>
              提取 {step.data.extracted_count} 条结构化数据
              {(step.data.items || []).slice(0, 5).map((item, j) => (
                <div key={j} style={{ marginLeft: 12, marginTop: 2 }}>
                  - {item.location || '?'}: {item.attribute} = {item.value}{item.unit} ({item.year || '?'}年)
                </div>
              ))}
            </div>
          )}
          {step.stage === 4 && step.data && (
            <div style={{ fontSize: 12, color: '#64748b' }}>
              生成回答 {step.data.answer_length} 字
            </div>
          )}
        </div>
      ))}
    </div>
  );
};

// Source panel sub-component
export const SourcePanel = ({ sources }) => {
  const [expanded, setExpanded] = useState(false);
  return (
    <div style={styles.qaSources}>
      <button style={styles.qaSourcesToggle} onClick={() => setExpanded(!expanded)}>
        <span>引用来源 ({sources.length})</span>
        <span style={{ transform: expanded ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}>&#x25BC;</span>
      </button>
      {expanded && sources.map((src, i) => (
        <div key={i} style={styles.qaSourceItem}>
          <div style={{ fontWeight: 600, color: '#0ea5e9' }}>
            [{i + 1}] {src.title} (相关度: {(src.relevance * 100).toFixed(0)}%)
          </div>
          <div style={{ color: '#64748b' }}>{src.snippet}</div>
        </div>
      ))}
    </div>
  );
};