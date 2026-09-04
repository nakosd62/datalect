// tests/e2e/analytics.spec.js
//
// Custom GA4 events fired via client.js's trackEvent() (a thin wrapper
// around gtag('event', name, params) - see that function's own header
// comment for the full list: translate_submitted, sql_executed,
// error_shown, report_submitted, database_selected, model_selected,
// help_viewed, history_viewed, history_nav_clicked, preferences_viewed,
// login, logout, mic_used, quick_prompt_clicked).
//
// Rather than stubbing/spying on window.gtag itself, these tests read
// window.dataLayer directly - index.html's own inline snippet defines
// `function gtag(){dataLayer.push(arguments)}` as a plain top-level
// function declaration, which would silently clobber any pre-injected
// stub the moment that script runs. gtag('event', name, params) always
// ends up pushing the exact ['event', name, params] arguments tuple into
// dataLayer regardless of whether the real GA library ever loads - and it
// never does in this suite, since fixtures.js's isolatedTest fixture
// aborts every request to googletagmanager.com/google-analytics.com (see
// its own header comment for why: without that block, every test's
// fresh/cookie-less browser context would mint a brand-new GA4 "user"
// against the real production property). So reading dataLayer back is a
// robust, stub-free way to assert on what was tracked, with zero real
// network involved either way - see the "analytics: network isolation"
// describe block below for a dedicated test of that block itself.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Every {..params} object gtag('event', name, params) pushed for the
 * given event name, in firing order. */
async function trackedEvents(page, name) {
  return page.evaluate((eventName) => {
    return (window.dataLayer || [])
      .filter((entry) => entry && entry[0] === 'event' && entry[1] === eventName)
      .map((entry) => entry[2] || {});
  }, name);
}

async function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

/** currentSql(), with all whitespace collapsed to single spaces - see
 * translate-execute.spec.js's own copy of this helper for the full
 * reasoning: client.js's setSqlQuery() runs generated SQL through a real
 * sql-formatter library (window.sqlFormatter, CDN-loaded) when it's
 * available, which can reflow even a trivial 'SELECT 1;' onto multiple
 * lines - purely a function of whether that CDN script loaded in this
 * particular browser/environment, not anything this suite controls. Any
 * assertion checking for more than one bare token (e.g. 'SELECT 1', not
 * just 'SELECT') needs this instead of raw currentSql() to stay safe
 * whether or not that reformatting happened. */
async function normalizedSql(page) {
  return (await currentSql(page) || '').replace(/\s+/g, ' ').trim();
}

async function setSqlBox(page, sql) {
  await page.evaluate((value) => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) {
      wrapper.CodeMirror.setValue(value);
    } else {
      // Plain assignment never fires a DOM 'input' event on its own (only
      // real keystrokes do) - client.js's own Execute/"report wrong SQL"
      // disabled-state tracking (applySqlActionButtonsContentState()) is
      // wired to that event for this exact fallback path, so it has to be
      // dispatched explicitly here to mimic a real user typing/pasting.
      const textarea = document.getElementById('sqlQuery');
      if (textarea) {
        textarea.value = value;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
  }, sql);
}

async function mockIssueReportingEnabled(page, enabled) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    json.issue_reporting_enabled = enabled;
    await route.fulfill({ response, json });
  });
}

function mockReportIssue(page) {
  page.route('**/api/report-issue', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
  });
}

test.describe('analytics: network isolation', () => {
  test('gtag.js is really requested by the page, and every such request is aborted, not merely absent in this sandbox', async ({ page }) => {
    // Passive observation only (page.on, not page.route) - a competing
    // page.route() registered here would just shadow fixtures.js's own
    // isolatedTest block for the same URLs, which would prove blocking is
    // *possible* but not that the fixture's own handler is what's doing it.
    const gaUrlPattern = /^https:\/\/(www\.googletagmanager\.com|([a-z0-9-]+\.)?google-analytics\.com|analytics\.google\.com)\//;
    const requested = [];
    const failed = [];
    page.on('request', (req) => {
      if (gaUrlPattern.test(req.url())) requested.push(req.url());
    });
    page.on('requestfailed', (req) => {
      if (gaUrlPattern.test(req.url())) failed.push(req.url());
    });

    await gotoApp(page);
    // A couple of ordinary interactions, in case gtag.js's own script tag
    // load is deferred/lazy rather than fired on initial page load. Close
    // the help modal before opening history - #helpModal sits on top and
    // intercepts clicks on the rest of the page while open.
    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);
    await page.locator('#helpModalCloseBtn').click();
    await expect(page.locator('#helpModal')).toHaveClass(/hidden/);
    await page.locator('#historyBtn').click();

    // index.html really does reference the real gtag.js URL (this isn't a
    // vacuous pass because nothing tried) ...
    expect(requested.length).toBeGreaterThan(0);
    // ... and every single attempt was intercepted and aborted by the
    // fixture - page.route()'s abort() surfaces as a 'requestfailed' event,
    // never as a normal completed/succeeded request.
    expect(failed.length).toBe(requested.length);

    // dataLayer-based tracking still works with the real library blocked -
    // the whole point per this file's header comment.
    expect((await trackedEvents(page, 'help_viewed')).length).toBe(1);
    expect((await trackedEvents(page, 'history_viewed')).length).toBe(1);
  });
});

test.describe('analytics: query flow', () => {
  test('translate_submitted fires once, with the mode and database - no prompt text', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const events = await trackedEvents(page, 'translate_submitted');
    expect(events.length).toBe(1);
    // No `prompt` field - the NL prompt text itself must never reach GA.
    expect(events[0].prompt).toBeUndefined();
    expect(events[0].mode).toBe('single');
    expect(typeof events[0].database_name).toBe('string');
    expect(events[0].database_name.length).toBeGreaterThan(0);
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('sql_executed fires with a "manual" trigger and database info on a direct Execute click - no SQL text', async ({ page }) => {
    await mockExecute(page, {
      results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }],
    });
    await gotoApp(page);

    await setSqlBox(page, 'SELECT 42 AS n;');
    await page.locator('#runBtn').click();
    // #resultsBody already has exactly one placeholder <tr> ("The answer
    // will appear here...", see index.html) before this click even
    // happens, so `toHaveCount(1)` here is trivially already true and
    // never actually waits for the real execution to finish - it's a race
    // against trackEvent('sql_executed', ...), which fires earlier in
    // executeSql() (client.js) but only after a real, unmocked
    // fetchBackendConfig() round trip settles. Waiting on the header text
    // instead (empty until real results render, same as the "auto"
    // trigger test below) actually synchronizes on the turn being done.
    await expect(page.locator('#resultsHeader th')).toHaveText(['n']);

    const events = await trackedEvents(page, 'sql_executed');
    expect(events.length).toBe(1);
    // No `sql` field - the generated SQL text itself must never reach GA.
    expect(events[0].sql).toBeUndefined();
    expect(events[0].trigger).toBe('manual');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('sql_executed fires with an "auto" trigger when auto-execute runs it', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    // Enable auto-execute first - see preferences-modal.spec.js for the
    // same checkbox/save button this drives. Waiting for the modal itself
    // to be visible (not just clicking the checkbox immediately after
    // #prefsBtn) matters here - the click handler awaits
    // fetchBackendConfig() before loadPreferencesIntoUI() sets the
    // checkbox's initial state, and checking it too early gets clobbered
    // right back to unchecked once that async load resolves.
    await page.locator('#prefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
    await page.locator('#autoSqlExecuteCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);

    const events = await trackedEvents(page, 'sql_executed');
    expect(events.length).toBe(1);
    expect(events[0].trigger).toBe('auto');
  });

  test('error_shown fires with category "translation" for a translation error', async ({ page }) => {
    await mockTranslate(page, { error: 'The model could not understand that request.', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('do something impossible');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsBody')).toContainText('Translation Error');

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('translation');
    expect(events[0].message).toContain('could not understand');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('error_shown fires with category "execution" for an execution error', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    // No separate #runBtn click - a brand-new session identity (this
    // suite's per-test crbot_user_id cookie guarantees one) gets
    // auto_sql_execute defaulted to true server-side (see state_store.py's
    // DEFAULT_AUTO_SQL_EXECUTE / get_session()'s fallback for a session
    // with no saved row yet), so translatePrompt() already auto-executes
    // the translated SQL internally once it comes back (same reasoning as
    // the "auto trigger" test above, which enables the setting explicitly
    // for clarity but doesn't actually need to). A second, explicit click
    // here used to race that internal auto-execute: whichever one's
    // uiActionBusy window (client.js) had already closed by the time the
    // click landed got to run a genuinely SEPARATE executeSql() call,
    // firing this exact event twice on an unlucky (~1 in 5) timing and
    // failing the events.length assertion below.
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('execution');
    expect(events[0].message).toContain('does_not_exist');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('quick_prompt_clicked fires with the chip label and prompt, and still submits a translation', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await gotoApp(page);

    const chip = page.locator('.example-chip').first();
    const chipLabel = (await chip.textContent()).trim();
    await chip.click();
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const events = await trackedEvents(page, 'quick_prompt_clicked');
    expect(events.length).toBe(1);
    expect(events[0].chip_label).toBe(chipLabel);
    expect(events[0].prompt.length).toBeGreaterThan(0);

    // The chip click still drives a real translation - one submitted event
    // downstream of it, same as typing the prompt in by hand would.
    const submitted = await trackedEvents(page, 'translate_submitted');
    expect(submitted.length).toBe(1);
  });
});

test.describe('analytics: report/feedback', () => {
  test('report_submitted fires with the report category on a successful send', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();
    await page.locator('.report-issue-inline-btn').click();
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('error');
  });

  test('report_submitted fires for a "wrong_sql" report too', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await gotoApp(page);

    // #reportSqlBtn is disabled on a genuinely empty SQL box (see client.js's
    // applySqlActionButtonsContentState()) - seed some placeholder SQL so
    // it's clickable at all; this test isn't about the SQL content itself.
    await setSqlBox(page, 'SELECT 1;');
    await page.locator('#reportSqlBtn').click();
    await page.locator('#reportIssueDetails').fill('This looks wrong.');
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('wrong_sql');
  });

  test('report_submitted fires for a plain "feedback" send (the header\'s Send Feedback button) too', async ({ page }) => {
    // sendReportIssue() itself never branches on category before firing
    // this event (see client.js) - the tests above/below already cover
    // 'error', 'wrong_sql', and the two summary_thumbs_up/down categories
    // (see report-issue.spec.js's "Summary tab feedback" describe block);
    // this closes the one remaining gap. 'wrong_result' has no UI trigger
    // at all today (by explicit prior design - see reportButtonHtml()'s
    // own comment), so there's nothing to click for it.
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#sendFeedbackBtn').click();
    await page.locator('#reportIssueDetails').fill('It would be great to have dark mode charts.');
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('feedback');
  });
});

test.describe('analytics: connection/model/nav', () => {
  test('database_selected fires with the newly-selected database name', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#configModal')).not.toHaveClass(/hidden/);
    await page.locator('#modalDbRadioGroup input[name="db_connection_option"][value^="preset:"]').first().check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'database_selected');
    expect(events.length).toBe(1);
    expect(events[0].database_name).toBe(await page.locator('#connDbName').textContent());
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('model_selected fires with the newly-selected provider and model', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#modelTriggerBadge').click();
    await expect(page.locator('#modelModal')).not.toHaveClass(/hidden/);
    await page.locator('input[name="llm_model_option"][value="anthropic::claude-sonnet-5"]').check();
    await page.locator('#modelSaveBtn').click();
    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'model_selected');
    expect(events.length).toBe(1);
    expect(events[0].provider).toBe('anthropic');
    expect(events[0].model).toBe('claude-sonnet-5');
  });

  test('help_viewed fires when the Doc button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'help_viewed')).length).toBe(1);
  });

  test('history_viewed fires when the History button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#historyBtn').click();
    await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'history_viewed')).length).toBe(1);
  });

  test('history_purge_clicked fires with the record count shown next to the Purge button, on click - before the confirm dialog resolves', async ({ page }) => {
    // /api/history is real (unmocked) elsewhere in this suite (see
    // history-anonymous-access.spec.js's header comment) - mocked here
    // instead, purely so the count next to "Purge Translations" is a known,
    // fixed number rather than whatever this test's isolated user identity
    // happens to already have on the shared real dev-server SQLite state.
    await page.route('**/api/history', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, total_count: 7, history: [], stats: [] }),
      });
    });
    await gotoApp(page);

    await page.locator('#historyBtn').click();
    await expect(page.locator('.btn-purge-title')).toHaveText('(7)');

    await page.locator('#purgeHistoryBtn').click();
    // The confirm dialog is up (nothing clicked in it yet) - the event
    // fires on the button click itself, not on confirmation - see
    // client.js's purgeHistoryBtn handler comment.
    await expect(page.locator('#confirmModal')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'history_purge_clicked');
    expect(events.length).toBe(1);
    expect(events[0].record_count).toBe(7);

    // Cancel rather than confirm - the purge itself (and its DELETE call)
    // is out of scope for this test.
    await page.locator('#confirmModalCancelBtn').click();
  });

  test('history_purge_clicked does not fire a record_count when the count has not loaded yet', async ({ page }) => {
    // A GET /api/history that never resolves - .btn-purge-title stays at
    // its "(...)" loading placeholder (see the historyBtn click handler in
    // client.js) for the lifetime of this test.
    await page.route('**/api/history', () => {});
    await gotoApp(page);

    await page.locator('#historyBtn').click();
    await expect(page.locator('.btn-purge-title')).toHaveText('(...)');

    await page.locator('#purgeHistoryBtn').click();
    await expect(page.locator('#confirmModal')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'history_purge_clicked');
    expect(events.length).toBe(1);
    expect(events[0].record_count).toBeUndefined();

    await page.locator('#confirmModalCancelBtn').click();
  });

  test('history_nav_clicked fires with the turn offset when stepping back and forward', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('first question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 1');

    // A second turn - #goBackBtn/#goForwardBtn only enable with more than
    // one turn in chatStore (see updateHistoryNavButtons()'s own comment on
    // why one remaining turn already counts as "oldest").
    await mockTranslate(page, { sql: 'SELECT 2;' });
    await page.locator('#aiPrompt').fill('second question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 2');

    // Current turn is 0; stepping back once lands on -1, forward again
    // returns to 0 - see chatStore.turnOffset()'s own comment.
    await page.locator('#goBackBtn').click();
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 1');
    await page.locator('#goForwardBtn').click();
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 2');

    const events = await trackedEvents(page, 'history_nav_clicked');
    expect(events.length).toBe(2);
    expect(events[0].turn_offset).toBe(-1);
    expect(events[1].turn_offset).toBe(0);
  });

  test('preferences_viewed fires when the Preferences button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#prefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'preferences_viewed')).length).toBe(1);
  });
});

// Reuses auth-clears-state.spec.js's own Google Identity Services stub -
// see that file's header comment for exactly what is/isn't real here.
test.describe('analytics: auth', () => {
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

  test('login fires once when Google Sign-In completes, with GA4\'s own "method" parameter', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('newuser@example.com')
    );

    const events = await trackedEvents(page, 'login');
    expect(events.length).toBe(1);
    // GA4's own recommended "login" event shape (see
    // https://developers.google.com/analytics/devguides/collection/ga4/reference/events) -
    // "method" is its one recommended parameter, always "Google" here since
    // that's the only sign-in method this app supports.
    expect(events[0].method).toBe('Google');
  });

  test('logout fires once when the user signs out', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();

    expect((await trackedEvents(page, 'logout')).length).toBe(1);
  });
});
