"""GIS 工具路由 - 集成 GIS Pipeline Engine 到豫水智能一张图

提供文件浏览器、脚本任务执行、批量去黑边、SRS归一化、影像镶嵌等功能。
所有接口前缀: /gis-tool
"""
import os
import shlex
import signal
import uuid
import subprocess
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/gis-tool")

# 日志目录（使用 map_assistant_v1 项目下的路径）
LOG_DIR = "/home/server/python/map_assistant_v1/backend/.gis_logs"
os.makedirs(LOG_DIR, exist_ok=True)

SUPPORTED_RASTER_EXTENSIONS = (".tif", ".tiff", ".img", ".vrt", ".ecw", ".jp2")

# 全局进程追踪表
_running_tasks: dict[str, dict] = {}


class ScriptData(BaseModel):
    script_content: str


class RemoveBlackEdgeData(BaseModel):
    input_file: str
    output_file: str
    nodata_value: Optional[float] = 0.0
    output_type: Optional[str] = "Byte"
    src_srs: Optional[str] = None


class BatchItem(BaseModel):
    input_file: str
    output_file: str


class RemoveBlackEdgeBatchData(BaseModel):
    files: list[BatchItem]
    nodata_value: Optional[float] = 0.0
    output_type: Optional[str] = "Byte"
    src_srs: Optional[str] = None
    parallel: Optional[int] = 1


class NormalizeSrsData(BaseModel):
    path: str
    target_srs: Optional[str] = "EPSG:4490"


class MosaicFileItem(BaseModel):
    path: str
    label: str = ""


class MosaicData(BaseModel):
    files: list[MosaicFileItem]
    output_file: str
    compress: Optional[str] = "ZSTD"
    zstd_level: Optional[int] = 3
    threads: Optional[int] = 12
    cache: Optional[int] = 32768
    predictor: Optional[int] = 2
    tiled: Optional[str] = "YES"
    bigtiff: Optional[str] = "YES"
    build_pyramids: Optional[bool] = True
    target_srs: Optional[str] = "EPSG:4490"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def new_task_id() -> str:
    return str(uuid.uuid4())[:8]


def build_task_paths(task_id: str) -> tuple[str, str]:
    script_path = os.path.join(LOG_DIR, f"job_{task_id}.sh")
    log_path = os.path.join(LOG_DIR, f"job_{task_id}.log")
    return script_path, log_path


def append_log(log_path: str, message: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(message)


def run_process_to_log(
    cmd: list[str],
    log_path: str,
    done_message: Optional[str] = None,
    make_executable: Optional[str] = None,
    use_setsid: bool = False,
    extra_env: Optional[dict[str, str]] = None,
    task_id: Optional[str] = None,
) -> None:
    try:
        if make_executable:
            os.chmod(make_executable, 0o755)

        env = {**os.environ, **(extra_env or {})}
        popen_kwargs = {
            "stdout": None,
            "stderr": subprocess.STDOUT,
            "env": env,
        }
        if use_setsid:
            popen_kwargs["preexec_fn"] = os.setsid

        with open(log_path, "w", encoding="utf-8", buffering=1) as f:
            popen_kwargs["stdout"] = f
            process = subprocess.Popen(cmd, **popen_kwargs)
            if task_id:
                pgid = None
                try:
                    pgid = os.getpgid(process.pid)
                except Exception:
                    pass
                _running_tasks[task_id] = {"process": process, "pgid": pgid}
            process.wait()
            f.write(f"\n[SYSTEM] Process exited with code: {process.returncode}")
            if process.returncode == 0 and done_message:
                f.write(f"\n{done_message}")
    except Exception as e:
        append_log(log_path, f"\n[CRITICAL ERROR] {str(e)}")
    finally:
        if task_id and task_id in _running_tasks:
            del _running_tasks[task_id]


def run_task_logic(script_path: str, log_path: str, task_id: str):
    run_process_to_log(
        cmd=["bash", script_path],
        log_path=log_path,
        make_executable=script_path,
        use_setsid=True,
        extra_env={"PYTHONUNBUFFERED": "1"},
        task_id=task_id,
    )


# ---------------------------------------------------------------------------
# 文件浏览器 API
# ---------------------------------------------------------------------------

@router.get("/list-files")
async def list_files(path: str = Query(...)):
    if not os.path.exists(path):
        return {"items": []}

    items = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir():
                    items.append({"name": entry.name, "type": "dir"})
                elif entry.name.lower().endswith(SUPPORTED_RASTER_EXTENSIONS):
                    items.append({"name": entry.name, "type": "file"})
    except OSError:
        return {"items": []}

    return {"items": sorted(items, key=lambda x: (x['type'] != 'dir', x['name']))}


@router.get("/list-files-recursive")
async def list_files_recursive(path: str = Query(...)):
    if not os.path.isdir(path):
        return {"files": []}

    files = []
    try:
        for root, _dirs, filenames in os.walk(path):
            for fname in filenames:
                if fname.lower().endswith(SUPPORTED_RASTER_EXTENSIONS):
                    full = os.path.join(root, fname)
                    files.append({"path": full, "label": fname})
    except OSError:
        return {"files": []}

    return {"files": sorted(files, key=lambda x: x["label"])}


# ---------------------------------------------------------------------------
# 任务执行 & 日志
# ---------------------------------------------------------------------------

@router.post("/run-task")
async def run_task(data: ScriptData, background_tasks: BackgroundTasks):
    task_id = new_task_id()
    s_path, l_path = build_task_paths(task_id)

    with open(s_path, "w", encoding="utf-8") as f:
        f.write(data.script_content.replace('\r\n', '\n'))

    background_tasks.add_task(run_task_logic, s_path, l_path, task_id)
    return {"task_id": task_id}


@router.get("/get-log/{task_id}")
async def get_log(task_id: str):
    l_path = os.path.join(LOG_DIR, f"job_{task_id}.log")
    if not os.path.exists(l_path):
        return {"logs": "Initializing...", "status": "running"}

    with open(l_path, "r", encoding="utf-8") as f:
        content = f.read()

    status = "finished" if "DONE:" in content or "[SYSTEM]" in content else "running"
    return {"logs": content, "status": status}


@router.post("/stop-task/{task_id}")
async def stop_task(task_id: str):
    if task_id not in _running_tasks:
        l_path = os.path.join(LOG_DIR, f"job_{task_id}.log")
        if os.path.exists(l_path):
            append_log(l_path, "\n[SYSTEM] Process exited with code: -1\n[STOPPED] Task manually stopped by user.")
        return {"status": "not_running", "message": "任务不在运行或已结束"}

    info = _running_tasks[task_id]
    pgid = info.get("pgid")
    process = info.get("process")
    killed = False

    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
            killed = True
        elif process and process.poll() is None:
            process.terminate()
            killed = True
    except ProcessLookupError:
        killed = True
    except Exception as e:
        return {"status": "error", "message": str(e)}

    l_path = os.path.join(LOG_DIR, f"job_{task_id}.log")
    if os.path.exists(l_path):
        append_log(l_path, "\n[SYSTEM] Process exited with code: -1\n[STOPPED] Task manually stopped by user.")

    if task_id in _running_tasks:
        del _running_tasks[task_id]

    return {"status": "stopped" if killed else "already_finished", "task_id": task_id}


@router.get("/running-tasks")
async def running_tasks():
    tasks = []
    for tid, info in list(_running_tasks.items()):
        proc = info.get("process")
        alive = proc.poll() is None if proc else False
        tasks.append({"task_id": tid, "pid": proc.pid if proc else None, "alive": alive})
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# 去除黑边（单文件 / 批量）
# ---------------------------------------------------------------------------

def build_blackedge_cmd(data: RemoveBlackEdgeData) -> list[str]:
    cmd = ["gdal_translate"]
    if data.src_srs:
        cmd += ["-a_srs", data.src_srs]
    cmd += ["-a_nodata", str(data.nodata_value), "-ot", data.output_type, "-of", "GTiff"]
    cmd += [data.input_file, data.output_file]
    return cmd


@router.post("/remove-blackedge")
async def remove_blackedge(data: RemoveBlackEdgeData, background_tasks: BackgroundTasks):
    task_id = new_task_id()
    _, l_path = build_task_paths(task_id)
    cmd = build_blackedge_cmd(data)

    def run():
        run_process_to_log(cmd=cmd, log_path=l_path, done_message="DONE: black-edge removal finished.")

    background_tasks.add_task(run)
    return {"task_id": task_id, "cmd": " ".join(cmd)}


def build_blackedge_common_args(data: RemoveBlackEdgeBatchData) -> str:
    args = []
    if data.src_srs:
        args.extend(["-a_srs", data.src_srs])
    args.extend(["-a_nodata", str(data.nodata_value), "-ot", data.output_type, "-of", "GTiff"])
    return " ".join(shlex.quote(x) for x in args)


def build_batch_blackedge_script(data: RemoveBlackEdgeBatchData) -> str:
    common_args = build_blackedge_common_args(data)
    total = len(data.files)
    parallel = data.parallel if data.parallel and data.parallel > 1 else 1

    lines = [
        "#!/bin/bash",
        f"# --- BATCH BLACK-EDGE REMOVAL ({total} files) ---",
        "set -e",
        "",
    ]

    if parallel > 1:
        lines.append(f"# 并行数: {parallel}")
        for i, item in enumerate(data.files, 1):
            out_dir = os.path.dirname(item.output_file)
            in_file = shlex.quote(item.input_file)
            out_file = shlex.quote(item.output_file)
            out_dir_q = shlex.quote(out_dir)
            lines.append(f"# [{i}/{total}] {os.path.basename(item.input_file)}")
            lines.append(f"while [ \"$(jobs -r | wc -l)\" -ge {parallel} ]; do wait -n; done")
            lines.append(f"(mkdir -p {out_dir_q} && gdal_translate {common_args} {in_file} {out_file}) &")
            lines.append("")
        lines.append("wait")
    else:
        for i, item in enumerate(data.files, 1):
            out_dir = shlex.quote(os.path.dirname(item.output_file))
            in_file = shlex.quote(item.input_file)
            out_file = shlex.quote(item.output_file)
            lines.append(f"# [{i}/{total}] {os.path.basename(item.input_file)}")
            lines.append(f"mkdir -p {out_dir}")
            lines.append(f"gdal_translate {common_args} {in_file} {out_file}")
            lines.append(f"echo \"[{i}/{total}] done: {os.path.basename(item.output_file)}\"")
            lines.append("")

    lines.append('echo "DONE: $(date)"')
    return "\n".join(lines)


def run_batch_blackedge_logic(script_path: str, log_path: str, task_id: str):
    run_process_to_log(
        cmd=["bash", script_path],
        log_path=log_path,
        done_message="DONE: all black-edge removal finished.",
        make_executable=script_path,
        use_setsid=True,
        extra_env={"PYTHONUNBUFFERED": "1"},
        task_id=task_id,
    )


@router.post("/remove-blackedge-batch")
async def remove_blackedge_batch(data: RemoveBlackEdgeBatchData, background_tasks: BackgroundTasks):
    if not data.files:
        return {"error": "files list is empty"}

    task_id = new_task_id()
    s_path, l_path = build_task_paths(task_id)
    script_content = build_batch_blackedge_script(data)

    with open(s_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    background_tasks.add_task(run_batch_blackedge_logic, s_path, l_path, task_id)
    return {"task_id": task_id, "total": len(data.files), "script": script_content}


# ---------------------------------------------------------------------------
# SRS 归一化
# ---------------------------------------------------------------------------

def build_normalize_srs_script(data: NormalizeSrsData) -> str:
    dir_path = shlex.quote(data.path)
    target_srs = shlex.quote(data.target_srs)
    return f"""#!/bin/bash
set -e
echo "Normalizing SRS to {data.target_srs} for all raster files in {data.path} ..."
total=0
fixed=0
while IFS= read -r -d '' f; do
    total=$((total + 1))
    gdal_edit.py -a_srs {target_srs} "$f" 2>&1 && fixed=$((fixed + 1))
done < <(find {dir_path} -type f \\( -name "*.img" -o -name "*.tif" -o -name "*.tiff" \\) -print0)
echo "Normalized {{fixed}}/{{total}} files."
echo "DONE: $(date)"
"""


def run_normalize_srs_logic(script_path: str, log_path: str):
    run_process_to_log(
        cmd=["bash", script_path],
        log_path=log_path,
        done_message="DONE: SRS normalization finished.",
        make_executable=script_path,
        use_setsid=True,
    )


@router.post("/normalize-srs")
async def normalize_srs(data: NormalizeSrsData, background_tasks: BackgroundTasks):
    if not os.path.isdir(data.path):
        return {"error": f"path not found: {data.path}"}

    task_id = new_task_id()
    s_path, l_path = build_task_paths(task_id)
    script_content = build_normalize_srs_script(data)

    with open(s_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    background_tasks.add_task(run_normalize_srs_logic, s_path, l_path)
    return {"task_id": task_id, "path": data.path, "target_srs": data.target_srs}


# ---------------------------------------------------------------------------
# 影像镶嵌（Mosaic）
# ---------------------------------------------------------------------------

def build_mosaic_script(data: MosaicData) -> str:
    co = f"-co TILED={data.tiled} -co BIGTIFF={data.bigtiff} -co COMPRESS={data.compress} -co PREDICTOR={data.predictor} -co NUM_THREADS={data.threads}"
    if data.compress == "ZSTD":
        co += f" -co ZSTD_LEVEL={data.zstd_level}"

    file_list = " ".join(shlex.quote(f.path) for f in data.files)
    file_labels = [f.label or os.path.basename(f.path) for f in data.files]

    out_dir = os.path.dirname(data.output_file)
    vrt_path = os.path.join(out_dir, "_mosaic_tmp.vrt")

    lines = [
        "#!/bin/bash",
        f"# --- GIS MOSAIC PIPELINE ---",
        f"# 镶嵌顺序（底→顶）: {', '.join(file_labels)}",
        "set -e",
        f"export GDAL_CACHEMAX={data.cache}",
        "",
        "# [STEP 1] SRS 归一化：统一所有输入栅格的 CRS 元数据",
        f'echo "[1/4] SRS 归一化 → {data.target_srs}"',
    ]
    for f in data.files:
        lines.append(f'gdal_edit.py -a_srs {data.target_srs} {shlex.quote(f.path)} 2>/dev/null || true')

    # 波段颜色解释归一化
    first_file = shlex.quote(data.files[0].path)
    lines += [
        "",
        "# [STEP 1b] 波段颜色解释归一化",
        f'_band_count=$(gdalinfo -json {first_file} 2>/dev/null | python3 -c "import json,sys;print(len(json.load(sys.stdin).get(\'bands\',[])))" 2>/dev/null || echo 0)',
        'if [ "$_band_count" = "3" ]; then',
    ]
    for f in data.files:
        lines.append(f'    gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue {shlex.quote(f.path)} 2>/dev/null || true')
    lines += [
        'elif [ "$_band_count" = "4" ]; then',
    ]
    for f in data.files:
        lines.append(f'    gdal_edit.py -colorinterp_1 red -colorinterp_2 green -colorinterp_3 blue -colorinterp_4 alpha {shlex.quote(f.path)} 2>/dev/null || true')
    lines.append('fi')

    lines += [
        "",
        "# [STEP 2] 创建 VRT 虚拟镶嵌（后列文件覆盖前列）",
        f'echo "[2/4] 创建 VRT 镶嵌..."',
        f'gdalbuildvrt -overwrite {shlex.quote(vrt_path)} {file_list}',
        "",
        "# [STEP 3] 转换为 GeoTIFF（含压缩与优化）",
        f'echo "[3/4] 导出 GeoTIFF → {os.path.basename(data.output_file)}"',
        f'mkdir -p {shlex.quote(out_dir)}',
        f'gdal_translate {co} {shlex.quote(vrt_path)} {shlex.quote(data.output_file)}',
        f'rm -f {shlex.quote(vrt_path)}',
    ]
    if data.build_pyramids:
        lines += [
            "",
            "# [STEP 4] 生成金字塔",
            f'echo "[4/4] 生成金字塔..."',
            f'gdaladdo -r average --config GDAL_NUM_THREADS {data.threads} {shlex.quote(data.output_file)} 2 4 8 16 32',
        ]
    else:
        lines.append(f'echo "[4/4] 跳过金字塔生成"')

    lines += ["", 'echo "DONE: $(date)"']
    return "\n".join(lines)


@router.post("/mosaic")
async def mosaic(data: MosaicData, background_tasks: BackgroundTasks):
    if len(data.files) < 2:
        return {"error": "至少需要 2 个影像文件才能镶嵌"}

    task_id = new_task_id()
    s_path, l_path = build_task_paths(task_id)
    script_content = build_mosaic_script(data)

    with open(s_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    def run():
        run_process_to_log(
            cmd=["bash", script_path],
            log_path=l_path,
            done_message="DONE: mosaic finished.",
            make_executable=script_path,
            use_setsid=True,
            extra_env={"PYTHONUNBUFFERED": "1"},
        )

    background_tasks.add_task(run)
    return {
        "task_id": task_id,
        "total": len(data.files),
        "output": data.output_file,
        "script": script_content,
    }
