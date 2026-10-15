// GraphPanel.jsx — 图谱可视化 tab（从 KnowledgeBaseManager.jsx 拆分）
// 支持 8 类节点（县区/河流/可采区/乡镇/行政村/文档/许可证/项目），
// 按类型渲染、tooltip 溯源（文档+章节+原文+预览跳转）、类型多选过滤。
import { useMemo, useRef, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { styles } from './styles';

// ── 节点类型表（顺序/颜色与后端 kg_pipeline.schema 保持一致）──
const NODE_TYPES = [
  { key: 'County', name: '县区', color: '#0ea5e9', size: 46, showLabel: true, fields: [['name', '名称']] },
  { key: 'River', name: '河流', color: '#06b6d4', size: 36, showLabel: true, fields: [['parent', '干流']] },
  { key: 'MineableArea', name: '可采区', color: '#f59e0b', size: 22, showLabel: false, fields: [
    ['raw_name', '源文档原名'], ['kind', '类型'], ['county', '所属县区'], ['river', '河流'], ['year', '年份'], ['license_id', '许可证'],
    ['avg_control_elevation', '平均控制高程(m)'], ['avg_measured_depth', '平均实测深度(m)'],
    ['measurement_count', '测量点数'], ['source', '数据来源'],
  ]},
  { key: 'Town', name: '乡镇', color: '#8b5cf6', size: 20, showLabel: false, fields: [['county', '县区']] },
  { key: 'Village', name: '行政村', color: '#a855f7', size: 16, showLabel: false, fields: [['town', '乡镇'], ['county', '县区']] },
  { key: 'Document', name: '文档', color: '#10b981', size: 30, showLabel: false, fields: [
    ['title', '文档标题'], ['project', '项目'], ['year', '年份'], ['chunk_count', '切片数'],
  ]},
  { key: 'License', name: '许可证', color: '#ef4444', size: 18, showLabel: false, fields: [['year', '年份'], ['county', '发证县区']] },
  { key: 'Project', name: '项目', color: '#64748b', size: 34, showLabel: true, fields: [['doc_count', '文档数'], ['year', '年份']] },
];
const byType = Object.fromEntries(NODE_TYPES.map(t => [t.key, t]));

// 解析溯源 JSON（refs），生成「来源文档 / 章节 / 原文 / 预览链接」
const renderRefs = (refsStr) => {
  if (!refsStr) return null;
  let refs;
  try { refs = JSON.parse(refsStr); } catch { return null; }
  if (!Array.isArray(refs) || refs.length === 0) return null;
  const top = refs.slice(0, 3).map(r => {
    const title = r.title || r.doc_id || '未知文档';
    const section = r.section ? `<br/><span style="color:#64748b">章节：${r.section}</span>` : '';
    const snippet = r.snippet ? `<br/><span style="color:#94a3b8">原文：${r.snippet}</span>` : '';
    const link = r.doc_id
      ? `<br/><a href="/api/knowledge/preview/${r.doc_id}" target="_blank" rel="noopener" style="color:#2563eb;text-decoration:underline">预览原文 →</a>`
      : '';
    return `<div style="margin-top:6px;border-top:1px solid #eef2f7;padding-top:6px"><span style="color:#0d9488">来源</span>：${title}${section}${snippet}${link}</div>`;
  }).join('');
  return `<div style="max-height:220px;overflow:auto">${top}</div>`;
};

// ── 力导向图组件 ──
const GraphChart = ({ data, activeTypes }) => {
  const chartRef = useRef(null);
  const option = useMemo(() => {
    if (!data || !data.nodes || !data.links) return {};
    const countyColors = {};
    const palette = ['#0ea5e9', '#06b6d4', '#f59e0b', '#8b5cf6', '#a855f7', '#10b981', '#ef4444', '#64748b'];
    let idx = 0;
    const visibleNodes = new Set(data.nodes
      .filter(n => activeTypes.includes(n.type))
      .map(n => n.id));

    const nodes = data.nodes
      .filter(n => visibleNodes.has(n.id))
      .map(node => {
        const t = byType[node.type] || byType[node.categoryName] || NODE_TYPES[0];
        if (node.type === 'County') {
          if (!countyColors[node.name]) { countyColors[node.name] = palette[idx % palette.length]; idx++; }
          return {
            ...node, symbolSize: t.size,
            itemStyle: { color: countyColors[node.name], borderColor: '#fff', borderWidth: 2 },
            label: { show: true, fontSize: 12, fontWeight: 'bold', color: '#1e293b' },
          };
        }
        const baseColor = t.color;
        const border = node.type === 'MineableArea' ? (countyColors[node.properties?.county] || baseColor) : '#fff';
        return {
          ...node, symbolSize: t.size,
          itemStyle: { color: baseColor, borderColor: border, borderWidth: node.type === 'MineableArea' ? 3 : 1 },
          label: { show: !!t.showLabel, fontSize: 11, color: '#475569' },
        };
      });

    const nodeIds = new Set(nodes.map(n => n.id));
    const links = data.links
      .filter(l => nodeIds.has(l.source) && nodeIds.has(l.target))
      .map(l => ({ ...l, lineStyle: { color: '#cbd5e1', width: 1, curveness: 0.15, opacity: 0.55 } }));

    const categories = NODE_TYPES.map(t => ({ name: t.name, itemStyle: { color: t.color } }));

    return {
      tooltip: {
        trigger: 'item',
        formatter: (params) => {
          if (params.dataType === 'node') {
            const p = params.data.properties || {};
            const t = byType[params.data.type] || NODE_TYPES[0];
            let rows = `<b style="font-size:13px">${params.name}</b>`;
            if (params.data.type === 'MineableArea' && p.kind === '工程') {
              rows += `<br/><span style="color:#64748b">类型：工程（清理/疏浚，非可采区）</span>`;
            }
            let hasField = false;
            for (const [k, label] of t.fields) {
              const v = p[k];
              if (v === undefined || v === null || v === '' || v === 0) continue;
              if (k === 'source') continue;
              rows += `<br/><span style="color:#64748b">${label}：</span>${v}`;
              hasField = true;
            }
            if (!hasField && p.source) {
              rows += `<br/><span style="color:#64748b">数据来源：</span>${p.source}`;
            }
            rows += renderRefs(params.data.refs) || '';
            return rows;
          }
          return `<b>${params.data.label || params.data.source}</b><br/>${params.data.source} → ${params.data.target}`;
        },
        backgroundColor: '#fff', borderColor: '#e2e8f0',
        textStyle: { color: '#334155', fontSize: 12 },
        extraCssText: 'box-shadow: 0 4px 12px rgba(0,0,0,0.12); border-radius: 8px; max-width: 420px;',
      },
      legend: { show: false },
      series: [{
        type: 'graph', layout: 'force', roam: true, draggable: true,
        force: { repulsion: 380, gravity: 0.08, edgeLength: [110, 240], layoutAnimation: true },
        data: nodes, links, categories,
        emphasis: { focus: 'adjacency', lineStyle: { width: 3 }, itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } },
        scaleLimit: { min: 0.3, max: 3 },
      }],
    };
  }, [data, activeTypes]);

  return <ReactECharts option={option} style={{ width: '100%', height: '100%' }}
    onChartReady={(e) => { chartRef.current = e; }} opts={{ renderer: 'canvas' }} />;
};

// ── 图谱可视化面板 ──
const GraphPanel = ({ graphData, graphLoading, graphError, onFetchGraph }) => {
  const [activeTypes, setActiveTypes] = useState(
    () => ['County', 'River', 'MineableArea']  // 默认展开 县区/河流/采区，避免上千节点卡死
  );
  const toggleType = (key) => {
    setActiveTypes(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]);
  };

  const stats = graphData?.stats || {};
  const by_type = stats.by_type || {};

  return (
    <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={styles.diagPanel}>
        <div style={styles.diagPanelHeader}>
          <span>📊 知识图谱可视化</span>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {stats.node_count !== undefined && (
              <span style={{ fontSize: 11, color: '#64748b', display: 'flex', gap: 12 }}>
                <span>节点: <b style={{ color: '#334155' }}>{stats.node_count}</b></span>
                <span>关系: <b style={{ color: '#334155' }}>{stats.link_count}</b></span>
              </span>
            )}
            <button style={{ ...styles.btnOutline, fontSize: 11, padding: '4px 10px' }}
              onClick={onFetchGraph} disabled={graphLoading}>
              {graphLoading ? '加载中...' : '刷新'}
            </button>
          </div>
        </div>

        {graphError && (
          <div style={{ background: '#fef2f2', border: '1px solid #fecaca', padding: '8px 12px', borderRadius: 8, marginBottom: 8, color: '#b91c1c', fontSize: 12 }}>
            {graphError}
          </div>
        )}

        {/* 类型多选过滤 */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
          {NODE_TYPES.map(t => {
            const on = activeTypes.includes(t.key);
            return (
              <button key={t.key} onClick={() => toggleType(t.key)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 5, border: `1px solid ${on ? t.color : '#e2e8f0'}`,
                  background: on ? `${t.color}18` : '#fff', color: on ? t.color : '#64748b',
                  borderRadius: 999, padding: '3px 10px', fontSize: 11, cursor: 'pointer',
                }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: t.color }} />
                {t.name}
                <span style={{ opacity: 0.6 }}>({by_type[t.key] ?? 0})</span>
              </button>
            );
          })}
        </div>

        {graphLoading && !graphData ? (
          <div style={{ textAlign: 'center', padding: 60, color: '#94a3b8' }}>
            <div style={styles.spinner} />
            <span style={{ display: 'block', marginTop: 12, fontSize: 13 }}>正在加载图谱数据...</span>
          </div>
        ) : graphData && graphData.nodes && graphData.nodes.length > 0 ? (
          <div style={{ height: 500, borderRadius: 8, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
            <GraphChart data={graphData} activeTypes={activeTypes} />
          </div>
        ) : graphData ? (
          <div style={{ textAlign: 'center', padding: 60, color: '#94a3b8', fontSize: 14 }}>
            图谱中没有节点数据，请先构建知识图谱。
          </div>
        ) : null}

        {/* 图例（动态由类型生成，且随过滤实时折叠） */}
        <div style={{ display: 'flex', gap: 14, marginTop: 10, justifyContent: 'center', fontSize: 12, color: '#64748b' }}>
          {NODE_TYPES.filter(t => activeTypes.includes(t.key)).map(t => (
            <div key={t.key} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: t.color,
                border: t.key === 'MineableArea' ? '2px solid ' + (t.color) : 'none' }} />
              {t.name}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default GraphPanel;
