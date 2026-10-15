// ChatInput.jsx — 聊天输入组件（从 App.jsx 抽离）
import React from 'react';

const ChatInput = ({
  inputValue,
  setInputValue,
  isBusy,
  connectionError,
  selectedFiles,
  removeSelectedFile,
  shpUploadInfo,
  shpUploading,
  clearShpUpload,
  fileInputRef,
  onFileSelect,
  onSubmit,
}) => {

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(inputValue);
  };

  return (
    <form onSubmit={handleSubmit} className="input-form">
      {/* 已选图片预览 */}
      {selectedFiles.length > 0 && (
        <div className="image-preview-bar">
          {selectedFiles.map((item, idx) => (
            <div key={idx} className={`image-preview-item${item.uploading ? ' uploading' : ''}`}>
              <img src={item.preview} alt={`预览 ${idx + 1}`} />
              {item.uploading && <div className="image-upload-overlay">上传中...</div>}
              <button
                type="button"
                className="image-remove-btn"
                onClick={() => removeSelectedFile(idx)}
                disabled={item.uploading}
                title="移除图片"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
      {/* 已上传 SHP 文件标签 */}
      {shpUploadInfo && (
        <div className="shp-upload-chip-bar">
          <div className="shp-upload-chip">
            <span className="shp-chip-icon">🗂️</span>
            <span className="shp-chip-name">{shpUploadInfo.layer_name}</span>
            <span className="shp-chip-meta">
              {shpUploadInfo.feature_count}个{shpUploadInfo.geometry_type || '要素'}
            </span>
            <button
              type="button"
              className="shp-chip-remove"
              onClick={clearShpUpload}
              disabled={shpUploading}
              title="移除图层"
            >
              ✕
            </button>
          </div>
        </div>
      )}
      <div className="input-container">
        <input
          type="file"
          ref={fileInputRef}
          onChange={onFileSelect}
          accept="image/*,.xlsx,.xls"
          multiple
          style={{ display: 'none' }}
        />
        <button
          type="button"
          className="upload-image-btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={isBusy}
          title="上传图片"
        >
          📎
        </button>
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          placeholder={connectionError ? "后端服务未连接..." : "请输入您的问题..."}
          disabled={isBusy}
          className="message-input"
        />
        <button
          type="submit"
          disabled={isBusy || (!inputValue.trim() && selectedFiles.length === 0 && !shpUploadInfo) || connectionError}
          className="send-button"
        >
          发送
        </button>
      </div>
      {connectionError && (
        <div className="connection-help">
          请确保后端服务正在运行：<code>cd backend && python main.py</code>
        </div>
      )}
    </form>
  );
};

export default ChatInput;