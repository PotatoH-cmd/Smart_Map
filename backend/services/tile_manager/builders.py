"""切片构建逻辑：tippecanoe（矢量 PBF）与 GDAL（无人机影像 MBTiles）。

- _run_tippecanoe：同步调用 tippecanoe 生成矢量切片目录
- _run_drone_mbtiles_build：同步调用 GDAL 构建 MBTiles（旧接口）
- _run_drone_build_with_progress：带进度输出的 SSE generator（新接口）

所有构建均在后台线程中由调用方驱动，本模块不直接创建线程。
"""
import json
import logging
import os
import re
import subprocess
from datetime import datetime as _dt
from typing import Any, Dict

from fastapi import HTTPException

from .registry import (
    _DRONE_MBTILES_DIR,
    _DRONE_WORK_DIR,
    _VT_DIR,
    _dir_stats,
    _drone_layer_to_row,
    _load_drone_registry,
    _parse_bounds,
    _register_drone_imagery,
    _sanitize_layer_key,
    _save_drone_registry,
)

logger = logging.getLogger(__name__)


def _run_tippecanoe(key: str, source_path: str, min_zoom: int, max_zoom: int) -> Dict[str, int]:
    target_dir = os.path.join(_VT_DIR, key)
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    cmd = [
        "tippecanoe",
        "-e", target_dir,
        f"-z{max_zoom}",
        f"-Z{min_zoom}",
        "--no-tile-compression",
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "-l", key,
        "--force",
        source_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(__file__),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="tippecanoe 未安装或不在 PATH 中")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="矢量切片生成超时")
    if result.returncode != 0:
        logger.error("tippecanoe failed: %s", result.stderr[-2000:])
        raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "tippecanoe failed")
    return _dir_stats(target_dir)


def _build_drone_warp_cmd(source_path: str, warped_path: str, s_srs: str = "") -> list:
    warp_cmd = [
        "gdalwarp", "-t_srs", "EPSG:3857", "-multi",
        "-wo", "NUM_THREADS=4", "-r", "bilinear",
        "-dstalpha",
        "-of", "GTiff", "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE",
        "-co", "BIGTIFF=YES",
    ]
    if s_srs:
        warp_cmd.extend(["-s_srs", s_srs])  # 显式指定源 CRS
    warp_cmd.extend([source_path, warped_path])
    return warp_cmd


def _run_drone_mbtiles_build(req: Any) -> Dict[str, Any]:
    """同步构建无人机影像 MBTiles（req 为鸭子类型：DroneImageryBuildRequest）。"""
    source_path = os.path.abspath(req.source_path)
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail=f"GeoTIFF 文件不存在: {source_path}")
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(source_path))[0])
    min_zoom = max(0, min(22, int(req.min_zoom)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom)))
    tile_format = req.tile_format.upper()
    if tile_format not in ("PNG", "PNG8", "JPEG"):
        tile_format = "PNG"
    quality = max(1, min(100, int(req.quality)))
    os.makedirs(_DRONE_MBTILES_DIR, exist_ok=True)
    os.makedirs(_DRONE_WORK_DIR, exist_ok=True)
    warped_path = os.path.join(_DRONE_WORK_DIR, f"{key}_3857.tif")
    mbtiles_path = os.path.join(_DRONE_MBTILES_DIR, f"{key}.mbtiles")
    if os.path.exists(mbtiles_path) and not req.overwrite:
        raise HTTPException(status_code=409, detail=f"MBTiles 已存在: {mbtiles_path}")
    for path in (warped_path, mbtiles_path):
        if os.path.exists(path) and req.overwrite:
            os.remove(path)
    # 源坐标系：用户手动指定优先，否则自动检测
    s_srs = (req.source_srs or "").strip()
    if not s_srs:
        s_srs = _detect_source_srs(source_path)
    warp_cmd = _build_drone_warp_cmd(source_path, warped_path, s_srs)
    translate_cmd = [
        "gdal_translate",
        "-of", "MBTILES",
        "-co", f"TILE_FORMAT={tile_format}",
        "-co", f"QUALITY={quality}",
        "-co", "RESAMPLING=BILINEAR",
        "-co", "WRITE_BOUNDS=YES",
        "-co", "WRITE_MINMAXZOOM=YES",
        warped_path,
        mbtiles_path,
    ]
    overview_count = max(0, max_zoom - min_zoom)
    overview_factors = [str(2 ** i) for i in range(1, overview_count + 1)]
    try:
        for cmd in (warp_cmd, translate_cmd):
            result = subprocess.run(
                cmd,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
            if result.returncode != 0:
                logger.error("drone imagery build failed: %s", result.stderr[-2000:])
                raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "GDAL 构建失败")
        if overview_factors:
            addo_cmd = ["gdaladdo", "-r", "average", mbtiles_path, *overview_factors]
            result = subprocess.run(
                addo_cmd,
                cwd=os.path.dirname(__file__),
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
            if result.returncode != 0:
                logger.error("drone imagery overview build failed: %s", result.stderr[-2000:])
                raise HTTPException(status_code=500, detail=result.stderr[-2000:] or "MBTiles 金字塔构建失败")
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"GDAL 工具未安装或不在 PATH 中: {e.filename}")
    row = _register_drone_imagery(_DroneRegReq(
        layer_key=key,
        name=req.name or key,
        path=mbtiles_path,
        area_key=req.area_key,
        year=req.year,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        max_native_zoom=max_zoom,
        opacity=req.opacity,
        scheme="tms",
    ))
    registry = _load_drone_registry()
    if key in registry:
        registry[key]["source_path"] = source_path
        registry[key]["build_type"] = "geotiff_to_mbtiles"
        registry[key]["updated_at"] = _dt.now().isoformat(timespec="seconds")
        _save_drone_registry(registry)
        row = _drone_layer_to_row(key, registry[key])
    import contextlib
    with contextlib.suppress(Exception):
        os.remove(warped_path)
    return row


def _detect_source_srs(source_path: str) -> str:
    """通过 gdalinfo 检测源文件坐标系；异常标记（地理CRS+投影坐标）自动修正。"""
    s_srs = ""
    try:
        gdalinfo_proc = subprocess.run(
            ["gdalinfo", source_path],
            capture_output=True, text=True, timeout=30,
        )
        if gdalinfo_proc.returncode == 0:
            info_output = gdalinfo_proc.stdout + gdalinfo_proc.stderr
            has_projcrs = "PROJCRS[" in info_output
            has_geogcrs = "GEOGCRS[" in info_output
            origin_match = re.search(r'Origin\s*=\s*\(([\d.]+),\s*([\d.]+)\)', info_output)
            is_projected_coords = False
            if origin_match:
                ox, oy = float(origin_match.group(1)), float(origin_match.group(2))
                is_projected_coords = abs(ox) > 360 or abs(oy) > 90
            epsg_matches = re.findall(r'ID\["EPSG",(\d+)\]', info_output)
            proj_epsg_candidates = [int(m) for m in epsg_matches if 2000 <= int(m) <= 9999]
            if has_projcrs and proj_epsg_candidates:
                s_srs = f"EPSG:{proj_epsg_candidates[-1]}"
            elif has_geogcrs and not has_projcrs and is_projected_coords:
                subprocess.run(
                    ["gdal_edit.py", "-a_srs", "EPSG:4548", source_path],
                    capture_output=True, text=True, timeout=30,
                )
                s_srs = "EPSG:4548"
            elif epsg_matches:
                s_srs = f"EPSG:{epsg_matches[-1]}"
    except Exception:
        pass
    return s_srs


def _run_drone_build_with_progress(req: Any):
    """Generator that yields SSE progress events during drone imagery build."""
    import time as _time

    def _send(stage, pct, msg, **extra):
        payload = {"stage": stage, "percent": pct, "message": msg, **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    source_path = os.path.abspath(req.source_path)
    if not os.path.isfile(source_path):
        yield _send("error", 0, f"GeoTIFF 文件不存在: {source_path}")
        return
    key = _sanitize_layer_key(req.layer_key or os.path.splitext(os.path.basename(source_path))[0])
    min_zoom = max(0, min(22, int(req.min_zoom)))
    max_zoom = max(min_zoom, min(22, int(req.max_zoom)))
    tile_format = req.tile_format.upper()
    if tile_format not in ("PNG", "PNG8", "JPEG"):
        tile_format = "PNG"
    quality = max(1, min(100, int(req.quality)))
    os.makedirs(_DRONE_MBTILES_DIR, exist_ok=True)
    os.makedirs(_DRONE_WORK_DIR, exist_ok=True)
    warped_path = os.path.join(_DRONE_WORK_DIR, f"{key}_3857.tif")
    mbtiles_path = os.path.join(_DRONE_MBTILES_DIR, f"{key}.mbtiles")
    if os.path.exists(mbtiles_path) and not req.overwrite:
        yield _send("error", 0, f"MBTiles 已存在: {mbtiles_path}")
        return
    for path in (warped_path, mbtiles_path):
        if os.path.exists(path) and req.overwrite:
            os.remove(path)

    # === CRS 检测与自动修复（处理地理坐标系标记+投影坐标数据的错误文件） ===
    yield _send("warp", 0, "检测源文件坐标系...")
    s_srs = (getattr(req, "source_srs", "") or "").strip()
    if not s_srs:
        s_srs = _detect_source_srs(source_path)

    # === 构建 GDAL 环境变量 ===
    gdal_env = os.environ.copy()
    for candidate in ("/usr/share/proj", "/usr/local/share/proj"):
        if os.path.isfile(os.path.join(candidate, "proj.db")):
            gdal_env["PROJ_LIB"] = candidate
            break

    warp_cmd = _build_drone_warp_cmd(source_path, warped_path, s_srs)

    stages = [
        ("warp", "投影变换 (gdalwarp)", warp_cmd, 5, 45),
        ("translate", "转换 MBTiles (gdal_translate)", [
            "gdal_translate", "-of", "MBTILES",
            "-co", f"TILE_FORMAT={tile_format}", "-co", f"QUALITY={quality}",
            "-co", "RESAMPLING=BILINEAR", "-co", "WRITE_BOUNDS=YES",
            "-co", "WRITE_MINMAXZOOM=YES", warped_path, mbtiles_path,
        ], 45, 75),
    ]
    overview_count = max(0, max_zoom - min_zoom)
    overview_factors = [str(2 ** i) for i in range(1, overview_count + 1)]
    if overview_factors:
        stages.append((
            "overview", "构建瓦片金字塔 (gdaladdo)",
            ["gdaladdo", "-r", "average", mbtiles_path, *overview_factors],
            75, 95,
        ))

    try:
        for stage_key, stage_label, cmd, pct_start, pct_end in stages:
            yield _send(stage_key, pct_start, f"开始{stage_label}...")
            proc = subprocess.Popen(
                cmd, cwd=os.path.dirname(__file__),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=gdal_env,
            )
            t0 = _time.time()
            last_pct = pct_start
            last_emit_ts = t0
            _stderr_lines = []
            while proc.poll() is None:
                line = ""
                try:
                    import select
                    ready, _, _ = select.select([proc.stdout], [], [], 1)
                    if ready:
                        line = proc.stdout.readline() or ""
                        if not line:
                            try:
                                _stderr_lines.append(proc.stderr.readline() or "")
                            except Exception:
                                pass
                except Exception:
                    line = proc.stdout.readline() or ""
                elapsed = _time.time() - t0
                progress_match = re.search(r'(\d+)%', line) if line else None
                dot_numbers = re.findall(r'(?:^|\.{3})(\d{1,3})(?=\.{3}|$)', line.strip()) if line else []
                if progress_match or dot_numbers:
                    sub_pct = int(progress_match.group(1) if progress_match else dot_numbers[-1])
                    if 0 <= sub_pct <= 100:
                        last_pct = pct_start + (pct_end - pct_start) * sub_pct / 100
                        last_emit_ts = _time.time()
                        yield _send(stage_key, int(last_pct), f"{stage_label}: {sub_pct}%")
                elif _time.time() - last_emit_ts >= 3:
                    last_pct = min(pct_end - 1, last_pct + 1)
                    last_emit_ts = _time.time()
                    yield _send(stage_key, int(last_pct), f"{stage_label} 进行中... 已运行 {int(elapsed)} 秒")
            try:
                remaining = proc.stderr.read()
                if remaining:
                    _stderr_lines.append(remaining)
            except Exception:
                pass
            proc.wait()
            if proc.returncode != 0:
                err_detail = "".join(_stderr_lines[-5:]).strip() or "(无详细错误输出)"
                yield _send("error", int(last_pct),
                    f"{stage_label} 失败 (退出码 {proc.returncode}): {err_detail[:200]}")
                return
            yield _send(stage_key, pct_end, f"{stage_label} 完成")
    except FileNotFoundError as e:
        yield _send("error", 0, f"GDAL 工具未安装: {e.filename}")
        return

    yield _send("register", 96, "注册图层...")
    try:
        row = _register_drone_imagery(_DroneRegReq(
            layer_key=key,
            name=req.name or key,
            path=mbtiles_path,
            area_key=req.area_key,
            year=req.year,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_native_zoom=max_zoom,
            opacity=req.opacity,
            scheme="tms",
        ))
        registry = _load_drone_registry()
        if key in registry:
            registry[key]["source_path"] = source_path
            registry[key]["build_type"] = "geotiff_to_mbtiles"
            registry[key]["updated_at"] = _dt.now().isoformat(timespec="seconds")
            _save_drone_registry(registry)
            row = _drone_layer_to_row(key, registry[key])
    except Exception as e:
        yield _send("error", 96, f"注册失败: {str(e)}")
        return
    import contextlib
    with contextlib.suppress(Exception):
        os.remove(warped_path)
    yield _send("done", 100, "构建完成！", layer=key)


class _DroneRegReq:
    """注册请求的轻量鸭子类型（避免与 main.py 的 pydantic 模型耦合）。"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
