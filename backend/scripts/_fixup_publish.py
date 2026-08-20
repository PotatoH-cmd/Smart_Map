#!/usr/bin/env python3
"""修复已发布图层的白底/坐标系问题 + 补充商城4个砂场发布。

修复项：
1. 青龙闵塆/淮河村/吴乡村：单波段无Alpha → 已加 -dstalpha，重发布
2. 灌河道超/灌河淮堰-周集/灌河叶岗/沈营：实际4547被误标4548 → 用4547重发
3. 新增商城4个（石牛村/金寨村/王楼村/李湖村）→ EPSG:4548
"""

import json
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from services.tile_manager.publisher import publish_raster_async
from services.tile_manager.tasks import _TILE_BUILD_JOBS
from services.tile_manager.registry import (
    _DRONE_REGISTRY_PATH, _DRONE_MBTILES_DIR, _DRONE_WORK_DIR,
)

SAND_IMAGES_DIR = "/home/server/python/采砂/砂场影像"
LOG_FILE = os.path.join(os.path.dirname(__file__), "_fixup_publish.log")


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def remove_registry_entry(layer_key):
    """从 registry 删除指定条目。"""
    if not os.path.exists(_DRONE_REGISTRY_PATH):
        return
    with open(_DRONE_REGISTRY_PATH) as f:
        reg = json.load(f)
    if layer_key in reg:
        del reg[layer_key]
    with open(_DRONE_REGISTRY_PATH, "w") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)


def clean_mbtiles(layer_key):
    """删除旧的 MBTiles 文件。"""
    path = os.path.join(_DRONE_MBTILES_DIR, f"{layer_key}.mbtiles")
    if os.path.exists(path):
        os.remove(path)
        log(f"  删除旧 MBTiles: {path}")


def clean_warp(layer_key):
    """删除旧的 warped tif。"""
    path = os.path.join(_DRONE_WORK_DIR, f"{layer_key}_3857.tif")
    if os.path.exists(path):
        os.remove(path)


def wait_job(job_id, timeout=3600):
    t0 = time.time()
    last_pct = -1
    while time.time() - t0 < timeout:
        job = _TILE_BUILD_JOBS.get(job_id)
        if job is None:
            return False, 0, "missing"
        pct = job.get("percent", 0)
        stage = job.get("stage", "")
        done = job.get("done", False)
        success = job.get("success", False)
        msg = job.get("message", "")
        if pct != last_pct:
            log(f"    进度 {pct}% ({stage}) {msg[:60]}")
            last_pct = pct
        if done:
            return success, pct, stage
        if stage == "error":
            log(f"    错误: {msg[:200]}")
            return False, pct, "error"
        time.sleep(5)
    log(f"    超时 ({timeout}s)")
    return False, 0, "timeout"


TASKS = [
    # ========== 修复：白底问题（单波段+alpha） ==========
    {"folder": "5豫罗砂许[2025]第04号", "county": "luoshan", "srs": "EPSG:4547",
     "layer_key": "luoshan_qinglong_minwan", "name": "青龙闵塆采区",
     "tifs": ["青龙闵塆采区-1.tif", "青龙闵塆采区-2.tif"], "reason": "白底-加dstalpha"},
    {"folder": "7豫罗砂许[2025]第06号", "county": "luoshan", "srs": "EPSG:4547",
     "layer_key": "luoshan_huaihe", "name": "淮河村采区",
     "tifs": ["淮河村采区.tif"], "reason": "白底-加dstalpha"},
    {"folder": "9豫罗砂许[2025]第08号", "county": "luoshan", "srs": "EPSG:4547",
     "layer_key": "luoshan_wuxiang", "name": "吴乡村采区",
     "tifs": ["吴乡村采区.tif"], "reason": "白底-加dstalpha"},
    # ========== 修复：坐标系从4548→4547 ==========
    {"folder": "31豫信固砂许[2025]第13号", "county": "gushi", "srs": "EPSG:4547",
     "layer_key": "gushi_daochao", "name": "灌河道超可采区",
     "tifs": ["灌河道超可采区.tif"], "reason": "CRS 4548→4547"},
    {"folder": "30豫信固砂许[2025]第12号", "county": "gushi", "srs": "EPSG:4547",
     "layer_key": "gushi_huaiyan_zhouji", "name": "灌河淮堰-周集可采区",
     "tifs": ["灌河淮堰-周集可采区.tif"], "reason": "CRS 4548→4547"},
    {"folder": "34豫信固砂许[2025]第16号", "county": "gushi", "srs": "EPSG:4547",
     "layer_key": "gushi_shenying", "name": "沈营可采区",
     "tifs": ["沈营可采区.tif"], "reason": "CRS 4548→4547"},
    {"folder": "33豫信固砂许[2025]第15号", "county": "gushi", "srs": "EPSG:4547",
     "layer_key": "gushi_yegang", "name": "灌河叶岗可采区",
     "tifs": ["灌河叶岗可采区.tif"], "reason": "CRS 4548→4547"},
    # ========== 新增：商城4个 ==========
    {"folder": "15豫商城砂许[2025]第01号", "county": "shangcheng", "srs": "EPSG:4548",
     "layer_key": "shangcheng_shiniu", "name": "石牛村采区",
     "tifs": ["石牛村采区.tif"], "reason": "新增-商城"},
    {"folder": "16豫商城砂许[2025]第02号", "county": "shangcheng", "srs": "EPSG:4548",
     "layer_key": "shangcheng_jinzhai", "name": "金寨村采区",
     "tifs": ["金寨村采区.tif"], "reason": "新增-商城"},
    {"folder": "17豫商城砂许[2025]第03号", "county": "shangcheng", "srs": "EPSG:4548",
     "layer_key": "shangcheng_wanglou", "name": "王楼村采区",
     "tifs": ["王楼村采区.tif"], "reason": "新增-商城"},
    {"folder": "18豫商城砂许[2025]第04号", "county": "shangcheng", "srs": "EPSG:4548",
     "layer_key": "shangcheng_lihu", "name": "李湖村采区",
     "tifs": ["李湖村采区.tif"], "reason": "新增-商城"},
]

MOSAIC_TMP = "/tmp/sand_mosaic"


def mosaic_tifs(tif_paths, output_path):
    log(f"  镶嵌 {len(tif_paths)} 个 TIF...")
    vrt_path = output_path.replace(".tif", ".vrt")
    subprocess.run(
        ["gdalbuildvrt", "-overwrite", vrt_path] + tif_paths,
        capture_output=True, text=True, check=False, timeout=120,
    )
    result = subprocess.run(
        ["gdal_translate", "-of", "GTiff", "-co", "TILED=YES",
         "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES",
         vrt_path, output_path],
        capture_output=True, text=True, check=False, timeout=600,
    )
    if result.returncode != 0:
        log(f"  镶嵌失败: {result.stderr[-500:]}")
        return None
    for p in [vrt_path, vrt_path + ".aux.xml", output_path + ".aux.xml"]:
        if os.path.exists(p):
            os.remove(p)
    return output_path


def main():
    os.makedirs(MOSAIC_TMP, exist_ok=True)
    try:
        os.remove(LOG_FILE)
    except OSError:
        pass

    total = len(TASKS)
    ok_count = 0
    fail_count = 0
    t_start = time.time()

    log(f"修复/补充发布 {total} 个砂场影像")
    log("=" * 50)

    for i, task in enumerate(TASKS):
        layer_key = task["layer_key"]
        srs = task["srs"]
        name = task["name"]
        reason = task["reason"]
        folder = task["folder"]

        log(f"--- #{i+1}/{total} [{layer_key}] {name} ({reason}) ---")

        # 清理旧数据
        clean_mbtiles(layer_key)
        clean_warp(layer_key)
        remove_registry_entry(layer_key)

        # 收集 TIF 路径
        folder_path = os.path.join(SAND_IMAGES_DIR, folder)
        tif_paths = [os.path.join(folder_path, tn) for tn in task["tifs"] if os.path.isfile(os.path.join(folder_path, tn))]
        if not tif_paths:
            log(f"  没有有效 TIF 文件，跳过")
            fail_count += 1
            continue

        # 镶嵌
        if len(tif_paths) > 1:
            mosaic_path = os.path.join(MOSAIC_TMP, f"{layer_key}.tif")
            source = mosaic_tifs(tif_paths, mosaic_path)
            if source is None:
                fail_count += 1
                continue
        else:
            source = tif_paths[0]

        # 提交发布
        t0 = time.time()
        try:
            job_id = publish_raster_async(
                source_path=source,
                layer_key=layer_key,
                name=name,
                min_zoom=10,
                max_zoom=22,
                opacity=0.95,
                overwrite=True,
                source_srs=srs,
            )
            log(f"  提交 job={job_id} srs={srs}")
        except Exception as e:
            log(f"  提交失败: {e}")
            fail_count += 1
            continue

        success, pct, stage = wait_job(job_id)
        elapsed = time.time() - t0
        status = "OK" if success else f"FAIL({stage})"
        log(f"  结果: {status} 耗时 {elapsed:.0f}s")

        if success:
            ok_count += 1
        else:
            fail_count += 1

        elapsed_total = time.time() - t_start
        log(f"  汇总: OK {ok_count} / FAIL {fail_count} / 总 {i+1}/{total}  已运行 {elapsed_total:.0f}s")

    elapsed_total = time.time() - t_start
    log("=" * 50)
    log(f"完成！OK {ok_count} / FAIL {fail_count} / 总 {total}")
    log(f"总耗时 {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)")

    import shutil
    if os.path.exists(MOSAIC_TMP):
        shutil.rmtree(MOSAIC_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
