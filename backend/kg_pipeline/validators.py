# -*- coding: utf-8 -*-
"""知识图谱产物一致性校验。

在 build 阶段读取抽取产物，检查：

  1. 孤儿节点：MineableArea 无任何关系（告警级，保留——边界采区可能确实
     只有位置描述）
  2. 多值归属：Area → 多个 County（需消歧，规则侧已按邻近窗口消歧）
  3. PG ↔ KB 归一化合并：传入 alias 校验两侧同名采区是否真的合并为单一节点
  4. 溯源完整性：统计各类型节点 refs 覆盖率（用户验收标准之一）

返回 {"warnings": [...], "refs_coverage": {...}, "ok": bool}
"""

import logging

from .refs import parse_refs

logger = logging.getLogger(__name__)


def validate_artifacts(nodes: dict, relations: list, alias: dict = None) -> dict:
    warnings = []

    rels = relations if isinstance(relations, list) else []
    area_ids = set(nodes.get("MineableArea", {}).keys())

    # ── 1. 孤儿采区 ──
    rel_members = set()
    for rel in rels:
        for side in ("src", "dst"):
            val = rel.get(side)
            if val in area_ids:
                rel_members.add(val)
    orphan = area_ids - rel_members
    if orphan:
        warnings.append(
            f"孤儿采区节点 {len(orphan)} 个（无任何关系），示例: {sorted(orphan)[:5]}"
        )

    # ── 2. 多值归属 ──
    area_county = {}
    for rel in rels:
        if rel.get("rel") == "BELONGS_TO" and rel.get("src") in area_ids:
            area_county.setdefault(rel["src"], set()).add(rel.get("dst"))
    multi = {a: sorted(cs) for a, cs in area_county.items() if len(cs) > 1}
    if multi:
        warnings.append(
            f"Area 多值归属 {len(multi)} 个（示例: {dict(list(multi.items())[:3])}）"
        )

    # ── 3. PG↔KB 归一化合并校验（同采区不允许产生重复节点）──
    if alias:
        buckets = {}
        for aid in area_ids:
            buckets.setdefault(alias.get(aid, aid), set()).add(aid)
        dup = {k: v for k, v in buckets.items() if len(v) > 1}
        if dup:
            warnings.append(
                f"同采区未合并 {len(dup)} 组（alias 未生效？示例: {dict(list(dup.items())[:3])}）"
            )

    # ── 4. 溯源覆盖率 ──
    coverage = {}
    for ntype, bucket in nodes.items():
        refs = sum(1 for n in bucket.values() if parse_refs(n.get("refs", "")))
        coverage[ntype] = (refs, len(bucket))

    ok = not any("未合并" in w for w in warnings)
    return {"warnings": warnings, "refs_coverage": coverage, "ok": ok}


def format_report(result: dict) -> str:
    """把校验结果渲染成可读报告（供 CLI 打印）。"""
    lines = ["── 溯源覆盖率 ──"]
    for ntype, (refs, total) in result["refs_coverage"].items():
        pct = (refs / total * 100) if total else 0.0
        lines.append(f"  {ntype:<14} {refs}/{total}  ({pct:.0f}%)")
    if result["warnings"]:
        lines.append("── 告警 ──")
        lines += [f"  ! {w}" for w in result["warnings"]]
    else:
        lines.append("── 告警 ──\n  无")
    return "\n".join(lines)
