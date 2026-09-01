// tests/e2e/auth-clears-state.spec.js
//
// Cloud Run's Google Sign-In login/logout flow, purely at the client
// layer: client.js's renderAuthUI() sign-in callback and handleLogout()
// both call clearActiveQueryState() (see config-modal.spec.js's
// "switching the active db connection..." test for the sibling behavior
// this mirrors) - a new user logging on, or the current one logging off,
// invalidates whatever NL prompt/SQL/results were on screen just as
// surely as switching DB connections does.
//
// This never drives a real Google OAuth flow or a real Cloud Run /
// Firestore backend (both impractical to run hermetically here - see
// config-modal.spec.js's Snowflake-coverage comment for the same
// real-network-risk reasoning). Instead:
//   - GET /api/config is mocked to report auth as enabled (matching what
//     a real Cloud Run deployment with GOOGLE_CLIENT_ID set would return),
//     while /api/translate and /api/execute are mocked the usual way (see
//     fixtures.js).
//   - The Google Identity Services SDK (loaded from a real CDN in
//     index.html) is stubbed out before the page's own scripts run, and
//     the real script request is blocked so it can't overwrite the stub -
//     the stub captures the `callback` client.js registers via
//     google.accounts.id.initialize() so the test can invoke it directly,
//     simulating a completed sign-in.
// What IS real: client.js's own event wiring (the sign-in callback,
// #logoutBtn's click handler) and clearActiveQueryState() itself.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

function fakeIdToken(email) {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    email,
    exp: Math.floor(Date.now() / 1000) + 3600,
    picture: '',
  })).toString('base64url');
  return `${header}.${payload}.fake-signature`;
}

const CLOUD_RUN_CONFIG_PAYLOAD = {
  auth_enabled: true,
  google_client_id: 'fake-client-id.apps.googleusercontent.com',
  session_id: 'e2e-session',
  user_id: 'anonymous:e2e-session',
  authenticated: false,
  is_cloud_run: true,
  configured_databases: [{ id: 'preset-0', name: 'Default DB', type: 'postgres' }],
  active_preset_id: 'preset-0',
  default_database_url: '',
  active_database_url: '',
  active_database_type: '',
  active_is_custom: false,
  active_custom_connection_key: '',
  active_uses_custom_credentials: false,
  database_name: 'Default DB',
  custom_database_name: '',
  custom_database_url: '',
  custom_databases: [],
  auto_sql_execute: false,
};

/** Stubs window.google.accounts.id before client.js's own DOMContentLoaded
 * handler runs, and blocks the real GSI script (index.html loads it from
 * accounts.google.com) so it never overwrites the stub. initialize()
 * stashes its callback on window.__gisCallback for the test to invoke
 * directly; renderButton() just needs to not throw. */
async function stubGoogleIdentityServices(page) {
  await page.route('**/gsi/client**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/javascript',
    body: '/* stubbed for e2e - see auth-clears-state.spec.js */',
  }));
  await page.addInitScript(() => {
    window.google = {
      accounts: {
        id: {
          initialize(opts) { window.__gisCallback = opts.callback; },
          renderButton(container) {
            if (container) container.innerHTML = '<button id="fakeGsiButton">Sign in</button>';
          },
          disableAutoSelect() {},
        },
      },
    };
  });
}

async function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

async function populatePromptSqlAndResults(page) {
  await page.locator('#aiPrompt').fill('list users');
  await page.locator('#aiPrompt').press('Enter');
  await expect.poll(() => currentSql(page)).toContain('SELECT');
  await page.locator('#runBtn').click();
  await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
}

async function assertPromptSqlAndResultsCleared(page) {
  await expect(page.locator('#aiPrompt')).toHaveValue('');
  expect(await currentSql(page)).toBe('');
  await expect(page.locator('#resultsBody')).toBeEmpty();
  await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
}

async function mockCloudRunConfig(page) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(CLOUD_RUN_CONFIG_PAYLOAD),
    });
  });
}

test.describe('logging out from the narrow-screen "more" menu', () => {
  // Same breakpoint more-menu.spec.js exercises for the Help/History
  // collapse - see style.css's @media (max-width: 480px) block and
  // client.js's relocateAuthContainer(). At this width the signed-in
  // avatar (#g_id_signin, holding .auth-menu-wrapper > #authAvatarBtn +
  // #authDropdown) is reparented into #moreMenuAuthSlot, itself nested
  // inside the triple-dot #moreMenuDropdown - so opening the avatar's own
  // dropdown here means one .auth-dropdown-menu popping up inside another.
  test.use({ viewport: { width: 375, height: 700 } });

  test('the avatar submenu inside the more menu is actually clickable, not just present in the DOM', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);

    await gotoApp(page);
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('mobileuser@example.com')
    );

    // Signed in - the avatar lives inside the more menu's own slot at this
    // width, not in the header directly.
    await expect(page.locator('#moreMenuAuthSlot #authAvatarBtn')).toBeAttached();
    await expect(page.locator('.header-actions > .auth-container')).toHaveCount(0);

    await page.locator('#moreMenuBtn').click();
    await page.locator('#authAvatarBtn').click();
    await expect(page.locator('#authDropdown')).not.toHaveClass(/hidden/);

    // The regression this guards against: #moreMenuDropdown's base
    // .auth-dropdown-menu styling carried a stray `overflow: hidden` (a
    // leftover from an older, otherwise-superseded duplicate ruleset in
    // style.css) that clipped this nested dropdown down to nothing - it
    // existed in the DOM with `.hidden` correctly removed, yet sat at
    // coordinates nothing could actually reach: a real click at its own
    // center hit whatever was rendered underneath instead (e.g. the SQL
    // editor). A plain locator.click() alone isn't a reliable guard against
    // this - Playwright's own actionability retries silently absorb a
    // transient "hasn't painted yet" race, which looks identical to this
    // bug for the first couple of retries and can let a click() pass
    // either way. Assert the real hit-test directly instead.
    const logoutIsHitTestable = await page.evaluate(() => {
      const btn = document.getElementById('logoutBtn');
      const r = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return !!hit && (hit === btn || btn.contains(hit));
    });
    expect(logoutIsHitTestable).toBe(true);

    await page.locator('#logoutBtn').click();

    // Logging out tears down the whole signed-in subtree and rebuilds the
    // (stubbed) sign-in button in the very same slot.
    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);

    // The more menu itself also closes as an incidental side effect here
    // (the click event keeps bubbling toward document's own outside-click
    // listener after logoutBtn's own handler has already detached it from
    // the DOM, so `moreMenuWrapper.contains(e.target)` reads false) -
    // harmless, and consistent with the menu already closing after
    // Help/History navigation. Reopen it to confirm the stubbed sign-in
    // control renders correctly in the same slot the avatar just vacated.
    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuAuthSlot #fakeGsiButton')).toBeVisible();
  });
});

test.describe('auth-triggered state clearing (Cloud Run)', () => {
  test('logging in via Google Sign-In clears the NL prompt, SQL, and results', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });

    await gotoApp(page);
    await populatePromptSqlAndResults(page);

    // Simulate Google Identity Services completing a real sign-in by
    // invoking the callback client.js registered via
    // google.accounts.id.initialize() - exactly what the real SDK would
    // call after the user picks an account.
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('newuser@example.com')
    );

    await assertPromptSqlAndResultsCleared(page);
  });

  test('logging out clears the NL prompt, SQL, and results', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });

    await gotoApp(page);

    // Sign in first, so there's an avatar/logout button to click - this
    // itself clears state (covered by the test above), so populate the
    // prompt/SQL/results AFTER signing in, not before.
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await populatePromptSqlAndResults(page);

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();

    await assertPromptSqlAndResultsCleared(page);
  });

  // Regression guard: the sign-in button (and, once signed in, the avatar's
  // "Log out") used to stay fully clickable while a query was in flight -
  // triggering either mid-turn tore down the whole turn via
  // clearActiveQueryState() (see the two tests above), with unpredictable
  // results depending on exactly when it landed. setButtonsDisabled() now
  // grays the whole container out (auth-disabled) and sets pointer-events:
  // none on it for that entire window - checked here via a real hit-test
  // (see the "avatar submenu" test above for why a plain .click() isn't a
  // strong enough guard on its own: it wouldn't distinguish "blocked" from
  // "just hasn't painted yet").
  test('the sign-in control is disabled while a query is in flight, and re-enabled once it settles', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);

    let resolveTranslate;
    const translateStarted = new Promise((resolve) => { resolveTranslate = resolve; });
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      resolveTranslate();
      await new Promise((r) => setTimeout(r, 2000));
      await route.fulfill({
        status: 200, contentType: 'application/x-ndjson',
        body: JSON.stringify({ status: 'done', success: true, sql: 'SELECT 1;' }) + '\n',
      });
    });

    await gotoApp(page);

    const authContainer = page.locator('#g_id_signin');
    const isSigninHitTestable = () => page.evaluate(() => {
      const btn = document.getElementById('fakeGsiButton');
      if (!btn) return false;
      const r = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return !!hit && (hit === btn || btn.contains(hit));
    });

    await expect(authContainer).not.toHaveClass(/auth-disabled/);
    expect(await isSigninHitTestable()).toBe(true);

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(authContainer).toHaveClass(/auth-disabled/);
    expect(await isSigninHitTestable()).toBe(false);

    await expect.poll(() => page.locator('#aiPrompt').isEnabled(), { timeout: 5000 }).toBe(true);

    await expect(authContainer).not.toHaveClass(/auth-disabled/);
    expect(await isSigninHitTestable()).toBe(true);
  });
});
