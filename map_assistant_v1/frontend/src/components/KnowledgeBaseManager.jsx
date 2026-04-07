import React, { useState, useEffect } from 'react';
import axios from 'axios';

const API_BASE_URL = 'http://172.136.16.14:8006';

const KnowledgeBaseManager = () => {
  const [knowledgeList, setKnowledgeList] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  
  // New state for viewing document content
  const [selectedDoc, setSelectedDoc] = useState(null);
  const [isViewing, setIsViewing] = useState(false);
  const [contentLoading, setContentLoading] = useState(false);

  useEffect(() => {
    fetchKnowledge();
  }, []);

  const fetchKnowledge = async () => {
    setIsLoading(true);
    try {
      const response = await axios.get(`${API_BASE_URL}/api/knowledge`);
      if (response.data.success) {
        setKnowledgeList(response.data.data);
      } else {
        setError(response.data.error || '获取知识库失败');
      }
    } catch (err) {
      setError('无法连接到服务器');
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleDelete = async (id, e) => {
    e.stopPropagation(); // Prevent triggering the card click
    if (!window.confirm('确定要删除这个知识点吗？')) return;

    setIsLoading(true);
    try {
      const response = await axios.delete(`${API_BASE_URL}/api/knowledge/${id}`);
      if (response.data.success) {
        fetchKnowledge();
      } else {
        setError(response.data.error || '删除失败');
      }
    } catch (err) {
      setError('删除失败');
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleTopicClick = async (topic) => {
    // 兼容处理：如果 topic 是字符串，说明是旧数据或 API 返回格式不符，无法获取 ID
    if (!topic || typeof topic !== 'object' || !topic.id) {
      console.error('Invalid topic object:', topic);
      setError('无法获取文档 ID，请刷新列表重试');
      return;
    }

    setIsViewing(true);
    setContentLoading(true);
    setSelectedDoc({ ...topic, content: '' }); // Reset content while loading

    try {
      const response = await axios.get(`${API_BASE_URL}/api/knowledge/${topic.id}`);
      if (response.data.success) {
        setSelectedDoc({
          ...topic,
          content: response.data.content
        });
      } else {
        setError(response.data.error || '获取文档内容失败');
      }
    } catch (err) {
      setError('获取文档内容失败');
      console.error(err);
    } finally {
      setContentLoading(false);
    }
  };

  const closeViewer = () => {
    setIsViewing(false);
    setSelectedDoc(null);
  };

  return (
    <div className="kb-manager">
      <div className="kb-header">
        <div className="header-title">
          <h2>知识库管理</h2>
          <span className="kb-status-tag">Dify 外挂模式</span>
        </div>
      </div>

      <div className="kb-info-card">
        <div className="info-icon">💡</div>
        <div className="info-content">
          <h4>关于 Dify 知识库</h4>
          <p>当前系统已接入 Dify 外部知识库。所有添加的文档将自动同步至 Dify 平台，并进行高精度的向量化处理，以供地图助手检索使用。</p>
        </div>
      </div>

      {error && (
        <div className="kb-error-alert">
          <span className="error-icon">⚠️</span>
          {error}
          <button className="close-alert" onClick={() => setError(null)}>×</button>
        </div>
      )}

      <div className="kb-main-content">
        <div className="content-header">
          <h3>已有知识主题 ({knowledgeList.length})</h3>
          <button className="btn-refresh" onClick={fetchKnowledge} disabled={isLoading}>
            刷新列表
          </button>
        </div>
        
        <div className="kb-grid-container">
          {isLoading ? (
            <div className="kb-loading-state">
              <div className="spinner"></div>
              <p>正在获取 Dify 数据...</p>
            </div>
          ) : (
            <div className="kb-topic-grid">
              {knowledgeList.map((topic, index) => (
                <div 
                  key={index} 
                  className="topic-card"
                  onClick={() => handleTopicClick(topic)}
                >
                  <div className="topic-icon">📄</div>
                  <div className="topic-info">
                    <div className="topic-name">{topic.name || topic}</div>
                    <div className="topic-meta">Dify 托管文档</div>
                  </div>
                  <div className="topic-actions">
                    <span className="status-online">已索引</span>
                  </div>
                </div>
              ))}
              {knowledgeList.length === 0 && (
                <div className="kb-empty-state">
                  <div className="empty-icon">📂</div>
                  <p>暂无已同步的知识文档</p>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Document Viewer Modal */}
      {isViewing && (
        <div className="modal-overlay" onClick={closeViewer}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{selectedDoc?.name || selectedDoc?.title || '文档详情'}</h3>
              <button className="btn-close" onClick={closeViewer}>×</button>
            </div>
            <div className="modal-body">
              {contentLoading ? (
                <div className="modal-loading">
                  <div className="spinner"></div>
                  <p>正在加载文档内容...</p>
                </div>
              ) : (
                <div className="doc-content-view">
                  {selectedDoc?.content ? (
                    <pre className="doc-text">{selectedDoc.content}</pre>
                  ) : (
                    <p className="no-content">暂无内容或加载失败</p>
                  )}
                </div>
              )}
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={closeViewer}>关闭</button>
            </div>
          </div>
        </div>
      )}

      <style jsx>{`
        .kb-manager {
          padding: 24px;
          height: 100%;
          display: flex;
          flex-direction: column;
          background: #f0f2f5;
          overflow-y: auto;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial;
          position: relative;
        }

        .kb-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 20px;
        }

        .header-title {
          display: flex;
          align-items: center;
          gap: 12px;
        }

        .header-title h2 {
          margin: 0;
          font-size: 24px;
          color: #1f1f1f;
          font-weight: 600;
        }

        .kb-status-tag {
          background: #e6f7ff;
          color: #1890ff;
          border: 1px solid #91d5ff;
          padding: 2px 10px;
          border-radius: 4px;
          font-size: 12px;
        }

        .kb-info-card {
          background: #fffbe6;
          border: 1px solid #ffe58f;
          padding: 16px;
          border-radius: 8px;
          display: flex;
          gap: 16px;
          margin-bottom: 24px;
        }

        .info-icon {
          font-size: 24px;
        }

        .info-content h4 {
          margin: 0 0 4px 0;
          color: #856404;
        }

        .info-content p {
          margin: 0;
          font-size: 14px;
          color: #666;
          line-height: 1.5;
        }

        .kb-error-alert {
          background: #fff2f0;
          border: 1px solid #ffccc7;
          color: #ff4d4f;
          padding: 12px 16px;
          border-radius: 6px;
          margin-bottom: 20px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
        }

        .close-alert {
          background: none;
          border: none;
          color: #ff4d4f;
          font-size: 20px;
          cursor: pointer;
        }

        .kb-main-content {
          background: white;
          padding: 24px;
          border-radius: 12px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.05);
          flex: 1;
          display: flex;
          flex-direction: column;
        }

        .content-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 20px;
        }

        .content-header h3 {
          margin: 0;
          font-size: 18px;
        }

        .btn-refresh {
          background: none;
          border: 1px solid #d9d9d9;
          padding: 4px 12px;
          border-radius: 4px;
          cursor: pointer;
          font-size: 13px;
        }

        .btn-refresh:hover {
          color: #1890ff;
          border-color: #1890ff;
        }

        .kb-grid-container {
          flex: 1;
          overflow-y: auto;
        }

        .kb-topic-grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
          gap: 16px;
        }

        .topic-card {
          border: 1px solid #f0f0f0;
          padding: 16px;
          border-radius: 8px;
          display: flex;
          align-items: center;
          gap: 12px;
          transition: all 0.3s;
          background: #fafafa;
          cursor: pointer;
        }

        .topic-card:hover {
          border-color: #1890ff;
          box-shadow: 0 2px 8px rgba(24,144,255,0.1);
          background: white;
          transform: translateY(-2px);
        }

        .topic-icon {
          font-size: 24px;
        }

        .topic-info {
          flex: 1;
        }

        .topic-name {
          font-weight: 500;
          color: #262626;
          margin-bottom: 4px;
        }

        .topic-meta {
          font-size: 12px;
          color: #8c8c8c;
        }

        .status-online {
          font-size: 11px;
          color: #52c41a;
          background: #f6ffed;
          border: 1px solid #b7eb8f;
          padding: 2px 8px;
          border-radius: 10px;
        }

        .kb-loading-state {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 60px;
          color: #8c8c8c;
        }

        .spinner {
          width: 32px;
          height: 32px;
          border: 3px solid #f3f3f3;
          border-top: 3px solid #1890ff;
          border-radius: 50%;
          animation: spin 1s linear infinite;
          margin-bottom: 16px;
        }

        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }

        .kb-empty-state {
          grid-column: 1 / -1;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 80px;
          color: #8c8c8c;
        }

        .empty-icon {
          font-size: 48px;
          margin-bottom: 16px;
        }

        /* Modal Styles */
        .modal-overlay {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(0, 0, 0, 0.5);
          display: flex;
          justify-content: center;
          align-items: center;
          z-index: 1000;
          backdrop-filter: blur(2px);
        }

        .modal-content {
          background: white;
          width: 80%;
          max-width: 800px;
          height: 80vh;
          border-radius: 12px;
          box-shadow: 0 4px 24px rgba(0, 0, 0, 0.15);
          display: flex;
          flex-direction: column;
          animation: modalFadeIn 0.3s ease-out;
        }

        @keyframes modalFadeIn {
          from { opacity: 0; transform: scale(0.95); }
          to { opacity: 1; transform: scale(1); }
        }

        .modal-header {
          padding: 16px 24px;
          border-bottom: 1px solid #f0f0f0;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }

        .modal-header h3 {
          margin: 0;
          font-size: 18px;
          color: #1f1f1f;
        }

        .btn-close {
          background: none;
          border: none;
          font-size: 24px;
          color: #999;
          cursor: pointer;
          transition: color 0.3s;
        }

        .btn-close:hover {
          color: #333;
        }

        .modal-body {
          flex: 1;
          padding: 24px;
          overflow-y: auto;
          background: #fafafa;
        }

        .modal-loading {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100%;
          color: #8c8c8c;
        }

        .doc-content-view {
          background: white;
          padding: 24px;
          border-radius: 8px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
          min-height: 200px;
        }

        .doc-text {
          white-space: pre-wrap;
          font-family: inherit;
          font-size: 14px;
          line-height: 1.6;
          color: #262626;
          margin: 0;
        }

        .no-content {
          text-align: center;
          color: #999;
          margin-top: 40px;
        }

        .modal-footer {
          padding: 16px 24px;
          border-top: 1px solid #f0f0f0;
          display: flex;
          justify-content: flex-end;
        }

        .btn-secondary {
          background: white;
          border: 1px solid #d9d9d9;
          color: #666;
          padding: 8px 24px;
          border-radius: 4px;
          cursor: pointer;
          transition: all 0.3s;
        }

        .btn-secondary:hover {
          color: #1890ff;
          border-color: #1890ff;
        }
      `}</style>
    </div>
  );
};

export default KnowledgeBaseManager;