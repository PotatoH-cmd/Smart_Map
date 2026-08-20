"""
ReSAM 组合损失函数

对应 ReSAM (CVPR 2026) 论文的多任务损失设计：
  L_total = w_focal * Focal + w_dice * Dice + w_iou * IoU + w_ssa * SSA

各项分工：
  - Focal Loss: 处理正负样本极度不平衡（遥感场景前景占比常<5%）
  - Dice Loss: 区域级重叠度，不受类别不平衡影响
  - IoU Loss: 直接优化 mask 质量度量（可微分近似）
  - SSA Loss: 语义对齐对比损失（来自 resam_ssa.py）

用法:
  criterion = ReSAMLoss(focal_weight=20.0, dice_weight=1.0, iou_weight=1.0, ssa_weight=0.5)
  loss = criterion(pred_mask, gt_mask, ssa_loss)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class FocalLoss(nn.Module):
    """
    Focal Loss: 聚焦难分样本，降低易分样本的权重。
    FL(p_t) = -alpha_t * (1 - p_t)^gamma * log(p_t)

    在遥感分割中，目标区域通常只占图像的 1~5%，
    focal loss 通过 gamma 参数自动降低大量背景像素的贡献。
    """

    def __init__(self, alpha: float = 0.25, gamma: float = 2.0):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """
        Args:
            pred: (B, H, W) 预测 logits（未经 sigmoid）
            target: (B, H, W) GT 二值 mask (0/1)
        """
        pred = pred.float()
        target = target.float()

        # BCE with logits
        bce = F.binary_cross_entropy_with_logits(pred, target, reduction='none')

        # 计算 p_t
        pred_prob = torch.sigmoid(pred)
        p_t = pred_prob * target + (1 - pred_prob) * (1 - target)

        # alpha 权重
        alpha_t = self.alpha * target + (1 - self.alpha) * (1 - target)

        # focal 权重
        focal_weight = alpha_t * (1 - p_t) ** self.gamma

        loss = focal_weight * bce
        return loss.mean()


class DiceLoss(nn.Module):
    """
    Dice Loss: 基于区域重叠度的损失。
    DL = 1 - (2 * |pred ∩ target|) / (|pred| + |target|)

    优点：不受类别不平衡影响，直接优化 F1 分数。
    """

    def __init__(self, smooth: float = 1.0):
        super().__init__()
        self.smooth = smooth

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """
        Args:
            pred: (B, H, W) 预测 logits
            target: (B, H, W) GT mask
        """
        pred_prob = torch.sigmoid(pred).flatten(1)  # (B, H*W)
        target_flat = target.float().flatten(1)     # (B, H*W)

        intersection = (pred_prob * target_flat).sum(dim=1)
        union = pred_prob.sum(dim=1) + target_flat.sum(dim=1)

        dice = (2.0 * intersection + self.smooth) / (union + self.smooth)
        return (1 - dice).mean()


class IoULoss(nn.Module):
    """
    IoU Loss (Jaccard Loss): 直接优化交并比。
    IoU = |pred ∩ target| / |pred ∪ target|
    Loss = 1 - IoU

    比 Dice 更严格，对小目标更敏感。
    """

    def __init__(self, smooth: float = 1.0):
        super().__init__()
        self.smooth = smooth

    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """
        Args:
            pred: (B, H, W) 预测 logits
            target: (B, H, W) GT mask
        """
        pred_prob = torch.sigmoid(pred).flatten(1)
        target_flat = target.float().flatten(1)

        intersection = (pred_prob * target_flat).sum(dim=1)
        total = pred_prob.sum(dim=1) + target_flat.sum(dim=1)
        union = total - intersection

        iou = (intersection + self.smooth) / (union + self.smooth)
        return (1 - iou).mean()


class ReSAMLoss(nn.Module):
    """
    ReSAM 组合损失：Focal + Dice + IoU + SSA

    默认权重参考 ReSAM 论文实验设置：
      - focal_weight=20.0 (主要监督信号)
      - dice_weight=1.0
      - iou_weight=1.0
      - ssa_weight=0.5 (辅助对齐信号)
    """

    def __init__(
        self,
        focal_alpha: float = 0.25,
        focal_gamma: float = 2.0,
        focal_weight: float = 20.0,
        dice_weight: float = 1.0,
        iou_weight: float = 1.0,
        ssa_weight: float = 0.5,
    ):
        super().__init__()
        self.focal_loss = FocalLoss(alpha=focal_alpha, gamma=focal_gamma)
        self.dice_loss = DiceLoss()
        self.iou_loss = IoULoss()

        self.focal_weight = focal_weight
        self.dice_weight = dice_weight
        self.iou_weight = iou_weight
        self.ssa_weight = ssa_weight

    def forward(
        self,
        pred_mask: torch.Tensor,
        gt_mask: torch.Tensor,
        ssa_loss: torch.Tensor = None,
    ) -> dict:
        """
        计算组合损失。

        Args:
            pred_mask: (B, H, W) 预测 mask logits
            gt_mask: (B, H, W) GT 二值 mask
            ssa_loss: 标量，来自 ReSAMSSA 模块的对比损失（可选）

        Returns:
            dict: {
                'total': 总损失,
                'focal': focal 分量,
                'dice': dice 分量,
                'iou': iou 分量,
                'ssa': ssa 分量,
            }
        """
        # 确保 gt_mask 尺寸与 pred 一致
        if gt_mask.shape[-2:] != pred_mask.shape[-2:]:
            gt_mask = F.interpolate(
                gt_mask.unsqueeze(1).float(),
                size=pred_mask.shape[-2:],
                mode='nearest',
            ).squeeze(1)

        focal = self.focal_loss(pred_mask, gt_mask)
        dice = self.dice_loss(pred_mask, gt_mask)
        iou = self.iou_loss(pred_mask, gt_mask)

        total = (
            self.focal_weight * focal
            + self.dice_weight * dice
            + self.iou_weight * iou
        )

        ssa_val = torch.tensor(0.0, device=pred_mask.device)
        if ssa_loss is not None:
            ssa_val = ssa_loss
            total = total + self.ssa_weight * ssa_val

        return {
            'total': total,
            'focal': focal.detach(),
            'dice': dice.detach(),
            'iou': iou.detach(),
            'ssa': ssa_val.detach() if ssa_val.requires_grad else ssa_val,
        }
