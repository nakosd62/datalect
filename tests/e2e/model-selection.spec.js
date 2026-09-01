// tests/e2e/model-selection.spec.js
//
// The model-selection badge/modal, against the REAL Flask server and REAL
// SqliteStateStore - no mocking (same reasoning as config-modal.spec.js:
// saving a model choice only ever validates and persists a string against
// translate_routes.py's LlmProvider registry, never makes a live LLM call).
// /api/translate itself is still mocked by the default `test` fixture, but
// this suite never exercises it - only /api/config's GET/POST.

const { test, expect, gotoApp } = require('./fixtures');

async function openModelModal(page) {
  await page.locator('#modelTriggerBadge').click();
  await expect(page.locator('#modelModal')).not.toHaveClass(/hidden/);
}

test.describe('model selection modal', () => {
  test('badge shows the default model on load', async ({ page }) => {
    await gotoApp(page);
    // playwright.config.js's webServer deliberately skips the real dev
    // .env (YDYL_SKIP_DOTENV) and sets no GOOGLE_MODELS override, so this
    // reflects the app's own hardcoded fallback default -
    // GeminiProvider.fallback_models.
    await expect(page.locator('#modelBadgeName')).toHaveText('gemini-3.6-flash');
  });

  test('opens showing one radio-group heading per provider, and closes', async ({ page }) => {
    await gotoApp(page);
    await openModelModal(page);

    const radioGroup = page.locator('#modalModelRadioGroup');
    await expect(radioGroup.locator('.radio-group-heading')).toHaveText(['Google', 'Anthropic', 'OpenAI']);
    await expect(radioGroup.locator('input[type="radio"]').first()).toBeVisible();

    // The currently-active model's radio is the one pre-checked.
    await expect(page.locator('input[name="llm_model_option"]:checked')).toHaveValue('google::gemini-3.6-flash');

    await page.locator('#modelModalCloseBtn').click();
    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);
  });

  test('selecting a different model and saving updates the badge and persists across reloads', async ({ page }) => {
    await gotoApp(page);
    await openModelModal(page);

    await page.locator('input[name="llm_model_option"][value="anthropic::claude-sonnet-5"]').check();
    await page.locator('#modelSaveBtn').click();

    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);
    await expect(page.locator('#modelBadgeName')).toHaveText('claude-sonnet-5');

    // Persisted server-side (state_store.py), not just client-side state -
    // survives a fresh page load the same way the DB connection badge does.
    await gotoApp(page);
    await expect(page.locator('#modelBadgeName')).toHaveText('claude-sonnet-5');
  });

  test('selecting a model does not disturb the database connection badge', async ({ page }) => {
    await gotoApp(page);
    const dbNameBefore = await page.locator('#connDbName').textContent();

    await openModelModal(page);
    await page.locator('input[name="llm_model_option"][value="openai::gpt-5.6-luna"]').check();
    await page.locator('#modelSaveBtn').click();
    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);

    await expect(page.locator('#connDbName')).toHaveText(dbNameBefore);
  });
});
