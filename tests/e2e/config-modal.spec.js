// tests/e2e/config-modal.spec.js
//
// The database-connection config modal, against the REAL Flask server and
// REAL SqliteStateStore - no mocking. Saving a custom connection (even a
// BigQuery one with a throwaway fake service-account key) never triggers a
// real network call to Postgres/BigQuery: config_routes.py's POST handler
// only validates and persists the descriptor, and the BigQuery Python
// client's constructor doesn't make a network call by itself (see
// backends/bigquery.py's connect()) - so this is safe to exercise for
// real. Only *executing* a query (mocked in translate-execute.spec.js)
// would actually need a live connection.

const crypto = require('crypto');
const { test, expect, gotoApp } = require('./fixtures');

/** A syntactically-valid but entirely fake service-account key, generated
 * fresh - never a real credential. Google's own libraries only need it to
 * parse as a well-formed key when nothing actually authenticates against
 * it (this suite never executes a query against a custom BigQuery
 * connection - see translate-execute.spec.js for the parts that do,
 * mocked at the network layer instead). */
function fakeServiceAccountKeyJson(projectId = 'fake-test-project') {
  const { privateKey } = crypto.generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
  });
  return JSON.stringify({
    type: 'service_account',
    project_id: projectId,
    private_key_id: 'fake-key-id',
    private_key: privateKey,
    client_email: `fake-e2e-test@${projectId}.iam.gserviceaccount.com`,
    client_id: '000000000000000000000',
    auth_uri: 'https://accounts.google.com/o/oauth2/auth',
    token_uri: 'https://oauth2.googleapis.com/token',
  });
}

async function openConfigModal(page) {
  await page.locator('#configTriggerBadge').click();
  await expect(page.locator('#configModal')).not.toHaveClass(/hidden/);
}

/** Clicks "+ Add custom connection" and waits for the row to be genuinely
 * ready before returning. client.js's addCustomDbBtn handler re-renders
 * the row synchronously but then schedules a requestAnimationFrame
 * callback that focuses its name input a frame later (a nicety for real
 * users, so they can start typing immediately) - a test that fills fields
 * faster than that callback fires can have its own keystrokes land, then
 * get raced by that late focus() shifting focus mid-fill. Waiting for the
 * name input to actually be focused guarantees that callback has already
 * run before this returns. */
async function addCustomDbRow(page) {
  await page.locator('#addCustomDbBtn').click();
  const nameInput = page.locator('.custom-db-name-input').last();
  await expect(nameInput).toBeFocused();
  return nameInput;
}

test.describe('config modal', () => {
  test('opens showing at least the default preset, and closes', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    await expect(page.locator('#modalDbRadioGroup input[type="radio"]').first()).toBeVisible();

    await page.locator('#modalCloseBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
  });

  test('adding a custom Postgres connection persists it and updates the badge', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    const nameInput = await addCustomDbRow(page);
    const urlInput = page.locator('.custom-db-url-input').last();
    await nameInput.fill('My Postgres DB');
    await urlInput.fill('postgresql://user:pass@localhost:5432/mydb');

    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('My Postgres DB');

    // Re-opening confirms it round-tripped through the real server/state
    // store, not just local in-memory JS state.
    await openConfigModal(page);
    await expect(page.locator('.custom-db-name-input').first()).toHaveValue('My Postgres DB');
  });

  test('custom BigQuery row shows labeled, hyperlinked Billing Project / Service Account Key fields', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');

    const billingLabel = page.locator('label[for^="custom-db-bq-billing-"]').last();
    const credsLabel = page.locator('label[for^="custom-db-bq-creds-"]').last();
    await expect(billingLabel).toContainText('Billing Project');
    await expect(credsLabel).toContainText('Service Account Key');

    await expect(billingLabel.locator('a')).toHaveAttribute('href', /cloud\.google\.com/);
    await expect(credsLabel.locator('a')).toHaveAttribute('href', /cloud\.google\.com\/iam\/docs\/keys-create-delete/);
  });

  test('an incomplete custom BigQuery row (no billing project / key) is never submitted', async ({ page }) => {
    // Mirrors config_routes.py's server-side rule (never inferring a
    // billing project or reusing another connection's key - see
    // test_config_billing_policy.py in the backend suite for that side),
    // but this is the client-side half: triggerConfigSave()'s
    // isCompleteBigQuery() gate means an unfinished row is silently
    // dropped rather than ever reaching the network half-filled. With no
    // other complete connection to select, saving quietly falls back to
    // the untouched default preset instead of persisting garbage.
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    // Deliberately leave billing project + key blank.

    await page.locator('#configSaveBtn').click();

    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await openConfigModal(page);
    await expect(page.locator('.custom-db-bq-project')).toHaveCount(0);
  });

  test('saving a complete custom BigQuery connection succeeds against the real server', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-name-input').last().fill('My BQ Conn');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    await page.locator('.custom-db-bq-billing').last().fill('my-own-billing-project');
    await page.locator('.custom-db-bq-creds').last().fill(fakeServiceAccountKeyJson());

    await page.locator('#configSaveBtn').click();

    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('My BQ Conn');
  });

  test('the service-account key never round-trips back into the page', async ({ page }) => {
    const keyJson = fakeServiceAccountKeyJson('leak-check-project');

    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-name-input').last().fill('Leak Check');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    await page.locator('.custom-db-bq-billing').last().fill('leak-check-billing');
    await page.locator('.custom-db-bq-creds').last().fill(keyJson);
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    // Re-open and inspect the full modal HTML - the key text must not
    // appear anywhere, and the key textarea must be blank (with a
    // placeholder indicating a key is already saved), never pre-filled.
    await openConfigModal(page);
    const modalHtml = await page.locator('#configModal').innerHTML();
    expect(modalHtml).not.toContain(keyJson);

    const credsTextarea = page.locator('.custom-db-bq-creds').last();
    await expect(credsTextarea).toHaveValue('');
    await expect(credsTextarea).toHaveAttribute('placeholder', /already saved|leave blank/i);
  });
});
