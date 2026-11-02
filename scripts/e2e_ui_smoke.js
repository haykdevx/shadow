#!/usr/bin/env node
/**
 * Browser smoke test against a running Shadow instance.
 *
 *   SHADOW_URL=http://127.0.0.1:7000 SHADOW_USER=admin SHADOW_PASSWORD=... \
 *     node scripts/e2e_ui_smoke.js
 *
 * Logs in, opens every main workspace view, and fails (exit 1) on any
 * uncaught page error, failed request, or HTTP >= 400 to a same-origin URL.
 * Prints load timing and long-task counts so UI regressions are visible.
 * Requires Playwright with Chromium (`pip install playwright && playwright
 * install chromium`, or `npm i playwright`).
 */
const { chromium } = require('playwright');

const BASE = (process.env.SHADOW_URL || 'http://127.0.0.1:7000').replace(/\/$/, '');
const USER = process.env.SHADOW_USER || 'admin';
const PASS = process.env.SHADOW_PASSWORD;
if (!PASS) { console.error('SHADOW_PASSWORD is required'); process.exit(2); }

const VIEWS = [
  ['New chat', 'New chat'], ['Calendar', 'Calendar'], ['Compare', 'Compare'], ['Cookbook', 'Cookbook'],
  ['Email', 'Email'], ['Gallery', 'Gallery'], ['Library', 'Library'], ['Brain', '#rail-memory'],
  ['Notes', 'Notes'], ['Telegram', 'Telegram'], ['Tasks', 'Tasks'], ['Theme', '#rail-theme'],
  ['Settings', '#rail-settings'],
];

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1366, height: 850 } })).newPage();
  const pageErrs = [], consoleErrs = [], failed = [], bad = [];
  page.on('pageerror', e => pageErrs.push(String(e).slice(0, 300)));
  page.on('console', m => { if (m.type() === 'error') consoleErrs.push(m.text().slice(0, 300)); });
  page.on('requestfailed', r => failed.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`));
  page.on('response', r => { if (r.status() >= 400 && r.url().startsWith(BASE)) bad.push(`${r.status()} ${r.request().method()} ${r.url()}`); });

  let t = Date.now();
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
  console.log(`login page: ${Date.now() - t}ms`);
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  t = Date.now();
  await Promise.all([
    page.waitForURL(u => !u.toString().includes('/login'), { timeout: 20000 }),
    page.locator('#submitBtn').click(),
  ]);
  await page.waitForLoadState('networkidle');
  const nav = await page.evaluate(() => { const n = performance.getEntriesByType('navigation')[0]; return n ? `${Math.round(n.domContentLoadedEventEnd)}ms DCL, ${Math.round(n.loadEventEnd)}ms load, ${Math.round(n.transferSize / 1024)}KB` : '?'; });
  console.log(`workspace: ${Date.now() - t}ms to idle (${nav})`);

  let okViews = 0;
  for (const [label, sel] of VIEWS) {
    const before = pageErrs.length + consoleErrs.length;
    const t0 = Date.now();
    try {
      const el = sel.startsWith('#') ? page.locator(sel) : page.getByRole('button', { name: sel }).first();
      await el.click({ timeout: 4000, noWaitAfter: true });
      await page.waitForTimeout(500);
      const lt = await page.evaluate(() => new Promise(res => { const out = []; try { new PerformanceObserver(l => l.getEntries().forEach(e => out.push(Math.round(e.duration)))).observe({ type: 'longtask', buffered: true }); } catch {} setTimeout(() => res(out), 50); }));
      const errs = pageErrs.length + consoleErrs.length - before;
      console.log(`  ${label.padEnd(10)} ${String(Date.now() - t0).padStart(5)}ms  long-tasks>=100ms: ${lt.filter(d => d >= 100).length}  new errors: ${errs}`);
      if (!errs) okViews++;
      await page.keyboard.press('Escape');
      await page.waitForTimeout(150);
    } catch (e) {
      console.log(`  ${label.padEnd(10)} FAILED to open: ${String(e.message).split('\n')[0]}`);
    }
  }

  await browser.close();
  const report = (title, list) => { console.log(`${title}: ${list.length}`); [...new Set(list)].slice(0, 25).forEach(x => console.log('   ', x)); };
  report('page errors', pageErrs); report('console errors', consoleErrs); report('failed requests', failed); report('HTTP >= 400 (same origin)', bad);
  const ok = pageErrs.length === 0 && failed.length === 0 && bad.length === 0 && okViews === VIEWS.length;
  console.log(ok ? '\nSMOKE PASSED' : '\nSMOKE FAILED');
  process.exit(ok ? 0 : 1);
})().catch(e => { console.error('SMOKE CRASHED', e); process.exit(1); });
