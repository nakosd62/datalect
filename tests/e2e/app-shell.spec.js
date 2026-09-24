// tests/e2e/app-shell.spec.js
//
// The app loads against the real (local-dev, no-auth) Flask server and the
// core UI chrome is present and interactive. No network mocking needed -
// this only exercises the real index page + real /api/config.

const { test, expect, gotoApp } = require('./fixtures');

test.describe('app shell', () => {
  test('loads and shows the main prompt/query UI', async ({ page }) => {
    await gotoApp(page);

    await expect(page).toHaveTitle(/Datalect/);
    await expect(page.locator('#aiPrompt')).toBeVisible();
    // The SQL box (and its #runBtn) is hidden by default now - see "Show
    // SQL" in client.js/SHOW_SQL_STORAGE_KEY - so it's asserted absent here
    // rather than visible; show-sql-toggle.spec.js covers turning it back on.
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);
    await expect(page.locator('#configTriggerBadge')).toBeVisible();
    await expect(page.locator('#helpBtn')).toBeVisible();
    await expect(page.locator('#historyBtn')).toBeVisible();
  });

  test('connection badge reflects the real default local-dev database', async ({ page }) => {
    await gotoApp(page);
    // Local dev with no DATABASE_PRESETS configured falls back to a single
    // synthetic "Default DB" preset - see app_config.py.
    await expect(page.locator('#connDbName')).not.toHaveText('');
  });

  // Regression guard for a removed feature: this app used to show a
  // dismissible "Sample prompts" chip row above the prompt box (#examplePrompts,
  // .example-chip buttons carrying a data-prompt), plus a "Restore quick
  // prompts" button in the Help modal to bring that row back once
  // dismissed - both were removed entirely (product decision: the section
  // was judged not worth its screen space). This used to be
  // 'example prompt chips are present and fill the prompt box', which
  // exercised that feature directly; it's rewritten here to instead guard
  // against either piece quietly coming back, rather than just skipping
  // itself forever now that the feature is gone (as it silently did once
  // .example-chip's count dropped to 0 - see this test's own git history).
  test('the removed sample-prompts section stays removed, from both the prompt box and the Help modal', async ({ page }) => {
    await gotoApp(page);
    await expect(page.locator('#examplePrompts')).toHaveCount(0);
    await expect(page.locator('.example-chip')).toHaveCount(0);

    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);
    await expect(page.locator('#restoreQuickPromptsBtn')).toHaveCount(0);
    // #replayTourBtn is a distinct, still-live feature - only the dedicated
    // sample-prompts-restore button (a separate id, and never reused for
    // anything else) is asserted gone above. It's now an inline link inside
    // help.html's own fetched text rather than a static button in
    // index.html (see openHelpModal()/wireHelpModalLinks() in client.js),
    // so this waits on the async fetch/render like any other doc content -
    // toBeVisible()'s own polling handles that with no extra code needed.
    await expect(page.locator('#replayTourBtn')).toBeVisible();
  });

  test('help modal opens and closes', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);
    await page.locator('#helpModalCloseBtn').click();
    await expect(page.locator('#helpModal')).toHaveClass(/hidden/);
  });

  test('history modal opens and closes', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#historyBtn').click();
    await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);
    await page.locator('#historyModalCloseBtn').click();
    await expect(page.locator('#historyModal')).toHaveClass(/hidden/);
  });
});
