// 知识库组件共享样式定义
export const styles = {
  container: {
    padding: 20, height: '100%', display: 'flex', flexDirection: 'column',
    background: '#f0ede8', overflow: 'hidden', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
  },
  header: {
    display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16
  },
  headerTitle: {
    margin: 0, fontSize: 22, color: '#1e293b', fontWeight: 700
  },
  badge: {
    background: '#e0f2fe', color: '#0284c7', padding: '2px 10px',
    borderRadius: 10, fontSize: 11, fontWeight: 600, border: '1px solid #bae6fd'
  },
  tabBar: {
    display: 'flex', gap: 0, marginBottom: 16, background: '#fff', borderRadius: 10,
    padding: 3, boxShadow: '0 1px 2px rgba(0,0,0,0.04)'
  },
  tab: (active) => ({
    flex: 1, padding: '8px 16px', border: 'none', borderRadius: 8,
    background: active ? 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)' : 'transparent',
    color: active ? '#fff' : '#64748b', cursor: 'pointer', fontWeight: active ? 700 : 500,
    fontSize: 13, transition: 'all 0.2s'
  }),
  infoCard: {
    background: '#fffbeb', border: '1px solid #fde68a', padding: '12px 16px',
    borderRadius: 8, display: 'flex', gap: 12, marginBottom: 16, fontSize: 13, color: '#92400e'
  },
  folderBar: {
    display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center'
  },
  folderChip: (active) => ({
    padding: '6px 14px', borderRadius: 20, cursor: 'pointer', fontSize: 12,
    fontWeight: active ? 700 : 500, transition: 'all 0.2s',
    border: active ? '1px solid transparent' : '1px solid #e2e8f0',
    background: active ? 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)' : '#fff',
    color: active ? '#fff' : '#475569',
    display: 'flex', alignItems: 'center', gap: 6
  }),
  folderCount: (active) => ({
    fontSize: 11, fontWeight: 700, padding: '0 6px', borderRadius: 8,
    background: active ? 'rgba(255,255,255,0.25)' : '#f1f5f9',
    color: active ? '#fff' : '#64748b'
  }),
  projectTag: {
    display: 'inline-block', padding: '1px 7px', borderRadius: 8, fontSize: 11,
    background: '#eef2ff', color: '#6366f1', border: '1px solid #e0e7ff', fontWeight: 600
  },
  staleBadge: {
    display: 'inline-block', padding: '1px 7px', borderRadius: 8, fontSize: 11,
    background: '#fff7ed', color: '#c2410c', border: '1px solid #fed7aa', fontWeight: 600
  },
  pager: {
    display: 'flex', alignItems: 'center', gap: 12, justifyContent: 'center',
    padding: '10px 0 2px', flexShrink: 0
  },
  pagerBtn: {
    padding: '5px 14px', borderRadius: 7, fontSize: 12, fontWeight: 600,
    background: '#fff', color: '#334155', border: '1px solid #e2e8f0', cursor: 'pointer'
  },
  pagerBtnDisabled: {
    color: '#cbd5e1', cursor: 'not-allowed', background: '#f8fafc'
  },
  pagerInfo: {
    fontSize: 12, color: '#64748b'
  },
  toolbar: {
    display: 'flex', gap: 8, marginBottom: 12
  },
  searchInput: {
    flex: 1, padding: '8px 12px', border: '1px solid #e2e8f0', borderRadius: 8,
    fontSize: 13, outline: 'none', background: '#fff', color: '#334155'
  },
  btnPrimary: {
    padding: '8px 16px', border: 'none', borderRadius: 8,
    background: 'linear-gradient(135deg, #0ea5e9 0%, #06b6d4 100%)',
    color: '#fff', fontWeight: 600, fontSize: 12, cursor: 'pointer',
    boxShadow: '0 2px 6px rgba(14,165,233,0.3)', whiteSpace: 'nowrap'
  },
  btnOutline: {
    padding: '8px 16px', border: '1px solid #cbd5e1', borderRadius: 8,
    background: '#fff', color: '#475569', fontWeight: 500, fontSize: 12, cursor: 'pointer'
  },
  docGrid: {
    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
    gap: 12, flex: 1, overflow: 'auto', alignContent: 'start'
  },
  folderCard: {
    background: '#fff', borderRadius: 12, padding: '28px 20px',
    border: '1px solid #e2e8f0', cursor: 'pointer', transition: 'all 0.2s',
    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10,
    minHeight: 140, justifyContent: 'center'
  },
  folderCardName: {
    fontSize: 15, fontWeight: 700, color: '#1e293b', textAlign: 'center'
  },
  folderCardCount: {
    fontSize: 12, color: '#64748b', background: '#f1f5f9',
    padding: '2px 10px', borderRadius: 10
  },
  docCard: {
    background: '#fff', borderRadius: 10, padding: 14,
    border: '1px solid #e2e8f0', cursor: 'pointer', transition: 'all 0.2s',
    display: 'flex', flexDirection: 'column', gap: 6, position: 'relative',
    minHeight: 90
  },
  docName: {
    fontWeight: 600, fontSize: 13, color: '#1e293b', lineHeight: 1.45,
    // 名称最多 2 行，超出省略。显式 line-height + flexShrink:0 是必需的：
    // 卡片是 column flex，子项默认可被压缩，行高被压时名称会与下方 meta 重叠。
    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
    overflow: 'hidden', wordBreak: 'break-word', flexShrink: 0, paddingRight: 24
  },
  docMeta: {
    fontSize: 11, color: '#94a3b8', display: 'flex', gap: 10, flexWrap: 'wrap',
    flexShrink: 0, lineHeight: 1.5
  },
  docStatus: (color) => ({
    display: 'inline-block', padding: '1px 7px', borderRadius: 8,
    fontSize: 11, background: color + '18', color: color, border: `1px solid ${color}33`,
    fontWeight: 600
  }),
  deleteBtn: {
    position: 'absolute', top: 8, right: 8, background: 'none', border: 'none',
    fontSize: 16, color: '#94a3b8', cursor: 'pointer', padding: '2px 6px',
    borderRadius: 4, lineHeight: 1
  },
  emptyState: {
    gridColumn: '1 / -1', textAlign: 'center', padding: 60, color: '#94a3b8', fontSize: 14
  },
  loadingState: {
    display: 'flex', flexDirection: 'column', alignItems: 'center', padding: 60, gap: 12, color: '#94a3b8'
  },
  spinner: {
    width: 28, height: 28, border: '3px solid #e2e8f0',
    borderTopColor: '#0ea5e9', borderRadius: '50%', animation: 'kbSpin 0.8s linear infinite'
  },
  errorBar: {
    background: '#fef2f2', border: '1px solid #fecaca', padding: '10px 14px',
    borderRadius: 8, marginBottom: 12, display: 'flex', justifyContent: 'space-between',
    alignItems: 'center', fontSize: 12, color: '#b91c1c'
  },
  // Modal
  modalOverlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(15,23,42,0.45)', display: 'flex', justifyContent: 'center',
    alignItems: 'center', zIndex: 2000, backdropFilter: 'blur(2px)'
  },
  modal: {
    background: '#fff', width: '85%', maxWidth: 700, maxHeight: '80vh',
    borderRadius: 14, display: 'flex', flexDirection: 'column',
    boxShadow: '0 20px 60px rgba(0,0,0,0.15)', animation: 'kbModalIn 0.25s ease-out'
  },
  modalHeader: {
    padding: '16px 20px', borderBottom: '1px solid #f1f5f9',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center'
  },
  modalTitle: { margin: 0, fontSize: 16, color: '#1e293b', fontWeight: 700 },
  modalClose: { background: 'none', border: 'none', fontSize: 22, color: '#94a3b8', cursor: 'pointer' },
  modalBody: {
    flex: 1, padding: 20, overflow: 'auto', background: '#f8fafc'
  },
  modalFooter: {
    padding: '12px 20px', borderTop: '1px solid #f1f5f9', display: 'flex', justifyContent: 'flex-end', gap: 8
  },
  // Add modal
  addModeBar: {
    display: 'flex', gap: 0, marginBottom: 16, background: '#f1f5f9', borderRadius: 8, padding: 3
  },
  addModeBtn: (active) => ({
    flex: 1, padding: '7px 14px', border: 'none', borderRadius: 6,
    background: active ? '#fff' : 'transparent', color: active ? '#0ea5e9' : '#64748b',
    fontWeight: active ? 600 : 400, fontSize: 12, cursor: 'pointer', boxShadow: active ? '0 1px 3px rgba(0,0,0,0.08)' : 'none'
  }),
  input: {
    width: '100%', padding: '8px 12px', border: '1px solid #e2e8f0', borderRadius: 8,
    fontSize: 13, outline: 'none', color: '#334155', boxSizing: 'border-box'
  },
  textarea: {
    width: '100%', padding: '10px 12px', border: '1px solid #e2e8f0', borderRadius: 8,
    fontSize: 13, outline: 'none', color: '#334155', resize: 'vertical', minHeight: 150,
    boxSizing: 'border-box', fontFamily: 'inherit'
  },
  // QA
  qaContainer: {
    flex: 1, display: 'flex', flexDirection: 'column', background: '#fff',
    borderRadius: 12, overflow: 'hidden', boxShadow: '0 1px 3px rgba(0,0,0,0.04)'
  },
  qaMessages: {
    flex: 1, overflow: 'auto', padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 16
  },
  qaMsg: (role) => ({
    display: 'flex', gap: 10, alignSelf: role === 'user' ? 'flex-end' : 'flex-start',
    maxWidth: '85%'
  }),
  qaAvatar: (role) => ({
    width: 32, height: 32, borderRadius: '50%',
    background: role === 'user' ? 'linear-gradient(135deg, #0ea5e9, #06b6d4)' : 'linear-gradient(135deg, #059669, #10b981)',
    color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: 13, fontWeight: 700, flexShrink: 0
  }),
  qaBubble: (role) => ({
    padding: '10px 14px', borderRadius: role === 'user' ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
    background: role === 'user' ? '#e0f2fe' : '#f1f5f9',
    color: '#1e293b', fontSize: 13, lineHeight: 1.55, wordBreak: 'break-word'
  }),
  qaSources: {
    marginTop: 8, background: '#f8fafc', borderRadius: 8, border: '1px solid #e2e8f0', overflow: 'hidden'
  },
  qaSourcesToggle: {
    width: '100%', padding: '6px 12px', background: 'none', border: 'none',
    cursor: 'pointer', fontSize: 11, color: '#64748b', textAlign: 'left',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center'
  },
  qaSourceItem: {
    padding: '8px 12px', borderTop: '1px solid #f1f5f9', fontSize: 11, color: '#475569',
    display: 'flex', flexDirection: 'column', gap: 3
  },
  qaInputArea: {
    padding: '12px 16px', borderTop: '1px solid #e2e8f0', display: 'flex', gap: 8,
    background: '#f8fafc'
  },
  qaInput: {
    flex: 1, padding: '10px 14px', border: '1px solid #e2e8f0', borderRadius: 24,
    fontSize: 13, outline: 'none', color: '#334155', background: '#fff'
  },
  qaSendBtn: (hasInput) => ({
    padding: '10px 20px', border: 'none', borderRadius: 24,
    background: hasInput ? 'linear-gradient(135deg, #0ea5e9, #06b6d4)' : '#e2e8f0',
    color: hasInput ? '#fff' : '#94a3b8', fontWeight: 700, fontSize: 13,
    cursor: hasInput ? 'pointer' : 'not-allowed',
    transition: 'all 0.2s', whiteSpace: 'nowrap'
  }),
  qaEmpty: {
    flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
    justifyContent: 'center', padding: 40, color: '#94a3b8', fontSize: 14, gap: 8
  },
  qaLoadingDots: {
    display: 'flex', gap: 4, padding: '8px 14px'
  },
  // ── 诊断测试样式 ──
  diagPanel: {
    background: '#fff', borderRadius: 10, padding: 16,
    boxShadow: '0 1px 3px rgba(0,0,0,0.04)', border: '1px solid #f1f5f9'
  },
  diagPanelHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    marginBottom: 12, fontSize: 14, fontWeight: 700, color: '#1e293b'
  },
  diagStatsGrid: {
    display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8
  },
  diagStatCard: {
    background: '#f8fafc', borderRadius: 8, padding: '12px 14px',
    textAlign: 'center', border: '1px solid #e2e8f0'
  },
  diagStatVal: {
    fontSize: 24, fontWeight: 700, color: '#0ea5e9', lineHeight: 1.2
  },
  diagStatLabel: {
    fontSize: 11, color: '#94a3b8', marginTop: 2, fontWeight: 500
  },
  diagDocBar: {
    display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0'
  },
  diagResultItem: {
    background: '#f8fafc', borderRadius: 8, padding: '10px 12px',
    marginBottom: 6, border: '1px solid #f1f5f9'
  },
  diagChunkBlock: {
    background: '#f8fafc', borderRadius: 8, padding: '10px 12px',
    marginBottom: 6, border: '1px solid #e2e8f0'
  },
  diagChunkHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    marginBottom: 6, paddingBottom: 4, borderBottom: '1px solid #f1f5f9'
  },
  diagOverlap: {
    marginTop: 6, fontSize: 11, color: '#8b5cf6', fontStyle: 'italic',
    background: '#f5f3ff', padding: '4px 8px', borderRadius: 4
  },
  dot: (i) => ({
    width: 6, height: 6, borderRadius: '50%', background: '#94a3b8',
    animation: `kbDot 1.4s ${i * 0.2}s ease-in-out infinite`
  })
};

// 共享动画样式（注入到主容器）
export const animations = `
  @keyframes kbSpin { to { transform: rotate(360deg); } }
  @keyframes kbModalIn { from { opacity: 0; transform: scale(0.95) translateY(10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
  @keyframes kbDot { 0%,80%,100% { opacity: 0.3; transform: scale(0.8); } 40% { opacity: 1; transform: scale(1); } }
`;
