// tests/e2e/analytics.spec.js
//
// Custom GA4 events fired via client.js's trackEvent() (a thin wrapper
// around gtag('event', name, params) - see that function's own header
// comment for the full list: translate_submitted, sql_executed,
// error_shown, report_submitted, database_selected, model_selected,
// help_viewed, history_viewed, history_nav_clicked, preferences_viewed,
// login, logout, mic_used).
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

  test('error_shown fires with category "Database Connection" when the status ping comes back down', async ({ page }) => {
    // Overrides the `test` fixture's own default /api/ping mock (always
    // success:true) - Playwright checks the most-recently-registered
    // matching route handler first, so this one wins for this test only.
    // gotoApp() itself only waits for A response (any status) to the
    // initial ping, not specifically a successful one, so this doesn't
    // need any special handling beyond the route override itself.
    await page.route('**/api/ping', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ success: false, error: 'could not connect to server: Connection refused' }),
      });
    });
    await gotoApp(page);

    await expect(page.locator('#connDbDot')).toHaveClass(/disconnected/);

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('Database Connection');
    expect(events[0].message).toContain('Connection refused');
    expect(typeof events[0].database_type).toBe('string');
  });

  test('error_shown fires with category "Database Connection" when the ping request itself fails (network error)', async ({ page }) => {
    // Same event, but for checkDbStatus()'s catch branch (the fetch itself
    // rejects) rather than a non-throwing success:false response - see
    // that function's own two trackEvent() call sites. Not using gotoApp()
    // here - it waits for a real 'response' event to the initial ping,
    // which an aborted request never produces (it only ever surfaces as
    // 'requestfailed'), so that wait would just hang until timeout. This
    // inlines gotoApp()'s own post-navigation waits minus that one.
    await page.route('**/api/ping', (route) => route.abort());
    await page.goto('/');
    await page.locator('#connDbName').waitFor({ state: 'attached' });
    await expect
      .poll(async () => (await page.locator('#connDbName').textContent())?.trim())
      .not.toBe('');

    await expect(page.locator('#connDbDot')).toHaveClass(/disconnected/);

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('Database Connection');
    // Whatever the browser's own fetch-rejection message is (e.g. "Failed
    // to fetch") - not asserting exact text since that's runtime/browser-
    // dependent, just that SOME message came through rather than nothing.
    expect(events[0].message.length).toBeGreaterThan(0);
  });

  // There used to be a test here for quick_prompt_clicked, fired by
  // clicking one of the "Sample prompts" example-chip buttons above the
  // prompt box. That whole section (#examplePrompts/.example-chip) was
  // removed from the app entirely (see app-shell.spec.js's own removal-
  // guard test), and nothing in client.js fires quick_prompt_clicked any
  // more - so there's deliberately no replacement coverage for this event
  // now. Left this comment specifically because the old test didn't fail
  // outright once the chips disappeared - it just hung waiting on a
  // selector that would never appear, timing out instead of failing fast,
  // which is what actually surfaced this gap.
});

// Dataset group mode fans a single NL prompt out into one real translate
// call per connection in the selected group (server-side, translate_routes.py's
// _run_phase_b_fanout) and, with auto-execute on, one real /api/execute
// call per connection too (client-side, executeOneAllModeConnection() in
// client.js) - before trackAllModeFanoutTranslate()/trackAllModeFanoutExecute()
// existed, none of those individual fanned-out requests were visible in
// GA at all, only the one top-level translate_submitted/sql_executed event
// per user action, regardless of how many databases it actually touched.
// Rather than invent new event names for this (per explicit request - "too
// many different events already"), those two functions fire the SAME
// translate_submitted/sql_executed events again, once per connection - so
// a turn against a 2-member dataset group shows up as 3 translate_submitted
// events (1 generic "the prompt was submitted" + 2 real per-database
// calls), distinguishable by `database_name`: the generic one's is the
// group's own name badge text, each fan-out one's is that specific
// database's own name. See trackEvent()'s own header comment in client.js
// for the full reasoning. Note trackAllModeFanoutTranslate()/
// trackAllModeFanoutExecute()'s own names, and this describe block's use of
// "all mode"/"all-mode" as a generic internal label for this multi-candidate
// fan-out machinery, predate and are independent of the "dataset group"
// concept itself (see translate_routes.py's own docstring on this same
// naming split) - only the actual user-facing config shape (in_scope_mode/
// in_scope_group_id/configured_database_groups) changed. Config/NDJSON
// shapes here mirror multi-database.spec.js's own dataset-group-mode
// fixtures (buildConfigState/mockConfig, the raw phase_a_route/
// phase_b_connection_done NDJSON bodies) rather than importing them - kept
// local, same convention that file's own helpers already use (not exported
// from fixtures.js).
test.describe('analytics: dataset group mode fan-out', () => {
  function buildAllModeConfigState(overrides) {
    return {
      auth_enabled: false,
      session_id: 'e2e-session',
      user_id: 'global',
      authenticated: false,
      is_cloud_run: false,
      configured_databases: [
        { id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
        { id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
      ],
      configured_database_groups: [
        { id: 'grp-ab', name: 'Sales & Marketing', dataset_list: ['p-a', 'p-b'] },
      ],
      active_preset_id: 'p-a',
      default_database_url: '',
      active_database_url: '',
      active_database_type: 'postgres',
      active_is_custom: false,
      active_custom_connection_key: '',
      active_uses_custom_credentials: false,
      database_name: 'Sales Postgres',
      custom_database_name: '',
      custom_database_url: '',
      custom_databases: [],
      auto_sql_execute: false,
      in_scope_preset_ids: ['p-a', 'p-b'],
      in_scope_custom_connection_keys: [],
      in_scope_mode: 'group',
      in_scope_group_id: 'grp-ab',
      max_in_scope_connections: 20,
      ...overrides,
    };
  }

  async function mockAllModeConfig(page, overrides) {
    const state = buildAllModeConfigState(overrides);
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
    });
  }

  function ndjsonBody(events) {
    return events.map((e) => JSON.stringify(e)).join('\n') + '\n';
  }

  test('translate_submitted fires once per connection in the fan-out, in addition to (not instead of) the once-per-prompt call', async ({ page }) => {
    await mockAllModeConfig(page); // auto_sql_execute: false - this test only cares about the translate side
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = ndjsonBody([
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres', prompt: 'deals and campaigns' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres', prompt: 'deals and campaigns' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres',
          outcome: 'note', text: 'Nothing relevant here.',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [{ kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant here.' }],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres', prompt: 'deals and campaigns' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres', prompt: 'deals and campaigns' },
          ],
        },
      ]);
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    // 1 generic "the prompt was submitted" call + 2 real per-database
    // calls, ALL named 'translate_submitted' - see this describe block's
    // own header comment for why they're not split into separate event
    // names.
    const events = await trackedEvents(page, 'translate_submitted');
    expect(events.length).toBe(3);

    // The generic, once-per-prompt call - same "Sales & Marketing" group
    // badge text connDbName shows, and no way to know which specific
    // database(s) will even be asked yet (translatePrompt() fires this
    // before the request is even sent).
    const genericEvent = events.find((e) => e.database_name === 'Sales & Marketing');
    expect(genericEvent).toBeTruthy();
    expect(genericEvent.mode).toBe('group');

    // The two real per-database calls, fired once phase_a_route reveals
    // which connections the fan-out actually picked.
    const perDatabase = events.filter((e) => e.database_name !== 'Sales & Marketing');
    expect(perDatabase.length).toBe(2);
    expect(perDatabase.every((e) => e.mode === 'group')).toBe(true);
    const byName = Object.fromEntries(perDatabase.map((e) => [e.database_name, e]));
    expect(byName['Sales Postgres'].database_type).toBe('postgres');
    expect(byName['Marketing Postgres'].database_type).toBe('postgres');

    // No `prompt`/`sql` text on any of them - same GA privacy rule as
    // every other translate/execute event in this file.
    expect(events.every((e) => e.prompt === undefined && e.sql === undefined)).toBe(true);
  });

  test('sql_executed fires once per connection with trigger "auto" when auto-execute streams the fan-out - never once for the whole batch', async ({ page }) => {
    await mockAllModeConfig(page, { auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = ndjsonBody([
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
          ],
        },
      ]);
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const body = route.request().postDataJSON();
      const isA = body.sql.includes('preset:p-a');
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            isA
              ? { columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }
              : { columns: ['total'], rows: [{ total: 100 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsTabsNav .result-tab-btn')).toHaveCount(3);

    // executeOneAllModeConnection() bypasses executeSql() (and its own
    // once-per-click trackEvent('sql_executed', ...) call) entirely, so
    // there's no third "generic" event here at all - only the two
    // per-connection ones. Previously this meant NEITHER database's
    // execution was tracked at all.
    const events = await trackedEvents(page, 'sql_executed');
    expect(events.length).toBe(2);
    expect(events.every((e) => e.trigger === 'auto')).toBe(true);
    const byName = Object.fromEntries(events.map((e) => [e.database_name, e]));
    expect(byName['Sales Postgres'].database_type).toBe('postgres');
    expect(byName['Marketing Postgres'].database_type).toBe('postgres');
  });

  test('sql_executed fires once per connection plus once for the whole click, for a single batched Execute (auto-execute off)', async ({ page }) => {
    await mockAllModeConfig(page); // auto_sql_execute: false (default)
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = ndjsonBody([
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
          ],
        },
      ]);
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    // Nothing executed yet - auto-execute is off, so both connections still
    // sit in their own "Ready to execute" placeholder (see
    // multi-database.spec.js's identical test for this exact banner text).
    expect((await trackedEvents(page, 'sql_executed')).length).toBe(0);

    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsTabsNav .result-tab-btn').nth(1)).toContainText('Sales Postgres');

    // One /api/execute round trip, but 1 generic click-level event + one
    // real per-connection event underneath it, all named 'sql_executed' -
    // same "additive under one name" reasoning as translate_submitted
    // above, not a separate event name for the fan-out. executeSql()'s own
    // trackEvent()/trackAllModeFanoutExecute() calls for this path fire
    // synchronously in the click handler, before the /api/execute fetch is
    // even sent, so in principle all 3 already exist by the time the tab
    // assertion above resolves - but this exact line has been observed to
    // read 0 events instead of 3 on an otherwise-identical run, which a
    // page.evaluate() snapshot timed right off a UI assertion can't fully
    // rule out (dataLayer lives in the page, snapshotting it is its own
    // round trip). expect.poll() costs nothing when the count is already
    // right (resolves on the first check) and removes the snapshot-timing
    // risk entirely when it isn't.
    await expect.poll(async () => (await trackedEvents(page, 'sql_executed')).length).toBe(3);
    const events = await trackedEvents(page, 'sql_executed');

    const genericEvent = events.find((e) => e.database_name === 'Sales & Marketing');
    expect(genericEvent).toBeTruthy();
    expect(genericEvent.trigger).toBe('manual');

    const perDatabase = events.filter((e) => e.database_name !== 'Sales & Marketing');
    expect(perDatabase.length).toBe(2);
    expect(perDatabase.every((e) => e.trigger === 'manual')).toBe(true);
    const byName = Object.fromEntries(perDatabase.map((e) => [e.database_name, e]));
    expect(byName['Sales Postgres'].database_type).toBe('postgres');
    expect(byName['Marketing Postgres'].database_type).toBe('postgres');
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

  test('report_submitted fires for a "correct_sql" report too', async ({ page }) => {
    // The positive counterpart to the 'wrong_sql' test above - same
    // reasoning (buildReportPayload() passes ctx.category through
    // unchanged, so this needs no special-casing anywhere in
    // sendReportIssue()/trackEvent() to already work).
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await gotoApp(page);

    await setSqlBox(page, 'SELECT 1;');
    await page.locator('#reportSqlGoodBtn').click();
    await page.locator('#reportIssueDetails').fill('This looks right.');
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('correct_sql');
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
    await expect(page.locator('#modelPickList')).not.toHaveClass(/hidden/);
    // Scoped to #modelPickList - renderModelPickList() (client.js) fills
    // an identical #moreMenuModelOptions list for the narrow-header more-
    // menu too (see model-selection.spec.js's own describe block for that
    // one), so an unscoped .model-pick-option[data-value=...] now matches
    // both and trips Playwright's strict mode.
    await page.locator('#modelPickList .model-pick-option[data-value="anthropic::claude-sonnet-5"]').click();
    await expect(page.locator('#modelPickList')).toHaveClass(/hidden/);

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

  test('chat_history_delete_clicked fires with the bucket\'s kind and turn count, on click - before the confirm dialog resolves', async ({ page }) => {
    // /api/chat-history/summary is real (unmocked) elsewhere in this suite
    // (chat-history-persistence.spec.js) - mocked here purely so there's a
    // known, fixed bucket to click Delete on, rather than whatever this
    // test's isolated identity happens to already have on the shared real
    // dev-server SQLite state.
    await page.route('**/api/chat-history/summary', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          buckets: [{ bucket_key: 'preset:x', turn_count: 5, kind: 'preset', name: 'Test DB', type: 'postgres', available: true }],
        }),
      });
    });
    await gotoApp(page);

    await page.locator('#historyBtn').click();
    const deleteBtn = page.locator('.chat-history-bucket-delete-btn');
    await expect(deleteBtn).toBeVisible();
    await deleteBtn.click();
    // The confirm dialog is up (nothing clicked in it yet) - the event
    // fires on the button click itself, not on confirmation - see
    // client.js's chatHistoryBucketList click handler comment.
    await expect(page.locator('#confirmModal')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'chat_history_delete_clicked');
    expect(events.length).toBe(1);
    expect(events[0].kind).toBe('preset');
    expect(events[0].turn_count).toBe(5);

    // Cancel rather than confirm - the delete itself (and its POST call)
    // is out of scope for this test.
    await page.locator('#confirmModalCancelBtn').click();
  });

  test('chat_history_delete_all_clicked fires with the total bucket and turn counts, on click - before the confirm dialog resolves', async ({ page }) => {
    await page.route('**/api/chat-history/summary', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          buckets: [
            { bucket_key: 'preset:x', turn_count: 3, kind: 'preset', name: 'Test DB', type: 'postgres', available: true },
            { bucket_key: 'all', turn_count: 2, kind: 'all', name: 'All Pre-Configured Datasets (combined)', type: null, available: true },
          ],
        }),
      });
    });
    await gotoApp(page);

    await page.locator('#historyBtn').click();
    await expect(page.locator('.chat-history-bucket-row')).toHaveCount(2);

    await page.locator('#deleteAllChatHistoryBtn').click();
    await expect(page.locator('#confirmModal')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'chat_history_delete_all_clicked');
    expect(events.length).toBe(1);
    expect(events[0].bucket_count).toBe(2);
    expect(events[0].turn_count).toBe(5);

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
