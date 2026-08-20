"""
ReSAM LoRA (Low-Rank Adaptation) 模块

对应 ReSAM (CVPR 2026) 论文中的 Refine 阶段：
在 SAM3 ViT 编码器的 Attention 层注入低秩适配分支，
以极少量可训练参数（<1% 主模型）实现领域微调。

目标注入层（vitdet.py）:
  - Attention.qkv (Linear dim → dim*3)
  - Attention.proj (Linear dim → dim)

用法:
  from resam_lora import inject_lora, extract_lora_state_dict, load_lora_state_dict

  # 注入 LoRA
  lora_params = inject_lora(model, rank=4, alpha=1.0)

  # 训练后导出
  lora_sd = extract_lora_state_dict(model)
  torch.save(lora_sd, 'lora_weights.pt')

  # 推理时加载
  load_lora_state_dict(model, torch.load('lora_weights.pt'))
"""

import math
from typing import List, Dict, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F


class LoRALinear(nn.Module):
    """
    在原始 Linear 层旁注入低秩分支。
    输出 = original_linear(x) + scale * (x @ A^T) @ B^T

    参数量: rank * (in_features + out_features) ≈ 2 * rank * dim
    例: dim=1024, rank=4 → 每层 8192 参数（原层 1M+ 参数的 <1%）
    """

    def __init__(self, original_linear: nn.Linear, rank: int = 4, alpha: float = 1.0):
        super().__init__()
        self.original_linear = original_linear
        self.rank = rank
        self.alpha = alpha
        self.scale = alpha / rank

        in_features = original_linear.in_features
        out_features = original_linear.out_features

        # A: 降维矩阵 (in_features → rank)，kaiming 初始化
        self.lora_A = nn.Parameter(torch.empty(rank, in_features))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))

        # B: 升维矩阵 (rank → out_features)，零初始化确保初始输出不变
        self.lora_B = nn.Parameter(torch.zeros(out_features, rank))

        # 冻结原始权重
        self.original_linear.weight.requires_grad = False
        if self.original_linear.bias is not None:
            self.original_linear.bias.requires_grad = False

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 原始输出
        out = self.original_linear(x)
        # LoRA 分支: x @ A^T @ B^T * scale
        lora_out = F.linear(F.linear(x, self.lora_A), self.lora_B) * self.scale
        return out + lora_out

    def extra_repr(self) -> str:
        return (
            f"in={self.original_linear.in_features}, "
            f"out={self.original_linear.out_features}, "
            f"rank={self.rank}, alpha={self.alpha}"
        )


def inject_lora(
    model: nn.Module,
    rank: int = 4,
    alpha: float = 1.0,
    target_modules: Optional[List[str]] = None,
) -> List[nn.Parameter]:
    """
    遍历模型，在 ViT Attention 层的 qkv 和 proj 注入 LoRA。

    Args:
        model: SAM3 完整模型 (Sam3Image)
        rank: LoRA 秩（推荐 4~8）
        alpha: 缩放因子
        target_modules: 目标模块名列表，默认 ['qkv', 'proj']

    Returns:
        所有 LoRA 参数列表（用于 optimizer）
    """
    if target_modules is None:
        target_modules = ['qkv', 'proj']

    lora_params = []
    injected_count = 0

    # 遍历所有模块，找到 Attention 层中的目标 Linear
    for name, module in model.named_modules():
        # 检查是否是我们要注入的 Linear 层
        for target in target_modules:
            if name.endswith(f'.attn.{target}') or name.endswith(f'.{target}'):
                # 确认父模块名包含 attn 相关特征
                parent_name = name.rsplit('.', 1)[0] if '.' in name else ''
                attr_name = name.rsplit('.', 1)[-1]

                # 获取父模块
                parent = model
                if parent_name:
                    for part in parent_name.split('.'):
                        parent = getattr(parent, part)

                # 确认是 Linear 层
                original_linear = getattr(parent, attr_name)
                if not isinstance(original_linear, nn.Linear):
                    continue

                # 替换为 LoRALinear
                lora_linear = LoRALinear(original_linear, rank=rank, alpha=alpha)
                setattr(parent, attr_name, lora_linear)

                # 收集 LoRA 参数
                lora_params.extend([lora_linear.lora_A, lora_linear.lora_B])
                injected_count += 1

    print(f"[ReSAM-LoRA] 注入完成: {injected_count} 层, rank={rank}, alpha={alpha}")
    print(f"[ReSAM-LoRA] LoRA 参数量: {sum(p.numel() for p in lora_params):,}")
    return lora_params


def inject_lora_to_vit(
    vit_module: nn.Module,
    rank: int = 4,
    alpha: float = 1.0,
) -> List[nn.Parameter]:
    """
    直接对 ViT 模块注入 LoRA（更精确的注入方式）。
    适用于已提取出 backbone.vision_backbone 的场景。

    遍历 ViT 的所有 Block，找到 attn.qkv 和 attn.proj 进行替换。
    """
    lora_params = []
    injected_count = 0

    for name, module in vit_module.named_modules():
        # 匹配 blocks.X.attn.qkv 或 blocks.X.attn.proj
        if isinstance(module, nn.Linear):
            parts = name.split('.')
            if len(parts) >= 2 and parts[-2] == 'attn' and parts[-1] in ('qkv', 'proj'):
                # 获取父 attn 模块
                parent_path = '.'.join(parts[:-1])
                parent = vit_module
                for p in parent_path.split('.'):
                    parent = getattr(parent, p)

                attr_name = parts[-1]
                original_linear = getattr(parent, attr_name)

                # 替换
                lora_linear = LoRALinear(original_linear, rank=rank, alpha=alpha)
                setattr(parent, attr_name, lora_linear)

                lora_params.extend([lora_linear.lora_A, lora_linear.lora_B])
                injected_count += 1

    print(f"[ReSAM-LoRA] ViT 注入完成: {injected_count} 层, rank={rank}")
    total_params = sum(p.numel() for p in lora_params)
    print(f"[ReSAM-LoRA] LoRA 参数量: {total_params:,} ({total_params/1e6:.2f}M)")
    return lora_params


def extract_lora_state_dict(model: nn.Module) -> Dict[str, torch.Tensor]:
    """
    从模型中提取所有 LoRA 权重。

    Returns:
        只包含 LoRA 参数的 state_dict（key 格式: 原始路径.lora_A / .lora_B）
    """
    lora_state = {}
    for name, module in model.named_modules():
        if isinstance(module, LoRALinear):
            lora_state[f"{name}.lora_A"] = module.lora_A.data.clone()
            lora_state[f"{name}.lora_B"] = module.lora_B.data.clone()
            lora_state[f"{name}.rank"] = torch.tensor(module.rank)
            lora_state[f"{name}.alpha"] = torch.tensor(module.alpha)
    return lora_state


def load_lora_state_dict(model: nn.Module, lora_state: Dict[str, torch.Tensor]) -> int:
    """
    将 LoRA 权重加载到已注入 LoRA 的模型中。
    如果模型尚未注入 LoRA，会自动注入。

    Args:
        model: 目标模型
        lora_state: extract_lora_state_dict 的输出

    Returns:
        成功加载的层数
    """
    # 检查是否已注入 LoRA
    has_lora = any(isinstance(m, LoRALinear) for m in model.modules())

    if not has_lora:
        # 从 state_dict 推断 rank
        ranks = [v.item() for k, v in lora_state.items() if k.endswith('.rank')]
        rank = int(ranks[0]) if ranks else 4
        alphas = [v.item() for k, v in lora_state.items() if k.endswith('.alpha')]
        alpha = float(alphas[0]) if alphas else 1.0
        inject_lora(model, rank=rank, alpha=alpha)

    loaded = 0
    for name, module in model.named_modules():
        if isinstance(module, LoRALinear):
            a_key = f"{name}.lora_A"
            b_key = f"{name}.lora_B"
            if a_key in lora_state and b_key in lora_state:
                module.lora_A.data.copy_(lora_state[a_key])
                module.lora_B.data.copy_(lora_state[b_key])
                loaded += 1

    print(f"[ReSAM-LoRA] 加载完成: {loaded} 层")
    return loaded


def get_lora_param_count(model: nn.Module) -> dict:
    """统计 LoRA 参数量"""
    total = 0
    layer_count = 0
    for module in model.modules():
        if isinstance(module, LoRALinear):
            total += module.lora_A.numel() + module.lora_B.numel()
            layer_count += 1
    return {
        'total_params': total,
        'layer_count': layer_count,
        'total_mb': total * 4 / 1024 / 1024,  # float32
    }
