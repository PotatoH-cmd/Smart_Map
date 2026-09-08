"""遥感 AI：Falcon 检测/进度/精修/下载（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
import os
import tempfile
import json
import asyncio
import uuid
import subprocess
import shutil
import logging
from core.config import falcon as _cfg_falcon

# Falcon 目标识别：检测脚本 + 常驻推理服务
_FALCON_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "falcon_detect.py")
_FALCON_PYTHON_BIN = _cfg_falcon.python_bin
FALCON_SERVICE_URL = _cfg_falcon.service_url


logger = logging.getLogger(__name__)

router = APIRouter()


class FalconDetectRequest(BaseModel):
    geometry: dict  # GeoJSON Polygon geometry
    prompt: str
    mode: str = "rectangle"  # rectangle | polygon
    fast_mode: bool = False
    quick_mode: bool = False
    precise_mode: bool = False  # 精度模式：默认关闭（快速优先）
@router.post("/api/falcon-detect")
async def falcon_detect(req: FalconDetectRequest):
    """
    Falcon 目标识别：根据绘制的 GeoJSON 区域和自然语言提示词，执行 Falcon-Perception 推理
    通过 SSE 实时推送进度，最终返回 GeoJSON 结果
    """
    import subprocess
    import tempfile
    import shutil
    import uuid
    import asyncio
    import queue
    import threading

    logger.info(f"Falcon detect request: prompt={req.prompt}, mode={req.mode}, precise_mode={req.precise_mode}")

    # 预检：Falcon 推理服务是否就绪（不可达立即报错，不启动任务）
    try:
        import requests as _requests
        _health = _requests.get(f"{FALCON_SERVICE_URL}/health", timeout=5).json()
        if _health.get("status") == "error":
            raise HTTPException(status_code=503,
                                detail=f"Falcon 推理服务异常: {_health.get('error')}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail=f"Falcon 推理服务不可达 ({FALCON_SERVICE_URL})，请先启动 falcon-service（pm2 start falcon-service）")

    # 预检：坐标是否在影像覆盖范围内
    coords = req.geometry.get("coordinates", [])
    if coords:
        flat = coords[0] if isinstance(coords[0][0], (int, float)) else coords[0][0] if coords else []
        if isinstance(flat[0], (int, float)):
            def extract_points(c):
                if isinstance(c[0], (int, float)):
                    return [c]
                return [p for sub in c for p in (extract_points(sub) if isinstance(sub[0], list) else [sub])]
            points = extract_points(coords)
            lons = [p[0] for p in points]
            lats = [p[1] for p in points]
            tif_bounds = (110.35, 116.65, 31.38, 36.37)
            if max(lons) < tif_bounds[0] or min(lons) > tif_bounds[1] or \
               max(lats) < tif_bounds[2] or min(lats) > tif_bounds[3]:
                raise HTTPException(
                    status_code=400,
                    detail=f"绘制区域超出影像覆盖范围。影像覆盖: 经度 110.35~116.65, 纬度 31.38~36.37"
                )

    task_id = uuid.uuid4().hex[:12]
    progress_dir = "/tmp/falcon_progress"
    os.makedirs(progress_dir, exist_ok=True)
    progress_file = os.path.join(progress_dir, f"{task_id}.json")

    # 初始化进度文件
    with open(progress_file, 'w') as f:
        json.dump({"stage": "init", "current": 0, "total": 1, "message": "任务已创建，正在启动..."}, f)

    output_dir = tempfile.mkdtemp(prefix="falcon_detect_")
    falcon_script = _FALCON_SCRIPT_PATH
    python_bin = _FALCON_PYTHON_BIN
    geometry_json = json.dumps(req.geometry)
    cmd = [python_bin, "-u", falcon_script, geometry_json, req.prompt]
    if not req.precise_mode:
        cmd.append("--demo")
    if req.quick_mode:
        cmd.append("--quick")

    logger.info(f"Falcon command (streaming): {' '.join(cmd)}")

    # ── SSE 流式生成器 ──
    def _run_and_stream(q: queue.Queue):
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ,
                     "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
                     "FALCON_PROGRESS_FILE": progress_file,
                     "FALCON_SERVICE_URL": FALCON_SERVICE_URL,
                     }
            )

            # 逐行读取 stdout，解析进度和结果
            stdout_lines = []
            for line in proc.stdout:
                line = line.rstrip('\n').rstrip('\r')
                if not line:
                    continue
                stdout_lines.append(line)

                # 解析 [PROGRESS] 行 → SSE 进度事件
                if line.startswith("[PROGRESS]"):
                    try:
                        parts = line[10:].strip().split(" ", 2)
                        stage = parts[0] if len(parts) > 0 else ""
                        current_total = parts[1].split("/") if len(parts) > 1 else ["0", "1"]
                        current = int(current_total[0]) if current_total[0].isdigit() else 0
                        total = int(current_total[1]) if len(current_total) > 1 and current_total[1].isdigit() else max(current, 1)
                        message = parts[2] if len(parts) > 2 else ""

                        # 阶段 → 百分比映射
                        pct_map = {
                            "init": 5, "cropped": 12, "inference_start": 18,
                        }
                        if stage in pct_map:
                            pct = pct_map[stage]
                        elif stage == "inference":
                            pct = min(18 + int((current / max(total, 1)) * 72), 90)
                        elif stage == "inference_done":
                            pct = 92
                        elif stage == "done":
                            pct = 100
                        elif stage == "error":
                            pct = 0
                        else:
                            pct = 50  # fallback

                        q.put(json.dumps({
                            "type": "progress", "stage": stage,
                            "percent": pct, "message": message,
                        }, ensure_ascii=False))
                    except Exception:
                        pass
                elif line.startswith("[FALCON]"):
                    # 日志行也推送为进度消息
                    q.put(json.dumps({
                        "type": "progress", "stage": "log",
                        "percent": -1, "message": line,
                    }, ensure_ascii=False))

            proc.wait()

            # 读取 stderr
            stderr_text = proc.stderr.read()

            if proc.returncode != 0:
                err_msg = stderr_text[:500] if stderr_text else f"进程退出码 {proc.returncode}"
                q.put(json.dumps({
                    "type": "error", "message": err_msg,
                }, ensure_ascii=False))
                return

            # 在 stdout_lines 中找最后一行 JSON（feature collection）
            result_data = None
            for line in reversed(stdout_lines):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        result_data = json.loads(line)
                        if isinstance(result_data, dict) and "features" in result_data:
                            break
                        result_data = None
                    except json.JSONDecodeError:
                        continue

            if result_data is None:
                # 再试 stderr
                if "超出影像范围" in stderr_text:
                    q.put(json.dumps({
                        "type": "error", "message": "绘制区域超出影像覆盖范围",
                    }, ensure_ascii=False))
                else:
                    q.put(json.dumps({
                        "type": "error", "message": "Falcon 输出解析失败",
                    }, ensure_ascii=False))
                return

            result_data["_task_id"] = task_id
            logger.info(f"Falcon result: {len(result_data.get('features', []))} features")
            q.put(json.dumps({
                "type": "final", "result": result_data,
            }, ensure_ascii=False))

        except Exception as e:
            q.put(json.dumps({
                "type": "error", "message": str(e),
            }, ensure_ascii=False))
        finally:
            q.put(None)  # 结束信号

    async def _sse_generator():
        q = queue.Queue()
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _run_and_stream, q)

        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                break
            yield f"data: {chunk}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
@router.get("/api/falcon-progress/{task_id}")
async def falcon_progress(task_id: str):
    """查询 Falcon 推理实时进度"""
    progress_file = f"/tmp/falcon_progress/{task_id}.json"
    if not os.path.exists(progress_file):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    try:
        with open(progress_file) as f:
            return json.load(f)
    except Exception as e:
        return {"stage": "error", "current": 0, "total": 1, "message": str(e)}
@router.get("/api/falcon-test-result")
async def falcon_test_result():
    """返回预计算的 Falcon 测试结果（南阳市区建筑检测，365个图斑）"""
    test_file = "/tmp/falcon_building_result.json"
    if os.path.exists(test_file):
        with open(test_file) as f:
            data = json.load(f)
        data["_note"] = "测试数据：南阳市区 (112.52~112.54, 33.01~33.03) 建筑检测结果"
        return data
    return {"type": "FeatureCollection", "features": [], "_note": "测试数据尚未生成"}
class FalconDownloadRequest(BaseModel):
    geojson: dict
@router.post("/api/falcon-download")
async def falcon_download(req: FalconDownloadRequest):
    """
    将 Falcon 识别结果 GeoJSON 打包为 SHP + ZIP 下载
    """
    import zipfile
    import tempfile
    import io

    geojson = req.geojson
    if not geojson or not geojson.get("features"):
        raise HTTPException(status_code=400, detail="无识别结果可下载")

    try:
        import geopandas as gpd

        # 转 GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(geojson["features"], crs="EPSG:4326")

        # 写入临时目录
        tmp_dir = tempfile.mkdtemp(prefix="falcon_shp_")
        shp_path = os.path.join(tmp_dir, "falcon_result.shp")
        gdf.to_file(shp_path)

        # 打包 ZIP
        zip_path = os.path.join(tmp_dir, "falcon_result.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
                fpath = shp_path.replace('.shp', ext)
                if os.path.exists(fpath):
                    zf.write(fpath, os.path.basename(fpath))

        # 读取 ZIP 返回
        with open(zip_path, 'rb') as f:
            content = f.read()

        # 清理
        shutil.rmtree(tmp_dir, ignore_errors=True)

        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="falcon_result.zip"'}
        )
    except Exception as e:
        logger.exception(f"SHP download error: {e}")
        raise HTTPException(status_code=500, detail=f"打包下载失败: {str(e)}")
class FalconRequeryRequest(BaseModel):
    geometry: dict           # GeoJSON Polygon — 原始裁剪区域
    prompt: str              # 文本提示词
    coarse_geojson: dict     # 粗检测 GeoJSON FeatureCollection（/api/falcon-detect 的输出）
@router.post("/api/falcon-requery")
async def falcon_requery(req: FalconRequeryRequest):
    """
    Requery 精修：对粗检测结果中的每个图斑，裁剪对应影像区域 →
    Falcon 再推理 → 返回精修 GeoJSON。

    输入：
      - geometry: 原始绘制区域（用于从 TIF 裁剪影像）
      - prompt: 文本提示词
      - coarse_geojson: 粗检测 GeoJSON FeatureCollection
    返回：
      精修后的 GeoJSON FeatureCollection
    """
    import subprocess
    import tempfile
    import shutil
    import uuid

    logger.info(f"Falcon Requery: prompt={req.prompt[:30]}, coarse_features={len(req.coarse_geojson.get('features', []))}")

    task_id = uuid.uuid4().hex[:12]
    output_dir = tempfile.mkdtemp(prefix="falcon_requery_")

    try:
        falcon_script = _FALCON_SCRIPT_PATH
        python_bin = _FALCON_PYTHON_BIN

        geometry_json = json.dumps(req.geometry)
        coarse_json = json.dumps(req.coarse_geojson)

        cmd = [python_bin, "-u", falcon_script, geometry_json, req.prompt, "--requery"]

        logger.info(f"Falcon Requery command: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            input=coarse_json,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ,
                 "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
                 "FALCON_SERVICE_URL": FALCON_SERVICE_URL,
                 }
        )

        if proc.returncode != 0:
            logger.error(f"Falcon Requery failed: {proc.stderr}")
            raise HTTPException(status_code=500, detail=f"Requery 推理失败: {proc.stderr[:500]}")

        # 解析 JSON 输出（取最后一行）
        output_lines = proc.stdout.strip().split("\n")
        result_json = None
        for line in reversed(output_lines):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result_json = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result_json is None:
            logger.error(f"Requery output parse failed: {proc.stdout[-500:]}")
            raise HTTPException(status_code=500, detail="Requery 输出解析失败")

        n_refined = sum(1 for f in result_json.get("features", [])
                        if f.get("properties", {}).get("refined"))
        logger.info(f"Falcon Requery done: {len(result_json.get('features', []))} features, {n_refined} refined")
        result_json["_task_id"] = task_id
        return result_json

    except subprocess.TimeoutExpired:
        logger.error("Falcon Requery timeout")
        raise HTTPException(status_code=504, detail="Requery 推理超时")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Falcon Requery error: {e}")
        raise HTTPException(status_code=500, detail=f"Requery 异常: {str(e)}")
    finally:
        try:
            shutil.rmtree(output_dir, ignore_errors=True)
        except Exception:
            pass
