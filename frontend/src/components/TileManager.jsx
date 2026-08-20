import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

const API_BASE_URL = '';

/* ---- 服务端文件浏览器弹窗 ---- */
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

/* ---- 删除确认弹窗（单次确认 + 可选“同时删除文件”） ---- */
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

const TileManager = ({ onSwitchTo3D }) => {
  const [layers, setLayers] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [regenerating, setRegenerating] = useState(null);
  const [isBuilding, setIsBuilding] = useState(false);
  const [isDroneBuilding, setIsDroneBuilding] = useState(false);
  const [buildProgress, setBuildProgress] = useState(null); // {percent, message, stage}
  const [gsStatus, setGsStatus] = useState(null);
  const [gsLayers, setGsLayers] = useState({});
  const [gsLoading, setGsLoading] = useState(false);
  const [gsExpanded, setGsExpanded] = useState(false);
  const [gsActionLayer, setGsActionLayer] = useState(null);
  const [gsPreviewLayer, setGsPreviewLayer] = useState('');
  const [gsPreviewBbox, setGsPreviewBbox] = useState(null);
  const [gsPreviewDrag, setGsPreviewDrag] = useState(null);
  const [toast, setToast] = useState(null);
  const [gwcOpen, setGwcOpen] = useState(false);
  const [gwcLoading, setGwcLoading] = useState(false);
  const [gwcProgress, setGwcProgress] = useState(null);
  const [gwcForm, setGwcForm] = useState({
    layer: '',
    minX: '',
    minY: '',
    maxX: '',
    maxY: '',
    minZoom: 0,
    maxZoom: 14,
    format: 'image/png',
    threads: 1,
  });
  const [buildForm, setBuildForm] = useState({
    layerKey: '',
    label: '',
    buildType: 'both',
    stroke: '#2773d7',
    fill: '#2773d7',
    fillAlpha: 0.18,
    strokeWidth: 2,
    pointSize: 8,
    minZoom: 0,
    maxZoom: 18,
    autoPublish: true,
    file: null,
  });
  const [droneForm, setDroneForm] = useState({
    sourcePath: '/home/server/python/map_assistant_v1/豫信固砂许〔2023〕第03号.tif',
    layerKey: 'yuxin_gusha_2023_03',
    name: '豫信固砂许〔2023〕第03号无人机影像',
    areaKey: 'yuxin_gusha_2023_03',
    year: 2023,
    minZoom: 10,
    maxZoom: 22,
    opacity: 0.9,
    tileFormat: 'PNG',
    quality: 85,
    sourceSrs: '',  // 手动指定源坐标系，如 EPSG:4547；留空则自动检测
  });

  // ---- 3D Tiles 管理 ----
  const [threeDTilesets, setThreeDTilesets] = useState([]);
  const [threeDLoading, setThreeDLoading] = useState(false);
  const [showThreeDUpload, setShowThreeDUpload] = useState(false);
  const [showThreeDRegister, setShowThreeDRegister] = useState(false);
  const [showServerGeoJsonBrowser, setShowServerGeoJsonBrowser] = useState(false); // 服务器 GeoJSON 文件浏览器
  const [threeDDeleteKey, setThreeDDeleteKey] = useState(null);
  const [showThreeDFileBrowser, setShowThreeDFileBrowser] = useState(false);
  const [threeDForm, setThreeDForm] = useState({
    key: '',
    name: '',
    label: '',
    alt_offset: 0.0,
    auto_ground_clamp: true,
    directory: '',
    file: null,
  });

  const fetchLayers = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [response, gsResponse] = await Promise.all([
        axios.get(`${API_BASE_URL}/api/tile_manager/layers`),
        axios.get(`${API_BASE_URL}/api/geoserver/layers`).catch(() => null),
      ]);
      const gsMapping = gsResponse?.data?.available
        ? normalizeGeoServerLayers(gsResponse.data.layers || [])
        : {};
      if (response.data.success) {
        const rawLayers = response.data.layers || [];
        // 读取用户已删除的虚拟图层列表，不再强制注入
        const dismissed = JSON.parse(localStorage.getItem('dismissed_virtual_layers') || '[]');
        const hasCeshen = rawLayers.some(layer => layer.key === 'ceshen');
        const nextLayers = hasCeshen || dismissed.includes('ceshen') ? rawLayers : [
          ...rawLayers,
          {
            key: 'ceshen',
            label: '测深点 ceshen',
            type: 'vector',
            status: 'ready',
            tile_count: null,
            size_bytes: null,
            min_zoom: 0,
            max_zoom: 22,
            api_url: '/api/vector-data?table_name=ceshen',
            directory: 'PostGIS public.ceshen',
            source_name: 'PostGIS ceshen',
            source_path: 'postgres.public.ceshen',
            color: '#13c2c2',
          },
        ];
        setLayers(nextLayers);
        if (!gwcForm.layer && nextLayers.length) {
          const firstPublished = nextLayers.find((layer) => gsMapping[layer.key]);
          if (firstPublished) {
            setGwcForm(prev => ({ ...prev, layer: firstPublished.key }));
          }
        }
      } else {
        setError(response.data.error || '获取图层列表失败');
      }
      setGsLayers(gsMapping);
      if (!gsPreviewLayer) {
        const firstGsLayer = Object.keys(gsMapping)[0];
        if (firstGsLayer) setGsPreviewLayer(firstGsLayer);
      }
    } catch (err) {
      setError('无法连接到服务器');
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  };

  const normalizeGeoServerLayers = (items = []) => {
    const mapping = {};
    items.forEach((item) => {
      if (!item?.name) return;
      mapping[item.name] = {
        ...item,
        published: true,
      };
    });
    return mapping;
  };

  const fetchGeoServerStatus = async () => {
    setGsLoading(true);
    try {
      const response = await axios.get(`${API_BASE_URL}/api/geoserver/status`);
      setGsStatus(response.data);
    } catch (err) {
      setGsStatus({ available: false, reason: err.response?.data?.detail || 'GeoServer 状态查询失败' });
    } finally {
      setGsLoading(false);
    }
  };

  const fetchGeoServerLayers = async () => {
    try {
      const response = await axios.get(`${API_BASE_URL}/api/geoserver/layers`);
      if (response.data.available) {
        const mapping = normalizeGeoServerLayers(response.data.layers || []);
        setGsLayers(mapping);
        if (!gsPreviewLayer) {
          const firstGsLayer = Object.keys(mapping)[0];
          if (firstGsLayer) setGsPreviewLayer(firstGsLayer);
        }
      } else {
        setGsLayers({});
      }
    } catch (err) {
      setGsLayers({});
    }
  };

  const showToast = (message, type = 'success') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 2600);
  };

  const copyText = async (text, message = '已复制到剪贴板') => {
    if (!text) {
      setError('没有可复制的 URL');
      return;
    }
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const el = document.createElement('textarea');
        el.value = text;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      }
      showToast(message);
    } catch (err) {
      setError('复制失败，请手动复制');
    }
  };


  useEffect(() => {
    fetchLayers();
    fetchGeoServerStatus();
    fetch3DTilesets();
    const timer = setInterval(fetchGeoServerStatus, 60000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pollTileBuildJob = async (jobId, { onDone, onError, onProgress } = {}) => {
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      let statusResp;
      try {
        statusResp = await axios.get(`${API_BASE_URL}/api/tile_manager/build_status/${jobId}`);
      } catch (err) {
        onError?.(err.response?.data?.detail || '构建状态查询失败');
        return;
      }
      const job = statusResp.data.job;
      onProgress?.(job);
      if (job.done) {
        if (job.success) {
          onDone?.(job);
        } else {
          onError?.(job.message || '构建失败');
        }
        return;
      }
    }
  };

  const handleRegenerate = async (layerKey) => {
    if (!window.confirm(`确定要重新生成 ${layerKey} 的矢量切片吗？此操作可能需要几分钟。`)) return;
    setRegenerating(layerKey);
    setError(null);
    setBuildProgress({ percent: 0, message: '正在提交任务...', stage: 'init' });
    try {
      const response = await axios.post(`${API_BASE_URL}/api/tile_manager/regenerate`, { layer: layerKey });
      if (!response.data.success) {
        setError(response.data.error || '重新生成失败');
        return;
      }
      if (response.data.job_id) {
        await pollTileBuildJob(response.data.job_id, {
          onProgress: (job) => setBuildProgress({
            percent: job.percent || 0,
            message: job.message || '生成中...',
            stage: job.stage || 'running',
          }),
          onDone: () => {
            fetchLayers();
            showToast(`${layerKey} 重新生成完成`);
          },
          onError: (msg) => setError(msg),
        });
      } else {
        fetchLayers();
      }
    } catch (err) {
      setError(err.response?.data?.detail || '重新生成失败');
      console.error(err);
    } finally {
      setRegenerating(null);
      setTimeout(() => setBuildProgress(null), 3000);
    }
  };

  const handleBuildChange = (field, value) => {
    setBuildForm(prev => ({ ...prev, [field]: value }));
  };

  const handleBuildSubmit = async (e) => {
    e.preventDefault();
    if (!buildForm.file) {
      setError('请先选择 GeoJSON 文件');
      return;
    }
    setIsBuilding(true);
    setError(null);
    const formData = new FormData();
    formData.append('file', buildForm.file);
    formData.append('layer_key', buildForm.layerKey);
    formData.append('label', buildForm.label);
    formData.append('build_type', buildForm.buildType);
    formData.append('stroke', buildForm.stroke);
    formData.append('fill', buildForm.fill);
    formData.append('fill_alpha', buildForm.fillAlpha);
    formData.append('stroke_width', buildForm.strokeWidth);
    formData.append('point_size', buildForm.pointSize);
    formData.append('min_zoom', buildForm.minZoom);
    formData.append('max_zoom', buildForm.maxZoom);
    formData.append('auto_publish', buildForm.autoPublish ? 'true' : 'false');
    try {
      const response = await axios.post(`${API_BASE_URL}/api/tile_manager/build`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      if (response.data.success) {
        setBuildForm(prev => ({ ...prev, file: null }));
        const quickInput = document.getElementById('quick-geojson-file');
        if (quickInput) quickInput.value = '';
        if (response.data.job_id) {
          // 后台任务：轮询进度，构建期间不阻塞其他操作
          await pollTileBuildJob(response.data.job_id, {
            onProgress: (job) => setBuildProgress({
              percent: job.percent || 0,
              message: job.message || '构建中...',
              stage: job.stage || 'running',
            }),
            onDone: async (job) => {
              const gsResult = job.geoserver;
              showToast(gsResult && !gsResult.error
                ? `${response.data.layer} 已构建并发布到 GeoServer`
                : `${response.data.layer} 构建完成`);
              await fetchLayers();
              await fetchGeoServerLayers();
              await fetchGeoServerStatus();
              if (gsResult?.layer_name) {
                setGsExpanded(true);
                setGsPreviewLayer(gsResult.layer_name);
                setGsPreviewBbox(null);
              }
            },
            onError: (msg) => setError(msg),
          });
        } else {
          showToast(`${response.data.layer} 构建完成`);
          await fetchLayers();
        }
      } else {
        setError(response.data.error || '构建失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '构建失败');
      console.error(err);
    } finally {
      setIsBuilding(false);
    }
  };

  const [showFileBrowser, setShowFileBrowser] = useState(false);

  // 服务器文件发布（矢量 GeoJSON）：选择文件 → 调 publish-tile
  const handleServerGeoJsonSelect = async (filePath) => {
    setShowServerGeoJsonBrowser(false);
    setError(null);
    setBuildProgress({ percent: 0, message: '正在提交矢量发布...', stage: 'init' });
    const baseName = filePath.split('/').pop().replace(/\.[^.]+$/, '');
    try {
      const body = {
        source_path: filePath,
        layer_key: buildForm.layerKey || baseName,
        name: buildForm.label || baseName,
        layer_type: 'vector',
        min_zoom: Number(buildForm.minZoom),
        max_zoom: Number(buildForm.maxZoom),
        stroke: buildForm.stroke,
        fill: buildForm.fill,
        fill_alpha: Number(buildForm.fillAlpha),
        stroke_width: Number(buildForm.strokeWidth),
        point_size: Number(buildForm.pointSize),
      };
      const res = await axios.post(`${API_BASE_URL}/gis-tool/publish-tile`, body);
      if (!res.data.success) {
        setError(res.data.detail || '矢量发布提交失败');
        setBuildProgress(null);
        return;
      }
      const jobId = res.data.job_id;
      while (true) {
        await new Promise(r => setTimeout(r, 1000));
        const sr = await axios.get(`${API_BASE_URL}/api/tile_manager/build_status/${jobId}`);
        const job = sr.data.job;
        setBuildProgress({ percent: job.percent || 0, message: job.message || '发布中...', stage: job.stage || 'running' });
        if (job.done) {
          if (job.success) {
            showToast(`矢量图层「${res.data.layer_key}」发布完成`);
            await fetchLayers();
            await fetchGeoServerLayers();
            await fetchGeoServerStatus();
          } else {
            setError(job.message || '矢量发布失败');
          }
          setTimeout(() => setBuildProgress(null), 5000);
          break;
        }
      }
    } catch (err) {
      setError(err.response?.data?.detail || '矢量发布请求失败: ' + (err.message || err));
      console.error(err);
      setBuildProgress(null);
    }
  };

  const handleDroneChange = (field, value) => {
    setDroneForm(prev => ({ ...prev, [field]: value }));
  };

  const handleFileSelect = (filePath) => {
    handleDroneChange('sourcePath', filePath);
    setShowFileBrowser(false);
  };

  const handleDroneBuildSubmit = async (e) => {
    e.preventDefault();
    if (!droneForm.sourcePath) {
      setError('请填写 GeoTIFF 文件路径');
      return;
    }
    setIsDroneBuilding(true);
    setError(null);
    setBuildProgress({ percent: 0, message: '正在连接...', stage: 'init' });
    try {
      const body = {
        source_path: droneForm.sourcePath,
        layer_key: droneForm.layerKey,
        name: droneForm.name,
        area_key: droneForm.areaKey,
        year: droneForm.year ? Number(droneForm.year) : null,
        min_zoom: Number(droneForm.minZoom),
        max_zoom: Number(droneForm.maxZoom),
        opacity: Number(droneForm.opacity),
        tile_format: droneForm.tileFormat,
        quality: Number(droneForm.quality),
        overwrite: true,
        source_srs: droneForm.sourceSrs ? droneForm.sourceSrs.trim() : '',
      };
      const startResp = await axios.post(`${API_BASE_URL}/api/drone_imagery/build_async`, body);
      const jobId = startResp.data.job_id;
      if (!jobId) {
        throw new Error('未获取到构建任务ID');
      }
      while (true) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const statusResp = await axios.get(`${API_BASE_URL}/api/drone_imagery/build_status/${jobId}`);
        const job = statusResp.data.job;
        setBuildProgress({
          percent: job.percent || 0,
          message: job.message || '构建中...',
          stage: job.stage || 'running',
        });
        if (job.done) {
          if (job.success) {
            fetchLayers();
          } else {
            setError(job.message || '无人机影像构建失败');
          }
          break;
        }
      }
    } catch (err) {
      setError('构建请求失败: ' + (err.message || '网络错误'));
      console.error(err);
    } finally {
      setIsDroneBuilding(false);
      setTimeout(() => setBuildProgress(null), 5000);
    }
  };

  const handleGeoServerPublish = async (layerKey, mode = 'publish') => {
    setGsActionLayer(`${mode}:${layerKey}`);
    setError(null);
    try {
      if (mode === 'resync') {
        await axios.post(`${API_BASE_URL}/api/geoserver/unpublish`, { layer: layerKey }).catch(() => null);
      }
      const response = await axios.post(`${API_BASE_URL}/api/geoserver/publish`, { layer: layerKey });
      if (response.data.success) {
        showToast(`${layerKey} 已发布到 GeoServer`);
        await fetchGeoServerLayers();
        await fetchGeoServerStatus();
      } else {
        setError(response.data.error || 'GeoServer 发布失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'GeoServer 发布失败');
    } finally {
      setGsActionLayer(null);
    }
  };

  const handleGeoServerUnpublish = async (layerKey) => {
    if (!window.confirm(`确定要从 GeoServer 取消发布 ${layerKey} 吗？`)) return;
    setGsActionLayer(`unpublish:${layerKey}`);
    setError(null);
    try {
      const response = await axios.post(`${API_BASE_URL}/api/geoserver/unpublish`, { layer: layerKey });
      if (response.data.success) {
        showToast(`${layerKey} 已取消发布`);
        await fetchGeoServerLayers();
        await fetchGeoServerStatus();
      } else {
        setError(response.data.error || 'GeoServer 取消发布失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'GeoServer 取消发布失败');
    } finally {
      setGsActionLayer(null);
    }
  };

  const [droneDeleteKey, setDroneDeleteKey] = useState(null);
  const [tileDeleteKey, setTileDeleteKey] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null); // { type:'drone'|'tile'|'3dtiles', key, label, isVirtual }

  /** 请求删除无人机影像：弹出单次确认（可选同时删除磁盘文件） */
  const handleDroneDelete = (layerKey, label) => {
    setConfirmDelete({ type: 'drone', key: layerKey, label });
  };

  const confirmDroneDelete = async (deleteFiles) => {
    const target = confirmDelete || {};
    const layerKey = target.key;
    const label = target.label;
    setConfirmDelete(null);
    if (!layerKey) return;
    setDroneDeleteKey(layerKey);
    setError(null);
    try {
      const response = await axios.delete(
        `${API_BASE_URL}/api/drone_imagery/${encodeURIComponent(layerKey)}`,
        { params: { delete_files: deleteFiles } }
      );
      if (response.data.success) {
        const removed = response.data.deleted_files || [];
        showToast(`已删除「${label || layerKey}」${removed.length ? `，清理了 ${removed.length} 个文件` : ''}`);
        await fetchLayers();
        await fetchGeoServerLayers();
        await fetchGeoServerStatus();
      } else {
        setError(response.data.error || '删除失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '删除无人机影像失败');
    } finally {
      setDroneDeleteKey(null);
    }
  };

  /** 请求删除图层列表中的任意图层（矢量/栅格/无人机/虚拟）：单次确认 + 勾选删除文件 */
  const handleTileLayerDelete = (layer) => {
    const isVirtual = layer.directory?.startsWith('PostGIS') || layer.source_path?.startsWith('postgres');
    setConfirmDelete({ type: 'tile', key: layer.key, label: layer.label, isVirtual, layerType: layer.type });
  };

  const confirmTileLayerDelete = async (deleteFiles) => {
    const target = confirmDelete || {};
    const key = target.key;
    const label = target.label;
    const isVirtual = target.isVirtual;
    const layerType = target.layerType; // 'drone' 时走无人机删除 API（会清理 MBTiles）
    setConfirmDelete(null);
    if (!key) return;
    setTileDeleteKey(key);
    setError(null);
    try {
      const apiUrl = layerType === 'drone'
        ? `${API_BASE_URL}/api/drone_imagery/${encodeURIComponent(key)}`
        : `${API_BASE_URL}/api/tile_manager/${encodeURIComponent(key)}`;
      const response = await axios.delete(apiUrl, { params: { delete_files: deleteFiles } });
      if (response.data.success) {
        // 虚拟图层删除后，记录到 localStorage，防止 fetchLayers 重新注入
        if (isVirtual) {
          try {
            const dismissed = JSON.parse(localStorage.getItem('dismissed_virtual_layers') || '[]');
            if (!dismissed.includes(key)) {
              dismissed.push(key);
              localStorage.setItem('dismissed_virtual_layers', JSON.stringify(dismissed));
            }
          } catch (_) { /* ignore */ }
        }
        const removed = response.data.deleted_files || [];
        const gsInfo = response.data.gs_removed ? '，已从 GeoServer 移除' : '';
        const pgInfo = response.data.pg_dropped ? '，已删除 PostGIS 数据表' : '';
        const fileInfo = removed.length ? `，清理了 ${removed.length} 个文件` : '';
        showToast(`已删除「${label || key}」${gsInfo}${pgInfo}${fileInfo}`);
        await fetchLayers();
        await fetchGeoServerLayers();
        await fetchGeoServerStatus();
      } else {
        setError(response.data.error || '删除失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '删除图层失败');
    } finally {
      setTileDeleteKey(null);
    }
  };

  // ---- 3D Tiles 操作函数 ----
  const fetch3DTilesets = async () => {
    try {
      const response = await axios.get(`${API_BASE_URL}/api/tile_manager/3dtiles`);
      if (response.data.success) {
        setThreeDTilesets(response.data.datasets || []);
      }
    } catch (err) {
      console.error('获取 3D Tiles 列表失败', err);
    }
  };

  const handle3DFormChange = (field, value) => {
    setThreeDForm(prev => ({ ...prev, [field]: value }));
  };

  const handle3DTilesetUpload = async (e) => {
    e.preventDefault();
    if (!threeDForm.file) {
      setError('请选择 3D Tiles zip 文件');
      return;
    }
    setThreeDLoading(true);
    setError(null);
    const formData = new FormData();
    formData.append('file', threeDForm.file);
    formData.append('key', threeDForm.key);
    formData.append('name', threeDForm.name || threeDForm.key);
    formData.append('label', threeDForm.label || threeDForm.name || threeDForm.key);
    formData.append('alt_offset', threeDForm.alt_offset);
    formData.append('auto_ground_clamp', threeDForm.auto_ground_clamp ? 'true' : 'false');
    try {
      const response = await axios.post(`${API_BASE_URL}/api/tile_manager/3dtiles/upload`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      if (response.data.success) {
        showToast(`3D Tiles「${response.data.layer}」上传成功`);
        setThreeDForm(prev => ({ ...prev, key: '', name: '', label: '', file: null }));
        setShowThreeDUpload(false);
        await fetch3DTilesets();
        await fetchLayers();
      } else {
        setError(response.data.error || '上传失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '3D Tiles 上传失败');
    } finally {
      setThreeDLoading(false);
    }
  };

  const handle3DTilesetRegister = async (e) => {
    e.preventDefault();
    if (!threeDForm.directory) {
      setError('请选择 3D Tiles 目录（必须包含 tileset.json）');
      return;
    }
    setThreeDLoading(true);
    setError(null);
    try {
      const body = {
        directory: threeDForm.directory,
        key: threeDForm.key,
        name: threeDForm.name || threeDForm.key,
        label: threeDForm.label || threeDForm.name || threeDForm.key,
        alt_offset: Number(threeDForm.alt_offset),
        auto_ground_clamp: threeDForm.auto_ground_clamp,
      };
      const response = await axios.post(`${API_BASE_URL}/api/tile_manager/3dtiles/register`, body);
      if (response.data.success) {
        showToast(`3D Tiles「${response.data.layer}」注册成功`);
        setThreeDForm(prev => ({ ...prev, key: '', name: '', label: '', directory: '' }));
        setShowThreeDRegister(false);
        await fetch3DTilesets();
        await fetchLayers();
      } else {
        setError(response.data.error || '注册失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '3D Tiles 注册失败');
    } finally {
      setThreeDLoading(false);
    }
  };

  /** 请求删除 3D Tiles 数据集：单次确认 + 勾选删除文件 */
  const handle3DTilesetDelete = (dataset) => {
    setConfirmDelete({ type: '3dtiles', key: dataset.key, label: dataset.label || dataset.key });
  };

  const confirm3DTilesetDelete = async (deleteFiles) => {
    const target = confirmDelete || {};
    const datasetKey = target.key;
    const datasetLabel = target.label;
    setConfirmDelete(null);
    if (!datasetKey) return;
    setThreeDDeleteKey(datasetKey);
    setError(null);
    try {
      const response = await axios.delete(
        `${API_BASE_URL}/api/tile_manager/3dtiles/${encodeURIComponent(datasetKey)}`,
        { params: { delete_files: deleteFiles } }
      );
      if (response.data.success) {
        const removed = response.data.deleted_files || [];
        showToast(`已删除「${datasetLabel || datasetKey}」${removed.length ? `，清理了 ${removed.length} 个文件` : ''}`);
        await fetch3DTilesets();
        await fetchLayers();
      } else {
        setError(response.data.error || '删除失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '删除 3D Tiles 失败');
    } finally {
      setThreeDDeleteKey(null);
    }
  };

  const handle3DTilesetRestats = async (dataset) => {
    setThreeDLoading(true);
    setError(null);
    try {
      const response = await axios.post(
        `${API_BASE_URL}/api/tile_manager/3dtiles/restats/${encodeURIComponent(dataset.key)}`
      );
      if (response.data.success) {
        setThreeDTilesets(prev => prev.map(item => (item.key === dataset.key ? response.data.item : item)));
        showToast(`已重新统计「${dataset.label || dataset.key}」`);
      } else {
        setError(response.data.error || '重新统计失败');
      }
    } catch (err) {
      setError(err.response?.data?.detail || '重新统计失败');
    } finally {
      setThreeDLoading(false);
    }
  };

  const handle3DTilesetPreview = (dataset) => {
    onSwitchTo3D?.();
    const tilesetUrl = `/api/3dtiles/${encodeURIComponent(dataset.key)}/tileset.json`;
    window.dispatchEvent(new CustomEvent('cesium_execute_command', {
      detail: {
        type: 'load3dTiles',
        url: tilesetUrl,
        name: dataset.label || dataset.key,
        autoGroundClamp: dataset.auto_ground_clamp !== false,
        altOffset: dataset.alt_offset || 0,
        flyTo: true,
        center: dataset.center || null,
      },
    }));
    showToast(`正在加载 3D Tiles「${dataset.label || dataset.key}」`);
  };

  const handle3DFileSelect = (filePath) => {
    handle3DFormChange('directory', filePath);
    setShowThreeDFileBrowser(false);
  };

  const handleGwcChange = (field, value) => {
    setGwcForm(prev => ({ ...prev, [field]: value }));
  };

  const parseGwcBounds = () => {
    const values = [gwcForm.minX, gwcForm.minY, gwcForm.maxX, gwcForm.maxY];
    if (values.every(v => v === '' || v === null || v === undefined)) return null;
    if (values.some(v => v === '' || v === null || v === undefined)) {
      throw new Error('BBOX 需要填写完整的 minX/minY/maxX/maxY，或全部留空');
    }
    return values.map(Number);
  };

  const handleUseLayerBounds = () => {
    const layer = layers.find(item => item.key === gwcForm.layer);
    const bounds = layer?.bounds || layer?.meta?.bounds;
    if (!bounds || bounds.length !== 4) {
      setError('当前图层没有可用 bounds，请手动填写 BBOX 或留空使用全范围');
      return;
    }
    setGwcForm(prev => ({
      ...prev,
      minX: bounds[0],
      minY: bounds[1],
      maxX: bounds[2],
      maxY: bounds[3],
    }));
  };

  const handleGwcSeed = async (action = 'seed') => {
    if (!gwcForm.layer) {
      setError('请选择已发布到 GeoServer 的图层');
      return;
    }
    if (action === 'truncate' && !window.confirm(`确定要清空 ${gwcForm.layer} 的 GWC 缓存吗？`)) return;
    setGwcLoading(true);
    setError(null);
    try {
      const bounds = parseGwcBounds();
      const body = {
        layer: gwcForm.layer,
        bounds,
        min_zoom: Number(gwcForm.minZoom),
        max_zoom: Number(gwcForm.maxZoom),
        format: gwcForm.format,
        threads: Number(gwcForm.threads),
      };
      const endpoint = action === 'truncate' ? '/api/geoserver/truncate' : '/api/geoserver/seed';
      const response = await axios.post(`${API_BASE_URL}${endpoint}`, body);
      if (response.data.success) {
        showToast(action === 'truncate' ? 'GWC 清空任务已提交' : 'GWC 预切任务已提交');
        await handleGwcStatus();
      } else {
        setError(response.data.error || 'GWC 操作失败');
      }
    } catch (err) {
      setError(err.message || err.response?.data?.detail || 'GWC 操作失败');
    } finally {
      setGwcLoading(false);
    }
  };

  const handleGwcStatus = async () => {
    if (!gwcForm.layer) {
      setError('请选择已发布到 GeoServer 的图层');
      return;
    }
    try {
      const response = await axios.get(`${API_BASE_URL}/api/geoserver/seed/${gwcForm.layer}`);
      setGwcProgress(prev => {
        const data = response.data;
        if ((!data.tasks || data.tasks.length === 0) && prev?.tasks?.length) {
          const lastTask = prev.tasks[0] || {};
          return {
            ...data,
            tasks: [{
              ...lastTask,
              status: 'DONE',
              percent: 100,
              remaining: 0,
              processed: lastTask.total || lastTask.processed || 0,
            }],
          };
        }
        return data;
      });
    } catch (err) {
      setError(err.response?.data?.detail || 'GWC 进度查询失败');
    }
  };


  const formatSize = (bytes) => {
    if (!bytes || bytes === 0) return '-';
    if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
  };

  const getTypeTag = (type) => {
    if (type === 'vector') return { label: '矢量切片', color: '#52c41a', bg: '#f6ffed', border: '#b7eb8f' };
    if (type === 'raster') return { label: '栅格切片', color: '#1890ff', bg: '#e6f7ff', border: '#91d5ff' };
    if (type === 'drone') return { label: '无人机影像', color: '#722ed1', bg: '#f9f0ff', border: '#d3adf7' };
    return { label: type, color: '#666', bg: '#f5f5f5', border: '#d9d9d9' };
  };

  const getStatusTag = (status) => {
    if (status === 'ready') return { label: '已发布', color: '#52c41a', bg: '#f6ffed', border: '#b7eb8f' };
    if (status === 'missing') return { label: '未生成', color: '#faad14', bg: '#fffbe6', border: '#ffe58f' };
    if (status === 'error') return { label: '异常', color: '#ff4d4f', bg: '#fff2f0', border: '#ffccc7' };
    return { label: status, color: '#666', bg: '#f5f5f5', border: '#d9d9d9' };
  };

  const gsAvailable = !!gsStatus?.available;
  const gsCapabilities = gsStatus?.capabilities || {};
  const publishedLayers = layers.filter(layer => gsLayers[layer.key]);
  const droneLayers = layers.filter(layer => layer.type === 'drone');
  const gsPublishedItems = Object.values(gsLayers);
  const gsPreviewItem = gsLayers[gsPreviewLayer] || gsPublishedItems[0];
  const currentPreviewBbox = gsPreviewBbox || gsPreviewItem?.bounds || null;
  const gwcTask = gwcProgress?.tasks?.[0];

  const resetGsPreview = (item = gsPreviewItem) => {
    setGsPreviewBbox(item?.bounds || null);
  };

  const handleGsPreviewSelect = (item) => {
    setGsPreviewLayer(item.name);
    setGsPreviewBbox(item.bounds || null);
  };

  const updateGsPreviewBbox = (mode) => {
    const bbox = currentPreviewBbox;
    if (!bbox || bbox.length !== 4) return;
    const [minX, minY, maxX, maxY] = bbox.map(Number);
    const width = maxX - minX;
    const height = maxY - minY;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    let next = bbox;
    if (mode === 'in' || mode === 'out') {
      const factor = mode === 'in' ? 0.55 : 1.8;
      const halfW = width * factor / 2;
      const halfH = height * factor / 2;
      next = [cx - halfW, cy - halfH, cx + halfW, cy + halfH];
    } else {
      const stepX = width * 0.25;
      const stepY = height * 0.25;
      const shifts = {
        left: [-stepX, 0],
        right: [stepX, 0],
        up: [0, stepY],
        down: [0, -stepY],
      };
      const [dx, dy] = shifts[mode] || [0, 0];
      next = [minX + dx, minY + dy, maxX + dx, maxY + dy];
    }
    setGsPreviewBbox(next);
  };

  const finishGsPreviewDrag = (e) => {
    if (!gsPreviewDrag || !currentPreviewBbox || currentPreviewBbox.length !== 4) return;
    const dx = e.clientX - gsPreviewDrag.x;
    const dy = e.clientY - gsPreviewDrag.y;
    const [minX, minY, maxX, maxY] = currentPreviewBbox.map(Number);
    const width = maxX - minX;
    const height = maxY - minY;
    const shiftX = -dx / Math.max(gsPreviewDrag.width, 1) * width;
    const shiftY = dy / Math.max(gsPreviewDrag.height, 1) * height;
    setGsPreviewBbox([minX + shiftX, minY + shiftY, maxX + shiftX, maxY + shiftY]);
    setGsPreviewDrag(null);
  };

  return (
    <div className="tile-manager">
      <div className="tm-header">
        <div className="header-title">
          <h2>切片管理</h2>
          <span className="tm-status-tag">XYZ Tile Service</span>
        </div>
      </div>

      <div className="tm-info-card">
        <div className="info-icon">🗂️</div>
        <div className="info-content">
          <h4>切片服务说明</h4>
          <p>
            管理栅格切片（PNG，供 3D 视图 Cesium 使用）和矢量切片（PBF，供 2D 地图 Leaflet 使用）。
            矢量切片由 tippecanoe 从 GeoJSON 源数据预生成，支持前端直接交互查询属性。
            栅格切片由后端实时渲染 + LRU 缓存提供。
          </p>
        </div>
      </div>

      <div className={`tm-geoserver-card ${gsAvailable ? 'gs-ok' : 'gs-off'}`}>
        <div className="gs-main-row">
          <div className="gs-title-wrap">
            <span className="gs-health-dot">{gsAvailable ? '🟢' : '🔴'}</span>
            <div>
              <h4>GeoServer OGC 发布中心</h4>
              <p>{gsAvailable ? `工作区：${gsStatus.workspace || 'map_assistant'}` : `未连接：${gsStatus?.reason || '等待状态检查'}`}</p>
            </div>
          </div>
          <div className="gs-stat-grid">
            <div><strong>{gsStatus?.versions?.GeoServer || '-'}</strong><span>GeoServer</span></div>
            <div><strong>{gsStatus?.workspace_count ?? '-'}</strong><span>Workspaces</span></div>
            <div><strong>{gsStatus?.layer_count ?? '-'}</strong><span>Layers</span></div>
            <div><strong>{Object.keys(gsLayers).length}</strong><span>已映射</span></div>
          </div>
          <div className="gs-actions">
            <button className="btn-refresh" onClick={fetchGeoServerStatus} disabled={gsLoading}>{gsLoading ? '刷新中...' : '刷新'}</button>
            <button className="btn-action" onClick={() => copyText(gsCapabilities.wms_1_3_0, 'WMS Capabilities 已复制')} disabled={!gsAvailable}>WMS</button>
            <button className="btn-action" onClick={() => copyText(gsCapabilities.wmts, 'WMTS Capabilities 已复制')} disabled={!gsAvailable}>WMTS</button>
            <button className="btn-action" onClick={() => copyText(gsCapabilities.wfs, 'WFS Capabilities 已复制')} disabled={!gsAvailable}>WFS</button>
            <button className="btn-action" onClick={() => setGsExpanded(!gsExpanded)}>{gsExpanded ? '收起' : '详情'}</button>
          </div>
        </div>
        {gsExpanded && (
          <div className="gs-detail-panel">
            <form className="gs-quick-publish" onSubmit={handleBuildSubmit}>
              <div className="gs-preview-head">
                <h5>新数据一键发布</h5>
                <span>GeoJSON → 切片 → PostGIS → GeoServer → 样式 → 预览</span>
              </div>
              <div className="gs-quick-grid">
                <label className="form-item file-item">
                  <span>GeoJSON 文件</span>
                  <input
                    id="quick-geojson-file"
                    type="file"
                    accept=".geojson,.json,application/geo+json,application/json"
                    onChange={(e) => handleBuildChange('file', e.target.files?.[0] || null)}
                  />
                </label>
                <label className="form-item">
                  <span>图层 Key</span>
                  <input
                    value={buildForm.layerKey}
                    onChange={(e) => handleBuildChange('layerKey', e.target.value)}
                    placeholder="例如 my_new_layer"
                  />
                </label>
                <label className="form-item">
                  <span>显示名称</span>
                  <input
                    value={buildForm.label}
                    onChange={(e) => handleBuildChange('label', e.target.value)}
                    placeholder="例如 我的新图层"
                  />
                </label>
                <label className="form-item color-item">
                  <span>描边</span>
                  <input type="color" value={buildForm.stroke} onChange={(e) => handleBuildChange('stroke', e.target.value)} />
                </label>
                <label className="form-item color-item">
                  <span>填充</span>
                  <input type="color" value={buildForm.fill} onChange={(e) => handleBuildChange('fill', e.target.value)} />
                </label>
                <label className="form-item compact-item">
                  <span>透明度</span>
                  <input type="number" min="0" max="1" step="0.05" value={buildForm.fillAlpha} onChange={(e) => handleBuildChange('fillAlpha', e.target.value)} />
                </label>
                <label className="form-item compact-item">
                  <span>线宽</span>
                  <input type="number" min="1" max="20" step="0.5" value={buildForm.strokeWidth} onChange={(e) => handleBuildChange('strokeWidth', e.target.value)} />
                </label>
                <label className="form-item compact-item">
                  <span>点大小</span>
                  <input type="number" min="2" max="64" step="1" value={buildForm.pointSize} onChange={(e) => handleBuildChange('pointSize', e.target.value)} />
                </label>
              </div>
              <div className="gs-quick-actions">
                <label className="tm-checkline">
                  <input type="checkbox" checked={buildForm.autoPublish} onChange={(e) => handleBuildChange('autoPublish', e.target.checked)} />
                  <span>自动发布到 GeoServer 并应用样式</span>
                </label>
                <button className="btn-primary" type="submit" disabled={isBuilding}>
                  {isBuilding ? '正在发布...' : '一键构建并发布'}
                </button>
                <button className="btn-secondary" type="button"
                  onClick={() => setShowServerGeoJsonBrowser(true)} disabled={isBuilding}
                  style={{
                    padding: '8px 14px', border: '1px solid #722ed1', borderRadius: 6,
                    background: '#fff', color: '#722ed1', cursor: 'pointer', fontSize: 13, fontWeight: 600,
                  }}
                >
                  📂 从服务器文件发布
                </button>
              </div>
            </form>
            <div className="gs-detail-section">
              <h5>服务入口</h5>
              {Object.entries(gsCapabilities).map(([key, url]) => (
                <div className="gs-url-row" key={key}>
                  <span>{key}</span>
                  <code>{url}</code>
                  <button className="btn-mini" onClick={() => copyText(url, `${key} 已复制`)}>复制</button>
                </div>
              ))}
            </div>
            <div className="gs-layer-preview-grid">
              <div className="gs-published-list">
                <div className="gs-preview-head">
                  <h5>已发布图层</h5>
                  <span>{gsPublishedItems.length} 个</span>
                </div>
                {gsPublishedItems.length ? gsPublishedItems.map((item) => (
                  <button
                    key={item.name}
                    type="button"
                    className={`gs-layer-item ${gsPreviewItem?.name === item.name ? 'active' : ''}`}
                    onClick={() => handleGsPreviewSelect(item)}
                  >
                    <div>
                      <strong>{item.name}</strong>
                      <small>{item.qualified_name}</small>
                    </div>
                    <span className="tag tm-gs-tag-published">{item.type === 'raster' ? '栅格' : '矢量'}</span>
                  </button>
                )) : (
                  <div className="gs-empty-preview">暂无已发布图层，请在表格中点击“发布”。</div>
                )}
              </div>
              <div className="gs-preview-panel">
                <div className="gs-preview-head">
                  <h5>图层预览</h5>
                  {gsPreviewItem && <span>{gsPreviewItem.qualified_name}</span>}
                </div>
                {gsPreviewItem ? (
                  <>
                    <div className="gs-preview-toolbar">
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('in')}>＋ 放大</button>
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('out')}>－ 缩小</button>
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('left')}>←</button>
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('right')}>→</button>
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('up')}>↑</button>
                      <button className="btn-mini" onClick={() => updateGsPreviewBbox('down')}>↓</button>
                      <button className="btn-mini" onClick={() => resetGsPreview()}>重置</button>
                    </div>
                    <div
                      className="gs-preview-image"
                      onWheel={(e) => {
                        e.preventDefault();
                        updateGsPreviewBbox(e.deltaY < 0 ? 'in' : 'out');
                      }}
                      onMouseDown={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect();
                        setGsPreviewDrag({ x: e.clientX, y: e.clientY, width: rect.width, height: rect.height });
                      }}
                      onMouseUp={finishGsPreviewDrag}
                      onMouseLeave={() => setGsPreviewDrag(null)}
                    >
                      <img
                        src={`${API_BASE_URL}${gsPreviewItem.preview_proxy_url || `/api/geoserver/preview/${gsPreviewItem.name}.png`}?width=760&height=320${currentPreviewBbox ? `&bbox=${currentPreviewBbox.join(',')}` : ''}`}
                        alt={`${gsPreviewItem.name} GeoServer WMS 预览`}
                      />
                    </div>
                    <div className="gs-preview-actions">
                      <button className="btn-mini" onClick={() => copyText(gsPreviewItem.urls?.wms_get_map_template, `${gsPreviewItem.name} WMS 已复制`)}>复制 WMS</button>
                      <button className="btn-mini" onClick={() => copyText(gsPreviewItem.urls?.wmts_xyz, `${gsPreviewItem.name} WMTS 已复制`)}>复制 WMTS</button>
                      <button className="btn-mini" onClick={() => copyText(gsPreviewItem.urls?.wfs_get_feature, `${gsPreviewItem.name} WFS 已复制`)}>复制 WFS</button>
                    </div>
                    <div className="gs-preview-meta">
                      <span>当前范围：{currentPreviewBbox ? currentPreviewBbox.map(v => Number(v).toFixed(4)).join(', ') : '暂无 bounds'}</span>
                    </div>
                  </>
                ) : (
                  <div className="gs-empty-preview">选择一个已发布图层后显示 WMS 缩略图。</div>
                )}
              </div>
            </div>
            {!gsAvailable && <div className="gs-config-hint">请确认后端 `.env` 中 GEOSERVER_URL / GEOSERVER_USER / GEOSERVER_PASSWORD 已配置，且 GeoServer 容器可访问。</div>}
          </div>
        )}
      </div>

      {toast && <div className={`tm-toast ${toast.type}`}>{toast.message}</div>}

      {error && (
        <div className="tm-error-alert">
          <span className="error-icon">⚠️</span>
          {error}
          <button className="close-alert" onClick={() => setError(null)}>×</button>
        </div>
      )}

      <form className="tm-build-card drone-build-card" onSubmit={handleDroneBuildSubmit}>
        <div className="content-header">
          <div>
            <h3>无人机 GeoTIFF 构建 MBTiles</h3>
            <p className="section-desc">适合单采区大影像年度更新，构建完成后会自动出现在图层列表和 2D 地图图层控制中。</p>
          </div>
          <button className="btn-primary drone-primary" type="submit" disabled={isDroneBuilding}>
            {isDroneBuilding ? '构建中...' : '构建 MBTiles'}
          </button>
        </div>
        <div className="build-sections">
          <div className="build-section">
            <div className="section-title drone-title">影像信息</div>
            <div className="build-grid grid-drone-basic">
              <label className="form-item" style={{ position: 'relative' }}>
                <span>GeoTIFF 路径</span>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    style={{ flex: 1 }}
                    value={droneForm.sourcePath}
                    onChange={(e) => handleDroneChange('sourcePath', e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setShowFileBrowser(true)}
                    style={{
                      padding: '4px 12px', border: '1px solid #722ed1', borderRadius: 6,
                      background: '#722ed1', color: '#fff', cursor: 'pointer', fontSize: 13,
                      whiteSpace: 'nowrap',
                    }}
                  >📂 浏览</button>
                </div>
              </label>
              <label className="form-item">
                <span>图层 Key</span>
                <input
                  value={droneForm.layerKey}
                  onChange={(e) => handleDroneChange('layerKey', e.target.value)}
                />
              </label>
              <label className="form-item">
                <span>显示名称</span>
                <input
                  value={droneForm.name}
                  onChange={(e) => handleDroneChange('name', e.target.value)}
                />
              </label>
              <label className="form-item compact-item">
                <span>采区标识</span>
                <input
                  value={droneForm.areaKey}
                  onChange={(e) => handleDroneChange('areaKey', e.target.value)}
                />
              </label>
            </div>
          </div>

          <div className="build-section">
            <div className="section-title drone-title">构建参数</div>
            <div className="build-grid grid-drone-params">
              <label className="form-item compact-item">
                <span>年份</span>
                <input type="number" value={droneForm.year} onChange={(e) => handleDroneChange('year', e.target.value)} />
              </label>
              <label className="form-item compact-item">
                <span>最小缩放</span>
                <input type="number" min="0" max="22" value={droneForm.minZoom} onChange={(e) => handleDroneChange('minZoom', e.target.value)} />
              </label>
              <label className="form-item compact-item">
                <span>最大缩放</span>
                <input type="number" min="0" max="22" value={droneForm.maxZoom} onChange={(e) => handleDroneChange('maxZoom', e.target.value)} />
              </label>
              <label className="form-item compact-item">
                <span>透明度</span>
                <input type="number" min="0" max="1" step="0.05" value={droneForm.opacity} onChange={(e) => handleDroneChange('opacity', e.target.value)} />
              </label>
              <label className="form-item compact-item">
                <span>瓦片格式</span>
                <select value={droneForm.tileFormat} onChange={(e) => handleDroneChange('tileFormat', e.target.value)}>
                  <option value="PNG">PNG</option>
                  <option value="PNG8">PNG8</option>
                  <option value="JPEG">JPEG</option>
                </select>
              </label>
              <label className="form-item compact-item">
                <span>JPEG质量</span>
                <input type="number" min="1" max="100" value={droneForm.quality} onChange={(e) => handleDroneChange('quality', e.target.value)} />
              </label>
              <label className="form-item compact-item">
                <span>源坐标系</span>
                <input
                  type="text"
                  placeholder="如 EPSG:4547，留空自动检测"
                  value={droneForm.sourceSrs}
                  onChange={(e) => handleDroneChange('sourceSrs', e.target.value)}
                />
              </label>
            </div>
          </div>
        </div>
      </form>

      {buildProgress && (
        <div style={{
          margin: '0 0 20px', padding: '16px 24px',
          background: buildProgress.stage === 'error' ? '#fff2f0' : buildProgress.stage === 'done' ? '#f6ffed' : '#f9f0ff',
          border: `1px solid ${buildProgress.stage === 'error' ? '#ffccc7' : buildProgress.stage === 'done' ? '#b7eb8f' : '#d3adf7'}`,
          borderRadius: 10,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: buildProgress.stage === 'error' ? '#cf1322' : buildProgress.stage === 'done' ? '#389e0d' : '#531dab' }}>
              {buildProgress.stage === 'error' ? '❌ 构建失败' : buildProgress.stage === 'done' ? '✅ 构建完成' : '🔨 正在构建...'}
            </span>
            <span style={{ fontSize: 13, color: '#666' }}>{buildProgress.percent}%</span>
          </div>
          <div style={{ background: '#e8e8e8', borderRadius: 6, height: 10, overflow: 'hidden' }}>
            <div style={{
              width: `${buildProgress.percent}%`,
              height: '100%',
              borderRadius: 6,
              background: buildProgress.stage === 'error' ? '#ff4d4f' : buildProgress.stage === 'done' ? '#52c41a' : 'linear-gradient(90deg, #722ed1, #b37feb)',
              transition: 'width 0.4s ease',
            }} />
          </div>
          <div style={{ marginTop: 6, fontSize: 12, color: '#888' }}>{buildProgress.message}</div>
        </div>
      )}

      <div className="tm-build-card drone-publish-card">
        <div className="content-header">
          <div>
            <h3>影像发布到 GeoServer</h3>
            <p className="section-desc">构建完成的 GeoTIFF/MBTiles 影像会显示在这里，点击发布后可获取 WMS / WMTS 服务地址。</p>
          </div>
          <button className="btn-refresh" type="button" onClick={fetchLayers} disabled={isLoading}>
            刷新影像列表
          </button>
        </div>
        {droneLayers.length ? (
          <div className="drone-publish-list">
            {droneLayers.map((layer) => {
              const gsLayer = gsLayers[layer.key];
              const publishing = gsActionLayer === `publish:${layer.key}` || gsActionLayer === `resync:${layer.key}`;
              return (
                <div className="drone-publish-item" key={`drone-publish-${layer.key}`}>
                  <div className="drone-publish-info">
                    <strong>{layer.label || layer.key}</strong>
                    <small title={layer.source_path}>Key：{layer.key}｜源数据：{layer.source_name || layer.source_path || '-'}</small>
                  </div>
                  <span className={`tag ${gsLayer ? 'tm-gs-tag-published' : 'tm-gs-tag-missing'}`}>
                    {gsLayer ? 'GeoServer 已发布' : '未发布到 GeoServer'}
                  </span>
                  <div className="gs-row-actions">
                    {gsLayer ? (
                      <>
                        <button
                          className="btn-action"
                          type="button"
                          onClick={() => copyText(gsLayer.urls?.wmts_xyz || gsLayer.urls?.wms_get_map_template, '影像 WMTS/WMS URL 已复制')}
                        >
                          复制 WMTS/WMS
                        </button>
                        <button
                          className="btn-action"
                          type="button"
                          onClick={() => {
                            setGsExpanded(true);
                            handleGsPreviewSelect(gsLayer);
                          }}
                        >
                          预览
                        </button>
                        <button
                          className="btn-action"
                          type="button"
                          onClick={() => handleGeoServerPublish(layer.key, 'resync')}
                          disabled={!gsAvailable || publishing}
                        >
                          {publishing ? '同步中...' : '重发布'}
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn-action primary-light"
                        type="button"
                        onClick={() => handleGeoServerPublish(layer.key)}
                        disabled={!gsAvailable || publishing}
                      >
                        {publishing ? '发布中...' : '发布到 GeoServer'}
                      </button>
                    )}
                    <button
                      className="btn-action danger"
                      type="button"
                      onClick={() => handleDroneDelete(layer.key, layer.label)}
                      disabled={droneDeleteKey === layer.key}
                      title="删除此无人机影像"
                    >
                      {droneDeleteKey === layer.key ? '删除中...' : '🗑 删除'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="drone-publish-empty">暂无可发布影像，请先在上方构建 MBTiles。</div>
        )}
      </div>

      <div className="tm-main-content">
        <div className="content-header">
          <h3>图层列表 ({layers.length})</h3>
          <button className="btn-refresh" onClick={fetchLayers} disabled={isLoading}>
            刷新
          </button>
        </div>

        {isLoading ? (
          <div className="tm-loading-state">
            <div className="spinner"></div>
            <p>正在获取切片信息...</p>
          </div>
        ) : (
          <div className="tm-table-wrapper">
            <table className="tm-table">
              <thead>
                <tr>
                  <th>图层</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>切片数</th>
                  <th>磁盘占用</th>
                  <th>缩放范围</th>
                  <th>API 地址</th>
                  <th>目录地址</th>
                  <th>源数据</th>
                  <th>GeoServer</th>
                  <th>GeoServer 发布</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {layers.map((layer) => {
                  const typeTag = getTypeTag(layer.type);
                  const statusTag = getStatusTag(layer.status);
                  const gsLayer = gsLayers[layer.key];
                  const publishing = gsActionLayer === `publish:${layer.key}` || gsActionLayer === `resync:${layer.key}`;
                  const unpublishing = gsActionLayer === `unpublish:${layer.key}`;
                  return (
                    <tr key={`${layer.key}-${layer.type}`}>
                      <td>
                        <div className="layer-name">
                          <span className="layer-swatch" style={{ background: layer.color || '#999' }}></span>
                          {layer.label}
                        </div>
                      </td>
                      <td>
                        <span className="tag" style={{ color: typeTag.color, background: typeTag.bg, border: `1px solid ${typeTag.border}` }}>
                          {typeTag.label}
                        </span>
                      </td>
                      <td>
                        <span className="tag" style={{ color: statusTag.color, background: statusTag.bg, border: `1px solid ${statusTag.border}` }}>
                          {statusTag.label}
                        </span>
                      </td>
                      <td className="num-cell">{layer.tile_count?.toLocaleString() || '-'}</td>
                      <td className="num-cell">{formatSize(layer.size_bytes)}</td>
                      <td className="num-cell">z{layer.min_zoom} - z{layer.max_zoom}</td>
                      <td>
                        <code className="api-url">{layer.api_url}</code>
                      </td>
                      <td className="directory-cell" title={layer.directory}>
                        <code className="api-url">{layer.directory}</code>
                      </td>
                      <td className="source-cell" title={layer.source_path}>
                        {layer.source_name || '-'}
                      </td>
                      <td>
                        <span className={`tag ${gsLayer ? 'tm-gs-tag-published' : 'tm-gs-tag-missing'}`} title={gsLayer?.qualified_name || '未发布到 GeoServer'}>
                          {gsLayer ? '已发布' : '未发布'}
                        </span>
                      </td>
                      <td>
                        <div className="gs-row-actions">
                          {gsLayer ? (
                            <>
                              <button className="btn-action" onClick={() => handleGeoServerPublish(layer.key, 'resync')} disabled={!gsAvailable || publishing || unpublishing}>
                                {publishing ? '同步中...' : '重发布'}
                              </button>
                              <button className="btn-action danger" onClick={() => handleGeoServerUnpublish(layer.key)} disabled={!gsAvailable || publishing || unpublishing}>
                                {unpublishing ? '取消中...' : '取消发布'}
                              </button>
                              <button className="btn-action" onClick={() => copyText(gsLayer.urls?.wmts_xyz || gsLayer.urls?.wms_get_map_template || gsLayer.urls?.wfs_get_feature, 'GeoServer 图层 URL 已复制')}>
                                复制服务URL
                              </button>
                            </>
                          ) : (
                            <button className="btn-action primary-light" onClick={() => handleGeoServerPublish(layer.key)} disabled={!gsAvailable || publishing}>
                              {publishing ? '发布中...' : '发布到GeoServer'}
                            </button>
                          )}
                        </div>
                      </td>
                      <td>
                        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                          {layer.type === 'vector' && (
                            <button
                              className="btn-action"
                              onClick={() => handleRegenerate(layer.key)}
                              disabled={regenerating === layer.key}
                            >
                              {regenerating === layer.key ? '生成中...' : '重新生成'}
                            </button>
                          )}
                          {layer.type === 'raster' && (
                            <span className="hint-text">实时渲染</span>
                          )}
                          {layer.type === 'drone' && (
                            <span className="hint-text">2D影像图层</span>
                          )}
                          {layer.custom !== false && (
                            <button
                              className="btn-action danger"
                              onClick={() => handleTileLayerDelete(layer)}
                              disabled={tileDeleteKey === layer.key}
                              title={`删除此${layer.type === 'drone' ? '影像' : '图层'}`}
                              style={{ fontSize: 12 }}
                            >
                              {tileDeleteKey === layer.key ? '删除中...' : '🗑 删除'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {layers.length === 0 && (
              <div className="tm-empty-state">
                <div className="empty-icon">🗂️</div>
                <p>暂无已注册的切片图层</p>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="tm-build-card tm-gwc-card">
        <div className="content-header">
          <div>
            <h3>GeoWebCache 预切任务</h3>
            <p className="section-desc">对已发布到 GeoServer 的图层执行 WMTS 缓存 seed / truncate，供 QGIS、ArcGIS Pro 和 WMTS 客户端快速访问。</p>
          </div>
          <button className="btn-refresh" onClick={() => setGwcOpen(!gwcOpen)}>{gwcOpen ? '收起' : '展开'}</button>
        </div>
        {gwcOpen && (
          <div className="gwc-panel">
            <div className="build-grid grid-gwc">
              <label className="form-item">
                <span>GeoServer 图层</span>
                <select value={gwcForm.layer} onChange={(e) => handleGwcChange('layer', e.target.value)}>
                  <option value="">请选择已发布图层</option>
                  {publishedLayers.map(layer => <option value={layer.key} key={layer.key}>{layer.label || layer.key}</option>)}
                </select>
              </label>
              <label className="form-item compact-item"><span>minX</span><input value={gwcForm.minX} onChange={(e) => handleGwcChange('minX', e.target.value)} placeholder="可留空" /></label>
              <label className="form-item compact-item"><span>minY</span><input value={gwcForm.minY} onChange={(e) => handleGwcChange('minY', e.target.value)} placeholder="可留空" /></label>
              <label className="form-item compact-item"><span>maxX</span><input value={gwcForm.maxX} onChange={(e) => handleGwcChange('maxX', e.target.value)} placeholder="可留空" /></label>
              <label className="form-item compact-item"><span>maxY</span><input value={gwcForm.maxY} onChange={(e) => handleGwcChange('maxY', e.target.value)} placeholder="可留空" /></label>
              <label className="form-item compact-item"><span>最小级别</span><input type="number" min="0" max="22" value={gwcForm.minZoom} onChange={(e) => handleGwcChange('minZoom', e.target.value)} /></label>
              <label className="form-item compact-item"><span>最大级别</span><input type="number" min="0" max="22" value={gwcForm.maxZoom} onChange={(e) => handleGwcChange('maxZoom', e.target.value)} /></label>
              <label className="form-item compact-item">
                <span>格式</span>
                <select value={gwcForm.format} onChange={(e) => handleGwcChange('format', e.target.value)}>
                  <option value="image/png">image/png</option>
                  <option value="image/jpeg">image/jpeg</option>
                  <option value="application/vnd.mapbox-vector-tile">MVT</option>
                </select>
              </label>
              <label className="form-item compact-item"><span>线程</span><input type="number" min="1" max="4" value={gwcForm.threads} onChange={(e) => handleGwcChange('threads', e.target.value)} /></label>
            </div>
            <div className="gwc-actions">
              <button className="btn-action" onClick={handleUseLayerBounds} disabled={!gwcForm.layer}>使用图层范围</button>
              <button className="btn-primary" onClick={() => handleGwcSeed('seed')} disabled={!gsAvailable || !gwcForm.layer || gwcLoading}>{gwcLoading ? '提交中...' : '开始预切'}</button>
              <button className="btn-action" onClick={handleGwcStatus} disabled={!gwcForm.layer}>查询进度</button>
              <button className="btn-action danger" onClick={() => handleGwcSeed('truncate')} disabled={!gsAvailable || !gwcForm.layer || gwcLoading}>清空缓存</button>
            </div>
            {gwcProgress && (
              <div className="gwc-progress-card">
                <div className="gwc-progress-head">
                  <strong>{gwcProgress.layer || gwcForm.layer}</strong>
                  <span>{gwcTask?.status || (gwcProgress.available ? '无任务' : gwcProgress.reason || '未知')}</span>
                </div>
                <div className="gwc-progress-bar"><div style={{ width: `${gwcTask?.percent || 0}%` }} /></div>
                <div className="gwc-progress-meta">已处理 {gwcTask?.processed ?? '-'} / 总数 {gwcTask?.total ?? '-'} / 剩余 {gwcTask?.remaining ?? '-'}</div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ---- 3D Tiles 数据集管理 ---- */}
      <div className="tm-build-card tm-3dtiles-card">
        <div className="content-header">
          <div>
            <h3>3D Tiles 数据集</h3>
            <p className="section-desc">管理 Cesium 3D Tiles 数据集（倾斜摄影、BIM 等）。支持 zip 上传和服务器目录注册。</p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className="btn-action primary-light"
              onClick={() => { setShowThreeDUpload(!showThreeDUpload); setShowThreeDRegister(false); }}
              disabled={threeDLoading}
            >
              {showThreeDUpload ? '收起上传' : '📤 上传'}
            </button>
            <button
              className="btn-action"
              onClick={() => { setShowThreeDRegister(!showThreeDRegister); setShowThreeDUpload(false); }}
              disabled={threeDLoading}
            >
              {showThreeDRegister ? '收起注册' : '📂 注册目录'}
            </button>
            <button className="btn-refresh" onClick={() => { fetch3DTilesets(); fetchLayers(); }} disabled={threeDLoading}>
              刷新
            </button>
          </div>
        </div>

        {/* 上传表单 */}
        {showThreeDUpload && (
          <form className="tm-3dtiles-form" onSubmit={handle3DTilesetUpload}>
            <div className="build-grid grid-3dtiles">
              <label className="form-item">
                <span>Zip 文件 *</span>
                <input
                  type="file"
                  accept=".zip"
                  onChange={(e) => handle3DFormChange('file', e.target.files?.[0] || null)}
                />
              </label>
              <label className="form-item">
                <span>数据集 Key *</span>
                <input
                  value={threeDForm.key}
                  onChange={(e) => handle3DFormChange('key', e.target.value)}
                  placeholder="例如 qx-dyt"
                />
              </label>
              <label className="form-item">
                <span>名称</span>
                <input
                  value={threeDForm.name}
                  onChange={(e) => handle3DFormChange('name', e.target.value)}
                  placeholder="数据集的原始名称"
                />
              </label>
              <label className="form-item">
                <span>显示标签</span>
                <input
                  value={threeDForm.label}
                  onChange={(e) => handle3DFormChange('label', e.target.value)}
                  placeholder="在列表中显示的标签"
                />
              </label>
              <label className="form-item compact-item">
                <span>高程偏移(m)</span>
                <input
                  type="number"
                  step="0.1"
                  value={threeDForm.alt_offset}
                  onChange={(e) => handle3DFormChange('alt_offset', e.target.value)}
                />
              </label>
              <label className="tm-checkline" style={{ alignSelf: 'center', marginTop: 18 }}>
                <input
                  type="checkbox"
                  checked={threeDForm.auto_ground_clamp}
                  onChange={(e) => handle3DFormChange('auto_ground_clamp', e.target.checked)}
                />
                <span>自动贴合地面</span>
              </label>
            </div>
            <div style={{ marginTop: 12 }}>
              <button className="btn-primary" type="submit" disabled={threeDLoading}>
                {threeDLoading ? '上传中...' : '上传并注册'}
              </button>
            </div>
          </form>
        )}

        {/* 注册表单 */}
        {showThreeDRegister && (
          <form className="tm-3dtiles-form" onSubmit={handle3DTilesetRegister}>
            <div className="build-grid grid-3dtiles">
              <label className="form-item" style={{ position: 'relative' }}>
                <span>服务器目录 *</span>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    style={{ flex: 1 }}
                    value={threeDForm.directory}
                    onChange={(e) => handle3DFormChange('directory', e.target.value)}
                    placeholder="包含 tileset.json 的目录绝对路径"
                  />
                  <button
                    type="button"
                    onClick={() => setShowThreeDFileBrowser(true)}
                    style={{
                      padding: '4px 12px', border: '1px solid #722ed1', borderRadius: 6,
                      background: '#722ed1', color: '#fff', cursor: 'pointer', fontSize: 13,
                      whiteSpace: 'nowrap',
                    }}
                  >📂 浏览</button>
                </div>
              </label>
              <label className="form-item">
                <span>数据集 Key *</span>
                <input
                  value={threeDForm.key}
                  onChange={(e) => handle3DFormChange('key', e.target.value)}
                  placeholder="唯一标识，例如 my_project_2024"
                />
              </label>
              <label className="form-item">
                <span>名称</span>
                <input
                  value={threeDForm.name}
                  onChange={(e) => handle3DFormChange('name', e.target.value)}
                  placeholder="数据集的原始名称"
                />
              </label>
              <label className="form-item">
                <span>显示标签</span>
                <input
                  value={threeDForm.label}
                  onChange={(e) => handle3DFormChange('label', e.target.value)}
                  placeholder="在列表中显示的标签"
                />
              </label>
              <label className="form-item compact-item">
                <span>高程偏移(m)</span>
                <input
                  type="number"
                  step="0.1"
                  value={threeDForm.alt_offset}
                  onChange={(e) => handle3DFormChange('alt_offset', e.target.value)}
                />
              </label>
              <label className="tm-checkline" style={{ alignSelf: 'center', marginTop: 18 }}>
                <input
                  type="checkbox"
                  checked={threeDForm.auto_ground_clamp}
                  onChange={(e) => handle3DFormChange('auto_ground_clamp', e.target.checked)}
                />
                <span>自动贴合地面</span>
              </label>
            </div>
            <div style={{ marginTop: 12 }}>
              <button className="btn-primary" type="submit" disabled={threeDLoading}>
                {threeDLoading ? '注册中...' : '注册数据集'}
              </button>
            </div>
          </form>
        )}

        {/* 数据集列表 */}
        {threeDTilesets.length ? (
          <div className="tm-table-wrapper" style={{ marginTop: 16 }}>
            <table className="tm-table">
              <thead>
                <tr>
                  <th>数据集</th>
                  <th>标签</th>
                  <th>切片数</th>
                  <th>磁盘占用</th>
                  <th>高程偏移</th>
                  <th>目录路径</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {threeDTilesets.map((ds) => (
                  <tr key={ds.key}>
                    <td>
                      <div className="layer-name">
                        <span className="layer-swatch" style={{ background: '#722ed1' }}></span>
                        {ds.name || ds.key}
                      </div>
                    </td>
                    <td>{ds.label || '-'}</td>
                    <td className="num-cell">{ds.tile_count?.toLocaleString() || '-'}</td>
                    <td className="num-cell">{formatSize(ds.size_bytes)}</td>
                    <td className="num-cell">{ds.alt_offset || 0} m</td>
                    <td className="directory-cell" title={ds.directory}>
                      <code className="api-url">{ds.directory}</code>
                    </td>
                    <td>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                        <button
                          className="btn-action primary-light"
                          onClick={() => handle3DTilesetPreview(ds)}
                          title="在 Cesium 3D 视图中预览"
                        >
                          👁 预览
                        </button>
                        <button
                          className="btn-action"
                          onClick={() => handle3DTilesetRestats(ds)}
                          disabled={threeDLoading}
                          title="重新统计切片数与磁盘占用"
                        >
                          📊 统计
                        </button>
                        <button
                          className="btn-action danger"
                          onClick={() => handle3DTilesetDelete(ds)}
                          disabled={threeDDeleteKey === ds.key}
                          title="删除此数据集"
                        >
                          {threeDDeleteKey === ds.key ? '删除中...' : '🗑 删除'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="drone-publish-empty" style={{ marginTop: 12 }}>
            暂无 3D Tiles 数据集，请上传 zip 文件或注册服务器目录。
          </div>
        )}
      </div>

      {/* 3D Tiles 文件浏览器 */}
      {showThreeDFileBrowser && (
        <FileBrowser
          visible={showThreeDFileBrowser}
          onClose={() => setShowThreeDFileBrowser(false)}
          onSelect={handle3DFileSelect}
          extensions=""
          title="选择 3D Tiles 目录（需包含 tileset.json）"
        />
      )}

      {/* 删除确认弹窗（单次确认 + 可选“同时删除文件”） */}
      <ConfirmDeleteModal
        visible={!!confirmDelete}
        title={
          confirmDelete?.type === 'drone' ? `删除无人机影像「${confirmDelete.label || confirmDelete.key}」` :
          confirmDelete?.type === '3dtiles' ? `删除 3D Tiles「${confirmDelete.label || confirmDelete.key}」` :
          `删除图层「${confirmDelete?.label || confirmDelete?.key}」`
        }
        message={
          confirmDelete?.type === 'drone' ? `将从此列表移除该无人机影像，且不可恢复。` :
          confirmDelete?.type === '3dtiles' ? `将从此列表移除该 3D Tiles 数据集。` :
          `将从此列表移除该图层。`
        }
        fileLabel={
          confirmDelete?.type === 'drone' ? '同时删除磁盘上的 MBTiles / 工作文件（不可恢复）' :
          confirmDelete?.type === '3dtiles' ? '同时删除磁盘上的 3D Tiles 文件（不可恢复）' :
          confirmDelete?.isVirtual ? '同时删除 PostGIS 数据库中的表（不可恢复）' :
          '同时删除磁盘上的切片/源文件'
        }
        onCancel={() => setConfirmDelete(null)}
        onConfirm={
          confirmDelete?.type === 'drone' ? confirmDroneDelete :
          confirmDelete?.type === '3dtiles' ? confirm3DTilesetDelete :
          confirmTileLayerDelete
        }
      />

      <style jsx>{`
        .tile-manager {
          padding: 24px;
          height: 100%;
          display: flex;
          flex-direction: column;
          background: #f0f2f5;
          overflow-y: auto;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial;
        }

        .tm-header {
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

        .tm-status-tag {
          background: #f0f5ff;
          color: #2f54eb;
          border: 1px solid #adc6ff;
          padding: 2px 10px;
          border-radius: 4px;
          font-size: 12px;
        }

        .tm-info-card {
          background: #f0f5ff;
          border: 1px solid #adc6ff;
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
          color: #1d39c4;
        }

        .info-content p {
          margin: 0;
          font-size: 14px;
          color: #666;
          line-height: 1.5;
        }

        .tm-error-alert {
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

        .tm-main-content {
          background: white;
          padding: 24px;
          border-radius: 12px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.05);
          flex: 1;
          display: flex;
          flex-direction: column;
        }

        .tm-build-card {
          background: white;
          padding: 22px 26px 24px;
          border-radius: 12px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.05);
          margin-bottom: 24px;
        }

        .build-sections {
          display: flex;
          flex-direction: column;
          gap: 18px;
        }

        .build-section {
          padding-top: 2px;
        }

        .section-title {
          font-size: 13px;
          font-weight: 600;
          color: #262626;
          margin-bottom: 10px;
          padding-left: 8px;
          border-left: 3px solid #1890ff;
        }

        .build-grid {
          display: grid;
          gap: 16px 20px;
          align-items: end;
        }

        .drone-build-card {
          border: 1px solid #d3adf7;
        }

        .drone-title {
          border-left-color: #722ed1;
        }

        .drone-primary {
          background: #722ed1;
          border-color: #722ed1;
        }

        .section-desc {
          margin: 6px 0 0;
          color: #8c8c8c;
          font-size: 13px;
          line-height: 1.5;
        }

        .grid-drone-basic {
          grid-template-columns: minmax(320px, 1.4fr) minmax(200px, 0.9fr) minmax(260px, 1.1fr) minmax(160px, 0.7fr);
        }

        .grid-drone-params {
          grid-template-columns: 110px 120px 120px 120px 120px 120px 180px;
          justify-content: start;
        }

        .form-item {
          display: flex;
          flex-direction: column;
          gap: 7px;
          font-size: 12px;
          color: #595959;
        }

        .form-item span {
          line-height: 1;
        }

        .form-item input,
        .form-item select {
          border: 1px solid #d9d9d9;
          border-radius: 4px;
          padding: 8px 10px;
          font-size: 13px;
          background: white;
          min-height: 34px;
          box-sizing: border-box;
          width: 100%;
        }

        .form-item input[type="color"] {
          width: 44px;
          height: 34px;
          padding: 3px;
        }

        .file-item input {
          padding: 5px 8px;
        }

        .btn-primary {
          background: #1890ff;
          border: 1px solid #1890ff;
          color: white;
          padding: 7px 16px;
          border-radius: 4px;
          cursor: pointer;
          font-size: 13px;
          min-width: 88px;
        }

        .btn-primary:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }

        .content-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 18px;
          gap: 16px;
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

        .tm-table-wrapper {
          flex: 1;
          overflow: auto;
        }

        .tm-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 13px;
        }

        .tm-table th {
          background: #fafafa;
          padding: 10px 12px;
          text-align: left;
          font-weight: 500;
          color: #666;
          border-bottom: 1px solid #f0f0f0;
          white-space: nowrap;
        }

        .tm-table td {
          padding: 12px;
          border-bottom: 1px solid #f5f5f5;
          vertical-align: middle;
        }

        .tm-table tr:hover td {
          background: #fafafa;
        }

        .layer-name {
          display: flex;
          align-items: center;
          gap: 8px;
          font-weight: 500;
          color: #262626;
        }

        .layer-swatch {
          width: 12px;
          height: 12px;
          border-radius: 3px;
          display: inline-block;
        }

        .tag {
          padding: 2px 8px;
          border-radius: 4px;
          font-size: 11px;
          white-space: nowrap;
        }

        .num-cell {
          text-align: right;
          color: #595959;
          font-variant-numeric: tabular-nums;
        }

        .api-url {
          background: #f5f5f5;
          padding: 2px 6px;
          border-radius: 3px;
          font-size: 11px;
          color: #595959;
          word-break: break-all;
        }

        .source-cell {
          max-width: 120px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          color: #8c8c8c;
          font-size: 12px;
        }

        .directory-cell {
          max-width: 220px;
        }

        .btn-action {
          background: white;
          border: 1px solid #d9d9d9;
          padding: 4px 10px;
          border-radius: 4px;
          cursor: pointer;
          font-size: 12px;
          transition: all 0.3s;
        }

        .btn-action:hover:not(:disabled) {
          color: #1890ff;
          border-color: #1890ff;
        }

        .btn-action:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .hint-text {
          color: #8c8c8c;
          font-size: 12px;
          font-style: italic;
        }

        .tm-loading-state {
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

        .tm-geoserver-card {
          background: white;
          border: 1px solid #d9d9d9;
          border-radius: 12px;
          padding: 18px 22px;
          margin-bottom: 24px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.04);
        }

        .tm-geoserver-card.gs-ok {
          border-color: #b7eb8f;
        }

        .tm-geoserver-card.gs-off {
          border-color: #ffccc7;
        }

        .gs-main-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 18px;
        }

        .gs-title-wrap {
          display: flex;
          align-items: center;
          gap: 12px;
          min-width: 260px;
        }

        .gs-health-dot {
          font-size: 20px;
        }

        .gs-title-wrap h4 {
          margin: 0 0 4px;
          font-size: 16px;
        }

        .gs-title-wrap p {
          margin: 0;
          color: #8c8c8c;
          font-size: 12px;
        }

        .gs-stat-grid {
          flex: 1;
          display: grid;
          grid-template-columns: repeat(4, minmax(90px, 1fr));
          gap: 10px;
        }

        .gs-stat-grid div {
          background: #fafafa;
          border: 1px solid #f0f0f0;
          border-radius: 8px;
          padding: 8px 10px;
        }

        .gs-stat-grid strong {
          display: block;
          color: #262626;
          font-variant-numeric: tabular-nums;
        }

        .gs-stat-grid span {
          font-size: 11px;
          color: #8c8c8c;
        }

        .gs-actions,
        .gs-row-actions,
        .gwc-actions {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          align-items: center;
        }

        .gs-detail-panel {
          margin-top: 14px;
          border-top: 1px solid #f0f0f0;
          padding-top: 12px;
        }

        .gs-quick-publish {
          background: linear-gradient(135deg, #f0f8ff, #ffffff);
          border: 1px solid #91d5ff;
          border-radius: 12px;
          padding: 14px;
          margin-bottom: 14px;
        }

        .gs-quick-grid {
          display: grid;
          grid-template-columns: minmax(240px, 1.4fr) minmax(160px, 0.8fr) minmax(180px, 1fr) repeat(5, minmax(86px, 0.45fr));
          gap: 10px;
          align-items: end;
        }

        .gs-quick-actions {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-top: 12px;
        }

        .tm-checkline {
          display: flex;
          flex-direction: row;
          align-items: center;
          gap: 8px;
          color: #595959;
          font-size: 12px;
        }

        .tm-checkline input[type="checkbox"] {
          width: auto;
          min-height: auto;
        }

        .tm-checkline em {
          color: #8c8c8c;
          font-style: normal;
          line-height: 1.3;
        }

        .gs-detail-section {
          margin-bottom: 14px;
        }

        .gs-detail-section h5,
        .gs-preview-head h5 {
          margin: 0;
          font-size: 13px;
          color: #262626;
        }

        .gs-url-row {
          display: grid;
          grid-template-columns: 120px minmax(240px, 1fr) 56px;
          gap: 8px;
          align-items: center;
          margin-bottom: 8px;
          font-size: 12px;
        }

        .gs-url-row code {
          background: #f5f5f5;
          border-radius: 4px;
          padding: 5px 8px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .gs-layer-preview-grid {
          display: grid;
          grid-template-columns: minmax(240px, 0.9fr) minmax(360px, 1.4fr);
          gap: 14px;
          margin-top: 14px;
        }

        .gs-published-list,
        .gs-preview-panel {
          border: 1px solid #f0f0f0;
          border-radius: 10px;
          background: #fff;
          padding: 12px;
          min-height: 220px;
        }

        .gs-preview-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          margin-bottom: 10px;
        }

        .gs-preview-head span {
          color: #8c8c8c;
          font-size: 12px;
          white-space: nowrap;
        }

        .gs-layer-item {
          width: 100%;
          border: 1px solid #f0f0f0;
          background: #fafafa;
          border-radius: 8px;
          padding: 10px;
          margin-bottom: 8px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          cursor: pointer;
          text-align: left;
        }

        .gs-layer-item.active {
          border-color: #91d5ff;
          background: #e6f7ff;
        }

        .gs-layer-item strong {
          display: block;
          color: #262626;
          font-size: 13px;
          margin-bottom: 3px;
        }

        .gs-layer-item small {
          color: #8c8c8c;
          font-size: 11px;
        }

        .gs-preview-image {
          height: 230px;
          border: 1px solid #f0f0f0;
          border-radius: 8px;
          overflow: hidden;
          background:
            linear-gradient(45deg, #fafafa 25%, transparent 25%),
            linear-gradient(-45deg, #fafafa 25%, transparent 25%),
            linear-gradient(45deg, transparent 75%, #fafafa 75%),
            linear-gradient(-45deg, transparent 75%, #fafafa 75%);
          background-size: 18px 18px;
          background-position: 0 0, 0 9px, 9px -9px, -9px 0;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: grab;
        }

        .gs-preview-image img {
          width: 100%;
          height: 100%;
          object-fit: contain;
        }

        .gs-preview-actions {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 10px;
        }

        .gs-preview-toolbar {
          display: flex;
          gap: 6px;
          flex-wrap: wrap;
          margin-bottom: 8px;
        }

        .gs-preview-meta {
          margin-top: 8px;
          color: #8c8c8c;
          font-size: 12px;
          word-break: break-all;
        }

        .gs-empty-preview {
          color: #8c8c8c;
          font-size: 12px;
          padding: 24px 0;
          text-align: center;
        }

        .btn-mini {
          border: 1px solid #d9d9d9;
          background: white;
          border-radius: 4px;
          padding: 4px 8px;
          cursor: pointer;
          font-size: 12px;
        }

        .gs-config-hint {
          color: #cf1322;
          font-size: 12px;
          margin-top: 8px;
        }

        .tm-toast {
          position: fixed;
          top: 18px;
          right: 24px;
          z-index: 10001;
          background: #f6ffed;
          color: #389e0d;
          border: 1px solid #b7eb8f;
          padding: 10px 14px;
          border-radius: 8px;
          box-shadow: 0 4px 14px rgba(0,0,0,0.12);
          font-size: 13px;
        }

        .tm-gs-tag-published {
          color: #52c41a;
          background: #f6ffed;
          border: 1px solid #b7eb8f;
        }

        .tm-gs-tag-missing {
          color: #8c8c8c;
          background: #fafafa;
          border: 1px solid #d9d9d9;
        }

        .btn-action.primary-light {
          color: #1890ff;
          border-color: #91d5ff;
          background: #e6f7ff;
        }

        .drone-publish-card {
          border: 1px solid #91d5ff;
        }

        .drone-publish-list {
          display: flex;
          flex-direction: column;
          gap: 10px;
        }

        .drone-publish-item {
          display: grid;
          grid-template-columns: minmax(260px, 1fr) auto auto;
          gap: 14px;
          align-items: center;
          padding: 12px 14px;
          border: 1px solid #f0f0f0;
          border-radius: 10px;
          background: #fafcff;
        }

        .drone-publish-info {
          display: flex;
          flex-direction: column;
          gap: 4px;
          min-width: 0;
        }

        .drone-publish-info strong {
          color: #262626;
          font-size: 14px;
        }

        .drone-publish-info small {
          color: #8c8c8c;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .drone-publish-empty {
          padding: 18px;
          border: 1px dashed #d9d9d9;
          border-radius: 10px;
          color: #8c8c8c;
          background: #fafafa;
          text-align: center;
        }

        .btn-action.danger:hover:not(:disabled) {
          color: #ff4d4f;
          border-color: #ff4d4f;
        }

        .tm-gwc-card {
          margin-top: 24px;
          border: 1px solid #91d5ff;
        }

        .gwc-panel {
          display: flex;
          flex-direction: column;
          gap: 14px;
        }

        .grid-gwc {
          grid-template-columns: minmax(220px, 1.2fr) repeat(4, minmax(96px, 0.6fr)) repeat(4, minmax(108px, 0.6fr));
        }

        .gwc-progress-card {
          background: #fafafa;
          border: 1px solid #f0f0f0;
          border-radius: 8px;
          padding: 12px;
        }

        .gwc-progress-head {
          display: flex;
          justify-content: space-between;
          font-size: 13px;
          margin-bottom: 8px;
        }

        .gwc-progress-bar {
          height: 9px;
          background: #e8e8e8;
          border-radius: 6px;
          overflow: hidden;
        }

        .gwc-progress-bar div {
          height: 100%;
          background: linear-gradient(90deg, #1890ff, #52c41a);
          transition: width 0.3s ease;
        }

        .gwc-progress-meta {
          margin-top: 6px;
          color: #8c8c8c;
          font-size: 12px;
        }

        /* ---- 3D Tiles ---- */
        .tm-3dtiles-card {
          margin-top: 24px;
          border: 1px solid #d3adf7;
        }

        .tm-3dtiles-form {
          margin-top: 14px;
          padding: 16px;
          background: #fafafa;
          border-radius: 8px;
          border: 1px solid #f0f0f0;
        }

        .grid-3dtiles {
          grid-template-columns: minmax(240px, 1fr) minmax(160px, 0.7fr) minmax(180px, 0.8fr) minmax(180px, 0.8fr) minmax(120px, 0.5fr) minmax(140px, 0.6fr);
        }

        .tm-empty-state {
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

        @media (max-width: 1200px) {
          .gs-quick-grid {
            grid-template-columns: repeat(3, minmax(180px, 1fr));
          }

          .grid-drone-basic {
            grid-template-columns: repeat(2, minmax(220px, 1fr));
          }

          .grid-drone-params {
            grid-template-columns: repeat(4, minmax(120px, 1fr));
          }
        }

        @media (max-width: 760px) {
          .content-header {
            align-items: flex-start;
            flex-direction: column;
          }

          .gs-quick-grid,
          .grid-drone-basic,
          .grid-drone-params {
            grid-template-columns: 1fr;
          }

          .tm-build-card {
            padding: 18px;
          }
        }
      `}</style>
      <FileBrowser
        visible={showFileBrowser}
        onClose={() => setShowFileBrowser(false)}
        onSelect={handleFileSelect}
        extensions=".tif,.tiff"
        title="选择 GeoTIFF 文件"
      />
      <FileBrowser
        visible={showServerGeoJsonBrowser}
        onClose={() => setShowServerGeoJsonBrowser(false)}
        onSelect={handleServerGeoJsonSelect}
        extensions=".geojson,.json"
        title="选择服务器 GeoJSON 文件"
      />
    </div>
  );
};

export default TileManager;
