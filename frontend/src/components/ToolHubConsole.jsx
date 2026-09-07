import React, { useCallback, useEffect, useMemo, useState } from 'react';
import './ToolHubConsole.css';

const API_BASE_URL = '';

const PROVIDER_TABS = [
  { key: 'all', label: '全部' },
  { key: 'native', label: 'Native' },
  { key: 'mcp', label: 'MCP' },
  { key: 'http', label: 'HTTP' },
  { key: 'skill', label: 'Skill' },
];

const PROVIDER_COLORS = {
  native: '#6366f1',
  mcp: '#8b5cf6',
  http: '#0891b2',
  skill: '#d97706',
};

/**
 * 工具中台控制台（Dark Ops 风格）
 * - 工具目录：四类 Provider（native/mcp/http/skill）+ 搜索 + 启用开关
 * - 工具详情：参数 schema、意图映射、关键词、约束段、provider 配置
 * - MCP Server 状态卡 / Skill 编排可视化
 * - 调试台：工具试跑（按 schema 生成表单）+ 意图分析试跑
 * 数据来源：主服务反向代理 /api/toolhub/* 与 /api/intent/*
 */
export default function ToolHubConsole() {
  const [tools, setTools] = useState([]);
  const [mcps, setMcps] = useState([]);
  const [loading, setLoading] = useState(false);
  const [reloadMsg, setReloadMsg] = useState('');
  const [providerTab, setProviderTab] = useState('all');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [debugTab, setDebugTab] = useState('tool'); // tool | intent

  // 调试台状态
  const [paramValues, setParamValues] = useState({});
  const [invokeResult, setInvokeResult] = useState(null);
  const [invoking, setInvoking] = useState(false);
  const [intentMsg, setIntentMsg] = useState('查一下信阳天气');
  const [intentView, setIntentView] = useState('map');
  const [intentResult, setIntentResult] = useState(null);
  const [intentLoading, setIntentLoading] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [t, m] = await Promise.all([
        fetch(`${API_BASE_URL}/api/toolhub/tools`).then((r) => r.json()),
        fetch(`${API_BASE_URL}/api/toolhub/mcp/servers`).then((r) => r.json()),
      ]);
      setTools(t?.data || []);
      setMcps(m?.data || []);
    } catch (e) {
      console.error('加载工具目录失败', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const filtered = useMemo(() => {
    return tools.filter((t) => {
      if (providerTab !== 'all' && t.provider !== providerTab) return false;
      if (search) {
        const s = search.toLowerCase();
        return (
          t.name.toLowerCase().includes(s) ||
          (t.description || '').toLowerCase().includes(s) ||
          (t.intents || []).some((i) => i.toLowerCase().includes(s))
        );
      }
      return true;
    });
  }, [tools, providerTab, search]);

  const countBy = (p) => tools.filter((t) => t.provider === p).length;

  const selectTool = (t) => {
    setSelected(t);
    setInvokeResult(null);
    const init = {};
    (t.parameters || []).forEach((p) => { init[p.name] = ''; });
    setParamValues(init);
  };

  const toggleEnabled = async (t, e) => {
    e.stopPropagation();
    try {
      await fetch(`${API_BASE_URL}/api/toolhub/tools/${t.name}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !t.enabled }),
      });
      setTools((prev) => prev.map((x) => (x.name === t.name ? { ...x, enabled: !t.enabled } : x)));
      if (selected?.name === t.name) setSelected((p) => ({ ...p, enabled: !t.enabled }));
    } catch (err) {
      console.error('切换失败', err);
    }
  };

  const reloadConfig = async () => {
    setReloadMsg('重载中...');
    try {
      const r = await fetch(`${API_BASE_URL}/api/toolhub/tools/reload`, { method: 'POST' }).then((r) => r.json());
      setReloadMsg(r?.success ? `✓ 已重载 ${r?.data?.tool_count} 个工具` : '✗ 重载失败');
      await fetchAll();
    } catch (e) {
      setReloadMsg('✗ 重载失败');
    }
    setTimeout(() => setReloadMsg(''), 3000);
  };

  const invokeTool = async () => {
    if (!selected) return;
    setInvoking(true);
    setInvokeResult(null);
    const started = Date.now();
    try {
      const params = {};
      Object.entries(paramValues).forEach(([k, v]) => {
        if (v !== '' && v !== undefined) params[k] = coerceParam(v);
      });
      const r = await fetch(`${API_BASE_URL}/api/toolhub/tools/${selected.name}/invoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ params }),
      }).then((r) => r.json());
      r._client_ms = Date.now() - started;
      setInvokeResult(r);
    } catch (e) {
      setInvokeResult({ success: false, error: String(e) });
    } finally {
      setInvoking(false);
    }
  };

  const runIntent = async () => {
    if (!intentMsg.trim()) return;
    setIntentLoading(true);
    setIntentResult(null);
    try {
      const r = await fetch(`${API_BASE_URL}/api/intent/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: intentMsg, history: [], context: { view: intentView } }),
      }).then((r) => r.json());
      setIntentResult(r);
    } catch (e) {
      setIntentResult({ primary_intent: 'error', task_context: String(e) });
    } finally {
      setIntentLoading(false);
    }
  };

  const selectedSkill = selected?.provider === 'skill' ? selected : null;

  return (
    <div className="thc-root">
      {/* ── 顶部栏 ── */}
      <div className="thc-header">
        <div className="thc-title">
          <div className="thc-logo">🧰</div>
          <div className="thc-title-text">
            <h2>工具中台控制台</h2>
            <div className="thc-subtitle">Tool Hub Console · 统一注册 / 调用 / 观测</div>
            <div className="thc-stats">
              <span className="thc-stat"><b>{tools.length}</b> 全部</span>
              <span className="thc-stat">
                <span className="thc-stat-dot" style={{ background: PROVIDER_COLORS.native }} />
                <b>{countBy('native')}</b> Native
              </span>
              <span className="thc-stat">
                <span className="thc-stat-dot" style={{ background: PROVIDER_COLORS.mcp }} />
                <b>{countBy('mcp')}</b> MCP
              </span>
              <span className="thc-stat">
                <span className="thc-stat-dot" style={{ background: PROVIDER_COLORS.http }} />
                <b>{countBy('http')}</b> HTTP
              </span>
              <span className="thc-stat">
                <span className="thc-stat-dot" style={{ background: PROVIDER_COLORS.skill }} />
                <b>{countBy('skill')}</b> Skill
              </span>
            </div>
          </div>
        </div>
        <div className="thc-header-actions">
          {reloadMsg && <span className="thc-reload-msg">{reloadMsg}</span>}
          <button className="thc-btn" onClick={reloadConfig}>
            ⟳ <span>重载配置</span>
          </button>
          <button className="thc-btn" onClick={fetchAll} disabled={loading}>
            <span>{loading ? '同步中…' : '↻ 刷新'}</span>
          </button>
        </div>
      </div>

      <div className="thc-body">
        {/* ── 左栏：工具目录 + MCP 状态 ── */}
        <div className="thc-left">
          <div className="thc-filters">
            <div className="thc-tabs">
              {PROVIDER_TABS.map((t) => (
                <button
                  key={t.key}
                  className={`thc-tab ${providerTab === t.key ? 'active' : ''}`}
                  onClick={() => setProviderTab(t.key)}
                >
                  {t.label}
                  <span className="thc-tab-count">
                    {t.key === 'all' ? tools.length : countBy(t.key)}
                  </span>
                </button>
              ))}
            </div>
            <div className="thc-search-wrap">
              <span className="thc-search-icon">⌕</span>
              <input
                className="thc-search"
                placeholder="搜索工具名 / 描述 / 意图…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
          </div>

          <div className="thc-tool-list">
            {loading && tools.length === 0 && (
              [0, 1, 2, 3, 4].map((i) => (
                <div key={i} className="thc-skeleton">
                  <div className="thc-skeleton-line w60" />
                  <div className="thc-skeleton-line w85" />
                </div>
              ))
            )}
            {!loading && filtered.map((t, i) => (
              <div
                key={t.name}
                className={`thc-tool-item ${selected?.name === t.name ? 'selected' : ''} ${!t.enabled ? 'disabled' : ''}`}
                style={{ '--stripe': PROVIDER_COLORS[t.provider] || '#64748b', animationDelay: `${Math.min(i * 24, 240)}ms` }}
                onClick={() => selectTool(t)}
              >
                <div className="thc-tool-item-head">
                  <span className="thc-badge" style={{ background: PROVIDER_COLORS[t.provider] || '#64748b' }}>
                    {t.provider}
                  </span>
                  <span className="thc-tool-name">{t.name}</span>
                  <label className="thc-switch" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={!!t.enabled} onChange={(e) => toggleEnabled(t, e)} />
                    <span className="thc-slider" />
                  </label>
                </div>
                <div className="thc-tool-desc">{t.description || '（无描述）'}</div>
                {(t.intents || []).length > 0 && (
                  <div className="thc-tool-intents">
                    {t.intents.map((i2) => <span key={i2} className="thc-intent-chip">{i2}</span>)}
                  </div>
                )}
              </div>
            ))}
            {!loading && filtered.length === 0 && <div className="thc-empty">∅ 无匹配工具</div>}
          </div>

          {/* MCP Server 状态卡 */}
          {mcps.length > 0 && (
            <div className="thc-mcp-section">
              <div className="thc-section-title">MCP Servers</div>
              <div className="thc-mcp-cards">
                {mcps.map((m) => (
                  <div key={m.name} className={`thc-mcp-card ${m.connected ? 'ok' : 'down'}`}>
                    <div className="thc-mcp-head">
                      <span className={`thc-dot ${m.connected ? 'ok' : 'down'}`} />
                      <span className="thc-mcp-name">{m.name}</span>
                      <span className="thc-mcp-transport">{m.transport}</span>
                    </div>
                    <div className="thc-mcp-meta">
                      {m.connected ? `${m.tool_count} 个远端工具已发现` : `未连接 · ${m.error?.slice(0, 52) || '未知错误'}`}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* ── 右栏：详情 + 调试台 ── */}
        <div className="thc-right">
          {!selected ? (
            <div className="thc-empty-panel">
              <span className="thc-empty-icon">⌘</span>
              <span>从左侧选择一个工具，查看详情与在线调试</span>
            </div>
          ) : (
            <>
              {/* 工具详情 */}
              <div className="thc-detail">
                <div className="thc-detail-head">
                  <span className="thc-badge" style={{ background: PROVIDER_COLORS[selected.provider] || '#64748b' }}>
                    {selected.provider}
                  </span>
                  <h3>{selected.name}</h3>
                  <span className={`thc-state ${selected.enabled ? 'on' : 'off'}`}>
                    {selected.enabled ? '● 已启用' : '○ 已禁用'}
                  </span>
                </div>
                <p className="thc-detail-desc">{selected.description || '（无描述）'}</p>

                <div className="thc-detail-grid">
                  <div className="thc-field">
                    <div className="thc-field-label">
                      Intents 意图映射
                      {(selected.intents || []).length > 0 && (
                        <span className="thc-label-count">×{selected.intents.length}</span>
                      )}
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                      {(selected.intents || []).length
                        ? selected.intents.map((i) => <span key={i} className="thc-intent-chip">{i}</span>)
                        : <span className="thc-dim">未关联意图</span>}
                    </div>
                  </div>

                  <div className="thc-field">
                    <div className="thc-field-label">
                      Keywords 关键词路由
                      <span className="thc-label-count">priority {selected.priority}</span>
                    </div>
                    {(selected.keywords || []).length ? (
                      <div>
                        {selected.keywords.map((k) => <span key={k} className="thc-kw-kbd">{k}</span>)}
                      </div>
                    ) : (
                      <span className="thc-dim">未配置关键词</span>
                    )}
                  </div>

                  {selected.constraint && (
                    <div className="thc-field">
                      <div className="thc-field-label">Constraint 参数约束段 · 注入意图分析</div>
                      <pre className="thc-pre">{selected.constraint}</pre>
                    </div>
                  )}

                  {(selected.provider === 'mcp' || selected.provider === 'http') && (
                    <div className="thc-field">
                      <div className="thc-field-label">Provider Config</div>
                      <pre className="thc-pre">{JSON.stringify(selected.config, null, 2)}</pre>
                    </div>
                  )}
                </div>

                {/* Skill 编排可视化 */}
                {selectedSkill && (
                  <div className="thc-field" style={{ marginTop: 18 }}>
                    <div className="thc-field-label">
                      Pipeline 技能编排
                      <span className="thc-label-count">{selectedSkill.config?.steps?.length || 0} steps</span>
                    </div>
                    <div className="thc-skill-steps">
                      {(selectedSkill.config?.steps || []).map((s, i) => (
                        <div key={i} className="thc-skill-step">
                          <div className="thc-skill-step-no">{i + 1}</div>
                          <div className="thc-skill-step-body">
                            <div className="thc-skill-step-tool">{s.tool}</div>
                            <pre className="thc-pre small">{JSON.stringify(s.params, null, 2)}</pre>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* 参数 schema */}
                <div className="thc-field" style={{ marginTop: 18 }}>
                  <div className="thc-field-label">
                    Parameters Schema
                    <span className="thc-label-count">×{(selected.parameters || []).length}</span>
                  </div>
                  {(selected.parameters || []).length ? (
                    <pre className="thc-pre">{JSON.stringify(selected.parameters, null, 2)}</pre>
                  ) : (
                    <span className="thc-dim">无声明参数</span>
                  )}
                </div>
              </div>

              {/* 调试台 */}
              <div className="thc-debug">
                <div className="thc-tabs sub">
                  <button className={`thc-tab ${debugTab === 'tool' ? 'active' : ''}`} onClick={() => setDebugTab('tool')}>
                    ⚡ 工具试跑
                  </button>
                  <button className={`thc-tab ${debugTab === 'intent' ? 'active' : ''}`} onClick={() => setDebugTab('intent')}>
                    ◈ 意图试跑
                  </button>
                </div>

                {debugTab === 'tool' && (
                  <div className="thc-debug-body">
                    <div className="thc-param-form">
                      {(selected.parameters || []).length === 0 && (
                        <div className="thc-dim">该工具无声明参数，将以空参数调用</div>
                      )}
                      {(selected.parameters || []).map((p) => (
                        <div key={p.name} className="thc-param-row">
                          <label>
                            {p.name}
                            {p.required && <span className="thc-required">*</span>}
                            {p.type && <span className="thc-param-type">{p.type}</span>}
                          </label>
                          <input
                            placeholder={p.description || ''}
                            value={paramValues[p.name] ?? ''}
                            onChange={(e) => setParamValues((v) => ({ ...v, [p.name]: e.target.value }))}
                          />
                        </div>
                      ))}
                    </div>
                    <div>
                      <button className="thc-btn primary" onClick={invokeTool} disabled={invoking}>
                        {invoking ? '◌ 执行中…' : `▶ 调用 ${selected.name}`}
                      </button>
                    </div>
                    {invokeResult && (
                      <div className={`thc-result ${invokeResult.success ? 'ok' : 'err'}`}>
                        <div className="thc-result-head">
                          {invokeResult.success ? '✓ 成功' : '✗ 失败'}
                          <span className="thc-result-ms">
                            {invokeResult._client_ms}ms{invokeResult.elapsed_ms ? ` · server ${invokeResult.elapsed_ms}ms` : ''}
                          </span>
                        </div>
                        {invokeResult.message && <div className="thc-result-msg">{invokeResult.message}</div>}
                        {invokeResult.error && <div className="thc-result-err">{invokeResult.error}</div>}
                        <pre className="thc-pre">{JSON.stringify(invokeResult.data ?? invokeResult, null, 2).slice(0, 3000)}</pre>
                      </div>
                    )}
                  </div>
                )}

                {debugTab === 'intent' && (
                  <div className="thc-debug-body">
                    <div className="thc-intent-row">
                      <input
                        className="thc-intent-input"
                        value={intentMsg}
                        onChange={(e) => setIntentMsg(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && runIntent()}
                        placeholder="输入用户消息测试意图识别…"
                      />
                      <select value={intentView} onChange={(e) => setIntentView(e.target.value)}>
                        <option value="map">2D 视图</option>
                        <option value="cesium">3D 视图</option>
                      </select>
                      <button className="thc-btn primary" onClick={runIntent} disabled={intentLoading}>
                        {intentLoading ? '◌ 分析中…' : '分析'}
                      </button>
                    </div>
                    {intentResult && (
                      <div className="thc-result ok">
                        <div className="thc-result-head">
                          <span className="thc-intent-chip big">{intentResult.primary_intent}</span>
                          <div className="thc-confidence">
                            <div className="thc-confidence-bar">
                              <div
                                className="thc-confidence-fill"
                                style={{ width: `${Math.round((intentResult.confidence ?? 0) * 100)}%` }}
                              />
                            </div>
                            <span className="thc-confidence-val">
                              {((intentResult.confidence ?? 0) * 100).toFixed(0)}%
                            </span>
                          </div>
                        </div>
                        {intentResult.task_context && <div className="thc-result-msg">{intentResult.task_context}</div>}
                        {(intentResult.execution_plan || []).length > 0 && (
                          <div className="thc-plan">
                            {intentResult.execution_plan.map((s) => (
                              <div key={s.step_id} className="thc-plan-step">
                                <span className="thc-plan-no">{s.step_id}</span>
                                <span className="thc-plan-tool">{s.tool || '（无工具）'}</span>
                                <code className="thc-plan-params">{JSON.stringify(s.params)}</code>
                              </div>
                            ))}
                          </div>
                        )}
                        <pre className="thc-pre small">{JSON.stringify(intentResult, null, 2).slice(0, 2500)}</pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function coerceParam(v) {
  const s = String(v).trim();
  if (/^-?\d+$/.test(s)) return parseInt(s, 10);
  if (/^-?\d*\.\d+$/.test(s)) return parseFloat(s);
  if (s === 'true') return true;
  if (s === 'false') return false;
  return v;
}
