// DocManager.jsx — 文档管理 tab（从 KnowledgeBaseManager.jsx 拆分）
import { useState } from 'react';
import { styles } from './styles';
import { formatDate, formatSize, cleanDocName } from './common';

const API_BASE_URL = '';

const DocManager = ({
  documents, folders, selectedProject, searchText, isLoading, error,
  onSelectFolder, onDelete, onRefresh, onSearch, onErrorClear,
  filteredDocs, showFolderView,
  total = 0, totalChunks = 0, page = 1, pageSize = 60, onPageChange = () => {},
}) => {
  const [showAddModal, setShowAddModal] = useState(false);
  const [addMode, setAddMode] = useState('text');
  const [addName, setAddName] = useState('');
  const [addContent, setAddContent] = useState('');
  const [addFile, setAddFile] = useState(null);
  const [addLoading, setAddLoading] = useState(false);
  const [localError, setLocalError] = useState(null);

  // Document viewer
  const [selectedDoc, setSelectedDoc] = useState(null);
  const [docContent, setDocContent] = useState('');
  const [contentLoading, setContentLoading] = useState(false);

  const handleViewDoc = async (doc) => {
    setSelectedDoc(doc);
    setDocContent('');
    setContentLoading(true);
    try {
      // 卡片是文档级实体，必须走 /raw 取原文：
      // 切片级的 /api/knowledge/{id} 只认节点 id，传 document_id 会退到 RagFlow 报 404
      const resp = await fetch(`${API_BASE_URL}/api/knowledge/${encodeURIComponent(doc.id)}/raw`);
      const data = await resp.json();
      if (data.success) {
        setDocContent(data.text || '');
      } else {
        setDocContent('加载失败: ' + (data.error || data.detail || '未知错误'));
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
        setLocalError('文档名称和内容不能为空');
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
          onRefresh();
        } else {
          setLocalError(data.error || '添加失败');
        }
      } catch (err) {
        setLocalError('添加请求失败');
      } finally {
        setAddLoading(false);
      }
    } else {
      if (!addFile) {
        setLocalError('请选择一个文件');
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
          onRefresh();
        } else {
          setLocalError(data.error || '上传失败');
        }
      } catch (err) {
        setLocalError('上传请求失败');
      } finally {
        setAddLoading(false);
      }
    }
  };

  const displayError = error || localError;
  const selectedFolderName = selectedProject
    ? (folders.find(f => f.project === selectedProject) || {}).project_name || selectedProject
    : '';

  return (
    <>
      <div style={styles.infoCard}>
        <span>
          {selectedProject
            ? <>当前文件夹「{selectedFolderName}」共 <b>{total || documents.length}</b> 个文档。点击卡片查看内容，右上角可删除。</>
            : showFolderView
              ? <>知识库共 <b>{folders.length}</b> 个文件夹、<b>{folders.reduce((a, f) => a + (f.count || 0), 0)}</b> 个文档。点击文件夹查看其中的文档。</>
              : <>全部文件夹共 <b>{total || documents.length}</b> 个文档。点击卡片查看内容，右上角可删除。</>}
        </span>
      </div>

      {displayError && (
        <div style={styles.errorBar}>
          <span>{displayError}</span>
          <button style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#b91c1c', fontSize: 16 }}
            onClick={() => { setLocalError(null); onErrorClear(); }}>x</button>
        </div>
      )}

      {folders.length > 0 && (
        <div style={styles.folderBar}>
          <button style={styles.folderChip(selectedProject === '')} onClick={() => onSelectFolder('')}>
            📁 全部
            <span style={styles.folderCount(selectedProject === '')}>{folders.reduce((a, f) => a + (f.count || 0), 0)}</span>
          </button>
          {folders.map(f => (
            <button
              key={f.project || 'none'}
              style={styles.folderChip(selectedProject === f.project)}
              onClick={() => onSelectFolder(f.project)}
            >
              📁 {f.project_name || '未分组'}
              <span style={styles.folderCount(selectedProject === f.project)}>{f.count}</span>
            </button>
          ))}
        </div>
      )}

      <div style={styles.toolbar}>
        <input
          style={styles.searchInput}
          type="text"
          placeholder="搜索文档名称..."
          value={searchText}
          onChange={e => onSearch(e.target.value)}
        />
        <button style={styles.btnPrimary} onClick={() => setShowAddModal(true)}>
          + 添加文档
        </button>
        <button style={styles.btnOutline} onClick={onRefresh} disabled={isLoading}>
          刷新
        </button>
      </div>

      {isLoading ? (
        <div style={styles.loadingState}>
          <div style={styles.spinner} />
          <span>正在加载文档列表...</span>
        </div>
      ) : showFolderView ? (
        <div style={styles.docGrid}>
          {folders.map(f => (
            <div
              key={f.project || 'none'}
              style={styles.folderCard}
              onClick={() => onSelectFolder(f.project)}
            >
              <span style={{ fontSize: 42 }}>📁</span>
              <span style={styles.folderCardName}>{f.project_name || '未分组'}</span>
              <span style={styles.folderCardCount}>{f.count} 个文档</span>
            </div>
          ))}
        </div>
      ) : (
        <>
          <div style={styles.docGrid}>
            {filteredDocs.map((doc, i) => (
              <div key={doc.id || i} style={styles.docCard} onClick={() => handleViewDoc(doc)}>
                <button style={styles.deleteBtn} onClick={(e) => { e.stopPropagation(); onDelete(doc.id, e); }} title="删除">x</button>
                <div style={styles.docName} title={doc.name || ''}>{cleanDocName(doc.name) || '未命名文档'}</div>
                <div style={styles.docMeta}>
                  {doc.stale ? (
                    <span style={styles.staleBadge}>切片已过期</span>
                  ) : (
                    <span style={styles.docStatus('#10b981')}>已入库</span>
                  )}
                  {doc.project_name ? <span style={styles.projectTag}>{doc.project_name}</span> : null}
                  {doc.chunk_count ? <span>{doc.chunk_count} 片</span> : null}
                  {doc.source_size ? <span>{formatSize(doc.source_size)}</span> : null}
                  {doc.updated_at ? <span>{formatDate(doc.updated_at)}</span> : null}
                </div>
              </div>
            ))}
            {filteredDocs.length === 0 && (
              <div style={styles.emptyState}>
                {searchText.trim()
                  ? '未找到匹配的文档。'
                  : selectedProject
                    ? '该项目下暂无文档。'
                    : '暂无已同步的知识文档，点击「添加文档」开始。'}
              </div>
            )}
          </div>
          {total > pageSize && (
            <div style={styles.pager}>
              <button
                style={{ ...styles.pagerBtn, ...(page <= 1 ? styles.pagerBtnDisabled : null) }}
                disabled={page <= 1}
                onClick={() => onPageChange(page - 1)}
              >
                上一页
              </button>
              <span style={styles.pagerInfo}>
                第 {page} / {Math.ceil(total / pageSize)} 页 · 共 {total} 篇文档
                {totalChunks ? ` / ${totalChunks} 个切片` : ''}
              </span>
              <button
                style={{ ...styles.pagerBtn, ...(page >= Math.ceil(total / pageSize) ? styles.pagerBtnDisabled : null) }}
                disabled={page >= Math.ceil(total / pageSize)}
                onClick={() => onPageChange(page + 1)}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}

      {/* Document Viewer Modal */}
      {selectedDoc && (
        <div style={styles.modalOverlay} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>
          <div style={styles.modal} onClick={e => e.stopPropagation()}>
            <div style={styles.modalHeader}>
              <h3 style={styles.modalTitle}>{selectedDoc.name || '文档详情'}</h3>
              <button style={styles.modalClose} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>x</button>
            </div>
            <div style={styles.modalBody}>
              {contentLoading ? (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: 40, gap: 12, color: '#94a3b8' }}>
                  <div style={styles.spinner} />
                  <span>加载文档内容...</span>
                </div>
              ) : (
                <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 13, lineHeight: 1.6, color: '#334155', margin: 0 }}>
                  {docContent || '暂无内容'}
                </pre>
              )}
            </div>
            <div style={styles.modalFooter}>
              <button style={styles.btnOutline} onClick={() => { setSelectedDoc(null); setDocContent(''); }}>关闭</button>
            </div>
          </div>
        </div>
      )}

      {/* Add Document Modal */}
      {showAddModal && (
        <div style={styles.modalOverlay} onClick={() => setShowAddModal(false)}>
          <div style={styles.modal} onClick={e => e.stopPropagation()}>
            <div style={styles.modalHeader}>
              <h3 style={styles.modalTitle}>添加文档</h3>
              <button style={styles.modalClose} onClick={() => setShowAddModal(false)}>x</button>
            </div>
            <div style={styles.modalBody}>
              <div style={styles.addModeBar}>
                <button style={styles.addModeBtn(addMode === 'text')} onClick={() => setAddMode('text')}>
                  文本输入
                </button>
                <button style={styles.addModeBtn(addMode === 'file')} onClick={() => setAddMode('file')}>
                  文件上传
                </button>
              </div>
              {addMode === 'text' ? (
                <>
                  <input
                    style={{ ...styles.input, marginBottom: 12 }}
                    type="text"
                    placeholder="文档名称（必填）"
                    value={addName}
                    onChange={e => setAddName(e.target.value)}
                  />
                  <textarea
                    style={styles.textarea}
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
                    onChange={e => { const file = e.target.files[0]; if (file) setAddFile(file); }}
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
            <div style={styles.modalFooter}>
              <button style={styles.btnOutline} onClick={() => setShowAddModal(false)}>取消</button>
              <button
                style={{ ...styles.btnPrimary, opacity: addLoading ? 0.6 : 1 }}
                onClick={handleAddDocument}
                disabled={addLoading}
              >
                {addLoading ? '提交中...' : '提交'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};

export default DocManager;