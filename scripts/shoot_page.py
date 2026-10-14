"""给问答页面截图（交付物之一：页面截图）。

为什么要脚本重截而不是复用旧图：索引已从 19 家换成 20 家，
旧截图里的页码、公司列表与当前代码不一致。截图必须与提交时的代码同源，
否则等于提供了假证据。

流程：启动服务 → 无头浏览器逐题提问 → 等答案渲染完 → 截图 → 关浏览器。
每张图都带问题与答案，肉眼即可判断「答案有没有出处、角标能不能点回原文」。

用法：python scripts/shoot_page.py --port 8010
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "shots"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

# (文件名, 问题) ——覆盖首页 + 四种题型，含翻车的那道跨公司题
QUESTIONS = [
    ("1-事实型-营收", "中信证券2025年营业收入是多少？同比增长多少？"),
    ("2-业务指标-新业务价值", "中国太保2025年集团内含价值和归属于母公司股东的净利润分别是多少？"),
    ("3-跨公司全景-翻车题", "本次收录的券商中，2025年营业收入最高的是哪家公司？它的归母净利润是多少？"),
    ("4-陷阱型-看是否拒答", "根据这些年报，中信证券2026年的净利润预计是多少？"),
    ("5-改名公司-国泰海通", "国泰海通2025年营业收入和归母净利润分别是多少？"),
]


def find_browser() -> str | None:
    for p in BROWSERS:
        if Path(p).exists():
            return p
    return None


def wait_ready(url: str, timeout: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{url}/api/stats", timeout=10) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise TimeoutError(f"服务未在 {timeout}s 内就绪：{url}")


SCRIPT = r"""
const puppeteer = require('puppeteer-core');
(async () => {
  const EDGE = process.argv[2];
  const BASE = process.argv[3];
  const SHOTS = process.argv[4];
  const jobs = JSON.parse(process.argv[5]);
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: 'new',
    args: ['--enable-unsafe-swiftshader','--use-angle=swiftshader'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1.4 });
  await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 90000 });
  await page.waitForFunction(
    () => { const s = document.getElementById('stats');
            return s && s.innerText && !s.innerText.includes('加载中'); },
    { timeout: 60000 }
  );
  await new Promise(r => setTimeout(r, 1500));
  await page.screenshot({ path: SHOTS + '/0-首页.png' });
  console.log('  已截图 0-首页');

  for (const job of jobs) {
    await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 90000 });
    await new Promise(r => setTimeout(r, 1000));
    await page.waitForSelector('#q', { timeout: 30000 });
    await page.click('#q', { clickCount: 3 });
    await page.type('#q', job.q, { delay: 6 });
    // 页面是按钮触发（#go onclick=ask()），不是回车
    await page.click('#go');
    try {
      await page.waitForFunction(() => {
        const a = document.getElementById('ans');
        if (!a) return false;
        const t = a.innerText || '';
        return t.length > 40 && !t.includes('检索并生成回答');
      }, { timeout: 100000 });
    } catch (e) {
      console.error('  等待答案超时:', job.name);
    }
    // 等右侧「召回的原文片段」也渲染出来
    try {
      await page.waitForFunction(() => {
        const c = document.getElementById('chunks');
        return c && c.innerText && c.innerText.trim().length > 30;
      }, { timeout: 40000 });
    } catch (e) { /* 片段区可能为空，不阻塞截图 */ }
    await new Promise(r => setTimeout(r, 1800));
    await page.screenshot({ path: SHOTS + '/' + job.name + '.png', fullPage: true });
    console.log('  已截图', job.name);
  }
  await browser.close();
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"

    browser = find_browser()
    if not browser:
        print("未找到 Edge/Chrome")
        return 1
    SHOTS.mkdir(parents=True, exist_ok=True)
    wait_ready(url)
    print(f"服务就绪：{url}")

    try:
        import puppeteer_core  # noqa: F401
    except ImportError:
        # puppeteer-core 是 Node 模块，装在受管 Node 工作区里，
        # 不在 Python 环境里——所以这里检测不到是正常的，
        # 真正要做的只是把 NODE_PATH 传给 node。直接放行。
        pass

    js = ROOT / "shots" / "_shoot.js"
    js.write_text(SCRIPT, encoding="utf-8")
    jobs = [{"name": n, "q": q} for n, q in QUESTIONS]
    print(f"准备截 {len(jobs) + 1} 张图")

    # puppeteer-core 装在受管工作区里，node 也要用受管版本，
    # 否则可能落到系统 node 上找不到模块。
    node = shutil.which("node") or "node"
    env = dict(os.environ)
    ws = Path.home() / ".workbuddy" / "binaries" / "node" / "workspace"
    cand = ws / "node_modules"
    if cand.exists():
        prev = env.get("NODE_PATH", "")
        env["NODE_PATH"] = str(cand) + ((";" + prev) if prev else "")

    proc = subprocess.run(
        [node, str(js), browser, url, str(SHOTS), json.dumps(jobs, ensure_ascii=False)],
        capture_output=True,
        timeout=1200,
        env=env,
    )
    print(proc.stdout.decode(errors="ignore"))
    err = proc.stderr.decode(errors="ignore")
    if err.strip():
        print("stderr:", err[-1200:])
    js.unlink(missing_ok=True)

    pngs = sorted(SHOTS.glob("[0-9]*.png"))
    print(f"\n完成，共 {len(pngs)} 张：")
    for p in pngs:
        print(f"  {p.name}  {p.stat().st_size // 1024} KB")
    return 0 if pngs else 1


if __name__ == "__main__":
    raise SystemExit(main())