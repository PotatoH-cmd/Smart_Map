// DocProcessor.jsx — 文档处理与预览（三栏：原文 / 规则与脚本 / 切片预览）
import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { DEFAULT_RULES, DEFAULT_SCRIPT, processDocument, summarize, parseChunkSegments } from './chunker';
import './docProcessor.css';

const API_BASE_URL = '';
// 左栏原文行高，必须与 .dp-raw 的 line-height 一致，否则按行滚动会偏移
const RAW_LINE_HEIGHT = 20;
const MAX_RENDER_CHUNKS = 400;
// 超过该行数不再逐行渲染（避免极端文档建几十万个 DOM 节点），降级为整段 + 偏移高亮
const MAX_LINE_RENDER = 8000;
// 脚本编辑器行高，必须与 .dp-editor / .dp-gutter 的 line-height 一致
const SCRIPT_LINE_HEIGHT = 18;
// 文档下拉一次取全（后端上限 500），避免被文档管理页的 60/页分页截断
const DOC_LIST_PAGE_SIZE = 500;

const SEG = {
  sliceMode: [['structural', '按章节层级'], ['fixed', '固定长度']],
  tableMode: [['keep', '整块保留'], ['summary', '摘要化'], ['split', '按行拆分']],
  imageMode: [['proxy', '代理前缀'], ['keep', '原路径'], ['strip', '剥离']],
};

const GROUPS = [
  {
    key: 'structure',
    name: '切片结构策略',
    fields: [
      { k: 'sliceMode', label: '切片模式', hint: '章节编号层级 / 固定长度滑窗', seg: true },
      { k: 'breadcrumb', label: '输出章节路径头', hint: '在正文前写入【章节路径】' },
      { k: 'keepSectionNo', label: '路径保留章节编号', hint: '形如「3.1 河道基本情况」' },
      { k: 'includeParentIntro', label: '附带上级引言', hint: '把父章节正文并入本块' },
      { k: 'includeChildToc', label: '附带本节子目', hint: '列出直属一级子标题' },
    ],
  },
  {
    key: 'length',
    name: '长度与拆分',
    fields: [
      { k: 'maxChars', label: '单块字符上限', hint: '超限触发拆分' },
      { k: 'splitOversized', label: '超长块自动拆分', hint: '按段落 / 表格边界切分' },
      { k: 'mergeShortChunks', label: '合并过短块', hint: '并入前一块' },
      { k: 'minChars', label: '过短块阈值', hint: '低于该值标为过短' },
      { k: 'chunkSize', label: '固定模式块大小', hint: '仅固定长度模式生效' },
      { k: 'chunkOverlap', label: '固定模式重叠长度', hint: '仅固定长度模式生效' },
    ],
  },
  {
    key: 'table',
    name: '表格与图片',
    fields: [
      { k: 'tableMode', label: '表格处理', hint: '整块保留 / 自然语言摘要 / 按行拆分', seg: true },
      { k: 'summaryPerSite', label: '综合汇总表逐站点摘要', hint: '每个砂场/采区单独成块，提升单点检索命中' },
      { k: 'repeatTableHeader', label: '拆分时重复表头', hint: '续表保留首行' },
      { k: 'imageMode', label: '图片处理', hint: '代理前缀 / 原路径 / 剥离', seg: true },
    ],
  },
  {
    key: 'text',
    name: '文本归一化',
    fields: [
      { k: 'normalizeWhitespace', label: '折叠连续空白', hint: '多个空格并为单个' },
      { k: 'normalizeSectionNo', label: '章节编号归一化', hint: '「5 . 5 . 3」→「5.5.3」' },
      { k: 'fullWidthToHalf', label: '全角数字字母转半角', hint: '不转换中文标点' },
      { k: 'dropEmptyLines', label: '丢弃空文本行', hint: '清理空元素' },
    ],
  },
];

const DocProcessor = () => {
  const [docList, setDocList] = useState([]);
  const [docListError, setDocListError] = useState(null);
  const [docLoading, setDocLoading] = useState(true);
  const [docId, setDocId] = useState('');
  const [raw, setRaw] = useState(null);
  const [rawLoading, setRawLoading] = useState(false);
  const [rawError, setRawError] = useState(null);

  const [rules, setRules] = useState(DEFAULT_RULES);
  const [script, setScript] = useState(DEFAULT_SCRIPT);
  const [runScript, setRunScript] = useState(DEFAULT_SCRIPT);
  const [open, setOpen] = useState({ structure: true, length: true, table: false, text: false });

  const [activeChunk, setActiveChunk] = useState(null);
  const [highlight, setHighlight] = useState(null);
  const [filter, setFilter] = useState('');
  const [expanded, setExpanded] = useState({});
  const [tableRaw, setTableRaw] = useState({});
  const [pulse, setPulse] = useState(false);

  const rawRef = useRef(null);
  const gutterRef = useRef(null);
  const editorRef = useRef(null);

  // ── 文档目录：按 doc_id 聚合的文档级列表 ──
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // 该页是「下拉选一篇」的用法，必须一次拿全，不能被文档管理页的分页截断
        const resp = await fetch(`${API_BASE_URL}/api/knowledge/documents?page=1&page_size=${DOC_LIST_PAGE_SIZE}`);
        const data = await resp.json();
        if (!alive) return;
        if (data.success) setDocList(data.documents || []);
        else setDocListError(data.error || data.detail || '获取文档目录失败');
      } catch (err) {
        if (alive) setDocListError('无法连接到服务器');
      } finally {
        if (alive) setDocLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  // ── 默认选中：优先挑切片最丰富且登记了源文件的文档，左栏即可见真·未处理原文 ──
  useEffect(() => {
    if (!docList.length) return;
    if (docId && docList.some((d) => d.doc_id === docId)) return;
    const preferred = docList.find((d) => d.has_source_file) || docList[0];
    setDocId(preferred.doc_id);
  }, [docList, docId]);

  const loadRaw = useCallback(async (id) => {
    if (!id) return;
    setRawLoading(true);
    setRawError(null);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/${encodeURIComponent(id)}/raw`);
      const data = await resp.json();
      if (data.success) {
        setRaw(data);
        setActiveChunk(null);
        setHighlight(null);
      } else {
        setRaw(null);
        setRawError(data.error || data.detail || '加载原文失败');
      }
    } catch (err) {
      setRaw(null);
      setRawError('无法连接到服务器');
    } finally {
      setRawLoading(false);
    }
  }, []);

  useEffect(() => { if (docId) loadRaw(docId); }, [docId, loadRaw]);

  // ── 脚本防抖：编辑停顿后再执行，避免每敲一键跑一次 ──
  useEffect(() => {
    const t = setTimeout(() => setRunScript(script), 260);
    return () => clearTimeout(t);
  }, [script]);

  // ── 规则兜底：防止输入框清空后出现 0 / NaN ──
  const effRules = useMemo(() => ({
    ...rules,
    maxChars: Math.max(200, Number(rules.maxChars) || 200),
    minChars: Math.max(0, Number(rules.minChars) || 0),
    chunkSize: Math.max(64, Number(rules.chunkSize) || 512),
    chunkOverlap: Math.max(0, Number(rules.chunkOverlap) || 0),
  }), [rules]);

  // ── 处理管线：规则 → 脚本，任何变更都会重算并刷新右栏 ──
  const result = useMemo(() => {
    if (!raw || !raw.text) return null;
    try {
      return processDocument({
        rawText: raw.text,
        docTitle: raw.title,
        rules: effRules,
        script: runScript,
      });
    } catch (e) {
      return {
        chunks: [],
        stats: summarize([], effRules),
        scriptError: null,
        scriptErrorLine: 0,
        ruleMs: 0,
        scriptMs: 0,
        fatal: (e && e.message) || String(e),
      };
    }
  }, [raw, effRules, runScript]);

  // 重算后闪一下指示灯，提示「已实时刷新」
  useEffect(() => {
    if (!result) return undefined;
    setPulse(true);
    const t = setTimeout(() => setPulse(false), 620);
    return () => clearTimeout(t);
  }, [result]);

  const setRule = (k, v) => setRules((p) => ({ ...p, [k]: v }));

  const chunks = useMemo(() => (result ? result.chunks : []), [result]);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return chunks;
    return chunks.filter((c) =>
      (c.text || '').toLowerCase().includes(q)
      || (c.breadcrumb || '').toLowerCase().includes(q)
      || (c.title || '').toLowerCase().includes(q));
  }, [chunks, filter]);

  // ── 联动：点右栏切片 → 左栏定位并高亮对应原文 ──
  const scrollToLine = useCallback((line) => {
    if (!line || line < 1) return;
    const box = rawRef.current;
    if (!box) return;
    const el = box.querySelector(`[data-line="${line}"]`);
    if (el) {
      // 逐行渲染时以逻辑行元素为准，折行不会让定位偏移
      el.scrollIntoView({ block: 'center' });
      return;
    }
    // 降级模式（整段 <pre>）下退回按行高估算
    box.scrollTop = Math.max(0, (line - 1) * RAW_LINE_HEIGHT - box.clientHeight / 3);
  }, []);

  const handleChunkClick = (c, idx) => {
    setActiveChunk(idx);
    if (!raw) return;
    const text = raw.text;

    // 跳过【…】生成头，取首个正文行在原文中定位
    const bodyLine = (c.text || '').split('\n')
      .map((s) => s.trim())
      .find((s) => s && !s.startsWith('【')) || '';

    let found = null;
    if (bodyLine) {
      const probe = bodyLine.slice(0, 40);
      const at = text.indexOf(probe);
      if (at >= 0) found = { start: at, end: at + bodyLine.length };
    }

    const line = found
      ? text.slice(0, found.start).split('\n').length
      : (c.source_line || 0);

    setHighlight(found ? { ...found, line } : (line ? { line } : null));
    requestAnimationFrame(() => scrollToLine(line));
  };

  // ── 左栏原文（含高亮切片）──
  const rawParts = useMemo(() => {
    const text = raw ? raw.text : '';
    if (!highlight || highlight.start == null || highlight.start < 0) return text;
    const end = Math.min(highlight.end, text.length);
    return [text.slice(0, highlight.start), text.slice(highlight.start, end), text.slice(end)];
  }, [raw, highlight]);

  // ── 逻辑行拆分：行号与 div 严格一一对应，行内折行不影响行号归属 ──
  const lineData = useMemo(() => {
    if (!raw || !raw.text) return null;
    const lines = raw.text.split('\n');
    if (lines.length > MAX_LINE_RENDER) return null; // 超长文档降级
    const offsets = new Array(lines.length);
    let acc = 0;
    for (let i = 0; i < lines.length; i += 1) {
      offsets[i] = acc;
      acc += lines[i].length + 1;
    }
    return { lines, offsets };
  }, [raw]);

  // 高亮范围换算成逻辑行区间（0 基）；仅有行号时整行高亮
  const hitRange = useMemo(() => {
    if (!highlight) return null;
    if (lineData && highlight.start != null && highlight.start >= 0) {
      const { lines, offsets } = lineData;
      const idxOf = (pos) => {
        let lo = 0;
        let hi = lines.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          if (offsets[mid] <= pos) lo = mid;
          else hi = mid - 1;
        }
        return lo;
      };
      const endPos = Math.max(highlight.start, (highlight.end || highlight.start) - 1);
      return { from: idxOf(highlight.start), to: idxOf(endPos), start: highlight.start, end: highlight.end };
    }
    if (highlight.line) return { from: highlight.line - 1, to: highlight.line - 1, start: null, end: null };
    return null;
  }, [highlight, lineData]);

  const stats = result ? result.stats : null;
  const scriptLines = script.split('\n').length;
  // 脚本报错行（1 基，0 表示解析不出行号）
  const scriptErrorLine = result && result.scriptError ? (result.scriptErrorLine || 0) : 0;
  const srcIsFile = raw && raw.source === 'file';

  // 报错时把编辑器滚到出错行，行号栏同步
  useEffect(() => {
    if (!scriptErrorLine) return;
    const ed = editorRef.current;
    if (!ed) return;
    const scroll = Math.max(0, (scriptErrorLine - 1) * SCRIPT_LINE_HEIGHT - ed.clientHeight / 3);
    ed.scrollTop = scroll;
    if (gutterRef.current) gutterRef.current.scrollTop = scroll;
  }, [scriptErrorLine]);

  const renderCtl = (f) => {
    if (f.seg) {
      return (
        <div className="dp-seg">
          {SEG[f.k].map(([v, t]) => (
            <button
              key={v}
              type="button"
              className={'dp-seg-btn' + (rules[f.k] === v ? ' on' : '')}
              onClick={() => setRule(f.k, v)}
            >
              {t}
            </button>
          ))}
        </div>
      );
    }
    if (typeof DEFAULT_RULES[f.k] === 'number') {
      return (
        <input
          className="dp-num"
          type="number"
          min="0"
          value={rules[f.k]}
          onChange={(e) => setRule(f.k, e.target.value === '' ? '' : Number(e.target.value))}
        />
      );
    }
    return (
      <button
        type="button"
        className={'dp-switch' + (rules[f.k] ? ' on' : '')}
        onClick={() => setRule(f.k, !rules[f.k])}
        aria-pressed={!!rules[f.k]}
      />
    );
  };

  return (
    <div className="dp-root">
      {/* ── 顶部工具条 ── */}
      <div className="dp-toolbar">
        <span className="dp-toolbar-title">文档处理与预览</span>
        <select
          className="dp-select"
          value={docId}
          onChange={(e) => setDocId(e.target.value)}
          disabled={docLoading}
        >
          {docList.length === 0 && (
            <option value="">{docLoading ? '正在加载文档目录…' : '（暂无文档）'}</option>
          )}
          {docList.map((d) => (
            <option key={d.doc_id} value={d.doc_id}>
              {d.name} · {d.chunk_count} 片{d.has_source_file ? ' · 有源文件' : ''}
            </option>
          ))}
        </select>
        {docListError && <span className="dp-badge dp-badge-warn">{docListError}</span>}

        {raw && (
          <span
            className={'dp-badge' + (srcIsFile ? '' : ' dp-badge-warn')}
            title={raw.source_file || '该文档未登记源文件，使用库内节点还原'}
          >
            {srcIsFile ? '原文来源：未处理源文件' : '原文来源：库内节点还原'}
          </span>
        )}
        {raw && (
          <span className="dp-badge">
            {raw.char_count.toLocaleString()} 字 / {raw.line_count.toLocaleString()} 行
          </span>
        )}

        <span className="dp-spacer" />

        <span className="dp-live">
          <span className={'dp-live-dot' + (pulse ? ' on' : '')} />
          {result ? `规则 ${result.ruleMs}ms · 脚本 ${result.scriptMs}ms` : '待处理'}
        </span>
        <button className="dp-btn" onClick={() => setRules(DEFAULT_RULES)}>重置规则</button>
        <button className="dp-btn" onClick={() => setScript(DEFAULT_SCRIPT)}>重置脚本</button>
        <button className="dp-btn" onClick={() => loadRaw(docId)} disabled={rawLoading}>重新加载</button>
      </div>

      <div className="dp-grid">
        {/* ══ 左栏：原始文档 ══ */}
        <div className="dp-panel">
          <div className="dp-panel-head">
            <span className="dp-panel-title">原始文档</span>
            {raw && raw.text && !lineData && (
              <span className="dp-panel-note">行数超过 {MAX_LINE_RENDER.toLocaleString()}，已关闭行号</span>
            )}
            <span className="dp-panel-meta">
              {highlight && highlight.line ? (
                <button
                  type="button"
                  className="dp-locate"
                  onClick={() => scrollToLine(highlight.line)}
                  title="点击滚动到该行"
                >
                  已定位第 {highlight.line} 行
                </button>
              ) : '点击右侧切片可定位原文'}
            </span>
          </div>
          <div className="dp-panel-body" ref={rawRef}>
            {rawLoading ? (
              <div className="dp-empty"><div className="dp-loading" /><span>正在加载原文…</span></div>
            ) : rawError ? (
              <div className="dp-empty"><span>{rawError}</span></div>
            ) : !raw ? (
              <div className="dp-empty"><span>请在上方选择一个文档</span></div>
            ) : !raw.text ? (
              <div className="dp-empty"><span>该文档内容为空</span></div>
            ) : lineData ? (
              <div className="dp-raw-lines">
                {lineData.lines.map((ln, i) => {
                  const hit = !!hitRange && i >= hitRange.from && i <= hitRange.to;
                  const lineStart = lineData.offsets[i];
                  let content = ln;
                  if (hit && hitRange.start != null) {
                    const s = Math.max(0, hitRange.start - lineStart);
                    const e = Math.min(ln.length, (hitRange.end || hitRange.start) - lineStart);
                    if (e > s) {
                      content = (
                        <>
                          {ln.slice(0, s)}
                          <mark>{ln.slice(s, e)}</mark>
                          {ln.slice(e)}
                        </>
                      );
                    }
                  }
                  return (
                    // eslint-disable-next-line react/no-array-index-key
                    <div className={'dp-raw-line' + (hit ? ' hit' : '')} data-line={i + 1} key={i}>
                      <span className="dp-raw-ln">{i + 1}</span>
                      <span className="dp-raw-text">{content}</span>
                    </div>
                  );
                })}
              </div>
            ) : Array.isArray(rawParts) ? (
              <pre className="dp-raw">
                {rawParts[0]}
                <mark>{rawParts[1]}</mark>
                {rawParts[2]}
              </pre>
            ) : (
              <pre className="dp-raw">{rawParts}</pre>
            )}
          </div>
        </div>

        {/* ══ 中栏：规则与脚本 ══ */}
        <div className="dp-panel dp-panel-mid">
          <div className="dp-panel-head">
            <span className="dp-panel-title">处理规则与脚本</span>
            <span className="dp-panel-meta">变更即时生效</span>
          </div>
          <div className="dp-panel-body">
            <div className="dp-mid-scroll">
              {GROUPS.map((g) => (
                <div key={g.key} className={'dp-group' + (open[g.key] ? ' open' : '')}>
                  <button
                    type="button"
                    className="dp-group-head"
                    onClick={() => setOpen((p) => ({ ...p, [g.key]: !p[g.key] }))}
                  >
                    <span>{g.name}</span>
                    <span className="dp-group-count">{g.fields.length}</span>
                    <span className="dp-group-caret">▼</span>
                  </button>
                  {open[g.key] && (
                    <div className="dp-group-body">
                      {g.fields.map((f) => (
                        <div className="dp-field" key={f.k}>
                          <span className="dp-field-label">
                            {f.label}
                            <span className="dp-field-hint">{f.hint}</span>
                          </span>
                          <span className="dp-field-ctl">{renderCtl(f)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}

              <div className="dp-sec-label">处理脚本（前端沙箱）</div>
              <div className="dp-script-wrap">
                <div className="dp-gutter" ref={gutterRef}>
                  {Array.from({ length: scriptLines }, (_, i) => (
                    <div className={'dp-gl' + (scriptErrorLine === i + 1 ? ' err' : '')} key={i}>
                      {i + 1}
                    </div>
                  ))}
                </div>
                <textarea
                  ref={editorRef}
                  className="dp-editor"
                  spellCheck={false}
                  value={script}
                  onChange={(e) => setScript(e.target.value)}
                  onScroll={(e) => { if (gutterRef.current) gutterRef.current.scrollTop = e.target.scrollTop; }}
                  onKeyDown={(e) => {
                    if (e.key !== 'Tab') return;
                    e.preventDefault();
                    const el = e.target;
                    const s = el.selectionStart;
                    const en = el.selectionEnd;
                    setScript(script.slice(0, s) + '  ' + script.slice(en));
                    requestAnimationFrame(() => { el.selectionStart = s + 2; el.selectionEnd = s + 2; });
                  }}
                />
              </div>
              <div className="dp-script-foot">
                <span>入参 ctx = {'{ text, docTitle, rules, chunks }'}，需返回 chunks 数组</span>
                <span className="dp-spacer" />
                <span>{scriptLines} 行</span>
              </div>
              {result && result.scriptError && (
                <div className="dp-err">
                  {scriptErrorLine
                    ? `脚本执行失败（第 ${scriptErrorLine} 行），右栏显示的是规则处理结果：`
                    : '脚本执行失败，右栏显示的是规则处理结果：'}
                  {'\n'}{result.scriptError}
                </div>
              )}
              {result && result.fatal && (
                <div className="dp-err">规则处理异常：{result.fatal}</div>
              )}
            </div>
          </div>
        </div>

        {/* ══ 右栏：切片预览 ══ */}
        <div className="dp-panel">
          <div className="dp-panel-head">
            <span className="dp-panel-title">处理后切片预览</span>
            <span className="dp-panel-meta">
              {stats ? `${stats.count} 块` : '—'}
            </span>
          </div>

          {stats && (
            <div className="dp-stats">
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.count}</div>
                <div className="dp-stat-label">块数</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.avgChars}</div>
                <div className="dp-stat-label">平均字符</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.maxChars}</div>
                <div className="dp-stat-label">最长块</div>
              </div>
              <div className={'dp-stat' + (stats.oversized ? ' bad' : '')}>
                <div className={'dp-stat-val' + (stats.oversized ? ' bad' : '')}>{stats.oversized}</div>
                <div className="dp-stat-label">超限块</div>
              </div>
              <div className={'dp-stat' + (stats.undersized ? ' warn' : '')}>
                <div className={'dp-stat-val' + (stats.undersized ? ' warn' : '')}>{stats.undersized}</div>
                <div className="dp-stat-label">过短块</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.tableChunks}</div>
                <div className="dp-stat-label">含表格</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.summaries}</div>
                <div className="dp-stat-label">摘要块</div>
              </div>
              <div className="dp-stat">
                <div className="dp-stat-val">{stats.images}</div>
                <div className="dp-stat-label">图片引用</div>
              </div>
            </div>
          )}

          <div className="dp-panel-body">
            {!result ? (
              <div className="dp-empty"><span>等待处理…</span></div>
            ) : chunks.length === 0 ? (
              <div className="dp-empty"><span>当前规则未产出任何切片</span></div>
            ) : (
              <div className="dp-chunk-list">
                <input
                  className="dp-select"
                  style={{ width: '100%', maxWidth: 'none', marginBottom: 10 }}
                  placeholder="在切片中搜索…"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
                {shown.slice(0, MAX_RENDER_CHUNKS).map((c, i) => {
                  const len = c.text.length;
                  const cls = len > effRules.maxChars ? ' over' : (len < effRules.minChars ? ' short' : '');
                  const isOpen = !!expanded[i];
                  const hasTbl = c.text.includes('<table>');
                  const showRawSrc = !!tableRaw[i];
                  return (
                    <div
                      key={i}
                      className={'dp-chunk' + (activeChunk === i ? ' active' : '')}
                      onClick={() => handleChunkClick(c, i)}
                    >
                      <div className="dp-chunk-head">
                        <span className="dp-chunk-no">#{i + 1}</span>
                        <span className={'dp-tag dp-tag-' + (c.chunk_type || 'custom')}>
                          {{ parent: '父块', child: '子块', cover: '封面', preface: '前言', fixed: '定长', summary: '摘要', custom: '脚本' }[c.chunk_type] || c.chunk_type}
                        </span>
                        {c.section_no ? <span className="dp-chunk-no">{c.section_no}</span> : null}
                        {c.has_table ? <span className="dp-tag dp-tag-table">表</span> : null}
                        {c.image_paths && c.image_paths.length ? (
                          <span className="dp-tag dp-tag-img">图×{c.image_paths.length}</span>
                        ) : null}
                        <span className={'dp-chunk-chars' + cls}>{len} 字</span>
                      </div>
                      {c.breadcrumb ? <div className="dp-chunk-bc">{c.breadcrumb}</div> : null}
                      <div className={'dp-chunk-text' + (isOpen ? ' expanded' : '') + (hasTbl ? ' has-table' : '')}>
                        {hasTbl && showRawSrc ? (
                          <span className="dp-table-src">{c.text}</span>
                        ) : hasTbl ? (
                          parseChunkSegments(c.text).map((seg, si) => (seg.type === 'text' ? (
                            <span className="dp-seg-text" key={si}>{seg.text}</span>
                          ) : (
                            <span className="dp-table-scroll" key={si}>
                              <table className="dp-mini-table">
                                <tbody>
                                  {seg.rows.map((row, ri) => (
                                    <tr key={ri}>
                                      {row.map((cell, ci) => (ri === 0 ? (
                                        <th key={ci} colSpan={cell.colspan}>{cell.text}</th>
                                      ) : (
                                        <td key={ci} colSpan={cell.colspan}>{cell.text}</td>
                                      )))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </span>
                          )))
                        ) : c.text}
                      </div>
                      {hasTbl && (
                        <button
                          type="button"
                          className="dp-chunk-more"
                          onClick={(e) => { e.stopPropagation(); setTableRaw((p2) => ({ ...p2, [i]: !p2[i] })); }}
                        >
                          {showRawSrc ? '渲染表格' : '查看表格源码'}
                        </button>
                      )}
                      {len > 240 && (
                        <button
                          type="button"
                          className="dp-chunk-more"
                          onClick={(e) => { e.stopPropagation(); setExpanded((p) => ({ ...p, [i]: !p[i] })); }}
                        >
                          {isOpen ? '收起' : '展开全文'}
                        </button>
                      )}
                    </div>
                  );
                })}
                <div className="dp-list-foot">
                  {filter.trim()
                    ? `匹配 ${shown.length} / ${chunks.length} 块`
                    : (chunks.length > MAX_RENDER_CHUNKS
                      ? `仅渲染前 ${MAX_RENDER_CHUNKS} 块，共 ${chunks.length} 块`
                      : `共 ${chunks.length} 块`)}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default DocProcessor;
