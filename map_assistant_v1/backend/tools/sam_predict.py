"""
SAM 目标识别推理脚本
通过 map_assistant_v1 前端传入 GeoJSON 区域和提示词，执行 SAM 推理，返回 GeoJSON 结果。
使用 SegEarth-OV-3 的 SAM3 模型进行文本提示的语义分割。
支持从本地 GeoTIFF 文件裁剪影像（替代 ArcGIS 服务下载）。
"""
import os
import sys
import json
import time
import tempfile
import base64
import re
import urllib.request
import numpy as np
from pathlib import Path
from PIL import Image
from io import BytesIO
from shapely.geometry import shape, mapping, Polygon
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.transform import from_bounds as transform_from_bounds

# 添加 SegEarth-OV-3 到路径
SEG_DIR = Path(__file__).parent.parent.parent.parent / "seg" / "SegEarth-OV-3-main"
sys.path.insert(0, str(SEG_DIR))

# 切换工作目录（SAM3 模型使用相对路径加载资源）
os.chdir(str(SEG_DIR))

# 本地合并影像文件路径（固始县+商城县）
LOCAL_TIF = "/home/server/python/GIS/output/merged_output.tif"
SAM_BACKEND = os.environ.get("SAM_BACKEND", "ollama").strip().lower()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11433").rstrip("/")
OLLAMA_MODEL = os.environ.get("SAM_OLLAMA_MODEL", "gemma4:31b")
OLLAMA_TIMEOUT = int(os.environ.get("SAM_OLLAMA_TIMEOUT", "180"))
OLLAMA_IMAGE_MAX_SIDE = int(os.environ.get("SAM_OLLAMA_IMAGE_MAX_SIDE", "1280"))
OLLAMA_CLASSIFY_MAX_SIDE = int(os.environ.get("SAM_OLLAMA_CLASSIFY_MAX_SIDE", "640"))

# 进度追踪：通过环境变量 SAM_PROGRESS_FILE 传递进度文件路径
PROGRESS_FILE = os.environ.get("SAM_PROGRESS_FILE", "")


CLASS_ALIASES = [
    ("background",),
    ("bareland", "barren", "裸地", "裸土", "荒地"),
    ("grass", "草", "草地"),
    ("road", "道路", "公路", "马路", "路"),
    ("car", "车辆", "汽车", "小车", "车"),
    ("tree", "forest", "树", "树木", "树林", "森林"),
    ("water", "river", "水", "水体", "河流", "河道"),
    ("cropland", "农田", "耕地", "田地", "耕作区"),
    ("building", "roof", "house", "建筑", "建筑物", "房子", "房屋", "屋顶", "厂房", "仓库", "楼"),
]


def resolve_target_indices(text_prompt, default_indices=None):
    prompt_lower = (text_prompt or "").lower()
    target_indices = []
    for idx, aliases in enumerate(CLASS_ALIASES):
        if any(alias.lower() in prompt_lower or prompt_lower in alias.lower() for alias in aliases):
            target_indices.append(idx)
    if target_indices:
        return target_indices
    if default_indices is None:
        return []
    return list(default_indices)


def is_building_prompt(text_prompt):
    return 8 in resolve_target_indices(text_prompt)


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
        print(f"[SAM] 本地 TIF: CRS={src_crs}, Size={src.width}x{src.height}, Bands={src.count}")

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

        print(f"[SAM] 裁剪窗口: col={window.col_off:.0f}, row={window.row_off:.0f}, "
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
        print(f"[SAM] 高分辨率模式: {orig_w}x{orig_h} (原始分辨率)")
    else:
        scale = min(target_size / orig_w, target_size / orig_h)
        new_w, new_h = int(orig_w * scale), int(orig_h * scale)
        if new_w < 1: new_w = 1
        if new_h < 1: new_h = 1
        # 快速模式使用 BILINEAR 缩放（比 LANCZOS 快 2-3x）
        resize_filter = Image.BILINEAR if fast_mode else Image.LANCZOS
        img = img.resize((new_w, new_h), resize_filter)
        filter_name = 'BILINEAR' if fast_mode else 'LANCZOS'
        print(f"[SAM] 缩放模式: {orig_w}x{orig_h} -> {new_w}x{new_h} (filter={filter_name})")

    img.save(buf, format='BMP')
    with open(output_path, 'wb') as f:
        f.write(buf.getvalue())
    print(f"[SAM] 裁剪影像已保存: {output_path} ({img.size[0]}x{img.size[1]})")
    print(f"[SAM] 实际地理范围(4326): {actual_bounds_4326}")
    return output_path, actual_bounds_4326


def run_sam_inference(image_path, text_prompt, output_dir):
    """
    使用 SegEarthOV3 模型执行文本提示分割
    返回: 分割结果 numpy array (H, W)
    """
    import torch
    from torchvision import transforms
    
    from segearthov3_segmentor import SegEarthOV3Segmentation
    from mmengine.structures import BaseDataElement, PixelData

    class SegDataSample(BaseDataElement):
        pass

    target_indices = resolve_target_indices(text_prompt)

    _prob_thd = float(os.environ.get("SAM_PROB_THD", "0.18"))
    _conf_thd = float(os.environ.get("SAM_CONF_THD", "0.12"))

    model = SegEarthOV3Segmentation(
        type='SegEarthOV3Segmentation',
        model_type='SAM3',
        classname_path=os.path.join(str(SEG_DIR), 'configs', 'my_name.txt'),
        prob_thd=_prob_thd,
        confidence_threshold=_conf_thd,
        slide_stride=512,
        slide_crop=512,
    )
    
    img = Image.open(image_path).convert('RGB')
    img_tensor = transforms.Compose([transforms.ToTensor()])(img).unsqueeze(0).to(
        'cuda' if torch.cuda.is_available() else 'cpu'
    )
    
    data_sample = SegDataSample()
    data_sample.set_metainfo({
        'img_path': image_path,
        'ori_shape': img.size[::-1],
    })
    
    seg_result = model.predict(img_tensor, data_samples=[data_sample])
    seg_pred = seg_result[0].pred_sem_seg.data.cpu().numpy().squeeze(0)
    
    mask = np.zeros_like(seg_pred, dtype=np.uint8)
    for idx in target_indices:
        mask |= (seg_pred == idx).astype(np.uint8)
    
    return mask, seg_pred.shape


def run_tile_based_inference(image_path, text_prompt, output_dir,
                              tile_size=512, zoom_factor=3, overlap=64):
    """
    瓦片分割 + 放大推理策略：
    1. 将高分辨率图像切成重叠的小瓦片
    2. 每个瓦片放大 zoom_factor 倍后进行 SAM 推理
    3. 将所有瓦片的预测 mask 拼接回原始尺寸
    
    参数：
      tile_size: 瓦片像素大小（默认 512）
      zoom_factor: 放大倍数（默认 3x，放大后小目标更清晰可检测）
      overlap: 瓦片间重叠像素数（避免边缘伪影）
    
    返回: (merged_mask, (H, W)) — 与原图等大的二值 mask
    """
    import torch
    from torchvision import transforms
    from segearthov3_segmentor import SegEarthOV3Segmentation
    from mmengine.structures import BaseDataElement, PixelData
    import cv2

    class SegDataSample(BaseDataElement):
        pass

    target_indices = resolve_target_indices(text_prompt)

    # === 通用置信度参数（可通过环境变量覆盖） ===
    # prob_thd: 模型内部概率阈值，过低会产生大量噪声预测
    # confidence_threshold: 模型置信度过滤门槛
    _prob_thd = float(os.environ.get("SAM_PROB_THD", "0.18"))
    _conf_thd = float(os.environ.get("SAM_CONF_THD", "0.12"))

    # 加载模型（全局只加载一次）
    print(f"[SAM-Tile] 加载模型... prob_thd={_prob_thd}, conf_thd={_conf_thd}")
    model = SegEarthOV3Segmentation(
        type='SegEarthOV3Segmentation',
        model_type='SAM3',
        classname_path=os.path.join(str(SEG_DIR), 'configs', 'my_name.txt'),
        prob_thd=_prob_thd,
        confidence_threshold=_conf_thd,
        slide_stride=256,
        slide_crop=512,
    )

    # 打开原图
    full_img = Image.open(image_path).convert('RGB')
    full_W, full_H = full_img.size
    print(f"[SAM-Tile] 原图尺寸: {full_W}x{full_H}")
    print(f"[SAM-Tile] 瓦片参数: size={tile_size}, zoom={zoom_factor}x, overlap={overlap}")

    # 计算瓦片网格（带步长和重叠）
    stride = tile_size - overlap
    n_cols = (full_W - overlap) // stride + 1
    n_rows = (full_H - overlap) // stride + 1
    total_tiles = n_cols * n_rows
    print(f"[SAM-Tile] 网格: {n_rows}行 x {n_cols}列 = {total_tiles} 个瓦片")

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
            zoomed_w = min(tw * zoom_factor, 2048)
            zoomed_h = min(th * zoom_factor, 2048)
            zoomed_tile = tile.resize((zoomed_w, zoomed_h), Image.LANCZOS)

            # 保存瓦片到临时文件（模型 predict() 需要从文件路径加载）
            tile_tmp_path = os.path.join(output_dir, f'tile_{row}_{col}.png')
            zoomed_tile.save(buf, format='BMP')
            with open(tile_tmp_path, 'wb') as f:
                f.write(buf.getvalue())

            # 推理
            ds = SegDataSample()
            ds.set_metainfo({
                'img_path': tile_tmp_path,
                'ori_shape': (zoomed_h, zoomed_w),
            })
            result = model.predict(None, data_samples=[ds])
            pred = result[0].pred_sem_seg.data.cpu().numpy().squeeze(0)

            # 提取目标类别的 mask
            tile_mask = np.zeros_like(pred, dtype=np.float64)
            for idx in target_indices:
                tile_mask += (pred == idx).astype(np.float64)

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
                print(f"[SAM-Tile] 进度: {processed}/{total_tiles}")

    counter[counter == 0] = 1e-6
    score = accumulator / counter

    # === 通用自适应阈值（替代硬编码 0.22/0.35） ===
    # 策略：基于 score 分布的统计特性动态计算，同时保留环境变量覆盖能力
    _base_threshold = float(os.environ.get("SAM_MERGE_THRESHOLD", "0.0"))  # 0=自适应
    if _base_threshold > 0:
        threshold = _base_threshold
    else:
        # 自适应策略：取 score>0 区域的 p70 分位数作为基准
        valid_scores = score[score > 0]
        if len(valid_scores) > 100:
            p70 = np.percentile(valid_scores, 70)
            p50 = np.percentile(valid_scores, 50)
            # 动态范围：如果分布集中（p70-p50小），用更高门槛；否则适中
            dynamic_range = p70 - p50
            if dynamic_range < 0.1:
                # 分数集中 → 需要更高阈值来过滤噪声
                threshold = max(p70 + 0.10, 0.45)
            elif dynamic_range < 0.25:
                threshold = max(p70, 0.40)
            else:
                # 分布分散 → 目标和背景区分明显
                threshold = max(p60 := np.percentile(valid_scores, 60), 0.35)
        else:
            threshold = 0.45  # 几乎无有效预测，高门槛保守处理

    merged_mask = score > threshold
    merged_mask = merged_mask.astype(np.uint8)

    print(f"[SAM-Tile] 瓦片合并完成，mask 尺寸: {full_H}x{full_W}, "
          f"threshold={threshold:.3f}, mask_ratio={merged_mask.sum() / merged_mask.size:.4f}")
    return merged_mask, (full_H, full_W)


def general_post_filter(image_path, mask, score_map=None):
    """
    通用后处理过滤器 v3 — 完全自适应（无类别硬编码、无固定阈值）

    核心思想：不预设任何绝对值，而是基于当前检测批次内各区域的**相对特征分布**
    做统计决策。"好目标 vs 误检"的区分来自它们在同一张图中的表现差异。

    流程：
      Step 1: 提取所有候选区域的特征向量（面积/形状/光谱/纹理/边缘）
      Step 2: 对每维特征做归一化（z-score），得到综合质量分
      Step 3: 用 IQR / DBSCAN 思想识别离群点（低质量 = 误检）
              - 分布集中 → 全部保留（说明无法区分，宁可不过滤）
              - 明显分群 → 只保留高质量组
      Step 4: 输出过滤后的 mask
    """
    import cv2

    img = np.array(Image.open(image_path).convert("RGB"))
    if img.shape[:2] != mask.shape[:2]:
        print("[PostFilter] 图像尺寸不匹配，跳过后处理")
        return mask

    H, W = mask.shape
    image_area = H * W

    # === 仅保留纯物理约束（与类别无关的硬性底线）===
    _min_area_px = max(100, int(image_area * 0.00008))   # 最小面积：约 0.008%
    _max_area_px = int(image_area * 0.40)                  # 最大面积：不超过图面 40%

    # === 预计算图像特征通道 ===
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    v_channel = hsv[:, :, 2]
    gray_img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float64)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # ---- Step 1: 提取每个候选区域的特征向量 ----
    candidates = []  # list of dict
    for idx, contour in enumerate(contours):
        area = cv2.contourArea(contour)
        if area < _min_area_px or area > _max_area_px:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w < 8 or h < 8:
            continue

        perimeter = cv2.arcLength(contour, True)
        compactness = (4 * np.pi * area) / max(perimeter ** 2, 1)       # 圆形=1, 越小越不规则
        hull_area = max(cv2.contourArea(cv2.convexHull(contour)), 1)
        solidity = area / hull_area                                       # 实心度 0~1

        cmask = np.zeros((H, W), dtype=np.uint8)
        cv2.drawContours(cmask, [contour], -1, 1, thickness=-1)

        region_v = v_channel[cmask > 0]
        color_std = float(np.std(region_v)) if len(region_v) > 20 else 0   # 内部亮度一致性

        # 边缘对比度（边界环带灰度标准差）
        dilated = cv2.dilate(cmask, np.ones((5, 5), np.uint8), iterations=2)
        eroded = cv2.erode(cmask, np.ones((3, 3), np.uint8), iterations=1)
        ring_pixels = gray_img[(dilated - eroded) > 0]
        edge_contrast = float(np.std(ring_pixels)) if len(ring_pixels) > 20 else 0

        # 纹理复杂度（Sobel梯度均值）
        region_gray = gray_img * cmask
        px_count = (cmask > 0).sum()
        if px_count > 200:
            gx = cv2.Sobel(region_gray, cv2.CV_64F, 1, 0, ksize=3)
            gy = cv2.Sobel(region_gray, cv2.CV_64F, 0, 1, ksize=3)
            texture = float(np.sqrt(gx**2 + gy**2)[cmask > 0].mean())
        else:
            texture = 0

        # score_map 平均值
        mean_score = float(score_map[cmask > 0].mean()) if score_map is not None and (cmask > 0).sum() > 10 else 1.0

        candidates.append({
            "idx": idx,
            "contour": contour,
            "area": area,
            "compactness": compactness,
            "solidity": solidity,
            "color_std": color_std,
            "edge_contrast": edge_contrast,
            "texture": texture,
            "mean_score": mean_score,
        })

    n = len(candidates)
    if n == 0:
        print("[PostFilter] 无候选区域")
        return mask
    if n <= 2:
        # 检测结果太少，无法做统计判断，全部保留
        print(f"[PostFilter] 仅{n}个候选，跳过统计过滤")
        filtered = np.zeros_like(mask)
        for c in candidates:
            cv2.drawContours(filtered, [c["contour"]], -1, 1, thickness=-1)
        return filtered

    # ---- Step 2: 特征归一化 → 综合质量分 ----
    # 每个特征方向：高 = 好，低 = 差
    # 归一化到 [0, 1] 区间（当前批次内的 min-max）
    feats_compactness = np.array([c["compactness"] for c in candidates])
    feats_solidity    = np.array([c["solidity"] for c in candidates])
    feats_color_std   = np.array([c["color_std"] for c in candidates])     # 越低越好 → 取反
    feats_edge        = np.array([c["edge_contrast"] for c in candidates])
    feats_texture     = np.array([c["texture"] for c in candidates])       # 越低越好 → 取反
    feats_score       = np.array([c["mean_score"] for c in candidates])

    def normalize_higher_better(arr):
        """归一化：越大越好，映射到 [0, 1]"""
        lo, hi = arr.min(), arr.max()
        if hi - lo < 1e-9:
            return np.ones(n)
        return (arr - lo) / (hi - lo)

    def normalize_lower_better(arr):
        """归一化：越小越好（如噪声/纹理），映射到 [0, 1]"""
        lo, hi = arr.min(), arr.max()
        if hi - lo < 1e-9:
            return np.ones(n)
        return 1.0 - (arr - lo) / (hi - lo)

    scores = {}
    scores["shape"]    = normalize_higher_better(feats_compactness)   * 1.0   # 形状规整度
    scores["solid"]    = normalize_higher_better(feats_solidity)       * 1.0   # 实心度
    scores["uniform"]  = normalize_lower_better(feats_color_std)       * 1.2   # 颜色均匀性(加权)
    scores["edge"]     = normalize_higher_better(feats_edge)           * 0.8   # 边缘清晰度
    scores["smooth"]   = normalize_lower_better(feats_texture)         * 1.2   # 纹理平滑度(加权)
    scores["conf"]     = normalize_higher_better(feats_score)          * 1.0   # 模型置信度

    total_weight = sum(scores.values())
    quality = np.zeros(n)
    for s_arr in scores.values():
        quality += s_arr
    quality = quality / total_weight if total_weight > 0 else np.ones(n)

    # ---- Step 3: 自适应过滤决策 ----
    q25, q50, q75 = np.percentile(quality, [25, 50, 75])
    iqr = q75 - q25

    # 决策逻辑：
    #   如果 IQR 很小（分布集中，大家质量差不多）→ 全部保留，不做区分
    #   如果 IQR 较大（有明显好坏差异）→ 过滤掉低质量的
    if iqr < 0.15:
        # 分布太集中，无法可靠区分好坏，全部保留
        keep_mask = np.ones(n, dtype=bool)
        decision = f"分布集中(IQR={iqr:.3f})，全保留"
    elif n >= 4 and iqr > 0.30:
        # 分异明显，且样本足够多 → 用较激进的下界（q25 - 0.5*IQR）
        threshold = max(q25 - 0.5 * iqr, 0.10)
        keep_mask = quality >= threshold
        decision = f"分异大(IQR={iqr:.3f}), 阈值={threshold:.3f}"
    else:
        # 中等情况 → 用保守下界
        threshold = q25
        keep_mask = quality >= threshold
        decision = f"IQR={iqr:.3f}, 阈值=q25={threshold:.3f}"

    kept_idx = [i for i in range(n) if keep_mask[i]]
    rejected_idx = [i for i in range(n) if not keep_mask[i]]

    # ---- Step 4: 构建输出 ----
    filtered = np.zeros_like(mask)
    for i in kept_idx:
        cv2.drawContours(filtered, [candidates[i]["contour"]], -1, 1, thickness=-1)

    # 日志：展示每个被拒绝区域的原因（哪个维度拉低了总分）
    rej_details = []
    for i in rejected_idx:
        weak_dims = []
        dim_names = {"shape": "形状", "solid": "实心", "uniform": "色均", "edge": "边缘", "smooth": "纹理", "conf": "置信"}
        for dim_name, s_arr in scores.items():
            if s_arr[i] < 0.25:
                weak_dims.append(dim_names.get(dim_name, dim_name))
        rej_details.append(f"# {candidates[i]['idx']}({quality[i]:.2f}): {','.join(weak_dims) or '?'}")

    print(f"[PostFilter] {n}个候选 → 保留{len(kept_idx)}个 | "
          f"q25={q25:.2f}/q50={q50:.2f}/q75={q75:.2f} | 决策:{decision}")
    if rejected_idx:
        print(f"[PostFilter] 拒绝详情: {'; '.join(rej_details)}")

    filtered = cv2.morphologyEx(filtered, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    return filtered


def enhance_building_mask(image_path, mask):
    import cv2

    img = np.array(Image.open(image_path).convert("RGB"))
    if img.shape[:2] != mask.shape[:2]:
        return mask

    r = img[:, :, 0].astype(np.int16)
    g = img[:, :, 1].astype(np.int16)
    b = img[:, :, 2].astype(np.int16)
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    vegetation = (((2 * g - r - b) > 28) & (g > r + 8) & (g > b + 4)) | ((h >= 35) & (h <= 85) & (s > 45) & (v > 45))
    keep = np.zeros(mask.shape, dtype=np.uint8)
    image_area = mask.shape[0] * mask.shape[1]
    min_area = max(45, int(image_area * 0.000035))
    max_area = max(min_area * 4, int(image_area * 0.20))
    water_like = (h >= 85) & (h <= 132) & (s > 45) & (v < 120) & (b > r + 10)
    shadow_like = v < 38

    layers = [
        ((h >= 88) & (h <= 132) & (s > 24) & (v > 55) & (b > r + 5), 3, 9, 24, 0.14, 0.18),
        ((((h <= 18) | (h >= 165)) & (s > 24) & (v > 55) & (r > b + 3)), 3, 7, 20, 0.16, 0.20),
        ((v > 135) & (s < 105), 3, 5, 16, 0.20, 0.24),
        ((v > 70) & (v < 220) & (s < 85) & (np.abs(r - g) < 45) & (np.abs(g - b) < 45), 3, 5, 16, 0.20, 0.24),
        ((v >= 42) & (v < 135) & (s < 135), 3, 5, 14, 0.22, 0.26),
    ]

    kept_count = 0
    for layer_mask, open_size, close_size, max_aspect, min_fill, min_rect in layers:
        candidates = layer_mask & (~vegetation) & (~water_like) & (~shadow_like)
        candidates = candidates.astype(np.uint8) * 255
        candidates = cv2.morphologyEx(candidates, cv2.MORPH_OPEN, np.ones((open_size, open_size), np.uint8))
        candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, np.ones((close_size, close_size), np.uint8))
        contours, _ = cv2.findContours(candidates, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area or area > max_area:
                continue
            x, y, w, hgt = cv2.boundingRect(contour)
            if w < 6 or hgt < 6:
                continue
            aspect = max(w / max(hgt, 1), hgt / max(w, 1))
            if aspect > max_aspect:
                continue
            fill_ratio = area / max(w * hgt, 1)
            if fill_ratio < min_fill:
                continue
            rect = cv2.minAreaRect(contour)
            rw, rh = rect[1]
            rect_area = max(rw * rh, 1)
            rectangularity = area / rect_area
            if rectangularity < min_rect:
                continue
            if aspect > 10 and min(w, hgt) < 14:
                continue
            cv2.drawContours(keep, [contour], -1, 1, thickness=-1)
            kept_count += 1

    keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    enhanced = ((mask > 0) | (keep > 0)).astype(np.uint8)
    print(f"[SAM] 建筑物增强: model_pixels={int((mask > 0).sum())}, roof_pixels={int((keep > 0).sum())}, roof_parts={kept_count}, merged_pixels={int(enhanced.sum())}")
    return enhanced


def detect_building_candidates(image_path):
    empty = np.zeros(np.array(Image.open(image_path).convert("RGB")).shape[:2], dtype=np.uint8)
    return enhance_building_mask(image_path, empty)


def _extract_json_payload(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass
    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if first_obj >= 0 and last_obj > first_obj:
        try:
            return json.loads(text[first_obj:last_obj + 1])
        except Exception:
            pass
    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if first_arr >= 0 and last_arr > first_arr:
        try:
            return json.loads(text[first_arr:last_arr + 1])
        except Exception:
            pass
    return None


def _prepare_ollama_image(image_path, output_dir, max_side=None):
    if max_side is None:
        max_side = OLLAMA_IMAGE_MAX_SIDE
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    ollama_path = os.path.join(output_dir, f"ollama_input_{max_side}.jpg")
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=92)
    with open(ollama_path, 'wb') as f:
        f.write(buf.getvalue())
    with open(ollama_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    return image_b64, img.size, (w, h)


def _ollama_call(prompt_text, image_b64, temperature=0, num_ctx=2048, num_predict=200):
    """发送单次 Ollama API 请求（/api/chat + think:false），返回 response 文本"""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt_text, "images": [image_b64]}],
        "stream": False,
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    response_text = raw.get("message", {}).get("content", "")
    duration_s = round(raw.get("total_duration", 0) / 1e9, 2)
    print(f"[Ollama] response length={len(response_text)}, duration={duration_s}s")
    return response_text


def _ollama_classify(image_b64, text_prompt):
    """阶段1: 用 Gemma4 VLM 快速判断图中是否存在目标（缩小图，~0.5s）"""
    prompt = (f'Look at this satellite/aerial image carefully. '
              f'Does it contain any "{text_prompt}"? '
              f'Reply ONLY JSON: {{"has_target":true/false,"confidence":0.0-1.0,"count_estimate":N}}')
    text = _ollama_call(prompt, image_b64, temperature=0, num_ctx=1024, num_predict=30)
    parsed = _extract_json_payload(text)
    if not isinstance(parsed, dict):
        yes = any(w in text.lower() for w in ["yes", "true", "是"])
        print(f"[Ollama-Classify] parse failed, fallback yes={yes}, raw: {text[:100]}")
        return {"has_target": yes, "confidence": 0.6 if yes else 0.3, "count_estimate": 0}
    return parsed


def _ollama_detect_boxes(image_b64, text_prompt, resized_size):
    """阶段2: 让 Gemma4 返回 bbox（~1.5s）"""
    w, h = resized_size
    prompt = (f'You are a precise remote sensing object detector. '
              f'Find ONLY "{text_prompt}" in this {w}x{h} pixel satellite image. '
              f'Be very strict: only return objects that are clearly "{text_prompt}", not similar objects. '
              f'Return ONLY JSON: [{{"label":"name","bbox":[x1,y1,x2,y2],"confidence":0.9}}]. '
              f'If none found, return [].')
    text = _ollama_call(prompt, image_b64, temperature=0, num_ctx=2048, num_predict=500)
    parsed = _extract_json_payload(text)
    if isinstance(parsed, dict):
        return parsed.get("objects") or parsed.get("detections") or []
    elif isinstance(parsed, list):
        return parsed
    return []


def _ollama_classify_and_detect(image_b64, text_prompt, resized_size):
    """Demo模式: 一次 API 调用完成 分类+检测（~1.5-2s，省去 classify 单独调用的 ~0.5s）"""
    w, h = resized_size
    prompt = (f'You are a remote sensing analyst. First, check if "{text_prompt}" exists in this {w}x{h} satellite image. '
              f'If NONE found, return {{"has_target":false,"objects":[]}}. '
              f'If found, return ONLY JSON: '
              f'{{"has_target":true,"confidence":0.0-1.0,"count_estimate":N,'
              f'"objects":[{{"label":"name","bbox":[x1,y1,x2,y2],"confidence":0.9}}]}}. '
              f'Be strict: only return clearly identifiable "{text_prompt}".')
    text = _ollama_call(prompt, image_b64, temperature=0, num_ctx=2048, num_predict=500)
    parsed = _extract_json_payload(text)
    if isinstance(parsed, dict):
        has_target = parsed.get("has_target", True)
        if not has_target:
            return []
        return parsed.get("objects") or parsed.get("detections") or []
    elif isinstance(parsed, list):
        return parsed
    return []


def _ollama_verify_box(image_b64, text_prompt, bbox, resized_size):
    """阶段3: 裁剪 bbox 区域，让 VLM 二次确认是否为目标（~0.8s）"""
    x1, y1, x2, y2 = [int(v) for v in bbox]
    w, h = resized_size
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return False
    # 从原图 base64 解码，裁剪 bbox 区域，再编码
    import io
    img_data = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(img_data))
    crop = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    crop.save(buf, format='JPEG', quality=85)
    crop_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    verify_conf_thd = float(os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75"))
    prompt = (f'This crop was proposed as target "{text_prompt}". '
              f'Verify strictly whether the crop itself is truly the requested target. '
              f'If it is a different object, only visually similar, adjacent context, or uncertain, return false. '
              f'Return ONLY JSON: {{"is_target":true/false,"confidence":0.0-1.0}}')
    text = _ollama_call(prompt, crop_b64, temperature=0, num_ctx=1024, num_predict=80)
    answer = text.strip().lower()
    parsed = _extract_json_payload(text)
    if isinstance(parsed, dict):
        is_target = bool(parsed.get("is_target", parsed.get("target", parsed.get("yes", False))))
        try:
            confidence = float(parsed.get("confidence", parsed.get("score", 0.0)))
        except Exception:
            confidence = 0.0
    else:
        is_target = any(w in answer for w in ['yes', 'true', '是'])
        confidence = 0.65 if is_target else 0.0
    ok = is_target and confidence >= verify_conf_thd
    print(f"[Ollama-Verify] bbox=[{x1},{y1},{x2},{y2}] -> target={is_target}, confidence={confidence:.2f}, threshold={verify_conf_thd:.2f} -> {'KEEP' if ok else 'REJECT'}")
    return ok


def run_ollama_detection(image_path, text_prompt, output_dir, fast_mode=False, demo_mode=False):
    """
    VLM 目标检测流水线
    
    标准模式: 分类(小图) → 检测bbox(全尺寸) → 验证每个bbox (3次API调用)
    快速模式: 分类(小图) → 检测bbox(中尺寸) → 跳过验证 (2次API调用)
    Demo模式: 分类+检测一次性完成(中尺寸) → 跳过验证 (1次API调用)
    """
    mask = np.zeros((1, 1), dtype=np.uint8)  # 占位，后续根据实际图尺寸调整
    
    if demo_mode:
        # === Demo模式：单次 API 调用完成 分类+检测 ===
        demo_max_side = min(OLLAMA_IMAGE_MAX_SIDE, 1024)
        image_b64, resized_size, original_size = _prepare_ollama_image(
            image_path, output_dir, max_side=demo_max_side)
        print(f"[Ollama-Demo] 单次调用 {OLLAMA_HOST} model={OLLAMA_MODEL}, size={demo_max_side}px")
        mask = np.zeros((original_size[1], original_size[0]), dtype=np.uint8)
        
        _write_progress("ollama", 10, 100, f"Gemma4 视觉检测中 (demo)...")
        objects = _ollama_classify_and_detect(image_b64, text_prompt, resized_size)
        
        if not objects:
            if is_building_prompt(text_prompt):
                color_mask = detect_building_candidates(image_path)
                if color_mask.sum() > 0:
                    mask = color_mask
            _write_progress("ollama", 100, 100, f"VLM 未返回 bbox")
            return mask, mask.shape
        
        # 直接填充 bbox 到 mask（跳过验证）
        sx = original_size[0] / max(resized_size[0], 1)
        sy = original_size[1] / max(resized_size[1], 1)
        box_conf_thd = float(os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.60"))  # demo 稍降低阈值
        found = 0
        for item in objects:
            if not isinstance(item, dict):
                continue
            bbox = item.get("bbox") or item.get("box") or item.get("xyxy")
            if not isinstance(bbox, list) or len(bbox) < 4:
                continue
            try:
                item_conf = float(item.get("confidence", item.get("score", item.get("probability", 1.0))))
            except Exception:
                item_conf = 1.0
            if item_conf < box_conf_thd:
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
            except Exception:
                continue
            if max(x1, y1, x2, y2) <= 1.5:
                x1 *= resized_size[0]; x2 *= resized_size[0]
                y1 *= resized_size[1]; y2 *= resized_size[1]
            ox1, ox2 = sorted((int(round(x1 * sx)), int(round(x2 * sx))))
            oy1, oy2 = sorted((int(round(y1 * sy)), int(round(y2 * sy))))
            ox1 = max(0, min(mask.shape[1] - 1, ox1))
            ox2 = max(0, min(mask.shape[1], ox2))
            oy1 = max(0, min(mask.shape[0] - 1, oy1))
            oy2 = max(0, min(mask.shape[0], oy2))
            if ox2 - ox1 < 4 or oy2 - oy1 < 4:
                continue
            mask[oy1:oy2, ox1:ox2] = 1
            found += 1
        print(f"[Ollama-Demo] 检测到 {found} 个目标")
        _write_progress("ollama", 100, 100, f"检测完成: {found} 个目标")
        return mask, mask.shape
    
    # === 标准/快速模式 ===
    # 阶段1 用小图加速
    cls_max_side = min(OLLAMA_CLASSIFY_MAX_SIDE, 512) if fast_mode else OLLAMA_CLASSIFY_MAX_SIDE
    image_b64_cls, _, original_size = _prepare_ollama_image(image_path, output_dir, max_side=cls_max_side)
    print(f"[Ollama] 调用 {OLLAMA_HOST} model={OLLAMA_MODEL}, classify_size={cls_max_side}px")

    mask = np.zeros((original_size[1], original_size[0]), dtype=np.uint8)

    # === 阶段 1: VLM 分类（小图，~0.5s）===
    _write_progress("ollama", 10, 100, f"Gemma4 视觉分析中...")
    classification = _ollama_classify(image_b64_cls, text_prompt)
    has_target = classification.get("has_target", True)
    confidence = float(classification.get("confidence", 0.5))
    count_est = int(classification.get("count_estimate", 0))
    print(f"[Ollama-Classify] has_target={has_target}, confidence={confidence:.2f}, count~{count_est}")

    classify_conf_thd = float(os.environ.get("SAM_OLLAMA_CLASSIFY_CONF_THD", "0.45"))
    if not has_target or confidence < classify_conf_thd:
        print(f"[Ollama] VLM 判定无目标 (confidence={confidence:.2f}, threshold={classify_conf_thd:.2f})，跳过")
        _write_progress("ollama", 100, 100, f"未发现 {text_prompt}")
        return mask, mask.shape

    # 分类通过，加载图用于精确检测（快速模式用更小尺寸）
    detect_max_side = min(OLLAMA_IMAGE_MAX_SIDE, 1024) if fast_mode else OLLAMA_IMAGE_MAX_SIDE
    image_b64, resized_size, _ = _prepare_ollama_image(image_path, output_dir, max_side=detect_max_side)

    # === 阶段 2: VLM bbox 检测 ===
    _write_progress("ollama", 30, 100, "Gemma4 bbox 检测...")
    try:
        objects = _ollama_detect_boxes(image_b64, text_prompt, resized_size)
    except Exception as e:
        print(f"[Ollama] bbox 检测失败: {e}")
        objects = []

    if not objects:
        if is_building_prompt(text_prompt):
            color_mask = detect_building_candidates(image_path)
            if color_mask.sum() > 0:
                mask = color_mask
                print(f"[Ollama] bbox 无结果，回退色彩分割 pixels={int(mask.sum())}")
        _write_progress("ollama", 100, 100, f"VLM 未返回 bbox")
        return mask, mask.shape

    # 解析 bbox 并转换到原始图坐标
    sx = original_size[0] / max(resized_size[0], 1)
    sy = original_size[1] / max(resized_size[1], 1)
    raw_boxes = []
    box_conf_thd = float(os.environ.get("SAM_OLLAMA_BOX_CONF_THD", "0.65"))
    for item in objects:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox") or item.get("box") or item.get("xyxy")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        try:
            item_conf = float(item.get("confidence", item.get("score", item.get("probability", 1.0))))
        except Exception:
            item_conf = 1.0
        if item_conf < box_conf_thd:
            print(f"[Ollama] bbox confidence too low: {item_conf:.2f} < {box_conf_thd:.2f}")
            continue
        try:
            x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        except Exception:
            continue
        if max(x1, y1, x2, y2) <= 1.5:
            x1 *= resized_size[0]; x2 *= resized_size[0]
            y1 *= resized_size[1]; y2 *= resized_size[1]
        raw_boxes.append((x1, y1, x2, y2, item_conf, str(item.get("label", text_prompt))))

    print(f"[Ollama] 置信度过滤后 bbox {len(raw_boxes)} 个 (threshold={box_conf_thd:.2f})")

    # === 阶段 3: VLM 验证（快速模式跳过） ===
    if fast_mode:
        # 快速模式：跳过验证，直接使用所有 bbox
        verified = 0
        for bx1, by1, bx2, by2, item_conf, item_label in raw_boxes:
            ox1, ox2 = sorted((int(round(bx1 * sx)), int(round(bx2 * sx))))
            oy1, oy2 = sorted((int(round(by1 * sy)), int(round(by2 * sy))))
            ox1 = max(0, min(mask.shape[1] - 1, ox1))
            ox2 = max(0, min(mask.shape[1], ox2))
            oy1 = max(0, min(mask.shape[0] - 1, oy1))
            oy2 = max(0, min(mask.shape[0], oy2))
            if ox2 - ox1 < 4 or oy2 - oy1 < 4:
                continue
            mask[oy1:oy2, ox1:ox2] = 1
            verified += 1
        print(f"[Ollama-Fast] raw={len(raw_boxes)}, direct_fill={verified}, mask_pixels={int(mask.sum())}")
        _write_progress("ollama", 100, 100, f"快速检测完成: {verified} 个目标")
        return mask, mask.shape
    
    # === 标准模式：逐个验证 ===
    _write_progress("ollama", 50, 100, f"验证 {len(raw_boxes)} 个候选框...")
    verified = 0
    verify_boxes = os.environ.get("SAM_OLLAMA_VERIFY_BOXES", "1") == "1"
    auto_verify_thd = float(os.environ.get("SAM_OLLAMA_AUTO_VERIFY_THD", "0.90"))
    verify_conf_thd = float(os.environ.get("SAM_OLLAMA_VERIFY_CONF_THD", "0.75"))
    for i, (bx1, by1, bx2, by2, item_conf, item_label) in enumerate(raw_boxes):
        if verify_boxes and item_conf >= auto_verify_thd and item_conf >= verify_conf_thd:
            print(f"[Ollama] bbox conf={item_conf:.2f} >= auto_verify={auto_verify_thd:.2f} & verify={verify_conf_thd:.2f}, skip verify")
            ok = True
        elif verify_boxes:
            try:
                ok = _ollama_verify_box(image_b64, text_prompt,
                                        [bx1, by1, bx2, by2], resized_size)
            except Exception:
                ok = False
        else:
            ok = True
        if not ok:
            continue
        ox1, ox2 = sorted((int(round(bx1 * sx)), int(round(bx2 * sx))))
        oy1, oy2 = sorted((int(round(by1 * sy)), int(round(by2 * sy))))
        ox1 = max(0, min(mask.shape[1] - 1, ox1))
        ox2 = max(0, min(mask.shape[1], ox2))
        oy1 = max(0, min(mask.shape[0] - 1, oy1))
        oy2 = max(0, min(mask.shape[0], oy2))
        if ox2 - ox1 < 4 or oy2 - oy1 < 4:
            continue
        mask[oy1:oy2, ox1:ox2] = 1
        verified += 1
        pct = 50 + int((i + 1) / max(len(raw_boxes), 1) * 40)
        _write_progress("ollama", pct, 100, f"验证 {i+1}/{len(raw_boxes)}")

    print(f"[Ollama] raw={len(raw_boxes)}, verified={verified}, mask_pixels={int(mask.sum())}")
    _write_progress("ollama", 95, 100, f"检测完成: {verified} 个确认目标")
    return mask, mask.shape


def mask_to_polygons(mask, min_area=100):
    """
    将二值 mask 转换为多边形列表（像素坐标）
    """
    import cv2
    
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polygons = []
    
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        # 简化多边形
        epsilon = 0.005 * cv2.arcLength(contour, True)
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
    """根据裁剪图大小自适应推理参数，平衡速度与精度。"""
    max_side = max(img_w, img_h)
    area = img_w * img_h

    # 小图：保持较高精度
    if max_side <= 1400 and area <= 1_600_000:
        return {"tile_size": 512, "zoom_factor": 3, "overlap": 64}

    # 中图：降低放大倍数，减少瓦片数量
    if max_side <= 2400 and area <= 4_000_000:
        return {"tile_size": 640, "zoom_factor": 2, "overlap": 64}

    # 大图：优先速度
    return {"tile_size": 768, "zoom_factor": 1, "overlap": 48}


def main(geometry, prompt, output_dir=None, use_tile_mode=True, fast_mode=False, quick_mode=False, demo_mode=False,
         custom_image_path=None, custom_bounds=None):
    """
    主推理流程（支持多种模式）
    
    demo_mode: 超快速演示模式（crop→1024px, 单次推理/API调用, 无后处理）
    fast_mode: 快速模式（降低分辨率 + 跳过验证 + 轻量后处理）
    quick_mode: 建筑类目标跳过SAM，直接用色彩特征检测
    use_tile_mode: SAM 瓦片分割推理模式
    custom_image_path: 若提供，跳过本地TIF裁剪，直接使用该影像文件
    custom_bounds: 配合 custom_image_path 使用，影像覆盖的地理范围 (minx,miny,maxx,maxy) EPSG:4326
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="sam_detect_")
    else:
        os.makedirs(output_dir, exist_ok=True)
    
    start_time = time.time()
    
    # 1. 解析 GeoJSON 获取边界
    geom = shape(geometry)
    bounds = geom.bounds
    
    print(f"[SAM] 边界: {bounds}")
    print(f"[SAM] 提示词: {prompt}")
    print(f"[SAM] 后端: {SAM_BACKEND}")
    mode_labels = []
    if demo_mode: mode_labels.append("DEMO")
    elif fast_mode: mode_labels.append("FAST")
    if quick_mode: mode_labels.append("QUICK")
    if use_tile_mode: mode_labels.append("TILE")
    print(f"[SAM] 模式: {'+'.join(mode_labels) if mode_labels else '标准'}")
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
        print(f"[SAM] 使用自定义影像: {img_path} ({img_w}x{img_h}), bounds={bounds}")
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
            print(f"[SAM] 影像裁剪失败: {e}")
            _write_progress("error", 0, 1, f"裁剪失败: {e}")
            import traceback
            traceback.print_exc()
            return {"type": "FeatureCollection", "features": []}
    
    # 3. SAM 推理
    try:
        _write_progress("inference_start", 0, 100, "开始目标识别推理...")
        if SAM_BACKEND == "ollama":
            mask, seg_shape = run_ollama_detection(
                img_path, prompt, output_dir,
                fast_mode=fast_mode or demo_mode,
                demo_mode=demo_mode)
        elif quick_mode and is_building_prompt(prompt):
            mask = detect_building_candidates(img_path)
            seg_shape = mask.shape
        elif demo_mode:
            # Demo 模式 SAM: 单次推理（不切瓦片），快速出结果
            mask, seg_shape = run_sam_inference(img_path, prompt, output_dir)
        elif use_tile_mode:
            tile_params = choose_tile_params(img_w, img_h)
            if fast_mode:
                tile_params = {
                    "tile_size": max(tile_params["tile_size"], 768),
                    "zoom_factor": 1,
                    "overlap": 16,  # 快速模式减少重叠
                }
            print(f"[SAM] 自适应参数: tile={tile_params['tile_size']}, zoom={tile_params['zoom_factor']}x, overlap={tile_params['overlap']}")
            mask, seg_shape = run_tile_based_inference(
                img_path, prompt, output_dir,
                tile_size=tile_params['tile_size'],
                zoom_factor=tile_params['zoom_factor'],
                overlap=tile_params['overlap']
            )
        else:
            mask, seg_shape = run_sam_inference(img_path, prompt, output_dir)
        
        # 后处理：demo/fast 模式跳过昂贵的增强和过滤
        if not demo_mode and not fast_mode and is_building_prompt(prompt) and SAM_BACKEND != "ollama":
            mask = enhance_building_mask(img_path, mask)

        # === 通用后处理过滤（demo/fast 模式跳过） ===
        _enable_postfilter = os.environ.get("SAM_POSTFILTER", "1") == "1"
        if _enable_postfilter and SAM_BACKEND != "ollama" and not demo_mode and not fast_mode:
            mask_before = mask.sum()
            mask = general_post_filter(img_path, mask)
            mask_after = mask.sum()
            print(f"[SAM] 通用后处理: {mask_before} → {mask_after} 像素 "
                  f"({(1 - mask_after/max(mask_before,1))*100:.0f}% 过滤)")
        elif demo_mode or fast_mode:
            # 快速模式仅做最小面积过滤
            print(f"[SAM] {'Demo' if demo_mode else 'Fast'} 模式: 跳过后处理，仅保留最小面积过滤")

        _write_progress("inference_done", 90, 100, "推理完成，正在生成多边形...")
    except Exception as e:
        print(f"[SAM] 推理失败: {e}")
        _write_progress("error", 0, 1, f"推理失败: {e}")
        import traceback
        traceback.print_exc()
        return {"type": "FeatureCollection", "features": []}
    
    # 4. Mask 转多边形
    polygons_px = mask_to_polygons(mask, min_area=50)
    print(f"[SAM] 检测到 {len(polygons_px)} 个多边形")
    
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
    if features and os.environ.get("SAM_WRITE_SHP", "0") == "1" and not demo_mode:
        import geopandas as gpd
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        shp_path = os.path.join(output_dir, "sam_result.shp")
        gdf.to_file(shp_path)
        print(f"[SAM] SHP 已保存: {shp_path}")
    
    elapsed = time.time() - start_time
    print(f"[SAM] 完成！耗时 {elapsed:.1f}s，输出 {len(features)} 个图斑")
    _write_progress("done", 100, 100, f"完成！检测到 {len(features)} 个目标，耗时 {elapsed:.1f}s")
    
    return {"type": "FeatureCollection", "features": features}




# ============================================================
# 变化检测模块
# ============================================================

# 各年份 ArcGIS 瓦片服务 URL 配置
YEAR_TILE_URLS = {
    2023: "http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202308_cache/MapServer/tile/{z}/{y}/{x}",
    2024: "http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile/{z}/{y}/{x}",
    2025: "http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_202509_cache/MapServer/tile/{z}/{y}/{x}",
}


def _latlon_to_tile(lat, lon, zoom):
    """经纬度转瓦片坐标 (x, y)"""
    import math
    lat_rad = math.radians(lat)
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _tile_to_latlon(x, y, zoom):
    """瓦片坐标转经纬度（左上角）"""
    import math
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def fetch_tiles_for_bbox(bbox, year, output_dir, zoom=15):
    """
    从指定年份的 ArcGIS 瓦片服务下载瓦片并拼接为 GeoTIFF。
    bbox: (minx, miny, maxx, maxy) in EPSG:4326
    returns: path to warped GeoTIFF file (in EPSG:3857, as these tiles are Web Mercator)
    """
    import urllib.request as _ur
    import ssl

    tile_url_template = YEAR_TILE_URLS.get(year)
    if not tile_url_template:
        raise ValueError(f"不支持的年份: {year}，可选: {list(YEAR_TILE_URLS.keys())}")

    minx, miny, maxx, maxy = bbox
    # 计算瓦片范围
    x1, y2 = _latlon_to_tile(maxy, minx, zoom)  # 注意 lat/lon 对应关系
    x2, y1 = _latlon_to_tile(miny, maxx, zoom)

    # 限制瓦片数量避免过大
    # y2=north(小), y1=south(大), 所以 y1 >= y2
    tile_count = (x2 - x1 + 1) * (y1 - y2 + 1)
    if tile_count > 64:
        zoom = zoom - 1
        x1, y2 = _latlon_to_tile(maxy, minx, zoom)
        x2, y1 = _latlon_to_tile(miny, maxx, zoom)
        tile_count = (x2 - x1 + 1) * (y1 - y2 + 1)

    _write_progress("fetch", 0, tile_count, f"开始下载 {year} 年瓦片 (zoom={zoom}, tiles≈{tile_count})...")

    os.makedirs(output_dir, exist_ok=True)
    downloaded = 0
    tiles = []

    # 忽略 SSL 证书错误（ArcGIS 自签名证书）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # y2=north(小) → y1=south(大), 从北到南遍历
    for y in range(y2, y1 + 1):
        row_tiles = []
        for x in range(x1, x2 + 1):
            url = tile_url_template.format(z=zoom, y=y, x=x)
            tile_path = os.path.join(output_dir, f"tile_{year}_{zoom}_{x}_{y}.png")
            try:
                req = _ur.Request(url, headers={"User-Agent": "MapAssistant/1.0"})
                with _ur.urlopen(req, timeout=15, context=ctx) as resp:
                    data = resp.read()
                    if len(data) < 500:
                        # 空白瓦片，创建空白占位
                        blank = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
                        buf = BytesIO()
                        blank.save(buf, format='BMP')
                        with open(tile_path, 'wb') as f:
                            f.write(buf.getvalue())
                    else:
                        with open(tile_path, 'wb') as f:
                            f.write(data)
                row_tiles.append(tile_path)
                downloaded += 1
            except Exception as e:
                # 下载失败，创建空白瓦片
                blank = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
                buf = BytesIO()
                blank.save(buf, format='BMP')
                with open(tile_path, 'wb') as f:
                    f.write(buf.getvalue())
                row_tiles.append(tile_path)
            _write_progress("fetch", downloaded, tile_count,
                            f"下载 {year} 年瓦片 {downloaded}/{tile_count}")
        tiles.append(row_tiles)

    # 拼接瓦片
    _write_progress("fetch", tile_count, tile_count, f"正在拼接 {year} 年瓦片...")
    cols = len(tiles[0]) if tiles else 0
    rows = len(tiles)

    if cols == 0 or rows == 0:
        raise RuntimeError(
            f"{year} 年瓦片下载失败：未获取到任何有效瓦片。"
            f"区域可能不在 ArcGIS 瓦片服务覆盖范围内，或网络不可达。")

    tile_size = 256
    full_w = cols * tile_size
    full_h = rows * tile_size

    merged = Image.new('RGBA', (full_w, full_h))
    for row_idx, row in enumerate(tiles):
        for col_idx, tile_path in enumerate(row):
            try:
                tile_img = Image.open(tile_path)
                merged.paste(tile_img, (col_idx * tile_size, row_idx * tile_size))
            except Exception:
                pass

    # 检测是否所有瓦片都是空白的（下载全部失败）
    merged_arr = np.array(merged)
    # RGBA 全透明 (0,0,0,0) 的像素比例
    blank_ratio = (merged_arr.max(axis=2) == 0).sum() / max(merged_arr.shape[0] * merged_arr.shape[1], 1)
    if blank_ratio > 0.99:
        raise RuntimeError(
            f"{year} 年瓦片全部无效：{blank_ratio:.1%} 像素为空。"
            f"请确认该区域在 {year} 年 ArcGIS 瓦片服务覆盖范围内，且网络可达。")

    # 转为 RGB 并保存为 PNG
    merged_rgb = merged.convert('RGB')
    tif_path = os.path.join(output_dir, f"merged_{year}.png")
    # 兼容旧版 Pillow：使用 BytesIO 避免 _idat.fileno() 错误
    buf = BytesIO()
    merged_rgb.save(buf, format='BMP')
    with open(tif_path, 'wb') as f:
        f.write(buf.getvalue())

    # 计算地理范围（Web Mercator）
    # NW 角: tile (x1, y2) 的左上角 (y2=north 小, x1=west)
    lat_top, lon_left = _tile_to_latlon(x1, y2, zoom)
    # SE 角: tile (x2, y1) 的右下角 (y1=south 大, x2=east)
    lat_bottom, lon_right = _tile_to_latlon(x2 + 1, y1 + 1, zoom)

    # 写入 world file (PNGW)
    pgw_path = os.path.join(output_dir, f"merged_{year}.pgw")
    x_res = (lon_right - lon_left) / full_w
    y_res = (lat_bottom - lat_top) / full_h
    with open(pgw_path, 'w') as f:
        f.write(f"{x_res}\n0.0\n0.0\n{y_res}\n{lon_left}\n{lat_top}\n")

    print(f"[CHANGE] {year} 年瓦片拼接完成: {full_w}x{full_h}, 地理范围 ({lon_left},{lat_top})-({lon_right},{lat_bottom})")
    _write_progress("fetch", 100, 100, f"{year} 年瓦片获取完成")

    # 返回 bounds 按 pixel_to_geo 期望的 (minx, miny, maxx, maxy) 顺序
    return tif_path, (lon_left, lat_bottom, lon_right, lat_top)


def compare_detections(geojson_a, geojson_b, iou_threshold=0.10):
    """
    对比两份 SAM 检测结果，识别变化图斑。
    
    匹配策略（三层递进，容忍 SAM 边界抖动）：
    1. IoU >= 0.10 → 直接匹配（同一建筑，边界基本一致）
    2. 缓冲区 IoU >= 0.20 → 补充匹配（expand 5m 后仍重叠，边界漂移）
    3. 质心距离 <= 25m → 补充匹配（SAM 边界差异极大但中心点近，明确同一建筑）
    4. 剩余未匹配 → 候选变化（需前端人工确认）
    
    year_a 为基准年份（早），year_b 为对比年份（晚）。
    返回带 change_type 属性的 GeoJSON FeatureCollection。
    """
    from shapely.geometry import shape as _shapely_shape
    from pyproj import Geod

    features_a = geojson_a.get("features", [])
    features_b = geojson_b.get("features", [])

    if not features_a and not features_b:
        return {"type": "FeatureCollection", "features": []}

    # 地球测地线计算（WGS84）
    geod = Geod(ellps="WGS84")

    polys_a = []
    for f in features_a:
        geom = _shapely_shape(f["geometry"])
        if geom.is_valid and not geom.is_empty:
            polys_a.append((geom, f))

    polys_b = []
    for f in features_b:
        geom = _shapely_shape(f["geometry"])
        if geom.is_valid and not geom.is_empty:
            polys_b.append((geom, f))

    matched_b = set()
    matched_a = set()
    result_features = []

    # === 阶段1: IoU 严格匹配 ===
    for ai, (pa, fa) in enumerate(polys_a):
        best_bi = -1
        best_iou = 0
        for bi, (pb, fb) in enumerate(polys_b):
            if bi in matched_b:
                continue
            try:
                intersection = pa.intersection(pb).area
                union = pa.union(pb).area
                iou = intersection / union if union > 0 else 0
                if iou >= iou_threshold and iou > best_iou:
                    best_iou = iou
                    best_bi = bi
            except Exception:
                continue
        if best_bi >= 0:
            matched_a.add(ai)
            matched_b.add(best_bi)
            _pb, _fb = polys_b[best_bi]
            new_f = dict(fa)
            new_f["properties"] = dict(fa.get("properties", {}))
            new_f["properties"]["change_type"] = "unchanged"
            new_f["properties"]["match_method"] = "iou"
            new_f["properties"]["year_a"] = fa.get("properties", {}).get("year", "A")
            new_f["properties"]["year_b"] = _fb.get("properties", {}).get("year", "B")
            result_features.append(new_f)

    iou_matched = len(matched_a)

    # === 阶段2: 缓冲区 IoU 匹配（expand 5m，容忍边界漂移） ===
    # 在 EPSG:3857 下做 buffer（米为单位），避免 WGS84 度单位的 buffer 误差
    buffer_meters = 5.0  # 5米缓冲区
    buffered_iou_threshold = 0.20

    # 先将所有多边形转到 EPSG:3857 做 buffer
    from pyproj import Transformer
    to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
    to_wgs = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
    from shapely.ops import transform as _shapely_transform

    # 预计算 B 的缓冲区版本（在 EPSG:3857 下）
    buffered_b = []
    for bi, (pb, fb) in enumerate(polys_b):
        if bi in matched_b:
            buffered_b.append(None)
            continue
        try:
            pb_merc = _shapely_transform(to_merc, pb)
            pb_buffered_merc = pb_merc.buffer(buffer_meters)
            pb_buffered = _shapely_transform(to_wgs, pb_buffered_merc)
            buffered_b.append(pb_buffered)
        except Exception:
            buffered_b.append(None)

    for ai, (pa, fa) in enumerate(polys_a):
        if ai in matched_a:
            continue
        try:
            pa_merc = _shapely_transform(to_merc, pa)
            pa_buffered = _shapely_transform(to_wgs, pa_merc.buffer(buffer_meters))
        except Exception:
            continue
        best_bi = -1
        best_iou = 0
        for bi, pb_buffered in enumerate(buffered_b):
            if pb_buffered is None:
                continue
            try:
                # 用 A 的缓冲区与 B 原始多边形算 IoU
                intersection = pa_buffered.intersection(polys_b[bi][0]).area
                union = pa_buffered.union(polys_b[bi][0]).area
                iou = intersection / union if union > 0 else 0
                if iou >= buffered_iou_threshold and iou > best_iou:
                    best_iou = iou
                    best_bi = bi
            except Exception:
                continue
        if best_bi >= 0:
            matched_a.add(ai)
            matched_b.add(best_bi)
            _pb, _fb = polys_b[best_bi]
            new_f = dict(fa)
            new_f["properties"] = dict(fa.get("properties", {}))
            new_f["properties"]["change_type"] = "unchanged"
            new_f["properties"]["match_method"] = "buffer_iou"
            new_f["properties"]["year_a"] = fa.get("properties", {}).get("year", "A")
            new_f["properties"]["year_b"] = _fb.get("properties", {}).get("year", "B")
            result_features.append(new_f)

    buffer_matched = len(matched_a) - iou_matched

    # === 阶段3: 质心距离匹配（SAM 边界差异极大，但中心点近，明确同一建筑） ===
    centroid_dist_threshold = 25.0  # 米

    for ai, (pa, fa) in enumerate(polys_a):
        if ai in matched_a:
            continue
        ca = pa.centroid
        best_bi = -1
        best_dist = float('inf')
        for bi, (pb, fb) in enumerate(polys_b):
            if bi in matched_b:
                continue
            try:
                cb = pb.centroid
                _, _, dist = geod.inv(ca.x, ca.y, cb.x, cb.y)
                if dist < centroid_dist_threshold and dist < best_dist:
                    best_dist = dist
                    best_bi = bi
            except Exception:
                continue
        if best_bi >= 0:
            matched_a.add(ai)
            matched_b.add(best_bi)
            _pb, _fb = polys_b[best_bi]
            new_f = dict(fa)
            new_f["properties"] = dict(fa.get("properties", {}))
            new_f["properties"]["change_type"] = "unchanged"
            new_f["properties"]["match_method"] = "centroid"
            new_f["properties"]["year_a"] = fa.get("properties", {}).get("year", "A")
            new_f["properties"]["year_b"] = _fb.get("properties", {}).get("year", "B")
            result_features.append(new_f)

    centroid_matched = len(matched_a) - iou_matched - buffer_matched

    # === 阶段4: 未匹配的 → 候选变化 ===
    # year_a(早)有但year_b(晚)未匹配 → 可能在year_b消失 → removed
    for ai, (pa, fa) in enumerate(polys_a):
        if ai not in matched_a:
            new_f = dict(fa)
            new_f["properties"] = dict(fa.get("properties", {}))
            new_f["properties"]["change_type"] = "removed"
            new_f["properties"]["year"] = fa.get("properties", {}).get("year", "A")
            result_features.append(new_f)

    # year_b(晚)有但year_a(早)未匹配 → 可能在year_b新增 → added
    for bi, (pb, fb) in enumerate(polys_b):
        if bi not in matched_b:
            new_f = dict(fb)
            new_f["properties"] = dict(fb.get("properties", {}))
            new_f["properties"]["change_type"] = "added"
            new_f["properties"]["year"] = fb.get("properties", {}).get("year", "B")
            result_features.append(new_f)

    added = sum(1 for f in result_features if f["properties"]["change_type"] == "added")
    removed = sum(1 for f in result_features if f["properties"]["change_type"] == "removed")
    unchanged = sum(1 for f in result_features if f["properties"]["change_type"] == "unchanged")
    print(f"[CHANGE] 对比完成: 新增={added}, 消失={removed}, 无变化={unchanged} (IoU={iou_matched}, buffer={buffer_matched}, centroid={centroid_matched})")

    return {"type": "FeatureCollection", "features": result_features}


def _verify_changes_with_images(change_result, img_a_path, img_b_path, bbox_a, bbox_b):
    """
    对 compare_detections 输出的 added/removed 图斑进行图像级验证。
    对每个候选变化图斑，提取其在两幅影像中的像素区域并比较颜色差异：
    - 差异小 → SAM 在另一年份漏检（假变化），改标为 unchanged
    - 差异大 → 确认为真实变化，保留原标签
    
    bbox_a, bbox_b: 影像对应的地理范围 (minx, miny, maxx, maxy)
    """
    from shapely.geometry import shape as _shapely_shape
    from rasterio.features import rasterize
    import cv2

    img_a = np.array(Image.open(img_a_path).convert('RGB'), dtype=np.float32)
    img_b = np.array(Image.open(img_b_path).convert('RGB'), dtype=np.float32)

    ha, wa = img_a.shape[:2]
    hb, wb = img_b.shape[:2]

    # 影像地理范围
    a_minx, a_miny, a_maxx, a_maxy = bbox_a
    b_minx, b_miny, b_maxx, b_maxy = bbox_b

    # 像素分辨率
    a_res_x = (a_maxx - a_minx) / wa
    a_res_y = (a_maxy - a_miny) / ha
    b_res_x = (b_maxx - b_minx) / wb
    b_res_y = (b_maxy - b_miny) / hb

    def geo_to_pixel(lon, lat, minx, miny, maxx, maxy, res_x, res_y, w, h):
        """地理坐标 → 像素坐标 (miny=south, maxy=north, pixel row 0 = north)"""
        px = int((lon - minx) / res_x)
        py = int((maxy - lat) / res_y)  # maxy=north=row0, lat↓ → py↑
        return max(0, min(w - 1, px)), max(0, min(h - 1, py))

    features = change_result.get("features", [])
    verified_count = 0
    reclassified = 0

    for feat in features:
        ct = feat["properties"].get("change_type", "")
        if ct not in ("added", "removed"):
            continue

        geom = _shapely_shape(feat["geometry"])
        if not geom.is_valid or geom.is_empty:
            continue

        # 获取多边形边界框
        g_minx, g_miny, g_maxx, g_maxy = geom.bounds

        # 转到影像 A 的像素坐标
        ax1, ay1 = geo_to_pixel(g_minx, g_maxy, a_minx, a_miny, a_maxx, a_maxy, a_res_x, a_res_y, wa, ha)
        ax2, ay2 = geo_to_pixel(g_maxx, g_miny, a_minx, a_miny, a_maxx, a_maxy, a_res_x, a_res_y, wa, ha)
        ax1, ax2 = min(ax1, ax2), max(ax1, ax2)
        ay1, ay2 = min(ay1, ay2), max(ay1, ay2)

        # 转到影像 B 的像素坐标
        bx1, by1 = geo_to_pixel(g_minx, g_maxy, b_minx, b_miny, b_maxx, b_maxy, b_res_x, b_res_y, wb, hb)
        bx2, by2 = geo_to_pixel(g_maxx, g_miny, b_minx, b_miny, b_maxx, b_maxy, b_res_x, b_res_y, wb, hb)
        bx1, bx2 = min(bx1, bx2), max(bx1, bx2)
        by1, by2 = min(by1, by2), max(by1, by2)

        # 确保区域有效
        if ax2 <= ax1 or ay2 <= ay1 or bx2 <= bx1 or by2 <= by1:
            continue

        # 提取两个影像中的对应区域
        patch_a = img_a[ay1:ay2, ax1:ax2]
        patch_b = img_b[by1:by2, bx1:bx2]

        if patch_a.size == 0 or patch_b.size == 0:
            continue

        # 调整到相同尺寸
        if patch_a.shape != patch_b.shape:
            patch_b = cv2.resize(patch_b, (patch_a.shape[1], patch_a.shape[0]))

        # 计算平均绝对颜色差异 (MAD, 0-255)
        mad = np.abs(patch_a - patch_b).mean()

        verified_count += 1

        # 阈值判断：MAD < 30 → 像素很相似，SAM 漏检（假变化）
        if mad < 30:
            feat["properties"]["change_type"] = "unchanged"
            feat["properties"]["match_method"] = "image_verify"
            feat["properties"]["verify_mad"] = round(float(mad), 1)
            reclassified += 1
        else:
            feat["properties"]["verify_mad"] = round(float(mad), 1)

    if verified_count > 0:
        print(f"[CHANGE] 图像验证: 检查 {verified_count} 个候选变化, {reclassified} 个确认为假变化(无变化)")

    return change_result


def change_detect_main(geometry, prompt, year_a, year_b,
                       output_dir=None, fast_mode=False):
    """
    变化检测主流程：
    1. 分别下载两年份瓦片并合成影像
    2. 使用 OmniOVCD (SAM3) 或 Ollama VLM 进行双时相变化检测
    3. 输出变化图斑 GeoJSON
    """
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="sam_change_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    start_time = time.time()
    geom = shape(geometry)
    bounds = geom.bounds

    print(f"[CHANGE] 变化检测: {year_a} vs {year_b}, 区域: {bounds}, 提示词: {prompt}")
    print(f"[CHANGE] 后端: {SAM_BACKEND}")
    _write_progress("init", 0, 1, f"变化检测: {year_a} vs {year_b}")

    # === 阶段1: 获取两年份瓦片影像 ===
    _write_progress("fetch_A", 0, 100, f"正在获取 {year_a} 年瓦片...")
    use_local_tif_a = False
    try:
        img_a_path, bbox_a = fetch_tiles_for_bbox(bounds, year_a, output_dir)
    except Exception as e:
        print(f"[CHANGE] {year_a} 年瓦片获取失败: {e}，回退到本地 TIF")
        _write_progress("fetch_A_fallback", 0, 100, f"{year_a} 年瓦片不可用，使用本地影像")
        use_local_tif_a = True

    _write_progress("fetch_B", 0, 100, f"正在获取 {year_b} 年瓦片...")
    use_local_tif_b = False
    try:
        img_b_path, bbox_b = fetch_tiles_for_bbox(bounds, year_b, output_dir)
    except Exception as e:
        print(f"[CHANGE] {year_b} 年瓦片获取失败: {e}，回退到本地 TIF")
        _write_progress("fetch_B_fallback", 0, 100, f"{year_b} 年瓦片不可用，使用本地影像")
        use_local_tif_b = True

    # === 阶段2: 变化检测 ===
    # 优先级: OmniOVCD > Ollama VLM > fallback (旧流水线)
    use_omniovcd = (SAM_BACKEND in ("omniovcd", "sam3")
                    and not use_local_tif_a and not use_local_tif_b)

    if use_omniovcd:
        # === OmniOVCD 模式: SAM3 直接双时相推理 ===
        _write_progress("omniovcd", 0, 100, "OmniOVCD (SAM3) 变化检测中...")
        try:
            from omniovcd_inference import run_omniovcd_change_detect
            change_result = run_omniovcd_change_detect(
                img_a_path, img_b_path, prompt, bbox_a,
                year_a, year_b,
                output_dir=os.path.join(output_dir, "omniovcd")
            )
            elapsed = time.time() - start_time
            change_result["metadata"] = {
                "year_a": year_a,
                "year_b": year_b,
                "prompt": prompt,
                "elapsed_s": round(elapsed, 1),
                "method": "OmniOVCD",
                "features_change": len(change_result.get("features", [])),
            }
            print(f"[CHANGE] OmniOVCD 完成！耗时 {elapsed:.1f}s，"
                  f"输出 {len(change_result.get('features',[]))} 个变化图斑")
            _write_progress("done", 100, 100, f"OmniOVCD 变化检测完成！耗时 {elapsed:.1f}s")
            return change_result
        except ImportError as e:
            print(f"[CHANGE] OmniOVCD 模块导入失败: {e}，回退到 Ollama 模式")
        except Exception as e:
            print(f"[CHANGE] OmniOVCD 推理失败: {e}，回退到 Ollama 模式")
            import traceback
            traceback.print_exc()

    # === Ollama VLM 模式 (fallback): 原有流水线 ===
    _write_progress("detect_A", 0, 100, f"{year_a} 年检测中...")
    if use_local_tif_a:
        result_a = main(geometry, prompt, output_dir=os.path.join(output_dir, f"detect_{year_a}"),
                        use_tile_mode=not fast_mode, fast_mode=fast_mode)
    else:
        result_a = main(geometry, prompt, output_dir=os.path.join(output_dir, f"detect_{year_a}"),
                        use_tile_mode=not fast_mode, fast_mode=fast_mode,
                        custom_image_path=img_a_path, custom_bounds=bbox_a)
    for f in result_a.get("features", []):
        f["properties"]["year"] = year_a

    _write_progress("detect_B", 0, 100, f"{year_b} 年检测中...")
    if use_local_tif_b:
        result_b = main(geometry, prompt, output_dir=os.path.join(output_dir, f"detect_{year_b}"),
                        use_tile_mode=not fast_mode, fast_mode=fast_mode)
    else:
        result_b = main(geometry, prompt, output_dir=os.path.join(output_dir, f"detect_{year_b}"),
                        use_tile_mode=not fast_mode, fast_mode=fast_mode,
                        custom_image_path=img_b_path, custom_bounds=bbox_b)
    for f in result_b.get("features", []):
        f["properties"]["year"] = year_b

    # === 对比 ===
    _write_progress("compare", 0, 100, "正在对比检测结果...")
    change_result = compare_detections(result_a, result_b)

    # === 图像交叉验证 ===
    _write_progress("verify", 0, 100, "图像交叉验证中...")
    if not use_local_tif_a and not use_local_tif_b:
        try:
            change_result = _verify_changes_with_images(
                change_result, img_a_path, img_b_path, bbox_a, bbox_b)
        except Exception as e:
            print(f"[CHANGE] 图像交叉验证跳过: {e}")

    elapsed = time.time() - start_time
    change_result["metadata"] = {
        "year_a": year_a,
        "year_b": year_b,
        "prompt": prompt,
        "elapsed_s": round(elapsed, 1),
        "method": "Ollama-VLM",
        "features_a": len(result_a.get("features", [])),
        "features_b": len(result_b.get("features", [])),
        "features_change": len(change_result.get("features", [])),
    }

    # 添加回退警告
    warnings = []
    if use_local_tif_a:
        warnings.append(f"{year_a} 年 ArcGIS 瓦片不可用，已回退到本地 TIF 影像")
    if use_local_tif_b:
        warnings.append(f"{year_b} 年 ArcGIS 瓦片不可用，已回退到本地 TIF 影像")
    if use_local_tif_a and use_local_tif_b:
        warnings.append("两个年份均使用同源本地影像，变化检测结果可能不反映真实年份差异")
    elif use_local_tif_a or use_local_tif_b:
        warnings.append("一个年份使用了本地影像，变化检测精度可能降低")
    if warnings:
        change_result["warnings"] = warnings

    print(f"[CHANGE] 完成！耗时 {elapsed:.1f}s，输出 {len(change_result.get('features',[]))} 个变化图斑")
    _write_progress("done", 100, 100, f"变化检测完成！耗时 {elapsed:.1f}s")

    return change_result

if __name__ == "__main__":
    if "--change-detect" in sys.argv:
        # 变化检测模式: python sam_predict.py --change-detect <geometry> <prompt> <year_a> <year_b>
        idx = sys.argv.index("--change-detect")
        geometry = json.loads(sys.argv[idx + 1])
        prompt = sys.argv[idx + 2]
        year_a = int(sys.argv[idx + 3])
        year_b = int(sys.argv[idx + 4])
        fast_mode = "--fast" in sys.argv
        demo_mode = "--demo" in sys.argv
        result = change_detect_main(geometry, prompt, year_a, year_b, fast_mode=fast_mode or demo_mode)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    if len(sys.argv) < 3:
        print("用法: python sam_predict.py <geometry_json> <prompt> [--fast] [--quick] [--demo]")
        print("       python sam_predict.py --change-detect <geometry> <prompt> <year_a> <year_b> [--fast] [--demo]")
        sys.exit(1)
    
    geometry = json.loads(sys.argv[1])
    prompt = sys.argv[2]

    fast_mode = "--fast" in sys.argv[3:]
    quick_mode = "--quick" in sys.argv[3:]
    demo_mode = "--demo" in sys.argv[3:]
    # demo 比 fast 更激进，覆盖 fast
    result = main(geometry, prompt, use_tile_mode=not demo_mode,
                  fast_mode=fast_mode or demo_mode,
                  quick_mode=quick_mode,
                  demo_mode=demo_mode)
    print(json.dumps(result, ensure_ascii=False))
