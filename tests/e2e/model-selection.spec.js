// tests/e2e/model-selection.spec.js
//
// The model-selection badge/pick-list, against the REAL Flask server and
// REAL SqliteStateStore - no mocking (same reasoning as config-modal.spec.js:
// saving a model choice only ever validates and persists a string against
// translate_routes.py's LlmProvider registry, never makes a live LLM call).
// /api/translate itself is still mocked by the default `test` fixture, but
// this suite never exercises it - only /api/config's GET/POST.
//
// The picker is an in-place dropdown (#modelPickList, anchored under
// #modelTriggerBadge via #modelPickerWrapper) rather than a modal dialog -
// clicking a #modelPickList option selects AND saves immediately, with no
// separate "Save Changes" step.

const { test, expect, gotoApp } = require('./fixtures');

/** Mirrors translate-execute.spec.js's own currentSql()/normalizedSql() -
 * duplicated locally (this file has no shared-helpers import for it)
 * rather than reaching across spec files, matching this suite's existing
 * no-shared-helpers convention (see more-menu.spec.js's own local
 * mockIssueReportingEnabled() for the same reasoning). */
function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

async function normalizedSql(page) {
  return (await currentSql(page) || '').replace(/\s+/g, ' ').trim();
}

async function openModelPickList(page) {
  await page.locator('#modelTriggerBadge').click();
  await expect(page.locator('#modelPickList')).not.toHaveClass(/hidden/);
}

test.describe('model selection pick list', () => {
  test('badge shows the default model on load', async ({ page }) => {
    await gotoApp(page);
    // playwright.config.js's webServer deliberately skips the real dev
    // .env (YDYL_SKIP_DOTENV) and sets no GOOGLE_MODELS override, so this
    // reflects the app's own hardcoded fallback default -
    // GeminiProvider.fallback_models.
    await expect(page.locator('#modelBadgeName')).toHaveText('gemini-3.6-flash');
  });

  test('opens showing one group heading per provider, and closes on an outside click', async ({ page }) => {
    await gotoApp(page);
    await openModelPickList(page);

    const pickList = page.locator('#modelPickList');
    await expect(pickList.locator('.model-pick-group-heading')).toHaveText(['Google', 'Anthropic', 'OpenAI']);
    await expect(pickList.locator('.model-pick-option').first()).toBeVisible();

    // The currently-active model's option is the one marked selected.
    await expect(pickList.locator('.model-pick-option.selected')).toHaveAttribute('data-value', 'google::gemini-3.6-flash');

    // No separate close button any more - a real pick list dismisses like
    // any other header dropdown (auth avatar menu, the more-menu), via a
    // click outside it.
    await page.locator('body').click({ position: { x: 10, y: 10 } });
    await expect(pickList).toHaveClass(/hidden/);
  });

  test('clicking a different option selects and saves it immediately, closing the list with no separate save step', async ({ page }) => {
    await gotoApp(page);
    await openModelPickList(page);

    await page.locator('#modelPickList .model-pick-option[data-value="anthropic::claude-sonnet-5"]').click();

    await expect(page.locator('#modelPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#modelBadgeName')).toHaveText('claude-sonnet-5');

    // Persisted server-side (state_store.py), not just client-side state -
    // survives a fresh page load the same way the DB connection badge does.
    await gotoApp(page);
    await expect(page.locator('#modelBadgeName')).toHaveText('claude-sonnet-5');
  });

  test('selecting a model does not disturb the database connection badge', async ({ page }) => {
    await gotoApp(page);
    const dbNameBefore = await page.locator('#connDbName').textContent();

    await openModelPickList(page);
    await page.locator('#modelPickList .model-pick-option[data-value="openai::gpt-5.6-luna"]').click();
    await expect(page.locator('#modelPickList')).toHaveClass(/hidden/);

    await expect(page.locator('#connDbName')).toHaveText(dbNameBefore);
  });

  test('clicking the badge again while the list is open closes it (toggle)', async ({ page }) => {
    await gotoApp(page);
    await openModelPickList(page);

    await page.locator('#modelTriggerBadge').click();
    await expect(page.locator('#modelPickList')).toHaveClass(/hidden/);
  });
});

// Under the same <=900px breakpoint that folds Help/History/Preferences/
// Feedback/Sign-in into the triple-dot menu (see more-menu.spec.js),
// #modelPickerWrapper is ALSO CSS-hidden and its own second-level
// presentation - #moreMenuModelBtn (current-selection display + toggle) and
// #moreMenuModelSubmenu (the same provider-grouped option list, shared via
// renderModelPickList() in client.js) - takes over instead. See index.html's
// comment on #moreMenuModelBtn for why this needed its own presentation
// rather than just forwarding a .click() to the header badge the way Doc/
// History/Preferences do to THEIR real buttons: there's no visible header
// badge left to forward to once this fires.
test.describe('model selection under the more menu (narrow header)', () => {
  test.use({ viewport: { width: 700, height: 800 } });

  async function openMoreMenu(page) {
    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuDropdown')).not.toHaveClass(/hidden/);
  }

  async function openMoreMenuModelSubmenu(page) {
    await openMoreMenu(page);
    await page.locator('#moreMenuModelBtn').click();
    await expect(page.locator('#moreMenuModelSubmenu')).not.toHaveClass(/hidden/);
  }

  test('the header badge is hidden and the more-menu shows the current model instead', async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator('#modelPickerWrapper')).not.toBeVisible();
    await openMoreMenu(page);
    await expect(page.locator('#moreMenuModelBtn')).toBeVisible();
    await expect(page.locator('#moreMenuModelCurrent')).toHaveText('gemini-3.6-flash');
  });

  test('clicking the Model row expands a second-level list of options, grouped by provider', async ({ page }) => {
    await gotoApp(page);
    await openMoreMenuModelSubmenu(page);

    const submenu = page.locator('#moreMenuModelSubmenu');
    await expect(submenu.locator('.model-pick-group-heading')).toHaveText(['Google', 'Anthropic', 'OpenAI']);
    await expect(submenu.locator('.model-pick-option.selected')).toHaveAttribute('data-value', 'google::gemini-3.6-flash');
  });

  test('clicking an option in the submenu selects and saves it immediately, closing the whole menu', async ({ page }) => {
    await gotoApp(page);
    await openMoreMenuModelSubmenu(page);

    await page.locator('#moreMenuModelSubmenu .model-pick-option[data-value="anthropic::claude-sonnet-5"]').click();

    // Not just the submenu - the whole triple-dot menu, same "a selection
    // is a completed action" treatment as picking a model from the header
    // badge's own list closes that one.
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);

    // Persisted server-side, same as the wide-header pick list - survives
    // a fresh page load.
    await gotoApp(page);
    await openMoreMenu(page);
    await expect(page.locator('#moreMenuModelCurrent')).toHaveText('claude-sonnet-5');
  });

  test('clicking the Model row again while its submenu is open collapses just the submenu, leaving the rest of the menu open', async ({ page }) => {
    await gotoApp(page);
    await openMoreMenuModelSubmenu(page);

    await page.locator('#moreMenuModelBtn').click();
    await expect(page.locator('#moreMenuModelSubmenu')).toHaveClass(/hidden/);
    await expect(page.locator('#moreMenuDropdown')).not.toHaveClass(/hidden/);
  });

  test('closing and reopening the more menu leaves the model submenu collapsed again, not still expanded from last time', async ({ page }) => {
    await gotoApp(page);
    await openMoreMenuModelSubmenu(page);

    // Click outside closes the whole menu (same idiom as more-menu.spec.js).
    await page.locator('.crbot-title-group').click();
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);

    await openMoreMenu(page);
    await expect(page.locator('#moreMenuModelSubmenu')).toHaveClass(/hidden/);
  });

  test('selecting a model here does not disturb the database connection badge', async ({ page }) => {
    await gotoApp(page);
    const dbNameBefore = await page.locator('#connDbName').textContent();

    await openMoreMenuModelSubmenu(page);
    await page.locator('#moreMenuModelSubmenu .model-pick-option[data-value="openai::gpt-5.6-luna"]').click();

    await expect(page.locator('#connDbName')).toHaveText(dbNameBefore);
  });

  // Mirrors translate-execute.spec.js's "the DB connection and model
  // badges are disabled while a query is in flight" test, for this
  // narrow-header surface - switching the active model mid-turn is exactly
  // as unsafe from here as from the header badge itself (see
  // setButtonsDisabled()'s own comment in client.js), so the guard has to
  // cover both presentations of the same control.
  test('the Model row is disabled while a query is in flight, and re-enabled once it settles', async ({ page }) => {
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
    await openMoreMenu(page);

    const moreMenuModelBtn = page.locator('#moreMenuModelBtn');
    await expect(moreMenuModelBtn).not.toBeDisabled();

    await page.locator('#aiPrompt').fill('anything');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    await expect(moreMenuModelBtn).toBeDisabled();

    await expect.poll(() => normalizedSql(page), { timeout: 5000 }).toContain('SELECT 1');

    await expect(moreMenuModelBtn).not.toBeDisabled();
  });
});
