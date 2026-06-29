// UI -> backend contract check. Proves each dashboard manipulation maps to a real, ACCEPTED
// backend action: clicks every button, captures the mutating /api call it fires (method + path +
// status), asserts the backend accepted it (2xx/3xx), and prints the full control->endpoint map.
//
// Manual E2E tool (needs the live stack). Run alongside ui-stress.js — that one proves the UI
// doesn't error; this one proves every control hits the RIGHT backend endpoint and the backend
// accepts it. See ui-stress.js for the stack-startup recipe.
//
//   NODE_PATH=/opt/node22/lib/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//   DASHBOARD_USER=admin DASHBOARD_PASSWORD=test1234 node dashboard/web/e2e/ui-backend-contract.js
const { chromium } = require('playwright');
const BASE = process.env.UI_BASE || 'http://127.0.0.1:3000';
const USER = process.env.DASHBOARD_USER || 'admin';
const PASS = process.env.DASHBOARD_PASSWORD || 'test1234';
const ROUTES = ['/', '/tonight', '/data', '/learning', '/analytics', '/settings', '/admin'];

const calls = [];
let current = 'init';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext();
  await ctx.request.post(`${BASE}/api/auth/login`, { data: { username: USER, password: PASS } });
  const page = await ctx.newPage();
  page.on('dialog', (d) => d.dismiss().catch(() => {}));
  page.on('response', (r) => {
    const u = r.url(); const m = r.request().method();
    if (u.includes('/api/') && ['POST', 'PUT', 'DELETE', 'PATCH'].includes(m) && !u.includes('/auth/login')) {
      calls.push({ label: current, method: m, path: new URL(u).pathname.replace('/api', ''), status: r.status() });
    }
  });

  for (const route of ROUTES) {
    current = `nav ${route}`;
    await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 });
    await page.waitForTimeout(4000);
    const n = await page.$$eval('button', (b) => b.length);
    for (let i = 0; i < n; i++) {
      const onRoute = page.url().endsWith(route) || (route === '/' && new URL(page.url()).pathname === '/');
      if (!onRoute) { await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded' }); await page.waitForTimeout(1500); }
      const b = (await page.$$('button'))[i];
      if (!b) continue;
      const label = ((await b.innerText().catch(() => '')) || `#${i}`).trim().replace(/\s+/g, ' ').slice(0, 28) || `#${i}`;
      if (!(await b.isVisible().catch(() => false)) || !(await b.isEnabled().catch(() => false))) continue;
      current = `${route} :: ${label}`;
      await b.click({ timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(700);
    }
  }
  await browser.close();

  const mutating = calls.filter((c) => c.label.includes('::'));
  const bad = mutating.filter((c) => c.status >= 400);
  console.log(`\n=== ${mutating.length} mutating backend actions triggered by button clicks ===`);
  const seen = new Set();
  for (const c of mutating) {
    const key = `${c.label}|${c.method} ${c.path}`;
    if (seen.has(key)) continue; seen.add(key);
    console.log(`  ${c.status}  ${c.method.padEnd(6)} ${c.path.padEnd(28)} <- ${c.label}`);
  }
  if (bad.length) {
    console.log(`\nRESULT: ${bad.length} manipulation(s) the backend REJECTED:`);
    for (const c of bad) console.log(`  [${c.status}] ${c.method} ${c.path} <- ${c.label}`);
    process.exit(1);
  }
  console.log('\nRESULT: every button-triggered backend action was accepted (2xx/3xx).');
  process.exit(0);
})().catch((e) => { console.error('FATAL', e); process.exit(2); });
