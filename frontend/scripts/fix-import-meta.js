/**
 * build 后自动修复 Cesium 残留的 import.meta 表达式
 * 在非 module 环境（普通 script 标签）中 import.meta 会抛语法错误
 */
const fs = require('fs');
const path = require('path');
const glob = require('glob') || { sync: (p) => require('child_process').execSync(`find ${path.dirname(p)} -name "${path.basename(p)}"`, { encoding: 'utf8' }).trim().split('\n').filter(Boolean) };

const buildDir = path.resolve(__dirname, '../build/static/js');

const files = fs.readdirSync(buildDir).filter(f => f.endsWith('.js'));
let totalFixed = 0;

files.forEach(file => {
  const filePath = path.join(buildDir, file);
  let content = fs.readFileSync(filePath, 'utf8');
  const before = (content.match(/import\.meta/g) || []).length;
  if (before > 0) {
    // 替换 Cesium 常见的 import.meta 用法
    content = content.replace(/null===import\.meta\|\|void 0==={}/g, 'true');
    content = content.replace(/import\.meta\.url/g, '""');
    content = content.replace(/import\.meta/g, 'undefined');
    fs.writeFileSync(filePath, content, 'utf8');
    const after = (content.match(/import\.meta/g) || []).length;
    console.log(`[fix-import-meta] ${file}: ${before} → ${after} occurrences`);
    totalFixed += before - after;
  }
});

console.log(`[fix-import-meta] Done. Fixed ${totalFixed} occurrence(s).`);
