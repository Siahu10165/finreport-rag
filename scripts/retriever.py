"""混合检索：向量 + BM25，RRF 融合。

对照课件的做法：
  * 向量管意思相近（硅基流动在线 Qwen3-Embedding-8B，1024 维，归一化后内积 = 余弦）
  * BM25 管字面必须出现（专有名词、机构名、年份、科目名）
  * 两条路并排跑，RRF 倒数排名融合后去重

RRF：score(d) = Σ 1 / (k + rank_i(d))，k 默认 60。
它只看排名不看分数，天然规避了「向量分数和 BM25 分数量纲不同」的问题。

在线 API 的一个关键细节：**查询侧必须加检索指令前缀**，块侧不加。
Qwen3-Embedding 是instruction-aware 模型，官方明确要求查询侧拼
`Instruct: {任务描述}\nQuery: {问题}`，文档侧不拼。不加会明显掉点。
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    BM25_TOPK,
    EMBED_API_MODEL,
    EMBED_DIM,
    FINAL_TOPK,
    INDEX_DIR,
    RRF_K,
    VECTOR_TOPK,
    read_jsonl,
)

VECTORS_PATH = INDEX_DIR / "vectors.npy"
CHUNKS_PATH = INDEX_DIR / "chunks.jsonl"
BM25_PATH = INDEX_DIR / "bm25.pkl"

# Qwen3-Embedding 官方推荐的检索指令。检索任务是「给定问题，找能回答它的段落」。
QUERY_INSTRUCT = (
    "Instruct: Given a financial report question, "
    "retrieve relevant passages that answer the question\nQuery: "
)


class HybridRetriever:
    def __init__(self):
        if not CHUNKS_PATH.exists() or not VECTORS_PATH.exists():
            raise FileNotFoundError(
                "索引不存在，请先跑 python scripts/03_build_index.py"
            )
        self.chunks: list[dict] = read_jsonl(CHUNKS_PATH)
        self.vectors = np.load(VECTORS_PATH)
        if len(self.chunks) != self.vectors.shape[0]:
            raise ValueError(
                f"切块数 {len(self.chunks)} 与向量数 {self.vectors.shape[0]} 不一致"
            )
        with BM25_PATH.open("rb") as f:
            self.bm25 = pickle.load(f)

        # 建索引时的模型/维度必须与查询时一致，否则向量空间不同，
        # 余弦相似度完全无意义。优先读 meta.json（记录了当时的真实配置）。
        meta_path = INDEX_DIR / "meta.json"
        self.meta = (
            json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists()
            else {}
        )
        self.embed_model = os.getenv(
            "EMBED_API_MODEL", self.meta.get("embed_model", EMBED_API_MODEL)
        )
        self.embed_dim = int(self.meta.get("embed_dim") or self.vectors.shape[1])
        if self.embed_dim != self.vectors.shape[1]:
            raise ValueError(
                f"配置维度 {self.embed_dim} 与索引向量维度 {self.vectors.shape[1]} "
                f"不一致。换过EMBED_DIM 必须重建索引。"
            )
        self._embedder = None
        self._vecs32: np.ndarray | None = None
        self._cache: dict[str, np.ndarray] = {}

    # ---------------------------------------------------------- 向量
    @property
    def embedder(self):
        if self._embedder is None:
            from embed_api import build_embedder_from_env

            self._embedder = build_embedder_from_env()
        return self._embedder

    def embed_query(self, q: str) -> np.ndarray:
        if q in self._cache:
            return self._cache[q]
        # 查询侧拼检索指令（Qwen3-Embedding 是 instruction-aware 模型）
        v = self.embedder.embed_query(f"{QUERY_INSTRUCT}{q}")
        v = v.astype(np.float32)
        self._cache[q] = v
        return v

    def search_vector(self, query: str, topk: int = VECTOR_TOPK):
        qv = self.embed_query(query)
        # 归一化向量内积 = 余弦。
        # vectors 存的是 float16，缓存一份 float32 免得每次查询都转
        # （16392×1024 每次转都要几十毫秒）
        if self._vecs32 is None:
            self._vecs32 = self.vectors.astype(np.float32)
        scores = self._vecs32 @ qv
        idx = np.argsort(-scores)[:topk]
        return [(int(i), float(scores[i])) for i in idx]

    # ---------------------------------------------------------- BM25
    def search_bm25(self, query: str, topk: int = BM25_TOPK):
        from bm25_index import tokenize

        toks = tokenize(query)
        scores = self.bm25.scores(toks)
        idx = np.argsort(-scores)[:topk]
        return [(int(i), float(scores[i])) for i in idx if scores[i] > 0]

    # ---------------------------------------------------------- 融合
    def search(self, query: str, final_topk: int = FINAL_TOPK) -> dict:
        """RRF 融合两路召回。返回带来源标记的块列表。"""
        v_hits = self.search_vector(query)
        b_hits = self.search_bm25(query)

        rrf: dict[int, float] = {}
        detail: dict[int, dict] = {}
        for rank, (i, s) in enumerate(v_hits, start=1):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank)
            detail.setdefault(i, {})["vector_rank"] = rank
            detail[i]["vector_score"] = s
        for rank, (i, s) in enumerate(b_hits, start=1):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (RRF_K + rank)
            detail.setdefault(i, {})["bm25_rank"] = rank
            detail[i]["bm25_score"] = s

        ranked = sorted(rrf.items(), key=lambda kv: -kv[1])[:final_topk]
        out = []
        for i, rrf_score in ranked:
            c = dict(self.chunks[i])
            c["rrf_score"] = float(rrf_score)
            c["retrieval"] = detail.get(i, {})
            c["chunk_index"] = i
            out.append(c)
        return {
            "query": query,
            "n_candidate_vector": len(v_hits),
            "n_candidate_bm25": len(b_hits),
            "chunks": out,
        }


if __name__ == "__main__":
    r = HybridRetriever()
    print(f"索引就绪：{len(r.chunks)} 块，{r.vectors.shape[1]} 维")
    res = r.search("中国平安2025年营业收入是多少", final_topk=5)
    for n, c in enumerate(res["chunks"], 1):
        print(f"\n[{n}] {c['company']} | {c['section']} | 第{c['page']}页 | {c['kind']}")
        print(f"    RRF={c['rrf_score']:.4f} 召回={c['retrieval']}")
        print(f"    {c['text'][:120]}")
