// UI stress crawler — logs in, visits every page, clicks every button and cycles every <select>,
// and fails if any console error, uncaught page exception, or 5xx network response occurs.
//
// This is a MANUAL end-to-end smoke/stress tool (it needs the live stack running), not part of the
// unit suites. Run it whenever you touch the dashboard to confirm nothing on the UI is broken.
//
//   # 1. start the stack (API + simulator daemon + web) — e.g. via scripts/codespace-up.sh, or:
//   export SLEEPCTL_DB=/tmp/uitest.db JWT_SECRET=$(openssl rand -hex 32) \
//          DASHBOARD_USER=admin DASHBOARD_PASSWORD=test1234 \
//          PYTHONPATH="$PWD:$PWD/dashboard/api"
//   python -c "from app.db import connect; from app.security import ensure_bootstrap_user; \
//              from app.seed import seed; connect(); ensure_bootstrap_user(); seed(21)"
//   uvicorn app.main:app --port 8000 --app-dir dashboard/api &
//   python dashboard/daemon/run_daemon.py &
//   (cd dashboard/web && API_URL=http://localhost:8000 PORT=3000 npm run dev) &
//
//   # 2. run the crawler (uses the globally-installed playwright + pre-installed Chromium)
//   NODE_PATH=/opt/node22/lib/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//   DASHBOARD_USER=admin DASHBOARD_PASSWORD=test1234 node dashboard/web/e2e/ui-stress.js
//
// Exit 0 + "RESULT: clean" = every interactive control works. Any problem is printed with the
// page + control that triggered it.
const { chromium } = require('playwright');

const BASE = process.env.UI_BASE || 'http://127.0.0.1:3000';
const USER = process.env.DASHBOARD_USER || 'admin';
const PASS = process.env.DASHBOARD_PASSWORD || 'test1234';
const ROUTES = ['/', '/tonight', '/data', '/learning', '/analytics', '/settings', '/admin'];

const problems = [];
function attach(page, where) {
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    // ignore benign dev-server / browser noise unrelated to app correctness
    if (/favicon|ResizeObserver|aborted|AbortError|RSC payload|Failed to fetch/i.test(t)) return;
    problems.push({ where, kind: 'console.error', detail: t.slice(0, 200) });
  });
  page.on('pageerror', (e) => problems.push({ where, kind: 'pageerror', detail: String(e).slice(0, 200) }));
  page.on('response', (r) => { if (r.status() >= 500) problems.push({ where, kind: `http_${r.status()}`, detail: r.url() }); });
  page.on('dialog', (d) => d.dismiss().catch(() => {}));
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  attach(page, 'login');

  // Auth via the context request API (shares cookies with the browser) — reliable vs racing the
  // dev-server hydration on the login form. The form itself is sanity-checked below.
  const lr = await ctx.request.post(`${BASE}/api/auth/login`, { data: { username: USER, password: PASS } });
  console.log('login response status:', lr.status());
  if (lr.status() !== 200) problems.push({ where: 'login', kind: 'login_failed', detail: `status ${lr.status()}` });
  console.log('auth/me status:', (await ctx.request.get(`${BASE}/api/auth/me`)).status());
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded', timeout: 25000 });
  await page.waitForTimeout(2500);
  if (!(await page.$('input[placeholder="Enter password"]'))) problems.push({ where: 'login', kind: 'form_missing', detail: 'no password field' });

  let total = 0;
  for (const route of ROUTES) {
    let n = 0;
    try {
      await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await page.waitForTimeout(4500);                                 // hydrate + SWR data load
      await page.evaluate(async () => {                               // mount below-the-fold cards
        for (let y = 0; y <= document.body.scrollHeight; y += 400) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 60)); }
        window.scrollTo(0, 0);
      }).catch(() => {});
      await page.waitForTimeout(1200);
      n = await page.$$eval('button, [role="button"]', (b) => b.length);
    } catch (e) { problems.push({ where: route, kind: 'nav_fail', detail: String(e).slice(0, 200) }); continue; }

    for (let i = 0; i < n; i++) {
      try {
        const onRoute = page.url().endsWith(route) || (route === '/' && new URL(page.url()).pathname === '/');
        if (!onRoute) { await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 }); await page.waitForTimeout(1500); }
        const b = (await page.$$('button, [role="button"]'))[i];
        if (!b) continue;
        const label = ((await b.innerText().catch(() => '')) || (await b.getAttribute('aria-label')) || `#${i}`).trim().slice(0, 30);
        if (!(await b.isVisible().catch(() => false)) || !(await b.isEnabled().catch(() => false))) continue;
        attach(page, `${route} [btn:${label}]`);
        await b.click({ timeout: 5000 }).catch((e) => problems.push({ where: `${route} [btn:${label}]`, kind: 'click_fail', detail: String(e).split('\n')[0].slice(0, 120) }));
        await page.waitForTimeout(500);
        total++;
      } catch (e) { problems.push({ where: `${route} btn#${i}`, kind: 'btn_error', detail: String(e).slice(0, 160) }); }
    }

    // cycle every <select> on a FRESH page (clicks above can mutate layout/start a session)
    try {
      await page.goto(`${BASE}${route}`, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await page.waitForTimeout(3500);
      const selects = await page.$$('select');
      for (let si = 0; si < selects.length; si++) {
        const s = selects[si];
        if (!(await s.isVisible().catch(() => false)) || !(await s.isEnabled().catch(() => false))) continue;
        for (const val of await s.$$eval('option', (os) => os.map((o) => o.value))) {
          attach(page, `${route} [select#${si}=${val}]`);
          await s.selectOption(val, { timeout: 4000 }).catch((e) => problems.push({ where: `${route} [select#${si}=${val}]`, kind: 'select_fail', detail: String(e).split('\n')[0].slice(0, 120) }));
          await page.waitForTimeout(400);
        }
      }
    } catch (e) { problems.push({ where: route, kind: 'select_loop_error', detail: String(e).slice(0, 160) }); }
    console.log(`route ${route}: ${n} interactive controls`);
  }

  await browser.close();
  console.log(`\n=== exercised ~${total} buttons + all selects across ${ROUTES.length} pages ===`);
  if (!problems.length) { console.log('RESULT: clean — no console errors, page exceptions, or 5xx responses.'); process.exit(0); }
  console.log(`RESULT: ${problems.length} problem(s):`);
  for (const p of problems.slice(0, 80)) console.log(`  [${p.kind}] ${p.where} :: ${p.detail}`);
  process.exit(1);
})().catch((e) => { console.error('FATAL', e); process.exit(2); });
