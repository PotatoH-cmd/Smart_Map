#!/usr/bin/env python
"""批量验证砂场影像TIF + 提取元信息"""
import subprocess, json, os, sys
from pathlib import Path

base = Path("/home/server/python/采砂/砂场影像")
tifs = sorted(base.rglob("*.tif"))

results = []
problems = []

print(f"共 {len(tifs)} 个 TIF\n")
print(f"{'状态':4} {'文件':50} {'宽度':>8} {'高度':>8} {'波段':>4} {'CRS':20}")
print("-" * 110)

for tif in tifs:
    rel = str(tif.relative_to(base))
    try:
        r = subprocess.run(
            ["gdalinfo", "-json", str(tif)],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            problems.append((rel, f"gdalinfo 失败: {r.stderr[:100]}"))
            print(f"{'❌':4} {rel[:50]:50} {'ERROR'}")
            continue

        info = json.loads(r.stdout)
        w = info.get("size", [0, 0])[0]
        h = info.get("size", [0, 0])[1]
        bands = len(info.get("bands", []))
        crs = info.get("coordinateSystem", {}).get("wkt", "").split('"')[1] if '"' in info.get("coordinateSystem", {}).get("wkt", "") else "Unknown"
        
        # 提取简短CRS名
        if "4326" in crs or "WGS 84" in crs:
            crs_short = "EPSG:4326"
        elif "3857" in crs or "Web Mercator" in crs:
            crs_short = "EPSG:3857"
        elif "4527" in crs:
            crs_short = "EPSG:4527"
        elif "4547" in crs:
            crs_short = "EPSG:4547"
        elif "4548" in crs:
            crs_short = "EPSG:4548"
        elif "4549" in crs:
            crs_short = "EPSG:4549"
        elif "4550" in crs:
            crs_short = "EPSG:4550"
        else:
            crs_short = crs[:20]

        # 检查灰度/问题
        band_types = [b.get("colorInterpretation", "Unknown") for b in info.get("bands", [])]
        band_warnings = []
        for bt in band_types:
            if bt == "Undefined":
                band_warnings.append("无色彩解释")

        tag = "✅" if not band_warnings else "⚠️"

        corner = info.get("cornerCoordinates", {})
        ul = corner.get("upperLeft", [0, 0])

        results.append({
            "path": str(tif),
            "rel": rel,
            "width": w, "height": h, "bands": bands,
            "crs": crs_short, "type": info.get("driverShortName", ""),
            "ul_x": ul[0], "ul_y": ul[1],
            "warnings": band_warnings,
        })

        warn_str = f" [{', '.join(band_warnings)}]" if band_warnings else ""
        print(f"{tag:4} {rel[:50]:50} {w:>8} {h:>8} {bands:>4} {crs_short:20}{warn_str}")

    except subprocess.TimeoutExpired:
        problems.append((rel, "超时"))
        print(f"{'⏱️':4} {rel[:50]:50} {'TIMEOUT'}")
    except Exception as e:
        problems.append((rel, str(e)[:100]))
        print(f"{'❌':4} {rel[:50]:50} {str(e)[:50]}")

print(f"\n--- 汇总 ---")
print(f"正常: {len(results)}  异常: {len(problems)}  总计: {len(tifs)}")

if problems:
    print("\n异常文件:")
    for rel, err in problems:
        print(f"  ❌ {rel}")
        print(f"     {err}")

# 保存结果供下一步使用
with open(os.path.join(os.path.dirname(__file__), "_tif_validation.json"), "w") as f:
    json.dump({"results": results, "problems": problems}, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存到 _tif_validation.json")
