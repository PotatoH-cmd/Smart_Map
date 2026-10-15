"""知识库切片增量更新管线。

把比对单位从「整篇文档」下沉到「单个切片」，用
`章节路径 + 章节号 + 归一化正文` 的指纹做集合差分，
只对真正变化的切片做删除/插入，未变切片原样保留。

模块划分：
  fingerprint  切片指纹与正文归一化
  manifest     kb_documents / kb_chunks 清单表读写
  merger       集合差分（keep / insert / delete）
  staleness    源文件 mtime 与库内版本比对
"""

from .applier import apply_incremental, full_rebuild
from .fingerprint import chunk_fingerprint, normalize_text
from .manifest import Manifest
from .merger import diff_chunks, summarize_diff
from .staleness import scan_stale

__all__ = [
    "chunk_fingerprint",
    "normalize_text",
    "Manifest",
    "diff_chunks",
    "summarize_diff",
    "scan_stale",
    "apply_incremental",
    "full_rebuild",
]
