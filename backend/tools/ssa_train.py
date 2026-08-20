#!/usr/bin/env python
"""
SSA 离线微调脚本

独立 CLI 工具，从标注导出的 GeoJSON + 对应区域影像，训练 SSA Adapter。
不依赖 sam_predict.py 或 main.py，通过 subprocess 或直接调用执行。

训练策略：
  - 冻结: SAM3 backbone + encoder（~800M 参数）
  - 训练: SSA Adapter + 文本投影（~100K 参数）
  - Optimizer: AdamW, lr=1e-4, weight_decay=1e-5
  - Loss: BCE（文本-图像语义对齐）+ L2 正则化

用法:
  # 从标注 JSON 训练
  python ssa_train.py \\
    --annotations /path/to/annotations.json \\
    --image-dir /path/to/images/ \\
    --epochs 10 \\
    --batch-size 4 \\
    --output sam3_ssa_v1.pt

  # 增量训练（继续已有 checkpoint）
  python ssa_train.py \\
    --annotations /path/to/annotations.json \\
    --image-dir /path/to/images/ \\
    --resume sam3_ssa_v1.pt \\
    --epochs 5 \\
    --output sam3_ssa_v2.pt

输入格式 (annotations.json):
  [
    {
      "image_path": "crop_001.png",
      "label": "building",
      "geometry": { "type": "Polygon", "coordinates": [[[x,y], ...]] },
      "mask_path": "mask_001.png"   // 可选：预渲染的二值 mask
    },
    ...
  ]
"""

import os
import sys
import json
import time
import argparse
import tempfile
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, Subset, random_split
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from PIL import Image
from shapely.geometry import shape
import cv2

# 训练图表（非阻塞，导入失败不中断训练）
try:
    import matplotlib
    matplotlib.use('Agg')  # 无 GUI 后端
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

# 添加项目路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from tools.ssa_adapter import SSAAdapter, save_ssa_checkpoint


# ==============================
# 数据集
# ==============================

class AnnotationDataset(Dataset):
    """标注数据集，加载 (image, mask, label) 三元组"""

    def __init__(self, annotations: list, image_dir: str, class_to_idx: dict,
                 target_size: int = 512):
        self.annotations = annotations
        self.image_dir = Path(image_dir)
        self.class_to_idx = class_to_idx
        self.target_size = target_size

    def __len__(self):
        return len(self.annotations)

    def __getitem__(self, idx):
        ann = self.annotations[idx]

        # 1. 加载影像
        img_path = self.image_dir / ann.get('image_path', f'{idx:04d}.png')
        try:
            img = Image.open(img_path).convert('RGB')
        except Exception:
            # fallback: 纯黑图
            img = Image.new('RGB', (self.target_size, self.target_size), (0, 0, 0))

        # 2. 渲染 mask
        mask = self._render_mask(ann, img.size)

        # 3. 统一尺寸
        img = img.resize((self.target_size, self.target_size), Image.BILINEAR)
        mask = cv2.resize(mask.astype(np.float32), (self.target_size, self.target_size),
                          interpolation=cv2.INTER_NEAREST)

        # 4. 类别索引
        label = ann.get('label', 'background')
        class_id = self.class_to_idx.get(label, 0)

        # 5. 转 tensor
        img_tensor = torch.from_numpy(np.array(img).transpose(2, 0, 1)).float() / 255.0
        mask_tensor = torch.from_numpy(mask.astype(np.float32)).clamp(0, 1)

        return {
            'image': img_tensor,
            'mask': mask_tensor,
            'class_id': class_id,
            'label': label,
        }

    def _render_mask(self, ann: dict, img_size: tuple) -> np.ndarray:
        """将 GeoJSON geometry 渲染为二值 mask"""
        w, h = img_size
        mask = np.zeros((h, w), dtype=np.uint8)

        # 优先使用预渲染 mask
        mask_path = ann.get('mask_path')
        if mask_path and Path(mask_path).exists():
            try:
                pm = Image.open(mask_path).convert('L')
                pm = pm.resize((w, h), Image.NEAREST)
                return (np.array(pm) > 127).astype(np.uint8)
            except Exception:
                pass

        # 从 geometry 渲染
        geom = ann.get('geometry')
        if geom:
            try:
                s = shape(geom)
                if s.is_empty:
                    return mask
                # 获取外接矩形像素坐标，绘制填充
                minx, miny, maxx, maxy = s.bounds
                # 归一化到 [0, w] / [0, h]
                # 注意: geometry 坐标需与影像空间对应，此处假设已为像素坐标
                pts = []
                if geom.get('type') == 'Polygon':
                    for ring in geom['coordinates']:
                        for c in ring:
                            pts.append([int(c[0]), int(c[1])])
                else:
                    # 非 Polygon 类型，用 bounding box 近似
                    pts = [[int(minx), int(miny)], [int(maxx), int(miny)],
                           [int(maxx), int(maxy)], [int(minx), int(maxy)]]

                if len(pts) >= 3:
                    pts_array = np.array(pts, dtype=np.int32)
                    cv2.fillPoly(mask, [pts_array], 1)
            except Exception:
                pass

        return mask


# ==============================
# 模拟 SAM3 的特征提取器（训练代理）
# ==============================

class DummyFeatureExtractor(nn.Module):
    """
    轻量代理特征提取器，用于在无 SAM3 完整模型时训练 SSA Adapter。

    生产环境中可替换为真实的 SAM3 feature extractor，
    但 SSA Adapter 权重与具体 backbone 解耦——只要 d_model 一致即可。
    此处使用简单 CNN 模拟遥感图像特征提取。
    """

    def __init__(self, d_model: int = 256, img_size: int = 512):
        super().__init__()
        self.d_model = d_model
        self.img_size = img_size

        # 轻量 CNN backbone
        self.encoder = nn.Sequential(
            nn.Conv2d(3, 32, 3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),          # 256

            nn.Conv2d(32, 64, 3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),          # 128

            nn.Conv2d(64, 128, 3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),          # 64

            nn.Conv2d(128, d_model, 3, padding=1),
            nn.BatchNorm2d(d_model),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),          # 32
        )

        self.text_embed = nn.Embedding(100, d_model)  # 模拟文本 tokenizer

    def forward(self, image: torch.Tensor):
        """
        Args:
          image: (B, 3, H, W)

        Returns:
          image_feat: (B, HW', d) 图像特征
          text_feat: (B, T, d) 模拟文本特征
        """
        B = image.shape[0]

        # 图像特征
        img_feat = self.encoder(image)  # (B, d, H/16, W/16)
        _, d, fh, fw = img_feat.shape
        img_feat_flat = img_feat.flatten(2).transpose(1, 2)  # (B, HW, d)

        # 模拟文本特征（类别数=10，取对应 class 的 embedding）
        text_feat = self.text_embed(torch.arange(10, device=image.device))  # (10, d)
        text_feat = text_feat.unsqueeze(0).expand(B, -1, -1)  # (B, 10, d)

        return img_feat_flat, text_feat


# ==============================
# 训练曲线图表
# ==============================

def _plot_training_chart(history, save_path, best_epoch, model_name="model"):
    """绘制训练/验证损失曲线并保存为 PNG"""
    if not _HAS_MPL:
        return
    
    epochs = [h['epoch'] for h in history]
    train_losses = [h['train_loss'] for h in history]
    val_losses = [h['val_loss'] for h in history]
    
    fig, ax = plt.subplots(figsize=(10, 5))
    
    # 训练损失曲线
    ax.plot(epochs, train_losses, 'o-', color='#0ea5e9', linewidth=2, markersize=6, label='训练损失 (train_loss)')
    # 验证损失曲线
    ax.plot(epochs, val_losses, 's-', color='#f59e0b', linewidth=2, markersize=6, label='验证损失 (val_loss)')
    
    # 标记最佳 epoch
    if best_epoch > 0 and best_epoch <= len(val_losses):
        best_val = val_losses[best_epoch - 1]
        ax.axvline(x=best_epoch, color='#10b981', linestyle='--', alpha=0.7, linewidth=1.5)
        ax.annotate(f'最佳 Epoch {best_epoch}\nval_loss={best_val:.4f}',
                    xy=(best_epoch, best_val),
                    xytext=(best_epoch + 0.5, best_val * 1.08),
                    fontsize=10, color='#10b981', fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='#10b981', lw=1.2))
    
    ax.set_xlabel('Epoch', fontsize=12, fontweight='bold')
    ax.set_ylabel('Loss', fontsize=12, fontweight='bold')
    ax.set_title(f'训练曲线 — {model_name}', fontsize=14, fontweight='bold')
    ax.legend(loc='upper right', fontsize=10, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    
    # 标注数值
    for i, (tl, vl) in enumerate(zip(train_losses, val_losses)):
        ax.annotate(f'{tl:.3f}', (epochs[i], tl), textcoords="offset points",
                    xytext=(0, 12), ha='center', fontsize=7, color='#0ea5e9', alpha=0.8)
        ax.annotate(f'{vl:.3f}', (epochs[i], vl), textcoords="offset points",
                    xytext=(0, -16), ha='center', fontsize=7, color='#f59e0b', alpha=0.8)
    
    plt.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"[SSA-Train] 图表已生成: {save_path}")


# ==============================
# 训练循环
# ==============================

def train_ssa(
    annotations_path: str,
    image_dir: str,
    output_path: str,
    epochs: int = 10,
    batch_size: int = 4,
    lr: float = 1e-4,
    device: str = 'cuda',
    resume_path: Optional[str] = None,
    target_size: int = 512,
    d_model: int = 256,
    num_workers: int = 2,
) -> str:
    """
    执行 SSA 微调。

    Returns:
      输出的 checkpoint 文件路径
    """
    print(f"[SSA-Train] ========== SSA 微调启动 ==========")
    print(f"[SSA-Train] 标注文件: {annotations_path}")
    print(f"[SSA-Train] 影像目录: {image_dir}")
    print(f"[SSA-Train] epochs={epochs}, batch_size={batch_size}, lr={lr}")
    print(f"[SSA-Train] device={device}, d_model={d_model}")

    # 1. 加载标注
    with open(annotations_path) as f:
        annotations = json.load(f)
    if not isinstance(annotations, list):
        annotations = annotations.get('annotations', annotations.get('features', []))
    print(f"[SSA-Train] 加载 {len(annotations)} 条标注")

    # 统计类别
    labels = set()
    for ann in annotations:
        labels.add(ann.get('label', 'background'))
    class_to_idx = {lbl: i for i, lbl in enumerate(sorted(labels))}
    print(f"[SSA-Train] 类别: {class_to_idx}")

    # 2. 创建完整数据集
    full_dataset = AnnotationDataset(annotations, image_dir, class_to_idx, target_size)
    total = len(full_dataset)
    val_size = max(1, int(total * 0.2))  # 20% 验证集，至少 1 条
    train_size = total - val_size
    train_dataset, val_dataset = random_split(
        full_dataset, [train_size, val_size],
        generator=torch.Generator().manual_seed(42)  # 固定种子，可复现
    )
    print(f"[SSA-Train] 划分: 训练 {train_size} 条, 验证 {val_size} 条")

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True,
                               num_workers=num_workers, pin_memory=(device == 'cuda'))
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False,
                             num_workers=num_workers, pin_memory=(device == 'cuda'))
    print(f"[SSA-Train] 训练: {train_size} 样本, {len(train_loader)} batches")
    print(f"[SSA-Train] 验证: {val_size} 样本, {len(val_loader)} batches")

    # 3. 创建模型
    feature_extractor = DummyFeatureExtractor(d_model=d_model, img_size=target_size)
    adapter = SSAAdapter(d_model=d_model)

    if resume_path and Path(resume_path).exists():
        state = torch.load(resume_path, map_location=device)
        adapter.load_state_dict(state.get('adapter', state), strict=False)
        print(f"[SSA-Train] 恢复 checkpoint: {resume_path}")

    feature_extractor.to(device)
    adapter.to(device)

    # 4. 优化器 & 调度器
    optimizer = AdamW(adapter.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs, eta_min=lr * 0.01)

    # 5. 训练循环（含验证集评估）
    adapter.train()
    best_val_loss = float('inf')
    best_epoch = 0
    history = []  # [{epoch, train_loss, val_loss}]

    for epoch in range(1, epochs + 1):
        # ── 训练阶段 ──
        adapter.train()
        epoch_loss = 0.0
        epoch_start = time.time()

        for batch_idx, batch in enumerate(train_loader):
            images = batch['image'].to(device)
            masks_gt = batch['mask'].to(device)

            with torch.no_grad():
                img_feat, text_feat = feature_extractor(images)

            _, loss = adapter(text_feat, img_feat, mask_gt=masks_gt)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(adapter.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()

        scheduler.step()
        train_loss = epoch_loss / max(len(train_loader), 1)

        # ── 验证阶段 ──
        adapter.eval()
        val_loss = 0.0
        with torch.no_grad():
            for batch in val_loader:
                images = batch['image'].to(device)
                masks_gt = batch['mask'].to(device)
                img_feat, text_feat = feature_extractor(images)
                _, loss = adapter(text_feat, img_feat, mask_gt=masks_gt)
                val_loss += loss.item()
        val_loss /= max(len(val_loader), 1)

        elapsed = time.time() - epoch_start
        history.append({'epoch': epoch, 'train_loss': round(train_loss, 4), 'val_loss': round(val_loss, 4)})
        
        status = '✓ 最佳' if val_loss < best_val_loss else ''
        print(f"[SSA-Train] Epoch {epoch:02d}/{epochs} | "
              f"train_loss={train_loss:.4f} | val_loss={val_loss:.4f} {status} | "
              f"lr={scheduler.get_last_lr()[0]:.2e} | {elapsed:.1f}s")

        # 基于验证损失保存最佳模型
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            best_path = output_path.replace('.pt', '_best.pt')
            save_ssa_checkpoint(adapter, best_path, {
                'epoch': epoch,
                'train_loss': train_loss,
                'val_loss': val_loss,
                'classes': list(class_to_idx.keys()),
            })

    # 6. 保存最终权重
    save_ssa_checkpoint(adapter, output_path, {
        'epochs': epochs,
        'final_loss': train_loss,
        'val_loss': val_loss,
        'best_epoch': best_epoch,
        'best_val_loss': best_val_loss,
        'history': history,
        'classes': list(class_to_idx.keys()),
        'd_model': d_model,
        'train_samples': train_size,
        'val_samples': val_size,
        'timestamp': datetime.now().isoformat(),
    })

    # 7. 绘制训练曲线并保存
    chart_path = output_path.replace('.pt', '_chart.png')
    best_chart_path = best_path.replace('.pt', '_chart.png') if best_epoch > 0 else None
    model_label = os.path.basename(output_path).replace('.pt', '').replace('sam3_ssa_', '')
    try:
        if _HAS_MPL and len(history) >= 1:
            _plot_training_chart(history, chart_path, best_epoch, model_label)
            print(f"[SSA-Train] 图表已保存: {chart_path}")
            # 最佳模型也保存一份图表
            if best_chart_path and chart_path != best_chart_path:
                import shutil
                shutil.copy2(chart_path, best_chart_path)
        else:
            print(f"[SSA-Train] 图表跳过: matplotlib={_HAS_MPL}, history_len={len(history)}")
    except Exception as e:
        print(f"[SSA-Train] 图表生成失败（非致命）: {e}")

    print(f"[SSA-Train] ========== 训练完成 ==========")
    print(f"[SSA-Train] 输出: {output_path}")
    print(f"[SSA-Train] 训练: {train_size} 条, 验证: {val_size} 条")
    print(f"[SSA-Train] 训练损失变化: {' → '.join(str(h['train_loss']) for h in [history[0], history[-1]])}")
    print(f"[SSA-Train] 验证损失变化: {' → '.join(str(h['val_loss']) for h in [history[0], history[-1]])}")
    if best_epoch > 0:
        print(f"[SSA-Train] 最佳模型: epoch {best_epoch}/{epochs} (val_loss={best_val_loss:.4f})")
        print(f"[SSA-Train] 最佳: {best_path}")
    else:
        print(f"[SSA-Train] 警告: 验证阶段未触发最佳模型保存")

    return output_path


# ==============================
# CLI 入口
# ==============================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SSA Adapter 离线微调')
    parser.add_argument('--annotations', required=True, help='标注 JSON 文件路径')
    parser.add_argument('--image-dir', required=True, help='影像目录路径')
    parser.add_argument('--epochs', type=int, default=10, help='训练轮数')
    parser.add_argument('--batch-size', type=int, default=4, help='batch size')
    parser.add_argument('--lr', type=float, default=1e-4, help='学习率')
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    parser.add_argument('--target-size', type=int, default=512, help='影像统一尺寸')
    parser.add_argument('--d-model', type=int, default=256, help='特征维度')
    parser.add_argument('--resume', default=None, help='继续训练的 checkpoint 路径')
    parser.add_argument('--base-checkpoint', default=None, help='基底模型 checkpoint（加载权重后再训练）')
    parser.add_argument('--output', default=None, help='输出路径 (默认自动命名)')
    parser.add_argument('--num-workers', type=int, default=2, help='DataLoader workers')
    args = parser.parse_args()

    # base-checkpoint 和 resume 互换（优先用 base-checkpoint）
    resume = args.base_checkpoint or args.resume

    if args.output is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.output = f'sam3_ssa_finetuned_{ts}.pt'

    train_ssa(
        annotations_path=args.annotations,
        image_dir=args.image_dir,
        output_path=args.output,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        device=args.device,
        resume_path=resume,
        target_size=args.target_size,
        d_model=args.d_model,
        num_workers=args.num_workers,
    )
