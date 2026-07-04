import React, { useState, useRef, useEffect, useCallback } from 'react';
import './GisPipeline.css';

const API_BASE = ''; // 所有请求使用相对路径

// 显示名 → 真实文件系统路径 映射表
const DIR_PATH_MAP = {
  'arcgisorgdata (默认)': '/mnt/arcgisorgdata',
  'bim与遥感5015': '/mnt/bim5015'
};

function resolvePath(displayName) {
  return DIR_PATH_MAP[displayName] || displayName;
}

export default function GisPipeline() {
  // 基础路径与输出
  const [baseDir, setBaseDir] = useState('/mnt/arcgisorgdata');
  const [outDir, setOutDir] = useState('/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像');
  const [quickDir, setQuickDir] = useState('arcgisorgdata (默认)');

  // 已选影像列表（相对路径）
  const [tifs, setTifs] = useState('');
  const [_browseSelected, setBrowseSelected] = useState(new Set());

  // GDAL 优化参数
  const [compress, setCompress] = useState('ZSTD');
  const [zstdLevel, setZstdLevel] = useState(3);
  const [parallel, setParallel] = useState(4);
  const [threads, setThreads] = useState(12);
  const [cache, setCache] = useState(32768);
  const [predictor, setPredictor] = useState(2);
  const [tiled, setTiled] = useState('YES');
  const [bigtiff, setBigtiff] = useState('YES');

  // 处理步骤开关
  const [doBlackEdge, setDoBlackEdge] = useState(false);
  const [doReproject, setDoReproject] = useState(true);
  const [doResample, setDoResample] = useState(true);
  const [doMosaic, setDoMosaic] = useState(true);

  // 步骤参数
  const [epsg, setEpsg] = useState(3857);
  const [resVal, setResVal] = useState(2);
  const [finalName, setFinalName] = useState('merged_output.tif');

  // 黑边参数
  const [beNodata, setBeNodata] = useState(0);
  const [beType, setBeType] = useState('Byte');
  const [beParallel, setBeParallel] = useState(2);

  // 批量去黑边
  const [beFiles, setBeFiles] = useState([]);
  const [beOutDir, setBeOutDir] = useState('');
  const [beSRS, setBeSRS] = useState('');

  // 脚本与日志
  const [codeEditor, setCodeEditor] = useState('');
  const [logContent, setLogContent] = useState('等待任务启动...');
  const [currentTaskId, setCurrentTaskId] = useState(null);
  const [taskRunning, setTaskRunning] = useState(false);

  // 文件浏览器
  const [fileModalVisible, setFileModalVisible] = useState(false);
  const [fileModalPath, setFileModalPath] = useState('');
  const [fileModalItems, setFileModalItems] = useState([]);
  const [fileModalMode, setFileModalMode] = useState('select'); // 'select' | 'be'
  const [fileModalConfirmText, setFileModalConfirmText] = useState('确认选择');

  // Refs
  const logTimerRef = useRef(null);
  const browseSelectedRef = useRef(new Set());
  const beFileBrowserPathRef = useRef('');

  // 生成流水线脚本
  const generate = useCallback(() => {
    const inBase = resolvePath(baseDir).replace(/\/$/, "");
    const outBase = resolvePath(outDir).replace(/\/$/, "");
    const tifLines = tifs.split('\n').filter(l => l.trim());

    let co = `-co TILED=${tiled} -co BIGTIFF=${bigtiff} -co COMPRESS=${compress} -co PREDICTOR=${predictor} -co NUM_THREADS=${threads}`;
    if (compress === 'ZSTD') co += ` -co ZSTD_LEVEL=${zstdLevel}`;

    let s = `#!/bin/bash\n# --- GIS PIPELINE GENERATED ---\nset -e\nexport GDAL_CACHEMAX=${cache}\n\n`;

    let lastIn = inBase;
    let currentFiles = tifLines.map(f => `"${inBase}/${f.trim()}"`);

    // STEP 0: 去除黑边
    if (doBlackEdge && tifLines.length > 0) {
      const stepDir = `${outBase}/0_blackedge`;
      let beArgs = ` -a_nodata ${beNodata} -ot ${beType} -of GTiff`;
      s += `# [STEP 0] 去除黑边 → ${stepDir}\n`;
      s += `mkdir -p "${stepDir}"\n`;
      s += `cat <<EOF | xargs -I {} -P ${beParallel} bash -c "{}"\n`;
      tifLines.forEach((f, i) => {
        s += `gdal_translate${beArgs} "${inBase}/${f.trim()}" "${stepDir}/be_${i}.tif"\n`;
      });
      s += `EOF\n\n`;
      lastIn = stepDir;
    }

    // STEP 1: 重投影
    if (doReproject) {
      const stepDir = `${outBase}/1_proj`;
      s += `# [STEP 1] 重投影至 EPSG:${epsg}\nmkdir -p "${stepDir}"\n`;
      s += `cat <<EOF | xargs -I {} -P ${parallel} bash -c "{}"\n`;
      tifLines.forEach((f, i) => {
        s += `gdalwarp -t_srs EPSG:${epsg} ${co} -overwrite "${inBase}/${f.trim()}" "${stepDir}/p_${i}.tif"\n`;
      });
      s += `EOF\n\n`;
      lastIn = stepDir;
    }

    // STEP 2: 重采样
    if (doResample) {
      const stepDir = `${outBase}/2_resample`;
      s += `# [STEP 2] 重采样至 ${resVal}m\nmkdir -p "${stepDir}"\n`;
      if (lastIn === inBase) {
        s += `for f in ${currentFiles.join(' ')}; do\n    gdal_edit.py -a_srs EPSG:4490 "$f" 2>/dev/null || true\ndone\n`;
      } else {
        s += `for f in "${lastIn}"/*.tif; do\n    gdal_edit.py -a_srs EPSG:4490 "$f" 2>/dev/null || true\ndone\n`;
      }
      s += `gdalbuildvrt "${outBase}/tmp.vrt" ${lastIn === inBase ? currentFiles.join(' ') : `"${lastIn}"/*.tif`}\n`;
      s += `gdalwarp -tr ${resVal} ${resVal} -r bilinear ${co} -overwrite "${outBase}/tmp.vrt" "${stepDir}/resampled_full.tif"\nrm "${outBase}/tmp.vrt"\n\n`;
      lastIn = `${stepDir}/resampled_full.tif`;
    }

    // STEP 3: 镶嵌与金字塔
    if (doMosaic) {
      const finalPath = `${outBase}/${finalName}`;
      s += `# [STEP 3] 镶嵌与金字塔\n`;
      if (!doResample) {
        s += `# SRS 归一化：统一所有栅格的 CRS 元数据\n`;
        if (lastIn === inBase) {
          s += `for f in ${currentFiles.join(' ')}; do\n    gdal_edit.py -a_srs EPSG:4490 "$f" 2>/dev/null || true\ndone\n`;
        } else {
          s += `for f in "${lastIn}"/*.tif; do\n    gdal_edit.py -a_srs EPSG:4490 "$f" 2>/dev/null || true\ndone\n`;
        }
        s += `# 波段颜色解释归一化\n`;
        if (lastIn === inBase && currentFiles.length > 0) {
          const firstFile = currentFiles[0];
          s += `_band_count=$(gdalinfo -json ${firstFile} 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('bands',[])))" 2>/dev/null || echo 0)\n`;
        } else {
          s += `_first_file=$(ls "${lastIn}"/*.tif 2>/dev/null | head -1)\n`;
          s += `_band_count=$(gdalinfo -json "$_first_file" 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('bands',[])))" 2>/dev/null || echo 0)\n`;
        }
        s += `if [ "$_band_count" = "3" ]; then\n`;
        if (lastIn === inBase) {
          s += `    for f in ${currentFiles.join(' ')}; do\n        gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue "$f" 2>/dev/null || true\n    done\n`;
        } else {
          s += `    for f in "${lastIn}"/*.tif; do\n        gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue "$f" 2>/dev/null || true\n    done\n`;
        }
        s += `elif [ "$_band_count" = "4" ]; then\n`;
        if (lastIn === inBase) {
          s += `    for f in ${currentFiles.join(' ')}; do\n        gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue -colorinterp_4 alpha "$f" 2>/dev/null || true\n    done\n`;
        } else {
          s += `    for f in "${lastIn}"/*.tif; do\n        gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue -colorinterp_4 alpha "$f" 2>/dev/null || true\n    done\n`;
        }
        s += `fi\n`;
        s += `gdalbuildvrt "${outBase}/merge.vrt" ${lastIn === inBase ? currentFiles.join(' ') : `"${lastIn}"/*.tif`}\n`;
        s += `gdal_translate ${co} "${outBase}/merge.vrt" "${finalPath}"\nrm "${outBase}/merge.vrt"\n`;
      } else {
        s += `mv "${lastIn}" "${finalPath}"\n`;
      }
      s += `gdaladdo -r average --config GDAL_NUM_THREADS ${threads} "${finalPath}" 2 4 8 16 32\n`;
    }

    s += `\necho "DONE: $(date)"`;
    setCodeEditor(s);
  }, [baseDir, outDir, tifs, compress, zstdLevel, parallel, threads, cache, predictor, tiled, bigtiff,
      doBlackEdge, doReproject, doResample, doMosaic, epsg, resVal, finalName, beNodata, beType, beParallel]);

  // 参数变化自动刷新脚本
  useEffect(() => { generate(); }, [generate]);

  // 清理定时器
  useEffect(() => () => {
    if (logTimerRef.current) clearInterval(logTimerRef.current);
  }, []);

  // 快捷目录切换
  const switchDirectory = (selectEl) => {
    const displayText = selectEl.options[selectEl.selectedIndex].text;
    const realPath = selectEl.options[selectEl.selectedIndex].dataset.path;
    setBaseDir(displayText);
    setOutDir(realPath);
    setTifs('');
    setBrowseSelected(new Set());
    browseSelectedRef.current = new Set();
    setQuickDir(displayText);
  };

  // 日志轮询
  const startLogPolling = (taskId) => {
    if (logTimerRef.current) clearInterval(logTimerRef.current);
    setCurrentTaskId(taskId);
    setTaskRunning(true);

    logTimerRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/gis-tool/get-log/${taskId}`);
        const data = await res.json();
        setLogContent(data.logs || 'Preparing...');
        if (data.status === 'finished') {
          clearInterval(logTimerRef.current);
          logTimerRef.current = null;
          setTaskRunning(false);
        }
      } catch (e) {
        console.error('日志轮询错误:', e);
      }
    }, 1000);
  };

  // 停止任务
  const stopTask = async () => {
    if (!currentTaskId) return;
    try {
      await fetch(`${API_BASE}/gis-tool/stop-task/${currentTaskId}`, { method: 'POST' });
    } catch (e) {
      console.error('停止任务错误:', e);
    }
    if (logTimerRef.current) { clearInterval(logTimerRef.current); logTimerRef.current = null; }
    setTaskRunning(false);
    // 重新拉取最终日志
    try {
      const res = await fetch(`${API_BASE}/gis-tool/get-log/${currentTaskId}`);
      const data = await res.json();
      setLogContent(data.logs);
    } catch (e) { }
  };

  // 提交流水线任务
  const submitPipeline = async () => {
    const finalScript = codeEditor;
    if (!finalScript.trim()) return;
    const res = await fetch(`${API_BASE}/gis-tool/run-task`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script_content: finalScript })
    });
    const d = await res.json();
    startLogPolling(d.task_id);
  };

  // 文件浏览器
  const openFileBrowser = async (path = null, mode = 'select') => {
    const inBase = resolvePath(baseDir);
    if (!path) {
      path = inBase;
      // 首次打开从 tifs 初始化选择集
      const fromTifs = tifs.split('\n').filter(l => l.trim());
      browseSelectedRef.current = new Set(fromTifs);
    } else {
      // 目录切换前捕获当前页勾选状态
      document.querySelectorAll('.file-check:checked').forEach(cb => browseSelectedRef.current.add(cb.value));
      document.querySelectorAll('.file-check:not(:checked)').forEach(cb => browseSelectedRef.current.delete(cb.value));
    }
    beFileBrowserPathRef.current = path;
    setFileModalMode(mode);
    setFileModalConfirmText(mode === 'be' ? '➕ 添加选中文件' : '确认选择');
    setFileModalPath(path);
    setFileModalVisible(true);

    try {
      const res = await fetch(`${API_BASE}/gis-tool/list-files?path=${encodeURIComponent(path)}`);
      const data = await res.json();
      setFileModalItems(data.items || []);
    } catch (e) {
      console.error('文件列表加载失败:', e);
      setFileModalItems([]);
    }
  };

  const handleFileModalClick = (fullPath, type, isDir) => {
    if (isDir) {
      openFileBrowser(fullPath, fileModalMode);
    }
  };

  const confirmSelection = () => {
    document.querySelectorAll('.file-check:checked').forEach(cb => browseSelectedRef.current.add(cb.value));
    document.querySelectorAll('.file-check:not(:checked)').forEach(cb => browseSelectedRef.current.delete(cb.value));
    setTifs([...browseSelectedRef.current].join('\n'));
    setBrowseSelected(new Set(browseSelectedRef.current));
    setFileModalVisible(false);
  };

  const confirmBeFiles = () => {
    const checked = Array.from(document.querySelectorAll('.be-file-check:checked'));
    checked.forEach(cb => {
      const inp = cb.value;
      const fname = inp.split('/').pop().replace(/\.[^.]+$/, '') + '_noedge.tif';
      const customOutDir = beOutDir.trim();
      const out = customOutDir ? customOutDir + '/' + fname : inp.substring(0, inp.lastIndexOf('/')) + '/' + fname;
      if (!beFiles.find(f => f.input_file === inp)) {
        setBeFiles(prev => [...prev, { input_file: inp, output_file: out }]);
      }
    });
    setFileModalVisible(false);
  };

  const handleFileConfirm = () => {
    if (fileModalMode === 'be') {
      confirmBeFiles();
    } else {
      confirmSelection();
    }
  };

  const removeBeFile = (idx) => {
    setBeFiles(prev => prev.filter((_, i) => i !== idx));
  };

  const clearBeFiles = () => {
    setBeFiles([]);
  };

  // 批量去黑边执行
  const submitBeTask = async () => {
    if (beFiles.length === 0) { alert('请先添加要处理的文件'); return; }
    const body = {
      files: beFiles,
      nodata_value: parseFloat(beNodata),
      output_type: beType,
      parallel: parseInt(beParallel) || 1
    };
    const srs = beSRS.trim();
    if (srs) body.src_srs = srs;

    const res = await fetch(`${API_BASE}/gis-tool/remove-blackedge-batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await res.json();
    if (d.error) { alert(d.error); return; }
    setCodeEditor(d.script || '');
    startLogPolling(d.task_id);
  };

  // 批量去黑边预览
  const bePreviewText = (() => {
    const srs = beSRS.trim();
    let common = '';
    if (srs) common += ` -a_srs ${srs}`;
    common += ` -a_nodata ${beNodata} -ot ${beType} -of GTiff`;

    if (doBlackEdge) {
      let preview = `# 批量去除黑边 | ${beFiles.length} 个文件 | 并行数: ${beParallel}\n`;
      beFiles.forEach((f, i) => {
        preview += `# [${i + 1}] gdal_translate${common} "${f.input_file}" "${f.output_file}"\n`;
      });
      return preview || '# 暂无文件';
    }
    return '';
  })();

  // 计算父路径
  const getParentPath = (path) => {
    const base = resolvePath(baseDir);
    if (path.length > base.length) {
      return path.substring(0, path.lastIndexOf('/'));
    }
    return path;
  };

  // 获取相对路径（相对于 baseDir）
  const getRelativePath = (fullPath) => {
    const base = resolvePath(baseDir);
    return fullPath.replace(base, '').replace(/^\//, '');
  };

  return (
    <div className="gis-pipeline">
      {/* 左侧控制面板 */}
      <div className="gis-pipeline-left">
        <h1 className="gis-pipeline-title">PIPELINE ENGINE v8.1</h1>

        {/* 快捷目录 */}
        <div className="gis-config-card">
          <div>
            <label className="gis-label-pro">快捷目录</label>
            <select className="gis-input-pro" value={quickDir} onChange={(e) => switchDirectory(e.target)}>
              <option value="arcgisorgdata (默认)" data-path="/mnt/arcgisorgdata">arcgisorgdata (默认)</option>
              <option value="bim与遥感5015" data-path="/mnt/bim5015">bim与遥感5015</option>
            </select>
          </div>
          <div className="gis-grid-2">
            <div>
              <label className="gis-label-pro">数据源 (Source)</label>
              <input type="text" className="gis-input-pro" value={baseDir}
                onChange={(e) => setBaseDir(e.target.value)} />
            </div>
            <div>
              <label className="gis-label-pro">输出目录 (Output)</label>
              <input type="text" className="gis-input-pro" value={outDir}
                onChange={(e) => setOutDir(e.target.value)} />
            </div>
          </div>
          <button className="gis-btn-browse" onClick={() => openFileBrowser()}>
            📂 浏览资产
          </button>
          <textarea className="gis-input-pro gis-ta" rows="2" placeholder="已选影像..."
            value={tifs} onChange={(e) => setTifs(e.target.value)} />
        </div>

        {/* GDAL 优化参数 */}
        <div className="gis-config-card gis-grid-2">
          <div className="gis-col-span-2">
            <label className="gis-label-pro gis-text-blue">GDAL Optimization (8 Params)</label>
          </div>
          <div><label className="gis-label-pro">1. 压缩</label>
            <select className="gis-input-pro" value={compress} onChange={(e) => setCompress(e.target.value)}>
              <option value="ZSTD">ZSTD</option><option value="LZW">LZW</option>
            </select>
          </div>
          <div><label className="gis-label-pro">2. ZSTD级</label>
            <input type="number" className="gis-input-pro" value={zstdLevel}
              onChange={(e) => setZstdLevel(Number(e.target.value))} />
          </div>
          <div><label className="gis-label-pro">3. 并行进程</label>
            <input type="number" className="gis-input-pro" value={parallel}
              onChange={(e) => setParallel(Number(e.target.value))} />
          </div>
          <div><label className="gis-label-pro">4. 线程/进程</label>
            <input type="number" className="gis-input-pro" value={threads}
              onChange={(e) => setThreads(Number(e.target.value))} />
          </div>
          <div><label className="gis-label-pro">5. GDAL缓存</label>
            <input type="number" className="gis-input-pro" value={cache}
              onChange={(e) => setCache(Number(e.target.value))} />
          </div>
          <div><label className="gis-label-pro">6. PREDICTOR</label>
            <select className="gis-input-pro" value={predictor} onChange={(e) => setPredictor(Number(e.target.value))}>
              <option value="1">1</option><option value="2">2</option><option value="3">3</option>
            </select>
          </div>
          <div><label className="gis-label-pro">7. 分块 (TILED)</label>
            <select className="gis-input-pro" value={tiled} onChange={(e) => setTiled(e.target.value)}>
              <option value="YES">YES</option><option value="NO">NO</option>
            </select>
          </div>
          <div><label className="gis-label-pro">8. BIGTIFF</label>
            <select className="gis-input-pro" value={bigtiff} onChange={(e) => setBigtiff(e.target.value)}>
              <option value="YES">YES</option><option value="NO">NO</option><option value="IF_NEEDED">IF_NEEDED</option>
            </select>
          </div>
        </div>

        {/* 处理步骤 */}
        <div className="gis-steps-section">
          {/* STEP 0 */}
          <div className="gis-config-card gis-step-purple">
            <label className="gis-step-check">
              <input type="checkbox" checked={doBlackEdge} onChange={(e) => setDoBlackEdge(e.target.checked)} />
              &nbsp;STEP 0. 去除黑边 (gdal_translate)
            </label>
            {doBlackEdge && (
              <div className="gis-grid-3">
                <div>
                  <label className="gis-label-pro">NoData值</label>
                  <input type="number" className="gis-input-pro" value={beNodata} step="any"
                    onChange={(e) => setBeNodata(Number(e.target.value))} />
                </div>
                <div>
                  <label className="gis-label-pro">输出类型</label>
                  <select className="gis-input-pro" value={beType} onChange={(e) => setBeType(e.target.value)}>
                    <option value="Byte">Byte</option><option value="UInt16">UInt16</option>
                    <option value="Int16">Int16</option><option value="Float32">Float32</option>
                  </select>
                </div>
                <div>
                  <label className="gis-label-pro">并行数</label>
                  <input type="number" className="gis-input-pro" value={beParallel} min="1" max="16"
                    onChange={(e) => setBeParallel(Number(e.target.value))} />
                </div>
              </div>
            )}
          </div>

          {/* STEP 1 */}
          <div className="gis-config-card gis-step-blue">
            <div className="gis-step-row">
              <label className="gis-step-check">
                <input type="checkbox" checked={doReproject} onChange={(e) => setDoReproject(e.target.checked)} />
                &nbsp;STEP 1. 重投影
              </label>
              <input type="text" className="gis-input-pro gis-input-short" value={epsg}
                onChange={(e) => setEpsg(e.target.value)} placeholder="EPSG" />
            </div>
          </div>

          {/* STEP 2 */}
          <div className="gis-config-card gis-step-orange">
            <div className="gis-step-row">
              <label className="gis-step-check">
                <input type="checkbox" checked={doResample} onChange={(e) => setDoResample(e.target.checked)} />
                &nbsp;STEP 2. 重采样
              </label>
              <div className="gis-input-group-inline">
                <input type="number" className="gis-input-pro gis-input-short" value={resVal}
                  onChange={(e) => setResVal(Number(e.target.value))} />
                <span className="gis-input-hint">M</span>
              </div>
            </div>
          </div>

          {/* STEP 3 */}
          <div className="gis-config-card gis-step-green">
            <div className="gis-step-row">
              <label className="gis-step-check">
                <input type="checkbox" checked={doMosaic} onChange={(e) => setDoMosaic(e.target.checked)} />
                &nbsp;STEP 3. 镶嵌与金字塔
              </label>
              <div className="gis-input-group-inline">
                <span className="gis-input-hint">文件名:</span>
                <input type="text" className="gis-input-pro" value={finalName}
                  onChange={(e) => setFinalName(e.target.value)} />
              </div>
            </div>
          </div>
        </div>

        {/* 独立模式：批量去除黑边 */}
        <div className="gis-config-card gis-step-purple-dim">
          <div className="gis-step-row">
            <label className="gis-label-pro gis-text-purple">🧪 独立模式：批量去除黑边</label>
            <span className="gis-input-hint-dim">不参与流水线，单独执行</span>
          </div>
          <div className="gis-grid-2">
            <div>
              <label className="gis-label-pro">输出目录</label>
              <input type="text" className="gis-input-pro" value={beOutDir}
                onChange={(e) => setBeOutDir(e.target.value)} placeholder="默认与输入文件同目录" />
            </div>
            <div>
              <label className="gis-label-pro">坐标系(可选)</label>
              <input type="text" className="gis-input-pro" value={beSRS}
                onChange={(e) => setBeSRS(e.target.value)} placeholder="EPSG:4326" />
            </div>
          </div>
          <div className="gis-step-row">
            <label className="gis-label-pro gis-mb0">待处理文件列表</label>
            <div className="gis-btn-group">
              <button className="gis-btn-xs gis-btn-purple"
                onClick={() => openFileBrowser(null, 'be')}>➕ 添加文件</button>
              <button className="gis-btn-xs gis-btn-red" onClick={clearBeFiles}>✖ 清空</button>
            </div>
          </div>
          <div className="gis-be-file-list">
            {beFiles.length === 0 ? (
              <div className="gis-be-empty">点击》➕ 添加文件《选择要处理的影像...</div>
            ) : (
              beFiles.map((f, i) => (
                <div key={i} className="gis-be-file-item">
                  <span className="gis-be-in" title={f.input_file}>{f.input_file.split('/').pop()}</span>
                  <span className="gis-be-arrow">→</span>
                  <span className="gis-be-out">{f.output_file.split('/').pop()}</span>
                  <button className="gis-be-remove" onClick={() => removeBeFile(i)}>✕</button>
                </div>
              ))
            )}
          </div>
          {doBlackEdge && beFiles.length > 0 && (
            <div className="gis-be-preview">{bePreviewText}</div>
          )}
        </div>

        {/* 执行按钮 */}
        <div className="gis-btn-area">
          <button className="gis-btn-run" onClick={submitPipeline} disabled={taskRunning}>
            🚀 提交流水线任务
          </button>
          {taskRunning && (
            <button className="gis-btn-stop" onClick={stopTask}>
              ⏹ 停止当前进程
            </button>
          )}
          {beFiles.length > 0 && (
            <button className="gis-btn-be-run" onClick={submitBeTask} disabled={taskRunning}>
              ▶ 执行去除黑边
            </button>
          )}
        </div>
      </div>

      {/* 右侧面板 */}
      <div className="gis-pipeline-right">
        {/* 脚本编辑器 */}
        <div className="gis-editor">
          <div className="gis-editor-header">
            <span className="gis-editor-dot">● 实时生成的 BASH 脚本</span>
          </div>
          <textarea className="gis-editor-text" value={codeEditor} readOnly spellCheck="false" />
        </div>

        {/* 日志终端 */}
        <div className="gis-terminal">
          <div className="gis-terminal-header">
            <span className="gis-terminal-label">终端日志 (STDOUT/STDERR)</span>
            <span className="gis-terminal-taskid">ID: {currentTaskId || 'NONE'}</span>
          </div>
          <pre className="gis-terminal-content">{logContent}</pre>
        </div>
      </div>

      {/* 文件浏览器弹窗 */}
      {fileModalVisible && (
        <div className="gis-modal-overlay" onClick={() => setFileModalVisible(false)}>
          <div className="gis-modal" onClick={(e) => e.stopPropagation()}>
            <div className="gis-modal-body">
              <div className="gis-modal-path">
                Path: {fileModalPath}
                <span className="gis-modal-count">
                  [已选 {browseSelectedRef.current.size} 个]
                </span>
              </div>
              {fileModalPath.length > resolvePath(baseDir).length && (
                <div className="gis-modal-parent"
                  onClick={() => openFileBrowser(getParentPath(fileModalPath), fileModalMode)}>
                  📁 .. 返回上级
                </div>
              )}
              {fileModalItems.map((item, idx) => {
                const full = (fileModalPath + '/' + item.name).replace(/\/+/g, '/');
                if (item.type === 'dir') {
                  return (
                    <div key={idx} className="gis-modal-dir"
                      onClick={() => openFileBrowser(full, fileModalMode)}>
                      📁 {item.name}
                    </div>
                  );
                } else {
                  if (fileModalMode === 'be') {
                    return (
                      <label key={idx} className="gis-modal-file">
                        <input type="checkbox" value={full} className="be-file-check" />
                        &nbsp;🗂 {item.name}
                      </label>
                    );
                  } else {
                    const relPath = getRelativePath(full);
                    const checked = browseSelectedRef.current.has(relPath);
                    return (
                      <label key={idx} className="gis-modal-file">
                        <input type="checkbox" value={relPath} className="file-check"
                          defaultChecked={checked} />
                        &nbsp;{item.name}
                      </label>
                    );
                  }
                }
              })}
            </div>
            <div className="gis-modal-footer">
              <button className="gis-btn-modal-confirm" onClick={handleFileConfirm}>
                {fileModalConfirmText}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
