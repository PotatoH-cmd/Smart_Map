# -*- coding: utf-8 -*-
"""jieba + BM25 稀疏向量编码器 — 为 Milvus SPARSE_FLOAT_VECTOR 生成词权重。

Milvus 2.4 没有内置 BM25 Function（2.5 才引入），且内置分析器对中文支持有限，
因此采用「客户端 jieba 分词 + BM25 权重 → 稀疏向量 → SPARSE_INVERTED_INDEX」方案：

- 文档侧权重 = tf 饱和项 × sqrt(idf)，查询侧权重 = sqrt(idf)，
  两侧内积 ≈ BM25 打分（idf 平方根对称分配，避免大语料数值溢出）。
- token → uint32 下标：crc32(token) & 0xFFFFFF（2^24 桶，冲突率可忽略）。
- idf / avgdl 依赖全语料统计（df、N、avgdl），fit() 后缓存为 JSON；
  增量文档复用缓存统计，检索语义与原先 llama_index 的 BM25Retriever 对齐
  （同为 jieba 分词 + BM25 打分）。
"""

import json
import logging
import math
import os
import re
import zlib
from collections import Counter
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# token → 稀疏向量下标的桶数量（2^24）
_IDX_MASK = (1 << 24) - 1

# 词内须至少含一个「词字符」（中文/字母/数字），纯标点与空白 token 丢弃
_HAS_WORDCHAR = re.compile(r"\w", re.UNICODE)

# jieba 不可用时的兜底切分：连续字母数字 或 单个中文字
_FALLBACK_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


class BM25SparseEncoder:
    """BM25 稀疏向量编码器（文档侧/查询侧对称接口）。

    用法：
        enc = BM25SparseEncoder(stats_path)
        if not enc.load():          # 缓存不存在时用全语料拟合一次
            enc.fit(all_texts)      # 内部 save()
        doc_vec = enc.encode_document(text)   # {token_id: weight}
        qry_vec = enc.encode_query(question)
    """

    def __init__(self, stats_path: Optional[str] = None, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.stats_path = stats_path or self.default_stats_path()
        self._df: Dict[str, int] = {}
        self._n = 0
        self._avgdl = 1.0
        self._jieba = None  # 延迟加载

    # ── 路径与拟合状态 ──

    @staticmethod
    def default_stats_path() -> str:
        return os.environ.get(
            "KB_BM25_STATS_PATH",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bm25_stats.json"),
        )

    @property
    def is_fitted(self) -> bool:
        return self._n > 0 and bool(self._df)

    # ── 分词 ──

    def _tokenizer(self):
        if self._jieba is None:
            try:
                import jieba

                self._jieba = jieba
            except ImportError:
                logger.warning("[BM25] jieba 不可用，使用字符级兜底切分")
                self._jieba = False
        return self._jieba

    def _cut(self, text: str) -> list:
        if not text:
            return []
        jb = self._tokenizer()
        if jb:
            raw = jb.lcut(text)
        else:
            raw = _FALLBACK_TOKEN.findall(text)
        return [t.strip().lower() for t in raw if isinstance(t, str) and _HAS_WORDCHAR.search(t)]

    # ── 全语料统计（df / N / avgdl） ──

    def fit(self, texts: Iterable[str], save: bool = True) -> "BM25SparseEncoder":
        """扫描全语料统计 df/avgdl；文本量级小（千级切片），一次性内存统计即可。"""
        df: Dict[str, int] = {}
        n = 0
        total_len = 0
        for text in texts:
            tokens = self._cut(text or "")
            if not tokens:
                continue
            n += 1
            total_len += len(tokens)
            for tok in set(tokens):
                df[tok] = df.get(tok, 0) + 1
        self._df = df
        self._n = n
        self._avgdl = (total_len / n) if n else 1.0
        logger.info(f"[BM25] 语料拟合完成：{n} 篇，词表 {len(df)}，平均长度 {self._avgdl:.1f}")
        if save:
            self.save()
        return self

    def load(self) -> bool:
        """从缓存加载统计；失败返回 False（调用方回退 fit）。"""
        try:
            with open(self.stats_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._df = data.get("df") or {}
            self._n = int(data.get("n") or 0)
            self._avgdl = float(data.get("avgdl") or 1.0)
            self.k1 = float(data.get("k1", self.k1))
            self.b = float(data.get("b", self.b))
            if self.is_fitted:
                logger.info(f"[BM25] 统计缓存已加载：{self._n} 篇，词表 {len(self._df)}（{self.stats_path}）")
                return True
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.warning(f"[BM25] 统计缓存加载失败（将重新拟合）: {e}")
        return False

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.stats_path), exist_ok=True)
            tmp = self.stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"n": self._n, "avgdl": self._avgdl, "k1": self.k1, "b": self.b, "df": self._df},
                    f, ensure_ascii=False,
                )
            os.replace(tmp, self.stats_path)
        except Exception as e:
            logger.warning(f"[BM25] 统计缓存保存失败: {e}")

    # ── 编码 ──

    @staticmethod
    def _token_id(token: str) -> int:
        return zlib.crc32(token.encode("utf-8")) & _IDX_MASK

    def _idf(self, token: str) -> float:
        if not self.is_fitted:
            return 1.0  # 未拟合时退化为纯 tf 权重，保证可用性
        df = self._df.get(token, 0)
        n = self._n
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def encode_document(self, text: str) -> Dict[int, float]:
        """文档侧：tf 饱和项 × sqrt(idf)，内积的一半因子。"""
        tokens = self._cut(text or "")
        if not tokens:
            return {}
        tf = Counter(tokens)
        dl = len(tokens)
        avgdl = self._avgdl if self._avgdl > 0 else 1.0
        denom_base = self.k1 * (1.0 - self.b + self.b * dl / avgdl)
        out: Dict[int, float] = {}
        for tok, cnt in tf.items():
            tf_norm = cnt * (self.k1 + 1.0) / (cnt + denom_base)
            w = tf_norm * math.sqrt(self._idf(tok))
            if w > 0:
                idx = self._token_id(tok)
                out[idx] = out.get(idx, 0.0) + w
        return out

    def encode_query(self, text: str) -> Dict[int, float]:
        """查询侧：sqrt(idf)（去重后的词各计一次）。"""
        tokens = set(self._cut(text or ""))
        out: Dict[int, float] = {}
        for tok in tokens:
            w = math.sqrt(self._idf(tok))
            if w > 0:
                idx = self._token_id(tok)
                out[idx] = out.get(idx, 0.0) + w
        return out
