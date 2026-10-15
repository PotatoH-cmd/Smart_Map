// SessionPanel.jsx — 会话管理面板（从 App.jsx 抽离）
import React, { useState, useEffect, useCallback } from 'react';
import { formatTime } from '../utils/chart';

const SessionPanel = ({ show, sessionId, onSwitchSession, onDeleteSession, onRenameSession }) => {
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) setSessions(await res.json());
    } catch (e) {
      console.error('Load sessions failed:', e);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (show) loadSessions();
  }, [show, loadSessions]);

  const startRename = (e, s) => {
    e.stopPropagation();
    setEditingSessionId(s.id);
    setEditingTitle(s.title);
  };

  const commitRename = async (sid) => {
    if (!editingTitle.trim()) { setEditingSessionId(null); return; }
    try {
      await fetch(`/api/sessions/${sid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: editingTitle.trim() }),
      });
      setSessions(prev => prev.map(s => s.id === sid ? { ...s, title: editingTitle.trim() } : s));
      onRenameSession?.(sid, editingTitle.trim());
    } catch (e) {
      console.error('Rename session failed:', e);
    }
    setEditingSessionId(null);
  };

  const handleDelete = async (e, sid) => {
    e.stopPropagation();
    if (!window.confirm('确定删除该会话及所有消息？')) return;
    try {
      await fetch(`/api/sessions/${sid}`, { method: 'DELETE' });
      setSessions(prev => prev.filter(s => s.id !== sid));
      onDeleteSession?.(sid);
    } catch (e) {
      console.error('Delete session failed:', e);
    }
  };

  return (
    <div className={`session-panel ${show ? 'open' : ''}`}>
      <div className="session-panel-inner">
        {sessionsLoading ? (
          <div className="session-loading">
            <span className="session-loading-dot" /><span className="session-loading-dot" /><span className="session-loading-dot" />
          </div>
        ) : sessions.length === 0 ? (
          <div className="session-empty">暂无历史会话</div>
        ) : (
          sessions.map(s => (
            <div
              key={s.id}
              className={`session-item ${s.id === sessionId ? 'active' : ''}`}
              onClick={() => onSwitchSession?.(s.id)}
            >
              <div className="session-item-left">
                {editingSessionId === s.id ? (
                  <input
                    className="session-edit-input"
                    value={editingTitle}
                    autoFocus
                    onChange={e => setEditingTitle(e.target.value)}
                    onBlur={() => commitRename(s.id)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') commitRename(s.id);
                      if (e.key === 'Escape') setEditingSessionId(null);
                    }}
                    onClick={e => e.stopPropagation()}
                  />
                ) : (
                  <span
                    className="session-title"
                    onDoubleClick={e => startRename(e, s)}
                    title="双击重命名"
                  >
                    {s.title}
                  </span>
                )}
                <span className="session-time">{formatTime(s.updated_at)}</span>
              </div>
              <button
                className="session-delete-btn"
                onClick={e => handleDelete(e, s.id)}
                title="删除会话"
              >
                ✕
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default SessionPanel;