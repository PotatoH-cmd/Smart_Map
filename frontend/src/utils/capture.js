// utils/capture.js — 地图截图与 Canvas 裁剪工具（从 App.jsx 抽离）

import html2canvas from 'html2canvas';

/**
 * 裁剪 Canvas 空白边缘（基于背景色检测，去除四周多余空白区域）
 */
export function trimCanvasWhitespace(sourceCanvas) {
  const ctx = sourceCanvas.getContext('2d');
  const w = sourceCanvas.width;
  const h = sourceCanvas.height;
  if (w === 0 || h === 0) return sourceCanvas;

  const imageData = ctx.getImageData(0, 0, w, h);
  const data = imageData.data; // RGBA

  // 目标背景色（#f0f2f5 的 RGB）
  const bgR = 240, bgG = 242, bgB = 245;
  const threshold = 25; // 颜色容差

  const isBg = (idx) => {
    const dr = Math.abs(data[idx] - bgR);
    const dg = Math.abs(data[idx + 1] - bgG);
    const db = Math.abs(data[idx + 2] - bgB);
    return dr < threshold && dg < threshold && db < threshold;
  };

  let top = h, bottom = 0, left = w, right = 0;

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const idx = (y * w + x) * 4;
      if (!isBg(idx)) {
        if (y < top) top = y;
        if (y > bottom) bottom = y;
        if (x < left) left = x;
        if (x > right) right = x;
      }
    }
  }

  // 如果全是背景色或几乎无内容，返回原 canvas
  if (top >= bottom || left >= right) {
    console.warn('[Screenshot] 裁剪检测：画布几乎全为背景色，跳过裁剪');
    return sourceCanvas;
  }

  // 添加小边距避免太紧
  const margin = 8;
  const cropW = right - left + margin * 2;
  const cropH = bottom - top + margin * 2;
  const cropX = Math.max(0, left - margin);
  const cropY = Math.max(0, top - margin);

  console.log(`[Screenshot] 裁剪: 原${w}x${h} → ${cropW}x${cropH} (节省 ${(1 - cropW * cropH / (w * h) * 100).toFixed(0)}%面积)`);

  const trimmed = document.createElement('canvas');
  trimmed.width = cropW;
  trimmed.height = cropH;
  const tCtx = trimmed.getContext('2d');
  tCtx.drawImage(sourceCanvas, cropX, cropY, cropW, cropH, 0, 0, cropW, cropH);
  return trimmed;
}

/**
 * 截取地图并保存到服务器，返回服务器端路径
 * @param {string} filePrefix - 文件名前缀
 * @param {object} options - { zoomIn, tileWaitMs }
 * @param {HTMLElement} mapWrapperRef - 地图容器 DOM 引用
 * @param {object} mapManager - 地图管理器实例
 * @param {function} setLastMapScreenshotPath - 设置截屏路径的状态函数
 */
export async function captureMapToServer(filePrefix = 'report_map', options = {}, mapWrapperRef, mapManager, setLastMapScreenshotPath) {
  const target = mapWrapperRef.current;
  if (!target) return null;
  try {
    if (mapManager && options.zoomIn !== false) {
      const mapObj = mapManager.map;
      if (mapObj) {
        let hasVisibleData = false;
        try {
          if (mapManager.overlays) {
            Object.values(mapManager.overlays).forEach(og => {
              if (og && og.getLayers && og.getLayers().length > 0) {
                hasVisibleData = true;
              }
            });
          }
          if (mapManager.markers && mapManager.markers.length > 0) {
            hasVisibleData = true;
          }
        } catch (_) {}

        if (hasVisibleData) {
          try { mapObj.fitBounds(mapObj.getBounds(), { padding: [20, 20], maxZoom: 18 }); } catch (_) {}
        }

        const currentZoom = mapObj.getZoom();
        if (currentZoom < 18) {
          const newZoom = Math.min(Math.round(currentZoom) + 2, 18);
          mapObj.setZoom(newZoom);
        }
      }
    }

    const waitForTiles = options.tileWaitMs || 4000;
    await new Promise(resolve => setTimeout(resolve, waitForTiles));

    let canvas = await html2canvas(target, {
      useCORS: true,
      allowTaint: true,
      logging: false,
      backgroundColor: '#f0f2f5',
      scale: 2,
      imageTimeout: 15000,
      onclone: (clonedDoc) => {
        const tiles = clonedDoc.querySelectorAll('.leaflet-tile-container img, .leaflet-layer img');
        tiles.forEach(img => {
          img.style.display = 'block';
          img.style.visibility = 'visible';
          if (!img.crossOrigin) {
            img.crossOrigin = 'anonymous';
          }
        });
      },
    });

    canvas = trimCanvasWhitespace(canvas);
    const dataUrl = canvas.toDataURL('image/png');
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const res = await fetch('/api/save-screenshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        image_data: dataUrl,
        file_name: `${filePrefix}_${timestamp}.png`,
      }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    const serverPath = data.file_path || null;
    if (serverPath && setLastMapScreenshotPath) setLastMapScreenshotPath(serverPath);
    return serverPath;
  } catch (e) {
    console.warn('地图截图保存失败:', e);
    return null;
  }
}