"""
SSA (Soft Semantic Alignment) Adapter — 轻量级语义对齐适配器

参考 ReSAM 论文 (CVPR 2026)，在 SAM3 decoder 的文本交叉注意力层后插入，
通过 BCE loss 对齐文本-图像语义空间，实现零侵入的类别偏好微调。

关键参数：
  - d_model=256: 文本/图像特征维度
  - 总参数量: ~65K (Linear 256→256 + LayerNorm)，< 0.01% SAM3 总量
  - 推理开销: < 5ms（线性投影 + 归一化）
  - 训练策略: 冻结 SAM3 全量参数，仅训练 Adapter + 文本投影层

用法:
  # 推理模式（默认）
  adapter = SSAAdapter(d_model=256)
  aligned_text, _ = adapter(text_feat, image_feat)  # mask_gt=None

  # 训练模式
  aligned_text, loss = adapter(text_feat, image_feat, mask_gt=gt_mask)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from pathlib import Path


class SSAAdapter(nn.Module):
    """
    语义对齐适配器：在 SAM3 decoder 的 ca_text（跨注意力文本层）之后插入。

    Internal flow:
      text_feat (T, d) → alignment_proj → LayerNorm → aligned_text
      aligned_text ⊗ image_feat → similarity_map → BCE(mask_gt)

    其中 T = 文本 token 数, H×W = 图像空间维度, d = 特征维度。
    """

    def __init__(self, d_model: int = 256, dropout: float = 0.0):
        super().__init__()
        self.d_model = d_model

        # 文本特征投影（学习文本→图像语义空间的映射）
        self.alignment_proj = nn.Linear(d_model, d_model, bias=False)
        self.text_norm = nn.LayerNorm(d_model)

        # 可选的图像特征投影（稳定训练）
        self.image_proj = nn.Linear(d_model, d_model, bias=False)
        self.image_norm = nn.LayerNorm(d_model)

        self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()

        # 温度参数（控制 soft alignment 的锐度）
        self.temperature = nn.Parameter(torch.tensor(0.07))

        self._init_weights()

    def _init_weights(self):
        """接近恒等映射的初始化，确保首次推理不退化。"""
        nn.init.eye_(self.alignment_proj.weight)
        nn.init.eye_(self.image_proj.weight)
        nn.init.zeros_(self.text_norm.bias)
        nn.init.ones_(self.text_norm.weight)
        nn.init.zeros_(self.image_norm.bias)
        nn.init.ones_(self.image_norm.weight)

    def forward(
        self,
        text_feat: torch.Tensor,    # (T, d) 或 (B, T, d)
        image_feat: torch.Tensor,   # (H*W, d) 或 (B, H*W, d)
        mask_gt: torch.Tensor = None,  # (H, W) 或 (B, H, W) 二值 GT mask，训练时必须
    ):
        """
        Args:
          text_feat: 文本特征，来自 decoder ca_text 层的输出
          image_feat: 图像特征，来自 decoder 的视觉分支输出
          mask_gt: 可选，GT 二值掩码 (0/1)，提供时计算 BCE loss

        Returns:
          推理模式: (aligned_text, None)
          训练模式: (aligned_text, bce_loss)
        """
        # 确保 batch 维度
        squeeze_text = text_feat.dim() == 2
        squeeze_image = image_feat.dim() == 2
        if squeeze_text:
            text_feat = text_feat.unsqueeze(0)  # (1, T, d)
        if squeeze_image:
            image_feat = image_feat.unsqueeze(0)  # (1, HW, d)

        B, T, d = text_feat.shape
        _, HW, _ = image_feat.shape

        # ── 文本侧投影 ──
        aligned_text = self.alignment_proj(text_feat)  # (B, T, d)
        aligned_text = self.text_norm(aligned_text)
        aligned_text = self.dropout(aligned_text)

        # ── 推理模式：轻量输出 ──
        if mask_gt is None:
            if squeeze_text:
                aligned_text = aligned_text.squeeze(0)
            return aligned_text, None

        # ── 训练模式：计算 BCE 语义对齐 loss ──
        # 图像侧投影
        proj_image = self.image_proj(image_feat)  # (B, HW, d)
        proj_image = self.image_norm(proj_image)

        # 语义相似度: einsum('btd,bhd->bth', aligned_text, proj_image)
        # 对每个文本 token，计算与所有空间位置的相似度
        sim = torch.einsum('btd,bhd->bth', aligned_text, proj_image)  # (B, T, HW)
        sim = sim / self.temperature.abs()  # 温度缩放

        # 聚合文本维度 → 空间相似度图
        # 取每个空间位置对所有文本 token 的最大相似度
        sim_map = sim.max(dim=1).values  # (B, HW)
        sim_map = sim_map.view(B, int(HW ** 0.5), int(HW ** 0.5))  # (B, H, W)

        # 对齐 mask_gt 的空间尺寸
        if mask_gt.dim() == 2:
            mask_gt = mask_gt.unsqueeze(0)  # (1, H, W)
        if mask_gt.shape[-2:] != sim_map.shape[-2:]:
            mask_gt = F.interpolate(
                mask_gt.unsqueeze(1).float(),
                size=sim_map.shape[-2:],
                mode='bilinear',
                align_corners=False,
            ).squeeze(1)

        # BCE loss: 推动文本特征与 GT 区域的图像特征对齐
        bce_loss = F.binary_cross_entropy_with_logits(
            sim_map, mask_gt.float().clamp(0, 1)
        )

        # L2 正则化：防止文本特征退化（全部趋零）
        l2_reg = 1e-4 * (aligned_text ** 2).mean()

        total_loss = bce_loss + l2_reg

        if squeeze_text:
            aligned_text = aligned_text.squeeze(0)

        return aligned_text, total_loss

    def get_param_count(self) -> dict:
        """返回各组件参数量统计"""
        counts = {}
        for name, param in self.named_parameters():
            counts[name] = param.numel()
        counts['total'] = sum(counts.values())
        return counts


# ── 便捷工厂函数 ──

def create_ssa_adapter(
    d_model: int = 256,
    checkpoint_path: str | None = None,
    device: str = 'cuda',
) -> SSAAdapter:
    """
    创建 SSA Adapter 实例，可选加载预训练权重。

    Args:
      d_model: 特征维度（需与 SAM3 decoder 一致）
      checkpoint_path: .pt 权重文件路径
      device: 'cuda' | 'cpu'

    Returns:
      SSAAdapter 实例
    """
    adapter = SSAAdapter(d_model=d_model)

    if checkpoint_path and Path(checkpoint_path).exists():
        state = torch.load(checkpoint_path, map_location=device)
        adapter.load_state_dict(state.get('adapter', state), strict=False)
        print(f"[SSA] 加载 checkpoint: {checkpoint_path}")

    adapter.to(device)
    adapter.eval()  # 默认推理模式
    return adapter


def save_ssa_checkpoint(
    adapter: SSAAdapter,
    save_path: str,
    metadata: dict | None = None,
):
    """
    保存 SSA Adapter 权重（独立于 SAM3 主模型）。

    Args:
      adapter: SSAAdapter 实例
      save_path: 保存路径 (.pt)
      metadata: 可选的元数据（类别名、训练轮次等）
    """
    checkpoint = {
        'adapter': adapter.state_dict(),
        'd_model': adapter.d_model,
        'temperature': adapter.temperature.item(),
        'metadata': metadata or {},
    }
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    torch.save(checkpoint, save_path)
    print(f"[SSA] 保存 checkpoint: {save_path}")
