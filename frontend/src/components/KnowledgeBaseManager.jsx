import React, { useState, useEffect, useRef } from 'react';
import ReactECharts from 'echarts-for-react';

const API_BASE_URL = '';

const KnowledgeBaseManager = () => {
  const [activeTab, setActiveTab] = useState('docs');
  const [documents, setDocuments] = useState([]);
  const [searchText, setSearchText] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);

  // 项目文件夹
  const [folders, setFolders] = useState([]);
  const [selectedProject, setSelectedProject] = useState(''); // ''=全部

  // Document viewer
  const [selectedDoc, setSelectedDoc] = useState(null);
  const [docContent, setDocContent] = useState('');
  const [contentLoading, setContentLoading] = useState(false);

  // Add document modal
  const [showAddModal, setShowAddModal] = useState(false);
  const [addMode, setAddMode] = useState('text'); // 'text' | 'file'
  const [addName, setAddName] = useState('');
  const [addContent, setAddContent] = useState('');
  const [addFile, setAddFile] = useState(null);
  const [addLoading, setAddLoading] = useState(false);

  // QA
  const [qaMessages, setQaMessages] = useState([]);
  const [qaInput, setQaInput] = useState('');
  const [qaLoading, setQaLoading] = useState(false);
  const qaEndRef = useRef(null);

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
  }, []);

  useEffect(() => {
    if (qaEndRef.current) {
      qaEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [qaMessages]);

  const fetchDocuments = async (project = selectedProject) => {
    setIsLoading(true);
    try {
      const url = project
        ? `${API_BASE_URL}/api/knowledge?project=${encodeURIComponent(project)}`
        : `${API_BASE_URL}/api/knowledge`;
      const resp = await fetch(url);
      const data = await resp.json();
      if (data.success) {
        setDocuments(data.data || []);
        if (Array.isArray(data.folders)) setFolders(data.folders);
      } else {
        setError(data.error || '获取文档列表失败');
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
      /* 文件夹不可用时静默降级（仍可展示全部文档） */
    }
  };

  const handleSelectFolder = (project) => {
    setSelectedProject(project);
    fetchDocuments(project);
  };

  const handleDelete = async (id, e) => {
    e.stopPropagation();
    if (!window.confirm('确定要删除这个文档吗？')) return;
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/${id}`, { method: 'DELETE' });
      const data = await resp.json();
      if (data.success) {
        setDocuments(prev => prev.filter(d => d.id !== id));
      } else {
        setError(data.error || '删除失败');
      }
    } catch (err) {
      setError('删除失败');
    }
  };

  const handleViewDoc = async (doc) => {
    setSelectedDoc(doc);
    setDocContent('');
    setContentLoading(true);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/${doc.id}`);
      const data = await resp.json();
      if (data.success) {
        setDocContent(data.content || '');
      } else {
        setDocContent('加载失败: ' + (data.error || ''));
      }
    } catch (err) {
      setDocContent('加载内容失败');
    } finally {
      setContentLoading(false);
    }
  };

  const handleAddDocument = async () => {
    if (addMode === 'text') {
      if (!addName.trim() || !addContent.trim()) {
        setError('文档名称和内容不能为空');
        return;
      }
      setAddLoading(true);
      try {
        const resp = await fetch(`${API_BASE_URL}/api/knowledge/add`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: addName.trim(), content: addContent })
        });
        const data = await resp.json();
        if (data.success) {
          setShowAddModal(false);
          setAddName('');
          setAddContent('');
          setAddFile(null);
          fetchDocuments();
        } else {
          setError(data.error || '添加失败');
        }
      } catch (err) {
        setError('添加请求失败');
      } finally {
        setAddLoading(false);
      }
    } else {
      if (!addFile) {
        setError('请选择一个文件');
        return;
      }
      setAddLoading(true);
      try {
        const formData = new FormData();
        formData.append('file', addFile);
        const resp = await fetch(`${API_BASE_URL}/api/knowledge/upload`, {
          method: 'POST',
          body: formData
        });
        const data = await resp.json();
        if (data.success) {
          setShowAddModal(false);
          setAddName('');
          setAddContent('');
          setAddFile(null);
          fetchDocuments();
        } else {
          setError(data.error || '上传失败');
        }
      } catch (err) {
        setError('上传请求失败');
      } finally {
        setAddLoading(false);
      }
    }
  };

  const handleSendQA = async () => {
    const question = qaInput.trim();
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
  // ── 图谱可视化：获取图谱数据 ──
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


  // ── 诊断测试：统计 ──
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

  // ── 诊断测试：检索 ──
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
      // 直接传 sources 字段，格式: [{title, relevance, snippet}]
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

  // ── 诊断测试：切片预览（客户端模拟，参数与后端一致） ──
  const handleDiagChunk = () => {
    const text = diagChunkText;
    if (!text.trim()) return;
    const CHUNK_SIZE = 512;
    const OVERLAP = 64;
    // 简单句子切分 + 512 字符块
    const sentences = text.split(/(?<=[。！？;.!?\n])/);
    const chunks = [];
    let current = '';
    for (const s of sentences) {
      if (current.length + s.length <= CHUNK_SIZE) {
        current += s;
      } else {
        if (current.trim()) chunks.push(current.trim());
        // 用重叠区填充新块的开始
        const overlap = current.length >= OVERLAP ? current.slice(-OVERLAP) : current;
        current = overlap + s;
      }
    }
    if (current.trim()) chunks.push(current.trim());

    setDiagChunkResult({
      originalLen: text.length,
      chunkCount: chunks.length,
      chunks: chunks.map((c, i) => {
        // 找与上一块的重叠
        let overlapText = '';
        if (i > 0 && chunks[i-1].length >= OVERLAP) {
          const prevEnd = chunks[i-1].slice(-OVERLAP);
          for (let j = OVERLAP; j > 10; j--) {
            const tail = prevEnd.slice(-j);
            if (c.startsWith(tail)) { overlapText = tail; break; }
          }
        }
        return { index: i + 1, text: c, len: c.length, overlap: overlapText };
      })
    });
  };

  const filteredDocs = documents.filter(doc => {
    const name = (doc.name || '').toLowerCase();
    return name.includes(searchText.toLowerCase());
  });

  // 「全部」视图下仅展示文件夹入口，不铺开全部文档；输入搜索词时切回全局文档搜索
  const showFolderView = selectedProject === '' && !searchText.trim() && folders.length > 0;

  const formatDate = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000 || ts);
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  };

  const formatSize = (bytes) => {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
    return (bytes / (1024 * 1024)).toFixed(1) + 'MB';
  };

  const statusLabel = (s) => {
    const map = { '1': '已解析', '2': '解析中', '0': '待处理' };
    return map[String(s)] || s || '未知';
  };
  const statusColor = (s) => {
    const map = { '1': '#10b981', '2': '#f59e0b', '0': '#94a3b8' };
    return map[String(s)] || '#94a3b8';
  };

  // Styles
  const s = {
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
      display: 'inline-block', padding: '1px 7px', borderRadius: 8, fontSize: 10,
      background: '#eef2ff', color: '#6366f1', border: '1px solid #e0e7ff', fontWeight: 600
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
      fontWeight: 600, fontSize: 13, color: '#1e293b', overflow: 'hidden',
      textOverflow: 'ellipsis', whiteSpace: 'nowrap', paddingRight: 24
    },
    docMeta: {
      fontSize: 11, color: '#94a3b8', display: 'flex', gap: 10, flexWrap: 'wrap'
    },
    docStatus: (color) => ({
      display: 'inline-block', padding: '1px 7px', borderRadius: 8,
      fontSize: 10, background: color + '18', color: color, border: `1px solid ${color}33`,
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
    qaSendBtn: {
      padding: '10px 20px', border: 'none', borderRadius: 24,
      background: qaInput.trim() ? 'linear-gradient(135deg, #0ea5e9, #06b6d4)' : '#e2e8f0',
      color: qaInput.trim() ? '#fff' : '#94a3b8', fontWeight: 700, fontSize: 13,
      cursor: qaInput.trim() ? 'pointer' : 'not-allowed',
      transition: 'all 0.2s', whiteSpace: 'nowrap'
    },
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
      fontSize: 10, color: '#94a3b8', marginTop: 2, fontWeight: 500
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
      marginTop: 6, fontSize: 10, color: '#8b5cf6', fontStyle: 'italic',
      background: '#f5f3ff', padding: '4px 8px', borderRadius: 4
    },
    dot: (i) => ({
      width: 6, height: 6, borderRadius: '50%', background: '#94a3b8',
      animation: `kbDot 1.4s ${i * 0.2}s ease-in-out infinite`
    })
  };

  return (
    <div style={s.container}>
      <div style={s.header}>
        <h2 style={s.headerTitle}>知识库管理</h2>
        <span style={s.badge}>RagFlow</span>
      </div>

      {/* Tab Bar */}
      <div style={s.tabBar}>
        <button style={s.tab(activeTab === 'docs')} onClick={() => setActiveTab('docs')}>
          文档管理
        </button>
        <button style={s.tab(activeTab === 'qa')} onClick={() => setActiveTab('qa')}>
          智能问答
        </button>
        <button style={s.tab(activeTab === 'diagnose')} onClick={() => { setActiveTab('diagnose'); if (!diagStats) fetchDiagStats(); }}>
          诊断测试
        </button>
        <button style={s.tab(activeTab === 'graph')} onClick={() => { setActiveTab('graph'); if (!graphData) fetchGraphData(); }}>
          图谱可视化
        </button>
      </div>

      {error && (
        <div style={s.errorBar}>
          <span>{error}</span>
          <button style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#b91c1c', fontSize: 16 }} onClick={() => setError(null)}>x</button>
        </div>
      )}

      {/* ===== TAB: 文档管理 ===== */}
      {activeTab === 'docs' && (
        <>
          <div style={s.infoCard}>
            <span>
              {selectedProject
                ? <>当前文件夹「{(folders.find(f => f.project === selectedProject) || {}).project_name || selectedProject}」共 <b>{documents.length}</b> 个文档。点击卡片查看内容，右上角可删除。</>
                : showFolderView
                  ? <>知识库共 <b>{folders.length}</b> 个文件夹、<b>{folders.reduce((a, f) => a + (f.count || 0), 0)}</b> 个文档。点击文件夹查看其中的文档。</>
                  : <>全部文件夹共 <b>{documents.length}</b> 个文档。点击卡片查看内容，右上角可删除。</>}
            </span>
          </div>

          {folders.length > 0 && (
            <div style={s.folderBar}>
              <button style={s.folderChip(selectedProject === '')} onClick={() => handleSelectFolder('')}>
                📁 全部
                <span style={s.folderCount(selectedProject === '')}>{folders.reduce((a, f) => a + (f.count || 0), 0)}</span>
              </button>
              {folders.map(f => (
                <button
                  key={f.project || 'none'}
                  style={s.folderChip(selectedProject === f.project)}
                  onClick={() => handleSelectFolder(f.project)}
                >
                  📁 {f.project_name || '未分组'}
                  <span style={s.folderCount(selectedProject === f.project)}>{f.count}</span>
                </button>
              ))}
            </div>
          )}

          <div style={s.toolbar}>
            <input
              style={s.searchInput}
              type="text"
              placeholder="搜索文档名称..."
              value={searchText}
              onChange={e => setSearchText(e.target.value)}
            />
            <button style={s.btnPrimary} onClick={() => setShowAddModal(true)}>
              + 添加文档
            </button>
            <button style={s.btnOutline} onClick={fetchDocuments} disabled={isLoading}>
              刷新
            </button>
          </div>

          {isLoading ? (
            <div style={s.loadingState}>
              <div style={s.spinner} />
              <span>正在加载文档列表...</span>
            </div>
          ) : showFolderView ? (
            <div style={s.docGrid}>
              {folders.map(f => (
                <div
                  key={f.project || 'none'}
                  style={s.folderCard}
                  onClick={() => handleSelectFolder(f.project)}
                >
                  <span style={{ fontSize: 42 }}>📁</span>
                  <span style={s.folderCardName}>{f.project_name || '未分组'}</span>
                  <span style={s.folderCardCount}>{f.count} 个文档</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={s.docGrid}>
              {filteredDocs.map((doc, i) => (
                <div key={doc.id || i} style={s.docCard} onClick={() => handleViewDoc(doc)}>
                  <button style={s.deleteBtn} onClick={(e) => handleDelete(doc.id, e)} title="删除">x</button>
                  <div style={s.docName}>{doc.name || '未命名文档'}</div>
                  <div style={s.docMeta}>
                    <span style={s.docStatus(statusColor(doc.status))}>{statusLabel(doc.status)}</span>
                    {doc.project_name ? <span style={s.projectTag}>{doc.project_name}</span> : null}
                    {doc.size ? <span>{formatSize(doc.size)}</span> : null}
                    {doc.created_at ? <span>{formatDate(doc.created_at)}</span> : null}
                  </div>
                </div>
              ))}
              {filteredDocs.length === 0 && (
                <div style={s.emptyState}>
                  {documents.length === 0 ? '暂无已同步的知识文档，点击「添加文档」开始。' : '未找到匹配的文档。'}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* ===== TAB: 智能问答 ===== */}
      {activeTab === 'qa' && (
        <div style={s.qaContainer}>
          <div style={s.qaMessages}>
            {qaMessages.length === 0 && (
              <div style={s.qaEmpty}>
                <span style={{ fontSize: 32 }}>?</span>
                <span>输入问题，从知识库中智能检索并生成回答</span>
              </div>
            )}
            {qaMessages.map((msg) => (
              <div key={msg.id} style={s.qaMsg(msg.role)}>
                <div style={s.qaAvatar(msg.role)}>
                  {msg.role === 'user' ? '我' : 'AI'}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={s.qaBubble(msg.role)}>
                    {msg.content}
                  </div>
                  {msg.thinkingSteps && msg.thinkingSteps.length > 0 && (
                    <ThinkingPanel steps={msg.thinkingSteps} styles={s} />
                  )}
                  {msg.sources && msg.sources.length > 0 && (
                    <SourcePanel sources={msg.sources} styles={s} />
                  )}
                </div>
              </div>
            ))}
            {qaLoading && (
              <div style={s.qaMsg('assistant')}>
                <div style={s.qaAvatar('assistant')}>AI</div>
                <div style={s.qaBubble('assistant')}>
                  <div style={s.qaLoadingDots}>
                    <div style={s.dot(0)} />
                    <div style={s.dot(1)} />
                    <div style={s.dot(2)} />
                  </div>
                </div>
              </div>
            )}
            <div ref={qaEndRef} />
          </div>
          <div style={s.qaInputArea}>
            <input
              style={s.qaInput}
              type="text"
              placeholder="输入问题，搜索知识库..."
              value={qaInput}
              onChange={e => setQaInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSendQA()}
            />
            <button style={s.qaSendBtn} onClick={handleSendQA} disabled={!qaInput.trim() || qaLoading}>
              {qaLoading ? '思考中' : '发送'}
            </button>
          </div>
        </div>
      )}

      {/* ===== TAB: 诊断测试 ===== */}
      {activeTab === 'diagnose' && (
        <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 16 }}>

          {/* ── 索引统计面板 ── */}
          <div style={s.diagPanel}>
            <div style={s.diagPanelHeader}>
              <span>📊 索引概览</span>
              <button style={{ ...s.btnOutline, fontSize: 11, padding: '4px 10px' }} onClick={fetchDiagStats} disabled={diagStatsLoading}>
                {diagStatsLoading ? '加载中...' : '刷新'}
              </button>
            </div>
            {diagStats ? (
              <div style={s.diagStatsGrid}>
                <div style={s.diagStatCard}>
                  <div style={s.diagStatVal}>{diagStats.total_documents}</div>
                  <div style={s.diagStatLabel}>文档数</div>
                </div>
                <div style={s.diagStatCard}>
                  <div style={s.diagStatVal}>{diagStats.total_chunks}</div>
                  <div style={s.diagStatLabel}>Chunk 数</div>
                </div>
                <div style={s.diagStatCard}>
                  <div style={s.diagStatVal}>{diagStats.chunk_size}</div>
                  <div style={s.diagStatLabel}>Chunk Size</div>
                </div>
                <div style={s.diagStatCard}>
                  <div style={s.diagStatVal}>{diagStats.chunk_overlap}</div>
                  <div style={s.diagStatLabel}>Overlap</div>
                </div>
                <div style={s.diagStatCard}>
                  <div style={{ ...s.diagStatVal, fontSize: 18 }}>{diagStats.embed_model}</div>
                  <div style={s.diagStatLabel}>Embed 模型</div>
                </div>
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
                {diagStatsLoading ? '加载中...' : '点击刷新获取索引统计'}
              </div>
            )}
            {diagStats && diagStats.documents && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 8 }}>
                  文档 Chunk 分布 (Top 15)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {diagStats.documents.slice(0, 15).map((d, i) => (
                    <div key={d.id || i} style={s.diagDocBar}>
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
            {diagStats && diagStats.file_sizes && (
              <div style={{ marginTop: 12, fontSize: 11, color: '#94a3b8', display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                {Object.entries(diagStats.file_sizes).map(([fname, size]) => (
                  <span key={fname}>{fname}: {formatSize(size)}</span>
                ))}
              </div>
            )}
          </div>

          {/* ── 检索测试面板 ── */}
          <div style={s.diagPanel}>
            <div style={s.diagPanelHeader}>
              <span>🔍 检索测试</span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input
                style={{ ...s.searchInput, flex: 1 }}
                placeholder="输入检索关键词..."
                value={diagSearchQuery}
                onChange={e => setDiagSearchQuery(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleDiagSearch()}
              />
              <select
                style={{ padding: '8px 8px', border: '1px solid #e2e8f0', borderRadius: 8, fontSize: 12, color: '#475569', background: '#fff', outline: 'none' }}
                value={diagTopK}
                onChange={e => setDiagTopK(Number(e.target.value))}
              >
                {[3, 5, 8, 10, 15].map(k => <option key={k} value={k}>Top-{k}</option>)}
              </select>
              <button style={s.btnPrimary} onClick={handleDiagSearch} disabled={diagSearchLoading || !diagSearchQuery.trim()}>
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
                  const barW = Math.min(Math.round(score * 20), 20);
                  const barColor = score > 0.7 ? '#10b981' : score > 0.5 ? '#f59e0b' : '#94a3b8';
                  return (
                    <div key={i} style={s.diagResultItem}>
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
          <div style={s.diagPanel}>
            <div style={s.diagPanelHeader}>
              <span>🔪 切片预览</span>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>chunk_size=512, overlap=64</span>
            </div>
            <textarea
              style={{ ...s.textarea, minHeight: 100, marginBottom: 8 }}
              placeholder="输入文本查看切片效果..."
              value={diagChunkText}
              onChange={e => setDiagChunkText(e.target.value)}
            />
            <button style={s.btnPrimary} onClick={handleDiagChunk} disabled={!diagChunkText.trim()}>
              预览切片
            </button>

            {diagChunkResult && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 12, color: '#475569', marginBottom: 8, fontWeight: 600 }}>
                  原文 {diagChunkResult.originalLen} 字符 → {diagChunkResult.chunkCount} 个 Chunk
                </div>
                {diagChunkResult.chunks.map((c, i) => (
                  <div key={i} style={s.diagChunkBlock}>
                    <div style={s.diagChunkHeader}>
                      <span style={{ fontWeight: 600, color: '#0ea5e9' }}>Chunk #{c.index}</span>
                      <span style={{ fontSize: 11, color: '#94a3b8' }}>{c.len} 字符</span>
                    </div>
                    <div style={{ fontSize: 12, color: '#334155', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                      {c.text.substring(0, 300)}
                      {c.text.length > 300 && <span style={{ color: '#94a3b8' }}> ... (省略 {c.text.length - 300} 字符)</span>}
                    </div>
                    {c.overlap && (
                      <div style={s.diagOverlap}>
                        ↳ 与前块重叠: ...{c.overlap.substring(0, 50)}...
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}


      {/* ===== TAB: 图谱可视化 ===== */}
      {activeTab === 'graph' && (
        <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={s.diagPanel}>
            <div style={s.diagPanelHeader}>
              <span>📊 知识图谱可视化</span>
              <div style={{ display: 'flex', gap: 8 }}>
                {graphData?.stats && (
                  <span style={{ fontSize: 11, color: '#64748b', display: 'flex', gap: 12 }}>
                    <span>县区: <b style={{ color: '#0ea5e9' }}>{graphData.stats.county_count}</b></span>
                    <span>采砂场: <b style={{ color: '#f59e0b' }}>{graphData.stats.area_count}</b></span>
                  </span>
                )}
                <button
                  style={{ ...s.btnOutline, fontSize: 11, padding: '4px 10px' }}
                  onClick={fetchGraphData}
                  disabled={graphLoading}
                >
                  {graphLoading ? '加载中...' : '刷新'}
                </button>
              </div>
            </div>

            {graphError && (
              <div style={{ background: '#fef2f2', border: '1px solid #fecaca', padding: '8px 12px', borderRadius: 8, marginBottom: 8, color: '#b91c1c', fontSize: 12 }}>
                {graphError}
              </div>
            )}

            {graphLoading && !graphData ? (
              <div style={{ textAlign: 'center', padding: 60, color: '#94a3b8' }}>
                <div style={s.spinner} />
                <span style={{ display: 'block', marginTop: 12, fontSize: 13 }}>正在加载图谱数据...</span>
              </div>
            ) : graphData && graphData.nodes && graphData.nodes.length > 0 ? (
              <div style={{ height: 500, borderRadius: 8, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
                <GraphChart data={graphData} />
              </div>
            ) : graphData ? (
              <div style={{ textAlign: 'center', padding: 60, color: '#94a3b8', fontSize: 14 }}>
                图谱中没有节点数据，请先构建知识图谱。
              </div>
            ) : null}

            {/* 图例 */}
            <div style={{ display: 'flex', gap: 16, marginTop: 10, justifyContent: 'center', fontSize: 12, color: '#64748b' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <div style={{ width: 14, height: 14, borderRadius: '50%', background: '#0ea5e9' }} />
                县区
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <div style={{ width: 14, height: 14, borderRadius: '50%', background: '#f59e0b' }} />
                采砂场
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Document Viewer Modal */}
      {selectedDoc && (
        <div style={s.modalOverlay} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>
          <div style={s.modal} onClick={e => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <h3 style={s.modalTitle}>{selectedDoc.name || '文档详情'}</h3>
              <button style={s.modalClose} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>x</button>
            </div>
            <div style={s.modalBody}>
              {contentLoading ? (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: 40, gap: 12, color: '#94a3b8' }}>
                  <div style={s.spinner} />
                  <span>加载文档内容...</span>
                </div>
              ) : (
                <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 13, lineHeight: 1.6, color: '#334155', margin: 0 }}>
                  {docContent || '暂无内容'}
                </pre>
              )}
            </div>
            <div style={s.modalFooter}>
              <button style={s.btnOutline} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>关闭</button>
            </div>
          </div>
        </div>
      )}

      {/* Add Document Modal */}
      {showAddModal && (
        <div style={s.modalOverlay} onClick={() => setShowAddModal(false)}>
          <div style={s.modal} onClick={e => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <h3 style={s.modalTitle}>添加文档</h3>
              <button style={s.modalClose} onClick={() => setShowAddModal(false)}>x</button>
            </div>
            <div style={s.modalBody}>
              <div style={s.addModeBar}>
                <button style={s.addModeBtn(addMode === 'text')} onClick={() => setAddMode('text')}>
                  文本输入
                </button>
                <button style={s.addModeBtn(addMode === 'file')} onClick={() => setAddMode('file')}>
                  文件上传
                </button>
              </div>
              {addMode === 'text' ? (
                <>
                  <input
                    style={{ ...s.input, marginBottom: 12 }}
                    type="text"
                    placeholder="文档名称（必填）"
                    value={addName}
                    onChange={e => setAddName(e.target.value)}
                  />
                  <textarea
                    style={s.textarea}
                    placeholder="文档正文内容（必填）"
                    value={addContent}
                    onChange={e => setAddContent(e.target.value)}
                  />
                </>
              ) : (
                <div
                  style={{
                    border: '2px dashed #cbd5e1', borderRadius: 12, padding: 40,
                    textAlign: 'center', color: '#94a3b8', cursor: 'pointer',
                    background: addFile ? '#f0fdf4' : '#fff', transition: 'all 0.2s'
                  }}
                  onClick={() => document.getElementById('kb-file-input').click()}
                  onDragOver={e => { e.preventDefault(); e.currentTarget.style.borderColor = '#0ea5e9'; }}
                  onDragLeave={e => { e.currentTarget.style.borderColor = '#cbd5e1'; }}
                  onDrop={e => {
                    e.preventDefault();
                    const file = e.dataTransfer.files[0];
                    if (file) setAddFile(file);
                    e.currentTarget.style.borderColor = '#cbd5e1';
                  }}
                >
                  <input
                    id="kb-file-input"
                    type="file"
                    accept=".pdf,.doc,.docx,.txt,.md"
                    style={{ display: 'none' }}
                    onChange={e => {
                      const file = e.target.files[0];
                      if (file) setAddFile(file);
                    }}
                  />
                  {addFile ? (
                    <div>
                      <div style={{ fontSize: 32, marginBottom: 8 }}>OK</div>
                      <div style={{ color: '#059669', fontWeight: 600 }}>{addFile.name}</div>
                      <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
                        {formatSize(addFile.size)}
                      </div>
                    </div>
                  ) : (
                    <div>
                      <div style={{ fontSize: 32, marginBottom: 8 }}>+</div>
                      <div>点击选择文件或拖拽到此处</div>
                      <div style={{ fontSize: 11, marginTop: 4 }}>支持 PDF / Word / TXT / Markdown</div>
                    </div>
                  )}
                </div>
              )}
            </div>
            <div style={s.modalFooter}>
              <button style={s.btnOutline} onClick={() => setShowAddModal(false)}>取消</button>
              <button
                style={{ ...s.btnPrimary, opacity: addLoading ? 0.6 : 1 }}
                onClick={handleAddDocument}
                disabled={addLoading}
              >
                {addLoading ? '提交中...' : '提交'}
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`
        @keyframes kbSpin { to { transform: rotate(360deg); } }
        @keyframes kbModalIn { from { opacity: 0; transform: scale(0.95) translateY(10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
        @keyframes kbDot { 0%,80%,100% { opacity: 0.3; transform: scale(0.8); } 40% { opacity: 1; transform: scale(1); } }
      `}</style>
    </div>
  );
};

// Thinking steps sub-component - shows the agent's reasoning pipeline
const ThinkingPanel = ({ steps, styles }) => {
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
const SourcePanel = ({ sources, styles }) => {
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


// ── 图谱可视化：力导向图组件 ──
const GraphChart = ({ data }) => {
  const chartRef = useRef(null);

  // 构建 ECharts 配置
  const option = React.useMemo(() => {
    if (!data || !data.nodes || !data.links) return {};

    // 按县区分组计算节点颜色（同一县区用同一色系）
    const countyColors = {};
    const colorPalette = [
      '#0ea5e9', '#06b6d4', '#6366f1', '#8b5cf6', '#a855f7',
      '#ec4899', '#f43f5e', '#ef4444', '#f97316', '#eab308'
    ];
    let colorIdx = 0;

    const nodes = data.nodes.map(node => {
      if (node.category === 0) {
        // County node - assign unique color
        if (!countyColors[node.name]) {
          countyColors[node.name] = colorPalette[colorIdx % colorPalette.length];
          colorIdx++;
        }
        return {
          ...node,
          category: 0,
          symbolSize: node.symbolSize || 45,
          itemStyle: { color: countyColors[node.name] },
          label: { show: true, fontSize: 13, fontWeight: 'bold', color: '#1e293b' },
        };
      } else {
        // MineableArea node - same color as parent county
        const parentColor = countyColors[node.properties?.county] || '#f59e0b';
        return {
          ...node,
          category: 1,
          symbolSize: node.symbolSize || 25,
          itemStyle: { color: parentColor, borderColor: '#fff', borderWidth: 2 },
          label: { show: false },
        };
      }
    });

    const links = data.links.map(link => ({
      ...link,
      lineStyle: { color: '#cbd5e1', width: 1, curveness: 0.15, opacity: 0.6 },
    }));

    return {
      tooltip: {
        trigger: 'item',
        formatter: (params) => {
          if (params.dataType === 'node') {
            const p = params.data.properties || {};
            if (params.data.category === 0) {
              return `<b>${params.name}</b><br/>
                批复采区: ${p.total_approved || '?'} 个<br/>
                实际开采: ${p.total_active || '?'} 个<br/>
                数据来源: ${p.source || 'unknown'}`;
            } else {
              return `<b>${params.name}</b><br/>
                所属县区: ${p.county || '?'}<br/>
                年份: ${p.year || '?'}<br/>
                河流: ${p.river || '?'}<br/>
                许可证: ${p.license_id || '?'}<br/>
                状态: ${p.is_active ? '开采中' : '已停采'}<br/>
                数据来源: ${p.source || 'unknown'}`;
            }
          }
          return `${params.data.source} → ${params.data.target}`;
        },
        backgroundColor: '#fff',
        borderColor: '#e2e8f0',
        textStyle: { color: '#334155', fontSize: 12 },
        extraCssText: 'box-shadow: 0 4px 12px rgba(0,0,0,0.1); border-radius: 8px;',
      },
      legend: { show: false },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          force: {
            repulsion: 400,
            gravity: 0.08,
            edgeLength: [120, 250],
            layoutAnimation: true,
          },
          data: nodes,
          links: links,
          categories: [
            { name: '县区', itemStyle: { color: '#0ea5e9' } },
            { name: '采砂场', itemStyle: { color: '#f59e0b' } },
          ],
          emphasis: {
            focus: 'adjacency',
            lineStyle: { width: 3 },
            itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' },
          },
          scaleLimit: { min: 0.3, max: 3 },
        },
      ],
    };
  }, [data]);

  const onChartReady = (echarts) => {
    chartRef.current = echarts;
  };

  return (
    <ReactECharts
      option={option}
      style={{ width: '100%', height: '100%' }}
      onChartReady={onChartReady}
      opts={{ renderer: 'canvas' }}
    />
  );
};

export default KnowledgeBaseManager;
