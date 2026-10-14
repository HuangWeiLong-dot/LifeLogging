/**
 * 用无头 Chrome 真正渲染 Grafana 仪表盘，把每个面板的标题和值打出来。
 *
 * **为什么需要它**
 *
 * 这个项目踩过两个「查得到但显示不出来」的坑，**两个都只有走完整前端才暴露**：
 *
 *   1. SQL 写进 `queryText` 而不是 `rawQueryText` —— 前端会用空串覆盖它，
 *      插件执行空查询、返回空帧、面板显示 "No data"，而日志里全是 `status=ok`。
 *   2. 数据源没配 `busy_timeout` —— 打开仪表盘时十几个面板查询同时发出，
 *      必然撞锁，每个面板报 "database is locked"。
 *
 * 这两个用 `curl /api/ds/query` 都验证不出来：
 * 后端认得 `queryText`，而且单个查询不会撞锁。
 *
 * **用法**
 *
 *   node docker/grafana/verify_dashboards.mjs
 *   node docker/grafana/verify_dashboards.mjs http://localhost:3001/d/xxx
 *
 * 环境变量：
 *   GRAFANA_URL   默认 http://127.0.0.1:3001
 *   GRAFANA_USER  默认 admin
 *   GRAFANA_PASS  默认 admin
 *   CHROME_PATH   默认自动在常见路径里找
 *
 * 依赖：Node 18+（用到内置的全局 WebSocket 和 fetch），不装任何 npm 包。
 */

import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = (process.env.GRAFANA_URL || 'http://127.0.0.1:3001').replace(/\/$/, '');
const USER = process.env.GRAFANA_USER || 'admin';
const PASS = process.env.GRAFANA_PASS || 'admin';
const PORT = 9226;

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

const chromePath = CHROME_CANDIDATES.find(p => existsSync(p));
if (!chromePath) {
  console.error('找不到 Chrome/Edge。用 CHROME_PATH 环境变量指一个。');
  process.exit(2);
}

// 默认验证本仓库 provisioning 的三个仪表盘
const urls = process.argv.slice(2).length
  ? process.argv.slice(2)
  : [`${BASE}/d/lifelogging-overview`, `${BASE}/d/lifelogging-time`, `${BASE}/d/lifelogging-location`];

const AUTH = 'Basic ' + Buffer.from(`${USER}:${PASS}`).toString('base64');
const profile = mkdtempSync(join(tmpdir(), 'grafana-verify-'));

const chrome = spawn(chromePath, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--window-size=1600,1200', `--user-data-dir=${profile}`,
  `--remote-debugging-port=${PORT}`, 'about:blank',
], { stdio: 'ignore' });

let ws, chromeDown = false;
const stop = () => { try { ws?.close(); } catch { } if (!chromeDown) { chromeDown = true; chrome.kill(); } };
process.on('exit', stop);
process.on('SIGINT', () => { stop(); process.exit(130); });

try {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    try { await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); ready = true; break; }
    catch { await sleep(250); }
  }
  if (!ready) throw new Error('Chrome 的调试端口没起来');

  const target = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === 'page');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise(res => {
    const id = ++seq; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params }));
  });
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m.error); pending.delete(m.id); }
  });

  const evaluate = async expr =>
    (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }))?.result?.value;

  await send('Network.enable');
  // URL 里带凭据会被 Chrome 剥掉，认证头得走 CDP 注入
  await send('Network.setExtraHTTPHeaders', { headers: { Authorization: AUTH } });
  await send('Page.enable');

  let problems = 0;
  for (const url of urls) {
    await send('Page.navigate', { url });
    await sleep(9000);

    // Grafana 的面板是懒渲染的：不滚到底，下面的面板根本不会渲染，也就查不出来
    for (let i = 0; i < 8; i++) {
      await evaluate('window.scrollBy(0, 900)');
      await sleep(1200);
    }
    await evaluate('window.scrollTo(0,0)');
    await sleep(1200);

    const raw = await evaluate(`(() => {
      const txt = document.body.innerText || '';
      const titles = Array.from(document.querySelectorAll('[data-testid^="data-testid Panel header"]'))
          .map(e => e.innerText.replace(/\\n/g, ' | ').trim()).filter(Boolean);
      return JSON.stringify({
        onLogin: /Email or username|Forgot your password/i.test(txt),
        noData:  (txt.match(/No data/gi) || []).length,
        locked:  (txt.match(/locked/gi) || []).length,
        titles,
      });
    })()`);

    const r = JSON.parse(raw);
    const name = url.replace(BASE, '') || '/';
    const bad = r.onLogin || r.noData || r.locked;
    if (bad) problems++;
    console.log(`\n${bad ? '❌' : '✅'} ${name}`);
    if (r.onLogin) console.log('   ⚠️ 停在登录页 —— 检查 GRAFANA_USER / GRAFANA_PASS');
    if (r.noData) console.log(`   ⚠️ ${r.noData} 个面板显示 No data（查 rawQueryText / 数据源）`);
    if (r.locked) console.log(`   ⚠️ ${r.locked} 处 database is locked（查 busy_timeout）`);
    if (!r.titles.length) console.log('   ⚠️ 一个面板都没渲染出来');
    for (const t of r.titles) console.log('   -', t.slice(0, 150));
  }

  console.log(problems ? `\n有 ${problems} 个仪表盘有问题。` : '\n全部正常。');
  stop();
  process.exit(problems ? 1 : 0);
} catch (err) {
  console.error('失败:', err.message);
  stop();
  process.exit(2);
}
