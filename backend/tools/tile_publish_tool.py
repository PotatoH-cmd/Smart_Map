"""切片发布工具 — AI 对话中触发 GIS 工具输出 → 切片管理发布。

支持：
- publish：将服务器上已有影像/矢量文件发布为地图切片图层（GeoTIFF→MBTiles / GeoJSON→PBF）
- status：查询发布任务进度

典型对话场景：
- "把 /mnt/output/task1/merged.tif 发布成底图"
- "检查一下刚才的发布任务做完了没有"
"""
import os
import json
import logging
from typing import Union

from qwen_agent.tools.base import BaseTool, register_tool

from services.tile_manager.publisher import publish_raster_async, publish_vector_async
from services.tile_manager.tasks import _TILE_BUILD_JOBS, _DRONE_BUILD_JOBS

logger = logging.getLogger(__name__)


@register_tool("tile_publish_tool")
class TilePublishTool(BaseTool):
    """
    切片发布工具。将服务器上已有的影像/矢量数据发布为地图瓦片图层。
    发布在后台执行，任务不阻塞对话；可通过 status 查询进度。
    """

    description = (
        "切片发布工具。将服务器文件发布为地图瓦片图层，供前端地图组件加载。\n"
        "支持两种发布类型：\n"
        "- raster（栅格）：GeoTIFF 影像 → MBTiles 瓦片（gdalwarp + gdal_translate），发布后可在切片区/地图中作为底图加载\n"
        "- vector（矢量）：GeoJSON → tippecanoe 矢量切片（PBF），发布后可叠加到地图\n\n"
        "action 参数说明：\n"
        "- publish：提交发布任务，后台执行，返回 job_id 供后续查询。source_path、layer_type 必填。\n"
        "- status：根据 job_id 查询任务进度，返回 percent/message/stage/success 等字段。\n\n"
        "使用注意：\n"
        "- source_path 必须是服务器文件系统上的绝对路径，不能是前端上传的临时文件\n"
        "- layer_type 不指定时根据文件扩展名自动判断（.tif/.tiff→raster, .geojson/.json→vector）\n"
        "- 栅格发布耗时长（大影像可能数十分钟），提交后可用 status 轮询或直接告知用户去切片区查看\n"
        "- 矢量发布较快（tippecanoe），通常几分钟内完成"
    )

    parameters = [
        {
            "name": "action",
            "type": "string",
            "description": "操作类型：publish（发布）或 status（查询进度）",
            "enum": ["publish", "status"],
            "required": True,
        },
        {
            "name": "source_path",
            "type": "string",
            "description": (
                "服务器上待发布文件的绝对路径。\n"
                "栅格示例：/mnt/arcgisorgdata/output/merged.tif\n"
                "矢量示例：/home/server/python/map_assistant_v1/backend/static/geojson/spatial_xxx.geojson"
            ),
            "required": False,
        },
        {
            "name": "layer_type",
            "type": "string",
            "description": "图层类型：raster（影像瓦片）、vector（矢量切片）或 auto（自动识别）",
            "enum": ["raster", "vector", "auto"],
            "required": False,
        },
        {
            "name": "layer_key",
            "type": "string",
            "description": "图层唯一标识（英文/数字/下划线），不指定时自动根据文件名生成",
            "required": False,
        },
        {
            "name": "name",
            "type": "string",
            "description": "图层显示名称，前端切片区展示用",
            "required": False,
        },
        {
            "name": "min_zoom",
            "type": "integer",
            "description": "瓦片最小缩放级别，默认 0",
            "required": False,
        },
        {
            "name": "max_zoom",
            "type": "integer",
            "description": "瓦片最大缩放级别，默认 22（栅格）/ 18（矢量）",
            "required": False,
        },
        {
            "name": "opacity",
            "type": "number",
            "description": "图层不透明度（0.0~1.0），仅栅格有效，默认 0.9",
            "required": False,
        },
        {
            "name": "job_id",
            "type": "string",
            "description": "发布任务 ID（status 操作时必需），由 publish 返回",
            "required": False,
        },
    ]

    def call(self, params: Union[str, dict], **kwargs) -> dict:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {"success": False, "error": "无效的 JSON 参数"}
        if not isinstance(params, dict):
            return {"success": False, "error": f"无效的参数类型: {type(params)}"}

        action = params.get("action", "publish")

        if action == "publish":
            return self._handle_publish(params)
        elif action == "status":
            return self._handle_status(params)
        else:
            return {"success": False, "error": f"未知操作: {action}，可选 publish / status"}

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------

    def _handle_publish(self, params: dict) -> dict:
        source_path = (params.get("source_path") or "").strip()
        if not source_path:
            return {"success": False, "error": "必须提供 source_path（服务器上的文件路径）"}
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            return {"success": False, "error": f"源文件不存在: {source_path}"}

        ext = os.path.splitext(source_path)[1].lower()
        layer_type = (params.get("layer_type") or "auto").lower()
        if layer_type == "auto":
            layer_type = "vector" if ext in (".geojson", ".json") else "raster"

        layer_key = (params.get("layer_key") or "").strip() or os.path.splitext(os.path.basename(source_path))[0]
        name = (params.get("name") or "").strip() or layer_key
        min_zoom = int(params.get("min_zoom") or 0)
        max_zoom = int(params.get("max_zoom") or (18 if layer_type == "vector" else 22))
        opacity = float(params.get("opacity") or 0.9)

        try:
            if layer_type == "vector":
                job_id = publish_vector_async(
                    source_path=source_path,
                    layer_key=layer_key,
                    name=name,
                    min_zoom=min_zoom,
                    max_zoom=max_zoom,
                    overwrite=True,
                )
                return {
                    "success": True,
                    "message": (
                        f"矢量图层「{layer_key}」已提交发布（GeoJSON → PBF 矢量切片）。\n"
                        f"任务 ID: {job_id}，可使用 tile_publish_tool(action='status', job_id='{job_id}') 查询进度。"
                    ),
                    "job_id": job_id,
                    "layer_key": layer_key,
                    "layer_type": "vector",
                }
            else:
                job_id = publish_raster_async(
                    source_path=source_path,
                    layer_key=layer_key,
                    name=name,
                    min_zoom=min_zoom,
                    max_zoom=max_zoom,
                    opacity=opacity,
                    overwrite=True,
                )
                return {
                    "success": True,
                    "message": (
                        f"栅格图层「{layer_key}」已提交发布（GeoTIFF → MBTiles 瓦片）。\n"
                        f"任务 ID: {job_id}，可使用 tile_publish_tool(action='status', job_id='{job_id}') 查询进度。"
                    ),
                    "job_id": job_id,
                    "layer_key": layer_key,
                    "layer_type": "raster",
                }
        except Exception as e:
            logger.error("tile publish tool error: %s", e, exc_info=True)
            return {"success": False, "error": f"发布失败: {str(e)[:300]}"}

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def _handle_status(self, params: dict) -> dict:
        job_id = (params.get("job_id") or "").strip()
        if not job_id:
            return {"success": False, "error": "查询进度必须提供 job_id"}

        job = _TILE_BUILD_JOBS.get(job_id) or _DRONE_BUILD_JOBS.get(job_id)
        if not job:
            # 已完成的 job 可能被清理（只有内存存储）
            return {
                "success": True,
                "job_id": job_id,
                "found": False,
                "message": "未找到该任务（可能已完成并被清理，或 job_id 错误）。发布成功的图层可在切片管理中查看。",
            }

        return {
            "success": True,
            "job_id": job_id,
            "found": True,
            "done": job.get("done", False),
            "success_result": job.get("success"),
            "stage": job.get("stage", ""),
            "percent": job.get("percent", 0),
            "message": job.get("message", ""),
            "layer": job.get("layer", ""),
        }
