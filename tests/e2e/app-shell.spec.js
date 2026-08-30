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
    await expect(page.locator('#runBtn')).toBeVisible();
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

  test('example prompt chips are present and fill the prompt box', async ({ page }) => {
    await gotoApp(page);
    const chips = page.locator('.example-chip');
    const count = await chips.count();
    test.skip(count === 0, 'no example chips configured');
    // Clicking a chip both fills #aiPrompt AND immediately fires a real
    // translate call - avoid actually clicking here (that would hit the
    // real, unmocked /api/translate) and just confirm the chip carries a
    // usable prompt.
    const firstPrompt = await chips.first().getAttribute('data-prompt');
    expect(firstPrompt).toBeTruthy();
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
