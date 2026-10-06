"""从 data/text/*.jsonl 里挖真实数字，为出题准备标准答案。

评测的 ground truth 必须从原文 grep 出��，不能凭印象编——
判分才有意义，结论才能写真实结果。
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import TEXT_DIR  # noqa: E402


def load(code: str) -> list[dict]:
    fp = TEXT_DIR / f"{code}.jsonl"
    if not fp.exists():
        return []
    return [json.loads(l) for l in fp.open(encoding="utf-8") if l.strip()]


def find(code: str, *keywords: str, limit: int = 4, before: int = 60, after: int = 260):
    """在正文里找包含全部关键词的片段。"""
    hits = []
    for r in load(code):
        t = r["text"]
        for kw in keywords:
            if kw not in t:
                break
        else:
            i = t.find(keywords[0])
            hits.append(
                (r["page"], r["section"], t[max(0, i - before): i + after].replace("\n", " "))
            )
            if len(hits) >= limit:
                return hits
    return hits


def find_regex(code: str, pattern: str, limit: int = 3, after: int = 300):
    rx = re.compile(pattern)
    hits = []
    for r in load(code):
        for m in rx.finditer(r["text"]):
            s = m.start()
            hits.append(
                (r["page"], r["section"], r["text"][s: s + after].replace("\n", " "))
            )
            if len(hits) >= limit:
                return hits
    return hits


def show(title: str, hits) -> None:
    print(f"\n########## {title}")
    if not hits:
        print("  （未找到）")
    for p, sec, s in hits:
        print(f"  p{p} [{sec}]")
        print(f"     {s[:300]}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    if which in ("all", "broker"):
        show("券商：营业收入与净利润", find_regex("600030", r"营业总收入|营业收入.{0,40}?本报告期比上年"))
        show("中信证券 净利润", find("600030", "归属于母公司股东的净利润", limit=3))
        show("国泰君安 营收", find_regex("601211", r"营业收入.{0,30}?本报告期比上年", limit=2))
        show("华泰证券 营收", find_regex("601688", r"营业收入.{0,30}?本报告期比上年", limit=2))
        show("招商证券 营收", find_regex("600999", r"营业收入.{0,30}?本报告期比上年", limit=2))
        show("银河证券 营收", find_regex("601881", r"营业收入.{0,30}?本报告期比上年", limit=2))

    if which in ("all", "insurer"):
        show("中国人寿 新业务价值", find("601628", "一年新业务价值", limit=3))
        show("新华保险 新业务价值/内含价值", find("601336", "一年新业务价值", limit=3))
        show("太保 内含价值", find("601601", "内含价值", limit=2, after=300))
        show("人保 内含价值", find("601319", "内含价值", limit=2, after=300))
        show("中国人保 偿付能力", find("601319", "偿付能力充足率", limit=2, after=320))

    if which in ("all", "staff"):
        for code in ("600030", "601318", "601601"):
            show(f"{code} 员工人数", find_regex(code, r"(在职)?员工.{0,10}(人数|总数|共计)|职工人数", limit=2, after=200))