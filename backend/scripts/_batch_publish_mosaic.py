#!/usr/bin/env python3
"""砂场无人机影像批量发布脚本（串行、镶嵌、规范命名）

处理范围：文件夹 2-34（排除 15-18 商城、12 无TIF、32 不存在）
每文件夹发布完成后等待再处理下一个，避免资源占满。
多 TIF 文件夹先镶嵌再发布。
"""

import json
import os
import subprocess
import sys
import time

# 切换到项目根让相对 import 正确
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from services.tile_manager.publisher import publish_raster_async
from services.tile_manager.tasks import _TILE_BUILD_JOBS

SAND_IMAGES_DIR = "/home/server/python/采砂/砂场影像"
LOG_FILE = os.path.join(os.path.dirname(__file__), "_publish_mosaic.log")
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "_publish_mosaic_results.json")
MOSAIC_TMP = "/tmp/sand_mosaic"

# ============================================================
# 每个文件夹的配置：图层名、砂场名、县区、EPSG
# ============================================================
TASKS = [
    # -- 罗山 (EPSG:4547) --
    {"folder": "2豫罗砂许[2025]第01号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "高店镇王湾村部分河段", "site_key": "gaodian_wangwan",
     "tifs": ["高店镇王湾村部分河段.tif"]},
    {"folder": "3豫罗砂许[2025]第02号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "浉淮村采区", "site_key": "shihuai",
     "tifs": ["浉淮村采区.tif"]},
    {"folder": "4豫罗砂许[2025]第03号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "山店乡青莲村(浅滩未测全)", "site_key": "qinglian",
     "tifs": ["result.tif"]},
    {"folder": "5豫罗砂许[2025]第04号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "青龙闵塆采区", "site_key": "qinglong_minwan",
     "tifs": ["青龙闵塆采区-1.tif", "青龙闵塆采区-2.tif"]},  # 需要镶嵌
    {"folder": "6豫罗砂许[2025]第05号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "中山村采区", "site_key": "zhongshan",
     "tifs": ["中山村采区.tif"]},
    {"folder": "7豫罗砂许[2025]第06号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "淮河村采区", "site_key": "huaihe",
     "tifs": ["淮河村采区.tif"]},
    {"folder": "8豫罗砂许[2025]第07号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "天湖郑洼采区", "site_key": "tianhu_zhengwa",
     "tifs": ["天湖郑洼采区.tif", "天湖郑洼采区3.tif"]},  # 需要镶嵌
    {"folder": "9豫罗砂许[2025]第08号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "吴乡村采区", "site_key": "wuxiang",
     "tifs": ["吴乡村采区.tif"]},
    {"folder": "10豫罗砂许[2025]第09号(未测全为浅滩)", "county": "luoshan", "srs": "EPSG:4547",
     "name": "罗山浅滩(未测全)", "site_key": "qiantan",
     "tifs": ["result.tif"]},
    {"folder": "11豫罗砂许[2025]第10号", "county": "luoshan", "srs": "EPSG:4547",
     "name": "楠杆镇李寨村、邵湾村部分河段", "site_key": "nangan_lizai",
     "tifs": ["楠杆镇李寨村、邵湾村部分河段.tif"]},
    # -- 潢川 (EPSG:4547) --
    {"folder": "13豫潢砂许[2025]第1号", "county": "huangchuan", "srs": "EPSG:4547",
     "name": "郝楼砂场", "site_key": "haolou",
     "tifs": ["郝楼砂场.tif"]},
    {"folder": "14豫潢砂许[2025]第2号", "county": "huangchuan", "srs": "EPSG:4547",
     "name": "黄寨砂场", "site_key": "huangzhai",
     "tifs": ["黄寨砂场.tif"]},
    # -- 固始 (EPSG:4548) --
    {"folder": "19豫信固砂许[2025]第01号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史灌河童庙可采区", "site_key": "tongmiao",
     "tifs": ["史灌河童庙可采区.tif"]},
    {"folder": "20豫信固砂许[2025]第02号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史灌河大营-范营可采区", "site_key": "daying_fanying",
     "tifs": ["史灌河大营-范营可采区.tif"]},
    {"folder": "21豫信固砂许[2025]第03号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史河汪营可采区", "site_key": "wangying",
     "tifs": ["史河汪营可采区.tif"]},
    {"folder": "22豫信固砂许[2025]第04号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史河红石可采区", "site_key": "hongshi",
     "tifs": ["史河红石可采区.tif"]},
    {"folder": "23豫信固砂许[2025]第05号", "county": "gushi", "srs": "EPSG:4548",
     "name": "陈营-柳沟采区", "site_key": "chenying_liugou",
     "tifs": ["陈营-柳沟采区.tif"]},
    {"folder": "24豫信固砂许[2025]第06号", "county": "gushi", "srs": "EPSG:4548",
     "name": "郑堂-大庄采区", "site_key": "zhengtang_dazhuang",
     "tifs": ["郑堂-大庄采区.tif"]},
    {"folder": "25豫信固砂许[2025]第07号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史河余庆-人主可采区", "site_key": "yuqing_renzhu",
     "tifs": ["史河余庆-人主可采区.tif"]},
    {"folder": "26豫信固砂许[2025]第08号", "county": "gushi", "srs": "EPSG:4548",
     "name": "南元、祝家楼采区", "site_key": "nanyuan_zhujialou",
     "tifs": ["南元、祝家楼采区.tif"]},
    {"folder": "27豫信固砂许[2025]第09号", "county": "gushi", "srs": "EPSG:4548",
     "name": "牛老家、龙潭采区", "site_key": "niulaojia_longtan",
     "tifs": ["牛老家、龙潭采区.tif"]},
    {"folder": "28豫信固砂许[2025]第10号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史河毕店可采区", "site_key": "bidian",
     "tifs": ["史河毕店可采区117.tif"]},
    {"folder": "29豫信固砂许[2025]第11号", "county": "gushi", "srs": "EPSG:4548",
     "name": "史河泗湖-长兴可采区", "site_key": "sihu_changxing",
     "tifs": ["史河泗湖-长兴可采区.tif"]},
    {"folder": "30豫信固砂许[2025]第12号", "county": "gushi", "srs": "EPSG:4548",
     "name": "灌河淮堰-周集可采区", "site_key": "huaiyan_zhouji",
     "tifs": ["灌河淮堰-周集可采区.tif"]},
    {"folder": "31豫信固砂许[2025]第13号", "county": "gushi", "srs": "EPSG:4548",
     "name": "灌河道超可采区", "site_key": "daochao",
     "tifs": ["灌河道超可采区.tif"]},
    {"folder": "33豫信固砂许[2025]第15号", "county": "gushi", "srs": "EPSG:4548",
     "name": "灌河叶岗可采区", "site_key": "yegang",
     "tifs": ["灌河叶岗可采区.tif"]},
    {"folder": "34豫信固砂许[2025]第16号", "county": "gushi", "srs": "EPSG:4548",
     "name": "沈营可采区", "site_key": "shenying",
     "tifs": ["沈营可采区.tif"]},
]


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def mosaic_tifs(tif_paths, output_path):
    """将多个 TIF 镶嵌为一个（通过 VRT）。"""
    log(f"  镶嵌 {len(tif_paths)} 个 TIF...")
    vrt_path = output_path.replace(".tif", ".vrt")
    # gdalbuildvrt
    subprocess.run(
        ["gdalbuildvrt", "-overwrite", vrt_path] + tif_paths,
        capture_output=True, text=True, check=False,
        timeout=120,
    )
    # gdal_translate
    result = subprocess.run(
        ["gdal_translate", "-of", "GTiff", "-co", "TILED=YES",
         "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=YES",
         vrt_path, output_path],
        capture_output=True, text=True, check=False,
        timeout=600,
    )
    if result.returncode != 0:
        log(f"  镶嵌失败: {result.stderr[-500:]}")
        return None
    # 清理 vrt
    if os.path.exists(vrt_path):
        os.remove(vrt_path)
    # 清理 xml
    for p in [vrt_path + ".aux.xml", output_path + ".aux.xml"]:
        if os.path.exists(p):
            os.remove(p)
    return output_path


def wait_job(job_id, timeout=3600):
    """等待单个 job 完成，返回 (success, percent, stage)。"""
    t0 = time.time()
    last_pct = -1
    while time.time() - t0 < timeout:
        job = _TILE_BUILD_JOBS.get(job_id)
        if job is None:
            return False, 0, "missing"
        # job 的 stage/percent/done/success 是 flat 的，无 info 子键
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


def main():
    os.makedirs(MOSAIC_TMP, exist_ok=True)
    results = {}
    total = len(TASKS)
    ok_count = 0
    fail_count = 0
    t_start = time.time()

    try:
        os.remove(LOG_FILE)
    except OSError:
        pass

    log(f"开始批量发布 {total} 个砂场影像")
    log("=" * 50)

    for i, task in enumerate(TASKS):
        folder = task["folder"]
        county = task["county"]
        srs = task["srs"]
        name = task["name"]
        layer_key = f"{county}_{task['site_key']}"
        tif_names = task["tifs"]

        folder_path = os.path.join(SAND_IMAGES_DIR, folder)
        if not os.path.isdir(folder_path):
            log(f"跳过 #{i+1}/{total} [{layer_key}] 文件夹不存在: {folder_path}")
            results[layer_key] = {"folder": folder, "success": False, "error": "folder not found"}
            fail_count += 1
            continue

        log(f"--- #{i+1}/{total} [{layer_key}] {name} ---")

        # 收集 tif 路径
        tif_paths = []
        for tn in tif_names:
            p = os.path.join(folder_path, tn)
            if os.path.isfile(p):
                tif_paths.append(p)
            else:
                log(f"  警告: 找不到 {tn}")

        if not tif_paths:
            log(f"  没有有效 TIF 文件，跳过")
            results[layer_key] = {"folder": folder, "success": False, "error": "no tif files"}
            fail_count += 1
            continue

        # 镶嵌（如需要）
        if len(tif_paths) > 1:
            mosaic_path = os.path.join(MOSAIC_TMP, f"{layer_key}.tif")
            source = mosaic_tifs(tif_paths, mosaic_path)
            if source is None:
                results[layer_key] = {"folder": folder, "success": False, "error": "mosaic failed"}
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
            results[layer_key] = {"folder": folder, "success": False, "error": str(e)[:200]}
            fail_count += 1
            continue

        # 等待完成
        success, pct, stage = wait_job(job_id)
        elapsed = time.time() - t0
        status = "OK" if success else f"FAIL({stage})"
        log(f"  结果: {status} 耗时 {elapsed:.0f}s 进度 {pct}%")

        results[layer_key] = {
            "folder": folder,
            "success": success,
            "elapsed_s": round(elapsed, 1),
            "percent": pct,
            "stage": stage,
            "srs": srs,
        }
        if success:
            ok_count += 1
        else:
            fail_count += 1

        # 进度汇总
        elapsed_total = time.time() - t_start
        log(f"  汇总: OK {ok_count} / FAIL {fail_count} / 总 {i+1}/{total}  已运行 {elapsed_total:.0f}s")

    # 最终汇总
    elapsed_total = time.time() - t_start
    log("=" * 50)
    log(f"完成！OK {ok_count} / FAIL {fail_count} / 总 {total}")
    log(f"总耗时 {elapsed_total:.0f}s ({elapsed_total/60:.1f}min)")

    # 保存结果
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"结果保存到 {RESULTS_FILE}")

    # 清理镶嵌临时目录
    import shutil
    if os.path.exists(MOSAIC_TMP):
        shutil.rmtree(MOSAIC_TMP, ignore_errors=True)
        log(f"清理临时目录 {MOSAIC_TMP}")

    return results


if __name__ == "__main__":
    main()
