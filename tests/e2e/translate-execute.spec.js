// tests/e2e/translate-execute.spec.js
//
// The core "ask a question -> get SQL -> run it -> see results" flow, with
// /api/translate and /api/execute intercepted in-browser (see fixtures.js)
// so this never needs a real Gemini key or a real target database. Every
// other request (page load, /api/config) hits the real Flask server.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

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
    // run. Scoped to #resultsTabsNav specifically: the unrelated History
    // modal's own internal tab switcher (#tabBtnTranslations/
    // #tabBtnStatistics in index.html) reuses the same .result-tab-btn
    // class name, so an unscoped page-wide locator would overcount.
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

    // Sanity check on the resting state, before anything is in flight.
    await expect(dbBadge).not.toHaveClass(/badge-disabled/);
    await expect(modelBadge).not.toHaveClass(/badge-disabled/);

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(dbBadge).toHaveClass(/badge-disabled/);
    await expect(modelBadge).toHaveClass(/badge-disabled/);
    // Not just visually grayed out - actually inert. Clicking either while
    // disabled must not open its modal.
    await dbBadge.click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await modelBadge.click();
    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);
    // An unrelated icon (history) stays fully enabled the whole time - its
    // popup doesn't touch the active connection/model.
    await expect(historyBtn).not.toBeDisabled();

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 1');

    await expect(dbBadge).not.toHaveClass(/badge-disabled/);
    await expect(modelBadge).not.toHaveClass(/badge-disabled/);
  });

  test('directly entering and running SQL bypasses translate entirely', async ({ page }) => {
    await mockExecute(page, {
      results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }],
    });
    await gotoApp(page);

    await page.evaluate(() => {
      const wrapper = document.querySelector('.CodeMirror');
      if (wrapper && wrapper.CodeMirror) {
        wrapper.CodeMirror.setValue('SELECT 42 AS n;');
      } else {
        const textarea = document.getElementById('sqlQuery');
        if (textarea) textarea.value = 'SELECT 42 AS n;';
      }
    });
    await page.locator('#runBtn').click();

    const rows = page.locator('#resultsBody tr');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('42');
  });
});
