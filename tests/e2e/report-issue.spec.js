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
