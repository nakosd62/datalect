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

  test('a conversational (no-SQL) reply is rendered as text, not a query', async ({ page }) => {
    await mockTranslate(page, { sql: '*** NO SQL *** I can only answer questions about your data.' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('what is the meaning of life');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('.response-text')).toContainText('I can only answer questions about your data.');
    await expect.poll(() => currentSql(page)).toBe('');
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
