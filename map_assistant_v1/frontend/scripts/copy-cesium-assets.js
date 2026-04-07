/**
 * 将 Cesium 静态资源复制到 public/cesium 目录
 * 在 npm start / npm run build 前自动执行
 */
const fs = require('fs-extra');
const path = require('path');

const src = path.resolve(__dirname, '../node_modules/cesium/Build/Cesium');
const dest = path.resolve(__dirname, '../public/cesium');

if (!fs.existsSync(src)) {
  console.warn('[copy-cesium-assets] Cesium build not found at:', src);
  console.warn('[copy-cesium-assets] Run: npm install cesium');
  process.exit(0);
}

console.log('[copy-cesium-assets] Copying Cesium assets...');
console.log('  from:', src);
console.log('  to:  ', dest);

fs.copySync(src, dest, { overwrite: true });
console.log('[copy-cesium-assets] Done.');
