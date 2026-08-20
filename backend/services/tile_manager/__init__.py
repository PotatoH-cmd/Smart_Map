"""切片管理服务包。

分层：
- registry.py：注册表 + 统计缓存（矢量/栅格/无人机/3D Tiles 通用）
- three_d.py：3D Tiles 安全解压/定位/自动注册
- builders.py：tippecanoe / GDAL 构建逻辑
- tasks.py：后台构建任务管理

路由层（APIRouter）暂由 main.py 承载，后续可迁入 routes.py。
"""
from .registry import (  # noqa: F401
    _3DTILES_DATA_DIR,
    _3DTILES_REGISTRY_PATH,
    _CUSTOM_TILE_DATA_DIR,
    _DRONE_IMAGERY_DIR,
    _DRONE_MBTILES_DIR,
    _DRONE_REGISTRY_PATH,
    _DRONE_WORK_DIR,
    _MAX_3DTILES_FILES,
    _MAX_3DTILES_UNZIP_BYTES,
    _MAX_3DTILES_ZIP_BYTES,
    _OVERLAY_DATA_DIR,
    _TILE_LAYER_META,
    _TILE_REGISTRY_PATH,
    _VT_DIR,
    _3dtiles_layer_to_row,
    _count_3dtiles,
    _dir_stats,
    _drone_layer_to_row,
    _invalidate_tile_stats,
    _load_3dtiles_registry,
    _load_drone_registry,
    _load_tile_registry,
    _mbtiles_metadata,
    _media_type_for_tile_format,
    _merged_tile_meta,
    _parse_bounds,
    _parse_style,
    _read_3dtiles_meta,
    _register_drone_imagery,
    _sanitize_layer_key,
    _save_3dtiles_registry,
    _save_drone_registry,
    _save_tile_registry,
    _ttl_cache,
    _write_3dtiles_meta,
)
from .three_d import (  # noqa: F401
    _auto_register_existing_3dtiles,
    _auto_register_existing_3dtiles_async,
    _copy_upload_limited,
    _extract_zip_safely,
    _locate_tileset_root,
)
from .builders import (  # noqa: F401
    _detect_source_srs,
    _run_drone_build_with_progress,
    _run_drone_mbtiles_build,
    _run_tippecanoe,
)
from .tasks import (  # noqa: F401
    _DRONE_BUILD_JOBS,
    _TILE_BUILD_JOBS,
    _submit_tile_build_job,
)
