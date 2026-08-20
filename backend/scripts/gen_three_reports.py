#!/usr/bin/env python3
"""
为青龙闵塆采区、淮河村采区、吴乡村采区单独生成采砂监测报告。
（修复灰度影像bug后的验证运行）
"""
import json, logging, os, sys, time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

log_path = os.path.join(BACKEND_DIR, 'logs', 'three_reports.log')
os.makedirs(os.path.dirname(log_path), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding='utf-8'),
    ])
logger = logging.getLogger('three_reports')

# 加载 .env
env_path = os.path.join(BACKEND_DIR, '.env')
if os.path.exists(env_path):
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
    logger.info('已加载 .env')

# 修复 PROJ_LIB
_rasterio_proj = '/home/server/miniconda3/envs/mapagent6/lib/python3.11/site-packages/rasterio/proj_data'
if os.path.isdir(_rasterio_proj):
    os.environ['PROJ_LIB'] = _rasterio_proj
    os.environ['PROJ_DATA'] = _rasterio_proj
    logger.info('PROJ_LIB=%s', _rasterio_proj)

logger.info('DASHSCOPE_API_KEY=%s', 'SET' if os.environ.get('DASHSCOPE_API_KEY') else 'MISSING!')

from tools.caisha_report_tool import CaishaReportTool

# 三个目标站点（数据库中的准确名称）
SITES = [
    ("青龙闵塆采区", "5豫罗砂许[2025]第04号"),
    ("淮河村采区",   "7豫罗砂许[2025]第06号"),
    ("吴乡村采区",   "9豫罗砂许[2025]第08号"),
]

tool = CaishaReportTool()
out_dir = os.path.join(BACKEND_DIR, 'static', 'reports')
os.makedirs(out_dir, exist_ok=True)

results = []

for idx, (site, folder_hint) in enumerate(SITES, 1):
    print(f'\n{"="*60}')
    print(f'[{idx}/{len(SITES)}] {site}')
    print(f'{"="*60}')
    t0 = time.time()
    try:
        result = tool.call({'site_name': site})
        elapsed = time.time() - t0
        if result.get('success'):
            filepath = result.get('file_path', '')
            notes_str = str(result.get('notes', ''))
            has_ortho = '影像图缺失' not in notes_str and '影像裁剪失败' not in notes_str
            has_elev = '高程点示意图' not in notes_str or '失败' not in notes_str
            fsize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else 0
            print(f'  ✓ 成功: {os.path.basename(filepath)}  ({elapsed:.0f}s | {fsize/1024:.0f}KB)')
            print(f'  正射影像: {"✓" if has_ortho else "✗"} | 高程图: {"✓" if has_elev else "✗"}')
            print(f'  notes: {notes_str[:200]}')
            results.append({'site': site, 'file': filepath, 'success': True})
        else:
            err = result.get('error', 'unknown')
            print(f'  ✗ 失败: {err}  ({elapsed:.0f}s)')
            results.append({'site': site, 'error': err, 'success': False})
    except Exception as e:
        elapsed = time.time() - t0
        import traceback
        print(f'  ✗ 异常: {e}  ({elapsed:.0f}s)')
        traceback.print_exc()
        results.append({'site': site, 'error': str(e), 'success': False})

print(f'\n{"="*60}')
success_count = sum(1 for r in results if r['success'])
print(f'完成: 成功 {success_count}/{len(SITES)}')
for r in results:
    if r['success']:
        print(f'  ✓ {r["site"]}: {os.path.basename(r["file"])}')
    else:
        print(f'  ✗ {r["site"]}: {r["error"]}')
