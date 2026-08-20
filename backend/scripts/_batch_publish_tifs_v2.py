#!/usr/bin/env python
"""批量发布砂场TIF影像到切片管理 v2 — 后台稳健版"""
import sys, os, json, time, traceback
sys.path.insert(0, "/home/server/python/map_assistant_v1/backend")

# 强制刷新输出
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

def log(msg):
    print(msg, flush=True)

try:
    from services.tile_manager.publisher import publish_raster_async
    from services.tile_manager.tasks import _TILE_BUILD_JOBS
    log("导入成功")
except Exception as e:
    log(f"导入失败: {e}")
    traceback.print_exc()
    sys.exit(1)

val_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tif_validation.json")
with open(val_path) as f:
    data = json.load(f)

results = data["results"]
log(f"共 {len(results)} 个 TIF 待发布")

MAX_CONCURRENT = 3
jobs = {}

def submit_one(tif_info):
    source = tif_info["path"]
    basename = os.path.splitext(os.path.basename(source))[0]
    key = basename.replace(" ", "_").replace("(", "").replace(")", "").replace("、", "_")[:50]
    try:
        job_id = publish_raster_async(
            source_path=source, layer_key=key, name=basename,
            min_zoom=10, max_zoom=22, opacity=0.95, overwrite=True,
        )
        return {"key": key, "name": basename, "rel": tif_info["rel"], "job_id": job_id, "status": "submitted"}
    except Exception as e:
        return {"key": key, "name": basename, "rel": tif_info["rel"], "job_id": None, "status": f"err: {e}"}

# 逐个提交（不用线程池，避免子线程输出混乱）
log("\n[逐个提交]")
for i, r in enumerate(results):
    log(f"  [{i+1}/{len(results)}] 提交: {r['rel'][:60]}")
    try:
        info = submit_one(r)
        jobs[info["key"]] = info
        tag = "✅" if info["job_id"] else "❌"
        log(f"    → {tag} job={info['job_id'] or info['status']}")
    except Exception as e:
        log(f"    → ❌ {e}")

ok = sum(1 for j in jobs.values() if j["job_id"])
log(f"\n[提交完成] {ok}/{len(results)} 成功")

# 监控进度
log("\n[监控]")
while True:
    active = {k: v for k, v in jobs.items() if v["job_id"] and not v.get("done")}
    done_ok = sum(1 for j in jobs.values() if j.get("done") and j.get("success"))
    done_fail = sum(1 for j in jobs.values() if j.get("done") and not j.get("success"))
    
    for k, v in list(active.items()):
        job = _TILE_BUILD_JOBS.get(v["job_id"])
        if job:
            v["percent"] = job.get("percent", 0)
            v["stage"] = job.get("stage", "")
            if job.get("done"):
                v["done"] = True
                v["success"] = job.get("success", False)
                log(f"  {'✅' if v['success'] else '❌'} {v['name'][:40]} ({v.get('stage','?')})")
    
    if not active:
        break
    
    running = [v for v in active.values() if v.get("percent", 0) > 0]
    if running:
        pcts = " | ".join(f"{v['name'][:20]}={v.get('percent',0)}%" for v in running[:6])
        log(f"  ⏳ 运行: {len(active)} | ✅{done_ok} ❌{done_fail} | {pcts}")
    
    time.sleep(30)

log(f"\n{'='*60}")
log(f"[最终] ✅{done_ok} ❌{done_fail} 总计{len(results)}")

# 保存结果
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_publish_results.json")
with open(out_path, "w") as f:
    json.dump(jobs, f, ensure_ascii=False, indent=2, default=str)
log(f"结果保存到 {out_path}")
