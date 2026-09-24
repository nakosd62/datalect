// tests/e2e/show-sql-toggle.spec.js
//
// "Show SQL" (Preferences > Automatic SQL Execution section, alongside
// auto-execute) - whether #editorPaneSql (the SQL box + its Execute/
// report-issue controls) is shown at all. Client-side only (localStorage,
// like theme used to be before it also became server-persisted - see
// SHOW_SQL_STORAGE_KEY in client.js), unlike auto_sql_execute, so this
// suite proves the OPPOSITE of preferences-modal.spec.js's "survives a
// reload even with localStorage cleared" theme/auto-execute tests: Show
// SQL resets to its default (hidden) once localStorage is cleared, since
// there's no server fallback for it at all.
//
// Against the REAL Flask server, same reasoning as preferences-modal.spec.js -
// nothing here needs mocking beyond what the default `test` fixture installs,
// except the one test that actually submits a translation to prove the app
// still works end-to-end with the SQL box hidden.

const { test, expect, gotoApp, mockTranslate } = require('./fixtures');

async function openPreferencesModal(page) {
  await page.locator('#prefsBtn').click();
  await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
}

test.describe('show SQL preference', () => {
  test('is hidden by default, with its checkbox unchecked in Preferences', async ({ page }) => {
    await gotoApp(page);
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('#showSqlCheckbox')).not.toBeChecked();
    await expect(page.locator('#showSqlCheckbox')).not.toBeDisabled();
  });

  test('checking it and saving shows the SQL box/divider and shrinks the NL prompt box back down', async ({ page }) => {
    await gotoApp(page);
    const nlWidthBefore = (await page.locator('#editorPaneNl').boundingBox()).width;

    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();

    await expect(page.locator('#editorPaneSql')).not.toHaveClass(/hidden/);
    await expect(page.locator('#editorPanesResizer')).not.toHaveClass(/hidden/);

    const nlWidthAfter = (await page.locator('#editorPaneNl').boundingBox()).width;
    expect(nlWidthAfter).toBeLessThan(nlWidthBefore / 1.5);

    // Clean up so this doesn't leak into other tests via shared localStorage
    // within the same worker/context.
    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').uncheck();
    await page.locator('#preferencesSaveBtn').click();
  });

  test('persists across a reload (localStorage), unlike a one-off in-memory toggle', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();

    await gotoApp(page);
    await expect(page.locator('#editorPaneSql')).not.toHaveClass(/hidden/);
    await openPreferencesModal(page);
    await expect(page.locator('#showSqlCheckbox')).toBeChecked();

    // Clean up.
    await page.locator('#showSqlCheckbox').uncheck();
    await page.locator('#preferencesSaveBtn').click();
  });

  test('resets to hidden once localStorage is cleared - no server-side fallback for this one, unlike theme/auto-execute', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#editorPaneSql')).not.toHaveClass(/hidden/);

    await page.evaluate(() => window.localStorage.removeItem('datalectShowSql'));
    await gotoApp(page);
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);
  });

  test('is never sent to the server - the /api/config save payload has no trace of it', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').check();

    const configRequest = page.waitForRequest(
      (req) => req.url().includes('/api/config') && req.method() === 'POST'
    );
    await page.locator('#preferencesSaveBtn').click();
    const req = await configRequest;
    const body = req.postData() || '';
    expect(body).not.toContain('show_sql');
    expect(body).not.toContain('showSql');

    // Clean up.
    await openPreferencesModal(page);
    await page.locator('#showSqlCheckbox').uncheck();
    await page.locator('#preferencesSaveBtn').click();
  });

  test('a translation still works end-to-end with the SQL box hidden (the default)', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM users LIMIT 10;' });
    await gotoApp(page);
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);

    await page.locator('#aiPrompt').fill('show me the first 10 users');
    await page.locator('#aiPrompt').press('Enter');

    // The SQL box stays hidden throughout - a translation completing must
    // not silently reveal it again.
    await page.waitForTimeout(300);
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);
  });
});

test.describe('show SQL / auto-execute linked rule', () => {
  test('unchecking auto-execute immediately force-checks and locks Show SQL, before Save', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    // Start from a known state: auto-execute on, Show SQL off (its default -
    // also exercises that turning auto-execute off overrides an off Show
    // SQL regardless of whether that's the default or an explicit choice).
    const autoExecCheckbox = page.locator('#autoSqlExecuteCheckbox');
    if (!(await autoExecCheckbox.isChecked())) await autoExecCheckbox.check();
    await page.locator('#showSqlCheckbox').uncheck();

    await autoExecCheckbox.uncheck();
    await expect(page.locator('#showSqlCheckbox')).toBeChecked();
    await expect(page.locator('#showSqlCheckbox')).toBeDisabled();
    await expect(page.locator('#showSqlLockedNote')).toBeVisible();

    // Re-checking auto-execute frees Show SQL again (but doesn't force it
    // back off - the user's last explicit choice, now checked, is left
    // alone since there's no rule requiring SQL be HIDDEN).
    await autoExecCheckbox.check();
    await expect(page.locator('#showSqlCheckbox')).not.toBeDisabled();
    await expect(page.locator('#showSqlLockedNote')).not.toBeVisible();

    await page.locator('#preferencesModalCloseBtn').click();
  });

  test('saving with auto-execute off persists Show SQL as visible even if it was unchecked moments before', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await page.locator('#autoSqlExecuteCheckbox').uncheck();
    // The checkbox is now disabled/force-checked - saving in this state
    // must not somehow persist a hidden SQL box despite the lock.
    await page.locator('#preferencesSaveBtn').click();

    await expect(page.locator('#editorPaneSql')).not.toHaveClass(/hidden/);
    await openPreferencesModal(page);
    await expect(page.locator('#showSqlCheckbox')).toBeChecked();

    // Clean up: restore auto-execute.
    await page.locator('#autoSqlExecuteCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();
  });

  test('opening the dialog with auto-execute already off (from a previous save) shows Show SQL pre-locked', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await page.locator('#autoSqlExecuteCheckbox').uncheck();
    await page.locator('#preferencesSaveBtn').click();

    // Fresh open, no interaction yet - loadPreferencesIntoUI() itself must
    // apply the lock, not just the live 'change' handler.
    await openPreferencesModal(page);
    await expect(page.locator('#showSqlCheckbox')).toBeChecked();
    await expect(page.locator('#showSqlCheckbox')).toBeDisabled();

    // Clean up.
    await page.locator('#autoSqlExecuteCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();
  });
});
