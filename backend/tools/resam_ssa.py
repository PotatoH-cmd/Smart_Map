"""
ReSAM SSA (Soft Semantic Alignment) — FIFO 队列对比学习模块

对应 ReSAM (CVPR 2026) 论文中的 Reinforce 阶段：
通过维护 per-class 特征队列（FIFO），使用余弦相似度对比损失
推动同类像素特征聚集、异类特征远离，实现语义空间对齐。

与旧版 ssa_adapter.py 的区别：
  - 旧版: BCE + einsum 空间对齐 → 单一 loss，无特征记忆
  - 新版: FIFO 队列 + InfoNCE 对比学习 → 跨 batch 累积知识，更稳定

用法:
  ssa = ReSAMSSA(d_model=256, queue_size=256, num_classes=5)
  ssa.to(device)

  # 训练时
  loss = ssa(image_features, mask_gt, class_ids)

  # 推理时不使用此模块（LoRA 已内化特征优化）
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import deque
from typing import Optional


class ReSAMSSA(nn.Module):
    """
    Soft Semantic Alignment — FIFO 队列 + 余弦相似度对比学习

    维护 per-class 的特征原型队列，通过 InfoNCE 对比损失：
    - 正对: 当前样本特征 vs 同类队列中的原型
    - 负对: 当前样本特征 vs 异类队列中的原型

    这样每个 batch 不仅从当前数据学习，还利用了历史 batch 的特征记忆。
    """

    def __init__(
        self,
        d_model: int = 256,
        queue_size: int = 256,
        temperature: float = 0.07,
        num_classes: int = 10,
        proj_dim: int = 128,
    ):
        """
        Args:
            d_model: 输入特征维度（需与 SAM3 decoder 输出一致）
            queue_size: 每个类别的 FIFO 队列长度
            temperature: 对比学习温度参数（越小越锐利）
            num_classes: 最大类别数
            proj_dim: 投影头输出维度
        """
        super().__init__()
        self.d_model = d_model
        self.queue_size = queue_size
        self.temperature = temperature
        self.num_classes = num_classes
        self.proj_dim = proj_dim

        # 投影头: 将特征投影到对比学习空间
        self.projector = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(inplace=True),
            nn.Linear(d_model, proj_dim),
        )

        # 初始化权重
        self._init_weights()

        # FIFO 队列（非参数，不参与梯度计算）
        # 每个类别维护一个固定长度的特征队列
        self.register_buffer(
            '_queues',
            torch.zeros(num_classes, queue_size, proj_dim),
        )
        self.register_buffer(
            '_queue_ptrs',
            torch.zeros(num_classes, dtype=torch.long),
        )
        self.register_buffer(
            '_queue_sizes',
            torch.zeros(num_classes, dtype=torch.long),
        )

    def _init_weights(self):
        """初始化投影头"""
        for m in self.projector.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

    @torch.no_grad()
    def _enqueue(self, features: torch.Tensor, class_id: int):
        """
        将特征原型入队（FIFO）。

        Args:
            features: (N, proj_dim) 特征向量
            class_id: 类别索引
        """
        batch_size = features.shape[0]
        ptr = int(self._queue_ptrs[class_id].item())

        # 循环写入
        for i in range(batch_size):
            self._queues[class_id, ptr] = features[i]
            ptr = (ptr + 1) % self.queue_size

        self._queue_ptrs[class_id] = ptr
        self._queue_sizes[class_id] = min(
            self._queue_sizes[class_id] + batch_size,
            self.queue_size
        )

    def _get_queue(self, class_id: int) -> Optional[torch.Tensor]:
        """获取指定类别的有效队列特征"""
        size = int(self._queue_sizes[class_id].item())
        if size == 0:
            return None
        return self._queues[class_id, :size]  # (size, proj_dim)

    def forward(
        self,
        image_features: torch.Tensor,
        mask_gt: torch.Tensor,
        class_ids: torch.Tensor,
    ) -> torch.Tensor:
        """
        计算 SSA 对比损失。

        Args:
            image_features: (B, HW, d_model) 图像特征图（来自 SAM encoder/decoder）
            mask_gt: (B, H, W) 二值 GT mask
            class_ids: (B,) 每个样本的类别 ID

        Returns:
            ssa_loss: 标量损失
        """
        B, HW, d = image_features.shape
        H = W = int(HW ** 0.5)

        # reshape 特征图为空间格式
        feat_map = image_features.view(B, H, W, d)

        total_loss = torch.tensor(0.0, device=image_features.device)
        valid_count = 0

        for i in range(B):
            cls_id = int(class_ids[i].item())

            # 调整 mask 尺寸匹配特征图
            mask_i = mask_gt[i]  # (H_orig, W_orig)
            if mask_i.shape != (H, W):
                mask_i = F.interpolate(
                    mask_i.float().unsqueeze(0).unsqueeze(0),
                    size=(H, W), mode='nearest'
                ).squeeze()

            mask_bool = mask_i > 0.5

            # 提取正样本（前景区域）和负样本（背景区域）特征
            pos_pixels = feat_map[i][mask_bool]     # (N_pos, d)
            neg_pixels = feat_map[i][~mask_bool]    # (N_neg, d)

            if pos_pixels.shape[0] < 1:
                continue

            # 计算类别原型（全局平均池化）
            pos_prototype = pos_pixels.mean(dim=0, keepdim=True)  # (1, d)

            # 投影到对比空间
            pos_proj = self.projector(pos_prototype)  # (1, proj_dim)
            pos_proj = F.normalize(pos_proj, dim=-1)

            # 获取同类队列（正对）
            pos_queue = self._get_queue(cls_id)

            # 获取异类队列（负对）
            neg_projs = []
            for other_cls in range(self.num_classes):
                if other_cls == cls_id:
                    continue
                q = self._get_queue(other_cls)
                if q is not None:
                    neg_projs.append(q)

            # 需要至少有正队列或负队列才能计算对比损失
            if pos_queue is None and len(neg_projs) == 0:
                # 队列为空（训练初期），只入队不计算损失
                self._enqueue(pos_proj.detach(), cls_id)
                continue

            # ── InfoNCE 损失 ──
            # 正对相似度
            if pos_queue is not None and pos_queue.shape[0] > 0:
                pos_sim = torch.mm(pos_proj, pos_queue.t()) / self.temperature  # (1, K_pos)
            else:
                pos_sim = torch.zeros(1, 1, device=image_features.device)

            # 负对相似度
            if len(neg_projs) > 0:
                neg_features = torch.cat(neg_projs, dim=0)  # (K_neg, proj_dim)
                neg_sim = torch.mm(pos_proj, neg_features.t()) / self.temperature  # (1, K_neg)
            else:
                neg_sim = torch.zeros(1, 1, device=image_features.device)

            # 组合: log(exp(pos) / (exp(pos) + sum(exp(neg))))
            if pos_queue is not None and pos_queue.shape[0] > 0:
                # 取正对中最相似的作为 anchor
                pos_logit = pos_sim.max(dim=-1).values  # (1,)
                all_logits = torch.cat([pos_sim, neg_sim], dim=-1)  # (1, K_pos + K_neg)
                # InfoNCE: -log(exp(pos_max) / sum(exp(all)))
                loss_i = -pos_logit + torch.logsumexp(all_logits, dim=-1)
                total_loss = total_loss + loss_i.mean()
                valid_count += 1

            # 入队（用 detach 避免梯度流向队列）
            self._enqueue(pos_proj.detach(), cls_id)

            # 负样本原型也入队（作为背景类）
            if neg_pixels.shape[0] > 10:
                neg_prototype = neg_pixels.mean(dim=0, keepdim=True)
                neg_proj_bg = self.projector(neg_prototype)
                neg_proj_bg = F.normalize(neg_proj_bg, dim=-1)
                # 背景作为额外类入队（用最后一个 slot）
                bg_cls = min(cls_id + self.num_classes // 2, self.num_classes - 1)
                if bg_cls != cls_id:
                    self._enqueue(neg_proj_bg.detach(), bg_cls)

        if valid_count > 0:
            return total_loss / valid_count
        else:
            return torch.tensor(0.0, device=image_features.device, requires_grad=True)

    def reset_queues(self):
        """重置所有队列（新的训练 session 时调用）"""
        self._queues.zero_()
        self._queue_ptrs.zero_()
        self._queue_sizes.zero_()

    def get_queue_stats(self) -> dict:
        """获取队列使用统计"""
        stats = {}
        for cls in range(self.num_classes):
            size = int(self._queue_sizes[cls].item())
            if size > 0:
                stats[cls] = size
        return stats
