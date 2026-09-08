"""文件资产：截图 · 报告下载/预览 · 图片/SHP 上传 · GeoJSON（自 main.py 机械搬移，行为不变）。"""
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
import re
import os
import base64
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse, HTMLResponse
import time
import uuid
import tempfile
import subprocess
import logging

# ── 文件域目录常量（自 main.py 收敛，目录均相对 backend 根解析）──
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_IMAGES_DIR = os.path.join(_BACKEND_DIR, "static", "uploads")  # 聊天多模态图片上传
GEOJSON_DIR = os.path.join(_BACKEND_DIR, "static", "geojson")        # 前端矢量图层 GeoJSON
SHP_UPLOAD_DIR = os.path.join(os.path.dirname(_BACKEND_DIR), "GIS", "uploads")  # SHP 上传（原 /home/server/python/GIS/uploads）
os.makedirs(UPLOAD_IMAGES_DIR, exist_ok=True)
os.makedirs(SHP_UPLOAD_DIR, exist_ok=True)

REQUIRED_SHP_EXTENSIONS = {".shp", ".dbf", ".shx"}


logger = logging.getLogger(__name__)

router = APIRouter()


class ScreenshotRequest(BaseModel):
    image_data: str
    file_name: Optional[str] = None
@router.post("/api/save-screenshot")
async def save_screenshot(payload: ScreenshotRequest):
    if not payload.image_data:
        raise HTTPException(status_code=400, detail="截图数据不能为空")
    match = re.match(r"^data:(image/\w+);base64,", payload.image_data)
    if not match:
        raise HTTPException(status_code=400, detail="无效的图片数据格式")
    mime_type = match.group(1)
    ext = mime_type.split("/")[-1]
    try:
        image_bytes = base64.b64decode(payload.image_data.split(",", 1)[1])
    except Exception:
        raise HTTPException(status_code=400, detail="图片解码失败")
    directory = "/home/server/python/map_assistant_v1/backend/static/screenshots"
    os.makedirs(directory, exist_ok=True)
    base_name = payload.file_name or f"map_screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", base_name)
    if not safe_name.lower().endswith(f".{ext.lower()}"):
        safe_name = f"{safe_name}.{ext}"
    file_path = os.path.join(directory, safe_name)
    with open(file_path, "wb") as f:
        f.write(image_bytes)
    url = f"/static/screenshots/{safe_name}"
    return {"url": url, "filename": safe_name, "file_path": file_path}
@router.api_route("/api/download/report/{filename}", methods=["GET", "HEAD"])
async def download_report(filename: str):
    """强制触发浏览器下载报告文件（带 Content-Disposition: attachment）"""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    return FileResponse(
        path=file_path,
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )
@router.get("/api/download/report")
async def download_report_query(filename: str = Query(..., description="报告文件名")):
    """通过 query 参数传递文件名，URL 不含 .docx 后缀，避免迅雷下载管理器拦截"""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    return FileResponse(
        path=file_path,
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )
@router.get("/api/preview/report", response_class=HTMLResponse)
async def preview_report(filename: str = Query(..., description="报告文件名")):
    """将 docx 报告转为 HTML 在线预览"""
    import mammoth
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    file_path = f"/home/server/python/map_assistant_v1/backend/static/reports/{safe_name}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {safe_name}")
    try:
        with open(file_path, "rb") as f:
            result = mammoth.convert_to_html(f)
        html_body = result.value
        warnings = result.messages
        if warnings:
            logger.warning(f"[Preview] mammoth warnings: {warnings[:3]}")
    except Exception as e:
        logger.error(f"[Preview] 转换失败: {e}")
        raise HTTPException(status_code=500, detail=f"报告转换失败: {str(e)}")

    html_page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>报告预览 - {safe_name}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 860px; margin: 30px auto; padding: 20px 40px; background: #fafafa; color: #333; line-height: 1.8; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #2563eb; padding-bottom: 8px; color: #1e3a5f; }}
  h2 {{ font-size: 18px; color: #2563eb; margin-top: 24px; }}
  h3 {{ font-size: 15px; color: #475569; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 8px 12px; text-align: left; font-size: 14px; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 8px 0; }}
  p {{ margin: 8px 0; }}
  .preview-toolbar {{ position: sticky; top: 0; background: #fff; border-bottom: 1px solid #e2e8f0; padding: 10px 0; margin-bottom: 20px; display: flex; align-items: center; gap: 12px; z-index: 10; }}
  .preview-toolbar h1 {{ font-size: 16px; margin: 0; border: none; padding: 0; flex: 1; }}
  .btn-download {{ background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 7px 16px; font-size: 13px; cursor: pointer; text-decoration: none; }}
  .btn-download:hover {{ background: #1d4ed8; }}
  .btn-print {{ background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 16px; font-size: 13px; cursor: pointer; }}
  .btn-print:hover {{ background: #e2e8f0; }}
  @media print {{ .preview-toolbar {{ display: none; }} body {{ max-width: 100%; padding: 0; }} }}
</style>
</head>
<body>
<div class="preview-toolbar">
  <h1>📄 {safe_name}</h1>
  <button class="btn-print" onclick="window.print()">🖨️ 打印</button>
  <a class="btn-download" href="/api/download/report/{safe_name}" download="{safe_name}">⬇ 下载</a>
</div>
<div class="report-content">
{html_body}
</div>
</body>
</html>"""
    return HTMLResponse(content=html_page)
@router.post("/api/upload_image")
async def upload_image(file: UploadFile = File(...)):
    """上传图片或 Excel 文件，返回访问 URL"""
    import time
    # 校验文件类型（图片 + Excel）
    allowed_types = {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/vnd.ms-excel",  # .xls
        "application/octet-stream",  # 某些浏览器对 Excel 的兼容类型
    }
    # 也通过后缀名判断，兼容浏览器发送的各种 MIME
    ext = os.path.splitext(file.filename or "image.png")[1].lower()
    is_excel = ext in {".xlsx", ".xls"}
    is_image = ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if not is_image and not is_excel:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
    
    # 生成唯一文件名
    ext = os.path.splitext(file.filename or "image.png")[1] or ".png"
    safe_ext = ext.lower() if ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".xlsx", ".xls"} else ".png"
    unique_name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{safe_ext}"
    save_path = os.path.join(UPLOAD_IMAGES_DIR, unique_name)
    
    # 保存文件
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)
    
    # 返回可访问的 URL
    url = f"/static/uploads/{unique_name}"
    logger.info(f"Image uploaded: {url} ({len(content)} bytes)")
    return JSONResponse({"url": url, "filename": unique_name, "size": len(content)})
@router.post("/api/upload/shp")
async def upload_shp(file: UploadFile = File(...)):
    """上传 SHP 文件（ZIP 压缩包），解压到 GIS/uploads/ 目录供 QGIS MCP 使用。
    
    要求 ZIP 至少包含 .shp、.dbf、.shx 三个文件。
    返回图层名称和容器内路径，可直接用于 qgis_mcp_tool 空间分析。
    """
    import time, zipfile, tempfile, shutil

    # 1. 校验文件类型
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext != ".zip":
        raise HTTPException(status_code=400, detail="SHP 文件必须打包为 ZIP 上传（.zip）")

    # 2. 读取并保存到临时文件
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:  # 200MB 限制
        raise HTTPException(status_code=400, detail="文件过大，限制 200MB")

    tmp_zip = os.path.join(tempfile.gettempdir(), f"shp_upload_{uuid.uuid4().hex}.zip")
    try:
        with open(tmp_zip, "wb") as f:
            f.write(content)

        # 3. 验证 ZIP 内容
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = zf.namelist()
            exts_in_zip = set()
            shp_stems = set()

            for name in names:
                # 跳过目录和 __MACOSX 隐藏文件
                base = os.path.basename(name)
                if not base or base.startswith("._") or name.endswith("/"):
                    continue
                file_ext = os.path.splitext(base)[1].lower()
                file_stem = os.path.splitext(base)[0].lower()
                if file_ext in REQUIRED_SHP_EXTENSIONS:
                    exts_in_zip.add(file_ext)
                    shp_stems.add(file_stem)

            if not (exts_in_zip >= REQUIRED_SHP_EXTENSIONS):
                missing = REQUIRED_SHP_EXTENSIONS - exts_in_zip
                raise HTTPException(
                    status_code=400,
                    detail=f"ZIP 缺少必要的 SHP 文件: {', '.join(sorted(missing))}（需要 .shp + .dbf + .shx）"
                )

            if len(shp_stems) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"ZIP 包含多个 SHP 数据集 ({', '.join(sorted(shp_stems))})，请每次上传一个"
                )

            # 4. 解压到目标目录
            ts = int(time.time())
            uid = uuid.uuid4().hex[:8]
            dest_dir = os.path.join(SHP_UPLOAD_DIR, f"{ts}_{uid}")
            os.makedirs(dest_dir, exist_ok=True)

            shp_name = None
            files_extracted = []
            for name in zf.namelist():
                base = os.path.basename(name)
                if not base or base.startswith("._") or name.endswith("/"):
                    continue
                dest_path = os.path.join(dest_dir, base)
                with zf.open(name) as src:
                    with open(dest_path, "wb") as dst:
                        dst.write(src.read())
                files_extracted.append(base)
                if os.path.splitext(base)[1].lower() == ".shp":
                    shp_name = base

            if not shp_name:
                raise HTTPException(status_code=500, detail="解压后未找到 .shp 文件")

        # 5. 用 ogrinfo 获取图层元数据
        shp_path = os.path.join(dest_dir, shp_name)
        feature_count = 0
        geom_type = "Unknown"
        fields = []
        try:
            import subprocess
            result = subprocess.run(
                ["ogrinfo", "-al", "-so", shp_path],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("Feature Count:"):
                    feature_count = int(line.split(":")[1].strip())
                if line.startswith("Geometry:"):
                    geom_type = line.split(":")[1].strip()
        except Exception as e:
            logger.warning(f"ogrinfo 失败，跳过元数据提取: {e}")

        # 容器内路径
        container_path = f"/uploads/{ts}_{uid}/{shp_name}"
        layer_name = os.path.splitext(shp_name)[0]

        logger.info(f"SHP uploaded: {shp_name} → {dest_dir} (容器: {container_path}, {feature_count} 要素)")

        return JSONResponse({
            "success": True,
            "layer_name": layer_name,
            "container_path": container_path,
            "feature_count": feature_count,
            "geometry_type": geom_type,
            "files": files_extracted,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"SHP 上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
    finally:
        # 清理临时 zip
        if os.path.exists(tmp_zip):
            os.unlink(tmp_zip)
@router.get("/api/geojson/{filename}")
async def get_geojson(filename: str):
    """读取 GeoJSON 文件，用于矢量图层加载到地图"""
    import re
    # 安全校验：仅允许 .geojson 后缀，防止路径穿越
    if not re.match(r'^[a-zA-Z0-9_\-\.]+\.geojson$', filename):
        raise HTTPException(status_code=400, detail="无效的文件名")
    file_path = os.path.join(GEOJSON_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="GeoJSON 文件不存在")
    return FileResponse(file_path, media_type="application/geo+json")
