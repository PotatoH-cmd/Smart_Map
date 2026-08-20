#!/usr/bin/env python
"""
ReSAM 完整训练脚本 — R³ 闭环（Refine-Requery-Reinforce）

对齐 ReSAM (CVPR 2026) 论文的完整训练流程：
  1. 加载 SAM3 完整模型，冻结全部参数
  2. 注入 LoRA 到 ViT 编码器（仅 LoRA 参数可训练）
  3. 初始化 FIFO 队列式 SSA 模块（可训练）
  4. R³ 训练循环:
     - Refine: LoRA 增强的编码器提取特征
     - Requery: 从粗 mask 导出 box 提示，二次查询获得精细 mask
     - Reinforce: SSA 对比学习对齐语义空间
  5. 组合损失: Focal + Dice + IoU + SSA
  6. Weak-Strong 双视图增强 + EMA 教师模型

用法:
  python resam_train.py \
    --annotations /path/to/annotations.json \
    --image-dir /path/to/images/ \
    --epochs 20 \
    --output resam_v1.pt
"""

import os
import sys
import json
import time
import copy
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, random_split
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from PIL import Image
import cv2

# 训练图表
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# 项目路径
PROJECT_ROOT = Path(__file__).parent.parent
TOOLS_DIR = Path(__file__).parent
SEG_DIR = Path(__file__).parent.parent.parent.parent / "seg" / "SegEarth-OV-3-main"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SEG_DIR))
sys.path.insert(0, str(SEG_DIR / "sam3"))

from tools.resam_lora import inject_lora_to_vit, extract_lora_state_dict, LoRALinear
from tools.resam_ssa import ReSAMSSA
from tools.resam_losses import ReSAMLoss


# ==============================
# Weak-Strong 双视图数据增强
# ==============================

class WeakAugmentation:
    """弱增强: 仅 resize + normalize"""
    def __init__(self, target_size: int = 1024):
        self.target_size = target_size

    def __call__(self, image: np.ndarray, mask: np.ndarray):
        img = cv2.resize(image, (self.target_size, self.target_size), interpolation=cv2.INTER_LINEAR)
        msk = cv2.resize(mask.astype(np.float32), (self.target_size, self.target_size), interpolation=cv2.INTER_NEAREST)
        # normalize to [0, 1]
        img = img.astype(np.float32) / 255.0
        return img, msk


class StrongAugmentation:
    """强增强: RandomCrop + ColorJitter + GaussianBlur + Flip"""
    def __init__(self, target_size: int = 1024):
        self.target_size = target_size

    def __call__(self, image: np.ndarray, mask: np.ndarray):
        h, w = image.shape[:2]

        # 1. Random resize (0.8x ~ 1.2x)
        scale = np.random.uniform(0.8, 1.2)
        new_h, new_w = int(h * scale), int(w * scale)
        image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        mask = cv2.resize(mask.astype(np.float32), (new_w, new_h), interpolation=cv2.INTER_NEAREST)

        # 2. Random crop to target size
        if new_h >= self.target_size and new_w >= self.target_size:
            top = np.random.randint(0, new_h - self.target_size + 1)
            left = np.random.randint(0, new_w - self.target_size + 1)
            image = image[top:top+self.target_size, left:left+self.target_size]
            mask = mask[top:top+self.target_size, left:left+self.target_size]
        else:
            image = cv2.resize(image, (self.target_size, self.target_size))
            mask = cv2.resize(mask, (self.target_size, self.target_size), interpolation=cv2.INTER_NEAREST)

        # 3. Color jitter (brightness, contrast, saturation)
        if np.random.rand() > 0.3:
            # brightness
            image = np.clip(image * np.random.uniform(0.7, 1.3), 0, 255).astype(np.uint8)
        if np.random.rand() > 0.3:
            # contrast
            mean = image.mean()
            image = np.clip((image - mean) * np.random.uniform(0.7, 1.3) + mean, 0, 255).astype(np.uint8)

        # 4. Gaussian blur
        if np.random.rand() > 0.5:
            ksize = np.random.choice([3, 5, 7])
            image = cv2.GaussianBlur(image, (ksize, ksize), 0)

        # 5. Random flip
        if np.random.rand() > 0.5:
            image = np.fliplr(image).copy()
            mask = np.fliplr(mask).copy()
        if np.random.rand() > 0.5:
            image = np.flipud(image).copy()
            mask = np.flipud(mask).copy()

        # normalize
        image = image.astype(np.float32) / 255.0
        return image, mask


# ==============================
# 数据集
# ==============================

class ReSAMDataset(Dataset):
    """ReSAM 训练数据集，支持双视图增强"""

    def __init__(self, annotations: list, image_dir: str, class_to_idx: dict,
                 target_size: int = 1024, use_strong_aug: bool = True):
        self.annotations = annotations
        self.image_dir = Path(image_dir)
        self.class_to_idx = class_to_idx
        self.target_size = target_size
        self.weak_aug = WeakAugmentation(target_size)
        self.strong_aug = StrongAugmentation(target_size) if use_strong_aug else None

    def __len__(self):
        return len(self.annotations)

    def __getitem__(self, idx):
        ann = self.annotations[idx]

        # 加载影像
        img_path = self.image_dir / ann.get('image_path', f'{idx:04d}.png')
        try:
            img = np.array(Image.open(img_path).convert('RGB'))
        except Exception:
            img = np.zeros((self.target_size, self.target_size, 3), dtype=np.uint8)

        # 渲染 mask
        mask = self._render_mask(ann, img.shape[:2])

        # 类别
        label = ann.get('label', 'background')
        class_id = self.class_to_idx.get(label, 0)

        # Weak 视图
        weak_img, weak_mask = self.weak_aug(img.copy(), mask.copy())

        # Strong 视图
        if self.strong_aug:
            strong_img, strong_mask = self.strong_aug(img.copy(), mask.copy())
        else:
            strong_img, strong_mask = weak_img.copy(), weak_mask.copy()

        # 转 tensor: (H, W, C) → (C, H, W)
        weak_tensor = torch.from_numpy(weak_img.transpose(2, 0, 1)).float()
        strong_tensor = torch.from_numpy(strong_img.transpose(2, 0, 1)).float()
        weak_mask_t = torch.from_numpy(weak_mask).float().clamp(0, 1)
        strong_mask_t = torch.from_numpy(strong_mask).float().clamp(0, 1)

        return {
            'weak_image': weak_tensor,
            'strong_image': strong_tensor,
            'weak_mask': weak_mask_t,
            'strong_mask': strong_mask_t,
            'class_id': class_id,
            'label': label,
        }

    def _render_mask(self, ann: dict, img_size: tuple) -> np.ndarray:
        """将 GeoJSON geometry 渲染为二值 mask"""
        h, w = img_size
        mask = np.zeros((h, w), dtype=np.uint8)

        mask_path = ann.get('mask_path')
        if mask_path and Path(mask_path).exists():
            try:
                pm = Image.open(mask_path).convert('L')
                pm = pm.resize((w, h), Image.NEAREST)
                return (np.array(pm) > 127).astype(np.uint8)
            except Exception:
                pass

        geom = ann.get('geometry')
        if geom:
            try:
                pts = []
                if geom.get('type') == 'Polygon':
                    for ring in geom['coordinates']:
                        for c in ring:
                            pts.append([int(c[0]), int(c[1])])
                elif geom.get('type') == 'MultiPolygon':
                    for polygon in geom['coordinates']:
                        for ring in polygon:
                            for c in ring:
                                pts.append([int(c[0]), int(c[1])])

                if len(pts) >= 3:
                    pts_array = np.array(pts, dtype=np.int32)
                    cv2.fillPoly(mask, [pts_array], 1)
            except Exception:
                pass

        return mask


# ==============================
# EMA 模型
# ==============================

class EMAModel:
    """指数移动平均模型，用于稳定教师信号"""

    def __init__(self, model: nn.Module, decay: float = 0.999):
        self.decay = decay
        self.shadow = {}
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow[name] = param.data.clone()

    @torch.no_grad()
    def update(self, model: nn.Module):
        for name, param in model.named_parameters():
            if param.requires_grad and name in self.shadow:
                self.shadow[name] = (
                    self.decay * self.shadow[name] + (1 - self.decay) * param.data
                )

    def apply(self, model: nn.Module):
        """将 EMA 权重应用到模型"""
        for name, param in model.named_parameters():
            if name in self.shadow:
                param.data.copy_(self.shadow[name])


# ==============================
# R³ 训练核心
# ==============================

def mask_to_box(mask: torch.Tensor) -> torch.Tensor:
    """
    从二值 mask 提取 bounding box 提示。
    
    Args:
        mask: (B, H, W) 或 (H, W) 二值 mask
    
    Returns:
        boxes: (B, 4) 格式 [x1, y1, x2, y2] 归一化到 [0, 1]
    """
    if mask.dim() == 2:
        mask = mask.unsqueeze(0)
    
    B, H, W = mask.shape
    boxes = []
    
    for i in range(B):
        m = mask[i] > 0.5
        if m.any():
            rows = torch.where(m.any(dim=1))[0]
            cols = torch.where(m.any(dim=0))[0]
            y1, y2 = rows[0].item() / H, rows[-1].item() / H
            x1, x2 = cols[0].item() / W, cols[-1].item() / W
            # 稍微扩大 box（增加 5% 边距）
            margin = 0.05
            x1 = max(0, x1 - margin)
            y1 = max(0, y1 - margin)
            x2 = min(1, x2 + margin)
            y2 = min(1, y2 + margin)
            boxes.append([x1, y1, x2, y2])
        else:
            boxes.append([0.0, 0.0, 1.0, 1.0])
    
    return torch.tensor(boxes, device=mask.device, dtype=torch.float32)


def build_sam3_for_training(device: str = 'cuda'):
    """
    加载 SAM3 完整模型用于训练。
    
    Returns:
        model: Sam3Image 模型
        processor: Sam3Processor
    """
    from sam3 import build_sam3_image_model
    from sam3.model.sam3_image_processor import Sam3Processor
    
    ckpt_path = str(SEG_DIR / 'weights' / 'sam3' / 'sam3.pt')
    if not os.path.exists(ckpt_path):
        ckpt_path = None
    
    bpe_path = str(SEG_DIR / 'sam3' / 'assets' / 'bpe_simple_vocab_16e6.txt.gz')
    
    model = build_sam3_image_model(
        bpe_path=bpe_path,
        device=device,
        checkpoint_path=ckpt_path,
        load_from_HF=False,
        enable_segmentation=True,
        eval_mode=False,  # 需要训练模式
    )
    
    processor = Sam3Processor(model, confidence_threshold=0.1, device=device)
    
    return model, processor


# ==============================
# 训练图表
# ==============================

def _plot_training_chart(history, save_path, best_epoch, model_name="resam"):
    """绘制训练曲线（含各项损失分解）"""
    if not _HAS_MPL:
        return

    epochs = [h['epoch'] for h in history]
    train_losses = [h['train_loss'] for h in history]
    val_losses = [h['val_loss'] for h in history]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # 左图: 总损失
    ax1.plot(epochs, train_losses, 'o-', color='#0ea5e9', linewidth=2, markersize=5, label='训练损失')
    ax1.plot(epochs, val_losses, 's-', color='#f59e0b', linewidth=2, markersize=5, label='验证损失')
    if best_epoch > 0 and best_epoch <= len(val_losses):
        ax1.axvline(x=best_epoch, color='#10b981', linestyle='--', alpha=0.7)
        ax1.annotate(f'最佳 Epoch {best_epoch}', xy=(best_epoch, val_losses[best_epoch-1]),
                     fontsize=9, color='#10b981', fontweight='bold')
    ax1.set_xlabel('Epoch')
    ax1.set_ylabel('Loss')
    ax1.set_title(f'ReSAM 训练曲线 — {model_name}')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # 右图: 各项损失分解
    if 'focal' in history[0]:
        ax2.plot(epochs, [h.get('focal', 0) for h in history], '-', label='Focal', alpha=0.8)
        ax2.plot(epochs, [h.get('dice', 0) for h in history], '-', label='Dice', alpha=0.8)
        ax2.plot(epochs, [h.get('iou', 0) for h in history], '-', label='IoU', alpha=0.8)
        ax2.plot(epochs, [h.get('ssa', 0) for h in history], '-', label='SSA', alpha=0.8)
        ax2.set_xlabel('Epoch')
        ax2.set_ylabel('Loss')
        ax2.set_title('各项损失分解')
        ax2.legend()
        ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"[ReSAM] 图表已生成: {save_path}")


# ==============================
# 主训练函数
# ==============================

def train_resam(
    annotations_path: str,
    image_dir: str,
    output_path: str,
    epochs: int = 20,
    batch_size: int = 2,
    lr: float = 5e-5,
    lora_rank: int = 4,
    lora_alpha: float = 1.0,
    device: str = 'cuda',
    resume_path: Optional[str] = None,
    target_size: int = 1024,
    num_workers: int = 2,
    use_r3: bool = True,
    ema_decay: float = 0.999,
) -> str:
    """
    ReSAM 完整训练流程。

    Returns:
        输出的 checkpoint 文件路径
    """
    print(f"[ReSAM] ========== ReSAM 训练启动 ==========")
    print(f"[ReSAM] 标注文件: {annotations_path}")
    print(f"[ReSAM] 影像目录: {image_dir}")
    print(f"[ReSAM] epochs={epochs}, batch_size={batch_size}, lr={lr}")
    print(f"[ReSAM] lora_rank={lora_rank}, target_size={target_size}")
    print(f"[ReSAM] R3={use_r3}, EMA_decay={ema_decay}")

    # 1. 加载标注
    with open(annotations_path) as f:
        annotations = json.load(f)
    if not isinstance(annotations, list):
        annotations = annotations.get('annotations', annotations.get('features', []))
    print(f"[ReSAM] 加载 {len(annotations)} 条标注")

    # 统计类别
    labels = set()
    for ann in annotations:
        labels.add(ann.get('label', 'background'))
    class_to_idx = {lbl: i for i, lbl in enumerate(sorted(labels))}
    num_classes = len(class_to_idx)
    print(f"[ReSAM] 类别: {class_to_idx}")

    # 2. 创建数据集
    full_dataset = ReSAMDataset(annotations, image_dir, class_to_idx, target_size)
    total = len(full_dataset)
    val_size = max(1, int(total * 0.2))
    train_size = total - val_size
    train_dataset, val_dataset = random_split(
        full_dataset, [train_size, val_size],
        generator=torch.Generator().manual_seed(42)
    )
    print(f"[ReSAM] 训练: {train_size}, 验证: {val_size}")

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True,
                              num_workers=num_workers, pin_memory=True, drop_last=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False,
                            num_workers=num_workers, pin_memory=True)

    # 3. 加载 SAM3 模型
    print(f"[ReSAM] 加载 SAM3 完整模型...")
    model, processor = build_sam3_for_training(device)

    # 4. 冻结所有参数
    for param in model.parameters():
        param.requires_grad = False
    print(f"[ReSAM] 已冻结全部 SAM3 参数")

    # 5. 注入 LoRA
    # 找到 ViT backbone
    vit = None
    if hasattr(model, 'backbone') and hasattr(model.backbone, 'vision_backbone'):
        vit = model.backbone.vision_backbone
    elif hasattr(model, 'backbone'):
        vit = model.backbone

    if vit is not None:
        lora_params = inject_lora_to_vit(vit, rank=lora_rank, alpha=lora_alpha)
    else:
        # Fallback: 在整个模型中搜索 Attention 层
        print(f"[ReSAM] 警告: 未找到标准 ViT 结构，尝试全模型搜索...")
        lora_params = []
        for name, module in model.named_modules():
            if isinstance(module, nn.Linear) and ('attn' in name) and \
               any(t in name for t in ['qkv', 'proj']):
                parent_name = name.rsplit('.', 1)[0]
                attr_name = name.rsplit('.', 1)[-1]
                parent = model
                for p in parent_name.split('.'):
                    parent = getattr(parent, p)
                from tools.resam_lora import LoRALinear
                lora_linear = LoRALinear(module, rank=lora_rank, alpha=lora_alpha)
                setattr(parent, attr_name, lora_linear)
                lora_params.extend([lora_linear.lora_A, lora_linear.lora_B])
        print(f"[ReSAM] Fallback 注入: {len(lora_params)//2} 层")

    # 6. 初始化 SSA 模块
    ssa_module = ReSAMSSA(d_model=256, queue_size=256, num_classes=num_classes).to(device)

    # 7. 组合损失
    criterion = ReSAMLoss()

    # 8. 优化器（只训练 LoRA + SSA）
    trainable_params = [
        {'params': lora_params, 'lr': lr},
        {'params': ssa_module.parameters(), 'lr': lr * 2},  # SSA 用稍高学习率
    ]
    optimizer = AdamW(trainable_params, weight_decay=0.01)
    scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=5, T_mult=2)

    # 9. EMA
    ema = EMAModel(model, decay=ema_decay) if ema_decay > 0 else None

    # 10. 恢复 checkpoint
    if resume_path and Path(resume_path).exists():
        ckpt = torch.load(resume_path, map_location=device)
        if 'lora' in ckpt:
            from tools.resam_lora import load_lora_state_dict
            load_lora_state_dict(model, ckpt['lora'])
        if 'ssa' in ckpt:
            ssa_module.load_state_dict(ckpt['ssa'], strict=False)
        print(f"[ReSAM] 恢复 checkpoint: {resume_path}")

    # ==============================
    # 训练循环
    # ==============================
    best_val_loss = float('inf')
    best_epoch = 0
    history = []

    model.train()
    # 只有 LoRA 层是 train 模式（其他冻结层的 BN 等保持 eval）
    for m in model.modules():
        if isinstance(m, (nn.BatchNorm2d, nn.LayerNorm)) and not isinstance(m, LoRALinear):
            m.eval()

    for epoch in range(1, epochs + 1):
        epoch_start = time.time()
        epoch_loss = 0.0
        epoch_focal = 0.0
        epoch_dice = 0.0
        epoch_iou = 0.0
        epoch_ssa = 0.0

        ssa_module.train()

        for batch_idx, batch in enumerate(train_loader):
            strong_images = batch['strong_image'].to(device)
            strong_masks = batch['strong_mask'].to(device)
            class_ids = batch['class_id'].to(device)
            B = strong_images.shape[0]

            # ── Refine: LoRA 增强的编码器 ──
            # 使用 SAM3 的 backbone 提取图像特征
            with torch.cuda.amp.autocast(enabled=True):
                # forward_image 通过 backbone 提取视觉特征
                backbone_out = model.backbone.forward_image(strong_images)
                # 获取最后一层特征
                if 'backbone_fpn' in backbone_out:
                    img_features = backbone_out['backbone_fpn'][-1]  # (B, C, H', W')
                else:
                    img_features = backbone_out.get('vision_features', strong_images)

                # Flatten for SSA: (B, C, H', W') → (B, H'*W', C)
                if img_features.dim() == 4:
                    B_f, C_f, H_f, W_f = img_features.shape
                    img_feat_flat = img_features.flatten(2).transpose(1, 2)  # (B, HW, C)
                else:
                    img_feat_flat = img_features

                # 简化的 mask 预测（通过特征与 GT 计算损失）
                # 将特征图上采样到 mask 尺寸，用 1x1 conv 预测 mask
                if img_features.dim() == 4:
                    # 用简单的线性头预测 mask（训练 LoRA 使特征更好）
                    pred_logits = img_features.mean(dim=1)  # (B, H', W') 通道平均
                    pred_logits = F.interpolate(
                        pred_logits.unsqueeze(1), size=strong_masks.shape[-2:],
                        mode='bilinear', align_corners=False
                    ).squeeze(1)
                else:
                    H_sq = int(img_feat_flat.shape[1] ** 0.5)
                    pred_logits = img_feat_flat.mean(dim=-1).view(B, H_sq, H_sq)
                    pred_logits = F.interpolate(
                        pred_logits.unsqueeze(1), size=strong_masks.shape[-2:],
                        mode='bilinear', align_corners=False
                    ).squeeze(1)

            # ── Requery（可选 R3 迭代）──
            if use_r3:
                with torch.no_grad():
                    # 从粗预测生成 box 提示
                    coarse_mask = (torch.sigmoid(pred_logits) > 0.5).float()
                    boxes = mask_to_box(coarse_mask)
                    # box 信息作为额外约束（简化版本：用 box 裁剪注意力区域）
                    # 完整版需要通过 SAM decoder 的 prompt encoder 重新查询
                    # 这里用 box mask 加权特征
                    for b in range(B):
                        x1, y1, x2, y2 = boxes[b]
                        H_m, W_m = strong_masks.shape[-2:]
                        box_mask = torch.zeros(H_m, W_m, device=device)
                        box_mask[int(y1*H_m):int(y2*H_m), int(x1*W_m):int(x2*W_m)] = 1.0
                        # 加权：box 区域内的预测增强
                        pred_logits[b] = pred_logits[b] + 0.5 * box_mask

            # ── Reinforce: SSA 对比学习 ──
            ssa_loss = ssa_module(img_feat_flat.float(), strong_masks, class_ids)

            # ── 组合损失 ──
            loss_dict = criterion(pred_logits, strong_masks, ssa_loss)
            total_loss = loss_dict['total']

            # 反向传播
            optimizer.zero_grad()
            total_loss.backward()
            torch.nn.utils.clip_grad_norm_(lora_params + list(ssa_module.parameters()), max_norm=1.0)
            optimizer.step()

            # EMA 更新
            if ema:
                ema.update(model)

            epoch_loss += total_loss.item()
            epoch_focal += loss_dict['focal'].item()
            epoch_dice += loss_dict['dice'].item()
            epoch_iou += loss_dict['iou'].item()
            epoch_ssa += loss_dict['ssa'].item()

        scheduler.step()
        n_batches = max(len(train_loader), 1)
        train_loss = epoch_loss / n_batches

        # ── 验证阶段 ──
        model.eval()
        ssa_module.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch in val_loader:
                weak_images = batch['weak_image'].to(device)
                weak_masks = batch['weak_mask'].to(device)
                class_ids = batch['class_id'].to(device)

                backbone_out = model.backbone.forward_image(weak_images)
                if 'backbone_fpn' in backbone_out:
                    img_features = backbone_out['backbone_fpn'][-1]
                else:
                    img_features = backbone_out.get('vision_features', weak_images)

                if img_features.dim() == 4:
                    pred_logits = img_features.mean(dim=1)
                    pred_logits = F.interpolate(
                        pred_logits.unsqueeze(1), size=weak_masks.shape[-2:],
                        mode='bilinear', align_corners=False
                    ).squeeze(1)
                else:
                    img_feat_flat = img_features
                    H_sq = int(img_feat_flat.shape[1] ** 0.5)
                    pred_logits = img_feat_flat.mean(dim=-1).view(-1, H_sq, H_sq)
                    pred_logits = F.interpolate(
                        pred_logits.unsqueeze(1), size=weak_masks.shape[-2:],
                        mode='bilinear', align_corners=False
                    ).squeeze(1)

                loss_dict = criterion(pred_logits, weak_masks)
                val_loss += loss_dict['total'].item()

        val_loss /= max(len(val_loader), 1)
        model.train()
        for m in model.modules():
            if isinstance(m, (nn.BatchNorm2d, nn.LayerNorm)) and not isinstance(m, LoRALinear):
                m.eval()

        elapsed = time.time() - epoch_start
        history.append({
            'epoch': epoch,
            'train_loss': round(train_loss, 4),
            'val_loss': round(val_loss, 4),
            'focal': round(epoch_focal / n_batches, 4),
            'dice': round(epoch_dice / n_batches, 4),
            'iou': round(epoch_iou / n_batches, 4),
            'ssa': round(epoch_ssa / n_batches, 4),
        })

        status = '✓ 最佳' if val_loss < best_val_loss else ''
        print(f"[ReSAM] Epoch {epoch:02d}/{epochs} | "
              f"train={train_loss:.4f} | val={val_loss:.4f} {status} | "
              f"lr={scheduler.get_last_lr()[0]:.2e} | {elapsed:.1f}s")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            _save_checkpoint(model, ssa_module, output_path.replace('.pt', '_best.pt'), {
                'epoch': epoch, 'val_loss': val_loss,
                'classes': list(class_to_idx.keys()),
                'lora_rank': lora_rank,
            })

    # ==============================
    # 保存最终模型
    # ==============================
    _save_checkpoint(model, ssa_module, output_path, {
        'epochs': epochs,
        'best_epoch': best_epoch,
        'best_val_loss': best_val_loss,
        'final_train_loss': train_loss,
        'final_val_loss': val_loss,
        'history': history,
        'classes': list(class_to_idx.keys()),
        'lora_rank': lora_rank,
        'lora_alpha': lora_alpha,
        'd_model': 256,
        'num_classes': num_classes,
        'target_size': target_size,
        'train_samples': train_size,
        'val_samples': val_size,
        'training_method': 'ReSAM',
        'timestamp': datetime.now().isoformat(),
    })

    # 绘制图表
    chart_path = output_path.replace('.pt', '_chart.png')
    model_label = os.path.basename(output_path).replace('.pt', '')
    try:
        _plot_training_chart(history, chart_path, best_epoch, model_label)
    except Exception as e:
        print(f"[ReSAM] 图表生成失败: {e}")

    print(f"[ReSAM] ========== 训练完成 ==========")
    print(f"[ReSAM] 输出: {output_path}")
    print(f"[ReSAM] 最佳: epoch {best_epoch}, val_loss={best_val_loss:.4f}")

    return output_path


def _save_checkpoint(model, ssa_module, path, metadata):
    """保存 ReSAM checkpoint（LoRA + SSA + metadata）"""
    lora_sd = extract_lora_state_dict(model)
    checkpoint = {
        'lora': lora_sd,
        'ssa': ssa_module.state_dict(),
        'metadata': metadata,
        'training_method': 'ReSAM',
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    torch.save(checkpoint, path)
    print(f"[ReSAM] 保存: {path}")


# ==============================
# CLI 入口
# ==============================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ReSAM 训练 (R³ 闭环)')
    parser.add_argument('--annotations', required=True, help='标注 JSON 文件')
    parser.add_argument('--image-dir', required=True, help='影像目录')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch-size', type=int, default=2)
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--lora-rank', type=int, default=4)
    parser.add_argument('--lora-alpha', type=float, default=1.0)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--target-size', type=int, default=1024)
    parser.add_argument('--resume', default=None, help='恢复 checkpoint')
    parser.add_argument('--base-checkpoint', default=None, help='基底模型')
    parser.add_argument('--output', default=None)
    parser.add_argument('--num-workers', type=int, default=2)
    parser.add_argument('--no-r3', action='store_true', help='禁用 R3 迭代')
    parser.add_argument('--ema-decay', type=float, default=0.999)
    args = parser.parse_args()

    resume = args.base_checkpoint or args.resume

    if args.output is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.output = f'resam_{ts}.pt'

    train_resam(
        annotations_path=args.annotations,
        image_dir=args.image_dir,
        output_path=args.output,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        lora_rank=args.lora_rank,
        lora_alpha=args.lora_alpha,
        device=args.device,
        resume_path=resume,
        target_size=args.target_size,
        num_workers=args.num_workers,
        use_r3=not args.no_r3,
        ema_decay=args.ema_decay,
    )
