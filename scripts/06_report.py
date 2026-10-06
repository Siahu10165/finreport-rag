"""把 data/index/eval_results.json 渲染成 docs/评测记录.md。

一页结论 + 逐题明细都要能直接给人看，不用再读 JSON。
答错与翻车的题**同等保留**——结论里最该看的就是这些。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import INDEX_DIR, ROOT, now_str  # noqa: E402


def render(data: dict) -> str:
    s = data["summary"]
    rows = data["results"]
    L: list[str] = []

    wrong = [r for r in rows if not r["correct"]]
    L.append("# 财报问答知识库 · 评测记录\n")
    L.append(
        f"生成时间：{s.get('generated_at', now_str())}　|　"
        f"题数 {s['n_questions']}　|　答对 {s['n_correct']}　|　"
        f"正确率 {s['accuracy'] * 100:.0f}%　|　"
        f"检索层命中率 {s.get('recall_accuracy', 0) * 100:.0f}%　|　"
        f"每题召回 {s['topk']} 块\n"
    )

    # ---------------- 一页结论 ----------------
    L.append("## 一页结论\n")
    L.append("| 题型 | 题数 | 答对 | 检索命中 | 正确率 |")
    L.append("|---|---|---|---|---|")
    for k, v in s["by_kind"].items():
        L.append(
            f"| {k} | {v['n']} | {v['ok']} | {v.get('recall_ok', 0)} | "
            f"{v['ok'] / v['n'] * 100:.0f}% |"
        )
    L.append("")

    # 错题清单放最前面——这是最该看的部分
    if wrong:
        L.append("### 错在哪（逐题如实列出）\n")
        L.append("| # | 题型 | 问题 | 检索层 | 生成层 | 原因 |")
        L.append("|---|---|---|---|---|---|")
        for r in wrong:
            recall = "✅ 命中" if r["recall_ok"] else f"❌ 漏 {r.get('missed_companies', [])}"
            gen = "✅ 达标" if r["gen_ok"] else "❌ 缺关键值"
            reason = "；".join(r.get("fail_reasons", []))[:120]
            L.append(
                f"| {r['no']} | {r['kind']} | {r['question'][:40]} | "
                f"{recall} | {gen} | {reason} |"
            )
        L.append("")
    else:
        L.append("**本轮 10 题全部答对。**\n")

    # ---------------- 逐题明细 ----------------
    L.append("## 逐题明细\n")
    for r in rows:
        mark = "✅" if r["correct"] else "❌"
        L.append(f"### {r['no']}. {r['question']}　{mark}\n")
        L.append(f"- **题型**：{r['kind']}　|　判分方式：`{r['judge_by']}`")
        L.append(f"- **标准答案**：{r['truth']}")
        L.append(
            f"- **召回**：向量 {r['n_vector_candidates']} 条 / "
            f"BM25 {r['n_bm25_candidates']} 条 → RRF 融合后 {r['n_chunks']} 块"
        )
        L.append(
            f"- **召回到的公司**：{'、'.join(r['companies_recalled']) or '（无）'}"
        )
        L.append(
            f"- **召回到的章节**：{'、'.join(r['sections_recalled'][:8]) or '（无）'}"
        )
        if r.get("need_companies"):
            recall_txt = "是" if r["recall_ok"] else "否，漏了 " + str(
                r.get("missed_companies", [])
            )
            L.append(f"- **目标公司是否全部召回**：{recall_txt}")
        L.append(
            f"- **命中期望值**：{'、'.join(r['expect_hit']) or '（无）'}"
            + (f"　|　**缺失**：{'、'.join(r['expect_missed'])}"
               if r.get("expect_missed") else "")
        )
        if r.get("hallucinated_cites"):
            L.append(f"- **⚠️ 编造引用编号**：{r['hallucinated_cites']}")
        if r.get("llm_error"):
            L.append(f"- **LLM 错误**：{r['llm_error']}")
        if r.get("fail_reasons"):
            L.append(f"- **判定为错的原因**：{'；'.join(r['fail_reasons'])}")
        L.append(f"- **耗时**：{r['elapsed_s']}s\n")

        if r.get("answer"):
            L.append("**模型回答**\n")
            L.append("> " + r["answer"].replace("\n", "\n> "))
            L.append("")

        L.append("<details><summary>召回了哪些块（点击展开）</summary>\n")
        L.append("| # | 公司 | 章节 | 页 | 类型 | RRF | V排名 | B排名 | 片段 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for c in r["chunks"]:
            snip = c["snippet"].replace("|", "丨").replace("\n", " ")[:120]
            L.append(
                f"| {c['idx']} | {c['company']} | {c['section'] or '—'} | {c['page']} | "
                f"{'表' if c['kind'] == 'table' else '文'} | {c['rrf_score']} | "
                f"{c['vector_rank'] or '—'} | {c['bm25_rank'] or '—'} | {snip} |"
            )
        L.append("\n</details>\n")
        L.append("---\n")

    return "\n".join(L)


def main() -> int:
    src = INDEX_DIR / "eval_results.json"
    if not src.exists():
        print(f"找不到 {src}，请先跑 python scripts/05_evaluate.py")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    out = ROOT / "docs" / "评测记录.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    print(f"已写入 {out}")

    # 顺带导出一份结论数据，供撰写 docs/结论.md 时引用
    s = data["summary"]
    print(
        f"总计 {s['n_correct']}/{s['n_questions']} 答对"
        f"（检索层 {s.get('recall_accuracy', 0)*100:.0f}%）"
    )
    for k, v in s["by_kind"].items():
        print(f"  {k}: {v['ok']}/{v['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())