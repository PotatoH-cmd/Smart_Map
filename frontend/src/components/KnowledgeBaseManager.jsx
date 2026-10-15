// KnowledgeBaseManager.jsx — 知识库管理容器（按 tab 拆子组件后瘦身）
import { useState, useEffect, useRef } from 'react';
import { styles, animations } from './knowledge/styles';
import { cleanDocName } from './knowledge/common';
import DocManager from './knowledge/DocManager';
import QAPanel from './knowledge/QAPanel';
import DiagnosePanel from './knowledge/DiagnosePanel';
import GraphPanel from './knowledge/GraphPanel';
import DocProcessor from './knowledge/DocProcessor';

const API_BASE_URL = '';

// 文档列表每页条数：文档级实体约 90 篇，分页比虚拟滚动更简单
const PAGE_SIZE = 60;

const KnowledgeBaseManager = () => {
  const [activeTab, setActiveTab] = useState('docs');
  const [documents, setDocuments] = useState([]);
  const [searchText, setSearchText] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  // 分页与文档级统计（总数/切片总数来自服务端，不再由切片列表推算）
  const [kbTotal, setKbTotal] = useState(0);
  const [kbTotalChunks, setKbTotalChunks] = useState(0);
  const [kbPage, setKbPage] = useState(1);

  // 项目文件夹
  const [folders, setFolders] = useState([]);
  const [selectedProject, setSelectedProject] = useState('');

  // QA
  const [qaMessages, setQaMessages] = useState([]);
  const [qaInput, setQaInput] = useState('');
  const [qaLoading, setQaLoading] = useState(false);

  // ── 诊断测试状态 ──
  const [diagSearchQuery, setDiagSearchQuery] = useState('');
  const [diagSearchResults, setDiagSearchResults] = useState(null);
  const [diagSearchLoading, setDiagSearchLoading] = useState(false);
  const [diagTopK, setDiagTopK] = useState(5);
  const [diagChunkText, setDiagChunkText] = useState('');
  const [diagChunkResult, setDiagChunkResult] = useState(null);
  const [diagStats, setDiagStats] = useState(null);
  const [diagStatsLoading, setDiagStatsLoading] = useState(false);

  // ── 图谱可视化状态 ──
  const [graphData, setGraphData] = useState(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState(null);

  useEffect(() => {
    fetchFolders();
    fetchDocuments();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 文档管理 ──

  const fetchDocuments = async (project = selectedProject, page = 1, q = searchText) => {
    setIsLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
      if (project) params.set('project', project);
      if (q && q.trim()) params.set('q', q.trim());
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/documents?${params}`);
      const data = await resp.json();
      if (data.success) {
        // 统一成文档级形状：id 用 doc_id（删除/查看都依赖它）
        setDocuments((data.documents || []).map((d) => ({
          id: d.doc_id,
          doc_id: d.doc_id,
          name: d.name,
          project_name: d.project_name,
          chunk_count: d.chunk_count,
          source_file: d.source_file,
          has_source_file: d.has_source_file,
          source_size: d.source_size,
          stale: d.stale,
          updated_at: d.updated_at,
        })));
        setKbTotal(data.count || 0);
        setKbTotalChunks(data.total_chunks || 0);
        setKbPage(data.page || page);
      } else {
        setError(data.error || data.detail || '获取文档列表失败');
      }
    } catch (err) {
      setError('无法连接到服务器');
    } finally {
      setIsLoading(false);
    }
  };

  const fetchFolders = async () => {
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/folders`);
      const data = await resp.json();
      if (data.success && Array.isArray(data.folders)) setFolders(data.folders);
    } catch (err) {
      /* 文件夹不可用时静默降级 */
    }
  };

  const handleSelectFolder = (project) => {
    setSelectedProject(project);
    fetchDocuments(project, 1, searchText);
  };

  const handleDelete = async (id, e) => {
    e.stopPropagation();
    const target = documents.find((d) => d.id === id);
    const label = target ? `${cleanDocName(target.name) || target.name}（${target.chunk_count} 个切片）` : id;
    if (!window.confirm(`确定要删除这个文档吗？\n\n${label}\n\n该文档的切片与向量会被一并删除。`)) return;
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const data = await resp.json();
      if (data.success) {
        // 整页重拉：删除会改变总数与分页，前端自行剔除会与后端口径不一致
        fetchDocuments(selectedProject, kbPage, searchText);
        fetchFolders();
      } else {
        setError(data.error || data.detail || '删除失败');
      }
    } catch (err) {
      setError('删除失败');
    }
  };

  const filterTimer = useRef(null);
  const handleSearch = (text) => {
    setSearchText(text);
    // 搜索改为服务端过滤，防抖 300ms 避免逐字打请求
    if (filterTimer.current) clearTimeout(filterTimer.current);
    filterTimer.current = setTimeout(() => {
      fetchDocuments(selectedProject, 1, text);
    }, 300);
  };

  const handlePageChange = (next) => {
    fetchDocuments(selectedProject, next, searchText);
  };

  const filteredDocs = documents;

  const showFolderView = selectedProject === '' && !searchText.trim() && folders.length > 0;

  // ── 智能问答 ──

  const handleSendQA = async (question) => {
    if (!question || qaLoading) return;

    const userMsg = { role: 'user', content: question, id: Date.now() };
    setQaMessages(prev => [...prev, userMsg]);
    setQaInput('');
    setQaLoading(true);

    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/qa`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, top_k: 5 })
      });
      const data = await resp.json();
      const aiMsg = {
        role: 'assistant',
        content: data.answer || '未能获取回答',
        sources: data.sources || [],
        thinkingSteps: data.thinking_steps || [],
        method: data.method || '',
        id: Date.now() + 1
      };
      setQaMessages(prev => [...prev, aiMsg]);
    } catch (err) {
      const errMsg = { role: 'assistant', content: '请求失败，请检查 Ollama 和 RagFlow 服务是否正常。', sources: [], id: Date.now() + 1 };
      setQaMessages(prev => [...prev, errMsg]);
    } finally {
      setQaLoading(false);
    }
  };

  // ── 图谱可视化 ──

  const fetchGraphData = async () => {
    setGraphLoading(true);
    setGraphError(null);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/graph`);
      const data = await resp.json();
      if (data.success) {
        setGraphData(data.data);
      } else {
        setGraphError(data.error || '获取图谱数据失败');
      }
    } catch (err) {
      setGraphError('无法连接到服务器');
    } finally {
      setGraphLoading(false);
    }
  };

  // ── 诊断测试 ──

  const fetchDiagStats = async () => {
    setDiagStatsLoading(true);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/diagnose/stats`);
      const data = await resp.json();
      if (data.success) setDiagStats(data);
      else setError(data.error || '获取统计失败');
    } catch (err) {
      setError('统计请求失败');
    } finally {
      setDiagStatsLoading(false);
    }
  };

  const handleDiagSearch = async () => {
    if (!diagSearchQuery.trim() || diagSearchLoading) return;
    setDiagSearchLoading(true);
    setDiagSearchResults(null);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/qa`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: diagSearchQuery.trim(), top_k: diagTopK })
      });
      const data = await resp.json();
      setDiagSearchResults({
        query: diagSearchQuery.trim(),
        results: data.sources || [],
        method: data.method || 'unknown',
        answer: data.answer || ''
      });
    } catch (err) {
      setDiagSearchResults({ query: diagSearchQuery.trim(), results: [], method: '', error: '请求失败' });
    } finally {
      setDiagSearchLoading(false);
    }
  };

  const handleDiagChunk = () => {
    const text = diagChunkText;
    if (!text.trim()) return;
    const CHUNK_SIZE = 512;
    const OVERLAP = 64;
    const sentences = text.split(/(?<=[。！？;.!?\n])/);
    const chunks = [];
    let current = '';
    for (const s of sentences) {
      if (current.length + s.length <= CHUNK_SIZE) {
        current += s;
      } else {
        if (current.trim()) chunks.push(current.trim());
        const overlap = current.length >= OVERLAP ? current.slice(-OVERLAP) : current;
        current = overlap + s;
      }
    }
    if (current.trim()) chunks.push(current.trim());

    setDiagChunkResult({
      originalLen: text.length,
      chunkCount: chunks.length,
      chunks: chunks.map((c, i) => {
        let overlapText = '';
        if (i > 0 && chunks[i - 1].length >= OVERLAP) {
          const prevEnd = chunks[i - 1].slice(-OVERLAP);
          for (let j = OVERLAP; j > 10; j--) {
            const tail = prevEnd.slice(-j);
            if (c.startsWith(tail)) { overlapText = tail; break; }
          }
        }
        return { index: i + 1, text: c, len: c.length, overlap: overlapText };
      })
    });
  };

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <h2 style={styles.headerTitle}>知识库管理</h2>
        <span style={styles.badge}>RagFlow</span>
      </div>

      {/* Tab Bar */}
      <div style={styles.tabBar}>
        <button style={styles.tab(activeTab === 'docs')} onClick={() => setActiveTab('docs')}>
          文档管理
        </button>
        <button style={styles.tab(activeTab === 'qa')} onClick={() => setActiveTab('qa')}>
          智能问答
        </button>
        <button style={styles.tab(activeTab === 'diagnose')} onClick={() => { setActiveTab('diagnose'); if (!diagStats) fetchDiagStats(); }}>
          诊断测试
        </button>
        <button style={styles.tab(activeTab === 'graph')} onClick={() => { setActiveTab('graph'); if (!graphData) fetchGraphData(); }}>
          图谱可视化
        </button>
        <button style={styles.tab(activeTab === 'process')} onClick={() => setActiveTab('process')}>
          文档处理
        </button>
      </div>

      {error && (
        <div style={styles.errorBar}>
          <span>{error}</span>
          <button style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#b91c1c', fontSize: 16 }} onClick={() => setError(null)}>x</button>
        </div>
      )}

      {activeTab === 'docs' && (
        <DocManager
          documents={documents}
          folders={folders}
          selectedProject={selectedProject}
          searchText={searchText}
          isLoading={isLoading}
          error={null}
          onSelectFolder={handleSelectFolder}
          onDelete={handleDelete}
          onRefresh={() => { fetchDocuments(selectedProject, kbPage, searchText); fetchFolders(); }}
          onSearch={handleSearch}
          onErrorClear={() => setError(null)}
          filteredDocs={filteredDocs}
          showFolderView={showFolderView}
          total={kbTotal}
          totalChunks={kbTotalChunks}
          page={kbPage}
          pageSize={PAGE_SIZE}
          onPageChange={handlePageChange}
        />
      )}

      {activeTab === 'qa' && (
        <QAPanel
          qaMessages={qaMessages}
          qaInput={qaInput}
          setQaInput={setQaInput}
          qaLoading={qaLoading}
          onSend={handleSendQA}
        />
      )}

      {activeTab === 'diagnose' && (
        <DiagnosePanel
          diagStats={diagStats}
          diagStatsLoading={diagStatsLoading}
          onFetchDiagStats={fetchDiagStats}
          diagSearchQuery={diagSearchQuery}
          setDiagSearchQuery={setDiagSearchQuery}
          diagSearchResults={diagSearchResults}
          diagSearchLoading={diagSearchLoading}
          diagTopK={diagTopK}
          setDiagTopK={setDiagTopK}
          onDiagSearch={handleDiagSearch}
          diagChunkText={diagChunkText}
          setDiagChunkText={setDiagChunkText}
          diagChunkResult={diagChunkResult}
          onDiagChunk={handleDiagChunk}
        />
      )}

      {activeTab === 'graph' && (
        <GraphPanel
          graphData={graphData}
          graphLoading={graphLoading}
          graphError={graphError}
          onFetchGraph={fetchGraphData}
        />
      )}

      {activeTab === 'process' && (
        <DocProcessor />
      )}

      <style>{animations}</style>
    </div>
  );
};

export default KnowledgeBaseManager;