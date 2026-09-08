// utils/geo.js — 2D/3D 共享的地理渲染工具（消除 MapComponent / CesiumComponent 复制粘贴）

/**
 * 风险色标：Control_Elevation - Measured_Depth 差值 → RGB。
 * diff <= -1 绿（安全），0~4+ 渐变到红（超深风险）。
 * @param {number} diff 高程差值（米）
 * @returns {[number, number, number]}
 */
export function riskDiffToRGB(diff) {
  if (diff <= -1) return [34, 197, 94];
  if (diff <= 0) {
    const s = (diff + 1) / 1;
    return [Math.round(34 + (74 - 34) * s), Math.round(197 + (222 - 197) * s), Math.round(94 + (128 - 94) * s)];
  }
  const t = Math.min(diff / 4, 1);
  if (t < 0.4) {
    const s = t / 0.4;
    return [Math.round(74 + (250 - 74) * s), Math.round(222 + (204 - 222) * s), Math.round(128 + (21 - 128) * s)];
  } else if (t < 0.7) {
    const s = (t - 0.4) / 0.3;
    return [Math.round(250 + (249 - 250) * s), Math.round(204 + (115 - 204) * s), Math.round(21 + (22 - 21) * s)];
  }
  const s = (t - 0.7) / 0.3;
  return [Math.round(249 + (220 - 249) * s), Math.round(115 + (38 - 115) * s), Math.round(22 + (38 - 22) * s)];
}

/**
 * 凸包（Graham Scan，经纬度点集），供风险面外轮廓描边。
 * @param {Array<{lng:number,lat:number}>} points
 */
export function convexHull(points) {
  if (points.length < 3) return points.slice();
  const sorted = points.slice().sort((a, b) => a.lng - b.lng || a.lat - b.lat);
  const cross = (o, a, b) =>
    (a.lng - o.lng) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lng - o.lng);
  const lower = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
    upper.push(p);
  }
  lower.pop();
  upper.pop();
  return lower.concat(upper);
}
