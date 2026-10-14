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


# ---------------------------------------------------------------- 三档判定
GRADES = {
    "正确": "答案的关键值全部命中，且检索召回了目标公司与章节",
    "部分正确": "命中一部分关键值，或召回了目标公司但数值不全/精度不同",
    "错误": "关键值一个都没命中，或召回了错误的公司（看起来合理但结论错）",
}


def grade_one(
    spec: dict,
    answer: str,
    recall_ok: bool,
    gen_ok: bool,
    expect_hit: list[str],
    expect_missed: list[str],
    companies_recalled: list[str],
) -> tuple[str, list[str]]:
    """给单题下三档结论，并返回逐条证据理由。

    判定顺序有讲究——先看「检索层」，再看「生成层」，
    因为检索错了的时候生成层的对错没有意义（模型可能靠语料知识蒙对）。
    """
    reasons: list[str] = []
    is_reject = bool(spec.get("expect_reject"))

    # 调用失败 ≠ 答错。必须分开，否则会把「模型没返回内容」误记成「模型答错」。
    if not (answer or "").strip():
        reasons.append("❌ 大模型未返回任何内容（调用层失败，不是答错）")
        return "错误", reasons

    # 陷阱题：期望模型明确拒答
    if is_reject:
        low = normalize_num(answer)
        said_no = any(
            normalize_num(w) in low
            for w in ("未提及", "未披露", "没有", "无法", "不包含", "不含",
                      "不提供", "未预测", "未涉及", "无法确定", "不能确定")
        )
        # 同时检查是否编造了具体数字（有数字但声称没提到 = 自相矛盾）
        has_number = bool(re.search(r"\d+\.?\d*\s*亿", answer))
        if said_no and not has_number:
            reasons.append("模型明确回答「资料中未提及」，且未给出任何预测数字 → 正确")
            return "正确", reasons
        if said_no and has_number:
            reasons.append("模型嘴上说未提及，但文中有具体金额，自相矛盾 → 错误")
            return "错误", reasons
        reasons.append(
            "模型没有明确说「未提及」，而是给出了具体数字 → 幻觉"
        )
        return "错误", reasons

    # 检索层证据
    if not recall_ok:
        reasons.append(
            f"检索层失败：未召回目标公司（实际召回 {companies_recalled}），"
            "后面的生成再合理也不成立"
        )
    else:
        reasons.append(f"检索层命中：召回 {companies_recalled}")

    # 生成层证据
    if spec.get("expect_all"):
        if expect_missed:
            reasons.append(
                f"生成层部分正确：命中 {expect_hit}，缺失 {expect_missed}"
                + ("（关键值缺失 → 判部分正确）" if expect_hit else "")
            )
            if not expect_hit:
                reasons.append("关键值一个都没命中 → 判错误")
                return "错误", reasons
            return ("部分正确" if recall_ok else "错误"), reasons
        reasons.append(f"生成层全部命中：{expect_hit} → 判正确")
        return "正确", reasons

    if expect_hit:
        reasons.append(f"生成层命中期望值：{expect_hit}")
        return "正确", reasons
    reasons.append("生成层未命中任何期望值 → 判错误")
    return "错误", reasons


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

        # 三档判定 + 逐条证据理由（正确/部分正确/错误）
        grade, evidence = grade_one(
            spec, answer, recall_ok, gen_ok, hits, missing, companies
        )
        # LLM 调用失败要写进证据，否则「答错」与「没答」混为一谈
        if err and "未启用" not in str(err):
            evidence = list(evidence) + [f"LLM 调用异常：{err}"]
        rec["grade"] = grade
        rec["evidence"] = evidence
        rec["recall_layer"] = "通过" if recall_ok else "失败"
        rec["gen_layer"] = "通过" if gen_ok else "失败"

        # 跨公司全景题：逐公司列出「覆盖 / 缺失」，不能只给一个总布尔值
        if spec["kind"] == "跨公司全景":
            targets = need_companies or _panorama_targets(spec)
            # cited 存的是**块序号**（[1] [2] …），不是 chunk_id。
            # 早期版本拿 cited 去比 chunk_id 字符串，永远匹配不上，
            # 导致「被答案引用」全是「否」，连✅的题也显示没引用。
            cited_set = set(cited or [])
            coverage = []
            for comp in targets:
                # 注意：chunks 是检索器返回的原始切块，没有 idx 字段
                # （idx 只存在于 rec["chunks"] 那个列表里），要自己按序号枚举。
                idxs = [
                    i for i, c in enumerate(chunks, start=1)
                    if c["company"] == comp
                ]
                coverage.append({
                    "company": comp,
                    "covered": comp in companies,
                    "n_chunks": len(idxs),
                    "chunk_idxs": idxs,
                    "cited_in_answer": any(i in cited_set for i in idxs),
                })
            rec["per_company_coverage"] = {
                "targets": targets,
                "coverage": coverage,
                "covered": sum(1 for c in coverage if c["covered"]),
                "missing": [c["company"] for c in coverage if not c["covered"]],
                # 库里实际存在但没被点名、也没召回的公司
                "not_targeted_but_relevant": [
                    comp for comp in companies if comp not in targets
                ],
            }

        # fail_reasons 保留给既有文档使用；判定理由统一放 evidence
        rec["fail_reasons"] = evidence

        mark = {"正确": "✅ 正确", "部分正确": "⚠️ 部分正确", "错误": "❌ 错误"}[grade]
        print(f"    -> {mark} | 召回 {companies} | {rec['elapsed_s']}s"
              + (f" | {evidence[0][:60]}" if evidence else ""), flush=True)
        results.append(rec)

    # 三档统计：不用单一正确率掩盖「部分正确」这类中间态
    n_ok = sum(1 for x in results if x["grade"] == "正确")
    n_part = sum(1 for x in results if x["grade"] == "部分正确")
    n_bad = sum(1 for x in results if x["grade"] == "错误")
    summary = {
        "n_questions": len(results),
        "n_correct": n_ok,
        "n_partial": n_part,
        "n_wrong": n_bad,
        "accuracy": round(n_ok / max(1, len(results)), 3),
        "grade_definitions": GRADES,
        "by_kind": {},
        "recall_accuracy": round(
            sum(1 for x in results if x["recall_ok"]) / max(1, len(results)), 3
        ),
        "generated_at": now_str(),
        "topk": args.topk,
        "llm_enabled": llm_ok,
    }
    for rec in results:
        s = summary["by_kind"].setdefault(
            rec["kind"], {"n": 0, "正确": 0, "部分正确": 0, "错误": 0, "recall_ok": 0}
        )
        s["n"] += 1
        s["recall_ok"] += int(rec["recall_ok"])
        s[rec["grade"]] = s.get(rec["grade"], 0) + 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[{now_str()}] ===== 评测完成："
          f"正确 {n_ok} / 部分正确 {n_part} / 错误 {n_bad} "
          f"（共 {len(results)} 题）=====")
    print(f"    检索层命中率 {summary['recall_accuracy']*100:.0f}%")
    for k, v in summary["by_kind"].items():
        print(f"    {k}: 正确 {v['正确']} / 部分正确 {v.get('部分正确', 0)} "
              f"/ 错误 {v.get('错误', 0)}（共 {v['n']}，检索命中 {v['recall_ok']}）")
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
        # 库里登记名是「国泰君安」（代码 601211），但该公司 2025 年完成合并重组，
        # 年报正文自称「国泰海通」。三种写法都归到库里真实存在的名字上，
        # 否则 `_guess_companies` 会返回「国泰海通」而召回列表里是「国泰君安」，
        # 被误判成「检索漏掉了目标公司」。
        "国泰海通": "国泰君安", "国泰君安": "国泰君安", "国泰": "国泰君安",
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


# 跨公司「排名类」题目的应覆盖对象。
# 「哪家营收最高」这类题问句里没点名公司，但要在全部券商间比较——
# 判定时必须知道「应该覆盖谁」，否则只能给一个笼统的 true/false。
PANORAMA_RANK_BROKERS = [
    "中信证券", "国泰君安", "华泰证券", "广发证券", "中国银河", "招商证券",
    "申万宏源", "中信建投", "国信证券", "东吴证券", "浙商证券",
    "长江证券", "国金证券", "国元证券", "中金公司",
]


def _panorama_targets(spec: dict) -> list[str]:
    """推断跨公司题的「应覆盖对象」列表。"""
    q = spec["q"]
    named = _guess_companies(q)
    if named:
        return named
    if "券商" in q or "营业收入最高" in q:
        return list(PANORAMA_RANK_BROKERS)
    return []


if __name__ == "__main__":
    raise SystemExit(main())