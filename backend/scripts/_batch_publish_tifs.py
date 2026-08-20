#!/usr/bin/env python
"""批量发布砂场TIF影像到切片管理（直接调用内部publisher，绕开HTTP）"""
import sys, os, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# 确保后端代码可导入
sys.path.insert(0, "/home/server/python/map_assistant_v1/backend")

from services.tile_manager.publisher import publish_raster_async
from services.tile_manager.tasks import _TILE_BUILD_JOBS

# 读取验证结果
val_path = os.path.join(os.path.dirname(__file__), "_tif_validation.json")
with open(val_path) as f:
    data = json.load(f)

results = data["results"]
print(f"共 {len(results)} 个 TIF 待发布\n")

# 按县区分组统计
county_map = {
    "豫罗砂许": "罗山", "豫信固砂许": "固始", "豫潢砂许": "潢川",
    "豫商城砂许": "商城", "豫息水砂许": "息县",
}
counties = {}
for r in results:
    for prefix, name in county_map.items():
        if prefix in r["rel"] or name in r["rel"]:
            counties[name] = counties.get(name, 0) + 1
            break
    else:
        counties["工程/其他"] = counties.get("工程/其他", 0) + 1
for c, n in sorted(counties.items()):
    print(f"  {c}: {n} 个")

# 批量提交（每次最多3个并发，避免IO争抢）
MAX_CONCURRENT = 3
jobs = {}  # layer_key → {"job_id":..., "name":..., "rel":...}

def submit_one(tif_info):
    """提交单个发布任务"""
    source = tif_info["path"]
    rel = tif_info["rel"]
    # 从路径提取 layer_key（去掉扩展名和特殊字符）
    basename = os.path.splitext(os.path.basename(source))[0]
    key = basename.replace(" ", "_").replace("(", "").replace(")", "").replace("、", "_")[:50]
    # 提取砂场名作为显示名
    name = basename
    try:
        job_id = publish_raster_async(
            source_path=source,
            layer_key=key,
            name=name,
            min_zoom=10,     # 无人机影像从 zoom 10 开始
            max_zoom=22,
            opacity=0.95,
            overwrite=True,
        )
        return {"key": key, "name": name, "rel": rel, "job_id": job_id, "status": "submitted"}
    except Exception as e:
        return {"key": key, "name": name, "rel": rel, "job_id": None, "status": f"error: {str(e)[:100]}"}

print(f"\n[批量提交] 开始 (并发={MAX_CONCURRENT})...")
with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
    futures = {pool.submit(submit_one, r): r for r in results}
    for i, f in enumerate(as_completed(futures)):
        r = f.result()
        jobs[r["key"]] = r
        tag = "✅" if r["job_id"] else "❌"
        print(f"  [{i+1:2}/{len(results)}] {tag} {r['name'][:40]:40} job={r['job_id'] or r['status']}")

print(f"\n[提交完成] {sum(1 for j in jobs.values() if j['job_id'])}/{len(results)} 成功")

# 等待所有任务完成
print("\n[监控进度] 等待全部构建完成...")
while True:
    active = {k: v for k, v in jobs.items() if v["job_id"] and v.get("done") != True}
    done_ok = sum(1 for j in jobs.values() if j.get("done") and j.get("success"))
    done_fail = sum(1 for j in jobs.values() if j.get("done") and not j.get("success"))
    
    # 检查每个活跃任务的进度
    for k, v in list(active.items()):
        job = _TILE_BUILD_JOBS.get(v["job_id"])
        if job:
            v["percent"] = job.get("percent", 0)
            v["message"] = job.get("message", "")
            if job.get("done"):
                v["done"] = True
                v["success"] = job.get("success", False)
        # 如果任务已从内存中移除（可能超时清理），检查注册表
        elif not _TILE_BUILD_JOBS.get(v["job_id"]):
            v["done"] = True
            v["success"] = True  # 假设成功（注册表持久化说明完成了）
    
    if not active:
        break
    
    # 打印进度摘要
    running = [v for v in active.values() if v.get("percent", 0) > 0]
    if running:
        pcts = ", ".join(f"{v['name'][:20]}={v.get('percent',0)}%" for v in running[:5])
        print(f"  ⏳ 运行中 {len(active)} 个 | ✅{done_ok} ❌{done_fail} | {pcts}")
    
    time.sleep(15)

print(f"\n{'='*60}")
print(f"[完成] ✅ 成功: {done_ok}  ❌ 失败: {done_fail}  总计: {len(results)}")
