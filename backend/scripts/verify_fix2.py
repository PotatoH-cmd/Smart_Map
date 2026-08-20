#!/usr/bin/env python3
"""验证 fix2：单波段/裁剪失败站点应使用高分彩色影像生成正射影像图。"""
import json, logging, os, re, sys, time

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

logging.basicConfig(level=logging.WARNING)
os.environ.setdefault('DASHSCOPE_API_KEY', 'sk-e4990da94bfb4037be1f755fa586d048')

_rasterio_proj = '/home/server/miniconda3/envs/mapagent6/lib/python3.11/site-packages/rasterio/proj_data'
if os.path.isdir(_rasterio_proj):
    os.environ['PROJ_LIB'] = _rasterio_proj
    os.environ['PROJ_DATA'] = _rasterio_proj

from tools.caisha_report_tool import CaishaReportTool

SITES = ['青龙闵塆采区', '史灌河童庙可采区', '淮河村采区']
tool = CaishaReportTool()

for site in SITES:
    t0 = time.time()
    print(f'\n{"="*60}\n[{site}]')
    try:
        result = tool.call({'site_name': site})
        notes = str(result.get('notes', ''))
        print(f'  success={result.get("success")} 耗时{time.time()-t0:.0f}s')
        print(f'  notes={notes[:300]}')
        if result.get('success'):
            # 检查生成的 ortho png 是否彩色
            fp = result.get('file_path', '')
            out_dir = os.path.join(BACKEND_DIR, 'static', 'reports')
            if os.path.isdir(out_dir):
                cands = sorted(
                    (f for f in os.listdir(out_dir) if f.startswith('caisha_') and f.endswith('_ortho.png')),
                    key=lambda f: os.path.getmtime(os.path.join(out_dir, f)), reverse=True)
                if cands:
                    import numpy as np
                    from PIL import Image
                    p = os.path.join(out_dir, cands[0])
                    img = np.asarray(Image.open(p).convert('RGB'))
                    b0, b1, b2 = img[..., 0].mean(), img[..., 1].mean(), img[..., 2].mean()
                    color = '彩色' if max(b0, b1, b2) - min(b0, b1, b2) > 8 else '灰色!'
                    print(f'  ortho: {cands[0]} RGB=({b0:.0f},{b1:.0f},{b2:.0f}) → {color}')
            if fp and os.path.exists(fp):
                print(f'  报告: {fp} ({os.path.getsize(fp)/1024:.0f}KB)')
    except Exception as e:
        import traceback
        print(f'  EXCEPTION: {e}')
        traceback.print_exc()
