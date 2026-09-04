// tests/e2e/fixtures.js
//
// Shared Playwright fixtures/helpers for the yDyL e2e suite.
//
// Every spec runs against the REAL Flask server + REAL SqliteStateStore
// (launched by playwright.config.js's webServer block, isolated under
// .e2e-runtime/) - /api/config, /api/history, session handling, and the
// custom-connection save/validation logic in config_routes.py are all the
// genuine article. The endpoints mocked are /api/translate, /api/execute,
// and /api/ping - see below for why /api/execute and /api/ping both get a
// harmless default mock even in specs that never mention them.
//
// TEST ISOLATION: with no GOOGLE_CLIENT_ID configured (local-dev default -
// see auth.py's get_current_user_identity()), a request with no
// "crbot_user_id"/"user_id" cookie resolves to the SAME shared "global"
// identity as every other such request - meaning every test's saved
// custom connections, active connection, and auto_sql_execute preference
// would otherwise live in one shared bucket server-side, regardless of
// which spec file or browser context made the request. That cookie check
// requires no real auth/verification, so the `page` fixture below exploits
// it directly: each test gets its own random crbot_user_id cookie set
// before it ever navigates, giving it a genuinely private slice of server
// state - the same isolation the real app gives two different signed-in
// users. Without this, config-modal.spec.js's custom BigQuery connections
// (real fake keys) could still be "active" server-side when a completely
// unrelated, parallel-running test's page loads and checkDbStatus() pings
// the real /api/ping in the background - which is exactly what the
// default ping mock below also guards against, belt-and-suspenders.
//
// Every test also gets a fresh browser context (Playwright's default), so
// without help every spec would see the first-run guided tour overlay
// (see client.js's ONBOARDING_SEEN_KEY / startGuidedTour()) covering the
// UI. The `test` fixture pre-seeds localStorage before the app's own
// script runs so the tour and the "Help" pulsing-ring nag are already
// dismissed - see onboarding.spec.js for a dedicated test of the tour
// itself, which uses `isolatedTest` (isolation only, no onboarding-skip)
// to get the real first-visit experience without losing state isolation.
//
// GA4 NETWORK ISOLATION: `isolatedTest` below also aborts every request to
// googletagmanager.com/google-analytics.com, so gtag.js's real library
// never actually loads in ANY spec (every other test variant/fixture in
// this file builds on `isolatedTest`, so this is the one place this needs
// to be wired). Playwright gives every test a fresh, cookie-less browser
// context by default, so without this, gtag.js would mint a brand-new GA4
// client ID on every single test that fires a trackEvent() call (no
// persisted _ga cookie to reuse across tests/contexts) - meaning running
// this suite anywhere with real internet access to Google's servers was
// silently manufacturing one "new user" per test against the app's real
// production GA4 property, bloating its Reports. analytics.spec.js's own
// tests read window.dataLayer directly rather than depending on the real
// library (see its module docstring) specifically because client.js's
// trackEvent() always pushes onto dataLayer via the inline gtag() stub in
// index.html regardless of whether the real script ever loads - so this
// block costs the suite no coverage at all.

const crypto = require('crypto');
const base = require('@playwright/test');
const { expect } = base;

/** Test-scoped state isolation only (unique crbot_user_id cookie) - no
 * onboarding/tour skip, no default network mocks. Use this when a spec
 * deliberately wants the untouched first-visit experience (see
 * onboarding.spec.js) but still needs its own private slice of server
 * state. */
const isolatedTest = base.test.extend({
  page: async ({ page, baseURL }, use, testInfo) => {
    const userId = `e2e-${testInfo.testId}-${crypto.randomBytes(4).toString('hex')}`;
    await page.context().addCookies([
      { name: 'crbot_user_id', value: userId, url: baseURL },
    ]);
    // See the GA4 NETWORK ISOLATION note atop this file: abort the gtag.js
    // script load itself (sufficient on its own - without the real library,
    // index.html's inline gtag() stub only ever pushes onto window.dataLayer
    // and never talks to the network) plus, belt-and-suspenders, any direct
    // hit to google-analytics.com's collection endpoints in case a future
    // change starts loading/calling GA some other way.
    await page.route(
      /^https:\/\/(www\.googletagmanager\.com|([a-z0-9-]+\.)?google-analytics\.com|analytics\.google\.com)\//,
      (route) => route.abort()
    );
    await use(page);
  },
});

/** The default for most specs: state isolation + onboarding/tour skipped +
 * a safe default /api/execute mock installed. */
const test = isolatedTest.extend({
  page: async ({ page }, use) => {
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('ydylOnboardingSeen', '1');
        window.localStorage.setItem('ydylHelpPulseDismissed', '1');
      } catch (e) { /* ignore */ }
    });
    // client.js's checkDbStatus() fires a real GET /api/ping call after
    // *every* fetchBackendConfig() - including the very first page load,
    // and again after every config save - regardless of whether the test
    // cares about connection status at all. (Prior to backends/base.py's
    // liveness_sql, this was a POST /api/execute "SELECT 1;" call instead -
    // moved to its own route once a single hardcoded query string turned
    // out not to be valid across every dialect, see execute_routes.py's
    // /api/ping docstring - the mock here moved with it.) Left unmocked, a
    // spec that saves a custom BigQuery connection (even with a throwaway
    // fake key, e.g. config-modal.spec.js) would have this background ping
    // attempt a real token exchange with Google before Postgres's much
    // faster local connection-refused failure would apply instead - on a
    // network that blocks that exchange, it hangs Flask's single-threaded
    // dev server for many seconds and stalls every other test sharing it
    // (state isolation above stops a *different* test's connection from
    // being the one that's active, but a test that itself saves a
    // fake-keyed BigQuery connection still needs this).
    await page.route('**/api/ping', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true }),
      });
    });
    // /api/execute itself (the "Run SQL" path, distinct from the status
    // ping above) gets the same harmless default - mockExecute() calls
    // made inside a test register their own route handler afterwards, and
    // Playwright checks the most-recently-registered matching handler
    // first, so a test's explicit mock always wins over this default.
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, results: [] }),
      });
    });
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Translate was not mocked for this test.' }),
      });
    });
    await use(page);
  },
});

/** Navigate to the app and wait for its initial fetchBackendConfig() call
 * (fired at the end of client.js's DOMContentLoaded handler) to fully
 * settle - not just for the connection badge text to appear.
 *
 * updateConnectionDetails() sets #connDbName's text and THEN awaits
 * checkDbStatus() (a GET /api/ping ping - mocked by default, see the
 * `test` fixture above) before the outer fetchBackendConfig() promise
 * actually resolves. A test that only waited for the badge text and then
 * immediately interacted with the page (e.g. opening the config modal,
 * which itself calls fetchBackendConfig() again) could overlap with that
 * still-in-flight initial call - whichever response arrives *last*
 * re-renders the DB radio group/custom-connection list from its own
 * (possibly now-stale) data, silently discarding anything the test just
 * did in between. Waiting for the initial checkDbStatus() ping to
 * complete here closes that window. */
async function gotoApp(page) {
  const initialStatusPing = page.waitForResponse(
    (resp) => resp.url().includes('/api/ping') && resp.request().method() === 'GET'
  );
  await page.goto('/');
  await initialStatusPing;
  await page.locator('#connDbName').waitFor({ state: 'attached' });
  await base.expect
    .poll(async () => (await page.locator('#connDbName').textContent())?.trim())
    .not.toBe('');
}

/** Intercept POST /api/translate at the browser network layer. Pass either
 * `sql` for a successful translation or `error`+`status` for a failure -
 * mirrors the shapes translate_routes.py actually returns. */
async function mockTranslate(page, { sql, error, status } = {}) {
  await page.route('**/api/translate', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    if (error !== undefined) {
      await route.fulfill({
        status: status || 400,
        contentType: 'application/json',
        body: JSON.stringify({ error }),
      });
    } else {
      await route.fulfill({
        status: status || 200,
        contentType: 'application/json',
        body: JSON.stringify({ sql }),
      });
    }
  });
}

/** Intercept POST /api/execute at the browser network layer. Pass either
 * `results` (an array of {columns, rows, rowCount} - the shape
 * renderMultiTurnResults() expects) for success, or `error`+`status` for a
 * failure - mirrors execute_routes.py's real response shapes.
 *
 * For a multi-statement script that fails PARTWAY through (the
 * SqlExecutionError path - see execute_routes.py's module docstring),
 * also pass `failedStatement` alongside `error`, plus `results` (the
 * statements that succeeded BEFORE the failure) and optionally
 * `failedIndex`/`totalStatements` - mirrors that route's richer failure
 * shape so a spec can exercise client.js's renderResultsWithFailedStatement()
 * tabbed rendering instead of the flat single-error block. */
async function mockExecute(page, { results, error, status, failedStatement, failedIndex, totalStatements } = {}) {
  await page.route('**/api/execute', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    if (error !== undefined) {
      const body = { success: false, error };
      if (failedStatement !== undefined) {
        body.results = results || [];
        body.failedStatement = failedStatement;
        body.failedIndex = failedIndex !== undefined ? failedIndex : (results || []).length;
        body.totalStatements = totalStatements !== undefined ? totalStatements : body.failedIndex + 1;
      }
      await route.fulfill({
        status: status || 400,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    } else {
      await route.fulfill({
        status: status || 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, results }),
      });
    }
  });
}

module.exports = { test, isolatedTest, expect, gotoApp, mockTranslate, mockExecute };
