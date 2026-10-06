"""切块 + 构建向量索引与 BM25 索引。

切块参数完全对照课件「半年报库的切块」：
  * 800 字一块、重合 120 字、尽量在句末收尾
  * 表格按行切开，切完仍然是「科目 | 本期 | 上期」这样的表
  * 每块带四个标签：公司、章节、页码、块序号
  * 向量化时把「公司名 + 章节名」一起放进文本，检索时才分得清是谁的数

产出：
  data/index/chunks.jsonl   切块全文 + 元数据
  data/index/vectors.npy    向量矩阵 (N, 1024) float16
  data/index/bm25.pkl       BM25 倒排索引
  data/index/meta.json      索引统计
"""
from __future__ import annotations

import os
import argparse
import json
import pickle
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBED_DIM,
    INDEX_DIR,
    TEXT_DIR,
    check_api_config,
    now_str,
    read_jsonl,
)
from embed_api import EmbeddingError  # noqa: E402

SENT_END = "。！？；!?;"


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


# ---------------------------------------------------------------- 切块


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，保留标点。"""
    out, buf = [], []
    for ch in text:
        buf.append(ch)
        if ch in SENT_END:
            out.append("".join(buf).strip())
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """按句累积到接近 size 就切一块，块间保留 overlap 字的上下文。"""
    sents = split_sentences(text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sents:
        cur.append(s)
        cur_len += len(s)
        if cur_len >= size:
            chunks.append("".join(cur))
            # 回退 overlap 字作为下一块的开头
            back, keep = 0, []
            for prev in reversed(cur):
                if back >= overlap:
                    break
                keep.insert(0, prev)
                back += len(prev)
            cur = keep
            cur_len = sum(len(x) for x in cur)
    if cur and cur_len > 20:
        chunks.append("".join(cur))
    return chunks


def chunk_table(tb: dict, max_rows: int = 14) -> list[str]:
    """表格按行切开，每块仍保持行列结构。"""
    rows = tb.get("rows") or []
    if not rows:
        return []
    lines: list[str] = []
    if tb.get("title"):
        lines.append(f"【表】{tb['title']}")
    for r in rows:
        cells = [c for c in r if c not in ("", None)]
        if cells:
            lines.append(" | ".join(cells))
    if not lines:
        return []
    out, cur, cur_len = [], [], 0
    for ln in lines:
        cur.append(ln)
        cur_len += len(ln)
        if len(cur) >= max_rows:
            out.append("\n".join(cur))
            cur, cur_len = [], 0
    if cur:
        out.append("\n".join(cur))
    return out


# ---------------------------------------------------------------- 建块


def build_chunks() -> list[dict]:
    files = sorted(TEXT_DIR.glob("*.jsonl"))
    if not files:
        log(f"{TEXT_DIR} 下没有 jsonl，请先跑 02_extract.py")
        return []

    chunks: list[dict] = []
    for fp in files:
        pages = read_jsonl(fp)
        if not pages:
            continue
        code = pages[0].get("code", fp.stem)
        name = pages[0].get("name", code)
        company_seq = 0
        n_text_chunk = n_table_chunk = 0

        for pg in pages:
            page_no = pg.get("page", 0)
            section = pg.get("section") or ""
            text = (pg.get("text") or "").strip()

            # 正文切块
            for piece in chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP):
                if len(piece.strip()) < 50:
                    continue
                company_seq += 1
                n_text_chunk += 1
                chunks.append(
                    {
                        "chunk_id": f"{code}-T{company_seq:05d}",
                        "code": code,
                        "company": name,
                        "section": section,
                        "page": page_no,
                        "seq": company_seq,
                        "kind": "text",
                        "text": piece,
                        # 向量化用的文本：公司名 + 章节名放前面，检索才分得清是谁的数
                        "embed_text": f"{name}（{code}） {section}\n{piece}",
                    }
                )

            # 表格切块
            for tb in pg.get("tables") or []:
                for piece in chunk_table(tb):
                    if len(piece.strip()) < 30:
                        continue
                    company_seq += 1
                    n_table_chunk += 1
                    chunks.append(
                        {
                            "chunk_id": f"{code}-T{company_seq:05d}",
                            "code": code,
                            "company": name,
                            "section": section,
                            "page": page_no,
                            "seq": company_seq,
                            "kind": "table",
                            "text": piece,
                            "embed_text": f"{name}（{code}） {section}\n{piece}",
                        }
                    )

        log(
            f"  {code} {name}: {company_seq} 块（正文 {n_text_chunk} / 表格 {n_table_chunk}）"
        )

    return chunks


# ---------------------------------------------------------------- 向量
#
# 向量化走**硅基流动在线 API**（Qwen/Qwen3-Embedding-8B）。
# 本机跑不动8B：1660 Ti 6GB 显存与 16GB 内存都放不下，退到 0.6B 也要
# 20 分钟到 4 小时。在线 ¥4 左右一次性搞定，且 8B 检索质量明显更好。
#
# 关键设计：**分批落盘 + 断点续传**。
# 16392 块要发 1000+ 次请求，中间必然遇到限流、断网、模型过载（503），
# 必须做到「跑到一半中断，重跑能接着跑」，而不是从头再来。
#
# 重试策略分层，避免无谓等待：
#   * 429 / 5xx  → 指数退避重试（真的会恢复）
#   * 400 / 401 / 403 → 立刻抛出（重试一万次也没用，要改参数或 key）
#   * 返回条数与请求不匹配 → 重试（服务端偶发截断）


def build_vectors(
    chunks: list[dict],
    batch_size: int | None = None,
    resume: bool = True,
    workers: int = 6,
) -> np.ndarray:
    """调用在线 Embedding API，返回 (N, dim) 的 float16 向量矩阵。

    workers：并发请求数。串行时每批一次往返 ≈0.7s，972 批要 2 小时；
    并发 6 路后总耗时降到十几分钟（实测网络 RTT 是主要瓶颈，不是服务端算力）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from embed_api import PartWriter, build_embedder_from_env

    cfg = check_api_config()
    embedder = build_embedder_from_env()
    if batch_size:
        embedder.batch_size = batch_size

    texts = [c["embed_text"] for c in chunks]
    bs = embedder.batch_size
    n_batches = (len(texts) + bs - 1) // bs

    log(
        f"在线向量化：{embedder.model} / {embedder.dimensions} 维"
        f" / 每批 {bs} 条 / 共 {n_batches}  批 / 并发 {workers}"
    )

    # ---- 断点续传：分片目录 ----
    parts_dir = INDEX_DIR / "_parts"
    writer = PartWriter(parts_dir, embedder.dimensions)

    # 切块参数变了就必须重来：分片与块序号是对应的，
    # 拿旧分片配新块会彻底错位
    if writer.meta.get("batch_size") != bs or not resume:
        if writer.done_batches():
            log("已有分片与当前配置不一致，重置分片（不重置会与新块错位）")
        writer.reset(bs)
    else:
        done = writer.done_batches()
        if done:
            log(f"检测到 {len(done)}/{n_batches} 批已向量化，继续跑剩余部分")

    # ---- 全跑完的情况：直接拼分片 ----
    full = writer.load_all(n_batches)
    if full is not None:
        log(f"全部 {n_batches} 批已存在，直接合并分片")
        return full.astype(np.float16)

    done = writer.done_batches()
    todo = [bi for bi in range(n_batches) if bi not in done]
    t_start = time.time()
    n_finished = 0
    save_every = max(1, len(todo) // 20)  # 每完成 5% 存一次进度，避免频繁写盘

    # 每个线程一份独立客户端：客户端内部有 self._last_call 限速状态，
    # 共享会互相干扰；且 requests.Session 非线程安全
    #
    # 并发下统计不能简单相加（各线程的 stats 只记自己的请求，
    # 但如果同一线程对象被复用就会重复计数），所以创建时注册引用，
    # 最后按 id 去重累加。
    all_stats: list[dict] = []
    _orig_env = build_embedder_from_env
    local = threading.local()

    def _make_embedder():
        emb = _orig_env()
        emb.batch_size = bs
        all_stats.append(emb.stats)
        return emb

    def embed_one(bi: int):
        emb = getattr(local, "emb", None)
        if emb is None:
            emb = _make_embedder()
            local.emb = emb
        lo, hi = bi * bs, min((bi + 1) * bs, len(texts))
        batch_texts = texts[lo:hi]
        if not any(t.strip() for t in batch_texts):
            arr = np.zeros((len(batch_texts), emb.dimensions), dtype=np.float32)
        else:
            arr = emb.embed_batch(batch_texts)
        # 归一化：让「内积 = 余弦」，检索端不必再算模长
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return bi, arr / norms

    failed_batch: int | None = None
    fail_reason = ""
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(embed_one, bi): bi for bi in todo}
        for fut in as_completed(futures):
            bi = futures[fut]
            try:
                bi, arr = fut.result()
            except EmbeddingError as e:
                failed_batch, fail_reason = bi, str(e)
                # 先停掉还没开始的任务，已在跑的会跑完（不强行中断，避免分片写一半）
                for f in futures:
                    f.cancel()
                break
            except Exception as e:  # noqa: BLE001
                failed_batch, fail_reason = bi, f"{type(e).__name__}: {e}"
                for f in futures:
                    f.cancel()
                break

            with lock:
                writer.write(bi, arr)
                n_finished += 1
                if n_finished % save_every == 0 or failed_batch is not None:
                    writer.save()

            if n_finished % 20 == 0 or failed_batch is not None:
                n_done = len(writer.done_batches())
                el = time.time() - t_start
                rate = el / max(1, n_finished)
                eta = rate * (len(todo) - n_finished)
                log(
                    f"  进度 {n_done}/{n_batches} ({n_done / n_batches * 100:.1f}%)"
                    f" | 已用 {el / 60:.1f} 分钟"
                    f" | 预计剩余 {eta / 60:.1f} 分钟"
                )

    if failed_batch is not None:
        raise EmbeddingError(
            f"第 {failed_batch}/{n_batches} 批（块 {failed_batch * bs}~"
            f"{failed_batch * bs + bs}）失败：{fail_reason}\n"
            f"已完成批次已落盘到 {parts_dir}，"
            f"修好问题后重跑本脚本会自动从这里继续。"
        )

    full = writer.load_all(n_batches)
    if full is None:
        raise EmbeddingError("所有批次已处理，但分片不完整，请检查 _parts 目录")

    # 并发下每个线程有自己的客户端统计，按 id 去重后累加才是真实消耗
    seen_ids: set[int] = set()
    tot_stats = {"requests": 0, "retries": 0, "rate_limited": 0, "prompt_tokens": 0}
    for s in all_stats:
        if id(s) in seen_ids:
            continue
        seen_ids.add(id(s))
        for k in tot_stats:
            tot_stats[k] += s.get(k, 0)
    cost = tot_stats["prompt_tokens"] / 1_000_000 * 0.28  # 8B 输入 ¥0.28/M tokens
    log(
        f"向量化完成：{full.shape}"
        f" | 含重试请求 {tot_stats['requests']} 次"
        f" | 重试 {tot_stats['retries']} | 限流 {tot_stats['rate_limited']}"
        f" | tokens {tot_stats['prompt_tokens']:,}"
        f" | 约 ¥{cost:.2f}"
    )

    # 分片保留：它们是断点续传的凭据，删掉会让人以为没跑过。
    # 想清理手动跑 rm -rf data/index/_parts（合并成功后已无用途，占约 30MB）。
    return full.astype(np.float16)


# ---------------------------------------------------------------- BM25
#
# BM25 类与分词器都在 bm25_index.py 里，原因见该文件顶部注释：
# BM25 会被 pickle 进 bm25.pkl，类必须定义在「检索时也会 import 的模块」中，
# 否则反序列化会报 Can't get attribute 'BM25'。

from bm25_index import BM25, build_bm25  # noqa: E402


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks-only", action="store_true", help="只切块，不建向量")
    ap.add_argument("--no-bm25", action="store_true", help="不建 BM25")
    ap.add_argument(
        "--batch", type=int, default=0, help="每批文本条数，覆盖 .env 的 EMBED_API_BATCH"
    )
    ap.add_argument(
        "--fresh", action="store_true", help="忽略已完成的分片，全部重新向量化"
    )
    ap.add_argument(
        "--workers", type=int, default=6,
        help="并发请求数（默认 6）。串行时每批一次往返 ≈0.7s，972 批要 2 小时",
    )
    args = ap.parse_args()

    log("===== 切块 =====")
    chunks = build_chunks()
    if not chunks:
        return 1

    out = INDEX_DIR / "chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    n_text = sum(1 for c in chunks if c["kind"] == "text")
    log(f"切块完成：{len(chunks)} 块（正文 {n_text} / 表格 {len(chunks) - n_text}）"
        f" -> {out}")

    # 校验 API 配置（缺 key / key 格式不对在这里就报错，不要等跑了一半才炸）
    cfg = check_api_config()
    log(f"向量化后端：{cfg['embed_provider']} | {cfg['embed_model']} | key尾号 {cfg['embed_api_key_tail']}")

    meta = {
        "n_chunks": len(chunks),
        "n_text": n_text,
        "n_table": len(chunks) - n_text,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "companies": sorted({c["company"] for c in chunks}),
        "embed_dim": EMBED_DIM,
        **cfg,
    }

    if not args.chunks_only:
        log("===== 向量索引（在线 API）=====")
        vecs = build_vectors(
            chunks,
            batch_size=args.batch or None,
            resume=not args.fresh,
            workers=args.workers,
        )
        if vecs.shape[0] != len(chunks):
            raise EmbeddingError(
                f"向量数 {vecs.shape[0]} 与切块数 {len(chunks)} 不一致，索引已损坏"
            )
        np.save(INDEX_DIR / "vectors.npy", vecs)
        log(f"向量已存 {INDEX_DIR / 'vectors.npy'}")

    if not args.no_bm25:
        log("===== BM25 索引 =====")
        log("BM25：jieba 分词建倒排 ...")
        bm = build_bm25(chunks)
        log(f"BM25 完成：{bm.N} 块，词表 {len(bm.idf)}")
        with (INDEX_DIR / "bm25.pkl").open("wb") as f:
            pickle.dump(bm, f)
        log(f"BM25 已存 {INDEX_DIR / 'bm25.pkl'}")

    (INDEX_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"===== 完成：{meta['n_chunks']} 块 / {len(meta['companies'])} 家公司 =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
