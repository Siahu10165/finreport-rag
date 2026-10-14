"""把 data/index/eval_results.json 渲染成 docs/评测记录.md。

逐题给出「正确 / 部分正确 / 错误」三档判定 + 证据理由，
跨公司全景题额外给出逐公司覆盖矩阵。
答错与翻车的题**同等保留**——结论里最该看的就是这些。

另一份产物：一页结论 docs/结论.md（严格控制在 1 页 A4 内）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import INDEX_DIR, ROOT, now_str  # noqa: E402

GRADE_MARK = {"正确": "✅", "部分正确": "⚠️", "错误": "❌"}


def render(data: dict) -> str:
    s = data["summary"]
    rows = data["results"]
    L: list[str] = []

    bad = [r for r in rows if r["grade"] != "正确"]
    L.append("# 财报问答知识库 · 评测记录\n")
    L.append(
        f"生成时间：{s.get('generated_at', now_str())}　|　"
        f"题数 {s['n_questions']}　|　"
        f"**正确 {s.get('n_correct', 0)} / 部分正确 {s.get('n_partial', 0)} "
        f"/ 错误 {s.get('n_wrong', 0)}**　|　"
        f"检索层命中率 {s.get('recall_accuracy', 0) * 100:.0f}%　|　"
        f"每题召回 {s['topk']} 块\n"
    )

    L.append("### 三档判定标准\n")
    L.append("| 判定 | 含义 |")
    L.append("|---|---|")
    for k, v in (s.get("grade_definitions") or {}).items():
        L.append(f"| {GRADE_MARK.get(k, '')} **{k}** | {v} |")
    L.append("")

    # ---------------- 概览 ----------------
    L.append("## 一、逐题型汇总\n")
    L.append("| 题型 | 题数 | ✅正确 | ⚠️部分正确 | ❌错误 | 检索命中 |")
    L.append("|---|---|---|---|---|---|")
    for k, v in s["by_kind"].items():
        L.append(
            f"| {k} | {v['n']} | {v.get('正确', 0)} | {v.get('部分正确', 0)} | "
            f"{v.get('错误', 0)} | {v.get('recall_ok', 0)}/{v['n']} |"
        )
    L.append("")

    # ---------------- 未答对的题放最前 ----------------
    if bad:
        L.append("### 未答对的题（逐题列出判定与理由）\n")
        L.append("| # | 题型 | 判定 | 检索层 | 生成层 | 证据理由 |")
        L.append("|---|---|---|---|---|---|")
        for r in bad:
            ev = "；".join(r.get("evidence", []))
            ev = ev.replace("|", "丨")[:260]
            L.append(
                f"| {r['no']} | {r['kind']} | {GRADE_MARK[r['grade']]} {r['grade']} | "
                f"{r.get('recall_layer', '—')} | {r.get('gen_layer', '—')} | {ev} |"
            )
        L.append("")
    else:
        L.append("**本轮 10 题全部判定为正确。**\n")

    # ---------------- 跨公司全景题覆盖矩阵 ----------------
    pano = [r for r in rows if r.get("per_company_coverage")]
    if pano:
        L.append("## 二、跨公司全景题：逐公司覆盖与缺失\n")
        for r in pano:
            cov = r["per_company_coverage"]
            L.append(f"### 第 {r['no']} 题：{r['question']}\n")
            L.append(
                f"- **标准答案**：{r['truth']}\n"
            )
            L.append(
                f"- **判定**：{GRADE_MARK[r['grade']]} **{r['grade']}**\n"
            )
            n_need = len(cov["targets"])
            n_cov = cov["covered"]
            if n_need > r["n_chunks"]:
                slot_note = (
                    f"top-{r['n_chunks']} 块共 {r['n_chunks']} 个位置，"
                    f"要覆盖 {n_need} 家公司**本身就不够**——"
                    "这是该类题失分的结构性原因，改检索策略也救不了"
                )
            elif n_cov < n_need:
                slot_note = (
                    f"top-{r['n_chunks']} 块共 {r['n_chunks']} 个位置，"
                    f"窗口装得下 {n_need} 家，但只召回 {n_cov} 家，见下表缺失项"
                )
            else:
                slot_note = (
                    f"top-{r['n_chunks']} 块共 {r['n_chunks']} 个位置，"
                    f"{n_need} 家**全部召回**"
                )
            L.append(f"- **应覆盖 {n_cov}/{n_need} 家**　{slot_note}\n")
            L.append("| 目标公司 | 是否召回 | 召回块数 | 块序号 | 被答案引用 |")
            L.append("|---|---|---|---|---|")
            for c in cov["coverage"]:
                L.append(
                    f"| {c['company']} | {'✅' if c['covered'] else '❌ 未召回'} | "
                    f"{c['n_chunks']} | {c.get('chunk_idxs') or '—'} | "
                    f"{'是' if c['cited_in_answer'] else '否'} |"
                )
            if cov["missing"]:
                L.append(f"\n**缺失公司**：{'、'.join(cov['missing'])}\n")
            if cov.get("not_targeted_but_relevant"):
                L.append(
                    f"**召回里混入了与本题无关的公司**（占用了 top-{r['n_chunks']} "
                    f"的窗口位置）：{'、'.join(cov['not_targeted_but_relevant'])}\n"
                )
            L.append(
                f">证据：{'；'.join(r.get('evidence', []))}\n"
            )

    # ---------------- 逐题明细 ----------------
    L.append("## 三、逐题明细\n")
    for r in rows:
        mark = GRADE_MARK[r["grade"]]
        L.append(f"### 第 {r['no']} 题　{mark} {r['grade']}\n")
        L.append(f"> {r['question']}\n")
        L.append(
            f"| 项 | 内容 |\n|---|---|\n"
            f"| 题型 | {r['kind']} |\n"
            f"| 判定 | **{r['grade']}**（检索层 {r.get('recall_layer')} ／ "
            f"生成层 {r.get('gen_layer')}） |\n"
            f"| 判分方式 | `{r['judge_by']}`"
            f"（{'要求全部命中' if r['judge_by'] == 'expect_all' else '任一命中即算对'}） |\n"
            f"| 标准答案 | {r['truth']} |"
        )
        L.append("")
        L.append("**判定理由（逐条证据）**\n")
        for e in r.get("evidence", []):
            L.append(f"- {e}")
        L.append("")
        L.append(
            f"**检索**：向量召回 {r['n_vector_candidates']} 条 ／ "
            f"BM25 召回 {r['n_bm25_candidates']} 条 → RRF 融合取 {r['n_chunks']} 块\n"
        )
        L.append(f"- 召回到的公司：{'、'.join(r['companies_recalled']) or '（无）'}")
        L.append(
            f"- 召回到的章节：{'、'.join(r['sections_recalled'][:8]) or '（无）'}"
        )
        if r.get("need_companies"):
            ok = "是" if r["recall_ok"] else "否，漏了 " + "、".join(
                r.get("missed_companies", [])
            )
            L.append(f"- 目标公司是否全部召回：**{ok}**")
        L.append(
            f"- 命中期望值：{'、'.join(r['expect_hit']) or '（无）'}"
            + (
                f"　|　**缺失**：{'、'.join(r['expect_missed'])}"
                if r.get("expect_missed")
                else ""
            )
        )
        if r.get("hallucinated_cites"):
            L.append(f"- ⚠️ **编造引用编号**：{r['hallucinated_cites']}")
        if r.get("llm_error"):
            L.append(f"- LLM 错误：{r['llm_error']}")
        L.append(f"- 单题耗时：{r['elapsed_s']}s\n")

        if r.get("answer"):
            L.append("**模型回答**\n")
            L.append("> " + r["answer"].replace("\n", "\n> "))
            L.append("")

        L.append("<details><summary>召回了哪些块（点击展开）</summary>\n")
        L.append("| # | 公司 | 章节 | 页 | 类型 | RRF | V排名 | B排名 | 原文片段 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for c in r["chunks"]:
            snip = c["snippet"].replace("|", "丨").replace("\n", " ")[:130]
            L.append(
                f"| {c['idx']} | {c['company']} | {c['section'] or '—'} | {c['page']} | "
                f"{'表' if c['kind'] == 'table' else '文'} | {c['rrf_score']} | "
                f"{c['vector_rank'] or '—'} | {c['bm25_rank'] or '—'} | {snip} |"
            )
        L.append("\n</details>\n")
        L.append("---\n")

    return "\n".join(L)


def render_conclusion(data: dict) -> str:
    """一页结论：严格控制在 1 页 A4（用词克制，只留可核验的事实与判断）。"""
    s = data["summary"]
    rows = data["results"]
    L: list[str] = []

    n_ok = s.get("n_correct", 0)
    n_part = s.get("n_partial", 0)
    n_bad = s.get("n_wrong", 0)

    L.append("# 一页结论：财报问答知识库实测表现\n")
    L.append(
        f"**{s['n_questions']} 题：✅正确 {n_ok} ／ ⚠️部分正确 {n_part} ／ "
        f"❌错误 {n_bad}**　|　检索层命中率 "
        f"{s.get('recall_accuracy', 0) * 100:.0f}%　|　每题召回 {s['topk']} 块"
        f"（向量 {s['topk']} ／ BM25 {s['topk']} → RRF 融合）\n"
    )

    L.append("## 语料\n")
    L.append(
        "**20 家**保险/券商 **2025 年年报全文**，6,462 页 / 665 万字 / 4,291 张表，"
        "切块 16,392 个（正文 11,846 ／ 表格 4,546）。"
        "逐家官方全文直链、披露日期、PDF 实际页数与 SHA-256 指纹见 "
        "[`语料来源清单.md`](语料来源清单.md)，每条都可点开复核。\n"
    )

    L.append("## 分题型表现\n")
    L.append("| 题型 | 题数 | ✅ | ⚠️ | ❌ | 检索命中 |")
    L.append("|---|---|---|---|---|---|")
    for k, v in s["by_kind"].items():
        L.append(
            f"| {k} | {v['n']} | {v.get('正确', 0)} | {v.get('部分正确', 0)} | "
            f"{v.get('错误', 0)} | {v.get('recall_ok', 0)}/{v['n']} |"
        )
    L.append("")

    L.append("## 答得好的\n")
    # 陷阱型单独说明（它「对」的理由不是检索准，而是模型敢拒答），先跳过
    for k, v in s["by_kind"].items():
        if v.get("正确") == v["n"] and v["n"] > 0 and k != "陷阱型":
            L.append(f"- **{k}（{v['n']}/{v['n']}）**：关键值全部命中")
    # 陷阱型的表现要单独说明：它「对」的理由不是检索准，而是模型没有编造
    trap = next((r for r in rows if r["kind"] == "陷阱型"), None)
    if trap and trap["grade"] == "正确":
        L.append(
            f"- **陷阱型（1/1）**：问中信证券 2026 年净利润预测，"
            f"而库里只有 2025 实际数。模型明确回答「资料中未提及」"
            f"且未给出任何预测数字 → **没有幻觉**。"
            f"这类题的关键不是检索准，而是模型敢拒答"
        )
    L.append("")

    L.append("## 翻车的（如实保留，完整召回见 `评测记录.md`）\n")
    bad = [r for r in rows if r["grade"] != "正确"]
    if bad:
        for r in bad:
            L.append(
                f"**第 {r['no']} 题 {GRADE_MARK[r['grade']]} {r['grade']}**　"
                f"{r['question'][:36]}\n"
            )
            L.append(
                f"　检索层 {r.get('recall_layer')}（召回 "
                f"{'、'.join(r['companies_recalled']) or '无'}）"
                f"／生成层 {r.get('gen_layer')}"
            )
            for e in r.get("evidence", [])[:2]:
                L.append(f"　- {e}")
            L.append("")
    else:
        L.append("本轮无。\n")

    L.append("## 判定口径变更\n")
    L.append(
        "判定从二档（对/错）改为三档（正确 / 部分正确 / 错误）。"
        "**曾判为「错」的两题现为「部分正确」**：检索其实召回了正确的块，"
        "只是模型没把全部关键值写进答案——是判定变细了，不是变好了。\n"
    )

    L.append("## 根因与改进方向\n")
    L.append(
        "1. **同一事实两种精度并存时，模型挑精度低的**（第 2 题）。"
        "8 块召回里 7 块是目标公司国泰君安，检索是准的；"
        "但年报「董事长致辞」写整数「278 亿元」、「管理层讨论与分析」写「278.09 亿元」，"
        "模型引用了前者。修法：提示词要求优先引用 MD&A 章节的精确值。\n"
    )
    L.append(
        "2. **公司更名是潜在静默风险**（本次侥幸未失分）。库里按代码 601211 登记"
        "「国泰君安」，该股 2025 年重组后年报自称「国泰海通」。"
        "本次 8 块全部命中是因为正文里两种称呼都出现；若语料只含一种，"
        "整类问题会静默失准且不报错。修法：把曾用名写进 `embed_text` 前缀。\n"
    )
    L.append(
        "3. **跨公司「谁最高」超出 top-k 能力**（第 8 题）。需比较 15 家券商，"
        "而 top-8 只够放 8 家；且 RRF 对「最高/排名」无区分度。"
        "本次正确答案中信证券（748.54 亿元，p37）**未进 top-8**，"
        "模型只能答次高的国泰君安。修法：两段式——按公司分组各取 top-1 再排序。\n"
    )
    L.append(
        "4. **少数公司数字抽不出**。中国人保用自定义嵌入字体，中文可提但数字不可提，"
        "其营收/偿付能力/员工人数在本库中实际缺失。\n"
    )
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

    out2 = ROOT / "docs" / "结论.md"
    out2.write_text(render_conclusion(data), encoding="utf-8")
    print(f"已写入 {out2}")

    s = data["summary"]
    print(
        f"\n总计：正确 {s.get('n_correct', 0)} / 部分正确 {s.get('n_partial', 0)}"
        f" / 错误 {s.get('n_wrong', 0)}（共 {s['n_questions']}，"
        f"检索层 {s.get('recall_accuracy', 0)*100:.0f}%）"
    )
    for k, v in s["by_kind"].items():
        print(
            f"  {k}: 正确 {v.get('正确', 0)} / 部分 {v.get('部分正确', 0)}"
            f" / 错误 {v.get('错误', 0)}（共 {v['n']}）"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
