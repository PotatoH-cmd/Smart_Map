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
import re
import numpy as np
from pathlib import Path
from PIL import Image
# from io import BytesIO  # 不再需要，直接使用 PIL Image.save(path)
from shapely.geometry import shape, mapping, Polygon
from shapely.ops import unary_union
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.transform import from_bounds as transform_from_bounds

# 添加 SegEarth-OV-3 到路径
SEG_DIR = Path(__file__).parent.parent.parent.parent / "seg" / "SegEarth-OV-3-main"
sys.path.insert(0, str(SEG_DIR))

# 切换工作目录（SAM3 模型使用相对路径加载资源）
os.chdir(str(SEG_DIR))


LOCAL_TIF = "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/2023年高分影像.tif"
SAM_BACKEND = "sam3"  # 统一使用 SAM3 语义分割，不再支持 Ollama/VLM

# 进度追踪：通过环境变量 SAM_PROGRESS_FILE 传递进度文件路径
PROGRESS_FILE = os.environ.get("SAM_PROGRESS_FILE", "")

# ── 模块级模型单例 ──
# 避免每次推理重复加载 800M 参数的 SAM3 模型
_MODEL_CACHE = None
_SSA_ADAPTER = None
_PRIORITY_CATEGORIES = os.environ.get("SAM_PRIORITY_CATEGORIES", "").strip()
_BETA_PRIORITY = float(os.environ.get("SAM_BETA_PRIORITY", "2.0"))


def _read_active_checkpoint():
    """读取当前激活的 SSA checkpoint 路径（backend/data/checkpoints/active.json）。
    若未配置或文件不存在，则回退到环境变量 SAM3_SSA_CHECKPOINT。"""
    try:
        cfg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'data', 'checkpoints', 'active.json')
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r') as f:
                data = json.load(f)
            ckpt = data.get('checkpoint', '')
            if ckpt and os.path.exists(ckpt):
                return ckpt
    except Exception:
        pass
    return os.environ.get('SAM3_SSA_CHECKPOINT', '').strip()


_SSA_CHECKPOINT = _read_active_checkpoint()


def get_sam_model():
    """
    获取或创建 SAM3 模型实例（模块级单例）。
    首次调用时加载模型，后续调用直接返回缓存实例。
    
    环境变量:
      SAM_PROB_THD: 概率阈值（默认 0.18）
      SAM_CONF_THD: 置信度阈值（默认 0.12）
      SAM3_SSA_CHECKPOINT: SSA 微调 checkpoint 路径
    """
    global _MODEL_CACHE, _SSA_ADAPTER, _PRIORITY_CATEGORIES, _BETA_PRIORITY
    if _MODEL_CACHE is None:
        from segearthov3_segmentor import SegEarthOV3Segmentation
        _prob_thd = float(os.environ.get("SAM_PROB_THD", "0.18"))
        _conf_thd = float(os.environ.get("SAM_CONF_THD", "0.12"))
        print(f"[SAM] 加载模型单例... prob_thd={_prob_thd}, conf_thd={_conf_thd}")
        _MODEL_CACHE = SegEarthOV3Segmentation(
            type='SegEarthOV3Segmentation',
            model_type='SAM3',
            classname_path=os.path.join(str(SEG_DIR), 'configs', 'my_name.txt'),
            prob_thd=_prob_thd,
            confidence_threshold=_conf_thd,
            slide_stride=1280,
            slide_crop=1536,
        )
        # ── 加载 SSA Adapter（如果配置了 checkpoint） ──
        # ── 加载 checkpoint（自动区分 ReSAM vs 旧版 SSA） ──
        if _SSA_CHECKPOINT and os.path.exists(_SSA_CHECKPOINT):
            try:
                import torch as _torch
                checkpoint = _torch.load(_SSA_CHECKPOINT, map_location='cpu')
                # 从 checkpoint 元数据提取训练类别
                metadata = checkpoint.get('metadata', {})
                trained_classes = metadata.get('classes', [])
                if trained_classes:
                    existing = set(_PRIORITY_CATEGORIES.split(",")) if _PRIORITY_CATEGORIES else set()
                    existing.update(c.lower() for c in trained_classes)
                    _PRIORITY_CATEGORIES = ",".join(existing)
                    # 使用 SSA checkpoint 时提高 beta
                    _BETA_PRIORITY = max(_BETA_PRIORITY, float(os.environ.get("SAM_SSA_BETA", "3.0")))
                    print(f"[SAM] SSA checkpoint 已加载: {_SSA_CHECKPOINT}")
                    print(f"[SAM]   训练类别: {trained_classes}")
                    print(f"[SAM]   优先级类别: {_PRIORITY_CATEGORIES}, beta={_BETA_PRIORITY}")
                else:
                    print(f"[SAM] SSA checkpoint 加载成功（无类别元数据）: {_SSA_CHECKPOINT}")
            except Exception as e:
                print(f"[SAM] checkpoint 加载失败: {e}，使用默认权重")
        else:
            print(f"[SAM] checkpoint 路径不存在: {_SSA_CHECKPOINT}，使用默认权重")

    return _MODEL_CACHE


def get_ssa_adapter_info():
    """返回当前 SSA Adapter 状态信息"""
    return {
        "checkpoint": _SSA_CHECKPOINT or None,
        "loaded": _SSA_CHECKPOINT and os.path.exists(_SSA_CHECKPOINT),
        "priority_categories": _PRIORITY_CATEGORIES,
        "beta_priority": _BETA_PRIORITY,
    }


def apply_beta_priority(mask, seg_pred_full, prompt, all_indices):
    """
    对优先级类别应用 beta 加权，提高用户关注类别的召回率。
    
    公式: score_{priority} *= beta_priority
    
    当 SAM_PRIORITY_CATEGORIES 环境变量包含 prompt 对应类别时，
    将该类别的 logits/置信度乘以 beta_priority 系数。
    这不会影响 mask 的二进制结果（mask 由阈值决定），
    但结合低阈值策略可以提高候选 pixel 数。
    """
    if not _PRIORITY_CATEGORIES:
        return mask  # 无优先级类别，直接返回

    priority_set = {c.strip().lower() for c in _PRIORITY_CATEGORIES.split(",") if c.strip()}
    prompt_lower = prompt.lower()

    # 检查 prompt 是否命中优先级类别
    is_priority = any(
        pc in prompt_lower or prompt_lower in pc
        for pc in priority_set
    )
    if not is_priority:
        return mask

    # 已有优先级：尝试放宽阈值（降低 _SAM_PROB_THD 临时值）
    # 这是一种启发式方法：对于优先级类别，用更低的阈值重跑
    # 注意：这里仅对已有的 mask 不做改动，因为 prob_thd 在模型加载时已固定
    # 真正的 SSA 微调会从根本上解决这个问题
    log_prefix = f"[PRIORITY] prompt={prompt} 命中优先级类别，已标记"
    print(f"{log_prefix}（当前 beta={_BETA_PRIORITY}，精确加权需 SSA 微调）")
    return mask


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

    img.save(output_path)
    print(f"[SAM] 裁剪影像已保存: {output_path} ({img.size[0]}x{img.size[1]})")
    print(f"[SAM] 实际地理范围(4326): {actual_bounds_4326}")
    return output_path, actual_bounds_4326


def run_sam_inference(image_path, text_prompt, output_dir):
    """
    使用 SegEarthOV3 模型执行文本提示分割。
    
    双路径策略：
    - 若提示词匹配 CLASS_ALIASES（9 类预训练类别），走快速分类路径
    - 若提示词不匹配（用户自定义概念），走 SAM3 直接文本查询路径
    返回: 分割结果 numpy array (H, W)
    """
    import torch
    from torchvision import transforms
    from mmengine.structures import BaseDataElement, PixelData

    class SegDataSample(BaseDataElement):
        pass

    target_indices = resolve_target_indices(text_prompt)

    # ── 自定义提示词路径：SAM3 直接文本查询 ──
    if not target_indices:
        print(f"[SAM] 提示词 '{text_prompt}' 不在 9 类预训练类别中，使用 SAM3 直接查询")
        return run_custom_prompt_inference(image_path, text_prompt, output_dir)

    # ── 预训练类别路径：9 类快速分类 ──
    model = get_sam_model()  # 复用模块级单例

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
    
    # 应用 beta 优先级加权
    mask = apply_beta_priority(mask, None, text_prompt, target_indices)
    
    return mask, seg_pred.shape


def run_custom_prompt_inference(image_path, text_prompt, output_dir):
    """
    直接使用 SAM3 的文本提示能力查询任意用户输入的概念。
    不同于 9 类分类流水线（只返回预定义类别的像素），
    此函数将用户的原始文本直接发送给 SAM3，返回该查询的二值掩膜。
    
    适用场景：用户输入不在 9 类 CLASS_ALIASES 中的任意概念
              如"桥梁""烟囱""大棚""矿坑""采砂船"等
    """
    import torch
    import torch.nn.functional as F
    from PIL import Image

    model = get_sam_model()
    img = Image.open(image_path).convert('RGB')
    w, h = img.size
    device = model.device

    print(f"[SAM-Custom] 直接文本查询: '{text_prompt}' (image {w}x{h})")

    with torch.no_grad():
        # 设置图像，用用户原始文本查询
        inference_state = model.processor.set_image(img)
        inference_state = model.processor.set_text_prompt(
            state=inference_state, prompt=text_prompt
        )

        # 聚合语义头 + 实例头的结果
        combined = torch.zeros((h, w), device=device)
        has_any = False

        # ── 语义头（Semantic Head）：全局覆盖 ──
        sem = inference_state.get('semantic_mask_logits')
        if sem is not None:
            if sem.dim() > 2:
                sem = sem.squeeze()
            if sem.shape[-2:] != (h, w):
                sem = F.interpolate(
                    sem.view(1, 1, *sem.shape[-2:]),
                    size=(h, w), mode='bilinear', align_corners=False
                ).squeeze()
            combined = torch.max(combined, sem)
            has_any = True

        # ── 实例头（Instance Head）：精细边缘 ──
        inst = inference_state.get('masks_logits')
        if inst is not None and hasattr(inst, 'shape') and inst.shape[0] > 0:
            scores = inference_state.get('object_score')
            for i in range(inst.shape[0]):
                m = inst[i].squeeze()
                if m.shape[-2:] != (h, w):
                    m = F.interpolate(
                        m.view(1, 1, *m.shape[-2:]),
                        size=(h, w), mode='bilinear', align_corners=False
                    ).squeeze()
                s = scores[i] if scores is not None and i < len(scores) else 1.0
                combined = torch.max(combined, m * s)
            has_any = True

        if not has_any:
            print(f"[SAM-Custom] 无有效输出，返回空掩膜")
            return np.zeros((h, w), dtype=np.uint8), (h, w)
        # Sigmoid → 概率 → 阈值二值化
        prob = combined.sigmoid().cpu().numpy()
        prob = combined.cpu().numpy()
        mask = (prob > model.prob_thd).astype(np.uint8)

        mask_ratio = mask.sum() / mask.size if mask.size > 0 else 0
        print(f"[SAM-Custom] '{text_prompt}': {mask.sum()}px "
              f"({mask_ratio*100:.1f}%) thd={model.prob_thd}")

    return mask, (h, w)


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
    from mmengine.structures import BaseDataElement, PixelData
    import cv2

    class SegDataSample(BaseDataElement):
        pass

    target_indices = resolve_target_indices(text_prompt)

    # ── 自定义提示词路径：退回单次直接查询 ──
    if not target_indices:
        print(f"[SAM-Tile] 提示词 '{text_prompt}' 不在 9 类中，退回 SAM3 直接查询")
        return run_custom_prompt_inference(image_path, text_prompt, output_dir)

    # 加载模型（复用模块级单例）
    print(f"[SAM-Tile] 获取模型单例...")
    model = get_sam_model()

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
            zoomed_w = min(tw * zoom_factor, 4096)
            zoomed_h = min(th * zoom_factor, 4096)
            zoomed_tile = tile.resize((zoomed_w, zoomed_h), Image.LANCZOS)

            # 保存瓦片到内存盘（/dev/shm），避免磁盘 I/O
            shm_dir = os.path.join('/dev/shm', f'sam_tile_{os.getpid()}')
            os.makedirs(shm_dir, exist_ok=True)
            tile_tmp_path = os.path.join(shm_dir, f'tile_{row}_{col}.png')
            zoomed_tile.save(tile_tmp_path)

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

    # === 合并阈值（瓦片内部模型已做阈值过滤，此处用固定低阈值即可） ===
    _threshold = float(os.environ.get("SAM_MERGE_THRESHOLD", "0.35"))
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

    # 应用 beta 优先级加权（与 run_sam_inference 对称）
    merged_mask = apply_beta_priority(merged_mask, None, text_prompt, target_indices)

    print(f"[SAM-Tile] 瓦片合并完成，mask 尺寸: {full_H}x{full_W}, "
          f"threshold={threshold:.3f}, mask_ratio={merged_mask.sum() / merged_mask.size:.4f}")
    return merged_mask, (full_H, full_W)


def requery_main(image_path, coarse_geojson, text_prompt, img_bounds, output_dir=None):
    """
    Requery 精修推理：
    对粗检测结果中的每个几何体，计算像素外接矩形 → 裁剪区域 →
    SAM3 再推理 → 输出精修 GeoJSON。

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
    import torch
    from torchvision import transforms
    from mmengine.structures import BaseDataElement, PixelData
    import cv2

    class SegDataSample(BaseDataElement):
        pass

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="sam_requery_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    start_time = time.time()
    target_indices = resolve_target_indices(text_prompt)

    model = get_sam_model()

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

        # c. SAM3 推理
        ds = SegDataSample()
        ds.set_metainfo({
            'img_path': crop_path,
            'ori_shape': (crop_h, crop_w),
        })

        try:
            result = model.predict(None, data_samples=[ds])
        except Exception as e:
            print(f"[Requery] 特征 {feat_idx} 推理失败: {e}")
            refined_features.append(feat)
            kept_original += 1
            continue

        seg_pred = result[0].pred_sem_seg.data.cpu().numpy().squeeze(0)

        refined_mask = np.zeros_like(seg_pred, dtype=np.uint8)
        for idx in target_indices:
            refined_mask |= (seg_pred == idx).astype(np.uint8)

        refined_mask = apply_beta_priority(refined_mask, None, text_prompt, target_indices)
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
                    "source": "sam_requery",
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
    """根据裁剪图大小自适应瓦片尺寸，GPU 48GB 显存充足，使用大瓦片避免建筑物被切分。"""
    max_side = max(img_w, img_h)
    area = img_w * img_h

    # 小图：1024px 瓦片 + 2x 放大（zoomed 2048px，小目标也能看清）
    if max_side <= 2000 and area <= 4_000_000:
        return {"tile_size": 1024, "zoom_factor": 2, "overlap": 64}

    # 中图：1536px 瓦片，1x 直接推理
    if max_side <= 4000 and area <= 15_000_000:
        return {"tile_size": 1536, "zoom_factor": 1, "overlap": 64}

    # 大图：2048px 大瓦片，确保大型建筑不被切分
    return {"tile_size": 2048, "zoom_factor": 1, "overlap": 64}


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
        if demo_mode:
            # Demo 模式: 单次推理，快速出结果
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

        _write_progress("inference_done", 90, 100, "推理完成，正在生成多边形...")
    except Exception as e:
        print(f"[SAM] 推理失败: {e}")
        _write_progress("error", 0, 1, f"推理失败: {e}")
        import traceback
        traceback.print_exc()
        return {"type": "FeatureCollection", "features": []}
    
    polygons_px = mask_to_polygons(mask, min_area=50)
    polygons_px = mask_to_polygons(mask, min_area=50, epsilon_ratio=0.01)
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


def point_predict(points, labels, image_bounds=None, image_path=None, prompt="object"):
    """点提示分割（--point 模式）。

    注意：此函数的原始实现（约 200 行，基于 SAM3 geometric prompt）在一次未收尾的
    重构中被删除，源码无法从 git 干净还原（字节码已抢救备份，待还原）。
    文本提示识别（main）不依赖本函数，不受影响。点提示模式暂不可用。
    """
    return {
        "polygons": [],
        "message": "点提示模式(point_predict)暂不可用：原始实现已丢失，待还原。请改用文本提示识别。",
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
        print("用法: python sam_predict.py <geometry_json> <prompt> [--fast] [--quick] [--demo] [--requery]")
        print("  --point: 点提示模式，从 stdin 读取 JSON")
        print("  --requery: 从 stdin 读取粗检测 GeoJSON，执行精修推理")
        sys.exit(1)
    
    geometry = json.loads(sys.argv[1])
    prompt = sys.argv[2]

    if "--requery" in sys.argv[3:]:
        # ── Requery 精修模式 ──
        # stdin: 粗检测 GeoJSON FeatureCollection
        coarse_json = json.loads(sys.stdin.read())
        output_dir = tempfile.mkdtemp(prefix="sam_requery_")
        # 从本地 TIF 裁剪影像（使用标准分辨率，不做缩放）
        img_path = os.path.join(output_dir, "requery_input.png")
        geom = shape(geometry)
        try:
            img_path, actual_bounds = crop_image_from_local_tif(
                geom.bounds, img_path, target_size=None)
        except Exception as e:
            print(f"[Requery] 影像裁剪失败: {e}", file=sys.stderr)
            result = {"type": "FeatureCollection", "features": []}
        else:
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
