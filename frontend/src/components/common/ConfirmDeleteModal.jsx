/* 删除确认弹窗（自 TileManager 抽为公共组件） */
import React, { useState, useEffect } from 'react';

const ConfirmDeleteModal = ({ visible, title, message, fileLabel, onCancel, onConfirm }) => {
  const [deleteFiles, setDeleteFiles] = useState(false);
  useEffect(() => {
    if (visible) setDeleteFiles(false);
  }, [visible]);
  if (!visible) return null;
  return (
    <div style={{
      position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
      background: 'rgba(0,0,0,0.45)', zIndex: 10001,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onCancel}>
      <div style={{
        background: '#fff', borderRadius: 12, width: 440, maxWidth: '92vw',
        boxShadow: '0 8px 40px rgba(0,0,0,0.18)', padding: '20px 24px',
      }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <span style={{ fontSize: 18 }}>⚠️</span>
          <span style={{ fontWeight: 700, fontSize: 15 }}>{title}</span>
        </div>
        <div style={{ fontSize: 13, color: '#555', lineHeight: 1.7, marginBottom: 16, whiteSpace: 'pre-wrap' }}>
          {message}
        </div>
        {fileLabel && (
          <label style={{
            display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: '#333',
            background: '#fff7e6', border: '1px solid #ffd591', borderRadius: 8,
            padding: '10px 12px', marginBottom: 16, cursor: 'pointer',
          }}>
            <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
            <span>{fileLabel}</span>
          </label>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button onClick={onCancel} style={{
            padding: '7px 18px', border: '1px solid #d9d9d9', borderRadius: 6,
            background: '#fff', cursor: 'pointer', fontSize: 13,
          }}>取消</button>
          <button onClick={() => onConfirm(deleteFiles)} style={{
            padding: '7px 18px', border: 'none', borderRadius: 6,
            background: '#ff4d4f', color: '#fff', cursor: 'pointer', fontSize: 13,
          }}>删除</button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmDeleteModal;
