"""
OmniOVCD 变化检测推理适配模块

基于 OmniOVCD (https://github.com/Erxucomeon/OmniOVCD) 的 SAM3 变化检测框架。
通过 SAM3 的文本提示分割 + 实例级双时相比较，实现开放词汇变化检测。

使用方式:
    from omniovcd_inference import OmniOVCDDetector
    detector = OmniOVCDDetector()
    change_mask = detector.detect(img1_path, img2_path, prompt="建筑物")
"""

import os
import sys
import numpy as np
from PIL import Image
from pathlib import Path

# 添加 OmniOVCD 到 Python 路径
OMNIOVCD_DIR = Path(__file__).parent.parent.parent.parent / "OmniOVCD"
if str(OMNIOVCD_DIR) not in sys.path:
    sys.path.insert(0, str(OMNIOVCD_DIR))

# 添加 sam3 到路径（OmniOVCD 内置）
SAM3_DIR = OMNIOVCD_DIR / "sam3"
if str(OMNIOVCD_DIR) not in sys.path:
    sys.path.insert(0, str(OMNIOVCD_DIR))


def _resolve_checkpoint_path():
    """查找 SAM3 模型权重文件"""
    candidates = [
        OMNIOVCD_DIR / "sam3_checkpoint" / "facebook" / "sam3" / "sam3.pt",
        OMNIOVCD_DIR / "sam3_checkpoint" / "sam3.pt",
        "/home/server/python/seg/SegEarth-OV-3-main/sam3_checkpoint/sam3.pt",
    ]
    for p in candidates:
        if Path(p).exists():
            return str(p)
    return "sam3_checkpoint/sam3.pt"  # fallback to default


def _generate_class_file(prompt, output_dir):
    """
    根据用户提示词动态生成类别文件。
    格式: 每行一个类别，逗号分隔别名。第一行为 background。
    """
    # 提示词到类别关键词的映射
    PROMPT_ALIASES = {
        "建筑": "building,house,roof,construction,edifice,建筑物,房子,房屋,屋顶,厂房,仓库,楼",
        "建筑物": "building,house,roof,construction,edifice,建筑物,房子,房屋,屋顶,厂房,仓库,楼",
        "building": "building,house,roof,construction,edifice,建筑物,房子,房屋,屋顶,厂房,仓库,楼",
        "道路": "road,street,highway,pavement,道路,公路,马路,路",
        "road": "road,street,highway,pavement,道路,公路,马路,路",
        "水体": "water,river,lake,pond,sea,ocean,水体,河流,湖泊,水",
        "water": "water,river,lake,pond,sea,ocean,水体,河流,湖泊,水",
        "车辆": "car,vehicle,truck,bus,汽车,车辆,卡车,车",
        "car": "car,vehicle,truck,bus,汽车,车辆,卡车,车",
        "树木": "tree,forest,wood,树,树木,树林,森林",
        "tree": "tree,forest,wood,树,树木,树林,森林",
        "农田": "cropland,farmland,crop,农田,耕地,田地",
        "裸地": "bareland,barren,bare soil,裸地,裸土,荒地",
        "操场": "playground,sports field,操场,运动场",
        "植被": "vegetation,grass,tree,plant,植被,草地,树木,植物",
    }

    # 尝试匹配
    prompt_lower = prompt.strip().lower()
    aliases = None
    for key, val in PROMPT_ALIASES.items():
        if key in prompt_lower:
            aliases = val
            break

    if aliases is None:
        # 使用原始提示词作为类别名
        aliases = prompt.strip()

    content = f"background\n{aliases}\n"

    cls_path = os.path.join(output_dir, "cls_dynamic.txt")
    os.makedirs(output_dir, exist_ok=True)
    with open(cls_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return cls_path


class OmniOVCDDetector:
    """
    OmniOVCD 变化检测器（单例模式，避免重复加载 3.2GB 模型）
    """

    _instance = None
    _model = None
    _device = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._model is not None:
            return  # 已初始化
        self._init_model()

    def _init_model(self):
        """加载 OmniOVCD 模型和 SAM3 权重"""
        import torch
        import OmniOVCD_seg
        import custom_datasets  # noqa: F401 注册 CDDataset
        import custom_transforms  # noqa: F401

        print("[OmniOVCD] 正在加载 SAM3 模型...")

        # 设置设备
        device_str = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = torch.device(device_str)

        print(f"[OmniOVCD] device: {device_str}")

        # OmniOVCDSeg 内部调用 build_sam3_image_model 加载模型
        # CWD 应为 OmniOVCD 目录，相对路径才能正确解析
        self._model = OmniOVCD_seg.OmniOVCDSeg(
            classname_path=str(OMNIOVCD_DIR / "configs" / "cls_levir_cd.txt"),
            device=self._device,
            prob_thd=0.4,
            confidence_threshold=0.5,
            use_sem_seg=True,
            use_presence_score=True,
            use_transformer_decoder=True,
            change_detection_method='instance_method',
            instance_iou_threshold=0.25,
            t12_min_instance_area=0,
            cd_min_instance_area=0,
            enable_vis=False,
            merge_classes_above_threshold=1,
        )

        n_params = sum(p.numel() for p in self._model.processor.model.parameters()) / 1e9
        print(f"[OmniOVCD] 模型加载完成 ({n_params:.1f}B params, {self._model.num_queries} queries)")

    def _set_query(self, prompt, output_dir):
        """根据用户提示词动态设置查询类别"""
        import torch
        import OmniOVCD_seg

        cls_path = _generate_class_file(prompt, output_dir)
        self._model.query_words, self._model.query_idx = OmniOVCD_seg.get_cls_idx(cls_path)
        self._model.num_cls = max(self._model.query_idx) + 1
        self._model.num_queries = len(self._model.query_idx)
        self._model.query_idx = torch.Tensor(self._model.query_idx).to(torch.int64).to(self._device)
        print(f"[OmniOVCD] 动态类别: {self._model.query_words}")

    def detect(self, img1_path, img2_path, prompt, output_dir=None):
        """
        对双时相影像执行变化检测。

        Args:
            img1_path: 年份A影像路径
            img2_path: 年份B影像路径
            prompt: 检测目标提示词（如"建筑物"）
            output_dir: 输出目录（可选）

        Returns:
            dict with keys:
                - change_mask: numpy array (H, W), 1=变化区域, 0=无变化
                - seg1_mask: numpy array (H, W), 年份A的分割结果
                - seg2_mask: numpy array (H, W), 年份B的分割结果
                - img_shape: (H, W) 原始图像尺寸
        """
        import tempfile
        import torch

        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix="omniovcd_")
        os.makedirs(output_dir, exist_ok=True)

        # 动态设置类别
        self._set_query(prompt, output_dir)

        # 加载图像并验证尺寸一致
        img1 = Image.open(img1_path).convert('RGB')
        img2 = Image.open(img2_path).convert('RGB')

        w1, h1 = img1.size
        w2, h2 = img2.size

        print(f"[OmniOVCD] img1: {w1}x{h1}, img2: {w2}x{h2}")

        # 如果尺寸不一致，需要对齐到相同尺寸
        if (w1, h1) != (w2, h2):
            print(f"[OmniOVCD] 图像尺寸不一致，对齐到 ({w1}, {h1})")
            img2 = img2.resize((w1, h1), Image.BILINEAR)
            # 保存对齐后的图像
            aligned_path = os.path.join(output_dir, "aligned_img2.png")
            img2.save(aligned_path)
            img2_path = aligned_path

        # 判断是否需要滑窗推理
        max_side = max(w1, h1)
        use_slide = max_side > 1024

        if use_slide:
            print(f"[OmniOVCD] 图像较大({max_side}px)，使用滑窗推理")
            # 使用滑窗推理
            seg1_logits = self._model.slide_inference(img1, stride=512, crop_size=1024)
            seg2_logits = self._model.slide_inference(img2, stride=512, crop_size=1024)
        else:
            seg1_logits = self._model._inference_single_view(img1)
            seg2_logits = self._model._inference_single_view(img2)

        # 处理类别索引
        if self._model.num_cls != self._model.num_queries:
            seg1_logits = seg1_logits.unsqueeze(0)
            cls_index = torch.nn.functional.one_hot(self._model.query_idx)
            cls_index = cls_index.T.view(self._model.num_cls, len(self._model.query_idx), 1, 1)
            seg1_logits = (seg1_logits * cls_index).max(1)[0]

            seg2_logits = seg2_logits.unsqueeze(0)
            seg2_logits = (seg2_logits * cls_index).max(1)[0]

        seg1_pred = torch.argmax(seg1_logits, dim=0)
        seg2_pred = torch.argmax(seg2_logits, dim=0)

        # 概率阈值过滤
        max_vals1 = seg1_logits.max(0)[0]
        seg1_pred[max_vals1 < self._model.prob_thd] = self._model.bg_idx

        max_vals2 = seg2_logits.max(0)[0]
        seg2_pred[max_vals2 < self._model.prob_thd] = self._model.bg_idx

        # 类别合并（merge_classes_above_threshold）
        if self._model.merge_classes_above_threshold is not None:
            threshold = self._model.merge_classes_above_threshold
            seg1_pred[seg1_pred >= threshold] = 1
            seg1_pred[seg1_pred < threshold] = self._model.bg_idx
            seg2_pred[seg2_pred >= threshold] = 1
            seg2_pred[seg2_pred < threshold] = self._model.bg_idx

        # 实例级变化检测
        seg1_binary = (seg1_pred == 1).long()
        seg2_binary = (seg2_pred == 1).long()
        change_pred = self._model._instance_level_change_detection(seg1_binary, seg2_binary)

        # 面积过滤
        if self._model.cd_min_instance_area > 0:
            change_pred = self._model._filter_cd_instances_by_area(change_pred)

        # 转为 numpy
        seg1_mask = seg1_pred.cpu().numpy().astype(np.uint8)
        seg2_mask = seg2_pred.cpu().numpy().astype(np.uint8)
        change_mask = change_pred.cpu().numpy().astype(np.uint8)

        # 保存调试图像
        debug_dir = os.path.join(output_dir, "debug")
        os.makedirs(debug_dir, exist_ok=True)
        Image.fromarray(seg1_mask * 255).save(os.path.join(debug_dir, "seg1.png"))
        Image.fromarray(seg2_mask * 255).save(os.path.join(debug_dir, "seg2.png"))
        Image.fromarray(change_mask * 255).save(os.path.join(debug_dir, "change.png"))

        print(f"[OmniOVCD] 检测完成: seg1={int(seg1_mask.sum())}px, "
              f"seg2={int(seg2_mask.sum())}px, change={int(change_mask.sum())}px")

        return {
            "change_mask": change_mask,
            "seg1_mask": seg1_mask,
            "seg2_mask": seg2_mask,
            "img_shape": (h1, w1),
        }

    def mask_to_geojson(self, mask, bounds, img_shape, source_year=None, change_type=None):
        """
        将变化检测 mask 转换为 GeoJSON FeatureCollection。

        Args:
            mask: (H, W) numpy array, 1=前景
            bounds: (minx, miny, maxx, maxy) EPSG:4326
            img_shape: (H, W)
            source_year: 来源年份（可选）
            change_type: 'added' 或 'removed'（可选）

        Returns:
            GeoJSON FeatureCollection
        """
        import cv2
        from shapely.geometry import shape, mapping, Polygon

        contours, _ = cv2.findContours(
            mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        minx, miny, maxx, maxy = bounds
        H, W = img_shape
        x_res = (maxx - minx) / W
        y_res = (maxy - miny) / H

        features = []
        for idx, contour in enumerate(contours):
            area = cv2.contourArea(contour)
            if area < 50:  # 最小面积过滤
                continue

            # 简化多边形
            epsilon = 0.005 * cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, epsilon, True)
            poly_px = approx.reshape(-1, 2)

            # 像素 → 地理坐标
            geo_coords = []
            for px, py in poly_px:
                geo_x = minx + px * x_res
                geo_y = maxy - py * y_res  # Y轴反向（像素0=北=lat最大）
                geo_coords.append([geo_x, geo_y])

            if len(geo_coords) < 4:
                continue

            # 闭合多边形
            if geo_coords[0] != geo_coords[-1]:
                geo_coords.append(geo_coords[0])

            try:
                geo_poly = Polygon(geo_coords)
                if geo_poly.is_valid and geo_poly.area > 0:
                    area_m2 = geo_poly.area * 111000 * 111000  # 粗略估算
                    props = {
                        "id": idx,
                        "area_m2": round(area_m2, 2),
                        "area_mu": round(area_m2 / 666.67, 3),
                    }
                    if source_year is not None:
                        props["year"] = source_year
                    if change_type is not None:
                        props["change_type"] = change_type

                    features.append({
                        "type": "Feature",
                        "geometry": mapping(geo_poly),
                        "properties": props,
                    })
            except Exception as e:
                continue

        return {"type": "FeatureCollection", "features": features}

    def detect_to_geojson(self, img1_path, img2_path, prompt, bounds,
                          year_a, year_b, output_dir=None):
        """
        完整的变化检测流水线：检测 → mask → GeoJSON。

        Returns:
            GeoJSON FeatureCollection with change_type properties
        """
        result = self.detect(img1_path, img2_path, prompt, output_dir)
        change_mask = result["change_mask"]
        seg1_mask = result["seg1_mask"]
        seg2_mask = result["seg2_mask"]
        img_shape = result["img_shape"]

        # 将变化 mask 分解为新增和消失
        # 新增: 在 seg2 中出现但不在 seg1 中 → change_mask ∩ seg2
        # 消失: 在 seg1 中出现但不在 seg2 中 → change_mask ∩ seg1
        added_mask = change_mask & seg2_mask
        removed_mask = change_mask & seg1_mask

        # 转换为 GeoJSON
        added_geojson = self.mask_to_geojson(
            added_mask, bounds, img_shape,
            source_year=year_b, change_type="added"
        )
        removed_geojson = self.mask_to_geojson(
            removed_mask, bounds, img_shape,
            source_year=year_a, change_type="removed"
        )

        # 合并
        all_features = added_geojson["features"] + removed_geojson["features"]

        return {
            "type": "FeatureCollection",
            "features": all_features,
            "metadata": {
                "year_a": year_a,
                "year_b": year_b,
                "prompt": prompt,
                "method": "OmniOVCD",
                "added_count": len(added_geojson["features"]),
                "removed_count": len(removed_geojson["features"]),
                "seg1_pixels": int(seg1_mask.sum()),
                "seg2_pixels": int(seg2_mask.sum()),
                "change_pixels": int(change_mask.sum()),
            }
        }


# 全局检测器实例（懒加载）
_detector = None


def get_detector():
    """获取全局 OmniOVCD 检测器实例"""
    global _detector
    if _detector is None:
        _detector = OmniOVCDDetector()
    return _detector


def run_omniovcd_change_detect(img1_path, img2_path, prompt, bounds,
                                year_a, year_b, output_dir=None):
    """
    便捷函数：执行 OmniOVCD 变化检测并返回 GeoJSON。

    Args:
        img1_path: 年份A影像路径
        img2_path: 年份B影像路径
        prompt: 检测目标提示词
        bounds: (minx, miny, maxx, maxy) EPSG:4326
        year_a: 年份A
        year_b: 年份B
        output_dir: 输出目录

    Returns:
        GeoJSON FeatureCollection
    """
    detector = get_detector()
    return detector.detect_to_geojson(
        img1_path, img2_path, prompt, bounds,
        year_a, year_b, output_dir
    )


if __name__ == "__main__":
    # 测试
    import sys
    if len(sys.argv) < 5:
        print("用法: python omniovcd_inference.py <img1> <img2> <prompt> <bounds_json> [year_a] [year_b]")
        sys.exit(1)

    img1 = sys.argv[1]
    img2 = sys.argv[2]
    prompt = sys.argv[3]
    bounds = tuple(json.loads(sys.argv[4]))
    year_a = int(sys.argv[5]) if len(sys.argv) > 5 else 2023
    year_b = int(sys.argv[6]) if len(sys.argv) > 6 else 2025

    import json
    result = run_omniovcd_change_detect(img1, img2, prompt, bounds, year_a, year_b)
    print(json.dumps(result, ensure_ascii=False, indent=2))
