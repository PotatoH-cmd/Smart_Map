"""通用 grounded（有据）校验：任何"结论/数字"必须能在检索原文中找到出处（L2）。

动机：聚合类问答最容易出现"按可见片段编数"（如只看到 5 个采区就答 5 个）。
本模块提供与领域无关的两项校验：
- sentence_grounded：结论句（或其中足够长的连续片段）确实出现在检索原文中；
- value_grounded：数字 + 单位（单位来自规则声明）确实出现在检索原文中。

调用方（领域 Provider / 汇总节点）在给出确定性结论前先过校验；校验不过即视为无据，
返回"未找到明确表述"，而不是放任编造。
"""
import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]{1,20}>")
_WS_RE = re.compile(r"[\s\u3000]+")
# 中英文标点/全角半角差异在"原句比对"时归一，避免因标点或空格差异误判未命中
_PUNCT_RE = re.compile(r"[，。；、,.;:：!！?？\"'“”‘’()（）\[\]【】<>《》\-—~·]")

_MIN_FRAG = 8
_WINDOW = 28


def normalize(text: Any) -> str:
    """检索原文/结论句的归一化形式：去 HTML 标签、去空白、统一标点。"""
    s = _TAG_RE.sub("", str(text or ""))
    s = _WS_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return s


def corpus_blob(texts: Iterable[Any]) -> str:
    return "".join(normalize(t) for t in (texts or []))


def sentence_grounded(sentence: str, corpus_texts: Iterable[Any], *, min_frag: int = _MIN_FRAG) -> bool:
    """结论句是否在检索原文中有据。

    先整体比对；不中（常见于片段被截断或跨切片拼接）则用足够长的连续窗口回退比对。
    """
    norm = normalize(sentence)
    if not norm:
        return False
    blob = corpus_blob(corpus_texts)
    if not blob:
        return False
    if norm in blob:
        return True
    if len(norm) < min_frag:
        return False
    window = min(_WINDOW, len(norm))
    while window >= min_frag:
        for i in range(0, len(norm) - window + 1):
            if norm[i:i + window] in blob:
                return True
        window -= 6
    return False


def value_grounded(value: Any, units: Iterable[Any], corpus_texts: Iterable[Any]) -> bool:
    """数字 + 单位（单位由规则声明）是否在检索原文中有据。

    与 kb_rules.build_unit_pattern 同源的负向断言：数字前一位不得为字母/数字/点/连字符，
    否则编号类文本（如 "SH5 采区"、"图 5.6-55 采区"）会被误判为"5 个采区"而放行编造值。
    """
    try:
        val = str(int(value))
    except (TypeError, ValueError):
        return False
    clean = [str(u) for u in (units or []) if str(u).strip()]
    if not clean:
        return False
    alt = "|".join(re.escape(u) for u in clean)
    blob = corpus_blob(corpus_texts)
    if not blob:
        return False
    return re.search(rf"(?<![A-Za-z0-9.\-]){re.escape(val)}\s*个?\s*(?:{alt})", blob) is not None


def verify_conclusion(total: Optional[int], units: Iterable[str], evidence: str,
                      corpus_texts: Iterable[Any]) -> bool:
    """结论整体校验：数字有据 或 结论句有据，即认为可给出确定性回答。"""
    if total is None:
        return False
    return value_grounded(total, units, corpus_texts) or sentence_grounded(evidence, corpus_texts)
