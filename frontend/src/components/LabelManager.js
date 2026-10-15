
// ============================================================
// LabelManager：智能标注管理器（缩放级别控制 + 像素碰撞避让）
// 使用 Leaflet Tooltip，在 openTooltip 后用 JS 内联样式强制覆盖默认样式
// 碰撞检测用空间网格(Grid)优化，避免 O(n²) 导致卡顿
// ============================================================
class LabelManager {
  constructor(map) {
    this.map = map;
    this.entries = []; // { latlng, layer, depthText }
    this._refreshTimer = null; // 防抖定时器
  }

  // 对 Tooltip DOM 元素强制注入内联样式（覆盖 Leaflet 默认白色背景）
  _applyStyle(layer) {
    const tooltip = layer.getTooltip();
    if (!tooltip) return;
    const el = tooltip.getElement ? tooltip.getElement() : null;
    if (!el) return;
    el.style.setProperty('background', 'transparent', 'important');
    el.style.setProperty('background-color', 'transparent', 'important');
    el.style.setProperty('border', 'none', 'important');
    el.style.setProperty('box-shadow', 'none', 'important');
    el.style.setProperty('padding', '0', 'important');
    el.style.setProperty('color', '#ef4444', 'important');
    el.style.setProperty('font-size', '14px', 'important');
    el.style.setProperty('font-weight', 'bold', 'important');
    el.style.setProperty('font-family', "'Times New Roman', Times, serif", 'important');
    el.style.setProperty('text-shadow',
      '-2px -2px 0 #fff, 2px -2px 0 #fff, -2px 2px 0 #fff, 2px 2px 0 #fff,' +
      '-2px 0 0 #fff, 2px 0 0 #fff, 0 -2px 0 #fff, 0 2px 0 #fff', 'important');
    el.style.setProperty('white-space', 'nowrap', 'important');
    el.classList.add('map-label-depth');
  }

  register(latlng, layer, depthText) {
    this.entries.push({ latlng, layer, depthText });
  }

  clear() {
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    // 关闭所有已显示的 Tooltip
    this.entries.forEach(({ layer }) => {
      try { layer.closeTooltip(); } catch (_) {}
    });
    this.entries = [];
  }

  // 防抖封装：快速连续缩放时只执行最后一次
  scheduleRefresh(delay = 200) {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this._refreshTimer = setTimeout(() => {
      this._refreshTimer = null;
      this.refresh();
    }, delay);
  }

  refresh() {
    if (!this.map || this.entries.length === 0) return;
    const zoom = this.map.getZoom();
    // === 修复：增大最小像素间距，减少注记密度 ===
    // 原值: zoom>=16→28, 14-15→50, 12-13→80, <12→120
    // 新值: zoom>=17→40, 15-16→65, 13-14→100, 11-12→140, <11→180
    const minPxGap = zoom >= 17 ? 40 : zoom >= 15 ? 65 : zoom >= 13 ? 100 : zoom >= 11 ? 140 : 180;

    // 获取当前视口范围（地理坐标）
    const bounds = this.map.getBounds().pad(0.1); // 稍微扩展一些，避免边缘闪烁

    // 空间网格碰撞检测（O(n) 复杂度）
    const cellSize = minPxGap;
    const grid = new Map();
    const cellKey = (cx, cy) => `${cx},${cy}`;

    const hasClash = (px, py) => {
      const cx = Math.floor(px / cellSize);
      const cy = Math.floor(py / cellSize);
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          const neighbors = grid.get(cellKey(cx + dx, cy + dy));
          if (!neighbors) continue;
          for (const [ox, oy] of neighbors) {
            if (Math.hypot(px - ox, py - oy) < minPxGap) return true;
          }
        }
      }
      return false;
    };

    const addToGrid = (px, py) => {
      const key = cellKey(Math.floor(px / cellSize), Math.floor(py / cellSize));
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push([px, py]);
    };

    // 批量 DOM 操作：先全部关闭，再开启视口内符合条件的
    // 这样只需遍历两次，避免每个点都要判断 open/close 导致大量混杂 DOM 操作
    this.entries.forEach(({ layer }) => {
      try { layer.closeTooltip(); } catch (_) {}
    });

    this.entries.forEach(({ latlng, layer, depthText }) => {
      if (!layer.getTooltip()) return;
      // 忽略视口外的点（这是最关键的优化）
      if (!bounds.contains(latlng)) return;
      try {
        const pt = this.map.latLngToContainerPoint(latlng);
        if (!hasClash(pt.x, pt.y)) {
          layer.setTooltipContent(depthText);
          layer.openTooltip();
          this._applyStyle(layer);
          addToGrid(pt.x, pt.y);
        }
      } catch (_) {}
    });
  }

  destroy() {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this.entries = [];
  }
}

export default LabelManager;
