// tests/e2e/auth-clears-state.spec.js
//
// Cloud Run's Google Sign-In login/logout flow, purely at the client
// layer. client.js used to call clearActiveQueryState() directly from
// both the sign-in callback and handleLogout() - a new user logging on,
// or the current one logging off, unconditionally wiped whatever NL
// prompt/SQL/results were on screen.
//
// That's no longer true: turn history is now bucketed by
// (identity, active connection) - see chatStoresByBucket/
// computeBucketKey()/reconcileActiveHistoryBucket() in client.js. Signing
// in or out changes CURRENT_USER_IDENTITY, which fetchBackendConfig()
// picks up from the server's own resolved identity (data.user_id) and
// feeds into reconcileActiveHistoryBucket() - that's what actually
// decides what appears on screen now: a BLANK slate if this identity has
// no bucket yet on this page load, or that identity's own last turn,
// restored, if it does. Neither sign-in nor sign-out clears anything
// directly any more. (See config-modal.spec.js's
// "switching the active db connection..." and "switching away from a
// connection and back restores..." tests for the sibling behavior on the
// connection-change axis rather than the identity axis.)
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
// #logoutBtn's click handler) and the history-bucket switch itself
// (reconcileActiveHistoryBucket(), driven by fetchBackendConfig()'s own
// GET /api/config call - so the mocked /api/config response below has to
// actually reflect the signed-in identity via the request's Authorization
// header, the same way the real server's get_current_user_identity()
// does, or every sign-in/sign-out in these tests would resolve to the
// same identity and never actually switch buckets at all).

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

function fakeIdToken(email, expiresInSeconds = 3600, extraClaims = {}) {
  const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({
    email,
    exp: Math.floor(Date.now() / 1000) + expiresInSeconds,
    picture: '',
    ...extraClaims,
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
 * directly, simulating a completed sign-in; renderButton() just needs to
 * not throw. client.js never calls prompt() itself (see renderAuthUI()'s
 * own comment on that) - a stale/expired token just reverts to showing
 * the ordinary sign-in button below, no silent-refresh attempt involved. */
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

/** Stubs /api/summarize-result with a plain success/no-summary response,
 * same convention config-modal.spec.js's own connection-switch test uses.
 * Without this, populatePromptSqlAndResults()'s real (un-mocked) POST to
 * this endpoint actually reaches this test environment's outbound network
 * and comes back a real error - which still gets saved onto the turn as
 * its `.summary` and, once restored via a later bucket switch, would
 * legitimately switch focus to that Summary/error tab (exactly the way
 * navigating back to any turn with a saved summary always has - see
 * translate-execute.spec.js's "navigating back to a turn replays its
 * saved summary" test) instead of leaving the results tab active. Nothing
 * to do with the history-bucket feature itself - just determinism. */
async function mockSummarizeResult(page) {
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
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
  // See mockSummarizeResult's own docstring for why this matters now that
  // a turn's saved `.summary` can resurface later via a history-bucket
  // switch, not just via back/forward navigation.
  await mockSummarizeResult(page);
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

/** Decodes the fake JWT's payload the same way fakeIdToken() built it, so
 * mockCloudRunConfig can hand back the email as `user_id` - mirroring
 * server/auth.py's get_current_user_identity(), which resolves a Bearer
 * token to the verified email. Returns null for a missing/malformed
 * header rather than throwing, so a request with no token at all (the
 * signed-out/anonymous case) falls through to the anonymous default.
 *
 * Also returns null for an EXPIRED token, mirroring the real server's
 * id_token.verify_oauth2_token, which enforces `exp` strictly - client.js's
 * getApiHeaders() sends whatever token is in localStorage regardless of its
 * own expiry (see that function), so without this check here, a stale
 * expired token would look "authenticated" to this mock even though the
 * real Google verification (and, with no session cookie modeled by this
 * lightweight header-only mock, no fallback identity either) would reject
 * it - see "an expired stored token does not restore a signed-in state
 * after reload" below, which depends on this expiry check actually
 * happening. */
function decodeFakeTokenEmail(authHeader) {
  if (!authHeader || !authHeader.startsWith('Bearer ')) return null;
  try {
    const token = authHeader.slice('Bearer '.length);
    const payloadB64 = token.split('.')[1];
    const payload = JSON.parse(Buffer.from(payloadB64, 'base64url').toString('utf8'));
    if (typeof payload.exp === 'number' && payload.exp <= Math.floor(Date.now() / 1000)) return null;
    return payload.email || null;
  } catch {
    return null;
  }
}

async function mockCloudRunConfig(page) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    // Real Cloud Run resolves user_id from the request's own Authorization
    // header (see server/auth.py's get_current_user_identity) rather than
    // a fixed value - reflecting that here is what lets these tests
    // actually exercise reconcileActiveHistoryBucket()'s identity-based
    // bucket switch instead of every sign-in/sign-out landing on the same
    // bucket (see the module comment up top for why this matters).
    const email = decodeFakeTokenEmail(route.request().headers()['authorization']);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...CLOUD_RUN_CONFIG_PAYLOAD,
        user_id: email || CLOUD_RUN_CONFIG_PAYLOAD.user_id,
        authenticated: Boolean(email),
      }),
    });
  });
  // /api/chat-history (GET/save/activate) is deliberately mocked here too,
  // not left to hit the real local server the way /api/translate/
  // /api/execute are: this file's fake JWTs all share the exact same
  // unsigned {"alg":"none","typ":"JWT"} header, which is exactly 32 base64
  // characters on its own - and since GOOGLE_CLIENT_ID isn't configured on
  // this real local test server (see fixtures.js's module docstring),
  // server/auth.py's get_current_user_identity() falls back to
  // `f"token:{token[:32]}"` for ANY Bearer token, which truncates to just
  // that header. Every fake sign-in in this whole file - alice, bob,
  // "user@example.com", newuser, all of them - would therefore collapse to
  // the exact SAME real server-side identity, regardless of which email
  // the mocked /api/config above claims. That's harmless for the
  // client-side bucket-switching behavior this file actually tests (which
  // never leaves the browser), but the real chat_history persistence
  // feature this history-bucket switch now also drives (see
  // hydrateChatHistoryFromServer()/persistChatBucket() in client.js) would
  // otherwise silently bleed real, server-persisted conversation state
  // between every "different identity" scenario below. Mocked to always
  // report/accept nothing, exactly as if this identity had never persisted
  // anything before - keeping this file scoped to what it says it tests:
  // the in-memory bucket switch, not server-side persistence (which has
  // its own dedicated coverage in tests/server/test_chat_history_routes.py
  // and test_state_store_*.py).
  await page.route('**/api/chat-history', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ success: true, buckets: {}, active_bucket_key: '' }),
    });
  });
  await page.route('**/api/chat-history/save', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
  });
  await page.route('**/api/chat-history/activate', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
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

test.describe('logging out clears the server-side session cookie', () => {
  // See server/auth_session.py's module docstring: the app's own
  // long-lived crbot_auth_session cookie is what actually keeps a user
  // signed in past their Google ID token's ~1hr expiry. Without
  // handleLogout() also telling the server to drop that cookie
  // (POST /api/auth/logout - server/auth.py's logout()), clearing the
  // client's own localStorage token would look like a logout but silently
  // NOT be one: the next request would still resolve via that durable
  // cookie and the user would appear signed back in. This test only
  // proves the CLIENT half - that clicking "Log out" actually fires this
  // request - since GOOGLE_CLIENT_ID isn't configured for this real local
  // server (see fixtures.js's module docstring), so no real session
  // cookie is ever minted here; the signing/verification mechanism itself
  // is covered server-side in tests/server/test_auth.py.
  test('clicking "Log out" POSTs to /api/auth/logout', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);

    await gotoApp(page);
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('logout-post-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    const logoutRequest = page.waitForRequest(
      (req) => req.url().includes('/api/auth/logout') && req.method() === 'POST'
    );
    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();
    await logoutRequest;

    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);
  });
});

test.describe('auth-triggered history bucket switching (Cloud Run)', () => {
  test('logging in as an identity with no prior conversation on this page load starts with a blank slate', async ({ page }) => {
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
    // call after the user picks an account. newuser@example.com has never
    // been seen this page load, so its own bucket doesn't exist yet -
    // reconcileActiveHistoryBucket() creates it fresh, which is why this
    // still looks blank (NOT because signing in force-clears anything -
    // see the "same identity" test below, which proves the opposite).
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('newuser@example.com')
    );

    await assertPromptSqlAndResultsCleared(page);
  });

  test('logging back in as the same identity restores that identity\'s own prior conversation', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });

    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await populatePromptSqlAndResults(page);

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();
    // Signing out drops to the anonymous session's own (separate, still
    // untouched) bucket - blank, same as the module-level assertion below
    // covers on its own.
    await assertPromptSqlAndResultsCleared(page);

    // Signing back in as the SAME email should find the conversation
    // exactly where it was left, not a second blank slate.
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#aiPrompt')).toHaveValue('list users');
    expect(await currentSql(page)).toContain('SELECT');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
  });

  test('logging out restores the anonymous session\'s own prior conversation, not a blank slate', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });

    await gotoApp(page);

    // Start a conversation while still anonymous...
    await populatePromptSqlAndResults(page);

    // ...then sign in, which lands on a fresh (blank) bucket for this
    // never-before-seen identity, and start a DIFFERENT conversation there.
    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await assertPromptSqlAndResultsCleared(page);
    await mockTranslate(page, { sql: 'SELECT * FROM orders;' });
    await mockExecute(page, {
      results: [{ columns: ['order_id'], rows: [{ order_id: 7 }], rowCount: 1 }],
    });
    await page.locator('#aiPrompt').fill('list orders');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('orders');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['order_id']);

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();

    // Back to the anonymous session's own bucket - the ORIGINAL "list
    // users" conversation, left alone this whole time, not blank and not
    // bleeding in the signed-in user's "list orders" turn.
    await expect(page.locator('#aiPrompt')).toHaveValue('list users');
    expect(await currentSql(page)).toContain('users');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
  });

  test('two different signed-in identities never see each other\'s conversation', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });

    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('alice@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await populatePromptSqlAndResults(page);

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('bob@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    // bob has never been seen before - his own bucket is blank, not
    // alice's conversation.
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

// Regression coverage for a real bug report: reloading the app (the exact
// same tab, e.g. hitting the browser's refresh button) used to always drop
// a signed-in user back to signed-out/anonymous - client.js's googleIdToken
// was populated ONLY by the Google Sign-In callback (see
// stubGoogleIdentityServices()'s __gisCallback above), which never fires
// again on its own, so nothing survived past the reload even though the
// user's real Google session (and the token itself, until it actually
// expires) was still perfectly valid. Fixed by persisting the ID token to
// localStorage on sign-in and restoring it before the app's first
// /api/config call - see client.js's persistGoogleIdToken()/
// clearPersistedGoogleIdToken() and the restore right next to
// googleIdToken's own declaration. localStorage (rather than sessionStorage)
// is a deliberate later choice too: it means a signed-in state also shows up
// in any OTHER tab/window of the same browser, not just the one that signed
// in - see the cross-tab describe block below for that behavior.
test.describe('sign-in survives a same-tab reload (Cloud Run)', () => {
  test('a signed-in user is still shown as signed in after reloading the page', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('reload-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    // Every route/init-script registered on `page` (stubGoogleIdentityServices,
    // mockCloudRunConfig) survives a reload - only the page's own JS state
    // (and, before this fix, googleIdToken along with it) gets torn down.
    await page.reload();

    // Still shows the avatar, not the "Sign in" button - proof the restored
    // token was accepted as a real, unexpired sign-in rather than starting
    // this reload back at signed-out.
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await expect(page.locator('#fakeGsiButton')).toHaveCount(0);
    await expect(page.locator('#authAvatarBtn')).toHaveAttribute('title', 'reload-user@example.com');
  });

  test('the very first request after a reload already carries the restored token, not just a later one', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('reload-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    const configAuthHeaders = [];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      configAuthHeaders.push(route.request().headers()['authorization'] || null);
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(CLOUD_RUN_CONFIG_PAYLOAD),
      });
    });

    await page.reload();
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    // The page's OWN startup sequence (fetchBackendConfig() at the very
    // bottom of the DOMContentLoaded handler) is what fires this GET - not
    // something the test triggered - so a Bearer header on its very first
    // call proves googleIdToken was restored before any network request
    // went out, not patched in afterward by some later re-sync.
    expect(configAuthHeaders.length).toBeGreaterThan(0);
    expect(configAuthHeaders[0]).toMatch(/^Bearer /);
  });

  test('logging out clears the persisted token too, so a later reload does not resurrect the old session', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('reload-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();
    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);

    await page.reload();

    await expect(page.locator('#fakeGsiButton')).toBeVisible();
    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);
  });

  test('an expired stored token does not restore a signed-in state after reload', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    // Seed an ALREADY-EXPIRED token directly into localStorage - the shape
    // a real signed-in session would leave behind if the tab (or browser)
    // stayed around long enough for it to actually expire before the next
    // reload - rather than going through a real sign-in (which always mints
    // a fresh, unexpired one via fakeIdToken()).
    const expiredToken = fakeIdToken('stale-user@example.com', -3600);
    await page.evaluate((token) => {
      window.localStorage.setItem('datalectGoogleIdToken', token);
    }, expiredToken);

    await page.reload();

    // renderAuthUI()'s own isExpired check discards it the moment it's
    // used - the sign-in button shows, not the stale user's avatar. No
    // silent-refresh attempt follows (see renderAuthUI()'s own comment on
    // why client.js never calls google.accounts.id.prompt() at all) -
    // signing back in takes an explicit click on this same button.
    await expect(page.locator('#fakeGsiButton')).toBeVisible();
    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);
  });
});

// Regression coverage for a real bug report: once the app's own long-lived
// session cookie (server/auth.py's refresh_auth_session_cookie()) started
// keeping a browser signed in for up to 30 days independent of the local
// Google ID token's own ~1hr `exp`, the avatar started flipping from
// Google's real profile photo (and the first letter of the user's actual
// NAME) to a plain circle showing the first letter of their EMAIL instead -
// surprising and faintly suspicious-looking for a user who never signed
// out. Root cause: renderAuthUI() only ever read `payload.picture` when
// `localSignedIn` (i.e. the token not yet expired) was also true, even
// though the token's own cached `picture`/`given_name` claims are still
// perfectly good for cosmetic display well past `exp` - expiry only means
// the token can no longer be TRUSTED to assert identity for real requests
// (which is exactly what serverConfirmedAuthEmail/the session cookie takes
// over doing instead). Fixed by reading `payload` for the avatar whenever
// it exists at all, regardless of isExpired.
test.describe('avatar keeps showing the real Google photo/name past the local token\'s expiry', () => {
  test('an expired-but-still-decodable token still renders the Google photo when the server confirms auth via the session cookie', async ({ page }) => {
    await stubGoogleIdentityServices(page);

    // Simulates the session-cookie fallback: /api/config reports
    // authenticated:true UNCONDITIONALLY here (representing the server's
    // own session-cookie check succeeding), independent of whatever this
    // request's own Authorization header carries - unlike
    // mockCloudRunConfig() above, which deliberately mirrors "the Bearer
    // token itself must still be valid" and so isn't usable for this
    // scenario.
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ...CLOUD_RUN_CONFIG_PAYLOAD,
          user_id: 'stale-user@example.com',
          authenticated: true,
        }),
      });
    });

    await gotoApp(page);

    // Seed an ALREADY-EXPIRED token, same as the "does not restore a
    // signed-in state" test above, but this one carries real
    // picture/given_name claims - the shape a real Google sign-in from
    // weeks ago would still leave sitting in localStorage.
    const staleToken = fakeIdToken('stale-user@example.com', -3600, {
      picture: 'https://example.com/stale-user-photo.jpg',
      given_name: 'Stale',
    });
    await page.evaluate((token) => {
      window.localStorage.setItem('datalectGoogleIdToken', token);
    }, staleToken);

    await page.reload();

    // The avatar shows Google's real photo, not the plain
    // first-letter-of-email fallback circle - proof the stale token's
    // cached claims were used for display even though it's long expired.
    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    const avatarImg = page.locator('#authAvatarBtn img.auth-avatar-img');
    await expect(avatarImg).toBeVisible();
    await expect(avatarImg).toHaveAttribute('src', 'https://example.com/stale-user-photo.jpg');
    await expect(page.locator('#authAvatarBtn .auth-avatar-initial')).toHaveCount(0);
  });

  test('falls back to the given_name initial (not the email initial) when there is no picture at all', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ...CLOUD_RUN_CONFIG_PAYLOAD,
          user_id: 'stale-user@example.com',
          authenticated: true,
        }),
      });
    });

    await gotoApp(page);

    // No `picture` claim this time - only given_name - and still expired,
    // to isolate the initial-letter fallback from the img-vs-no-img case
    // the test above already covers.
    const staleToken = fakeIdToken('stale-user@example.com', -3600, {
      picture: '',
      given_name: 'Zara',
    });
    await page.evaluate((token) => {
      window.localStorage.setItem('datalectGoogleIdToken', token);
    }, staleToken);

    await page.reload();

    await expect(page.locator('#authAvatarBtn')).toBeVisible();
    await expect(page.locator('#authAvatarBtn img.auth-avatar-img')).toHaveCount(0);
    // 'Z' (from given_name "Zara"), not 's' (from the email
    // "stale-user@example.com") - the whole point of this fix.
    await expect(page.locator('#authAvatarBtn .auth-avatar-initial')).toHaveText('Z');
  });
});

// Regression coverage for the localStorage (not sessionStorage) choice
// itself: signing in on one tab should make a DIFFERENT tab of the same
// browser show signed-in too, as soon as that other tab loads/reloads -
// this is the whole reason persistGoogleIdToken()/clearPersistedGoogleIdToken()
// use localStorage rather than sessionStorage (see the comment on
// googleIdToken's declaration in client.js). Two Playwright `page`s opened
// from the SAME `context` share one browser-storage origin, exactly like two
// tabs of one real browser window would - the default `page` fixture used
// everywhere else in this file is its own fresh, isolated context per test,
// so this describe block deliberately opens a second page itself rather than
// relying on the fixture.
test.describe('sign-in and sign-out propagate to other tabs of the same browser (Cloud Run)', () => {
  test('signing in on one tab shows the other tab as signed in once it reloads', async ({ page, context }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    const otherPage = await context.newPage();
    await stubGoogleIdentityServices(otherPage);
    await mockCloudRunConfig(otherPage);
    await gotoApp(otherPage);

    // Before any sign-in, both tabs start out signed-out.
    await expect(page.locator('#fakeGsiButton')).toBeVisible();
    await expect(otherPage.locator('#fakeGsiButton')).toBeVisible();

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('cross-tab-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    // The other tab doesn't know about this on its own - it only re-reads
    // localStorage on its own load/reload (see the comment on
    // googleIdToken's declaration in client.js) - so it still shows
    // signed-out until it reloads.
    await expect(otherPage.locator('#fakeGsiButton')).toBeVisible();

    await otherPage.reload();

    await expect(otherPage.locator('#authAvatarBtn')).toBeVisible();
    await expect(otherPage.locator('#fakeGsiButton')).toHaveCount(0);
    await expect(otherPage.locator('#authAvatarBtn')).toHaveAttribute('title', 'cross-tab-user@example.com');

    await otherPage.close();
  });

  test('logging out on one tab signs the other tab out too, once it reloads', async ({ page, context }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('cross-tab-user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    const otherPage = await context.newPage();
    await stubGoogleIdentityServices(otherPage);
    await mockCloudRunConfig(otherPage);
    await gotoApp(otherPage);
    await expect(otherPage.locator('#authAvatarBtn')).toBeVisible();

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();
    await expect(page.locator('#authAvatarBtn')).toHaveCount(0);

    // Same "only re-reads on its own load" caveat as the sign-in test above -
    // the other tab still shows the (now stale) signed-in state until it
    // reloads.
    await expect(otherPage.locator('#authAvatarBtn')).toBeVisible();

    await otherPage.reload();

    await expect(otherPage.locator('#fakeGsiButton')).toBeVisible();
    await expect(otherPage.locator('#authAvatarBtn')).toHaveCount(0);

    await otherPage.close();
  });
});
