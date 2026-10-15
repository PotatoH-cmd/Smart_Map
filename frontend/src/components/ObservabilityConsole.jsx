// ObservabilityConsole.jsx — 智能体可观测性面板（本地 SQLite 聚合 + LangFuse 深链）
import { useState, useEffect, useCallback } from 'react';
import './ObservabilityConsole.css';

const fmtDuration = (ms) => {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}min`;
};

const fmtTime = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

const STATUS_META = {
  completed: { label: '完成', cls: 'ok' },
  failed: { label: '失败', cls: 'fail' },
  cancelled: { label: '取消', cls: 'muted' },
  running: { label: '运行中', cls: 'run' },
  awaiting_confirmation: { label: '待确认', cls: 'wait' },
  awaiting_input: { label: '待补参', cls: 'wait' },
};

const EVENT_ICON = {
  status: '💬', intent: '🎯', plan: '📋', tool_start: '⚡', tool_result: '✅',
  verification: '🛡', pending: '⏸', final: '🏁', error: '❌', cancelled: '🚫',
};

const KpiCard = ({ icon, label, value, sub, tone }) => (
  <div className={`obc-kpi ${tone || ''}`}>
    <div className="obc-kpi-icon">{icon}</div>
    <div className="obc-kpi-body">
      <div className="obc-kpi-value">{value}</div>
      <div className="obc-kpi-label">{label}{sub ? <span className="obc-kpi-sub">{sub}</span> : null}</div>
    </div>
  </div>
);

// 单 run 事件瀑布（懒加载）
const RunEventsPanel = ({ runId, onClose }) => {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    fetch(`/api/run/${runId}/events?since=0`)
      .then(r => r.json())
      .then(d => { if (mounted) setEvents(d.events || []); })
      .catch(() => { if (mounted) setError('事件加载失败'); });
    return () => { mounted = false; };
  }, [runId]);

  const maxElapsed = Math.max(1, ...((events || [])
    .map(e => e.elapsed_ms).filter(v => typeof v === 'number' && v > 0)));

  return (
    <div className="obc-events">
      <div className="obc-events-head">
        <span>事件瀑布 · {runId.slice(0, 8)}…</span>
        <button className="obc-events-close" onClick={onClose}>收起 ▲</button>
      </div>
      {error && <div className="obc-events-error">{error}</div>}
      {!events && !error && <div className="obc-events-loading">加载事件流…</div>}
      {(events || []).map((e, i) => (
        <div key={i} className={`obc-event ${e.type === 'error' ? 'is-error' : ''}`}>
          <span className="obc-event-icon">{EVENT_ICON[e.type] || '•'}</span>
          <div className="obc-event-main">
            <div className="obc-event-title">
              {e.title || e.stage || e.type}
              {typeof e.elapsed_ms === 'number' && (
                <span className="obc-event-ms">{fmtDuration(e.elapsed_ms)}</span>
              )}
            </div>
            {e.message && <div className="obc-event-msg">{e.message.slice(0, 160)}{e.message.length > 160 ? '…' : ''}</div>}
            {typeof e.elapsed_ms === 'number' && e.elapsed_ms > 0 && (
              <div className="obc-event-bar-track">
                <div className="obc-event-bar" style={{ width: `${Math.max(4, (e.elapsed_ms / maxElapsed) * 100)}%` }} />
              </div>
            )}
          </div>
          <span className="obc-event-seq">#{e.seq ?? i + 1}</span>
        </div>
      ))}
      {events && events.length === 0 && <div className="obc-events-loading">无事件记录</div>}
    </div>
  );
};

const ObservabilityConsole = () => {
  const [days, setDays] = useState(7);
  const [overview, setOverview] = useState(null);
  const [runs, setRuns] = useState([]);
  const [tools, setTools] = useState([]);
  const [langfuse, setLangfuse] = useState(null);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');
  const [expandedRun, setExpandedRun] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = `days=${days}`;
      const [ov, rn, tl, lf] = await Promise.all([
        fetch(`/api/obs/overview?${qs}`).then(r => r.json()),
        fetch(`/api/obs/runs?limit=100&${qs}`).then(r => r.json()),
        fetch(`/api/obs/tools?${qs}`).then(r => r.json()),
        fetch('/api/obs/langfuse/status').then(r => r.json()),
      ]);
      setOverview(ov.data || null);
      setRuns((rn.data || []).filter(r => !statusFilter || r.status === statusFilter));
      setTools(tl.data || []);
      setLangfuse(lf.data || null);
    } catch (e) {
      console.error('[obs] load failed', e);
    } finally {
      setLoading(false);
    }
  }, [days, statusFilter]);

  useEffect(() => { load(); }, [load]);

  const ov = overview || {};
  const filtered = statusFilter ? runs.filter(r => r.status === statusFilter) : runs;

  return (
    <div className="obc-root">
      <div className="obc-header">
        <div className="obc-title-wrap">
          <div className="obc-logo">📈</div>
          <div>
            <div className="obc-title">智能体观测台</div>
            <div className="obc-subtitle">Run 追踪 · 工具性能 · LLM Trace（LangFuse）</div>
          </div>
        </div>
        <div className="obc-header-right">
          {langfuse && (
            <div className={`obc-lf-badge ${langfuse.enabled ? 'on' : 'off'}`} title={langfuse.hint || langfuse.host}>
              <span className="obc-lf-dot" />
              LangFuse {langfuse.enabled ? '已接入' : '未启用'}
            </div>
          )}
          <div className="obc-days">
            {[1, 7, 30].map(d => (
              <button key={d} className={`obc-days-btn ${days === d ? 'active' : ''}`} onClick={() => setDays(d)}>
                {d === 1 ? '今日' : `${d} 天`}
              </button>
            ))}
          </div>
          <button className="obc-refresh" onClick={load} disabled={loading}>
            {loading ? '刷新中…' : '↻ 刷新'}
          </button>
        </div>
      </div>

      {/* KPI 卡片行 */}
      <div className="obc-kpis">
        <KpiCard icon="🧭" label="总 Run 数" value={ov.total ?? '—'} sub={`${days} 天内`} tone="blue" />
        <KpiCard icon="⚡" label="今日活跃" value={ov.today ?? '—'} sub="条对话" tone="cyan" />
        <KpiCard icon="✅" label="成功率" value={ov.success_rate != null ? `${(ov.success_rate * 100).toFixed(0)}%` : '—'} tone="green" />
        <KpiCard icon="⏱️" label="平均耗时" value={fmtDuration(ov.avg_duration_ms)} tone="purple" />
        <KpiCard icon="📊" label="P95 耗时" value={fmtDuration(ov.p95_duration_ms)} tone="orange" />
      </div>

      <div className="obc-grid">
        {/* 左列：Run 时间线 */}
        <div className="obc-card obc-runs-card">
          <div className="obc-card-head">
            <span>Run 时间线</span>
            <div className="obc-runs-filter">
              {['', 'completed', 'failed', 'running', 'awaiting_confirmation', 'awaiting_input'].map(s => (
                <button
                  key={s}
                  className={`obc-chip ${statusFilter === s ? 'active' : ''}`}
                  onClick={() => setStatusFilter(s)}
                >
                  {s === '' ? '全部' : (STATUS_META[s] || { label: s }).label}
                </button>
              ))}
            </div>
          </div>
          <div className="obc-run-list">
            {filtered.length === 0 && <div className="obc-empty">暂无 Run 记录</div>}
            {filtered.map((r, i) => {
              const meta = STATUS_META[r.status] || { label: r.status, cls: 'muted' };
              return (
                <div
                  key={r.run_id}
                  className="obc-run-item"
                  style={{ animationDelay: `${Math.min(i * 30, 300)}ms` }}
                >
                  <div className="obc-run-row" onClick={() => setExpandedRun(expandedRun === r.run_id ? null : r.run_id)}>
                    <span className={`obc-status obc-status-${meta.cls}`}>{meta.label}</span>
                    <div className="obc-run-msg" title={r.user_message}>
                      {r.user_message || '(空消息)'}
                    </div>
                    <span className="obc-run-intent">{r.intent_name || '—'}</span>
                    <span className="obc-run-dur">{fmtDuration(r.duration_ms)}</span>
                    <span className="obc-run-time">{fmtTime(r.created_at)}</span>
                    {r.langfuse_url && (
                      <a
                        className="obc-run-lf"
                        href={r.langfuse_url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={e => e.stopPropagation()}
                        title="在 LangFuse 中查看 Trace"
                      >
                        ↗
                      </a>
                    )}
                  </div>
                  {expandedRun === r.run_id && (
                    <RunEventsPanel runId={r.run_id} onClose={() => setExpandedRun(null)} />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* 右列：工具性能 + 意图分布 */}
        <div className="obc-right-col">
          <div className="obc-card">
            <div className="obc-card-head"><span>🔧 工具性能</span><span className="obc-card-meta">{tools.length} 个工具</span></div>
            <div className="obc-tool-list">
              {tools.length === 0 && <div className="obc-empty">暂无工具调用记录</div>}
              {tools.map(t => (
                <div key={t.tool} className="obc-tool-item">
                  <div className="obc-tool-line1">
                    <span className="obc-tool-name" title={t.tool}>{t.tool}</span>
                    <span className="obc-tool-calls">{t.calls} 次</span>
                    <span className={`obc-tool-rate ${t.success_rate != null && t.success_rate < 0.8 ? 'low' : ''}`}>
                      {t.success_rate != null ? `${(t.success_rate * 100).toFixed(0)}%` : '—'}
                    </span>
                  </div>
                  <div className="obc-tool-line2">
                    <div className="obc-tool-bar-track">
                      <div
                        className={`obc-tool-bar ${t.success_rate != null && t.success_rate < 0.8 ? 'low' : ''}`}
                        style={{ width: `${(t.success_rate ?? 0) * 100}%` }}
                      />
                    </div>
                    <span className="obc-tool-ms">avg {fmtDuration(t.avg_ms)} · p95 {fmtDuration(t.p95_ms)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="obc-card">
            <div className="obc-card-head"><span>🎯 意图分布</span><span className="obc-card-meta">Top {((ov.top_intents || []).length)}</span></div>
            <div className="obc-intent-list">
              {!(ov.top_intents || []).length && <div className="obc-empty">暂无意图统计</div>}
              {(ov.top_intents || []).map((it, i) => {
                const maxCount = Math.max(...(ov.top_intents || []).map(x => x.count), 1);
                return (
                  <div key={i} className="obc-intent-item">
                    <span className="obc-intent-name">{it.intent}</span>
                    <div className="obc-intent-track">
                      <div className="obc-intent-bar" style={{ width: `${(it.count / maxCount) * 100}%` }} />
                    </div>
                    <span className="obc-intent-count">{it.count}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {langfuse && langfuse.enabled && (
            <a className="obc-card obc-lf-card" href={langfuse.host} target="_blank" rel="noreferrer">
              <div className="obc-lf-card-icon">🔍</div>
              <div>
                <div className="obc-lf-card-title">打开 LangFuse 控制台</div>
                <div className="obc-lf-card-sub">查看 LLM 调用瀑布 · Prompt · Token 消耗 · Trace 深链</div>
              </div>
              <span className="obc-lf-card-arrow">↗</span>
            </a>
          )}
        </div>
      </div>
    </div>
  );
};

export default ObservabilityConsole;