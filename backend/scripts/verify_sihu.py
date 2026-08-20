#!/usr/bin/env python3
"""单站点报告生成验证（复用 batch 的环境配置）。"""
import json, logging, os, sys, time

BACKEND_DIR = '/home/server/python/map_assistant_v1/backend'
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

log_path = os.path.join(BACKEND_DIR, 'logs', 'verify_sihu.log')
os.makedirs(os.path.dirname(log_path), exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding='utf-8')])

env_path = os.path.join(BACKEND_DIR, '.env')
if os.path.exists(env_path):
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

_rasterio_proj = '/home/server/miniconda3/envs/mapagent6/lib/python3.11/site-packages/rasterio/proj_data'
if os.path.isdir(_rasterio_proj):
    os.environ['PROJ_LIB'] = _rasterio_proj
    os.environ['PROJ_DATA'] = _rasterio_proj

from tools.caisha_report_tool import CaishaReportTool

tool = CaishaReportTool()
t0 = time.time()
res = tool.call({'site_name': '史河泗湖-长兴可采区'})
print('耗时 %.1fs' % (time.time() - t0))
print(json.dumps(res, ensure_ascii=False, indent=1)[:2000])
