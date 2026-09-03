// tests/e2e/report-issue.spec.js
//
// "Report Error" (see server/report_routes.py's module docstring): a small
// Report button rendered INLINE, directly beside the "Execution Error"
// title of whichever tab is actually showing a raw DB execution error -
// lets the user review exactly what will be emailed in #reportIssueModal,
// then send it. By explicit request, a "Report Wrong Result" button (for a
// successful response or a plain-text reply, as opposed to an error) is
// never shown at all any more - see the tests below asserting its absence
// on those tabs, which is a regression guard for that removal, not
// leftover naming. It's a `.report-issue-inline-btn`-classed element
// created fresh by whichever render pass drew the reportable content, not
// a persistent, always-in-DOM element toggled by a `.hidden` class - so
// `toBeHidden()`/`toBeVisible()` below are asserting on whether the button
// exists in the DOM at all (Playwright's `toBeHidden()` already treats "no
// matching element" as hidden), not on a CSS class.
//
// GET /api/config is intercepted just enough to force
// 'issue_reporting_enabled' to a known value (real local-dev servers never
// have ISSUE_REPORT_* env vars configured - see playwright.config.js's
// webServer block) - everything else in that response is the real server's
// own, via route.fetch(). POST /api/report-issue is always mocked (no
// spec here should ever depend on - or risk - a real SMTP send).

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

async function mockIssueReportingEnabled(page, enabled) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    json.issue_reporting_enabled = enabled;
    await route.fulfill({ response, json });
  });
}

/** Intercepts POST /api/report-issue. Returns an object whose `.lastBody`
 * is set to the most recently POSTed JSON body, so a test can assert on
 * exactly what the client sent. */
function mockReportIssue(page, { success = true, error, status } = {}) {
  const state = { lastBody: null };
  page.route('**/api/report-issue', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    state.lastBody = route.request().postDataJSON();
    if (success) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    } else {
      await route.fulfill({
        status: status || 500,
        contentType: 'application/json',
        body: JSON.stringify({ success: false, error: error || 'Failed to send report.' }),
      });
    }
  });
  return state;
}

test.describe('report an issue', () => {
  test('the Report button stays hidden when the feature is not configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, false);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsBody')).toContainText('Execution Error');
    await expect(page.locator('.report-issue-inline-btn')).toBeHidden();
  });

  test('an execution error shows a Report Error button that previews the right content', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    const reportBtn = page.locator('.report-issue-inline-btn');
    await expect(reportBtn).toBeVisible();
    await expect(reportBtn).toContainText('Report Error');

    await reportBtn.click();
    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueModalTitle')).toHaveText('Report Error');
    const preview = page.locator('#reportIssuePreview');
    await expect(preview).toContainText('Category: Execution Error');
    await expect(preview).toContainText('does_not_exist');
    await expect(preview).toContainText('relation "does_not_exist" does not exist');
  });

  test('sending a report posts the reviewed content and closes the modal', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await page.locator('.report-issue-inline-btn').click();
    await page.locator('#reportIssueDetails').fill('This should not have failed.');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    expect(reportState.lastBody).toBeTruthy();
    expect(reportState.lastBody.category).toBe('error');
    expect(reportState.lastBody.details).toBe('This should not have failed.');
    expect(reportState.lastBody.content).toContain('does_not_exist');
    expect(reportState.lastBody.sql).toContain('does_not_exist');
  });

  test('a failed send shows an inline error and keeps the modal open', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page, { success: false, error: 'SMTP connection failed.' });
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await page.locator('.report-issue-inline-btn').click();
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueStatus')).toContainText('SMTP connection failed.');
  });

  test('the Cancel button closes the modal without sending anything', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await page.locator('.report-issue-inline-btn').click();
    await page.locator('#reportIssueCancelBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    expect(reportState.lastBody).toBeNull();
  });

  test('a successful result never shows a Report button', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();

    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
    await expect(page.locator('.report-issue-inline-btn')).toBeHidden();
  });

  test('a plain-text (no-SQL) reply never shows a Report button', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await mockTranslate(page, { sql: '*** NO SQL *** Paris is the capital of France.' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('what is the capital of france');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#resultsBody')).toContainText('Paris is the capital of France.');
    await expect(page.locator('.report-issue-inline-btn')).toBeHidden();
  });

  test('a translation error does not show a Report button', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await mockTranslate(page, { error: 'The model could not understand that request.', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('do something impossible');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#resultsBody')).toContainText('Translation Error');
    await expect(page.locator('.report-issue-inline-btn')).toBeHidden();
  });
});

// "Send Feedback" (see report_routes.py's module docstring on the
// 'feedback' category, and client.js's REPORT_CATEGORY_CONFIG.feedback):
// a persistent button in the app header, next to the Doc/#helpBtn button
// (collapsing into the triple-dot more-menu on narrow screens - see
// more-menu.spec.js for that variant) - unlike the inline Report buttons
// above, which are recreated per render - that opens the SAME
// #reportIssueModal, but with no result/error to preview (the preview
// section is hidden entirely) and the details textarea as the whole,
// required message rather than an optional add-on. Replaces what used to
// be a plain mailto: link in help.html, which depended on the visitor
// having a configured mail client.
test.describe('send feedback', () => {
  test('the Send Feedback button stays hidden when the feature is not configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, false);
    await gotoApp(page);

    await expect(page.locator('#sendFeedbackBtn')).toBeHidden();
  });

  test('opens the report modal in feedback mode, with no result preview', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await gotoApp(page);

    await expect(page.locator('#sendFeedbackBtn')).toBeVisible();
    await page.locator('#sendFeedbackBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueModalTitle')).toHaveText('Send Feedback');
    await expect(page.locator('#reportIssuePreviewSection')).toBeHidden();
    await expect(page.locator('#reportIssueDetailsLabel')).toHaveText('Your feedback');
    await expect(page.locator('#reportIssueSendBtn')).toHaveText('Send Feedback');
  });

  test('sending without any text shows a validation message and does not call the API', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#sendFeedbackBtn').click();
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueStatus')).toContainText('Please enter your feedback');
    expect(reportState.lastBody).toBeNull();
  });

  test('sending feedback posts just the category and details, and closes the modal', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#sendFeedbackBtn').click();
    await page.locator('#reportIssueDetails').fill('It would be great to have dark mode charts.');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    expect(reportState.lastBody).toEqual({
      category: 'feedback',
      details: 'It would be great to have dark mode charts.',
    });
  });
});

// "Report wrong SQL" (see report_routes.py's module docstring on the
// 'wrong_sql' category, and client.js's REPORT_CATEGORY_CONFIG.wrong_sql):
// the thumbs-down button (#reportSqlBtn) next to the SQL box's own Execute
// button - independent of whether that SQL has ever been run. Unlike every
// other category, the preview is an editable <textarea>
// (#reportIssuePreviewEditable, not the usual read-only #reportIssuePreview
// <pre>), preloaded with the current NL prompt + SQL but freely rewritable
// before Send - see setSqlBox() below for why setting the SQL box's value
// needs a page.evaluate() branch (CodeMirror may or may not have loaded).
test.describe('report wrong SQL', () => {
  async function setSqlBox(page, sql) {
    await page.evaluate((value) => {
      const wrapper = document.querySelector('.CodeMirror');
      if (wrapper && wrapper.CodeMirror) {
        wrapper.CodeMirror.setValue(value);
      } else {
        const textarea = document.getElementById('sqlQuery');
        if (textarea) textarea.value = value;
      }
    }, sql);
  }

  test('the thumbs-down button stays hidden when the feature is not configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, false);
    await gotoApp(page);

    await expect(page.locator('#reportSqlBtn')).toBeHidden();
  });

  test('opens the report modal in wrong_sql mode, preloaded with the prompt and SQL, and editable', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('How many orders were placed last week?');
    await setSqlBox(page, 'SELECT * FROM ordrs;');

    await expect(page.locator('#reportSqlBtn')).toBeVisible();
    await page.locator('#reportSqlBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueModalTitle')).toHaveText('Report Wrong SQL');
    // The usual read-only preview stays out of the DOM's visible flow -
    // this category's own editable textarea takes its place.
    await expect(page.locator('#reportIssuePreview')).toBeHidden();
    const editable = page.locator('#reportIssuePreviewEditable');
    await expect(editable).toBeVisible();
    await expect(editable).toHaveValue(/How many orders were placed last week\?/);
    await expect(editable).toHaveValue(/SELECT \* FROM ordrs;/);
    await expect(page.locator('#reportIssueDetailsLabel')).toHaveText('Additional comments (optional)');
    await expect(page.locator('#reportIssueSendBtn')).toHaveText('Report Wrong SQL');
  });

  test('the user can rewrite the preloaded text before sending, and the edited text is what gets sent', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('How many orders were placed last week?');
    await setSqlBox(page, 'SELECT * FROM ordrs;');
    await page.locator('#reportSqlBtn').click();

    await page.locator('#reportIssuePreviewEditable').fill('SQL:\nSELECT * FROM orders; -- fixed typo');
    await page.locator('#reportIssueDetails').fill('The table name was misspelled.');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    expect(reportState.lastBody).toBeTruthy();
    expect(reportState.lastBody.category).toBe('wrong_sql');
    expect(reportState.lastBody.content).toBe('SQL:\nSELECT * FROM orders; -- fixed typo');
    expect(reportState.lastBody.details).toBe('The table name was misspelled.');
  });

  test('sending with nothing in the preview and no comment shows a validation message and does not call the API', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    const reportState = mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#reportSqlBtn').click();
    await page.locator('#reportIssuePreviewEditable').fill('');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueStatus')).toContainText('Please include the SQL you want to report');
    expect(reportState.lastBody).toBeNull();
  });

  test('the button is disabled while a query is in flight, and re-enabled once it settles', async ({ page }) => {
    // Mirrors translate-execute.spec.js's own badge-disabling test - see
    // setButtonsDisabled() in client.js, which now disables #reportSqlBtn
    // alongside #runBtn/#micBtn while a translate/execute call is running,
    // same as "most other buttons" (explicit request).
    await mockIssueReportingEnabled(page, true);
    let resolveTranslate;
    const translateStarted = new Promise((resolve) => { resolveTranslate = resolve; });
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      resolveTranslate();
      await new Promise((r) => setTimeout(r, 1000));
      await route.fulfill({
        status: 200, contentType: 'application/x-ndjson',
        body: JSON.stringify({ status: 'done', success: true, sql: 'SELECT 1;' }) + '\n',
      });
    });
    await gotoApp(page);

    const reportSqlBtn = page.locator('#reportSqlBtn');
    await expect(reportSqlBtn).toBeVisible();
    await expect(reportSqlBtn).not.toBeDisabled();

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(reportSqlBtn).toBeDisabled();

    await expect.poll(() => page.evaluate(() => {
      const wrapper = document.querySelector('.CodeMirror');
      return wrapper && wrapper.CodeMirror ? wrapper.CodeMirror.getValue() : document.getElementById('sqlQuery').value;
    }), { timeout: 5000 }).toContain('SELECT 1');

    await expect(reportSqlBtn).not.toBeDisabled();
  });
});
