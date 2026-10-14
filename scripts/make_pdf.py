"""把 docs/*.md 渲染成 PDF，并**断言页数**。

为什么要自己生成：结论.pdf 之前是用 Typora 导的，出过 4 页（作业要求一页）。
本脚本用 markdown 转 HTML，再用本机 Edge 的 headless 打印 PDF，
最后用 PyMuPDF 读回真实页数做断言——不靠肉眼估计。

用法：
python scripts/make_pdf.py              # 渲染全部
python scripts/make_pdf.py --only 结论   # 只渲染结论（断言必须 1 页）
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# 依次尝试本机可用浏览器
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# 每份文档的期望页数上限（None = 不限制）
PAGE_LIMIT = {
    "结论": 1,      # 作业硬性要求：一页
    "评测记录": None,
    "语料来源清单": None,
}

CSS = """
@page { size: A4; margin: 14mm 13mm; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 9.2pt; line-height: 1.5; color: #1a1a1a;
}
h1 { font-size: 15pt; margin: 0 0 6pt; border-bottom: 1.6px solid #333;
     padding-bottom: 3pt; }
h2 { font-size: 11pt; margin: 9pt 0 4pt; border-left: 3px solid #666;
     padding-left: 5px; }
h3 { font-size: 9.8pt; margin: 7pt 0 3pt; }
p, li { margin: 2.5pt 0; }
ul, ol { padding-left: 16pt; margin: 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0;
        font-size: 8.2pt; page-break-inside: avoid; }
th, td { border: 0.6px solid #bbb; padding: 2.6pt 4pt; text-align: left;
         vertical-align: top; }
th { background: #f2f2f2; font-weight: 600; }
code { background: #f4f4f4; padding: 0 2.5pt; border-radius: 2px;
       font-family: Consolas, monospace; font-size: 8.4pt; }
blockquote { border-left: 2.5px solid #ccc; margin: 4pt 0; padding-left: 8pt;
             color: #444; }
strong { font-weight: 600; }
hr { border: none; border-top: 0.6px solid #ddd; margin: 6pt 0; }
a { color: #1a4a7a; text-decoration: none; }
"""


def find_browser() -> str | None:
    for p in BROWSERS:
        if Path(p).exists():
            return p
    return None


def pdf_pages(p: Path) -> int:
    import pymupdf

    with pymupdf.open(p) as d:
        return d.page_count


def render(md_path: Path, browser: str) -> Path:
    import markdown

    html_body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "toc"],
    )
    title = md_path.stem
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title><style>{CSS}</style></head>
<body>{html_body}</body></html>"""

    tmp = Path(tempfile.mkdtemp())
    html_file = tmp / f"{title}.html"
    html_file.write_text(html, encoding="utf-8")
    out = md_path.with_suffix(".pdf")
    if out.exists():
        out.unlink()

    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        f"--print-to-pdf={out}",
        "--no-pdf-header-footer",
        html_file.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    shutil.rmtree(tmp, ignore_errors=True)
    if not out.exists():
        raise RuntimeError(
            f"PDF 未生成\nstdout: {proc.stdout.decode(errors='ignore')[:400]}\n"
            f"stderr: {proc.stderr.decode(errors='ignore')[:400]}"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只渲染指定文件名（不含扩展名）")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("未找到 Edge/Chrome，无法生成 PDF")
        return 1
    print(f"浏览器：{browser}")

    targets = (
        [DOCS / f"{args.only}.md"]
        if args.only
        else sorted(DOCS.glob("*.md"))
    )
    if not targets:
        print(f"{DOCS} 下没有 .md")
        return 1

    failed = False
    for md in targets:
        if not md.exists():
            print(f"  跳过 {md.name}（不存在）")
            continue
        pdf = render(md, browser)
        n = pdf_pages(pdf)
        limit = PAGE_LIMIT.get(md.stem)
        size_kb = pdf.stat().st_size / 1024
        if limit is None:
            print(f"  ✅ {md.name} → {pdf.name}  {n} 页 / {size_kb:.0f} KB")
        else:
            ok = n <= limit
            failed = failed or not ok
            flag = "✅" if ok else "❌"
            print(
                f"  {flag} {md.name} → {pdf.name}  {n} 页 / {size_kb:.0f} KB"
                f"（要求 ≤ {limit} 页）"
            )
            if not ok:
                print(f"     超了 {n - limit} 页，需要再压缩内容")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
