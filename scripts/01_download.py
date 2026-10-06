"""从巨潮资讯（cninfo）批量下载 2025 年年报 PDF 全文。

用法：
    python scripts/01_download.py              # 下载全部 20 家
    python scripts/01_download.py 601318 600030  # 只下指定公司
    python scripts/01_download.py --force      # 忽略续传，重新下载

特性：
  * 一个接口覆盖沪深两市（category_ndbg_szsh = 年度报告）
  * 只取「XXXX年年度报告」正文，排除「摘要」「英文版」「更正后」等
  * 断点续传：已完整下载的文件跳过，中断的文件删除后重下
  * 限速 + 失败重试，避免给交易所站点造成压力
  * 校验 PDF 头尾，防止把错误页当成 PDF 存下来
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CNINFO_QUERY,
    CNINFO_STATIC,
    CNINFO_TOPSEARCH,
    COMPANIES,
    HEADERS,
    PDF_DIR,
    SE_DATE,
    clean_title,
    is_complete_pdf,
    now_str,
    polite_sleep,
)

MANIFEST = PDF_DIR / "manifest.json"
LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "logs" / "download.log"


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_org_id(code: str, session: requests.Session) -> str | None:
    """按股票代码反查巨潮内部 orgId。"""
    for attempt in range(3):
        try:
            r = session.post(
                CNINFO_TOPSEARCH,
                data={"keyWord": code, "maxNum": 10},
                headers=HEADERS,
                timeout=20,
            )
            items = r.json()
            for it in items or []:
                if it.get("code") == code:
                    return it.get("orgId")
            return None
        except Exception as e:  # noqa: BLE001
            log(f"  orgId 查询失败({attempt + 1}/3) {code}: {e}")
            time.sleep(2)
    return None


def pick_annual_report(titles: list[str], year: int) -> str | None:
    """从候选标题里挑出「正文年报」，排除摘要/英文版/更正版/审计报告等。

    注意标题写法不统一：既有「2025年年度报告」，也有「2025年度报告」。
    """
    banned = ("摘要", "英文", "更正", "取消", "修订", "审计报告", "内部控制",
              "社会责任", "ESG", "独立董事", "关于", "说明", "公告", "致股东")
    for t in titles:
        if f"{year}年年度报告" not in t and f"{year}年度报告" not in t:
            continue
        if any(b in t for b in banned):
            continue
        # 公司名 + 年报，且不能是「XX：YY公司年报」（子公司/参股）
        if t.endswith("年年度报告") or t.endswith("年度报告"):
            return t
    # 放宽：允许带冒号的全称
    for t in titles:
        if (f"{year}年年度报告" in t or f"{year}年度报告" in t) and not any(
            b in t for b in banned
        ):
            return t
    return None


def find_report(code: str, name: str, column: str, year: int,
                session: requests.Session) -> dict | None:
    """查某公司某年的年报，返回 {title, url, ...}。"""
    org_id = fetch_org_id(code, session)
    if not org_id:
        log(f"  {code} {name}: 查不到 orgId")
        return None

    payload = {
        "stock": f"{code},{org_id}",
        "tabName": "fulltext",
        "pageSize": 30,
        "pageNum": 1,
        "column": column,
        "category": "category_ndbg_szsh",  # 年度报告
        "plate": "",
        "seDate": SE_DATE,
        "isHLtitle": "true",
    }
    try:
        r = session.post(CNINFO_QUERY, data=payload, headers=HEADERS, timeout=30)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log(f"  {code} {name}: 查询异常 {e}")
        return None

    anns = data.get("announcements") or []
    entries = []
    for a in anns:
        title = clean_title(a.get("announcementTitle", ""))
        adj = a.get("adjunctUrl", "")
        if adj and (
            f"{year}年年度报告" in title or f"{year}年度报告" in title
        ):
            entries.append((title, adj))

    chosen = pick_annual_report([t for t, _ in entries], year)
    if not chosen:
        log(f"  {code} {name}: 未找到 {year} 年年报正文（候选 {[t for t,_ in entries]}）")
        return None

    adj = next(a for t, a in entries if t == chosen)
    return {
        "code": code,
        "name": name,
        "org_id": org_id,
        "title": chosen,
        "url": CNINFO_STATIC + adj,
        "relative": adj,
        "report_year": year,
    }


def download_pdf(info: dict, dest_dir: Path, session: requests.Session) -> dict:
    """下载单个 PDF，返回状态记录。已存在则跳过（断点续传）。"""
    code, name = info["code"], info["name"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dest_dir / f"{code}_{name}_{info['report_year']}年年度报告.pdf"

    record = dict(info)
    record["local_path"] = str(pdf_path.relative_to(dest_dir.parent.parent))

    if is_complete_pdf(pdf_path):
        log(f"  {code} {name}: 已存在，跳过（{pdf_path.stat().st_size / 1e6:.1f} MB）")
        record["status"] = "skipped"
        record["size"] = pdf_path.stat().st_size
        return record

    # 上次可能下到一半，先清掉
    if pdf_path.exists():
        pdf_path.unlink()

    for attempt in range(3):
        try:
            polite_sleep(0.5)
            with session.get(info["url"], headers=HEADERS, timeout=180, stream=True) as r:
                if r.status_code != 200:
                    log(f"  {code} {name}: HTTP {r.status_code}，重试")
                    time.sleep(3)
                    continue
                ctype = r.headers.get("Content-Type", "")
                tmp = pdf_path.with_suffix(".pdf.part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                size = tmp.stat().st_size
                if size < 20_000 or "pdf" not in ctype.lower():
                    tmp.unlink(missing_ok=True)
                    log(f"  {code} {name}: 返回的不是 PDF（{ctype}, {size}B），重试")
                    time.sleep(3)
                    continue
                tmp.rename(pdf_path)

            if not is_complete_pdf(pdf_path):
                pdf_path.unlink(missing_ok=True)
                log(f"  {code} {name}: PDF 不完整（无 EOF），重试")
                time.sleep(3)
                continue

            log(f"  {code} {name}: 下载完成 {size / 1e6:.1f} MB")
            record["status"] = "downloaded"
            record["size"] = size
            return record
        except Exception as e:  # noqa: BLE001
            log(f"  {code} {name}: 下载异常({attempt + 1}/3) {e}")
            time.sleep(3)

    record["status"] = "failed"
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", help="只下这些股票代码")
    ap.add_argument("--force", action="store_true", help="忽略续传，重新下载")
    args = ap.parse_args()

    targets = COMPANIES
    if args.codes:
        wanted = set(args.codes)
        targets = [c for c in COMPANIES if c[0] in wanted]
    if not targets:
        log("没有匹配到公司")
        return 1

    if args.force:
        for code, name, _ in targets:
            d = PDF_DIR / f"{code}_{name}"
            for p in d.glob("*年度报告.pdf"):
                p.unlink()
        log("已清空目标公司的 PDF，将重新下载")

    manifest = load_manifest()
    session = requests.Session()
    ok = fail = skip = 0

    log(f"===== 开始下载 {len(targets)} 家公司 {REPORT_YEAR if False else ''}年报 =====".strip())
    for i, (code, name, column) in enumerate(targets, 1):
        log(f"[{i}/{len(targets)}] {code} {name}")
        info = find_report(code, name, column, 2025, session)
        if not info:
            fail += 1
            manifest[code] = {"code": code, "name": name, "status": "no_report"}
            save_manifest(manifest)
            continue
        rec = download_pdf(info, PDF_DIR / f"{code}_{name}", session)
        manifest[code] = rec
        save_manifest(manifest)  # 每家都落盘，中断也不丢进度
        if rec["status"] == "downloaded":
            ok += 1
        elif rec["status"] == "skipped":
            skip += 1
        else:
            fail += 1

    log(f"===== 结束：新下载 {ok}，跳过 {skip}，失败 {fail} =====")
    log(f"清单已写入 {MANIFEST}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
