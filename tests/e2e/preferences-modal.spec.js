// tests/e2e/preferences-modal.spec.js
//
// The Preferences modal (theme + auto-execute-SQL), against the REAL Flask
// server and REAL SqliteStateStore - same reasoning as
// model-selection.spec.js: saving preferences only ever persists plain
// values (a boolean, a "dark"/"light" string) via /api/config, never makes
// a live LLM/DB call, so nothing here needs mocking beyond what the default
// `test` fixture already installs.
//
// Theme is persisted BOTH server-side (session, or user if logged in - see
// state_store.py's "theme" session field) and in localStorage (applied to
// <html data-theme> before first paint by an inline script in index.html's
// <head>, since that script only ever reads localStorage - there's no time
// for a network round trip before first paint). Most tests below exercise
// the localStorage/immediate-DOM path (clearing localStorage isn't needed
// to prove that path works); the dedicated server-persistence test further
// down clears localStorage before reloading to prove the value survives
// from the server alone.

const { test, expect, gotoApp } = require('./fixtures');

async function openPreferencesModal(page) {
  await page.locator('#prefsBtn').click();
  await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
}

test.describe('preferences modal', () => {
  test('opens via the header gear button showing Dark selected by default, and closes', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await expect(page.locator('#themeOptionDark')).toBeChecked();
    await expect(page.locator('#themeOptionLight')).not.toBeChecked();
    await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'light');

    await page.locator('#preferencesModalCloseBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);
  });

  test('the auto-execute checkbox lives here now, not in the DB connection modal', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await expect(page.locator('#autoSqlExecuteCheckbox')).toBeVisible();
    await page.locator('#preferencesModalCloseBtn').click();

    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#configModal')).not.toHaveClass(/hidden/);
    await expect(page.locator('#configModal #autoSqlExecuteCheckbox')).toHaveCount(0);
  });

  test('selecting Light and saving applies data-theme and persists across a reload', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await page.locator('#themeOptionLight').check();
    await page.locator('#preferencesSaveBtn').click();

    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Persisted client-side (localStorage) - survives a fresh page load,
    // applied before first paint (no flash of the wrong theme to catch
    // here, but the end state after load is what matters for the test).
    await gotoApp(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Reopening the modal reflects the now-active theme.
    await openPreferencesModal(page);
    await expect(page.locator('#themeOptionLight')).toBeChecked();
    await expect(page.locator('#themeOptionDark')).not.toBeChecked();

    // Switch back to Dark so this test doesn't leak state into others via
    // shared browser storage within the same worker/context.
    await page.locator('#themeOptionDark').check();
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'light');
  });

  test('the saved theme survives a reload even with localStorage cleared, proving server-side persistence', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await page.locator('#themeOptionLight').check();
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Wipe the client-side copy so the only thing that could reapply
    // "light" on the next load is fetchBackendConfig()'s own session read
    // (see state_store.py's "theme" field) - the inline flash-prevention
    // script in index.html's <head> has nothing to read at first paint.
    await page.evaluate(() => window.localStorage.removeItem('datalectTheme'));

    await gotoApp(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Clean up: restore Dark (and let it re-sync localStorage) so this
    // test doesn't leak state into others via the shared server-side
    // session within the same worker/context.
    await openPreferencesModal(page);
    await page.locator('#themeOptionDark').check();
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('html')).not.toHaveAttribute('data-theme', 'light');
  });

  test('toggling auto-execute and saving persists it server-side across a reload', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const checkbox = page.locator('#autoSqlExecuteCheckbox');
    const initiallyChecked = await checkbox.isChecked();
    if (initiallyChecked) {
      await checkbox.uncheck();
    } else {
      await checkbox.check();
    }
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // Persisted server-side (state_store.py via /api/config), same
    // pattern as the model-selection badge - survives a fresh page load.
    await gotoApp(page);
    await openPreferencesModal(page);
    await expect(page.locator('#autoSqlExecuteCheckbox')).toBeChecked({ checked: !initiallyChecked });

    // Restore the original value so this test doesn't leak state.
    if (initiallyChecked) {
      await checkbox.check();
    } else {
      await checkbox.uncheck();
    }
    await page.locator('#preferencesSaveBtn').click();
  });

  test('saving the theme does not disturb the already-saved auto-execute value, and vice versa', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    // Turn auto-execute off and save.
    await page.locator('#autoSqlExecuteCheckbox').uncheck();
    await page.locator('#preferencesSaveBtn').click();

    // Now only touch the theme and save.
    await openPreferencesModal(page);
    await page.locator('#themeOptionLight').check();
    await page.locator('#preferencesSaveBtn').click();

    // Auto-execute should still be off.
    await openPreferencesModal(page);
    await expect(page.locator('#autoSqlExecuteCheckbox')).not.toBeChecked();

    // Clean up: restore both to their defaults.
    await page.locator('#autoSqlExecuteCheckbox').check();
    await page.locator('#themeOptionDark').check();
    await page.locator('#preferencesSaveBtn').click();
  });

  test('on a narrow (mobile) header, the gear button is hidden and the more-menu item opens the same modal', async ({ page }) => {
    await page.setViewportSize({ width: 420, height: 800 });
    await gotoApp(page);

    await expect(page.locator('#prefsBtn')).not.toBeVisible();

    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuDropdown')).not.toHaveClass(/hidden/);
    await expect(page.locator('#moreMenuPrefsBtn')).toHaveText('Preferences');

    await page.locator('#moreMenuPrefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
    // The more-menu dropdown itself closes once an item forwards the click.
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);
  });
});
