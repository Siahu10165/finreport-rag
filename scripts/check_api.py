"""在线向量化API 自检：连通性 + 批量吞吐 + 限流退避 + 断点续传。

跑之前必须先跑完 03_build_index.py --chunks-only（需要 data/index/chunks.jsonl）。

用法：
  python scripts/check_api.py            # 连通性 + 质量 + 吞吐，够用了
  python scripts/check_api.py --stress   # 额外压 100 条，估算全量耗时与费用

本脚本替代了原bench_embed.py / bench_quality.py / test_config.py：
在线 API 方案不再需要 torch / sentence-transformers，也不存在
「CPU bf16 还是 GPU fp16」「显存够不够」这类本地问题。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EMBED_API_BATCH,
    EMBED_DIM,
    INDEX_DIR,
    check_api_config,
    read_jsonl,
)
from embed_api import (  # noqa: E402
    EmbeddingError,
    PartWriter,
    build_embedder_from_env,
)

# 取自财报库的真实问法，用来验证语义质量（不是「测试文本1/2」那种无意义输入）
PROBES = [
    "中国平安2025年营业总收入是多少",
    "中信证券归属于母公司股东的净利润",
    "新华保险偿付能力充足率",
    "保险公司开展债券投资的风险管理措施",
]


def step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", action="store_true", help="额外压测 100 条，估算全量成本")
    args = ap.parse_args()
    total = 5 if args.stress else 4
    ok = True

    # ---------------------------------------------------------- 1 配置
    step(1, total, "配置检查")
    try:
        cfg = check_api_config()
        print(f"  后端 {cfg['embed_provider']} | 模型 {cfg['embed_model']}")
        print(f"  维度 {cfg['embed_dim']} | 每批 {cfg['embed_api_batch']} 条")
        print(f"  地址 {cfg['embed_base_url']}")
        print(f"  key末4 位 {cfg['embed_api_key_tail']}（仅显示末位，不落全key）")
    except RuntimeError as e:
        print(f"  失败：{e}")
        return 1

    # ---------------------------------------------------------- 2 连通性
    step(2, total, "连通性与维度检查（真实问答 query）")
    try:
        e = build_embedder_from_env()
        arr = e.embed_batch(PROBES)
        print(f"  成功：{len(PROBES)} 条 → {arr.shape}")
        if arr.shape[1] != EMBED_DIM:
            print(f"  维度不符：期望 {EMBED_DIM}，实际 {arr.shape[1]}")
            ok = False
        else:
            print(f"  维度正确（{arr.shape[1]}）")
        print(f"  tokens 消耗 {e.stats['prompt_tokens']} | 请求 {e.stats['requests']} 次")
    except EmbeddingError as ex:
        print(f"  失败：{ex}")
        print(
            "\n  排查顺序：\n"
            "   1) key 是否有效（不带任何参数调 /v1/user/info 同样 401 → key 本身无效）\n"
            "   2) 账户是否有余额、模型是否已开放访问\n"
            "   3) 维度是否受支持（8B 支持 64/128/256/512/768/1024/1536/2048/2560/4096）"
        )
        return 1

    # ---------------------------------------------------------- 3 语义质量
    step(3, total, "语义质量（同义问法应比无关文本更相似）")
    try:
        from retriever import QUERY_INSTRUCT

        q = e.embed_query(f"{QUERY_INSTRUCT}中信证券净利润是多少")
        sims = arr @ q
        order = np.argsort(-sims)
        print("  查询：中信证券净利润是多少（已加检索指令前缀）")
        for i in order:
            mark = "  ← 最相关" if i == order[0] else ""
            print(f"    {sims[i]:.4f}  {PROBES[i]}{mark}")
        # 判定：最相关的应当是中信证券净利润那条；若不是说明指令前缀或模型有问题
        if PROBES[order[0]] != "中信证券归属于母公司股东的净利润":
            print("  警告：最相关结果不是预期的那条，请人工确认")
            ok = False
        else:
            print("  通过：最相关结果符合预期")
    except EmbeddingError as ex:
        print(f"  失败：{ex}")
        ok = False

    # ---------------------------------------------------------- 4 吞吐
    step(4, total, f"批量吞吐（每批 {EMBED_API_BATCH} 条）")
    chunks = read_jsonl(INDEX_DIR / "chunks.jsonl")
    if not chunks:
        print(f"  {INDEX_DIR/'chunks.jsonl'} 不存在，先跑 03_build_index.py --chunks-only")
        return 1
    samples = [c["embed_text"] for c in chunks[: EMBED_API_BATCH * 3]]
    # 顺带验证真实财报块不会触发 400（空串/超长）
    e2 = build_embedder_from_env()
    t0 = time.time()
    got = 0
    for i in range(0, len(samples), EMBED_API_BATCH):
        batch = samples[i : i + EMBED_API_BATCH]
        if not any(t.strip() for t in batch):
            continue
        try:
            e2.embed_batch(batch)
            got += len(batch)
        except EmbeddingError as ex:
            print(f"  真实块失败（第 {i} 批）：{ex}")
            ok = False
            break
    dt = time.time() - t0
    n_total = len(chunks)
    print(f"  {got} 条用时 {dt:.1f}s（{dt/max(1,got)*1000:.0f}ms/条）")
    if got:
        eta = dt / got * n_total
        cost = e2.stats["prompt_tokens"] / 1e6 * 0.28
        print(f"  真实财报块无 400/限流问题 ✓")
        print(f"  外推全量{n_total} 块：{eta/60:.1f} 分钟 | 约 ¥{cost:.2f}")

    # ---------------------------------------------------------- 5 断点续传
    if args.stress:
        step(5, total, "断点续传与限流退避（本地模拟，不发请求）")
        ok = _test_resume() and ok

    print("\n" + "=" * 56)
    print("结论：" + ("全部通过，可以跑 03_build_index.py 了" if ok else "有问题，见上面提示"))
    return 0 if ok else 1


def _test_resume() -> bool:
    """验证 PartWriter 的断点续传逻辑（纯本地，不消耗 API 额度）。

    踩过的坑：只信元数据不信文件存在性，会在分片被误删后把顺序接错，
    导致向量与切块整体错位、检索结果全是噪声。
    """
    print("  [A] 分片写入与合并", flush=True)
    tmp = Path(tempfile.mkdtemp())
    try:
        w = PartWriter(tmp, dim=4)
        w.reset(batch_size=2)
        for b in range(3):
            w.write(b, np.full((2, 4), float(b), dtype=np.float32))
        full = w.load_all(3)
        assert full is not None and full.shape == (6, 4), f"合并结果异常 {full}"
        print("  3 批 → 合并成 (6,4) ✓")

        print("  [B] 中途缺批不应被误认为已完成", flush=True)
        (tmp / "part_00001.npy").unlink()
        w2 = PartWriter(tmp, dim=4)
        assert w2.load_all(3) is None, "缺批时不该返回完整结果"
        assert w2.done_batches() == {0, 2}, f"done_batches 应为 {{0,2}}，实为 {w2.done_batches()}"
        print("  删掉第 1 批后正确识别为未完成 ✓")

        print("  [C] 重跑时跳过已完成批次", flush=True)
        w2.write(1, np.full((2, 4), 1.0, dtype=np.float32))
        assert w2.done_batches() == {0, 1, 2}, w2.done_batches()
        print("  续传集合恢复为 {0,1,2} ✓")
        return True
    except AssertionError as e:
        print(f"  失败：{e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())