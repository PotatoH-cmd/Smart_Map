// DiagnosePanel.jsx — 诊断测试 tab（从 KnowledgeBaseManager.jsx 拆分）
import { styles } from './styles';
import { formatSize } from './common';

const DiagnosePanel = ({
  diagStats, diagStatsLoading, onFetchDiagStats,
  diagSearchQuery, setDiagSearchQuery, diagSearchResults, diagSearchLoading,
  diagTopK, setDiagTopK, onDiagSearch,
  diagChunkText, setDiagChunkText, diagChunkResult, onDiagChunk,
}) => {
  return (
    <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* ── 索引统计面板 ── */}
      <div style={styles.diagPanel}>
        <div style={styles.diagPanelHeader}>
          <span>📊 索引概览</span>
          <button style={{ ...styles.btnOutline, fontSize: 11, padding: '4px 10px' }} onClick={onFetchDiagStats} disabled={diagStatsLoading}>
            {diagStatsLoading ? '加载中...' : '刷新'}
          </button>
        </div>
        {diagStats ? (
          <>
            <div style={styles.diagStatsGrid}>
              <div style={styles.diagStatCard}>
                <div style={styles.diagStatVal}>{diagStats.total_documents}</div>
                <div style={styles.diagStatLabel}>文档数</div>
              </div>
              <div style={styles.diagStatCard}>
                <div style={styles.diagStatVal}>{diagStats.total_chunks}</div>
                <div style={styles.diagStatLabel}>Chunk 数</div>
              </div>
              <div style={styles.diagStatCard}>
                <div style={styles.diagStatVal}>{diagStats.chunk_size}</div>
                <div style={styles.diagStatLabel}>Chunk Size</div>
              </div>
              <div style={styles.diagStatCard}>
                <div style={styles.diagStatVal}>{diagStats.chunk_overlap}</div>
                <div style={styles.diagStatLabel}>Overlap</div>
              </div>
              <div style={styles.diagStatCard}>
                <div style={{ ...styles.diagStatVal, fontSize: 18 }}>{diagStats.embed_model}</div>
                <div style={styles.diagStatLabel}>Embed 模型</div>
              </div>
            </div>
            {diagStats.documents && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 8 }}>
                  文档 Chunk 分布 (Top 15)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {diagStats.documents.slice(0, 15).map((d, i) => (
                    <div key={d.id || i} style={styles.diagDocBar}>
                      <span style={{ flex: 1, fontSize: 11, color: '#334155', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={d.name}>
                        {d.name}
                      </span>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <div style={{
                          width: Math.max(d.chunk_count * 4, 8), height: 14,
                          background: 'linear-gradient(90deg, #06b6d4, #0ea5e9)', borderRadius: 4
                        }} title={`${d.chunk_count} chunks`} />
                        <span style={{ fontSize: 11, color: '#64748b', minWidth: 24, textAlign: 'right' }}>{d.chunk_count}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {diagStats.file_sizes && (
              <div style={{ marginTop: 12, fontSize: 11, color: '#94a3b8', display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                {Object.entries(diagStats.file_sizes).map(([fname, size]) => (
                  <span key={fname}>{fname}: {formatSize(size)}</span>
                ))}
              </div>
            )}
          </>
        ) : (
          <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
            {diagStatsLoading ? '加载中...' : '点击刷新获取索引统计'}
          </div>
        )}
      </div>

      {/* ── 检索测试面板 ── */}
      <div style={styles.diagPanel}>
        <div style={styles.diagPanelHeader}>
          <span>🔍 检索测试</span>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            style={{ ...styles.searchInput, flex: 1 }}
            placeholder="输入检索关键词..."
            value={diagSearchQuery}
            onChange={e => setDiagSearchQuery(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && onDiagSearch()}
          />
          <select
            style={{ padding: '8px 8px', border: '1px solid #e2e8f0', borderRadius: 8, fontSize: 12, color: '#475569', background: '#fff', outline: 'none' }}
            value={diagTopK}
            onChange={e => setDiagTopK(Number(e.target.value))}
          >
            {[3, 5, 8, 10, 15].map(k => <option key={k} value={k}>Top-{k}</option>)}
          </select>
          <button style={styles.btnPrimary} onClick={onDiagSearch} disabled={diagSearchLoading || !diagSearchQuery.trim()}>
            {diagSearchLoading ? '搜索中' : '搜索'}
          </button>
        </div>

        {diagSearchResults && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 10 }}>
              查询: "{diagSearchResults.query}" | 方法: {diagSearchResults.method} | 返回 {diagSearchResults.results.length} 条
            </div>
            {diagSearchResults.error && (
              <div style={{ color: '#ef4444', fontSize: 12, marginBottom: 8 }}>{diagSearchResults.error}</div>
            )}
            {diagSearchResults.results.length === 0 && !diagSearchResults.error && (
              <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>未找到匹配结果</div>
            )}
            {diagSearchResults.results.map((item, i) => {
              const score = item.relevance || 0;
              const barColor = score > 0.7 ? '#10b981' : score > 0.5 ? '#f59e0b' : '#94a3b8';
              return (
                <div key={i} style={styles.diagResultItem}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 12, color: '#1e293b' }}>#{i + 1} {item.title || '未命名'}</span>
                    <span style={{ fontSize: 11, color: barColor, fontWeight: 600 }}>{(score * 100).toFixed(1)}%</span>
                  </div>
                  <div style={{ marginBottom: 4, height: 6, background: '#f1f5f9', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ width: `${Math.round(score * 100)}%`, height: '100%', background: barColor, borderRadius: 3, transition: 'width 0.3s' }} />
                  </div>
                  <div style={{ fontSize: 11, color: '#64748b', lineHeight: 1.5 }}>
                    {(item.snippet || item.content || '').substring(0, 200)}
                    {(item.snippet || item.content || '').length > 200 ? '...' : ''}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── 切片预览面板 ── */}
      <div style={styles.diagPanel}>
        <div style={styles.diagPanelHeader}>
          <span>🔪 切片预览</span>
          <span style={{ fontSize: 11, color: '#94a3b8' }}>chunk_size=512, overlap=64</span>
        </div>
        <textarea
          style={{ ...styles.textarea, minHeight: 100, marginBottom: 8 }}
          placeholder="输入文本查看切片效果..."
          value={diagChunkText}
          onChange={e => setDiagChunkText(e.target.value)}
        />
        <button style={styles.btnPrimary} onClick={onDiagChunk} disabled={!diagChunkText.trim()}>
          预览切片
        </button>

        {diagChunkResult && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, color: '#475569', marginBottom: 8, fontWeight: 600 }}>
              原文 {diagChunkResult.originalLen} 字符 → {diagChunkResult.chunkCount} 个 Chunk
            </div>
            {diagChunkResult.chunks.map((c, i) => (
              <div key={i} style={styles.diagChunkBlock}>
                <div style={styles.diagChunkHeader}>
                  <span style={{ fontWeight: 600, color: '#0ea5e9' }}>Chunk #{c.index}</span>
                  <span style={{ fontSize: 11, color: '#94a3b8' }}>{c.len} 字符</span>
                </div>
                <div style={{ fontSize: 12, color: '#334155', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                  {c.text.substring(0, 300)}
                  {c.text.length > 300 && <span style={{ color: '#94a3b8' }}> ... (省略 {c.text.length - 300} 字符)</span>}
                </div>
                {c.overlap && (
                  <div style={styles.diagOverlap}>
                    ↳ 与前块重叠: ...{c.overlap.substring(0, 50)}...
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default DiagnosePanel;