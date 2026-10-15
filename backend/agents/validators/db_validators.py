"""
db_validators.py — 数据查询类工具校验（postgresql_tool / knowledge_base_tool）。

- check_empty_result：DB 查询 success=True 但 data 为空 → critical（run_engine 触发知识库回退）
- check_aggregate_zero：聚合查询全零 → warning（可能是合法结果，提示即可；逻辑自 _is_empty_aggregate 迁入）
- check_kb_content_empty：知识库检索 content 为空 → critical（无可用知识，触发下游兜底）
"""
from typing import Dict, List

from ..rules_gateway import RISK_CRITICAL, RISK_WARNING, Check


def check_empty_result(result: Dict, state: Dict) -> List[Check]:
    """DB 查询返回 0 行数据。critical：数据不足时应走知识库回退 / 跳过报告。"""
    if not isinstance(result, dict) or not result.get("success"):
        return []
    data = result.get("data")
    if isinstance(data, list) and not data:
        return [Check(
            code="db_empty", name="空结果", passed=False,
            detail="数据库查询返回 0 行数据",
            severity=RISK_CRITICAL,
        )]
    return []


def check_aggregate_zero(result: Dict, state: Dict) -> List[Check]:
    """聚合查询（COUNT/SUM 等）返回的 data 全部为零/空值。

    例如 [{"count": 0}]、[{"total": "0"}]、[{"cnt": None}] 等。
    非字典行不做判断，避免误判（与 _is_empty_aggregate 语义一致）。
    """
    if not isinstance(result, dict) or not result.get("success"):
        return []
    data = result.get("data")
    if not isinstance(data, list) or not data:
        return []
    for row in data:
        if not isinstance(row, dict):
            return []  # 非字典行不加判断，避免误判
        for v in row.values():
            if v is not None and v != 0 and v != "0" and v != "" and v is not False:
                return []  # 存在非零非空的实际值
    return [Check(
        code="agg_zero", name="聚合结果全零", passed=False,
        detail="聚合结果全部为零/空值，可能无有效数据",
        severity=RISK_WARNING,
    )]


def check_kb_content_empty(result: Dict, state: Dict) -> List[Check]:
    """知识库检索成功但无内容。critical：无可用知识。

    判据必须与检索实现对齐：knowledge_base_tool 的检索结果放在 `data`（切片列表），
    **不写** 顶层 `content`。历史上只读 `content` 导致「任何知识库步骤都被判为空」的
    误报（前端时间线出现"校验未通过：知识库检索未返回有效内容"，而实际召回正常）。
    此处与 react_loop._kb_result_empty / tool_node 的空判定语义保持一致：
    data 或 content 任一有实质内容即不算空。
    """
    if not isinstance(result, dict) or not result.get("success"):
        return []
    data = result.get("data")
    if isinstance(data, (list, dict)) and data:
        return []
    if str(result.get("content") or "").strip():
        return []
    return [Check(
        code="kb_empty", name="知识库结果为空", passed=False,
        detail="知识库检索未返回有效内容",
        severity=RISK_CRITICAL,
    )]
