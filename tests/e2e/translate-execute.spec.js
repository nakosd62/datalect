// tests/e2e/translate-execute.spec.js
//
// The core "ask a question -> get SQL -> run it -> see results" flow, with
// /api/translate and /api/execute intercepted in-browser (see fixtures.js)
// so this never needs a real Gemini key or a real target database. Every
// other request (page load, /api/config) hits the real Flask server.

// Uses testShowSqlVisible (aliased to `test`) rather than the plain `test`
// fixture - see that fixture's own comment in fixtures.js - since this
// suite drives the app through the SQL box itself (#runBtn, #sqlQuery/
// CodeMirror), which client.js now hides by default.
const { testShowSqlVisible: test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

function currentSql(page) {
  // Mirrors client.js's own getSqlQuery(): CodeMirror (loaded from a CDN -
  // see index.html) replaces #sqlQuery with a rendered editor when it's
  // available, but client.js deliberately falls back to the plain
  // textarea's value when it isn't (offline/CDN-blocked environments), so
  // this checks both rather than assuming CodeMirror initialized.
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

/** client.js's setSqlQuery() also runs generated SQL through sql-formatter
 * (another CDN script - see index.html) when it's available, pretty-
 * printing it onto multiple lines. Whether that happens is purely a
 * function of whether that CDN script loaded, not anything this suite
 * controls, so assertions against generated SQL check for individual
 * whitespace-free tokens (normalized to single spaces) rather than one
 * exact multi-word phrase - safe whether or not it got reformatted. */
async function normalizedSql(page) {
  return (await currentSql(page) || '').replace(/\s+/g, ' ').trim();
}

/** Directly seeds the SQL box, bypassing translate entirely - mirrors
 * currentSql()'s own CodeMirror-or-textarea fallback. Used where a test
 * needs SOME non-empty SQL present (Execute/#reportSqlBtn are both
 * disabled on an empty box - see client.js's applySqlActionButtonsContentState())
 * but isn't itself testing translation. */
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

test.describe('translate + execute', () => {
  test('translating a prompt fills in the generated SQL', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM users LIMIT 10;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('show me the first 10 users');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => normalizedSql(page)).toContain('SELECT');
    expect(await normalizedSql(page)).toContain('users');
    expect(await normalizedSql(page)).toContain('LIMIT');
  });

  test('resubmitting the same, un-edited prompt clears the stale SQL right away instead of leaving it up through the whole in-flight request', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM users LIMIT 10;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('show me the first 10 users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('users');

    // Re-mock with a response that only resolves once this test lets it,
    // so the SQL box's state can be inspected WHILE the second request is
    // still in flight - the whole point of this test.
    let resolveTranslate;
    const translateGate = new Promise((resolve) => { resolveTranslate = resolve; });
    await page.unroute('**/api/translate');
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await translateGate;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sql: 'SELECT * FROM orders LIMIT 5;' }),
      });
    });

    // Resubmit via Enter with NO edit to #aiPrompt in between (no .fill()
    // call, no keystroke) - the exact scenario that used to leave the
    // first response's SQL sitting in the box, since client.js only ever
    // cleared it from aiPrompt's own 'input' listener, which never fires
    // without an actual edit.
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => normalizedSql(page)).toBe('');

    resolveTranslate();
    await expect.poll(() => normalizedSql(page)).toContain('orders');
  });

  test('running the generated SQL renders a results table', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{
        columns: ['id', 'name'],
        rows: [{ id: 1, name: 'Ada' }, { id: 2, name: 'Grace' }],
        rowCount: 2,
      }],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT');
    expect(await normalizedSql(page)).toContain('id');
    expect(await normalizedSql(page)).toContain('name');

    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
    const rows = page.locator('#resultsBody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText('Ada');
    await expect(rows.nth(1)).toContainText('Grace');
  });

  // Client-only column sorting (client.js's classifySortableColumnType()/
  // handleSortableColumnClick()) - no network request involved at all, so
  // this only needs the one initial /api/execute mock; every assertion below
  // is about in-browser reordering of the already-rendered rows.
  test('clicking a column header sorts rows: numbers/dates default DESC, strings default ASC, and a second click toggles', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT name, age FROM people;' });
    await mockExecute(page, {
      results: [{
        columns: ['name', 'age'],
        rows: [
          { name: 'Charlie', age: 30 },
          { name: 'Alice', age: 10 },
          { name: 'Bob', age: 20 },
        ],
        rowCount: 3,
      }],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list people');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    const rows = page.locator('#resultsBody tr.result-data-row');
    await expect(rows).toHaveCount(3);
    // Unsorted (translate/execute mock order): Charlie, Alice, Bob.
    await expect(rows.nth(0)).toContainText('Charlie');

    const ageHeader = page.locator('#resultsHeader th', { hasText: 'age' });
    const nameHeader = page.locator('#resultsHeader th', { hasText: 'name' });

    // Numeric column's first click defaults to DESC.
    await ageHeader.click();
    await expect(rows.nth(0)).toContainText('30');
    await expect(rows.nth(1)).toContainText('20');
    await expect(rows.nth(2)).toContainText('10');
    await expect(ageHeader).toContainText('▼');

    // Second click on the SAME header toggles to ASC.
    await ageHeader.click();
    await expect(rows.nth(0)).toContainText('10');
    await expect(rows.nth(1)).toContainText('20');
    await expect(rows.nth(2)).toContainText('30');
    await expect(ageHeader).toContainText('▲');

    // Switching to a string column's header defaults (fresh) to ASC, and
    // only that header shows an arrow now.
    await nameHeader.click();
    await expect(rows.nth(0)).toContainText('Alice');
    await expect(rows.nth(1)).toContainText('Bob');
    await expect(rows.nth(2)).toContainText('Charlie');
    await expect(nameHeader).toContainText('▲');
    await expect(ageHeader).not.toContainText('▼');
    await expect(ageHeader).not.toContainText('▲');

    // No server calls were made by any of the clicks above beyond the
    // original translate/execute ones already awaited - nothing to assert
    // here beyond the fact that the mocked routes only fire once each,
    // which mockTranslate/mockExecute's own single-registration already
    // guarantees (a second real request with no matching route would hang
    // the test rather than silently passing).
  });

  // Regression/feature coverage for EXECUTE_RESULTS_MAX_ROWS (backends/
  // base.py) - a query matching more rows than that cap gets its result
  // silently truncated server-side (every backend's execute() flags this
  // with a "truncated": true key on that statement's result dict, see
  // fetch_capped_rows()'s own docstring), and the client must never show
  // that as if it were the complete answer.
  test('a truncated result shows a visible warning banner and a "+" on its tab, scoped to just that tab', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id FROM huge_table; SELECT 1 AS n;' });
    await mockExecute(page, {
      results: [
        { columns: ['id'], rows: [{ id: 1 }, { id: 2 }], rowCount: 2, truncated: true },
        { columns: ['n'], rows: [{ n: 1 }], rowCount: 1 },
      ],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('show me every id, then a plain 1');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('huge_table');

    await page.locator('#runBtn').click();

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    // The truncated statement's own tab (index 0, active by default) shows
    // a "2+ rows" count, not a plain "2 rows".
    await expect(tabs.nth(0)).toContainText('2+ rows');
    await expect(page.locator('#resultsTruncatedNotice')).toBeVisible();
    await expect(page.locator('#resultsTruncatedNotice')).toContainText('first 2 rows');

    // The second, un-truncated statement's own tab has a plain row count
    // and no banner at all when it's the active one.
    await expect(tabs.nth(1)).toContainText('1 row');
    await expect(tabs.nth(1)).not.toContainText('+');
    await tabs.nth(1).click();
    await expect(page.locator('#resultsTruncatedNotice')).toBeHidden();
  });

  test('an ordinary, un-truncated result never shows the truncation banner or a "+" on its tab', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
    await expect(page.locator('#resultsTruncatedNotice')).toBeHidden();
  });

  test('a translation error is surfaced in the results area', async ({ page }) => {
    await mockTranslate(page, { error: 'The model could not understand that request.', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('do something impossible');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#resultsBody')).toContainText('Translation Error');
    await expect(page.locator('#resultsBody')).toContainText('The model could not understand that request.');
  });

  test('an execution error is surfaced in the results area', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('does_not_exist');

    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    await expect(page.locator('#resultsBody')).toContainText('does not exist');
  });

  test('a multi-statement script that fails partway through shows one tab per attempted statement, with the failed one flagged', async ({ page }) => {
    // Mirrors execute_routes.py's SqlExecutionError-shaped response: the
    // first of three statements succeeded, the second failed, and the
    // third was never attempted (correct behavior - the script stops at
    // the first failure) - see backends/base.py's SqlExecutionError
    // docstring and this app's fixtures.js mockExecute() jsdoc.
    await mockTranslate(page, { sql: 'UPDATE users SET x=1; SELEC bad syntax; SELECT 1;' });
    await mockExecute(page, {
      results: [{ columns: null, rows: null, rowCount: 3, statement: 'UPDATE users SET x=1' }],
      error: 'syntax error at or near "SELEC"',
      failedStatement: 'SELEC bad syntax',
      failedIndex: 1,
      totalStatements: 3,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('do three things, the second one is bad');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELEC');

    await page.locator('#runBtn').click();

    // Two tabs total - one per ATTEMPTED statement (the succeeded one +
    // the failed one) - never a third for the statement that was never
    // run. Scoped to #resultsTabsNav rather than an unscoped page-wide
    // .result-tab-btn locator, on general principle.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);

    // Defaults to showing the failure immediately, not the first
    // (successful) tab - see renderResultsWithFailedStatement()'s comment
    // on why.
    await expect(tabs.nth(1)).toHaveClass(/result-tab-btn--error/);
    await expect(tabs.nth(1)).toHaveClass(/active/);
    await expect(tabs.nth(0)).not.toHaveClass(/result-tab-btn--error/);
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    await expect(page.locator('#resultsBody')).toContainText('syntax error at or near "SELEC"');

    // Clicking back to the first (successful) tab shows its own results,
    // not the error - the two tabs' content is genuinely independent.
    await tabs.nth(0).click();
    await expect(page.locator('#resultsBody')).not.toContainText('Execution Error');
    await expect(tabs.nth(0)).toHaveClass(/active/);
  });

  test('a conversational (no-SQL) reply is rendered as text, not a query', async ({ page }) => {
    await mockTranslate(page, { sql: '*** NO SQL *** I can only answer questions about your data.' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('what is the meaning of life');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('.response-text')).toContainText('I can only answer questions about your data.');
    await expect.poll(() => currentSql(page)).toBe('');
  });

  // The "OPEN HELP POPUP"/"OPEN SCHEMA VIEWER" marker conventions
  // (client.js's isOpenHelp/isOpenSchema branches, just above the plain
  // isNoSql branch the test above covers) had no e2e coverage at all
  // before this pair of tests - discovered while reviewing this exact
  // area for translate_routes.py's own two-call single-connection
  // redesign (see that file's module-level section comment above
  // _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION): Call 1 now DECIDES the
  // "schema"/"help" outcome via a JSON "action" field rather than a model
  // free-typing one of these marker strings itself, but the server still
  // constructs the exact same marker text for client.js to consume - so
  // client.js's own handling of it is completely unchanged and, like the
  // rest of this file, exercised here purely through mockTranslate()'s
  // network-layer mock, independent of anything server-side.
  test('an "OPEN HELP POPUP" reply opens the real Help modal instead of rendering as text', async ({ page }) => {
    await mockTranslate(page, { sql: '*** NO SQL *** OPEN HELP POPUP ***' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how do I use this app?');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);
    // Never falls through to the plain NO-SQL text render alongside it.
    await expect(page.locator('.response-text')).toHaveCount(0);
    await expect.poll(() => currentSql(page)).toBe('');
  });

  test('an "OPEN SCHEMA VIEWER" reply opens the real Schema Viewer modal instead of rendering as text', async ({ page }) => {
    await mockTranslate(page, { sql: '*** NO SQL *** OPEN SCHEMA VIEWER ***' });
    // openSchemaViewer() (client.js) fetches the real Schema Viewer's own
    // data source - mocked here the same minimal way schema-viewer.spec.js
    // does for its own tests, since this spec only cares that client.js
    // actually reaches the modal, not what the modal then renders from it.
    await page.route('**/api/schema*', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          kind: 'preset',
          id: 'default',
          name: 'Orders DB',
          dialect: 'PostgreSQL',
          truncated: false,
          has_omitted_tables: false,
          entries: [{ name: 'orders', heading: 'Table: orders', text: 'Table: orders\n  id integer NOT NULL' }],
          cached_at: null,
          overview: null,
        }),
      });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('what is in this dataset?');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#schemaViewerModal')).not.toHaveClass(/hidden/);
    await expect(page.locator('.response-text')).toHaveCount(0);
    await expect.poll(() => currentSql(page)).toBe('');
  });

  // /api/translate normally streams newline-delimited JSON - zero or more
  // {"status": "retrying", ...} progress lines (rendered live at the top
  // of the results area - see client.js's showRetryStatus()/
  // readTranslateStream()) followed by one terminal {"status": "done",
  // ...} line - rather than the single-object body mockTranslate() above
  // sends (see translate_routes.py's module docstring). The retry-line
  // shape itself is covered thoroughly at the Python level (see
  // tests/server/test_translate_routes.py) and the live-progress timing
  // isn't reliably observable through Playwright's route.fulfill() (it
  // delivers a mocked body as one atomic chunk, not staggered over real
  // time, so the "retrying" line and the terminal line both get parsed in
  // the same synchronous burst before anything repaints - there's nothing
  // for a test to catch mid-transition). What IS worth covering here,
  // and wouldn't be caught by any single-object mock: that
  // readTranslateStream()'s line-by-line parser still finds and returns
  // the terminal line correctly when a real retry line precedes it in the
  // same body, and that the retry banner doesn't linger once the terminal
  // line has been processed.
  test('a translate response with a retry line ahead of the terminal line still resolves to the terminal SQL, with no lingering retry banner', async ({ page }) => {
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson =
        JSON.stringify({ status: 'retrying', attempt: 2, maxAttempts: 5, delaySeconds: 1, rotatedKey: false }) + '\n' +
        JSON.stringify({ status: 'done', success: true, sql: 'SELECT * FROM retried_users;' }) + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('show me the first 10 users');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => normalizedSql(page)).toContain('retried_users');
    await expect(page.locator('#resultsRetryStatus')).toHaveClass(/hidden/);
  });

  // The Cancel button (#stopBtn) shows/hides purely off setButtonsDisabled()
  // - independent of #resultsRetryStatus, which single-connection mode never
  // shows at all outside a real retry. .results-status-row used to lay the
  // two out with plain flex-start, so the button sat wherever
  // #resultsRetryStatus's box ended - flush against it when the status text
  // was visible, but all the way over at the row's LEFT edge whenever that
  // text was hidden (as it is for this entire test), visibly jumping left
  // and right as the banner came and went. justify-content: flex-end pins
  // it to the row's right edge unconditionally instead.
  test('the Cancel button stays pinned to the right edge while the progress banner is hidden', async ({ page }) => {
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

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    const stopBtn = page.locator('#stopBtn');
    await expect(stopBtn).toBeVisible();
    await expect(stopBtn).toHaveText(/Cancel/);
    await expect(page.locator('#resultsRetryStatus')).toHaveClass(/hidden/);

    const [btnBox, rowBox] = await Promise.all([
      stopBtn.boundingBox(),
      page.locator('.results-status-row').boundingBox(),
    ]);
    // #stopBtn.btn-stop carries its own 0.75rem (12px) right margin, so its
    // right edge sits a little inside the row's - the regression this
    // guards against was the button flush against the row's LEFT edge
    // instead (tens/hundreds of pixels away), so a generous margin-sized
    // tolerance is enough to distinguish "pinned right" from "fell left".
    expect(rowBox.x + rowBox.width - (btnBox.x + btnBox.width)).toBeLessThan(20);

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 1');
  });

  // Regression guard: the DB connection and model badges used to stay
  // fully clickable while a translate/execute call was in flight, letting
  // someone swap the active connection or model out from under a request
  // that was already running against the old one. setButtonsDisabled()
  // now grays both out (badge-disabled) and their own click handlers no-op
  // while that class is present - see its comment in client.js. The
  // doc/history/preferences icons are deliberately NOT covered by this -
  // none of their popups touch state an in-flight turn depends on.
  test('the DB connection and model badges are disabled while a query is in flight, and re-enabled once it settles', async ({ page }) => {
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

    const dbBadge = page.locator('#configTriggerBadge');
    const modelBadge = page.locator('#modelTriggerBadge');
    const historyBtn = page.locator('#historyBtn');
    const micBtn = page.locator('#micBtn');
    const runBtn = page.locator('#runBtn');

    // Execute is ALSO disabled on a genuinely empty SQL box (see
    // client.js's applySqlActionButtonsContentState()) - a concern this
    // test isn't about, so seed some placeholder SQL first to isolate the
    // "in flight vs settled" behavior this test actually exercises.
    await setSqlBox(page, 'SELECT 1;');

    // Sanity check on the resting state, before anything is in flight.
    await expect(dbBadge).not.toHaveClass(/badge-disabled/);
    await expect(modelBadge).not.toHaveClass(/badge-disabled/);
    await expect(micBtn).not.toBeDisabled();
    await expect(runBtn).not.toBeDisabled();

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(dbBadge).toHaveClass(/badge-disabled/);
    await expect(modelBadge).toHaveClass(/badge-disabled/);
    // Not just visually grayed out - actually inert. Clicking either while
    // disabled must not open its modal/pick list.
    await dbBadge.click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await modelBadge.click();
    await expect(page.locator('#modelPickList')).toHaveClass(/hidden/);
    // An unrelated icon (history) stays fully enabled the whole time - its
    // popup doesn't touch the active connection/model.
    await expect(historyBtn).not.toBeDisabled();
    // The mic and Execute buttons are disabled too (see setButtonsDisabled()
    // in client.js) - a real `disabled` attribute, not just a CSS look, per
    // explicit request that the mic get the same treatment "like most other
    // buttons".
    await expect(micBtn).toBeDisabled();
    await expect(runBtn).toBeDisabled();

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 1');

    await expect(dbBadge).not.toHaveClass(/badge-disabled/);
    await expect(modelBadge).not.toHaveClass(/badge-disabled/);
    await expect(micBtn).not.toBeDisabled();
    await expect(runBtn).not.toBeDisabled();
  });

  // Regression guard: the history nav buttons (#goBackBtn/#goForwardBtn/
  // #newTurnBtn) used to get re-enabled mid-turn, well before the turn as a
  // whole actually settled - setButtonsDisabled(true) force-disables them
  // at the very start of a turn, same as every other control above, but
  // updateHistoryNavButtons() (wired to fire again as soon as the new
  // turn's own pushActiveTurn()/updateHistoryTurnsSubtitle() runs, right
  // after /api/translate's terminal line arrives but BEFORE auto-execute's
  // own /api/execute call - which auto_sql_execute defaults to firing
  // automatically - has resolved) used to recompute a fresh, no-longer-
  // boundary undo/redo state and flip them back on itself, ignoring the
  // in-flight turn entirely. Fixed by having updateHistoryNavButtons() also
  // check uiActionBusy (client.js) and stay forced-disabled while it's
  // true, mirroring the identical pattern onSqlContentMaybeChanged()
  // already used for the SQL box's own Execute/report buttons.
  test('the history nav buttons stay disabled through the ENTIRE turn, not just until the SQL comes back', async ({ page }) => {
    // Two turns completed up front (fast, no delay) so #goBackBtn is
    // genuinely enabled at rest afterward (chatStore.canUndo() needs 2+
    // turns - see client.js's ChatStore) - otherwise its "no earlier turn
    // to go back to" boundary state alone would already leave it disabled,
    // masking the regression this test is actually about.
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 1 }], rowCount: 1 }] });
    await gotoApp(page);
    await page.locator('#aiPrompt').fill('first question');
    await page.locator('#aiPrompt').press('Enter');
    // normalizedSql(), not raw currentSql() - see this file's own comment
    // on normalizedSql() for why: sql-formatter (a CDN script) may or may
    // not have loaded, and when it has, it pretty-prints "SELECT 1;" onto
    // multiple lines/indentation, which a plain toContain('SELECT 1')
    // against the unnormalized text can miss depending on exactly how it
    // got reformatted.
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 1');

    await mockTranslate(page, { sql: 'SELECT 2;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 2 }], rowCount: 1 }] });
    await page.locator('#aiPrompt').fill('second question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 2');

    const goBackBtn = page.locator('#goBackBtn');
    const newTurnBtn = page.locator('#newTurnBtn');
    await expect(goBackBtn).not.toBeDisabled();
    await expect(newTurnBtn).not.toBeDisabled();

    // Third turn: /api/translate resolves quickly with real SQL (so
    // pushActiveTurn()/updateHistoryTurnsSubtitle() fires - the exact call
    // that used to re-enable these buttons), but the auto-execute call it
    // triggers internally is held open, simulating a slow query - the turn
    // as a whole is still very much in flight for the whole delay.
    await mockTranslate(page, { sql: 'SELECT 3;' });
    let resolveExecute;
    const executeStarted = new Promise((resolve) => { resolveExecute = resolve; });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      resolveExecute();
      await new Promise((r) => setTimeout(r, 2000));
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, results: [{ columns: ['n'], rows: [{ n: 3 }], rowCount: 1 }] }),
      });
    });

    await page.locator('#aiPrompt').fill('third question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 3');
    await executeStarted;

    // The SQL is already in the box (translate's own terminal line landed)
    // but /api/execute is still in flight - this is exactly the window the
    // old code got wrong.
    await expect(goBackBtn).toBeDisabled();
    await expect(newTurnBtn).toBeDisabled();

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 3');
    await expect(page.locator('#resultsBody')).toContainText('3');

    // Now the whole turn has genuinely settled - back to their real,
    // boundary-derived enabled state.
    await expect(goBackBtn).not.toBeDisabled();
    await expect(newTurnBtn).not.toBeDisabled();
  });

  // Regression guard: .badge-disabled used to be applied straight to
  // #configTriggerBadge itself, and CSS opacity/filter both composite the
  // WHOLE rendered subtree as one group once set on an ancestor - so the
  // grayed-out look during an in-flight query also desaturated/dimmed
  // #connDbDot, making a perfectly healthy (green) connection look grey,
  // as if its actual status had changed or become unknown. Fixed by
  // applying opacity/grayscale to the badge's other children individually
  // instead of the badge itself, explicitly excluding .status-dot (see
  // style.css's own comment) - this asserts the dot's real computed style
  // is untouched while the badge is disabled, and that the rest of the
  // badge (its text) still visibly dims exactly as before.
  test('the DB badge dot keeps its real connected/disconnected color while the badge is grayed out for an in-flight query', async ({ page }) => {
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

    const dot = page.locator('#connDbDot');
    const nameText = page.locator('#connDbName');
    const readDotStyle = () => dot.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { opacity: cs.opacity, filter: cs.filter, backgroundColor: cs.backgroundColor };
    });

    await expect(dot).toHaveClass(/connected/);
    // `.status-dot` animates background-color via `transition: var(--transition)`
    // (style.css) - right after the "connected" class lands, the color may
    // still be mid-transition from its previous (checking/muted) resting
    // color. Poll until two reads taken a beat apart agree, so the captured
    // baseline is the settled color rather than a transitional one - same
    // idiom onboarding.spec.js uses for #tourSpotlight's own CSS transition.
    let restingDotStyle;
    await expect(async () => {
      const first = await readDotStyle();
      await new Promise((r) => setTimeout(r, 50));
      const second = await readDotStyle();
      expect(second).toEqual(first);
      restingDotStyle = second;
    }).toPass({ timeout: 2000 });
    // Sanity check on the resting state - a real, non-grayscale color and
    // full opacity, before anything is in flight.
    expect(restingDotStyle.opacity).toBe('1');
    expect(restingDotStyle.filter).toBe('none');

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(page.locator('#configTriggerBadge')).toHaveClass(/badge-disabled/);
    // The dot's own computed style is completely unchanged from its
    // resting state above - same opacity, same "no filter", same actual
    // color - even while its parent badge carries badge-disabled.
    const dotStyleWhileDisabled = await readDotStyle();
    expect(dotStyleWhileDisabled).toEqual(restingDotStyle);
    // The badge's OTHER content still visibly dims, same as always - this
    // fix is scoped to the dot specifically, not a regression on the
    // "grayed out, not clickable" look for the rest of the badge.
    const nameOpacityWhileDisabled = await nameText.evaluate((el) => getComputedStyle(el).opacity);
    expect(Number(nameOpacityWhileDisabled)).toBeLessThan(1);

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 1');
  });

  test('directly entering and running SQL bypasses translate entirely', async ({ page }) => {
    await mockExecute(page, {
      results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }],
    });
    await gotoApp(page);

    await setSqlBox(page, 'SELECT 42 AS n;');
    await page.locator('#runBtn').click();

    const rows = page.locator('#resultsBody tr');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('42');
  });
});

/** Intercept POST /api/summarize-result - single-connection mode's own
 * post-execution results summarization endpoint (see this suite's own
 * describe block below, and translate_routes.py's docstring on that
 * route). Pass either `summary` (already carrying the server's own
 * "*** NO SQL ***" convention prefix, mirroring the real route's response
 * shape) for success, or `error` for a best-effort failure. */
async function mockSummarizeResult(page, { summary, error } = {}) {
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = error !== undefined ? { success: false, error } : { success: true, summary };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.describe('single-connection mode: post-execution results summarization', () => {
  test('a leading Summary tab with actionable insight appears after running translated SQL', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups are up sharply - worth investigating channel X.',
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups this week');
    await page.locator('#aiPrompt').press('Enter');
    // auto_sql_execute defaults to on (state_store.py), so translatePrompt()
    // itself runs the generated SQL - no separate #runBtn click needed (and
    // clicking it anyway would fire a second, redundant execute+summarize
    // round trip for this same turn).
    await expect(page.locator('.response-text')).toContainText('Signups are up sharply', { timeout: 10000 });

    // Two tabs now: the new leading "Summary" tab, plus the one real query
    // result - a single result alone would never show tab-nav at all (see
    // buildResultsTabsNav()'s own currentResultsList.length <= 1 guard), so
    // this count is itself proof the Summary tab was actually prepended.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(0)).toHaveClass(/active/);
    await expect(page.locator('.response-text')).toContainText('Signups are up sharply');
    // The server's internal "*** NO SQL ***" convention is stripped before
    // display, same as every other consumer of it - never shown verbatim.
    await expect(page.locator('.response-text')).not.toContainText('NO SQL');
  });

  // Regression guard for the gap this closes: /api/summarize-result's own
  // retry loop used to be entirely invisible to the client - one plain
  // JSON body, returned only once the whole retry loop had already
  // finished. It now streams NDJSON exactly like /api/translate already
  // does (readNdjsonStream() is shared by both) - this mirrors this
  // file's own "a translate response with a retry line ahead of the
  // terminal line..." test above, for this second endpoint.
  test('a summarize-result response with a retry line ahead of the terminal line still resolves to the summary, with no lingering retry banner', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson =
        JSON.stringify({ status: 'retrying', attempt: 2, maxAttempts: 5, delaySeconds: 1, rotatedKey: false }) + '\n' +
        JSON.stringify({ status: 'done', success: true, summary: '*** NO SQL *** Results Summary\n\nSignups are up sharply.' }) + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups this week');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('.response-text')).toContainText('Signups are up sharply', { timeout: 10000 });
    await expect(page.locator('#resultsRetryStatus')).toHaveClass(/hidden/);
  });

  test('no Summary tab, and no summarization call at all, for a direct SQL entry with no real question', async ({ page }) => {
    let summarizeCalled = false;
    await page.route('**/api/summarize-result', async (route) => {
      summarizeCalled = true;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** should never be requested' }),
      });
    });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    await gotoApp(page);

    // Typed directly into the SQL editor, never translated from a prompt -
    // client.js's own "[Direct SQL Execution]" convention (see executeSql()'s
    // own aiPrompt fallback) is what this feature is deliberately gated on.
    await setSqlBox(page, 'SELECT COUNT(*) AS n FROM signups;');
    await page.locator('#runBtn').click();

    const rows = page.locator('#resultsBody tr');
    await expect(rows).toHaveCount(1);
    // Single result, no Summary tab prepended - tab-nav never even shows.
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    expect(summarizeCalled).toBe(false);
  });

  test('an apology tab is shown, still leading, when summarization itself fails', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 1 }], rowCount: 1 }] });
    await mockSummarizeResult(page, { error: 'Unable to summarize results right now.' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('give me one');
    await page.locator('#aiPrompt').press('Enter');
    // auto_sql_execute defaults to on - see the previous test's comment.
    await expect(page.locator('.response-text')).toContainText('Unable to summarize results right now.', { timeout: 10000 });

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('Summary');
  });

  // Regression coverage for a bug: this summarization step used to be
  // skipped entirely whenever the SQL execution itself failed, unlike
  // "all databases" mode's own Phase C, which already summarizes over
  // whatever DID execute alongside any failures. Both single-connection
  // failure shapes (execute_routes.py's module docstring: the bare
  // {success:false, error} shape for a connect()/single-statement
  // failure, and the {results, failedStatement, ...} shape for a
  // multi-statement script that fails partway through) now also request
  // a summary - but deliberately WITHOUT stealing the active tab away
  // from the error the user needs to see (see client.js's
  // prependSingleModeSummaryTabPreservingActiveTab docstring) - so these
  // assert the Summary tab is present but NOT active/shown, rather than
  // repeating the "leading, active Summary tab" shape the success-path
  // tests above assert.
  test('a bare execution failure also gets a Summary tab, without stealing focus from the error', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThat table does not exist in this schema.',
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');

    // The error is what's shown, and stays shown - not silently replaced
    // by the summary the moment it arrives.
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(0)).not.toHaveClass(/active/);
    await expect(tabs.nth(1)).toHaveClass(/active/);
    await expect(tabs.nth(1)).toContainText('Error');
    // Still there once the summarization round trip actually settles.
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    // Clicking into the Summary tab shows the real summary text.
    await tabs.nth(0).click();
    await expect(page.locator('.response-text')).toContainText('That table does not exist in this schema.');
  });

  test('a multi-statement script that fails partway through also gets a Summary tab', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1; SELECT * FROM does_not_exist;' });
    await mockExecute(page, {
      results: [{ columns: ['?column?'], rows: [{ '?column?': 1 }], rowCount: 1 }],
      error: 'relation "does_not_exist" does not exist',
      failedStatement: 'SELECT * FROM does_not_exist;',
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThe first statement ran fine; the second referenced a missing table.',
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('run two statements, one of them bad');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    // Summary + the one succeeded statement + the one failed statement.
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(0)).not.toHaveClass(/active/);
    // The failed statement's own tab (last one) stays active - same "the
    // user needs to see this" jump renderResultsWithFailedStatement()
    // already did before Summary support was added.
    await expect(tabs.last()).toHaveClass(/active/);
    await expect(tabs.last()).toContainText('Error');
  });

  // Regression guard: it's not enough for the summarization call to fire
  // on a failure - the actual error text has to be WHAT gets sent, so the
  // model has something concrete to explain (see _SINGLE_SUMMARY_SYSTEM_
  // INSTRUCTION in translate_routes.py, which now explicitly asks it to
  // explain an error rather than just directly answering the question).
  // Covers both single-connection failure shapes: a bare execution failure
  // (one statement, one error, nothing succeeded) and a multi-statement
  // script where some statements succeeded before the failure.
  test('the real error text (not a generic message) is what gets sent to /api/summarize-result', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    let requestBody = null;
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      requestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Results Summary\n\nThat table is missing.' }),
      });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => requestBody).not.toBeNull();
    expect(requestBody.results).toEqual([{ error: 'relation "does_not_exist" does not exist' }]);
  });

  test('no summarization call at all for an execution failure with no real question (direct SQL entry)', async ({ page }) => {
    let summarizeCalled = false;
    await page.route('**/api/summarize-result', async (route) => {
      summarizeCalled = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: false }) });
    });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await setSqlBox(page, 'SELECT * FROM does_not_exist;');
    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    // No Summary tab, and no tab-nav at all - same "direct SQL entry never
    // summarizes" guard the success-path test above covers, now also
    // holding for a FAILED direct execution.
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    expect(summarizeCalled).toBe(false);
  });

  test('no summarization call for an execution failure that is really an auth requirement (401)', async ({ page }) => {
    let summarizeCalled = false;
    await page.route('**/api/summarize-result', async (route) => {
      summarizeCalled = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: false }) });
    });
    await mockTranslate(page, { sql: 'SELECT * FROM signups;' });
    await mockExecute(page, { error: 'Authentication required.', status: 401 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#resultsBody')).toContainText('Authentication required');
    // Same out-of-scope-for-the-Report-feature reasoning already applied
    // to the Report button/context for a 401 (see renderTableResult()'s
    // isError branch, result.notReportable) - nothing meaningful happened
    // here for the model to reason over either, so no summarization call.
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    expect(summarizeCalled).toBe(false);
  });

  test('navigating back to a turn replays its saved summary without a new network call', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    let summarizeCalls = 0;
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeCalls += 1;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Results Summary\n\nSignups are trending up.' }),
      });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups this week');
    await page.locator('#aiPrompt').press('Enter');
    // auto_sql_execute defaults to on - see the first test's comment above.
    await expect(page.locator('.response-text')).toContainText('Signups are trending up', { timeout: 10000 });
    expect(summarizeCalls).toBe(1);

    // A second, unrelated turn - somewhere to navigate back FROM. Its own
    // real question also gets its own (distinctly-worded) summary.
    await mockTranslate(page, { sql: 'SELECT 2 AS n;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 2 }], rowCount: 1 }] });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeCalls += 1;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Results Summary\n\nHere is two.' }),
      });
    });
    await page.locator('#aiPrompt').fill('give me two');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Here is two', { timeout: 10000 });
    expect(summarizeCalls).toBe(2);

    await page.locator('#goBackBtn').click();
    await expect(page.locator('.response-text')).toContainText('Signups are trending up');
    // Replayed from the saved turn (chatStore's own history) - no third
    // network call was made just to view it again.
    expect(summarizeCalls).toBe(2);
  });

  // Regression guard: summarizeResultForHistory()'s success branch used to
  // build its summarized copy from just {columns, rowCount, rows}, silently
  // dropping the result's own `.statement` - the exact SQL that produced it
  // - even though the error branch right above it already preserved that
  // field. A FRESH turn's own live results (never round-tripped through
  // that function) always had it, so buildResultsTabsNav()'s hover title
  // (see its own `res.query || res.sql || res.statement` fallback) only
  // ever went missing once a turn was persisted to history and restored -
  // i.e. exactly what stepping back to an earlier turn does here.
  test('stepping back to an earlier turn still shows its query text on tab hover', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1, statement: 'SELECT COUNT(*) AS n FROM signups' }] });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Results Summary\n\nSignups are trending up.' }),
      });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups are trending up', { timeout: 10000 });

    // Confirmed still correct on a LIVE (never-persisted) turn's own tab.
    const liveTab = page.locator('#resultsTabsNav .result-tab-btn', { hasText: 'Query 1' });
    await expect(liveTab).toHaveAttribute('title', 'SELECT COUNT(*) AS n FROM signups');

    // A second, unrelated turn - somewhere to navigate back FROM.
    await mockTranslate(page, { sql: 'SELECT 2 AS n;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 2 }], rowCount: 1, statement: 'SELECT 2 AS n' }] });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Results Summary\n\nHere is two.' }),
      });
    });
    await page.locator('#aiPrompt').fill('give me two');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Here is two', { timeout: 10000 });

    // Stepping back re-renders the FIRST turn's tab strip from its history
    // entry, not from a fresh /api/execute response - this is the path that
    // used to lose the tooltip.
    await page.locator('#goBackBtn').click();
    await expect(page.locator('.response-text')).toContainText('Signups are trending up');
    const restoredTab = page.locator('#resultsTabsNav .result-tab-btn', { hasText: 'Query 1' });
    await expect(restoredTab).toHaveAttribute('title', 'SELECT COUNT(*) AS n FROM signups');
  });

  // Regression guard for "Turn History Handling in Datalect" Gap 1: a turn
  // that concludes with an error still gets added to chatStore's history,
  // same as a successful turn - previously executeSql()'s entire failure
  // branch never called chatStore.pushTurn()/mutated the pending entry at
  // all, so a failed turn simply vanished from history the instant the
  // user asked anything else. Proven here by inspecting what the client
  // actually sends as `history` on the NEXT /api/translate call, rather
  // than via the UI (back/forward navigation only proves the turn is
  // reachable again, not that its error reached the model).
  test('a multi-statement script that fails partway through is added to history, with its error visible to the next question', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1; SELECT * FROM does_not_exist;' });
    await mockExecute(page, {
      results: [{ columns: ['?column?'], rows: [{ '?column?': 1 }], rowCount: 1 }],
      error: 'relation "does_not_exist" does not exist',
      failedStatement: 'SELECT * FROM does_not_exist;',
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThe first statement ran fine; the second referenced a missing table.',
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('run two statements, one of them bad');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 2;' }),
      });
    });
    await page.locator('#aiPrompt').fill('what should we do about that error');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    expect(Array.isArray(secondRequestBody.history)).toBe(true);
    const failedTurn = secondRequestBody.history.find(
      (m) => m.role === 'model' && Array.isArray(m.results) && m.results.some((r) => r.error)
    );
    expect(failedTurn).toBeTruthy();
    // isError: true (not just the error text) must survive into history too
    // - summarizeResultForHistory()'s error branch preserves it so that
    // stepping back to this turn later still renders a real error box
    // (renderTableResult()'s isError branch) instead of silently falling
    // through to "No dataset returned".
    expect(failedTurn.results).toContainEqual({ error: 'relation "does_not_exist" does not exist', isError: true });
    // The Summary tab's own explanation (Phase C's single-connection
    // equivalent) is preserved on the turn too - see Gap 2's fix on the
    // server side (build_gemini_history_contents et al. now append a
    // turn's stored `summary`).
    expect(failedTurn.summary).toContain('referenced a missing table');
  });

  test('a bare execution failure (connect() error) is added to history, with its error visible to the next question', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThat table is missing.',
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 2;' }),
      });
    });
    await page.locator('#aiPrompt').fill('what went wrong there');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    const failedTurn = secondRequestBody.history.find(
      (m) => m.role === 'model' && Array.isArray(m.results) && m.results.some((r) => r.error)
    );
    expect(failedTurn).toBeTruthy();
    // isError: true must survive into history too - see the identical
    // assertion/comment in the multi-statement-failure test above.
    expect(failedTurn.results).toContainEqual({ error: 'relation "does_not_exist" does not exist', isError: true });
    expect(failedTurn.summary).toContain('That table is missing.');
  });
});
