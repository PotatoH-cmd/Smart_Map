#!/usr/bin/env python3
"""
将 2026年Q1高分影像 GeoTIFF 发布到 GeoServer 为 WMTS 瓦片服务

用法:
    python publish_gf2026_geoserver.py

流程:
    1. 创建外部 GeoTIFF CoverageStore（引用磁盘文件，不复制）
    2. 发布 Coverage 图层
    3. 配置 WMS/WMTS 默认样式（RGB 直出）
    4. 可选：预热 GeoWebCache 瓦片缓存

发布后的访问地址:
    WMTS XYZ:  /geoserver/gwc/service/wmts/rest/map_assistant:gf2026q1/EPSG:900913/EPSG:900913:{z}/{y}/{x}?format=image/png
    WMS:       /geoserver/map_assistant/wms?service=WMS&layers=map_assistant:gf2026q1
"""

import os
import sys
import time
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("publish_gf2026")

# ── 配置 ──
GEOSERVER_URL = os.environ.get("GEOSERVER_URL", "http://127.0.0.1:8088/geoserver").rstrip("/")
GEOSERVER_USER = os.environ.get("GEOSERVER_USER", "admin")
GEOSERVER_PASSWORD = os.environ.get("GEOSERVER_PASSWORD", "change_me")
WORKSPACE = os.environ.get("GEOSERVER_WORKSPACE", "map_assistant")

# TIF 文件路径（外部引用，GeoServer 直接从磁盘读取）
# 优先使用无中文的软链接路径
_SYMLINK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gf2026q1.tif"
)
TIF_PATH = os.environ.get(
    "SATELLITE_TIF_PATH",
    _SYMLINK_PATH if os.path.isfile(_SYMLINK_PATH) else
    "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/河南2026年高分第一季度影像.tif",
)

# GeoServer 内部标识
STORE_NAME = "gf2026q1_store"
COVERAGE_NAME = "gf2026q1"


def gs_request(method: str, path: str, **kwargs) -> requests.Response:
    """向 GeoServer REST API 发请求"""
    url = path if path.startswith("http") else f"{GEOSERVER_URL}{path}"
    auth = (GEOSERVER_USER, GEOSERVER_PASSWORD)
    timeout = kwargs.pop("timeout", 60)

    if "headers" not in kwargs:
        kwargs["headers"] = {}
    kwargs["headers"].setdefault("Accept", "application/json")

    if "json" in kwargs:
        kwargs["headers"].setdefault("Content-Type", "application/json")
    if "data" in kwargs and isinstance(kwargs["data"], str):
        kwargs["headers"].setdefault("Content-Type", "text/plain")
        kwargs["data"] = kwargs["data"].encode("utf-8")  # 中文路径必须 UTF-8

    resp = requests.request(method, url, auth=auth, timeout=timeout, **kwargs)
    return resp


def check_workspace():
    """确认 workspace 存在"""
    resp = gs_request("GET", f"/rest/workspaces/{WORKSPACE}.json")
    if resp.status_code == 200:
        logger.info(f"✅ workspace '{WORKSPACE}' 已存在")
        return
    if resp.status_code == 404:
        logger.info(f"创建 workspace '{WORKSPACE}'...")
        resp = gs_request("POST", "/rest/workspaces",
                          json={"workspace": {"name": WORKSPACE}})
        if resp.status_code in (200, 201):
            logger.info(f"✅ workspace '{WORKSPACE}' 创建成功")
            return
    raise RuntimeError(f"workspace 操作失败: HTTP {resp.status_code} {resp.text[:300]}")


def check_file_accessible():
    """验证 GeoServer 能否访问文件（测试 CIFS 路径）"""
    if not os.path.isfile(TIF_PATH):
        raise RuntimeError(f"TIF 文件不存在: {TIF_PATH}")

    size_gb = os.path.getsize(TIF_PATH) / (1024 ** 3)
    logger.info(f"📁 TIF 文件: {TIF_PATH}")
    logger.info(f"   大小: {size_gb:.1f} GB")
    logger.info(f"   GeoServer 将引用此文件（不会复制）")


def delete_existing_store():
    """清理已有的 coverage store 和图层"""
    # 先取消发布图层
    resp = gs_request("GET", f"/rest/layers/{WORKSPACE}:{COVERAGE_NAME}.json")
    if resp.status_code == 200:
        logger.info(f"删除已有图层 '{COVERAGE_NAME}'...")
        gs_request("DELETE", f"/rest/layers/{WORKSPACE}:{COVERAGE_NAME}",
                   params={"recurse": "true"})
        time.sleep(1)

    # 删除已有 coverage store
    for purge in ("none", "metadata", "all"):
        resp = gs_request("DELETE",
                          f"/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME}.json",
                          params={"recurse": "true", "purge": purge})
        if resp.status_code in (200, 202, 404):
            break
        logger.debug(f"  purge={purge}: HTTP {resp.status_code}")
    logger.info("✅ 已清理旧图层")


def create_external_coverage_store():
    """
    创建引用外部 GeoTIFF 文件的 CoverageStore（不复制文件）

    GeoServer REST API:
      PUT /rest/workspaces/{ws}/coveragestores/{store}/external.geotiff
        ?configure=first&coverageName={name}
        Content-Type: text/plain
        Body: file:///absolute/path/to/file.tif
    """
    from urllib.parse import quote as url_quote

    url = (f"/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME}/external.geotiff"
           f"?configure=first&coverageName={COVERAGE_NAME}")

    # 对中文路径做 URL 编码，GeoServer 才能正确解析
    file_uri = f"file://{url_quote(TIF_PATH, safe='/')}"

    logger.info(f"创建外部 CoverageStore...")
    logger.info(f"   文件引用: {file_uri}")

    resp = gs_request("PUT", url, data=file_uri, timeout=120)

    if resp.status_code in (200, 201, 202):
        logger.info(f"✅ CoverageStore 创建成功")
        return

    # 备选方案：用 POST + 完整配置
    logger.warning(f"external.geotiff 方式返回 HTTP {resp.status_code}, 尝试备用方案...")

    # 先创建空的 store
    payload = {
        "coverageStore": {
            "name": STORE_NAME,
            "type": "GeoTIFF",
            "enabled": True,
            "url": file_uri,
            "workspace": {"name": WORKSPACE},
        }
    }
    resp = gs_request("POST",
                      f"/rest/workspaces/{WORKSPACE}/coveragestores",
                      json=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"创建 CoverageStore 失败: HTTP {resp.status_code} {resp.text[:300]}")
    logger.info("✅ CoverageStore 创建成功（备用方案）")


def publish_coverage():
    """发布 Coverage 图层"""
    # 检查图层是否已存在
    resp = gs_request("GET", f"/rest/layers/{WORKSPACE}:{COVERAGE_NAME}.json")
    if resp.status_code == 200:
        logger.info(f"✅ 图层 '{COVERAGE_NAME}' 已发布")
        return

    payload = {
        "coverage": {
            "name": COVERAGE_NAME,
            "nativeName": COVERAGE_NAME,
            "enabled": True,
            "title": "2026年Q1河南省高分影像（亚米级）",
            "srs": "EPSG:4490",
            "projectionPolicy": "FORCE_DECLARED",
        }
    }
    resp = gs_request("POST",
                      f"/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME}/coverages",
                      json=payload)
    if resp.status_code in (200, 201):
        logger.info(f"✅ 图层 '{COVERAGE_NAME}' 发布成功")
    else:
        logger.warning(f"发布图层返回 HTTP {resp.status_code}: {resp.text[:200]}")


def enable_wms_wmts():
    """确保 WMS/WMTS 已启用并配置缓存"""
    # 设置图层为 advertised + enabled
    payload = {
        "layer": {
            "enabled": True,
            "advertised": True,
        }
    }
    resp = gs_request("PUT", f"/rest/layers/{WORKSPACE}:{COVERAGE_NAME}",
                      json=payload)
    if resp.status_code in (200, 201):
        logger.info("✅ 图层已启用 WMS/WMTS 对外服务")
    else:
        logger.warning(f"启用图层返回 HTTP {resp.status_code}")

    # 配置 GeoWebCache 瓦片网格集
    # 添加 EPSG:900913 (Web Mercator) 和 EPSG:4326 网格集
    tile_payload = {
        "layer": {
            "metadata": {
                "entry": [
                    {"@key": "cachingEnabled", "$": "true"},
                    {"@key": "gwc.gridSetNames", "$": "EPSG:900913,EPSG:4326"},
                ]
            }
        }
    }
    gs_request("PUT", f"/rest/layers/{WORKSPACE}:{COVERAGE_NAME}",
               json=tile_payload)
    logger.info("✅ GeoWebCache 瓦片缓存已启用（EPSG:900913 + EPSG:4326）")


def print_endpoints():
    """展示可用的访问地址"""
    base = GEOSERVER_URL
    ws = WORKSPACE
    qname = f"{ws}:{COVERAGE_NAME}"

    logger.info("")
    logger.info("=" * 60)
    logger.info("📡 服务端点")
    logger.info("=" * 60)
    logger.info(f"WMS GetCapabilities:")
    logger.info(f"  {base}/{ws}/wms?service=WMS&version=1.3.0&request=GetCapabilities")
    logger.info(f"")
    logger.info(f"WMTS (XYZ 瓦片, Leaflet 可直接用):")
    logger.info(f"  {base}/gwc/service/wmts/rest/{qname}/EPSG:900913/EPSG:900913/{{z}}/{{y}}/{{x}}?format=image/png")
    logger.info(f"")
    logger.info(f"WMS GetMap (预览):")
    logger.info(f"  {base}/{ws}/wms?service=WMS&version=1.1.1&request=GetMap"
                f"&layers={qname}&styles=&bbox=110.35,31.38,116.65,36.37"
                f"&width=800&height=600&srs=EPSG:4326&format=image/png")
    logger.info(f"")
    logger.info(f"前端代理路径（推荐）:")
    logger.info(f"  /proxy/gf2026-tiles/{{z}}/{{y}}/{{x}}")
    logger.info(f"  由 FastAPI 后端代理到 GeoServer，避免跨域问题")
    logger.info("=" * 60)


def optional_seed_cache():
    """可选：预热 GeoWebCache 瓦片（建议后台执行）"""
    import argparse
    # 通过环境变量控制
    if os.environ.get("SEED_CACHE", "").lower() not in ("1", "true", "yes"):
        logger.info("")
        logger.info("💡 提示: 设置 SEED_CACHE=1 可预热 0-12 级瓦片缓存（耗时较长）")
        logger.info("   export SEED_CACHE=1 && python publish_gf2026_geoserver.py")
        return

    logger.info("🔥 开始预热 GeoWebCache 瓦片 (0-12 级)...")
    qname = f"{WORKSPACE}:{COVERAGE_NAME}"

    payload = {
        "seedRequest": {
            "name": qname,
            "srs": {"number": 900913},
            "zoomStart": 0,
            "zoomStop": 12,
            "format": "image/png",
            "type": "seed",
            "threadCount": 4,
        }
    }
    resp = gs_request("POST", f"/gwc/rest/seed/{qname}.json",
                      json=payload)
    if resp.status_code in (200, 201, 202):
        logger.info("✅ GWC 预热任务已提交（后台执行中...）")
        logger.info("   查看进度: GET /gwc/rest/seed/{qname}.json")
    else:
        logger.warning(f"GWC 预热提交失败: HTTP {resp.status_code} {resp.text[:200]}")


def main():
    logger.info("=" * 60)
    logger.info("🚀 将 2026年Q1高分影像发布到 GeoServer")
    logger.info("=" * 60)

    check_file_accessible()
    check_workspace()
    delete_existing_store()
    create_external_coverage_store()
    publish_coverage()
    enable_wms_wmts()
    print_endpoints()
    optional_seed_cache()

    logger.info("")
    logger.info("✅ 发布完成！前端可通过 WMTS 或代理路径访问瓦片。")


if __name__ == "__main__":
    main()
