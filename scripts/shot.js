// 无头截图：用系统 Edge 打开问答页，抓几张图放docs/shots/
//
// 用法：
//   node scripts/shot.js                # 默认抓 4 张
//   node scripts/shot.js --port 8000
//
// 依赖 puppeteer-core + 系统已装浏览器，不需要下载 Chromium。
// 无 GPU 环境必须加 swiftshader 两个参数，否则黑屏。
'use strict';

const path = require('path');
const fs = require('fs');
const puppeteer = require('puppeteer-core');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const OUT = path.join(__dirname, '..', 'shots');
const argPort = process.argv.indexOf('--port');
const PORT = argPort > 0 ? process.argv[argPort + 1] : '8000';
const BASE = `http://127.0.0.1:${PORT}`;

// 页面要问的问题：覆盖事实型、业务指标、跨公司全景、陷阱型
const QUESTIONS = [
  { q: '中信证券2025年营业收入是多少？同比增长多少？', name: '1-事实型-营收' },
  { q: '中国人寿2025年的一年新业务价值是多少？同比增长多少？', name: '2-业务指标-新业务价值' },
  { q: '本次收录的券商中，2025年营业收入最高的是哪家公司？它的归母净利润是多少？', name: '3-跨公司全景' },
  { q: '根据这些年报，中信证券2026年的净利润预计是多少？', name: '4-陷阱型-看是否拒答' },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: 'new',
    args: [
      '--enable-unsafe-swiftshader',
      '--use-angle=swiftshader',
      '--disable-dev-shm-usage',
      '--no-sandbox',
    ],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1.5 });

    const errors = [];
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('requestfailed', (r) => errors.push('requestfailed: ' + r.url()));

    console.log('打开首页 ' + BASE);
    await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
    await sleep(2500);
    await page.screenshot({ path: path.join(OUT, '0-首页.png') });
    console.log('  -> shots/0-首页.png');

    // 逐题提问并截图
    for (const item of QUESTIONS) {
      console.log('提问: ' + item.q);
      // 清空输入框（页面是 <input id="q">，不是 textarea）
      await page.evaluate(() => {
        const el = document.getElementById('q');
        if (el) {
          el.value = '';
          el.dispatchEvent(new Event('input', { bubbles: true }));
        }
      });
      await page.type('#q', item.q, { delay: 8 });

      // 点提问按钮（id 固定为 go）
      await page.click('#go');

      // 等答案渲染：等「召回的原文片段」面板里出现 .chunk 元素
      try {
        await page.waitForFunction(
          () => document.querySelectorAll('#chunks .chunk').length > 0,
          { timeout: 90000 }
        );
      } catch (e) {
        console.log('  !! 等待答案超时');
      }
      await sleep(2500);
      const fp = path.join(OUT, item.name + '.png');
      await page.screenshot({ path: fp, fullPage: true });
      console.log('  -> shots/' + item.name + '.png');
    }

    // 单独抓一张点开引用角标后的图（展示出处与原文片段联动）
    console.log('抓取引用定位图');
    await page.evaluate(() => {
      const c = document.querySelector('#ans .cite');
      if (c) c.click();
    });
    await sleep(1800);
    await page.screenshot({ path: path.join(OUT, '5-引用定位到原文.png'), fullPage: true });
    console.log('  -> shots/5-引用定位到原文.png');

    if (errors.length) {
      console.log('\n页面错误:');
      errors.slice(0, 10).forEach((e) => console.log('  ' + e));
    } else {
      console.log('\n无页面错误');
    }
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error('截图失败:', e.message);
  process.exit(1);
});