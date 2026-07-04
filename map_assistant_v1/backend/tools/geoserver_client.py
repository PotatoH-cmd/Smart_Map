"""
GeoServer REST 客户端封装
========================

为 FastAPI `/api/geoserver/**` 端点提供：
- 健康检查 / Capabilities URL 拼装
- 已发布图层映射（GeoServer layer ↔ TileManager layer key）
- 一键发布：按 TileManager key 推断资源类型并调用相应发布逻辑
  (PostGIS featuretype / GeoTIFF coveragestore / MBTiles coveragestore)
- GeoWebCache seed / truncate / 状态查询

设计说明：
- 凭据来源：环境变量 GEOSERVER_URL / GEOSERVER_USER / GEOSERVER_PASSWORD /
  GEOSERVER_WORKSPACE / GEOSERVER_PG_HOST / GEOSERVER_PG_PORT /
  GEOSERVER_PG_DB / GEOSERVER_PG_USER / GEOSERVER_PG_PASSWORD
- 不可用时所有方法返回 {"available": False, ...} 或抛 GeoServerUnavailable，
  调用方应当捕获并优雅降级，避免影响切片管理主流程
- 该模块对 GeoServer 不可达完全容错，只读接口尽量幂等
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests

try:
    import psycopg2
except Exception:
    psycopg2 = None

logger = logging.getLogger(__name__)


class GeoServerUnavailable(RuntimeError):
    """GeoServer 不可达或返回非预期状态。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def get_config() -> Dict[str, Any]:
    return {
        "url": _env("GEOSERVER_URL", "http://127.0.0.1:8088/geoserver").rstrip("/"),
        "user": _env("GEOSERVER_USER", "admin"),
        "password": _env("GEOSERVER_PASSWORD", "change_me"),
        "workspace": _env("GEOSERVER_WORKSPACE", "map_assistant"),
        "pg_host": _env("GEOSERVER_PG_HOST", "172.136.16.52"),
        "pg_port": _env("GEOSERVER_PG_PORT", "5432"),
        "pg_db": _env("GEOSERVER_PG_DB", "postgres"),
        "pg_user": _env("GEOSERVER_PG_USER", "postgres"),
        "pg_password": _env("GEOSERVER_PG_PASSWORD", "8720622"),
        "pg_schema_overlay": _env("GEOSERVER_PG_SCHEMA_OVERLAY", "overlay"),
        "pg_schema_business": _env("GEOSERVER_PG_SCHEMA_BUSINESS", "public"),
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        cfg = get_config()
        s = requests.Session()
        s.auth = (cfg["user"], cfg["password"])
        s.headers.update({"Accept": "application/json"})
        _SESSION = s
    return _SESSION


def _request(method: str, path: str, *, timeout: int = 15, **kwargs) -> requests.Response:
    cfg = get_config()
    url = path if path.startswith("http") else f"{cfg['url']}{path}"
    try:
        resp = _session().request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as e:
        raise GeoServerUnavailable(str(e)) from e
    return resp


# ---------------------------------------------------------------------------
# 状态 / Capabilities
# ---------------------------------------------------------------------------

def health_status() -> Dict[str, Any]:
    cfg = get_config()
    base = cfg["url"]
    try:
        resp = _request("GET", "/rest/about/version.json", timeout=5)
        if resp.status_code != 200:
            return {
                "available": False,
                "url": base,
                "reason": f"HTTP {resp.status_code}",
            }
        data = resp.json()
    except GeoServerUnavailable as e:
        return {"available": False, "url": base, "reason": str(e)}

    versions = {}
    for item in data.get("about", {}).get("resource", []) or []:
        name = item.get("name") or item.get("@name") or "?"
        version = item.get("version") or item.get("Version") or "?"
        versions[name] = str(version)

    workspace_count = layer_count = 0
    try:
        ws_resp = _request("GET", "/rest/workspaces.json", timeout=5)
        if ws_resp.status_code == 200:
            ws_list = ws_resp.json().get("workspaces", {}) or {}
            ws_arr = (ws_list or {}).get("workspace", []) or []
            workspace_count = len(ws_arr)
    except GeoServerUnavailable:
        pass
    try:
        layers_resp = _request("GET", "/rest/layers.json", timeout=5)
        if layers_resp.status_code == 200:
            layers_arr = (layers_resp.json().get("layers", {}) or {}).get("layer", []) or []
            layer_count = len(layers_arr)
    except GeoServerUnavailable:
        pass

    gwc = {"available": False}
    try:
        gwc_resp = _request("GET", "/gwc/rest/diskquota.json", timeout=5)
        if gwc_resp.status_code == 200:
            gwc = {"available": True, "raw": gwc_resp.json()}
    except GeoServerUnavailable:
        pass

    ws = cfg["workspace"]
    return {
        "available": True,
        "url": base,
        "workspace": ws,
        "versions": versions,
        "workspace_count": workspace_count,
        "layer_count": layer_count,
        "gwc": gwc,
        "capabilities": capabilities_urls(),
    }


def capabilities_urls() -> Dict[str, str]:
    cfg = get_config()
    base = cfg["url"]
    ws = cfg["workspace"]
    return {
        "wms_1_3_0": f"{base}/{ws}/wms?service=WMS&version=1.3.0&request=GetCapabilities",
        "wms_1_1_1": f"{base}/{ws}/wms?service=WMS&version=1.1.1&request=GetCapabilities",
        "wmts": f"{base}/gwc/service/wmts?REQUEST=GetCapabilities",
        "wfs": f"{base}/{ws}/wfs?service=WFS&version=2.0.0&request=GetCapabilities",
        "wcs": f"{base}/{ws}/wcs?service=WCS&version=2.0.1&request=GetCapabilities",
    }


def layer_urls(layer_name: str) -> Dict[str, str]:
    """组装单个图层常用接入 URL（仅拼字符串，不验证存在性）。"""
    cfg = get_config()
    base = cfg["url"]
    ws = cfg["workspace"]
    qname = quote(f"{ws}:{layer_name}", safe=":")
    return {
        "wms_get_map_template": (
            f"{base}/{ws}/wms?service=WMS&version=1.1.1&request=GetMap&layers={qname}"
            f"&styles=&bbox={{minx,miny,maxx,maxy}}&width=512&height=512&srs=EPSG:3857&format=image/png"
        ),
        "wmts_xyz": (
            f"{base}/gwc/service/wmts/rest/{qname}/EPSG:900913/EPSG:900913:{{z}}/{{y}}/{{x}}?format=image/png"
        ),
        "wfs_get_feature": (
            f"{base}/{ws}/wfs?service=WFS&version=2.0.0&request=GetFeature&typeNames={qname}"
            f"&outputFormat=application/json&count=10"
        ),
        "tilejson": f"{base}/gwc/service/tms/1.0.0/{ws}:{layer_name}@EPSG:900913@png",
    }


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_bbox(resource: Dict[str, Any]) -> Optional[List[float]]:
    box = resource.get("latLonBoundingBox") or resource.get("nativeBoundingBox") or {}
    minx = _to_float(box.get("minx"))
    miny = _to_float(box.get("miny"))
    maxx = _to_float(box.get("maxx"))
    maxy = _to_float(box.get("maxy"))
    if None in (minx, miny, maxx, maxy):
        return None
    if minx == maxx:
        minx -= 0.01
        maxx += 0.01
    if miny == maxy:
        miny -= 0.01
        maxy += 0.01
    return [minx, miny, maxx, maxy]


def _postgis_table_bounds(table_name: str) -> Optional[List[float]]:
    if psycopg2 is None or not re.match(r"^[A-Za-z0-9_]+$", table_name):
        return None
    cfg = get_config()
    schema = _resolve_postgis_schema(table_name, [cfg["pg_schema_overlay"], cfg["pg_schema_business"]])
    try:
        conn = psycopg2.connect(
            host=cfg["pg_host"],
            port=int(cfg["pg_port"]),
            dbname=cfg["pg_db"],
            user=cfg["pg_user"],
            password=cfg["pg_password"],
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            f"""
            select ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
            from (select ST_Extent(geom)::box3d e from "{schema}"."{table_name}") s
            """
        )
        row = cur.fetchone()
        conn.close()
        if not row or any(v is None for v in row):
            return None
        minx, miny, maxx, maxy = [float(v) for v in row]
        if minx == maxx:
            minx -= 0.01
            maxx += 0.01
        if miny == maxy:
            miny -= 0.01
            maxy += 0.01
        return [minx, miny, maxx, maxy]
    except Exception as e:
        logger.warning("postgis bounds failed for %s: %s", table_name, e)
        return None


def _postgis_geometry_type(table_name: str) -> str:
    if psycopg2 is None or not re.match(r"^[A-Za-z0-9_]+$", table_name):
        return ""
    cfg = get_config()
    schema = _resolve_postgis_schema(table_name, [cfg["pg_schema_overlay"], cfg["pg_schema_business"]])
    try:
        conn = psycopg2.connect(
            host=cfg["pg_host"],
            port=int(cfg["pg_port"]),
            dbname=cfg["pg_db"],
            user=cfg["pg_user"],
            password=cfg["pg_password"],
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            """
            select type
            from geometry_columns
            where f_table_schema = %s and f_table_name = %s
            limit 1
            """,
            (schema, table_name),
        )
        row = cur.fetchone()
        conn.close()
        return str(row[0]).upper() if row and row[0] else ""
    except Exception as e:
        logger.warning("postgis geometry type failed for %s: %s", table_name, e)
        return ""


def _preview_url(layer_name: str, bbox: Optional[List[float]]) -> Optional[str]:
    if not bbox:
        return None
    cfg = get_config()
    base = cfg["url"]
    ws = cfg["workspace"]
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": f"{ws}:{layer_name}",
        "styles": "",
        "bbox": ",".join(str(v) for v in bbox),
        "width": "520",
        "height": "280",
        "srs": "EPSG:4326",
        "format": "image/png",
        "transparent": "true",
    }
    return f"{base}/{ws}/wms?{urlencode(params)}"


def _layer_bounds(layer_name: str) -> Optional[List[float]]:
    postgis_bounds = _postgis_table_bounds(layer_name)
    if postgis_bounds:
        return postgis_bounds
    cfg = get_config()
    ws = cfg["workspace"]
    detail = _request("GET", f"/rest/layers/{ws}:{layer_name}.json", timeout=5)
    if detail.status_code != 200:
        return None
    data = detail.json().get("layer", {}) or {}
    resource = data.get("resource") or {}
    resource_href = resource.get("href") if isinstance(resource, dict) else ""
    if not resource_href:
        return None
    resource_resp = _request("GET", resource_href, timeout=5)
    if resource_resp.status_code != 200:
        return None
    resource_data = resource_resp.json() or {}
    resource_body = resource_data.get("featureType") or resource_data.get("coverage") or {}
    return _extract_bbox(resource_body)


def preview_image(layer_name: str, *, bbox: Optional[List[float]] = None,
                  width: int = 520, height: int = 280) -> Tuple[bytes, str]:
    if not re.match(r"^[A-Za-z0-9_-]+$", layer_name):
        raise GeoServerUnavailable(f"invalid layer name: {layer_name}")
    bounds = bbox if bbox and len(bbox) == 4 else _layer_bounds(layer_name)
    url = _preview_url(layer_name, bounds)
    if not url:
        raise GeoServerUnavailable(f"layer preview bounds unavailable: {layer_name}")
    url = url.replace("width=520", f"width={max(128, min(int(width), 1600))}")
    url = url.replace("height=280", f"height={max(128, min(int(height), 1200))}")
    resp = _request("GET", url, timeout=20, headers={"Accept": "image/png"})
    if resp.status_code != 200:
        raise GeoServerUnavailable(f"preview image failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.content, resp.headers.get("Content-Type", "image/png")


# ---------------------------------------------------------------------------
# 图层列表
# ---------------------------------------------------------------------------

def list_layers() -> List[Dict[str, Any]]:
    """列出当前 workspace 下的全部图层及其类型（vector/raster）。"""
    cfg = get_config()
    ws = cfg["workspace"]
    items: List[Dict[str, Any]] = []
    try:
        resp = _request("GET", f"/rest/workspaces/{ws}/layers.json", timeout=10)
    except GeoServerUnavailable:
        return items
    if resp.status_code != 200:
        return items
    arr = (resp.json().get("layers", {}) or {}).get("layer", []) or []
    for entry in arr:
        name = entry.get("name", "")
        if not name:
            continue
        # 详情查询：判断 vector/raster
        try:
            detail = _request("GET", f"/rest/layers/{ws}:{name}.json", timeout=5)
            if detail.status_code == 200:
                d = detail.json().get("layer", {}) or {}
                ltype = (d.get("type") or "").upper()
                resource = d.get("resource") or {}
            else:
                ltype = ""
                resource = {}
        except GeoServerUnavailable:
            ltype = ""
            resource = {}
        try:
            bounds = _layer_bounds(name)
        except GeoServerUnavailable:
            bounds = None
        items.append({
            "name": name,
            "qualified_name": f"{ws}:{name}",
            "type": "raster" if ltype == "RASTER" else "vector",
            "urls": layer_urls(name),
            "bounds": bounds,
            "preview_url": _preview_url(name, bounds),
            "preview_proxy_url": f"/api/geoserver/preview/{name}.png",
        })
    return items


# ---------------------------------------------------------------------------
# Workspace / Datastore 准备
# ---------------------------------------------------------------------------

def ensure_workspace() -> None:
    cfg = get_config()
    ws = cfg["workspace"]
    resp = _request("GET", f"/rest/workspaces/{ws}.json", timeout=5)
    if resp.status_code == 200:
        return
    if resp.status_code != 404:
        raise GeoServerUnavailable(f"check workspace failed: HTTP {resp.status_code}")
    create = _request(
        "POST",
        "/rest/workspaces",
        json={"workspace": {"name": ws}},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if create.status_code not in (200, 201):
        raise GeoServerUnavailable(f"create workspace failed: HTTP {create.status_code} {create.text[:200]}")


def _postgis_store_payload(store_name: str, schema: str) -> Dict[str, Any]:
    cfg = get_config()
    return {
        "dataStore": {
            "name": store_name,
            "type": "PostGIS",
            "enabled": True,
            "connectionParameters": {
                "entry": [
                    {"@key": "host", "$": cfg["pg_host"]},
                    {"@key": "port", "$": str(cfg["pg_port"])},
                    {"@key": "database", "$": cfg["pg_db"]},
                    {"@key": "schema", "$": schema},
                    {"@key": "user", "$": cfg["pg_user"]},
                    {"@key": "passwd", "$": cfg["pg_password"]},
                    {"@key": "dbtype", "$": "postgis"},
                    {"@key": "Expose primary keys", "$": "true"},
                ]
            },
        }
    }


def _postgis_store_name(schema: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", schema or "public")
    return f"{safe}_pg"


def _resolve_postgis_schema(table_name: str, preferred: List[str]) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", table_name):
        raise GeoServerUnavailable(f"invalid table name: {table_name}")
    unique = []
    for schema in preferred:
        if schema and schema not in unique:
            unique.append(schema)
    if "public" not in unique:
        unique.append("public")
    if psycopg2 is None:
        return unique[0]
    cfg = get_config()
    try:
        conn = psycopg2.connect(
            host=cfg["pg_host"],
            port=int(cfg["pg_port"]),
            dbname=cfg["pg_db"],
            user=cfg["pg_user"],
            password=cfg["pg_password"],
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            """
            select table_schema
            from information_schema.tables
            where table_name = %s and table_schema = any(%s)
            """,
            (table_name, unique),
        )
        found = {row[0] for row in cur.fetchall()}
        conn.close()
        for schema in unique:
            if schema in found:
                return schema
    except Exception as e:
        logger.warning("resolve postgis schema failed for %s: %s", table_name, e)
    return unique[0]


def _ensure_postgis_schema(schema: str) -> None:
    if psycopg2 is None:
        raise GeoServerUnavailable("psycopg2 unavailable, cannot create PostGIS schema")
    if not re.match(r"^[A-Za-z0-9_]+$", schema):
        raise GeoServerUnavailable(f"invalid schema name: {schema}")
    cfg = get_config()
    try:
        conn = psycopg2.connect(
            host=cfg["pg_host"],
            port=int(cfg["pg_port"]),
            dbname=cfg["pg_db"],
            user=cfg["pg_user"],
            password=cfg["pg_password"],
            connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.close()
    except Exception as e:
        raise GeoServerUnavailable(f"create schema failed: {e}") from e


def import_geojson_to_postgis(layer_key: str, source_path: str, schema: Optional[str] = None) -> Dict[str, Any]:
    if not re.match(r"^[A-Za-z0-9_]+$", layer_key):
        raise GeoServerUnavailable(f"invalid layer key for PostGIS import: {layer_key}")
    if not source_path or not os.path.isfile(source_path):
        raise GeoServerUnavailable(f"source geojson not found: {source_path}")
    cfg = get_config()
    target_schema = schema or cfg["pg_schema_overlay"] or "overlay"
    _ensure_postgis_schema(target_schema)
    conn_str = (
        f"PG:host={cfg['pg_host']} port={cfg['pg_port']} "
        f"dbname={cfg['pg_db']} user={cfg['pg_user']}"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["pg_password"]
    cmd = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        conn_str,
        source_path,
        "-nln",
        f"{target_schema}.{layer_key}",
        "-overwrite",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "FID=fid",
        "-t_srs",
        "EPSG:4326",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=env,
        )
    except FileNotFoundError as e:
        raise GeoServerUnavailable("ogr2ogr 未安装或不在 PATH 中") from e
    except subprocess.TimeoutExpired as e:
        raise GeoServerUnavailable("ogr2ogr 导入 PostGIS 超时") from e
    if result.returncode != 0:
        raise GeoServerUnavailable(result.stderr[-800:] or "ogr2ogr import failed")
    return {"schema": target_schema, "table": layer_key}


def ensure_postgis_store(store_name: str, schema: str) -> None:
    """确保 PostGIS DataStore 存在，幂等。"""
    cfg = get_config()
    ws = cfg["workspace"]
    payload = _postgis_store_payload(store_name, schema)
    resp = _request("GET", f"/rest/workspaces/{ws}/datastores/{store_name}.json", timeout=5)
    if resp.status_code == 200:
        update = _request(
            "PUT",
            f"/rest/workspaces/{ws}/datastores/{store_name}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if update.status_code not in (200, 201):
            raise GeoServerUnavailable(f"update datastore failed: HTTP {update.status_code} {update.text[:200]}")
        return
    if resp.status_code != 404:
        raise GeoServerUnavailable(f"check datastore failed: HTTP {resp.status_code}")
    create = _request(
        "POST",
        f"/rest/workspaces/{ws}/datastores",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if create.status_code not in (200, 201):
        raise GeoServerUnavailable(f"create datastore failed: HTTP {create.status_code} {create.text[:200]}")


def _delete_geotiff_store(store_name: str) -> None:
    cfg = get_config()
    ws = cfg["workspace"]
    errors = []
    for purge in ("none", "metadata", "all"):
        resp = _request(
            "DELETE",
            f"/rest/workspaces/{ws}/coveragestores/{store_name}.json",
            params={"recurse": "true", "purge": purge},
            timeout=30,
        )
        if resp.status_code in (200, 202, 404):
            return
        errors.append(f"purge={purge} HTTP {resp.status_code} {resp.text[:160]}")
    raise GeoServerUnavailable(f"delete coveragestore failed: {'; '.join(errors)}")


def ensure_geotiff_store(store_name: str, file_path: str, coverage_name: str = "") -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    if not file_path or not os.path.isfile(file_path):
        raise GeoServerUnavailable(f"source geotiff not found: {file_path}")
    resp = _request("GET", f"/rest/workspaces/{ws}/coveragestores/{store_name}.json", timeout=5)
    if resp.status_code == 200:
        _delete_geotiff_store(store_name)
    elif resp.status_code != 404:
        raise GeoServerUnavailable(f"check coveragestore failed: HTTP {resp.status_code}")
    name = coverage_name or store_name
    with open(file_path, "rb") as f:
        upload = _request(
            "PUT",
            f"/rest/workspaces/{ws}/coveragestores/{store_name}/file.geotiff",
            params={"configure": "first", "coverageName": name, "update": "overwrite"},
            data=f,
            headers={"Content-Type": "image/tiff", "Accept": "application/json"},
            timeout=600,
        )
    if upload.status_code not in (200, 201, 202):
        raise GeoServerUnavailable(f"upload geotiff failed: HTTP {upload.status_code} {upload.text[:300]}")
    return {"store": store_name, "coverage": name, "uploaded": True}


# ---------------------------------------------------------------------------
# 发布 (publish_by_tm_key)
# ---------------------------------------------------------------------------

def _publish_postgis_featuretype(store: str, table_name: str, layer_name: str, *, srs: str = "EPSG:4326") -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    payload = {
        "featureType": {
            "name": layer_name,
            "nativeName": table_name,
            "srs": srs,
            "enabled": True,
        }
    }
    resp = _request(
        "POST",
        f"/rest/workspaces/{ws}/datastores/{store}/featuretypes",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        # 已存在则视为成功
        if resp.status_code == 500 and "already exists" in resp.text.lower():
            return {"reused": True}
        raise GeoServerUnavailable(f"publish featuretype failed: HTTP {resp.status_code} {resp.text[:300]}")
    return {"reused": False}


def _publish_geotiff_coverage(store: str, layer_name: str, native_name: str = "") -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    payload = {
        "coverage": {
            "name": layer_name,
            "nativeName": native_name or layer_name,
            "enabled": True,
        }
    }
    resp = _request(
        "POST",
        f"/rest/workspaces/{ws}/coveragestores/{store}/coverages",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        if resp.status_code == 500 and "already exists" in resp.text.lower():
            return {"reused": True}
        raise GeoServerUnavailable(f"publish coverage failed: HTTP {resp.status_code} {resp.text[:300]}")
    return {"reused": False}


def _hex_color(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if re.match(r"^#[0-9a-fA-F]{6}$", text) else default


def _sld_for_style(style_name: str, style: Dict[str, Any], geometry_type: str = "") -> str:
    stroke = _hex_color(style.get("stroke"), "#2773d7")
    fill = _hex_color(style.get("fill"), stroke)
    try:
        fill_alpha = max(0.0, min(1.0, float(style.get("fillAlpha", 0.18))))
    except (TypeError, ValueError):
        fill_alpha = 0.18
    try:
        stroke_width = max(0.1, min(20.0, float(style.get("strokeWidth", 2))))
    except (TypeError, ValueError):
        stroke_width = 2
    try:
        point_size = max(2.0, min(64.0, float(style.get("pointSize", 8))))
    except (TypeError, ValueError):
        point_size = 8
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", style_name)
    geometry_type = (geometry_type or "").upper()
    if "POINT" in geometry_type:
        symbolizer = f"""
          <PointSymbolizer>
            <Graphic>
              <Mark>
                <WellKnownName>circle</WellKnownName>
                <Fill>
                  <CssParameter name="fill">{fill}</CssParameter>
                  <CssParameter name="fill-opacity">{max(fill_alpha, 0.35)}</CssParameter>
                </Fill>
                <Stroke>
                  <CssParameter name="stroke">{stroke}</CssParameter>
                  <CssParameter name="stroke-width">{max(stroke_width / 2, 1)}</CssParameter>
                </Stroke>
              </Mark>
              <Size>{point_size}</Size>
            </Graphic>
          </PointSymbolizer>"""
    elif "LINE" in geometry_type:
        symbolizer = f"""
          <LineSymbolizer>
            <Stroke>
              <CssParameter name="stroke">{stroke}</CssParameter>
              <CssParameter name="stroke-width">{stroke_width}</CssParameter>
            </Stroke>
          </LineSymbolizer>"""
    else:
        symbolizer = f"""
          <PolygonSymbolizer>
            <Fill>
              <CssParameter name="fill">{fill}</CssParameter>
              <CssParameter name="fill-opacity">{fill_alpha}</CssParameter>
            </Fill>
            <Stroke>
              <CssParameter name="stroke">{stroke}</CssParameter>
              <CssParameter name="stroke-width">{stroke_width}</CssParameter>
            </Stroke>
          </PolygonSymbolizer>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld"
  xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>{name}</Name>
    <UserStyle>
      <Name>{name}</Name>
      <FeatureTypeStyle>
        <Rule>
{symbolizer}
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>"""


def apply_layer_style(layer_name: str, style: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    style_name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{layer_name}_style")
    sld = _sld_for_style(style_name, style or {}, _postgis_geometry_type(layer_name))
    headers = {"Content-Type": "application/vnd.ogc.sld+xml"}
    check = _request("GET", f"/rest/workspaces/{ws}/styles/{style_name}.json", timeout=5)
    if check.status_code == 200:
        resp = _request(
            "PUT",
            f"/rest/workspaces/{ws}/styles/{style_name}",
            data=sld.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    elif check.status_code == 404:
        resp = _request(
            "POST",
            f"/rest/workspaces/{ws}/styles",
            params={"name": style_name},
            data=sld.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    else:
        raise GeoServerUnavailable(f"check style failed: HTTP {check.status_code}")
    if resp.status_code not in (200, 201):
        raise GeoServerUnavailable(f"save style failed: HTTP {resp.status_code} {resp.text[:200]}")
    layer_payload = {
        "layer": {
            "defaultStyle": {
                "name": style_name,
                "workspace": ws,
            }
        }
    }
    bind = _request(
        "PUT",
        f"/rest/layers/{ws}:{layer_name}",
        json=layer_payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if bind.status_code not in (200, 201):
        raise GeoServerUnavailable(f"bind style failed: HTTP {bind.status_code} {bind.text[:200]}")
    return {"style_name": style_name}


def publish_by_tm_key(layer_key: str, layer_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据 TileManager 提供的 layer key 与原始 meta 推断资源类型并发布到 GeoServer。
    `layer_meta` 期待包含 `type`, `source_path` 等字段（来源 /api/tile_manager/layers）。
    返回 {layer_name, type, urls}。
    """
    ensure_workspace()
    cfg = get_config()
    ws = cfg["workspace"]
    type_ = (layer_meta or {}).get("type") or ""
    source_path = (layer_meta or {}).get("source_path") or ""
    style = (layer_meta or {}).get("style") or {}
    publish_extra: Dict[str, Any] = {}

    if layer_key in ("hx", "caiqu"):
        # 内置矢量：从 PostGIS overlay schema 发布；调用方负责确保数据已 ogr2ogr 入库
        schema = _resolve_postgis_schema(layer_key, [cfg["pg_schema_overlay"], cfg["pg_schema_business"]])
        store = _postgis_store_name(schema)
        ensure_postgis_store(store, schema)
        _publish_postgis_featuretype(store, layer_key, layer_key, srs="EPSG:4326")
        published = layer_key
    elif layer_key == "ceshen":
        schema = _resolve_postgis_schema("ceshen", [cfg["pg_schema_business"], cfg["pg_schema_overlay"]])
        store = _postgis_store_name(schema)
        ensure_postgis_store(store, schema)
        _publish_postgis_featuretype(store, "ceshen", "ceshen", srs="EPSG:4326")
        published = "ceshen"
    elif type_ == "drone":
        if not source_path or not os.path.isfile(source_path):
            # MBTiles 也走 GeoTIFF 路径不可行；要求先有 _3857.tif
            raise GeoServerUnavailable(
                f"drone layer '{layer_key}' 缺少可用 GeoTIFF（{source_path}），请重新构建并保留 _3857.tif"
            )
        store_name = f"drone_{layer_key}"
        publish_extra["upload"] = ensure_geotiff_store(store_name, source_path, coverage_name=layer_key)
        published = layer_key
    elif type_ in ("vector", "raster"):
        # 自定义上传图层；走 PostGIS overlay schema（由调用方 ogr2ogr 入库）
        schema = _resolve_postgis_schema(layer_key, [cfg["pg_schema_overlay"], cfg["pg_schema_business"]])
        store = _postgis_store_name(schema)
        ensure_postgis_store(store, schema)
        _publish_postgis_featuretype(store, layer_key, layer_key, srs="EPSG:4326")
        published = layer_key
    else:
        raise GeoServerUnavailable(f"未识别的 TileManager 图层类型: key={layer_key} type={type_}")

    style_result = None
    if type_ != "drone" and isinstance(style, dict) and style:
        style_result = apply_layer_style(published, style)

    result = {
        "layer_name": published,
        "qualified_name": f"{ws}:{published}",
        "workspace": ws,
        "urls": layer_urls(published),
        "style": style_result,
    }
    result.update(publish_extra)
    return result


def unpublish_layer(layer_name: str, *, recurse: bool = True) -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    params = {"recurse": "true" if recurse else "false"}
    resp = _request(
        "DELETE",
        f"/rest/layers/{ws}:{layer_name}",
        params=params,
        timeout=15,
    )
    if resp.status_code in (200, 202):
        return {"removed": True}
    if resp.status_code == 404:
        return {"removed": False, "reason": "not found"}
    raise GeoServerUnavailable(f"unpublish layer failed: HTTP {resp.status_code} {resp.text[:200]}")


# ---------------------------------------------------------------------------
# GeoWebCache seed / truncate
# ---------------------------------------------------------------------------

def _gwc_seed_payload(layer_name: str, *, bounds: Optional[List[float]], min_zoom: int,
                      max_zoom: int, fmt: str, threads: int, action: str = "seed") -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    qname = f"{ws}:{layer_name}"
    body: Dict[str, Any] = {
        "seedRequest": {
            "name": qname,
            "srs": {"number": 900913},
            "zoomStart": int(min_zoom),
            "zoomStop": int(max_zoom),
            "format": fmt,
            "type": action,  # seed | reseed | truncate
            "threadCount": int(threads),
        }
    }
    if bounds and len(bounds) == 4:
        body["seedRequest"]["bounds"] = {"coords": {"double": [float(v) for v in bounds]}}
    return body


def gwc_seed(layer_name: str, *, bounds: Optional[List[float]] = None, min_zoom: int = 0,
             max_zoom: int = 14, fmt: str = "image/png", threads: int = 1) -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    qname = f"{ws}:{layer_name}"
    payload = _gwc_seed_payload(layer_name, bounds=bounds, min_zoom=min_zoom,
                                max_zoom=max_zoom, fmt=fmt, threads=threads, action="seed")
    resp = _request(
        "POST",
        f"/gwc/rest/seed/{qname}.json",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code not in (200, 201, 202):
        raise GeoServerUnavailable(f"gwc seed failed: HTTP {resp.status_code} {resp.text[:200]}")
    return {"submitted": True, "layer": qname}


def gwc_truncate(layer_name: str, *, bounds: Optional[List[float]] = None, min_zoom: int = 0,
                 max_zoom: int = 22, fmt: str = "image/png") -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    qname = f"{ws}:{layer_name}"
    payload = _gwc_seed_payload(layer_name, bounds=bounds, min_zoom=min_zoom,
                                max_zoom=max_zoom, fmt=fmt, threads=1, action="truncate")
    resp = _request(
        "POST",
        f"/gwc/rest/seed/{qname}.json",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code not in (200, 201, 202):
        raise GeoServerUnavailable(f"gwc truncate failed: HTTP {resp.status_code} {resp.text[:200]}")
    return {"submitted": True, "layer": qname}


def gwc_seed_status(layer_name: str) -> Dict[str, Any]:
    cfg = get_config()
    ws = cfg["workspace"]
    qname = f"{ws}:{layer_name}"
    resp = _request("GET", f"/gwc/rest/seed/{qname}.json", timeout=10)
    if resp.status_code != 200:
        return {"available": False, "reason": f"HTTP {resp.status_code}"}
    data = resp.json() or {}
    # GeoWebCache 返回结构 {"long-array-array": [[tilesProcessed, tilesTotal, tilesRemaining, taskId, status], ...]}
    rows = (data.get("long-array-array") or [])
    tasks: List[Dict[str, Any]] = []
    status_map = {-1: "ABORTED", 0: "UNSET", 1: "READY", 2: "RUNNING", 3: "DONE"}
    for r in rows:
        if not r or len(r) < 5:
            continue
        processed, total, remaining, task_id, status = r[0], r[1], r[2], r[3], r[4]
        percent = 0
        if total and total > 0:
            percent = max(0, min(100, int(processed * 100 / total)))
        elif processed and (processed + remaining) > 0:
            percent = max(0, min(100, int(processed * 100 / (processed + remaining))))
        tasks.append({
            "task_id": task_id,
            "processed": processed,
            "total": total,
            "remaining": remaining,
            "status": status_map.get(int(status), str(status)),
            "percent": percent,
        })
    return {"available": True, "layer": qname, "tasks": tasks, "raw": data}
