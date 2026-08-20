#!/usr/bin/env python3
"""修复商城4个砂场：EPSG:4548 → 4547"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.tile_manager.publisher import publish_raster_async
from services.tile_manager.tasks import _TILE_BUILD_JOBS

SAND = '/home/server/python/采砂/砂场影像'
MBTILES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'drone_imagery', 'mbtiles'))
REGISTRY = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'drone_imagery', 'registry.json'))

TASKS = [
    ('15豫商城砂许[2025]第01号', 'shangcheng_shiniu', '石牛村采区', '石牛村采区.tif'),
    ('16豫商城砂许[2025]第02号', 'shangcheng_jinzhai', '金寨村采区', '金寨村采区.tif'),
    ('17豫商城砂许[2025]第03号', 'shangcheng_wanglou', '王楼村采区', '王楼村采区.tif'),
    ('18豫商城砂许[2025]第04号', 'shangcheng_lihu', '李湖村采区', '李湖村采区.tif'),
]

# 清理旧数据
with open(REGISTRY) as f:
    reg = json.load(f)
for _, key, _, _ in TASKS:
    if key in reg:
        del reg[key]
        print(f'删除 registry: {key}')
    mbtiles = os.path.join(MBTILES_DIR, f'{key}.mbtiles')
    if os.path.exists(mbtiles):
        os.remove(mbtiles)
        print(f'删除 MBTiles: {key}')
with open(REGISTRY, 'w') as f:
    json.dump(reg, f, ensure_ascii=False, indent=2)

for folder, key, name, tif_name in TASKS:
    tif_path = os.path.join(SAND, folder, tif_name)
    print(f'\n--- {key}: {name} (EPSG:4547) ---')
    t0 = time.time()
    job_id = publish_raster_async(
        source_path=tif_path, layer_key=key, name=name,
        min_zoom=10, max_zoom=22, opacity=0.95, overwrite=True,
        source_srs='EPSG:4547',
    )
    print(f'  job={job_id}')
    last_pct = -1
    while True:
        time.sleep(5)
        job = _TILE_BUILD_JOBS.get(job_id, {})
        pct = job.get('percent', 0)
        stage = job.get('stage', '')
        done = job.get('done', False)
        success = job.get('success', False)
        if pct != last_pct:
            print(f'  {pct}% ({stage})')
            last_pct = pct
        if done:
            elapsed = time.time() - t0
            print(f'  结果: {"OK" if success else "FAIL"} 耗时 {elapsed:.0f}s')
            break

print('\n全部完成')
