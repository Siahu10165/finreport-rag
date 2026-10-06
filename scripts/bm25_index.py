"""中文分词（jieba）+ BM25 索引，供建索引与检索共用。

单独成模块有两个原因：
  1. 避免 retriever 与 03_build_index 互相 import
  2. **BM25 类必须定义在这里**——它会被 pickle 进 bm25.pkl，
     pickle 记录的是「模块名.类名」，如果类在 03_build_index 里，
     检索脚本（retriever）反序列化时会报
     `Can't get attribute 'BM25' on <module '__main__'>`
"""
from __future__ import annotations

import numpy as np

# 财报里高频但对检索无意义的词
STOPWORDS = {
    "的", "了", "和", "是", "在", "有", "为", "与", "及", "或", "等",
    "公司", "本", "该", "其", "之", "以", "上", "以下", "包括",
    "报告", "年度", "报告期", "期末", "期初", "单位", "人民币", "元",
    "其中", "我们", "本公司", "集团", "合计", "小计", "其他",
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
}

_jieba_ready = False


def _ensure_jieba():
    global _jieba_ready
    if not _jieba_ready:
        import jieba

        jieba.setLogLevel(60)
        _jieba_ready = True


def tokenize(text: str) -> list[str]:
    """分词并去停用词。英文数字保留（金额、代码要看）。"""
    _ensure_jieba()
    import jieba

    out = []
    for w in jieba.cut(text):
        w = w.strip()
        if not w or w in STOPWORDS:
            continue
        if len(w) < 2 and not w.isdigit():
            continue
        out.append(w)
    return out


class BM25:
    """标准 BM25（k1=1.5, b=0.75），jieba 分词。

    参数取业界默认值：k1 控制词频饱和速度，b 控制文档长度归一化强度。
    财报块长度差异不大（都按 800 字切），所以 b=0.75 影响有限。
    """

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.N = len(corpus_tokens)
        self.doc_len = np.array([len(d) for d in corpus_tokens], dtype=np.float32)
        self.avgdl = float(self.doc_len.mean()) if self.N else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in corpus_tokens:
            counts: dict[str, int] = {}
            for t in doc:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                df[t] = df.get(t, 0) + 1
        self.idf = {
            t: float(np.log(1 + (self.N - c + 0.5) / (c + 0.5)))
            for t, c in df.items()
        }

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        out = np.zeros(self.N, dtype=np.float32)
        if not self.N or not self.avgdl:
            return out
        for t in set(query_tokens):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, counts in enumerate(self.tf):
                f = counts.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                out[i] += idf * (f * (self.k1 + 1)) / denom
        return out


def build_bm25(chunks: list[dict]) -> BM25:
    """用切块的 embed_text 建 BM25（公司名+章节名参与分词，检索时更容易锁定来源）。"""
    corpus = [tokenize(c["embed_text"]) for c in chunks]
    return BM25(corpus)
