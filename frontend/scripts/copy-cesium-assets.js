/**
 * 将 Cesium 静态资源复制到 public/cesium 目录
 * 在 npm start / npm run build 前自动执行
 */
const fs = require('fs-extra');
const path = require('path');

const src = path.resolve(__dirname, '../node_modules/cesium/Build/Cesium');
const dest = path.resolve(__dirname, '../public/cesium');
const cesiumPkg = path.resolve(__dirname, '../node_modules/cesium/package.json');
const markerFile = path.resolve(dest, '.asset-version');

if (!fs.existsSync(src)) {
  console.warn('[copy-cesium-assets] Cesium build not found at:', src);
  console.warn('[copy-cesium-assets] Run: npm install cesium');
  process.exit(0);
}

let currentVersion = 'unknown';
try {
  const pkg = fs.readJsonSync(cesiumPkg);
  currentVersion = pkg.version || 'unknown';
} catch (_) {
  currentVersion = 'unknown';
}

const existingVersion = fs.existsSync(markerFile)
  ? String(fs.readFileSync(markerFile, 'utf8') || '').trim()
  : '';

if (
  existingVersion &&
  existingVersion === currentVersion &&
  fs.existsSync(path.join(dest, 'Cesium.js'))
) {
  console.log(`[copy-cesium-assets] Skip copy, assets already synced (cesium@${currentVersion}).`);
  process.exit(0);
}

console.log('[copy-cesium-assets] Copying Cesium assets...');
console.log('  from:', src);
console.log('  to:  ', dest);

fs.copySync(src, dest, { overwrite: true });
fs.ensureDirSync(dest);
fs.writeFileSync(markerFile, `${currentVersion}\n`, 'utf8');
console.log('[copy-cesium-assets] Done.');
