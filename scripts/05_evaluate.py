"""出 10 道题跑评测，逐题记录「召回了哪些块、答得对不对、错在哪」。

题目设计（8 道单公司 + 2 道跨公司全景）：
  * 事实型：直接问某个数字（营收、净利润、内含价值）
  * 业务指标：保险公司的寿险特有指标（新业务价值）
  * 表格型：问表格里的数（考验表格切块与还原）
  * 对比型：跨公司比较（最难，考验混合检索与长上下文）
  * 陷阱型：问库里根本没有的信息（看模型会不会编）

**所有 truth 都是从 data/text/<code>.jsonl 里grep 出来的原文数字**
（用 scripts/mine_truth.py 挖），不是编的——判分才能反映真实表现。
出处格式：公司 + PDF页码 + 章节。

判分不看字符串全等，而是「关键数字/名称是否命中」：
  * expect_all 里的每个数字都命中才算对（数字型题目用这个，更严格）
  * expect 里任一命中即算对
  * 陷阱题期望模型明确说「库里没有」

产出 data/index/eval_results.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import INDEX_DIR, now_str  # noqa: E402

# 每题：
#   q           问题
#   expect      期望命中的关键值（任一命中即算对）
#   expect_all  期望**全部**命中的关键值（数字型题目用，更严格）
#   expect_reject  True 表示陷阱题，期望模型明确说「没有」
#   kind        题型，用于最后分组统计
#   truth       从原文核过的标准答案（含出处），写进记录方便人工复核
QUESTIONS = [
    # ---------------- 单公司·事实型 ----------------
    {
        "q": "中信证券2025年营业收入是多少？同比增长多少？",
        "expect": ["748.54", "28.79"],
        "expect_all": ["748.54", "28.79"],
        "kind": "事实型",
        "truth": "营业收入 748.54 亿元，同比增长 28.79%（p37 管理层讨论与分析）",
    },
    {
        "q": "国泰海通2025年营业收入和归母净利润分别是多少？同比增长幅度是多少？",
        "expect": ["631.07", "278.09", "87.40", "113.52"],
        "expect_all": ["631.07", "278.09"],
        "kind": "事实型",
        "truth": "营业收入 631.07 亿元(+87.40%)；归母净利润 278.09 亿元(+113.52%)（p48 管理层讨论与分析）",
    },
    {
        "q": "广发证券2025年营业总收入和归属于上市公司股东的净利润是多少？",
        "expect": ["354.93", "137.02", "34.33", "42.18"],
        "expect_all": ["354.93", "137.02"],
        "kind": "事实型",
        "truth": "营业总收入 354.93 亿元(+34.33%)；归母净利润 137.02 亿元(+42.18%)（p30 管理层讨论与分析）",
    },
    # ---------------- 单公司·业务指标（寿险特有）----------------
    {
        "q": "中国人寿2025年的一年新业务价值是多少？同比增长多少？",
        "expect": ["457.52", "35.7"],
        "expect_all": ["457.52"],
        "kind": "业务指标",
        "truth": "一年新业务价值 457.52 亿元，同比增长 35.7%（p11 业务综述；p7 财务摘要）",
    },
    {
        "q": "新华保险2025年的一年新业务价值和原保险保费收入是多少？同比分别增长多少？",
        "expect": ["98.42", "1,958.71", "195,871", "57.4", "14.9"],
        "expect_all": ["98.42", "57.4"],
        "kind": "业务指标",
        "truth": "一年新业务价值 98.42 亿元(+57.4%)；原保险保费收入 1,958.71 亿元(+14.9%)（p55 利源分析；p19 公司信息）",
    },
    {
        "q": "中国太保2025年集团内含价值和归属于母公司股东的净利润分别是多少？",
        "expect": ["613,365", "6133.65", "6,133.65", "53,505", "535.05", "9.1", "19.0"],
        "expect_all": ["53,505", "9.1"],
        "kind": "业务指标",
        "truth": "集团内含价值 6,133.65 亿元(+9.1%)；归母净利润 535.05 亿元(+19.0%)（p21 经营业绩回顾与分析）",
    },
    # ---------------- 单公司·监管指标 ----------------
    {
        "q": "中国人寿2025年末的综合偿付能力充足率和核心偿付能力充足率分别是多少？",
        "expect": ["174.01", "128.77"],
        "expect_all": ["174.01", "128.77"],
        "kind": "事实型",
        "truth": "综合偿付能力充足率 174.01%；核心偿付能力充足率 128.77%（p14 保险业务分析；p25 偿付能力状况）",
    },
    # ---------------- 跨公司全景（2 道，作业硬性要求）----------------
    {
        "q": "本次收录的券商中，2025年营业收入最高的是哪家公司？它的归母净利润是多少？",
        "expect": ["中信", "748.54", "300.76"],
        "expect_all": ["中信", "748.54"],
        "kind": "跨公司全景",
        "truth": "中信证券营收 748.54 亿元居首，归母净利润 300.76 亿元（p37）。"
                 "本库券商营收排序：中信 748.54 > 国泰海通 631.07 > 华泰 358.10 > 广发 354.93 > 银河 283.02 > 招商 249.72",
    },
    {
        "q": "太保、招商证券、中国人寿三家公司2025年末的在职员工总数分别是多少？",
        "expect": ["91,639", "91639", "12,792", "12792", "97,505", "97505"],
        "expect_all": ["91,639", "12,792", "97,505"],
        "kind": "跨公司全景",
        "truth": "太保 91,639 人（p110 公司治理情况）；招商证券 12,792 人（p95 公司治理、环境和社会）；"
                 "中国人寿 97,505 人（p59 公司治理报告）",
    },
    # ---------------- 陷阱型 ----------------
    {
        "q": "根据这些年报，中信证券2026年的净利润预计是多少？",
        "expect": [
            "未提及", "未披露", "没有", "无法", "不包含", "不含", "不提供",
            "未预测", "未涉及", "无法确定", "不能确定", "无从",
        ],
        "expect_reject": True,
        "kind": "陷阱型",
        "truth": "库里只有 2025 实际数，没有 2026 预测，模型应明确说资料中未提及，"
                 "若给出一个具体数字即为幻觉",
    },
]


def normalize_num(s: str) -> str:
    return re.sub(r"[,\s，]", "", s)


def hit_expect(answer: str, expect: list[str]) -> tuple[bool, list[str]]:
    """任一命中即算对。返回 (是否命中, 命中列表)。"""
    if not expect:
        return True, []
    low = normalize_num(answer).lower()
    hit = [e for e in expect if normalize_num(e).lower() in low]
    return (len(hit) > 0), hit


def hit_expect_all(answer: str, expect_all: list[str]) -> tuple[bool, list[str], list[str]]:
    """全部命中才算对。返回 (是否全中, 命中列表, 缺失列表)。"""
    if not expect_all:
        return True, [], []
    low = normalize_num(answer).lower()
    hit, miss = [], []
    for e in expect_all:
        (hit if normalize_num(e).lower() in low else miss).append(e)
    return (not miss), hit, miss


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--no-llm", action="store_true", help="只测检索，不调大模型")
    ap.add_argument("-o", "--out", default=str(INDEX_DIR / "eval_results.json"))
    args = ap.parse_args()

    from retriever import HybridRetriever

    r = HybridRetriever()
    print(f"[{now_str()}] 索引 {len(r.chunks)} 块 / {r.vectors.shape[1]} 维，"
          f"开始 {len(QUESTIONS)} 题评测", flush=True)

    llm_ok = not args.no_llm
    if llm_ok:
        try:
            import llm  # noqa: F401
        except ImportError:
            llm_ok = False

    results = []
    for i, spec in enumerate(QUESTIONS, start=1):
        t0 = time.time()
        print(f"[{now_str()}] ({i}/{len(QUESTIONS)}) {spec['q']}", flush=True)
        res = r.search(spec["q"], final_topk=args.topk)
        chunks = res["chunks"]

        # --- 检索层：召回了哪些公司/章节，是否含目标公司 ---
        companies = sorted({c["company"] for c in chunks})
        sections = sorted({c["section"] for c in chunks if c["section"]})
        # 跨公司题会点名多家，全部都要召回才算检索过关
        need_companies = _guess_companies(spec["q"])
        missed_companies = [c for c in need_companies if c not in companies]
        company_ok = not missed_companies

        # --- 生成层 ---
        cited: list = []
        hallucinated: list = []
        if llm_ok:
            from llm import call_deepseek

            out = call_deepseek(spec["q"], chunks)
            answer = out["answer"]
            err = out["error"]
            cited = out.get("used", [])
            hallucinated = out.get("hallucinated_cites", [])
        else:
            answer = ""
            err = "未启用 LLM"

        # --- 判分 ---
        # 检索层：目标公司是否在召回里
        recall_ok = company_ok
        # 生成层：严格判分（expect_all 全中才算对，退化到 expect 任一命中）
        if spec.get("expect_all"):
            gen_ok, hits, missing = hit_expect_all(answer, spec["expect_all"])
            judge_by = "expect_all"
        else:
            gen_ok, hits = hit_expect(answer, spec.get("expect", []))
            missing = []
            judge_by = "expect_any"
        # 陷阱题：以「是否明确说没有」为准
        if spec.get("expect_reject"):
            gen_ok, hits = hit_expect(answer, spec["expect"])
            judge_by = "expect_reject"

        rec = {
            "no": i,
            "question": spec["q"],
            "kind": spec["kind"],
            "truth": spec["truth"],
            "judge_by": judge_by,
            "expect": spec.get("expect", []),
            "expect_all": spec.get("expect_all", []),
            "expect_reject": bool(spec.get("expect_reject")),
            "n_vector_candidates": res["n_candidate_vector"],
            "n_bm25_candidates": res["n_candidate_bm25"],
            "n_chunks": len(chunks),
            "companies_recalled": companies,
            "sections_recalled": sections,
            "recall_ok": recall_ok,
            "need_companies": need_companies,
            "missed_companies": missed_companies,
            "answer": answer,
            "llm_error": err,
            "cited": cited,
            "hallucinated_cites": hallucinated,
            "expect_hit": hits,
            "expect_missed": missing,
            "gen_ok": gen_ok,
            # 总判定：检索命中目标公司 且 生成答案达标
            "correct": bool(recall_ok and gen_ok),
            "chunks": [
                {
                    "idx": j,
                    "chunk_id": c["chunk_id"],
                    "company": c["company"],
                    "section": c["section"],
                    "page": c["page"],
                    "kind": c["kind"],
                    "rrf_score": round(c["rrf_score"], 5),
                    "vector_rank": c["retrieval"].get("vector_rank"),
                    "bm25_rank": c["retrieval"].get("bm25_rank"),
                    "snippet": c["text"][:260],
                }
                for j, c in enumerate(chunks, start=1)
            ],
            "elapsed_s": round(time.time() - t0, 1),
        }

        # 记录错在哪：分检索层与生成层
        reasons = []
        if not recall_ok:
            reasons.append(
                f"检索漏掉了 {missed_companies}，只召回了 {companies}"
            )
        if not gen_ok:
            if missing:
                reasons.append(f"生成答案缺少关键值 {missing}")
            else:
                reasons.append("生成答案未命中任何期望值")
        rec["fail_reasons"] = reasons

        mark = "对" if rec["correct"] else ("错-检索" if not recall_ok else "错-生成")
        print(f"    -> {mark} | 召回 {companies} | {rec['elapsed_s']}s"
              + (f" | {reasons[0][:60]}" if reasons else ""), flush=True)
        results.append(rec)

    n_ok = sum(1 for x in results if x["correct"])
    summary = {
        "n_questions": len(results),
        "n_correct": n_ok,
        "accuracy": round(n_ok / max(1, len(results)), 3),
        "by_kind": {},
        "recall_accuracy": round(
            sum(1 for x in results if x["recall_ok"]) / max(1, len(results)), 3
        ),
        "generated_at": now_str(),
        "topk": args.topk,
        "llm_enabled": llm_ok,
    }
    for rec in results:
        s = summary["by_kind"].setdefault(rec["kind"], {"n": 0, "ok": 0, "recall_ok": 0})
        s["n"] += 1
        s["recall_ok"] += int(rec["recall_ok"])
        s["ok"] += int(rec["correct"])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[{now_str()}] ===== 评测完成：{n_ok}/{len(results)} 题答对 =====")
    print(f"    检索层命中率 {summary['recall_accuracy']*100:.0f}%")
    for k, v in summary["by_kind"].items():
        print(f"    {k}: {v['ok']}/{v['n']}（检索命中 {v['recall_ok']}/{v['n']}）")
    print(f"明细已存 {out_path}")
    return 0


def _guess_companies(q: str) -> list[str]:
    """从问题里猜它想问哪几家公司，用于检查召回公司是否对。

    跨公司全景题会点名多家（如「太保、招商证券、中国人寿」），
    全部都要召回才算检索过关——这正是这类题最难的地方。
    """
    names = {
        "中国平安": "中国平安", "平安": "中国平安",
        "中信证券": "中信证券",
        "中国人寿": "中国人寿", "国寿": "中国人寿",
        "新华保险": "新华保险",
        "招商证券": "招商证券", "招商": "招商证券",
        "华泰证券": "华泰证券", "华泰": "华泰证券",
        "中国太保": "中国太保", "太保": "中国太保",
        "中国人保": "中国人保", "人保": "中国人保",
        "国泰君安": "国泰君安", "国泰海通": "国泰海通", "国泰": "国泰海通",
        "广发证券": "广发证券", "广发": "广发证券",
        "中国银河": "中国银河", "银河": "中国银河",
        "中信建投": "中信建投",
        "国信证券": "国信证券",
        "东吴证券": "东吴证券",
        "浙商证券": "浙商证券",
        "长江证券": "长江证券",
        "国金证券": "国金证券",
        "申万宏源": "申万宏源",
        "国元证券": "国元证券",
        "中金公司": "中金公司",
    }
    # 长名优先：先匹配「中国平安」再匹配「平安」，否则短名会抢先
    out: list[str] = []
    for k in sorted(names, key=len, reverse=True):
        if k in q and names[k] not in out:
            out.append(names[k])
    return out


if __name__ == "__main__":
    raise SystemExit(main())