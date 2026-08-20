"""3D Tiles 数据处理：安全解压、tileset 定位、meta 维护、启动自动注册。

包含上传 zip 的安全校验（zip slip 防护、大小/文件数限制），
以及启动时对已有数据集的异步自动注册。
"""
import logging
import os
import shutil
import threading
from datetime import datetime as dt
from typing import Any, Dict, Optional

from .registry import (
    _3DTILES_DATA_DIR,
    _3DTILES_REGISTRY_PATH,
    _MAX_3DTILES_FILES,
    _MAX_3DTILES_UNZIP_BYTES,
    _count_3dtiles,
    _load_3dtiles_registry,
    _read_3dtiles_meta,
    _save_3dtiles_registry,
)

logger = logging.getLogger(__name__)


def _locate_tileset_root(directory: str) -> Optional[str]:
    """定位包含 tileset.json 的目录：根目录优先；否则查找唯一含 tileset.json 的子目录（不限层级）。"""
    if os.path.isfile(os.path.join(directory, "tileset.json")):
        return directory
    candidates = []
    try:
        for root, _, files in os.walk(directory):
            if "tileset.json" in files:
                candidates.append(root)
                if len(candidates) > 1:
                    return None  # 多处 tileset.json，无法确定唯一根，交由上层报错
    except OSError:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def _extract_zip_safely(zip_path: str, target_dir: str) -> int:
    """安全解压 3D Tiles zip：校验路径穿越（zip slip）、文件数与解压总量限制。返回条目数。"""
    import zipfile
    total_size = 0
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename or ""
            norm = name.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                raise ValueError(f"压缩包包含非法路径: {name}")
            total_size += int(info.file_size or 0)
            count += 1
            if count > _MAX_3DTILES_FILES:
                raise ValueError(f"压缩包文件数超过限制（>{_MAX_3DTILES_FILES}）")
            if total_size > _MAX_3DTILES_UNZIP_BYTES:
                raise ValueError("压缩包解压后总大小超过限制")
        zf.extractall(target_dir)
    return count


def _copy_upload_limited(src, dst, limit: int) -> int:
    """限制大小的流式复制，超过 limit 抛 ValueError。返回写入字节数。"""
    total = 0
    while True:
        chunk = src.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"文件超过大小限制（{limit // 1024 // 1024} MB）")
        dst.write(chunk)
    return total


def _auto_register_existing_3dtiles() -> None:
    """启动时自动扫描 3dtiles_data 并注册已有的数据集。"""
    registry = _load_3dtiles_registry()
    if not os.path.isdir(_3DTILES_DATA_DIR):
        return
    updated = False
    for entry in sorted(os.listdir(_3DTILES_DATA_DIR)):
        full_dir = os.path.join(_3DTILES_DATA_DIR, entry)
        if not os.path.isdir(full_dir):
            continue
        tileset_path = os.path.join(full_dir, "tileset.json")
        if not os.path.isfile(tileset_path):
            continue
        if entry in registry:
            continue
        meta_info = _read_3dtiles_meta(full_dir)
        stats = _count_3dtiles(full_dir)
        registry[entry] = {
            "directory": full_dir,
            "label": meta_info.get("label") or meta_info.get("name") or entry,
            "name": meta_info.get("name") or entry,
            "tile_count": stats["tile_count"],
            "size_bytes": stats["size_bytes"],
            "alt_offset": meta_info.get("altOffset", 0.0),
            "auto_ground_clamp": meta_info.get("autoGroundClamp", True),
            "center": meta_info.get("center"),
            "description": meta_info.get("description") or "",
            "meta": meta_info,
            "created_at": dt.now().isoformat(timespec="seconds"),
        }
        logger.info("auto-registered 3dtiles: %s", entry)
        updated = True
    if updated:
        _save_3dtiles_registry(registry)


# 在模块加载时自动注册已有数据集（后台线程执行，不阻塞启动）
def _auto_register_existing_3dtiles_async() -> None:
    def _run() -> None:
        try:
            _auto_register_existing_3dtiles()
        except Exception as e:
            logger.warning("后台自动注册 3D Tiles 失败: %s", e)

    threading.Thread(target=_run, daemon=True).start()
