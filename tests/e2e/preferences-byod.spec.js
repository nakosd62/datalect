// tests/e2e/preferences-byod.spec.js
//
// The "Bring Your Own Data" section of the Preferences/Settings dialog
// (#preferencesModal, opened via #prefsBtn) - creating, editing, and
// deleting a custom (private) database connection's own saved record.
// This is everything that used to live inside the datasource selector
// (the old #configModal, before the Phase 1 relocation, and now the
// #datasetPickList dropdown - see dataset-picker.spec.js's own module
// docstring for that conversion): popping a full credential form open from
// inside a "just pick a datasource" picker surprised users, so creating/
// editing/deleting a custom connection moved here, and the dataset picker's
// own "Private" section became a plain, select-only list of names (see
// dataset-picker.spec.js for that half, and its own module docstring for
// the exact mechanics of the split).
//
// Against the REAL Flask server, same reasoning as dataset-picker.spec.js's
// own header: saving a custom connection (even a BigQuery one with a
// throwaway fake service-account key) never triggers a real network call
// out to Postgres/BigQuery - config_routes.py's POST handler only
// validates and persists the descriptor, and the BigQuery Python client's
// constructor doesn't make a network call by itself (see backends/
// bigquery.py's connect()). Only *executing* a query would actually need a
// live connection (mocked in translate-execute.spec.js), and nothing here
// does that.
//
// IMPORTANT, and the whole reason most tests below never touch the dataset
// picker dropdown at all: savePreferences() (this dialog's own Save,
// #preferencesSaveBtn) persists the `custom_databases` list completely
// independently of which connection is currently ACTIVE (see
// config_routes.py's handle_config() - the two are deliberately
// orthogonal concerns, and this payload never includes database_name/
// database_type/is_custom/etc. at all - see buildCustomDatabasesPayload()'s
// own comment in client.js). So a test here that only cares "did this
// persist" never needs to also select/activate it - it just saves, then
// reopens Preferences (a fresh fetchBackendConfig()) to confirm the row
// round-tripped through the real server/state store, not just local
// in-memory JS state. A test that explicitly wants to know whether a
// persisted connection can be SELECTED and made active (e.g. "updates the
// badge") belongs in dataset-picker.spec.js instead, against its own
// select-only "Private" list - see that file's
// createCustomConnectionViaPreferences() helper, which exists precisely
// so those tests can set up a saved connection without duplicating this
// file's own creation flow.

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

/** Opens the Preferences dialog, the same way show-sql-toggle.spec.js's
 * own helper of the same name does - except this one also gives the
 * initial open a generous timeout, mirroring dataset-picker.spec.js's own
 * openDatasetPickList(): #prefsBtn's click handler awaits a real, unmocked
 * fetch('/api/config') (fetchBackendConfig() in client.js) before it
 * removes #preferencesModal's hidden class, and this file's own tests
 * routinely save several custom connections (some with real fake
 * BigQuery keys) in a single test, so the same "a loaded/constrained
 * environment can occasionally make one request take longer than the
 * default 8s expect timeout, without anything actually being stuck"
 * reasoning applies here too. */
async function openPreferencesModal(page) {
  await page.locator('#prefsBtn').click();
  await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
}

/** Clicks "+ Create new" and waits for the row to be genuinely
 * ready before returning - same as dataset-picker.spec.js's own former copy
 * of this helper (moved here along with the "+ Create new" button/rows
 * themselves - see this file's own module docstring). client.js's
 * addCustomDbBtn handler re-renders the row synchronously but then
 * schedules a requestAnimationFrame callback that focuses its name input
 * a frame later (a nicety for real users, so they can start typing
 * immediately) - a test that fills fields faster than that callback fires
 * can have its own keystrokes land, then get raced by that late focus()
 * shifting focus mid-fill. Waiting for the name input to actually be
 * focused guarantees that callback has already run before this returns. */
async function addCustomDbRow(page) {
  await page.locator('#addCustomDbBtn').click();
  const nameInput = page.locator('.custom-db-name-input').last();
  await expect(nameInput).toBeFocused();
  return nameInput;
}

test.describe('Bring Your Own Data (Preferences)', () => {
  test('opens from Preferences, immediately above Bring Your Own Key', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const byodHeading = page.locator('.config-label', { hasText: 'Bring Your Own Data' });
    const byokHeading = page.locator('.config-label', { hasText: 'Bring Your Own Key' });
    await expect(byodHeading).toBeVisible();
    await expect(byokHeading).toBeVisible();

    // "immediately above" - BYOD's own section comes right before BYOK's
    // in document order (both are .config-section siblings within the
    // same modal body).
    const sections = page.locator('#preferencesModal .config-section');
    const labelTexts = await sections.locator('.config-label').allTextContents();
    const byodIdx = labelTexts.indexOf('Bring Your Own Data');
    const byokIdx = labelTexts.indexOf('Bring Your Own Key');
    expect(byodIdx).toBeGreaterThanOrEqual(0);
    expect(byokIdx).toBeGreaterThan(byodIdx);
  });

  test('adding a custom Postgres connection persists it', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const nameInput = await addCustomDbRow(page);
    const urlInput = page.locator('.custom-db-url-input').last();
    await nameInput.fill('My Postgres DB');
    await urlInput.fill('postgresql://user:pass@localhost:5432/mydb');

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // Saving here never touches which connection is currently ACTIVE (see
    // this file's own module docstring) - the badge still reads the
    // untouched default. See dataset-picker.spec.js's own "selecting a saved
    // private datasource..." test for the activation half of this story.
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    // Re-opening confirms it round-tripped through the real server/state
    // store, not just local in-memory JS state.
    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-name-input').first()).toHaveValue('My Postgres DB');
  });

  test('custom BigQuery row shows labeled, hyperlinked Billing Project / Service Account Key fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

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
    // but this is the client-side half: savePreferences()'s own
    // buildCustomDatabasesPayload()/isCompleteBigQuery() gate means an
    // unfinished row is silently filtered out of the payload rather than
    // ever reaching the network half-filled - it simply never persists,
    // with no error shown.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    // Deliberately leave billing project + key blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-bq-project')).toHaveCount(0);
  });

  test('saving a complete custom BigQuery connection succeeds against the real server', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-name-input').last().fill('My BQ Conn');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    await page.locator('.custom-db-bq-billing').last().fill('my-own-billing-project');
    await page.locator('.custom-db-bq-creds').last().fill(fakeServiceAccountKeyJson());

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // No error, and it genuinely persisted (round-tripped through the
    // real server/state store as a BigQuery connection, not silently
    // dropped) - activating it is a dataset-picker.spec.js concern (the
    // selection mechanism is dialect-agnostic, already covered generically
    // there), so this just confirms the save itself succeeded.
    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-name-input').first()).toHaveValue('My BQ Conn');
  });

  test('the service-account key never round-trips back into the page', async ({ page }) => {
    const keyJson = fakeServiceAccountKeyJson('leak-check-project');

    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');
    await page.locator('.custom-db-name-input').last().fill('Leak Check');
    await page.locator('.custom-db-bq-project').last().fill('bigquery-public-data');
    await page.locator('.custom-db-bq-dataset').last().fill('usa_names');
    await page.locator('.custom-db-bq-billing').last().fill('leak-check-billing');
    await page.locator('.custom-db-bq-creds').last().fill(keyJson);
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // Re-open - a saved connection like this one now defaults to
    // collapsed (see the custom-db-toggle-btn coverage below), so expand
    // it before inspecting its fields.
    await openPreferencesModal(page);
    await page.locator('.custom-db-toggle-btn').last().click();

    // Inspect the BYOD container's full HTML - the key text must not
    // appear anywhere, and the key textarea must be blank (with a
    // placeholder indicating a key is already saved), never pre-filled.
    const containerHtml = await page.locator('#customDbsContainer').innerHTML();
    expect(containerHtml).not.toContain(keyJson);

    const credsTextarea = page.locator('.custom-db-bq-creds').last();
    await expect(credsTextarea).toHaveValue('');
    await expect(credsTextarea).toHaveAttribute('placeholder', /already saved|leave blank/i);
  });

  // Snowflake coverage below is deliberately narrower than BigQuery's above
  // and never actually SAVES a complete Snowflake connection against this
  // real, unmocked e2e server. Unlike bigquery.Client(...) (lazy - no
  // network I/O until a query actually runs, see the comment at the top of
  // this file), snowflake.connector.connect(...) performs a real,
  // synchronous, blocking login request as part of construction itself
  // (verified against the installed driver's source - SnowflakeConnection.
  // __init__ calls self.connect(...) directly, which opens the connection
  // immediately). Posting a complete custom Snowflake connection here would
  // make config_routes.py's post-save identity-check block (backend.connect()
  // - see handle_config) actually try to reach a fake account over the
  // network from this test run, on a single-threaded, unthreaded dev
  // server (see run_server.sh/Dockerfile) - a slow DNS/connection failure
  // there would block every other request, including every other test in
  // this suite, for however long that attempt takes to time out. The
  // save-succeeds / credential-never-leaks / credential-reuse behaviors
  // this would otherwise cover are already fully exercised, safely, at the
  // Python level against a mocked connector - see
  // tests/server/test_config_snowflake.py.
  test('custom Snowflake row shows warehouse/database/schema, account/user/role, and auth fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');

    await expect(page.locator('.custom-db-sf-account').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-database').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-user').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-warehouse').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-schema').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-role').last()).toBeVisible();
    // Password auth is the default - private-key fields shouldn't render
    // until the auth-method select is switched.
    await expect(page.locator('.custom-db-sf-password').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-private-key').last()).toHaveCount(0);
  });

  test('switching Snowflake auth method to key pair swaps the password field for private-key fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');
    await page.locator('.custom-db-sf-auth-method').last().selectOption('private_key');

    await expect(page.locator('.custom-db-sf-password').last()).toHaveCount(0);
    await expect(page.locator('.custom-db-sf-private-key').last()).toBeVisible();
    await expect(page.locator('.custom-db-sf-passphrase').last()).toBeVisible();

    const keyLabel = page.locator('label[for^="custom-db-sf-private-key-"]').last();
    await expect(keyLabel.locator('a')).toHaveAttribute('href', /docs\.snowflake\.com/);
  });

  test('an incomplete custom Snowflake row (no warehouse / credential) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery case above (buildCustomDatabasesPayload's
    // isCompleteSnowflake()) - and since the row never makes it into the
    // request, this never triggers the real-network-call risk described
    // above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');
    await page.locator('.custom-db-sf-account').last().fill('some-account');
    await page.locator('.custom-db-sf-database').last().fill('some_db');
    // Deliberately leave user/warehouse/credential blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-sf-account')).toHaveCount(0);
  });

  test('a custom connection row is visually enclosed between horizontal lines', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);

    const card = page.locator('.custom-db-card').last();
    await expect(card).toBeVisible();
    const borderBottom = await card.evaluate(el => getComputedStyle(el).borderBottomStyle);
    expect(borderBottom).toBe('solid');
    // The line above the first card comes from the list container itself
    // (so adjacent cards share one line instead of doubling up) - see
    // .custom-dbs-list:not(:empty) in style.css.
    const list = page.locator('#customDbsContainer');
    const borderTop = await list.evaluate(el => getComputedStyle(el).borderTopStyle);
    expect(borderTop).toBe('solid');
  });

  test('a Postgres custom row shows an unlabeled Name field on line 1 and a labeled URL field on line 2', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);

    const card = page.locator('.custom-db-card').last();
    // The "Name" label was removed completely per explicit request - no
    // visible <label> and no aria-label either, just the placeholder.
    await expect(card.locator('.custom-db-header-row label.custom-db-field-label')).toHaveCount(0);
    await expect(card.locator('.custom-db-header-row .custom-db-name-input')).not.toHaveAttribute('aria-label', /.+/);
    await expect(card.locator('.custom-db-header-row .custom-db-name-input')).toHaveAttribute('placeholder', 'Datasource Name');
    // A default (URL-based) Postgres row renders TWO .custom-db-field-row
    // divs - URL, then a separate CA Certificate row below it - so the
    // unscoped locator here used to match both labels and fail Playwright's
    // strict mode. .first() pins this to the URL row specifically, which is
    // always the one rendered first (see client.js's custom-db-url-${index}
    // markup, immediately followed by custom-db-cacert-${index}).
    await expect(card.locator('.custom-db-field-row label.custom-db-field-label').first()).toHaveText('URL:');
  });

  test('a newly-added custom connection row starts expanded, showing its detail fields right away', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);

    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-url-input')).toBeVisible();
    await expect(card.locator('.custom-db-toggle-btn')).toHaveAttribute('aria-expanded', 'true');
  });

  test('a previously-saved custom connection collapses by default, showing only type/name plus expand and remove controls', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Collapsible DB');
    await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/collapsible');
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // Re-open - this connection now came back from the server (it has a
    // connection_key), so it should default to collapsed: type/name still
    // visible, but the URL field hidden until expanded.
    await openPreferencesModal(page);
    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-name-input')).toHaveValue('Collapsible DB');
    await expect(card.locator('.custom-db-type-select')).toBeVisible();
    await expect(card.locator('.custom-db-url-input')).toHaveCount(0);
    const toggleBtn = card.locator('.custom-db-toggle-btn');
    await expect(toggleBtn).toHaveAttribute('aria-expanded', 'false');

    // Expanding reveals the URL field; toggling again re-collapses it.
    await toggleBtn.click();
    await expect(card.locator('.custom-db-url-input')).toBeVisible();
    await expect(toggleBtn).toHaveAttribute('aria-expanded', 'true');

    await toggleBtn.click();
    await expect(card.locator('.custom-db-url-input')).toHaveCount(0);
    await expect(toggleBtn).toHaveAttribute('aria-expanded', 'false');
  });

  test('the remove control still works on a collapsed (previously-saved) connection', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Removable DB');
    await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/removable');
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-url-input')).toHaveCount(0);
    // Scoped to this specific card, not `.custom-db-remove-btn` page-wide -
    // this same Preferences modal's 3 BYOK "Remove saved key" buttons
    // (lower down in the same dialog) share this exact same class name, so
    // an unscoped `.last()` here silently resolves to the BYOK "openai"
    // button instead of this card's own remove control.
    await card.locator('.custom-db-remove-btn').click();
    await expect(page.locator('.custom-db-name-input')).toHaveCount(0);

    // And the removal itself persists once saved - not just a client-side
    // in-memory change.
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);
    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-name-input')).toHaveCount(0);
  });

  test('a BigQuery custom row labels each field on its own line: Project ID/Dataset, Billing Project ID, Service Account Key', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 4th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(4);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Project ID:', 'Dataset:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Billing Project ID:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Service Account Key:']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);
  });

  test('a Snowflake custom row labels each field on its own line: Warehouse/Database/Schema, Account/User/Role, Authentication Method, then the credential', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 5th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(5);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Warehouse:', 'Database:', 'Schema: (optional)']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Account:', 'User:', 'Role: (optional)']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Authentication Method:']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText(['Password:']);
    await expect(fieldRows.nth(4).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);

    // Switching to key-pair auth swaps that last line for Private Key +
    // Passphrase, still both on their own (fifth) line - the timeout row
    // stays put as the now-sixth row.
    await page.locator('.custom-db-sf-auth-method').last().selectOption('private_key');
    const fieldRowsAfter = card.locator('.custom-db-field-row');
    await expect(fieldRowsAfter).toHaveCount(5);
    await expect(fieldRowsAfter.nth(3).locator('.custom-db-field-label')).toHaveText(['Private Key:', 'Passphrase: (if key is encrypted)']);
  });

  test('the type dropdown offers PostgreSQL, MySQL, BigQuery, Snowflake, Databricks, Oracle, Redshift, SQL Server, Google Sheets, and MongoDB', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);

    const options = await page.locator('.custom-db-type-select').last().locator('option').allTextContents();
    expect(options).toEqual(['PostgreSQL', 'MySQL', 'BigQuery', 'Snowflake', 'Databricks', 'Oracle', 'Redshift', 'SQL Server', 'Google Sheets', 'MongoDB']);
  });

  test('adding a custom MySQL connection persists it as MySQL specifically, not silently as Postgres', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    const nameInput = await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mysql');
    const urlInput = page.locator('.custom-db-url-input').last();
    await nameInput.fill('My MySQL DB');
    await urlInput.fill('mysql://user:pass@localhost:3306/mydb');

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    // Re-opening confirms it round-tripped through the real server/state
    // store as a MySQL connection specifically (see config_routes.py's
    // now-fixed dialect-fallback branches). The URL field redisplays with
    // its password masked (maskConnectionUrl in client.js) - same as every
    // other dialect's URL field - so the expected value here has the
    // password starred out too.
    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-name-input').first()).toHaveValue('My MySQL DB');
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('mysql');
    await expect(page.locator('.custom-db-url-input').first()).toHaveValue('mysql://user:******@localhost:3306/mydb');
  });

  test('a MySQL custom row shows an unlabeled Name field on line 1 and a labeled URL field on line 2', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mysql');

    const card = page.locator('.custom-db-card').last();
    // See the Postgres version of this test above - the "Name" label was
    // removed completely per explicit request, no aria-label either.
    await expect(card.locator('.custom-db-header-row label.custom-db-field-label')).toHaveCount(0);
    await expect(card.locator('.custom-db-header-row .custom-db-name-input')).not.toHaveAttribute('aria-label', /.+/);
    // Same strict-mode fix as the Postgres version of this test above: a
    // default (URL-based) MySQL row also renders a separate CA Certificate
    // row after the URL row, so this must be pinned to the URL row (first).
    await expect(card.locator('.custom-db-field-row label.custom-db-field-label').first()).toHaveText('URL:');
    await expect(card.locator('.custom-db-url-input')).toHaveAttribute('placeholder', /^mysql:\/\//);
  });

  test('switching a custom connection between Postgres and MySQL and Postgres again preserves the right type each time', async ({ page }) => {
    // Regression test for the bug this dialect's addition surfaced:
    // client.js used to treat "anything that isn't BigQuery/Snowflake" as
    // implicitly Postgres, so a MySQL selection would silently save as a
    // mislabeled Postgres connection (see buildCustomDatabasesPayload's
    // now-fixed simpleUrlType branch). Purely a persistence round-trip -
    // no activation involved at all.
    await gotoApp(page);
    await openPreferencesModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Switchy DB');
    await page.locator('.custom-db-type-select').last().selectOption('mysql');
    await page.locator('.custom-db-url-input').last().fill('mysql://user:pass@localhost:3306/switchy');
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('mysql');

    // Switch it to Postgres and save again.
    await page.locator('.custom-db-type-select').first().selectOption('postgres');
    await page.locator('.custom-db-name-input').first().fill('Switchy DB');
    await page.locator('.custom-db-url-input').first().fill('postgresql://user:pass@localhost:5432/switchy');
    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('postgres');
    // Masked the same way the MySQL round-trip above is - see that test's
    // comment.
    await expect(page.locator('.custom-db-url-input').first()).toHaveValue('postgresql://user:******@localhost:5432/switchy');
  });

  // Databricks coverage below is deliberately narrow, for the same reason
  // Snowflake's is (see the comment above that block): posting a complete
  // custom Databricks connection here would make config_routes.py's
  // post-save identity-check block (backend.connect() - see handle_config)
  // actually try to reach a fake workspace over the network from this test
  // run, on a single-threaded, unthreaded dev server - a slow DNS/
  // connection failure there would block every other request in this
  // suite. The save-succeeds / credential-never-leaks / credential-reuse
  // behaviors this would otherwise cover are already fully exercised,
  // safely, at the Python level against a mocked connector - see
  // tests/server/test_config_databricks.py.
  test('custom Databricks row shows Server Hostname, HTTP Path, Catalog/Schema, and Access Token fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');

    await expect(page.locator('.custom-db-dbx-hostname').last()).toBeVisible();
    await expect(page.locator('.custom-db-dbx-path').last()).toBeVisible();
    await expect(page.locator('.custom-db-dbx-catalog').last()).toBeVisible();
    await expect(page.locator('.custom-db-dbx-schema').last()).toBeVisible();
    await expect(page.locator('.custom-db-dbx-token').last()).toBeVisible();
  });

  test('a Databricks custom row labels each field on its own line: Server Hostname, HTTP Path, Catalog/Schema, then Access Token', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 5th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(5);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Server Hostname:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['HTTP Path:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Catalog: (optional)', 'Schema: (optional)']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText(['Access Token:']);
    await expect(fieldRows.nth(4).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);
  });

  test('an incomplete custom Databricks row (no HTTP path / token) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake cases above
    // (buildCustomDatabasesPayload's isCompleteDatabricks()) - and since
    // the row never makes it into the request, this never triggers the
    // real-network-call risk described above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');
    await page.locator('.custom-db-dbx-hostname').last().fill('dbc-x.cloud.databricks.com');
    // Deliberately leave HTTP path/access token blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-dbx-hostname')).toHaveCount(0);
  });

  test('the Databricks access token never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key test above: an access token
    // is a credential, never redisplayed once saved, same as every other
    // dialect's credential field(s). The row is never actually submitted
    // here (see the network-call-safety note above) - this just pins that
    // the field starts, and stays, blank rather than ever showing a value.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');

    await expect(page.locator('.custom-db-dbx-token').last()).toHaveValue('');
  });

  // Oracle coverage below is deliberately narrow, for the same reason
  // Snowflake's/Databricks' is (see the comments above those blocks):
  // posting a complete custom Oracle connection here would make
  // config_routes.py's post-save identity-check block (backend.connect() -
  // see handle_config) actually try to reach a fake host over the network
  // from this test run, on a single-threaded, unthreaded dev server - a
  // slow DNS/connection failure there would block every other request in
  // this suite. The save-succeeds / credential-never-leaks / credential-
  // reuse behaviors this would otherwise cover are already fully
  // exercised, safely, at the Python level against a mocked connector -
  // see tests/server/test_config_oracle.py.
  test('custom Oracle row shows Host, Port, Service Name/SID, User/Schema, Password, and Use TLS fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    await expect(page.locator('.custom-db-ora-host').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-port').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-service').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-sid').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-user').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-schema').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-password').last()).toBeVisible();
    await expect(page.locator('.custom-db-ora-ssl').last()).toBeVisible();
  });

  test('an Oracle custom row labels each field on its own line: Host/Port, Service Name/SID/Schema, User/Password, then Use TLS', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 5th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(5);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Host:', 'Port:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText([
      'Service Name:', 'SID: (legacy)', 'Schema: (optional)',
    ]);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['User:', 'Password:']);
    await expect(fieldRows.nth(3)).toContainText('Use TLS (required for Oracle Cloud)');
    await expect(fieldRows.nth(4).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);
  });

  test('the Oracle "Use TLS" checkbox starts checked (by default) and toggles off on click', async ({ page }) => {
    // Defaults ON - most Oracle connections added through this dialog
    // target Oracle Cloud, which requires TLS (see backends/oracle.py's
    // module docstring); a plain on-prem/XE listener is the exception,
    // not the common case, so it's opt-out rather than opt-in here.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    const sslCheckbox = page.locator('.custom-db-ora-ssl').last();
    await expect(sslCheckbox).toBeChecked();
    await sslCheckbox.click();
    await expect(sslCheckbox).not.toBeChecked();
  });

  test('an incomplete custom Oracle row (no service name/sid / password) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake/Databricks cases
    // above (buildCustomDatabasesPayload's isCompleteOracle()) - and since
    // the row never makes it into the request, this never triggers the
    // real-network-call risk described above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');
    await page.locator('.custom-db-ora-host').last().fill('db.example.com');
    await page.locator('.custom-db-ora-user').last().fill('alice');
    // Deliberately leave service name/SID and password blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-ora-host')).toHaveCount(0);
  });

  test('the Oracle password never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key/Databricks access-token
    // tests above: a password is a credential, never redisplayed once
    // saved, same as every other dialect's credential field(s). The row is
    // never actually submitted here (see the network-call caution above) -
    // this just pins that the field starts, and stays, blank rather than
    // ever showing a value.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    await expect(page.locator('.custom-db-ora-password').last()).toHaveValue('');
  });

  // Redshift coverage below is deliberately narrow, for the same reason
  // Snowflake's/Databricks'/Oracle's is (see the comments above those
  // blocks): posting a complete custom Redshift connection here would make
  // config_routes.py's post-save identity-check block (backend.connect() -
  // see handle_config) actually try to reach a fake cluster over the
  // network from this test run, on a single-threaded, unthreaded dev
  // server - a slow DNS/connection failure there would block every other
  // request in this suite. The save-succeeds / credential-never-leaks /
  // credential-reuse behaviors this would otherwise cover are already
  // fully exercised, safely, at the Python level against a mocked
  // connector - see tests/server/test_config_redshift.py. Unlike Oracle,
  // there's no "Use TLS" checkbox here - Redshift connections always
  // require TLS (see backends/redshift.py's connect()), so it's simply
  // always on rather than a per-connection choice.
  test('custom Redshift row shows Host, Port, Database, Schema, User, and Password fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('redshift');

    await expect(page.locator('.custom-db-rs-host').last()).toBeVisible();
    await expect(page.locator('.custom-db-rs-port').last()).toBeVisible();
    await expect(page.locator('.custom-db-rs-database').last()).toBeVisible();
    await expect(page.locator('.custom-db-rs-schema').last()).toBeVisible();
    await expect(page.locator('.custom-db-rs-user').last()).toBeVisible();
    await expect(page.locator('.custom-db-rs-password').last()).toBeVisible();
  });

  test('a Redshift custom row labels each field on its own line: Host/Port, Database/Schema, then User/Password', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('redshift');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 4th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(4);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Host:', 'Port:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Database:', 'Schema: (optional)']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['User:', 'Password:']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);
  });

  test('an incomplete custom Redshift row (no database / password) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake/Databricks/Oracle
    // cases above (buildCustomDatabasesPayload's isCompleteRedshift()) -
    // and since the row never makes it into the request, this never
    // triggers the real-network-call risk described above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('redshift');
    await page.locator('.custom-db-rs-host').last().fill('cluster.example.com');
    await page.locator('.custom-db-rs-user').last().fill('alice');
    // Deliberately leave database and password blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-rs-host')).toHaveCount(0);
  });

  test('the Redshift password never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key/Databricks access-token/
    // Oracle password tests above: a password is a credential, never
    // redisplayed once saved, same as every other dialect's credential
    // field(s). The row is never actually submitted here (see the
    // network-call caution above) - this just pins that the field starts,
    // and stays, blank rather than ever showing a value.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('redshift');

    await expect(page.locator('.custom-db-rs-password').last()).toHaveValue('');
  });

  // SQL Server coverage below is deliberately narrow, for the same reason
  // Snowflake's/Databricks'/Oracle's/Redshift's is (see the comments above
  // those blocks): posting a complete custom SQL Server connection here
  // would make config_routes.py's post-save identity-check block
  // (backend.connect() - see handle_config) actually try to reach a fake
  // server over the network from this test run, on a single-threaded,
  // unthreaded dev server - a slow DNS/connection failure there would
  // block every other request in this suite. The save-succeeds/
  // credential-never-leaks/credential-reuse behaviors this would
  // otherwise cover are already fully exercised, safely, at the Python
  // level against a mocked connector - see tests/server/test_config_mssql.py.
  // Like Oracle's "Use TLS" checkbox (and unlike Redshift's always-on
  // TLS), SQL Server's "Encrypt Connection" is a per-connection opt-in/out
  // checkbox - but it defaults checked ON, not off, matching connect()'s
  // own absent-defaults-to-True behavior (see backends/mssql.py's module
  // docstring).
  test('custom SQL Server row shows Host, Port, Database, Schema, User, Password, and Encrypt Connection fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mssql');

    await expect(page.locator('.custom-db-ms-host').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-port').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-database').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-schema').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-user').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-password').last()).toBeVisible();
    await expect(page.locator('.custom-db-ms-encrypt').last()).toBeVisible();
  });

  test('a SQL Server custom row labels each field on its own line: Host/Port, Database/Schema, User/Password, then Encrypt Connection', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mssql');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    // 5th row: the shared, dialect-agnostic optional timeout-override pair
    // (see customDbTimeoutFieldsHtml() in client.js) rendered at the
    // bottom of every dialect's own field block.
    await expect(fieldRows).toHaveCount(5);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Host:', 'Port:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Database:', 'Schema: (optional)']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['User:', 'Password:']);
    await expect(fieldRows.nth(3)).toContainText('Encrypt Connection (required for Azure SQL Database)');
    await expect(fieldRows.nth(4).locator('.custom-db-field-label')).toHaveText([
      'Connect Timeout (seconds): (optional)', 'Execute Timeout (seconds): (optional)',
    ]);
  });

  test('the SQL Server "Encrypt Connection" checkbox starts checked (by default) and toggles off on click', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mssql');

    const encryptCheckbox = page.locator('.custom-db-ms-encrypt').last();
    await expect(encryptCheckbox).toBeChecked();
    await encryptCheckbox.click();
    await expect(encryptCheckbox).not.toBeChecked();
  });

  test('an incomplete custom SQL Server row (no database / password) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake/Databricks/Oracle/
    // Redshift cases above (buildCustomDatabasesPayload's isCompleteMssql()) -
    // and since the row never makes it into the request, this never
    // triggers the real-network-call risk described above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mssql');
    await page.locator('.custom-db-ms-host').last().fill('server.example.com');
    await page.locator('.custom-db-ms-user').last().fill('alice');
    // Deliberately leave database and password blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-ms-host')).toHaveCount(0);
  });

  test('the SQL Server password never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key/Databricks access-token/
    // Oracle-Redshift password tests above: a password is a credential,
    // never redisplayed once saved, same as every other dialect's
    // credential field(s). The row is never actually submitted here (see
    // the network-call caution above) - this just pins that the field
    // starts, and stays, blank rather than ever showing a value.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mssql');

    await expect(page.locator('.custom-db-ms-password').last()).toHaveValue('');
  });

  // Google Sheets coverage below is deliberately narrow, for the same
  // network-call-safety reason as the SQL Server/Redshift/Oracle/
  // Databricks/Snowflake blocks above - identity_label() (called during
  // config_routes.py's post-save identity check) performs a real HTTP GET
  // against Google's gviz endpoint for this dialect (see
  // backends/sheets.py's module docstring), so a complete row is never
  // actually submitted here. That save-succeeds/round-trips-the-right-
  // fields behavior is already fully exercised, safely, at the Python
  // level against a fake HTTP layer - see tests/server/test_config_sheets.py.
  // Unlike every other dialect's own block above, the credential field here
  // (Service Account Key) is OPTIONAL, not required - a row is already
  // "complete" with just Spreadsheet URL + Tab Name filled in (see
  // isCompleteSheets in client.js), so there's no "leaving it blank keeps
  // has_custom_credentials false"-style gate to test the way BigQuery's/
  // SQL Server's blocks above do.
  test('custom Google Sheets row shows Spreadsheet URL, Tab Name, and Service Account Key fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('sheets');

    await expect(page.locator('.custom-db-sh-url').last()).toBeVisible();
    await expect(page.locator('.custom-db-sh-tab').last()).toBeVisible();
    await expect(page.locator('.custom-db-sh-creds').last()).toBeVisible();
  });

  test('a Google Sheets custom row labels each field on its own line: Spreadsheet URL, Tab Name, then Service Account Key', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('sheets');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Spreadsheet URL:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Tab Name:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Service Account Key (optional):']);
  });

  test('an incomplete custom Google Sheets row (no tab name) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake/Databricks/Oracle/
    // Redshift/SQL Server cases above (buildCustomDatabasesPayload's
    // isCompleteSheets()) - and since the row never makes it into the
    // request, this never triggers the real-network-call risk described
    // above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('sheets');
    await page.locator('.custom-db-sh-url').last().fill('https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit');
    // Deliberately leave tab name (and the optional credential) blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-sh-url')).toHaveCount(0);
  });

  test('leaving the Google Sheets Service Account Key blank does not block the row from being otherwise complete', async ({ page }) => {
    // Filling url+tab (both required) but leaving the optional credential
    // textarea untouched must still register as "complete" client-side -
    // unlike BigQuery's always-required key, a blank one here is the
    // normal, common case, not something that should gate the row. Doesn't
    // actually click Save (see the network-call-safety note above) - just
    // confirms the field's value stays blank and doesn't itself throw/
    // block typing into the other two fields.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('sheets');
    await page.locator('.custom-db-sh-url').last().fill('https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit');
    await page.locator('.custom-db-sh-tab').last().fill('Sheet1');

    await expect(page.locator('.custom-db-sh-creds').last()).toHaveValue('');
    await expect(page.locator('.custom-db-sh-url').last()).toHaveValue('https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit');
  });

  // MongoDB Atlas coverage below is deliberately narrow, for the same
  // network-call-safety reason as the SQL Server/Redshift/Oracle/
  // Databricks/Snowflake blocks above - connect() (called during
  // config_routes.py's post-save identity check) opens a real ODBC/TCP
  // connection to the Atlas host for this dialect (see backends/
  // mongodb_sql.py's module docstring), so a complete row is never
  // actually submitted here. That save-succeeds/round-trips-the-right-
  // fields behavior is already fully exercised, safely, at the Python
  // level against a mocked pyodbc.connect - see
  // tests/server/test_config_mongodb_sql.py. Unlike every other structured
  // dialect above, this one has a real "url" field (the bare mongodb://
  // deployment URI) ALONGSIDE database/user/password, rather than no url
  // at all - see this dialect's isCompleteMongo() in client.js.
  test('custom MongoDB row shows URI, Database, User, and Password fields', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('MongoDB');

    await expect(page.locator('.custom-db-mongo-uri').last()).toBeVisible();
    await expect(page.locator('.custom-db-mongo-database').last()).toBeVisible();
    await expect(page.locator('.custom-db-mongo-user').last()).toBeVisible();
    await expect(page.locator('.custom-db-mongo-password').last()).toBeVisible();
  });

  test('a MongoDB custom row labels each field on its own line: URI, then Database/User, then Password', async ({ page }) => {
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('MongoDB');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['URI:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Database:', 'User:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Password:']);
  });

  test('an incomplete custom MongoDB row (no database / password) is never submitted', async ({ page }) => {
    // Same client-side gate as every other structured dialect above
    // (buildCustomDatabasesPayload's isCompleteMongo()) - and since the
    // row never makes it into the request, this never triggers the
    // real-network-call risk described above either.
    await gotoApp(page);
    await openPreferencesModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('MongoDB');
    await page.locator('.custom-db-mongo-uri').last().fill('mongodb://atlas-sql-abc.mongodb.net/?ssl=true');
    await page.locator('.custom-db-mongo-user').last().fill('alice');
    // Deliberately leave database and password blank.

    await page.locator('#preferencesSaveBtn').click();
    await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);

    await openPreferencesModal(page);
    await expect(page.locator('.custom-db-mongo-uri')).toHaveCount(0);
  });

  test('the MongoDB password never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key/Databricks access-token/
    // Oracle-Redshift-SQL Server password tests above: a password is a
    // credential, never redisplayed once saved, same as every other
    // dialect's credential field(s). The row is never actually submitted
    // here (see the network-call caution above) - this just pins that the
    // field starts, and stays, blank rather than ever showing a value.
    await gotoApp(page);
    await openPreferencesModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('MongoDB');

    await expect(page.locator('.custom-db-mongo-password').last()).toHaveValue('');
  });
});
