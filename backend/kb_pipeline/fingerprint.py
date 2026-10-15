"""切片指纹：判定「同一片内容」的唯一依据。

指纹 = sha256(章节路径 + 章节号 + 归一化正文)[:16]

为什么把章节路径纳入指纹：同一段文字被挪到另一章属于真实变更，
只对正文取 hash 会误判为「未变」。

为什么正文要归一化：连续空白折叠为单空格、去首尾空白，
避免纯格式调整触发无谓的重新嵌入。
"""

import hashlib
import re

_WS = re.compile(r"\s+")

# 指纹长度：16 位 hex（64 bit）。987 量级下碰撞概率可忽略。
FP_LEN = 16


def normalize_text(text: str) -> str:
    """正文归一化：折叠连续空白、去首尾空白。"""
    return _WS.sub(" ", (text or "").strip())


def chunk_fingerprint(breadcrumb: str, section_no: str, text: str) -> str:
    """计算切片指纹。

    三段用 \\x1f（单元分隔符）拼接，避免拼接歧义
    （如 breadcrumb="a" + section_no="bc" 与 "ab" + "c" 撞车）。
    """
    raw = f"{breadcrumb or ''}\x1f{section_no or ''}\x1f{normalize_text(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:FP_LEN]


def fingerprint_of_meta(meta: dict, text: str) -> str:
    """按节点 meta 计算指纹，供 backfill 与 ingest 共用。"""
    meta = meta or {}
    return chunk_fingerprint(
        meta.get("breadcrumb", ""), str(meta.get("section_no", "")), text
    )
