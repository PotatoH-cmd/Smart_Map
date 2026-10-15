"""集合差分：把「旧切片指纹集」与「新切片指纹集」分成三集。

  keep   = 旧 ∩ 新   → 内容未变，完全跳过（不重新嵌入）
  insert = 新 - 旧   → 新增或改动后的切片，需插入并向量化
  delete = 旧 - 新   → 源中已消失的切片，需从 docstore 与向量库删除

这是整套增量更新的核心：只有 insert/delete 两集会被真正处理，
文档改动一个切片时，其余切片零成本保留。
"""

from typing import Dict, Iterable, Set


def diff_chunks(old_fps: Iterable[str], new_fps: Iterable[str]) -> Dict[str, Set[str]]:
    """求三集差分。"""
    old = {f for f in (old_fps or ()) if f}
    new = {f for f in (new_fps or ()) if f}
    return {
        "keep": old & new,
        "insert": new - old,
        "delete": old - new,
    }


def summarize_diff(d: Dict[str, Set[str]]) -> str:
    """差分结果的可读摘要，用于 CLI 输出与日志。"""
    return (
        f"keep={len(d.get('keep', ()))} "
        f"insert={len(d.get('insert', ()))} "
        f"delete={len(d.get('delete', ()))}"
    )
