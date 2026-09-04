"""
Falcon 目标识别检测脚本
通过 map_assistant_v1 前端传入 GeoJSON 区域和自然语言提示词，执行 Falcon-Perception
（tiiuae/Falcon-Perception，0.6B 早融合多模态 Transformer）推理，返回 GeoJSON 结果。

架构：本脚本负责影像获取（本地 TIF / ArcGIS 缓存瓦片拼接）、瓦片切分与 zoom 放大、
mask 线性权重融合、多边形提取与地理坐标转换；模型推理通过 HTTP 调用常驻服务
falcon_service.py（FALCON_SERVICE_URL，默认 http://127.0.0.1:8765）。

Falcon 天然支持自由文本 query：无需词表、无需注册类，中文目标经
resolve_query()（内置映射 + qwen-flash 翻译兜底）转为英文后直接查询。
"""
import os
import sys
import io
import json
import time
import tempfile
import re
import base64
import numpy as np
from pathlib import Path
from PIL import Image
from shapely.geometry import shape, mapping, Polygon
from shapely.ops import unary_union
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.transform import from_bounds as transform_from_bounds

LOCAL_TIF = "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/2023年高分影像.tif"

# 常驻 Falcon 推理服务地址（falcon_service.py）
FALCON_SERVICE_URL = os.environ.get("FALCON_SERVICE_URL", "http://127.0.0.1:8765").rstrip("/")
FALCON_DETECT_TIMEOUT = int(os.environ.get("FALCON_DETECT_TIMEOUT", "180"))

# 进度追踪：通过环境变量 FALCON_PROGRESS_FILE 传递进度文件路径
PROGRESS_FILE = os.environ.get("FALCON_PROGRESS_FILE", "")


def falcon_service_ready():
    """检查常驻 Falcon 推理服务是否可达（不阻塞等待模型加载完成）。"""
    try:
        import requests
        r = requests.get(f"{FALCON_SERVICE_URL}/health", timeout=5)
        return r.status_code == 200 and r.json().get("status") != "error"
    except Exception:
        return False


def falcon_detect_image(image, query, task="segmentation"):
    """调用常驻 Falcon 服务执行单图推理。

    image: PIL.Image 或本机图片绝对路径（服务与本脚本同机部署时优先传路径，
           省去 base64 编解码开销；跨机场景自动退化为 base64）。
    返回: (instances, processed_size) — instances 为实例列表
          [{mask_rle: {counts, size}, bbox_norm: {x,y,h,w}}]，
          RLE 为模型处理分辨率（16 的倍数、≤1024）。
    失败抛 RuntimeError，不静默返回空结果。
    """
    import requests
    payload = {"query": query, "task": task}
    if isinstance(image, str):
        payload["image_path"] = image
    else:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        payload["image_b64"] = base64.b64encode(buf.getvalue()).decode()

    last_err = None
    for attempt in (1, 2):  # 失败重试 1 次
        try:
            r = requests.post(f"{FALCON_SERVICE_URL}/detect", json=payload,
                              timeout=FALCON_DETECT_TIMEOUT)
            if r.status_code != 200:
                raise RuntimeError(f"Falcon 服务返回 {r.status_code}: {r.text[:200]}")
            data = r.json()
            return data.get("instances", []), data.get("processed_size")
        except Exception as e:
            last_err = e
            if attempt == 1:
                print(f"[FALCON] 服务调用失败（将重试 1 次）: {e}")
                time.sleep(1.0)
    raise RuntimeError(f"Falcon 服务调用失败: {last_err}")


def falcon_instances_to_mask(instances, out_h, out_w):
    """将 /detect 返回的实例 RLE 解码、缩放到目标尺寸并 union 成二值 mask。

    RLE 的 size 是模型处理分辨率，与本脚本的瓦片/裁剪尺寸不一致，
    用 NEAREST 缩放保持二值边界。
    """
    import cv2
    from pycocotools import mask as mask_utils

    mask = np.zeros((out_h, out_w), dtype=np.uint8)
    for inst in instances:
        rle = inst.get("mask_rle") if isinstance(inst, dict) else None
        if not rle or not rle.get("size"):
            continue
        counts = rle.get("counts")
        if isinstance(counts, str):
            counts = counts.encode("utf-8")
        try:
            m = mask_utils.decode({"counts": counts, "size": rle["size"]})
        except Exception as e:
            print(f"[FALCON] RLE 解码失败（跳过该实例）: {e}")
            continue
        if m.shape != (out_h, out_w):
            m = cv2.resize(m, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
        mask |= (m > 0).astype(np.uint8)
    return mask


# ── 查询词解析：Falcon 天然自由文本，中文目标统一转英文（英文 query 精度最佳） ──
QUERY_MAP = {
    "水体": "water body", "水": "water", "河流": "river", "河道": "river channel",
    "湖泊": "lake", "坑塘": "pond", "池塘": "pond",
    "建筑": "building", "建筑物": "building", "房子": "house", "房屋": "house",
    "屋顶": "rooftop", "厂房": "factory building", "仓库": "warehouse",
    "蓝顶厂房": "blue roof", "蓝色屋顶": "blue roof", "蓝屋顶": "blue roof",
    "彩钢瓦厂房": "factory building", "钢结构厂房": "factory building",
    "工业建筑": "industrial building", "车间": "industrial building", "工业园": "industrial park",
    "道路": "road", "公路": "highway", "马路": "road", "路": "road",
    "车辆": "vehicle", "汽车": "car", "车": "vehicle",
    "树木": "tree", "树": "tree", "森林": "forest", "林地": "forest",
    "草地": "grassland", "植被": "vegetation",
    "耕地": "cropland", "农田": "farmland", "田地": "farmland",
    "裸地": "bareland", "裸土": "bare soil", "荒地": "barren land",
    "桥梁": "bridge", "烟囱": "chimney", "大棚": "greenhouse",
    "采砂船": "sand dredging vessel", "挖砂船": "sand dredging vessel",
    "砂场": "sand yard", "采砂场": "sand mining site",
    "堆砂": "sand pile", "砂堆": "sand pile",
}

_query_cache = {}


def _translate_via_qwen(text):
    """qwen-flash 兜底：中文检测目标 → 面向开放词汇分割模型的英文 query。

    返回候选列表 [主候选, 备选1, 备选2]，主候选失效时可自动降级重试。
    """
    import dashscope
    resp = dashscope.Generation.call(
        model=os.environ.get("FALCON_TRANSLATE_MODEL", "qwen-flash"),
        api_key=os.environ.get("DASHSCOPE_API_KEY"),
        messages=[{"role": "user",
                   "content": "你是遥感目标检测助手。开放词汇分割模型接受简洁的英文名词短语作为查询。\n"
                              f"将检测目标「{text}」转换为英文查询短语，输出 3 个候选，用分号分隔：\n"
                              "- 第 1 个是最优表达，后面 2 个是同义或更通用的类别词\n"
                              "- 使用常见英文类别词（building/factory/roof/water/road/tree 等），可带颜色、材质属性修饰\n"
                              "- 每个短语不超过 4 个单词；只输出短语本身，不要解释、编号和标点（分号除外）\n"
                              "示例：\n蓝顶厂房 → blue-roofed building; blue roof; factory building\n"
                              "水体 → water body; water surface; river\n"
                              "大棚 → plastic greenhouse; agricultural greenhouse; greenhouse"}],
        result_format="message",
    )
    if resp.status_code == 200:
        out = resp.output.choices[0].message.content.strip()
        parts = [re.sub(r"[^A-Za-z ]", "", p).strip().lower()
                 for p in re.split(r"[;；、\n]", out)]
        seen, candidates = set(), []
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                candidates.append(p)
        if candidates:
            return candidates[:3]
    raise RuntimeError(f"qwen-flash 翻译失败: {getattr(resp, 'message', resp)}")


# 映射命中时的同义备选（主 query 0 结果时自动降级重试，均为实测有效表达）
GENERAL_FALLBACKS = {
    "blue roof": ["factory building", "building"],
    "factory building": ["industrial building", "building"],
    "industrial building": ["factory building", "building"],
    "water body": ["water", "river"],
    "building": ["house", "industrial building"],
    "vehicle": ["car", "truck"],
}


def resolve_query_variants(prompt):
    """检测目标 → (主英文 query, 备选列表)。

    1) 已是英文 → 原样使用（无备选）
    2) 精确命中内置映射 → 直接返回，附同义备选
    3) 其余 → qwen-flash 生成 3 候选，失败原样透传（Falcon 对部分中文也有泛化）
    """
    q = (prompt or "").strip()
    if not q or re.fullmatch(r"[A-Za-z0-9 ,\-_/']+", q):
        return q, []
    if q in _query_cache:
        return _query_cache[q]
    if q in QUERY_MAP:
        primary = QUERY_MAP[q]
        variants = (primary, list(GENERAL_FALLBACKS.get(primary, [])))
        _query_cache[q] = variants
        return variants
    try:
        cands = _translate_via_qwen(q)
        print(f"[FALCON] 查询生成: 「{q}」→ 主 '{cands[0]}'，备选 {cands[1:]}")
        variants = (cands[0], cands[1:])
        _query_cache[q] = variants
        return variants
    except Exception as e:
        print(f"[FALCON] 查询生成失败（原样透传）: {e}")
        return q, []


def resolve_query(prompt):
    """兼容接口：仅返回主英文 query。"""
    return resolve_query_variants(prompt)[0]


def falcon_detect_with_fallback(image, variants, task="segmentation", log_prefix="[FALCON]"):
    """按候选顺序推理，返回首个非空实例结果；全部为空则返回最后候选的（空）结果。

    variants: (primary_query, [fallback_queries]) 或单个 query 字符串。
    Falcon 单帧推理仅 ~2.5s，备选重试的额外成本可控。
    """
    primary, fallbacks = variants if isinstance(variants, tuple) else (variants, [])
    candidates = [primary] + [f for f in fallbacks if f and f != primary]
    last = ([], None)
    for i, q in enumerate(candidates):
        instances, size = falcon_detect_image(image, q, task=task)
        if instances:
            if i > 0:
                print(f"{log_prefix} 备选 query 生效: '{q}' → {len(instances)} 实例")
            return instances, size
        last = (instances, size)
        if i < len(candidates) - 1:
            print(f"{log_prefix} query '{q}' 0 实例，尝试备选 '{candidates[i + 1]}'")
    return last




def _write_progress(stage, current=0, total=0, message=""):
    """写进度到文件，供后端 API 查询"""
    if not PROGRESS_FILE:
        return
    import json as _j
    data = {"stage": stage, "current": current, "total": total, "message": message}
    try:
        with open(PROGRESS_FILE, 'w') as f:
            _j.dump(data, f)
        # 同时打印到 stdout（保持原有日志输出）
        print(f"[PROGRESS] {stage} {current}/{total} {message}".strip())
    except Exception:
        pass


def crop_image_from_local_tif(bounds, output_path, target_size=1024, fast_mode=False):
    """
    从本地 GeoTIFF 裁剪指定范围的影像并转为 RGB PNG
    bounds: (minx, miny, maxx, maxy) in EPSG:4326
    target_size=None 时保留原始分辨率（用于瓦片分割+放大策略）
    fast_mode=True: 使用 min-max 拉伸 + BILINEAR 缩放，显著加速
    返回: (output_path, actual_bounds_4326)
    """
    minx, miny, maxx, maxy = bounds

    with rasterio.open(LOCAL_TIF) as src:
        src_crs = src.crs
        print(f"[FALCON] 本地 TIF: CRS={src_crs}, Size={src.width}x{src.height}, Bands={src.count}")

        # 如果 TIF 不是 EPSG:4326，需要将请求的 bounds 转换到 TIF 坐标系
        if src_crs and str(src_crs) != 'EPSG:4326':
            from pyproj import Transformer
            transformer = Transformer.from_crs('EPSG:4326', src_crs, always_xy=True)
            t_minx, t_miny = transformer.transform(minx, miny)
            t_maxx, t_maxy = transformer.transform(maxx, maxy)
        else:
            t_minx, t_miny, t_maxx, t_maxy = minx, miny, maxx, maxy

        # 计算裁剪窗口
        window = from_bounds(t_minx, t_miny, t_maxx, t_maxy, src.transform)
        window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))

        if window.width < 1 or window.height < 1:
            raise ValueError(f"绘制区域超出影像范围。影像范围: {src.bounds}")

        print(f"[FALCON] 裁剪窗口: col={window.col_off:.0f}, row={window.row_off:.0f}, "
              f"w={window.width:.0f}, h={window.height:.0f}")

        band_count = src.count
        if band_count >= 3:
            data = src.read([1, 2, 3], window=window)
        elif band_count == 1:
            band = src.read(1, window=window)
            data = np.stack([band, band, band])
        else:
            data = src.read(window=window)[:3]

        win_transform = src.window_transform(window)
        actual_bounds_src = rasterio.windows.bounds(window, src.transform)

        if src_crs and str(src_crs) != 'EPSG:4326':
            transformer_inv = Transformer.from_crs(src_crs, 'EPSG:4326', always_xy=True)
            ab_minx, ab_miny = transformer_inv.transform(actual_bounds_src[0], actual_bounds_src[1])
            ab_maxx, ab_maxy = transformer_inv.transform(actual_bounds_src[2], actual_bounds_src[3])
            actual_bounds_4326 = (ab_minx, ab_miny, ab_maxx, ab_maxy)
        else:
            actual_bounds_4326 = actual_bounds_src

    # 线性拉伸 → uint8（快速模式用 min-max，省去 percentile 计算）
    rgb = np.zeros((3, data.shape[1], data.shape[2]), dtype=np.uint8)
    if fast_mode:
        for i in range(3):
            band = data[i].astype(np.float64)
            bmin, bmax = band.min(), band.max()
            if bmax > bmin:
                band = np.clip((band - bmin) / (bmax - bmin) * 255, 0, 255)
            rgb[i] = band.astype(np.uint8)
    else:
        for i in range(3):
            band = data[i].astype(np.float64)
            valid = band[band > 0]
            if len(valid) > 0:
                p2, p98 = np.percentile(valid, [2, 98])
                if p98 > p2:
                    band = np.clip((band - p2) / (p98 - p2) * 255, 0, 255)
                else:
                    band = np.clip(band / max(band.max(), 1) * 255, 0, 255)
            rgb[i] = band.astype(np.uint8)

    # (C, H, W) → (H, W, C)
    rgb_hwc = np.transpose(rgb, (1, 2, 0))

    img = Image.fromarray(rgb_hwc)
    orig_w, orig_h = img.size

    # target_size=None: 高分辨率模式（保留原分辨率用于瓦片分割）
    if target_size is None:
        print(f"[FALCON] 高分辨率模式: {orig_w}x{orig_h} (原始分辨率)")
    else:
        scale = min(target_size / orig_w, target_size / orig_h)
        new_w, new_h = int(orig_w * scale), int(orig_h * scale)
        if new_w < 1: new_w = 1
        if new_h < 1: new_h = 1
        # 快速模式使用 BILINEAR 缩放（比 LANCZOS 快 2-3x）
        resize_filter = Image.BILINEAR if fast_mode else Image.LANCZOS
        img = img.resize((new_w, new_h), resize_filter)
        filter_name = 'BILINEAR' if fast_mode else 'LANCZOS'
        print(f"[FALCON] 缩放模式: {orig_w}x{orig_h} -> {new_w}x{new_h} (filter={filter_name})")

    img.save(output_path)
    print(f"[FALCON] 裁剪影像已保存: {output_path} ({img.size[0]}x{img.size[1]})")
    print(f"[FALCON] 实际地理范围(4326): {actual_bounds_4326}")
    return output_path, actual_bounds_4326


# ArcGIS 缓存影像服务（与前端底图同源，保证所见即所得）
ARCGIS_TILE_BASE = os.environ.get(
    "FALCON_IMAGERY_SERVICE",
    os.environ.get(
        "SAM_IMAGERY_SERVICE",
        "http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202308_cache/MapServer/tile",
    ),
)


def crop_image_from_arcgis_tiles(bounds, output_path, target_size=None, fast_mode=False):
    """
    兜底影像源：从 ArcGIS 缓存服务下载 Web-Mercator 瓦片并拼接为 RGB PNG。
    本地 TIF 缺失时使用，保证 Falcon 看到的影像与前端底图一致。
    bounds: (minx, miny, maxx, maxy) in EPSG:4326
    返回: (output_path, actual_bounds_4326) —— actual_bounds 即请求 bounds（像素级精确裁剪）
    """
    import math
    from concurrent.futures import ThreadPoolExecutor
    import urllib.request

    minx, miny, maxx, maxy = bounds
    if not (minx < maxx and miny < maxy):
        raise ValueError(f"无效范围: {bounds}")

    def lonlat_to_world_px(lon, lat, z):
        n = 256 * (2 ** z)
        px = (lon + 180.0) / 360.0 * n
        lat_r = math.radians(max(min(lat, 85.0511), -85.0511))
        py = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
        return px, py

    dlon = maxx - minx
    # 选层级：目标宽度 ≥2048px 的最低层级（上限 z19），服务缺失瓦片时向下降级
    z_pref = math.ceil(math.log2(max(2048.0 * 360.0 / (256.0 * dlon), 1.0)))
    z_pref = max(15, min(z_pref, 19))
    print(f"[FALCON][ArcGIS] 目标层级: z{z_pref} (范围 {dlon:.4f}°)")

    last_err = None
    for z in range(z_pref, 14, -1):
        n = 2 ** z
        px0, py0 = lonlat_to_world_px(minx, maxy, z)   # 左上
        px1, py1 = lonlat_to_world_px(maxx, miny, z)   # 右下
        c0, c1 = int(px0 // 256), int(px1 // 256)
        r0, r1 = int(py0 // 256), int(py1 // 256)
        cols, rows = c1 - c0 + 1, r1 - r0 + 1
        if cols * rows > 400:  # 防止区域过大拖垮下载
            continue
        tiles = [(c, r) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

        def fetch_bytes(cr):
            c, r = cr
            url = f"{ARCGIS_TILE_BASE}/{z}/{r}/{c}"
            for _ in range(3):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "FalconDetect/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = resp.read()
                    if resp.status == 200 and len(data) > 100:
                        return cr, data
                except Exception:
                    continue
            return cr, None

        _write_progress("cropping", 0, len(tiles), f"在线影像下载中 z{z}（{cols}x{rows} 瓦片）...")
        results = {}
        with ThreadPoolExecutor(max_workers=12) as pool:
            for cr, data in pool.map(fetch_bytes, tiles):
                results[cr] = data
        missing = sum(1 for v in results.values() if v is None)
        print(f"[FALCON][ArcGIS] z{z}: 下载 {len(tiles)} 瓦片, 缺失 {missing}")
        if len(tiles) and missing / len(tiles) > 0.3:
            last_err = f"z{z} 缺失率 {missing}/{len(tiles)}"
            continue  # 降级到更低层级

        canvas = Image.new("RGB", (cols * 256, rows * 256), (128, 128, 128))
        for (c, r), data in results.items():
            if data is None:
                continue
            tile_img = Image.open(io.BytesIO(data)).convert("RGB")
            canvas.paste(tile_img, ((c - c0) * 256, (r - r0) * 256))

        # 裁剪到精确 bbox 像素窗口
        x0, y0 = px0 - c0 * 256, py0 - r0 * 256
        x1, y1 = px1 - c0 * 256, py1 - r0 * 256
        crop_box = (max(int(x0), 0), max(int(y0), 0),
                    min(int(x1), canvas.width), min(int(y1), canvas.height))
        img = canvas.crop(crop_box)
        if img.width < 16 or img.height < 16:
            last_err = f"裁剪窗口过小 {img.size}"
            continue

        if target_size is not None:
            scale = min(target_size / img.width, target_size / img.height)
            img = img.resize((max(int(img.width * scale), 1), max(int(img.height * scale), 1)),
                             Image.BILINEAR if fast_mode else Image.LANCZOS)
            print(f"[FALCON][ArcGIS] 缩放: -> {img.size[0]}x{img.size[1]}")

        img.save(output_path)
        actual_bounds = (minx, miny, maxx, maxy)
        print(f"[FALCON][ArcGIS] 在线影像已保存: {output_path} ({img.size[0]}x{img.size[1]}), bounds={actual_bounds}")
        return output_path, actual_bounds

    raise ValueError(f"ArcGIS 影像下载失败（{last_err}），区域可能无影像覆盖: {bounds}")


def run_falcon_inference(image_path, text_prompt, output_dir):
    """
    Falcon 单帧推理（整图一次查询，适用于小图/demo 模式）。

    将影像与解析后的英文 query 送到常驻 Falcon 服务，解码返回的实例 RLE，
    union 成与原图等大的二值 mask。
    返回: (mask, (H, W)) — 二值 uint8 mask 与其形状
    """
    img = Image.open(image_path).convert('RGB')
    w, h = img.size

    variants = resolve_query_variants(text_prompt)
    print(f"[FALCON] 单帧推理: query='{variants[0]}' (image {w}x{h})")

    instances, _ = falcon_detect_with_fallback(image_path, variants, task="segmentation")
    mask = falcon_instances_to_mask(instances, h, w)

    mask_ratio = mask.sum() / mask.size if mask.size > 0 else 0
    print(f"[FALCON] 单帧结果: {len(instances)} 个实例, {mask.sum()}px "
          f"({mask_ratio*100:.1f}%)")
    return mask, (h, w)


def run_tile_based_inference(image_path, text_prompt, output_dir,
                              tile_size=512, zoom_factor=3, overlap=64):
    """
    瓦片分割 + 放大推理策略：
    1. 将高分辨率图像切成重叠的小瓦片
    2. 每个瓦片放大 zoom_factor 倍后送 Falcon 常驻服务推理
    3. 将所有瓦片的预测 mask 拼接回原始尺寸

    参数：
      tile_size: 瓦片像素大小（默认 512）
      zoom_factor: 放大倍数（默认 3x，放大后小目标更清晰可检测）
      overlap: 瓦片间重叠像素数（避免边缘伪影）

    返回: (merged_mask, (H, W)) — 与原图等大的二值 mask
    """
    import cv2

    full_img = Image.open(image_path).convert('RGB')
    full_W, full_H = full_img.size
    print(f"[FALCON-Tile] 原图尺寸: {full_W}x{full_H}")
    print(f"[FALCON-Tile] 瓦片参数: size={tile_size}, zoom={zoom_factor}x, overlap={overlap}")

    # 解析检测目标（内置映射 + qwen-flash 查询生成 + 备选自动重试）
    query_variants = resolve_query_variants(text_prompt)
    print(f"[FALCON-Tile] 检测目标: '{text_prompt}' → query='{query_variants[0]}'"
          + (f"，备选 {query_variants[1]}" if query_variants[1] else ""))

    # 服务预检：不可达立即报错，避免逐瓦片重试浪费时间
    if not falcon_service_ready():
        raise RuntimeError(
            f"Falcon 推理服务不可达: {FALCON_SERVICE_URL}。"
            f"请先启动服务（pm2 start falcon-service 或 python backend/tools/falcon_service.py）")

    # 计算瓦片网格（带步长和重叠）
    stride = tile_size - overlap
    n_cols = (full_W - overlap) // stride + 1
    n_rows = (full_H - overlap) // stride + 1
    total_tiles = n_cols * n_rows
    print(f"[FALCON-Tile] 网格: {n_rows}行 x {n_cols}列 = {total_tiles} 个瓦片")

    # 输出累加器（float 用于加权融合）和计数器
    accumulator = np.zeros((full_H, full_W), dtype=np.float64)
    counter = np.zeros((full_H, full_W), dtype=np.float64)

    processed = 0
    for row in range(n_rows):
        for col in range(n_cols):
            x_start = col * stride
            y_start = row * stride
            x_end = min(x_start + tile_size, full_W)
            y_end = min(y_start + tile_size, full_H)

            # 提取瓦片
            tile = full_img.crop((x_start, y_start, x_end, y_end))
            tw, th = tile.size

            # 放大瓦片！这是关键：让小目标在放大后变得可检测
            zoomed_w = min(tw * zoom_factor, 4096)
            zoomed_h = min(th * zoom_factor, 4096)
            zoomed_tile = tile.resize((zoomed_w, zoomed_h), Image.LANCZOS)

            # Falcon 推理（传 PIL 对象走 base64；服务返回 RLE 再解码 union；
            # 主 query 0 实例时自动用备选 query 重试该瓦片）
            try:
                instances, _ = falcon_detect_with_fallback(
                    zoomed_tile, query_variants, task="segmentation",
                    log_prefix="[FALCON-Tile]")
                tile_mask = falcon_instances_to_mask(instances, zoomed_h, zoomed_w).astype(np.float64)
            except Exception as e:
                print(f"[FALCON-Tile] 瓦片 ({row},{col}) 推理失败: {e}")
                raise

            # 缩放回原始瓦片大小
            tile_mask_resized = cv2.resize(
                tile_mask.astype(np.float32), (tw, th), interpolation=cv2.INTER_LINEAR
            )

            # 放入累加器（使用线性权重：中心区域权重更高，边缘渐变以消除拼接痕迹）
            weight_map = np.ones((th, tw), dtype=np.float64)
            if overlap > 0:
                for dy in range(overlap):
                    w = (dy + 1) / (overlap + 1)
                    if y_start + dy < full_H:
                        weight_map[dy, :] *= w
                    if y_end - 1 - dy >= 0:
                        weight_map[th - 1 - dy, :] *= w
                for dx in range(overlap):
                    w = (dx + 1) / (overlap + 1)
                    if x_start + dx < full_W:
                        weight_map[:, dx] *= w
                    if x_end - 1 - dx >= 0:
                        weight_map[:, tw - 1 - dx] *= w

            accumulator[y_start:y_end, x_start:x_end] += tile_mask_resized * weight_map
            counter[y_start:y_end, x_start:x_end] += weight_map

            processed += 1
            # 进度：推理阶段占 10%~85%（留余量给合并和后处理）
            pct = int(10 + (processed / total_tiles) * 75)
            _write_progress("inference", processed, total_tiles,
                            f"瓦片推理 {processed}/{total_tiles} ({pct}%)")
            if processed % 5 == 0 or processed == total_tiles:
                print(f"[FALCON-Tile] 进度: {processed}/{total_tiles}")

    counter[counter == 0] = 1e-6
    score = accumulator / counter

    # === 合并阈值 ===
    _threshold = float(os.environ.get("FALCON_MERGE_THRESHOLD", "0.35"))
    if _threshold <= 0:
        # 自适应：基于 score 分布的 p50 分位数，但上限不超过 0.8
        valid_scores = score[score > 0]
        if len(valid_scores) > 100:
            p50 = np.percentile(valid_scores, 50)
            _threshold = min(p50 * 0.7, 0.80)  # 取 p50 的 70%，最高 0.8
        else:
            _threshold = 0.35
    threshold = _threshold

    merged_mask = score > threshold
    merged_mask = merged_mask.astype(np.uint8)

    print(f"[FALCON-Tile] 瓦片合并完成，mask 尺寸: {full_H}x{full_W}, "
          f"threshold={threshold:.3f}, mask_ratio={merged_mask.sum() / merged_mask.size:.4f}")
    return merged_mask, (full_H, full_W)


def requery_main(image_path, coarse_geojson, text_prompt, img_bounds, output_dir=None):
    """
    Requery 精修推理：
    对粗检测结果中的每个几何体，计算像素外接矩形 → 裁剪区域 →
    Falcon 再推理 → 输出精修 GeoJSON。

    输入:
      image_path: 原始裁剪影像路径 (PNG)
      coarse_geojson: dict {"type": "FeatureCollection", "features": [...]}
         坐标系: EPSG:4326 地理坐标（与主 API 输出一致）
      text_prompt: 原始文本提示词
      img_bounds: (minx, miny, maxx, maxy) EPSG:4326 影像地理范围
      output_dir: 临时输出目录（可选）

    返回:
      精修后的 GeoJSON FeatureCollection (EPSG:4326)
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="falcon_requery_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    start_time = time.time()

    query = resolve_query(text_prompt)

    full_img = Image.open(image_path).convert('RGB')
    full_W, full_H = full_img.size  # pixel dimensions
    minx, miny, maxx, maxy = img_bounds

    # 地理 → 像素 仿射变换参数
    x_res = (maxx - minx) / full_W
    y_res = (maxy - miny) / full_H

    def geo_to_pixel(lon, lat):
        """EPSG:4326 地理坐标 → 图像像素坐标"""
        px = (lon - minx) / x_res
        py = full_H - (lat - miny) / y_res  # Y 轴反向
        return px, py

    def pixel_to_geo_coords(px, py):
        """像素坐标 → EPSG:4326"""
        lon = minx + px * x_res
        lat = maxy - py * y_res
        return [lon, lat]

    features = coarse_geojson.get("features", [])
    if not features:
        print("[Requery] 无粗检测特征，直接返回空结果")
        return {"type": "FeatureCollection", "features": []}

    print(f"[Requery] 粗检测特征数: {len(features)}, 影像: {full_W}x{full_H}, bounds={img_bounds}")

    refined_features = []
    processed = 0
    kept_original = 0

    for feat_idx, feat in enumerate(features):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords:
            refined_features.append(feat)  # 保留原始
            kept_original += 1
            continue

        # 展平坐标环（支持 Polygon 和 MultiPolygon）
        if geom.get("type") == "Polygon":
            rings = coords
        elif geom.get("type") == "MultiPolygon":
            rings = [ring for poly_coords in coords for ring in poly_coords]
        else:
            refined_features.append(feat)
            kept_original += 1
            continue

        all_px = []
        for ring in rings:
            for c in ring:
                px, py = geo_to_pixel(c[0], c[1])
                all_px.append((px, py))

        if len(all_px) < 3:
            refined_features.append(feat)
            kept_original += 1
            continue

        # a. 计算外接矩形（扩展 10% 边距）
        xs = [p[0] for p in all_px]
        ys = [p[1] for p in all_px]
        pminx, pmaxx = min(xs), max(xs)
        pminy, pmaxy = min(ys), max(ys)
        pw, ph = pmaxx - pminx, pmaxy - pminy

        pad_x = max(pw * 0.10, 10)  # 至少 10px 边距
        pad_y = max(ph * 0.10, 10)

        x1 = max(0, int(pminx - pad_x))
        y1 = max(0, int(pminy - pad_y))
        x2 = min(full_W, int(pmaxx + pad_x))
        y2 = min(full_H, int(pmaxy + pad_y))

        if x2 - x1 < 16 or y2 - y1 < 16:
            # 区域太小，保留原始
            refined_features.append(feat)
            kept_original += 1
            continue

        # b. 裁剪影像
        crop = full_img.crop((x1, y1, x2, y2))
        crop_w, crop_h = x2 - x1, y2 - y1

        # 保存裁剪区域
        crop_path = os.path.join(output_dir, f"requery_{feat_idx:04d}.png")
        crop.save(crop_path)

        # c. Falcon 推理（小裁剪区域单次查询）
        try:
            instances, _ = falcon_detect_with_fallback(crop_path, query_variants, task="segmentation")
            refined_mask = falcon_instances_to_mask(instances, crop_h, crop_w)
        except Exception as e:
            print(f"[Requery] 特征 {feat_idx} 推理失败: {e}")
            refined_features.append(feat)
            kept_original += 1
            continue

        # d. 提取精修多边形（像素坐标 → 地理坐标）
        refined_polygons_px = mask_to_polygons(refined_mask, min_area=20)
        refined_polygons_px = mask_to_polygons(refined_mask, min_area=50, epsilon_ratio=0.01)

        if not refined_polygons_px:
            # 精修无结果，保留原始粗检测
            refined_features.append(feat)
            kept_original += 1
            continue

        for poly_px in refined_polygons_px:
            # crop 像素 → 原图像素 → 地理
            geo_coords = []
            for px, py in poly_px:
                global_px = px + x1
                global_py = py + y1
                geo_coords.append(pixel_to_geo_coords(global_px, global_py))

            refined_features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [geo_coords]
                },
                "properties": {
                    **(feat.get("properties") or {}),
                    "source": "falcon_requery",
                    "refined": True,
                    "requery_confidence": round(float(refined_mask.sum()) / max(refined_mask.size, 1), 4),
                }
            })

        processed += 1
        if processed % 20 == 0 or processed == len(features):
            print(f"[Requery] 进度: {processed}/{len(features)}")

    elapsed = time.time() - start_time
    print(f"[Requery] 完成! 共 {len(features)} 个特征 "
          f"(精修 {processed}, 保留原始 {kept_original}), 耗时 {elapsed:.1f}s")

    return {"type": "FeatureCollection", "features": refined_features}



def mask_to_polygons(mask, min_area=100, epsilon_ratio=0.002):
    """
    将二值 mask 转换为多边形列表（像素坐标）

    参数:
      min_area: 最小像素面积，过滤细碎噪声
      epsilon_ratio: 多边形简化比例，越小越保留原始形状（建议 0.001~0.005）
    """
    import cv2

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polygons = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        # 简化多边形
        epsilon = max(1.0, epsilon_ratio * cv2.arcLength(contour, True))
        approx = cv2.approxPolyDP(contour, epsilon, True)
        poly = approx.reshape(-1, 2).tolist()
        if len(poly) >= 3:
            # 闭合多边形
            poly.append(poly[0])
            polygons.append(poly)

    return polygons


def pixel_to_geo(poly_pixels, img_bounds, img_shape):
    """
    将像素坐标转换为地理坐标 (EPSG:4326)
    poly_pixels: [[x1, y1], [x2, y2], ...]
    img_bounds: [minx, miny, maxx, maxy]
    img_shape: (H, W)
    """
    minx, miny, maxx, maxy = img_bounds
    H, W = img_shape
    
    # 像素到地理的仿射变换
    x_res = (maxx - minx) / W
    y_res = (maxy - miny) / H
    
    geo_coords = []
    for px, py in poly_pixels:
        geo_x = minx + px * x_res
        geo_y = maxy - py * y_res  # Y 轴反向
        geo_coords.append([geo_x, geo_y])
    
    return geo_coords


def choose_tile_params(img_w, img_h):
    """自适应瓦片尺寸：按图像大小选择。

    Falcon 0.6B bf16 显存占用仅 ~2GB，单瓦片前向激活小，
    原按共卡剩余显存收缩瓦片的 SAM3 逻辑简化为纯图像尺寸策略。
    """
    max_side = max(img_w, img_h)
    area = img_w * img_h

    if max_side <= 2000 and area <= 4_000_000:
        params = {"tile_size": 1024, "zoom_factor": 2, "overlap": 64}
    elif max_side <= 4000 and area <= 15_000_000:
        params = {"tile_size": 1536, "zoom_factor": 1, "overlap": 64}
    else:
        params = {"tile_size": 2048, "zoom_factor": 1, "overlap": 64}

    print(f"[FALCON] 瓦片参数: {params}")
    return params


def main(geometry, prompt, output_dir=None, use_tile_mode=True, fast_mode=False, quick_mode=False, demo_mode=False,
         custom_image_path=None, custom_bounds=None):
    """
    主推理流程（支持多种模式）

    demo_mode: 超快速演示模式（crop→1024px, 单次推理/API调用, 无后处理）
    fast_mode: 快速模式（降低分辨率 + 跳过验证 + 轻量后处理）
    quick_mode: 快速裁剪模式（低分辨率裁剪，色彩敏感目标）
    use_tile_mode: Falcon 瓦片分割推理模式
    custom_image_path: 若提供，跳过本地TIF裁剪，直接使用该影像文件
    custom_bounds: 配合 custom_image_path 使用，影像覆盖的地理范围 (minx,miny,maxx,maxy) EPSG:4326
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="falcon_detect_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    start_time = time.time()

    # 1. 解析 GeoJSON 获取边界
    geom = shape(geometry)
    bounds = geom.bounds

    print(f"[FALCON] 边界: {bounds}")
    print(f"[FALCON] 提示词: {prompt}")
    print(f"[FALCON] 模型: Falcon-Perception (tiiuae/Falcon-Perception)")
    mode_labels = []
    if demo_mode: mode_labels.append("DEMO")
    elif fast_mode: mode_labels.append("FAST")
    if quick_mode: mode_labels.append("QUICK")
    if use_tile_mode: mode_labels.append("TILE")
    print(f"[FALCON] 模式: {'+'.join(mode_labels) if mode_labels else '标准'}")
    _write_progress("init", 0, 1, f"解析区域边界完成，准备裁剪影像...")
    
    # 2. 获取影像
    if custom_image_path and os.path.exists(custom_image_path):
        # === 使用外部提供的影像（变化检测场景：各年份 ArcGIS 瓦片） ===
        img_path = custom_image_path
        if custom_bounds:
            bounds = tuple(custom_bounds)
        else:
            bounds = geom.bounds
        img_w, img_h = Image.open(img_path).size
        print(f"[FALCON] 使用自定义影像: {img_path} ({img_w}x{img_h}), bounds={bounds}")
        _write_progress("cropped", 0, 1, f"使用预下载影像（{img_w}x{img_h}），加载模型中...")
    else:
        # === 从本地 TIF 裁剪影像 ===
        img_path = os.path.join(output_dir, "input_image.png")
        try:
            # 确定裁剪分辨率
            if demo_mode:
                crop_size = 1024  # Demo: 限制到 1024px 大幅减少数据量
            elif use_tile_mode and not fast_mode:
                crop_size = None  # 标准瓦片模式: 保留原始分辨率
            elif quick_mode:
                crop_size = 1024  # Quick: 色彩检测不需要高分辨率
            else:
                crop_size = 1536  # Fast/标准单次: 适度缩小
            
            is_fast_crop = demo_mode or fast_mode or quick_mode
            img_path, actual_bounds = crop_image_from_local_tif(
                bounds, img_path, target_size=crop_size, fast_mode=is_fast_crop)
            bounds = actual_bounds
            img_w, img_h = Image.open(img_path).size
            _write_progress("cropped", 0, 1, f"影像裁剪完成（{img_w}x{img_h}），加载模型中...")
        except Exception as e:
            # 本地 TIF 缺失/裁剪失败 → 自动回退在线影像服务（与前端底图同源）
            print(f"[FALCON] 本地TIF裁剪失败: {e}，尝试在线影像兜底...")
            _write_progress("cropping", 0, 1, "本地影像缺失，改用在线影像服务...")
            try:
                img_path, actual_bounds = crop_image_from_arcgis_tiles(
                    bounds, img_path, target_size=crop_size, fast_mode=is_fast_crop)
                bounds = actual_bounds
                img_w, img_h = Image.open(img_path).size
                _write_progress("cropped", 0, 1, f"在线影像裁剪完成（{img_w}x{img_h}），加载模型中...")
            except Exception as e2:
                import traceback
                traceback.print_exc()
                _write_progress("error", 0, 1, f"影像获取失败: {e2}")
                return {"type": "FeatureCollection", "features": []}
    
    # 3. Falcon 推理
    try:
        _write_progress("inference_start", 0, 100, "开始目标识别推理...")
        if demo_mode:
            # Demo 模式: 单次推理，快速出结果
            mask, seg_shape = run_falcon_inference(img_path, prompt, output_dir)
        elif use_tile_mode:
            tile_params = choose_tile_params(img_w, img_h)
            if fast_mode:
                tile_params = {
                    "tile_size": max(tile_params["tile_size"], 768),
                    "zoom_factor": 1,
                    "overlap": 16,  # 快速模式减少重叠
                }
            print(f"[FALCON] 自适应参数: tile={tile_params['tile_size']}, zoom={tile_params['zoom_factor']}x, overlap={tile_params['overlap']}")
            mask, seg_shape = run_tile_based_inference(
                img_path, prompt, output_dir,
                tile_size=tile_params['tile_size'],
                zoom_factor=tile_params['zoom_factor'],
                overlap=tile_params['overlap']
            )
        else:
            mask, seg_shape = run_falcon_inference(img_path, prompt, output_dir)

        _write_progress("inference_done", 90, 100, "推理完成，正在生成多边形...")
    except Exception as e:
        print(f"[FALCON] 推理失败: {e}")
        _write_progress("error", 0, 1, f"推理失败: {e}")
        import traceback
        traceback.print_exc()
        return {"type": "FeatureCollection", "features": []}
    
    polygons_px = mask_to_polygons(mask, min_area=50)
    polygons_px = mask_to_polygons(mask, min_area=50, epsilon_ratio=0.01)
    print(f"[FALCON] 检测到 {len(polygons_px)} 个多边形")
    
    # 5. 像素坐标转地理坐标
    img = Image.open(img_path)
    img_w, img_h = img.size
    
    features = []
    for idx, poly_px in enumerate(polygons_px):
        geo_coords = pixel_to_geo(poly_px, bounds, (img_h, img_w))
        
        # 构建 GeoJSON 多边形
        if len(geo_coords) >= 4:  # 至少 4 个点（含闭合点）
            geo_poly = Polygon(geo_coords)
            if geo_poly.is_valid and geo_poly.area > 0:
                area_m2 = geo_poly.area * 111000 * 111000  # 粗略转平方米（仅适用于小范围）
                area_mu = area_m2 / 666.67
                
                features.append({
                    "type": "Feature",
                    "geometry": mapping(geo_poly),
                    "properties": {
                        "id": idx,
                        "prompt": prompt,
                        "area_m2": round(area_m2, 2),
                        "area_mu": round(area_mu, 3),
                    }
                })
    
    # 6. 保存 SHP（demo/fast 模式不保存）
    if features and os.environ.get("FALCON_WRITE_SHP", "0") == "1" and not demo_mode:
        import geopandas as gpd
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        shp_path = os.path.join(output_dir, "falcon_result.shp")
        gdf.to_file(shp_path)
        print(f"[FALCON] SHP 已保存: {shp_path}")
    
    elapsed = time.time() - start_time
    print(f"[FALCON] 完成！耗时 {elapsed:.1f}s，输出 {len(features)} 个图斑")
    _write_progress("done", 100, 100, f"完成！检测到 {len(features)} 个目标，耗时 {elapsed:.1f}s")
    
    return {"type": "FeatureCollection", "features": features}


def point_predict(points, labels, image_bounds=None, image_path=None, prompt="object"):
    """点提示分割（--point 模式）。

    注意：此函数的原始实现（约 200 行，基于旧 SAM3 模型的 geometric prompt）已删除。
    Falcon 仅支持文本 query（自由表达即可描述目标），点提示模式不再需要；
    文本提示识别（main）不依赖本函数。
    """
    return {
        "polygons": [],
        "message": "点提示模式(point_predict)已随 SAM3 下线。Falcon 使用文本 query 即可描述目标，请改用文本提示识别。",
    }


if __name__ == "__main__":
    # ── 点提示模式 ──
    if len(sys.argv) >= 2 and sys.argv[1] == "--point":
        # 从 stdin 读取 JSON: {"points": [...], "labels": [...], "image_bounds": {...}}
        input_data = json.loads(sys.stdin.read())
        result = point_predict(
            points=input_data.get('points', []),
            labels=input_data.get('labels', []),
            image_bounds=input_data.get('image_bounds'),
            image_path=input_data.get('image_path'),
            prompt=input_data.get('prompt', 'object'),
        )
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    if len(sys.argv) < 3:
        print("用法: python falcon_detect.py <geometry_json> <prompt> [--fast] [--quick] [--demo] [--requery]")
        print("  --point: 点提示模式（已下线，Falcon 使用文本 query）")
        print("  --requery: 从 stdin 读取粗检测 GeoJSON，执行精修推理")
        sys.exit(1)
    
    geometry = json.loads(sys.argv[1])
    prompt = sys.argv[2]

    if "--requery" in sys.argv[3:]:
        # ── Requery 精修模式 ──
        # stdin: 粗检测 GeoJSON FeatureCollection
        coarse_json = json.loads(sys.stdin.read())
        output_dir = tempfile.mkdtemp(prefix="falcon_requery_")
        # 从本地 TIF 裁剪影像（使用标准分辨率，不做缩放）
        img_path = os.path.join(output_dir, "requery_input.png")
        geom = shape(geometry)
        try:
            img_path, actual_bounds = crop_image_from_local_tif(
                geom.bounds, img_path, target_size=None)
        except Exception as e:
            print(f"[Requery] 本地TIF裁剪失败: {e}，尝试在线影像兜底...", file=sys.stderr)
            # 在线兜底失败时抛异常 → 子进程非 0 退出，由调用方报错
            img_path, actual_bounds = crop_image_from_arcgis_tiles(
                geom.bounds, img_path, target_size=None)
        result = requery_main(img_path, coarse_json, prompt, actual_bounds, output_dir)
    else:
        fast_mode = "--fast" in sys.argv[3:]
        quick_mode = "--quick" in sys.argv[3:]
        demo_mode = "--demo" in sys.argv[3:]
        # demo 比 fast 更激进，覆盖 fast
        result = main(geometry, prompt, use_tile_mode=not demo_mode,
                      fast_mode=fast_mode or demo_mode,
                      quick_mode=quick_mode,
                      demo_mode=demo_mode)
    print(json.dumps(result, ensure_ascii=False))
