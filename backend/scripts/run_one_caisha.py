# -*- coding: utf-8 -*-
"""试生成单站报告：python run_one_caisha.py <site_name>"""
import json, os, sys, time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

env_path = os.path.join(BACKEND_DIR, '.env')
if os.path.exists(env_path):
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
_proj = '/home/server/miniconda3/envs/mapagent6/lib/python3.11/site-packages/rasterio/proj_data'
if os.path.isdir(_proj):
    os.environ['PROJ_LIB'] = _proj
    os.environ['PROJ_DATA'] = _proj

from tools.caisha_report_tool import CaishaReportTool

site = sys.argv[1] if len(sys.argv) > 1 else '郝楼砂场'
tool = CaishaReportTool()
t0 = time.time()
result = tool.call({'site_name': site})
elapsed = time.time() - t0
print('ELAPSED: %.0fs' % elapsed)
print('SUCCESS:', result.get('success'))
print('ERROR:', result.get('error'))
print('NOTES:', json.dumps(result.get('notes', []), ensure_ascii=False))
v = result.get('variables') or {}
for k in ('site_title', 'display_name', 'permit_no', 'basic_info', 'basic_section_name',
          'monitor_section_name', 'drone_subject', 'drone_scope', 'field_monitoring',
          'drone_interpretation', 'depth_evaluation', 'fig_ortho_no', 'fig_elev_no'):
    print(f'VAR[{k}]:', v.get(k))
print('FILE:', result.get('file_path'))
