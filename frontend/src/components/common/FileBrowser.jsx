/* 服务端文件浏览器弹窗（自 TileManager 抽为公共组件；GisPipeline 可复用） */
import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

const API_BASE_URL = '';

const FileBrowser = ({ visible, onClose, onSelect, extensions = '.tif,.tiff', title = '选择文件' }) => {
  const [currentPath, setCurrentPath] = useState('/mnt');
  const [dirs, setDirs] = useState([]);
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [parentPath, setParentPath] = useState(null);
  const [pathInput, setPathInput] = useState('/mnt');

  const loadDir = useCallback(async (dirPath) => {
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`${API_BASE_URL}/api/file_browser`, {
        params: { path: dirPath, extensions },
      });
      setCurrentPath(res.data.path);
      setPathInput(res.data.path);
      setParentPath(res.data.parent);
      setDirs(res.data.dirs || []);
      setFiles(res.data.files || []);
    } catch (err) {
      setError(err.response?.data?.detail || '无法加载目录');
    } finally {
      setLoading(false);
    }
  }, [extensions]);

  useEffect(() => {
    if (visible) loadDir(currentPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  if (!visible) return null;


  const formatSize = (bytes) => {
    if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
  };

  const handlePathGo = () => {
    if (pathInput.trim()) loadDir(pathInput.trim());
  };

  return (
    <div style={{
      position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
      background: 'rgba(0,0,0,0.45)', zIndex: 10000,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onClose}>
      <div style={{
        background: '#fff', borderRadius: 12, width: 620, maxHeight: '80vh',
        display: 'flex', flexDirection: 'column', boxShadow: '0 8px 40px rgba(0,0,0,0.18)',
      }} onClick={(e) => e.stopPropagation()}>
        {/* header */}
        <div style={{
          padding: '16px 20px', borderBottom: '1px solid #f0f0f0',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <span style={{ fontSize: 18 }}>📂</span>
          <span style={{ fontWeight: 700, fontSize: 15, flex: 1 }}>{title}</span>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', fontSize: 20, cursor: 'pointer', color: '#999',
          }}>✕</button>
        </div>

        {/* path bar */}
        <div style={{
          padding: '10px 20px', borderBottom: '1px solid #f0f0f0',
          display: 'flex', gap: 6, alignItems: 'center',
        }}>
          <button
            onClick={() => parentPath && loadDir(parentPath)}
            disabled={!parentPath}
            style={{
              padding: '4px 10px', border: '1px solid #d9d9d9', borderRadius: 6,
              background: parentPath ? '#fafafa' : '#f5f5f5', cursor: parentPath ? 'pointer' : 'default',
              fontSize: 13, color: parentPath ? '#333' : '#bbb',
            }}
          >⬆ 上级</button>
          <input
            value={pathInput}
            onChange={(e) => setPathInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handlePathGo()}
            style={{
              flex: 1, padding: '5px 10px', border: '1px solid #d9d9d9',
              borderRadius: 6, fontSize: 13, fontFamily: 'monospace',
            }}
          />
          <button onClick={handlePathGo} style={{
            padding: '4px 12px', border: '1px solid #722ed1', borderRadius: 6,
            background: '#722ed1', color: '#fff', cursor: 'pointer', fontSize: 13,
          }}>前往</button>
        </div>

        {/* content */}
        <div style={{
          flex: 1, overflowY: 'auto', padding: '4px 0', minHeight: 200, maxHeight: '55vh',
        }}>
          {loading && <div style={{ textAlign: 'center', padding: 30, color: '#999' }}>加载中...</div>}
          {error && <div style={{ textAlign: 'center', padding: 20, color: '#ff4d4f' }}>{error}</div>}
          {!loading && !error && dirs.length === 0 && files.length === 0 && (
            <div style={{ textAlign: 'center', padding: 30, color: '#bbb' }}>此目录为空或无匹配文件</div>
          )}
          {!loading && !error && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <tbody>
                {dirs.map((d) => (
                  <tr
                    key={'d-' + d.name}
                    onClick={() => loadDir(currentPath + '/' + d.name)}
                    style={{ cursor: 'pointer' }}
                    onMouseEnter={(e) => e.currentTarget.style.background = '#f5f0ff'}
                    onMouseLeave={(e) => e.currentTarget.style.background = ''}
                  >
                    <td style={{ padding: '8px 20px', whiteSpace: 'nowrap' }}>📁</td>
                    <td style={{ padding: '8px 4px', fontWeight: 500 }}>{d.name}</td>
                    <td style={{ padding: '8px 20px', color: '#bbb', textAlign: 'right' }}>文件夹</td>
                  </tr>
                ))}
                {files.map((f) => (
                  <tr
                    key={'f-' + f.name}
                    onClick={() => onSelect(currentPath + '/' + f.name)}
                    style={{ cursor: 'pointer' }}
                    onMouseEnter={(e) => e.currentTarget.style.background = '#f9f0ff'}
                    onMouseLeave={(e) => e.currentTarget.style.background = ''}
                  >
                    <td style={{ padding: '8px 20px', whiteSpace: 'nowrap' }}>🗺️</td>
                    <td style={{ padding: '8px 4px', color: '#531dab' }}>{f.name}</td>
                    <td style={{ padding: '8px 20px', color: '#999', textAlign: 'right', whiteSpace: 'nowrap' }}>{formatSize(f.size)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

export default FileBrowser;
