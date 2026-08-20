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
const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

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

  test('presets and custom connections are grouped under their own headings, in that order', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);

    const radioGroup = page.locator('#modalDbRadioGroup');
    const headings = radioGroup.locator('.radio-group-heading');
    await expect(headings).toHaveText([
      'Pre-configured Database Playgrounds', 'Custom Database Connections',
    ]);

    // Not just present - actually in this order: every preset option comes
    // before the "Custom Database Connections" heading, which itself comes
    // before customDbsContainer (the add button/custom rows).
    const children = await radioGroup.evaluate(el =>
      [...el.children].map(c => c.className)
    );
    const presetHeadingIdx = children.indexOf('radio-group-heading');
    const firstPresetIdx = children.indexOf('radio-option');
    const customHeadingIdx = children.findIndex(c => c.includes('radio-group-heading-custom'));
    const customContainerIdx = children.indexOf('custom-dbs-list');
    expect(presetHeadingIdx).toBeLessThan(firstPresetIdx);
    expect(firstPresetIdx).toBeLessThan(customHeadingIdx);
    expect(customHeadingIdx).toBeLessThan(customContainerIdx);
  });

  test('an anonymous Cloud Run visitor sees both the presets and custom-connections headings', async ({ page }) => {
    // Anonymous visitors can now save their own custom connections too
    // (see config_routes.py's handle_config) - the "Custom Database
    // Connections" heading and its "+ Add custom connection" control are no
    // longer hidden for this identity, even before they've saved one yet,
    // exactly like an authenticated user with zero saved connections still
    // sees the heading.
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: true,
          google_client_id: 'fake-client-id.apps.googleusercontent.com',
          session_id: 'e2e-session',
          user_id: 'anonymous:e2e-session',
          authenticated: false,
          is_cloud_run: true,
          configured_databases: [{ name: 'Default DB', type: 'postgres' }],
          active_preset_index: 0,
          default_database_url: '',
          active_database_url: '',
          active_database_type: '',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Default DB',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openConfigModal(page);

    await expect(page.locator('.radio-group-heading', { hasText: 'Pre-configured Database Playgrounds' })).toBeVisible();
    await expect(page.locator('.radio-group-heading', { hasText: 'Custom Database Connections' })).toBeVisible();
    await expect(page.locator('#addCustomDbBtn')).toBeVisible();
  });

  test("an anonymous Cloud Run visitor's own saved custom connection renders, selected, with real details shown", async ({ page }) => {
    // Unlike an admin preset's connection string/credentials (still
    // withheld from anonymous visitors - see the redacted
    // configured_databases below), a visitor's OWN custom connection is not
    // a secret from them: its real name/url round-trips, it renders as a
    // selected row, and the connection badge shows its real name rather
    // than the generic preset-label fallback.
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: true,
          google_client_id: 'fake-client-id.apps.googleusercontent.com',
          session_id: 'e2e-session',
          user_id: 'anonymous:e2e-session',
          authenticated: false,
          is_cloud_run: true,
          configured_databases: [{ name: 'Default DB', type: 'postgres' }],
          active_preset_index: null,
          default_database_url: '',
          active_database_url: 'postgresql://user:pass@localhost:5432/mydb',
          active_database_type: 'postgres',
          active_is_custom: true,
          active_custom_connection_key: 'anon-key-1',
          active_uses_custom_credentials: false,
          database_name: 'My Anonymous DB',
          custom_database_name: 'My Anonymous DB',
          custom_database_url: 'postgresql://user:pass@localhost:5432/mydb',
          custom_databases: [{
            connection_key: 'anon-key-1',
            name: 'My Anonymous DB',
            type: 'postgres',
            url: 'postgresql://user:pass@localhost:5432/mydb',
            config: {},
            has_custom_credentials: false,
          }],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await expect(page.locator('#connDbName')).toHaveText('My Anonymous DB');

    await openConfigModal(page);
    const row = page.locator('.custom-db-name-input').first();
    await expect(row).toHaveValue('My Anonymous DB');
    await expect(page.locator('input[name="db_connection_option"]:checked')).toHaveCount(1);
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

    // Re-open - a saved connection like this one now defaults to
    // collapsed (see the custom-db-toggle-btn coverage below), so expand
    // it before inspecting its fields.
    await openConfigModal(page);
    await page.locator('.custom-db-toggle-btn').last().click();

    // Inspect the full modal HTML - the key text must not appear
    // anywhere, and the key textarea must be blank (with a placeholder
    // indicating a key is already saved), never pre-filled.
    const modalHtml = await page.locator('#configModal').innerHTML();
    expect(modalHtml).not.toContain(keyJson);

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
    await openConfigModal(page);

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
    await openConfigModal(page);

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
    // Same client-side gate as the BigQuery case above (triggerConfigSave's
    // isCompleteSnowflake()) - and since the row never makes it into the
    // request, this never triggers the real-network-call risk described
    // above either (database_type stays 'postgres', falling back to the
    // untouched default preset).
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');
    await page.locator('.custom-db-sf-account').last().fill('some-account');
    await page.locator('.custom-db-sf-database').last().fill('some_db');
    // Deliberately leave user/warehouse/credential blank.

    await page.locator('#configSaveBtn').click();

    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await openConfigModal(page);
    await expect(page.locator('.custom-db-sf-account')).toHaveCount(0);
  });

  test('a custom connection row is visually enclosed between horizontal lines', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
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

  test('a Postgres custom row shows a labeled Name field on line 1 and a labeled URL field on line 2', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);

    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-header-row label.custom-db-field-label')).toHaveText('Name:');
    await expect(card.locator('.custom-db-field-row label.custom-db-field-label')).toHaveText('URL:');
  });

  test('a newly-added custom connection row starts expanded, showing its detail fields right away', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);

    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-url-input')).toBeVisible();
    await expect(card.locator('.custom-db-toggle-btn')).toHaveAttribute('aria-expanded', 'true');
  });

  test('a previously-saved custom connection collapses by default, showing only type/name plus expand and remove controls', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Collapsible DB');
    await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/collapsible');
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    // Re-open - this connection now came back from the server (it has a
    // connection_key), so it should default to collapsed: type/name still
    // visible, but the URL field hidden until expanded.
    await openConfigModal(page);
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
    await openConfigModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Removable DB');
    await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/removable');
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await openConfigModal(page);
    await expect(page.locator('.custom-db-card').last().locator('.custom-db-url-input')).toHaveCount(0);
    await page.locator('.custom-db-remove-btn').last().click();
    await expect(page.locator('.custom-db-name-input')).toHaveCount(0);
  });

  test('a BigQuery custom row labels each field on its own line: Project ID/Dataset, Billing Project ID, Service Account Key', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('bigquery');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows).toHaveCount(3);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Project ID:', 'Dataset:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Billing Project ID:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Service Account Key:']);
  });

  test('a Snowflake custom row labels each field on its own line: Warehouse/Database/Schema, Account/User/Role, Authentication Method, then the credential', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('snowflake');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows).toHaveCount(4);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Warehouse:', 'Database:', 'Schema: (optional)']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['Account:', 'User:', 'Role: (optional)']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Authentication Method:']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText(['Password:']);

    // Switching to key-pair auth swaps that last line for Private Key +
    // Passphrase, still both on their own (fifth) line.
    await page.locator('.custom-db-sf-auth-method').last().selectOption('private_key');
    const fieldRowsAfter = card.locator('.custom-db-field-row');
    await expect(fieldRowsAfter).toHaveCount(4);
    await expect(fieldRowsAfter.nth(3).locator('.custom-db-field-label')).toHaveText(['Private Key:', 'Passphrase: (if key is encrypted)']);
  });

  test('switching the active db connection clears the NL prompt, SQL, and results', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    // Save a second custom connection to switch to later - the default
    // preset is already active, so this gives us an actual "other"
    // connection to change to.
    await openConfigModal(page);
    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Other DB');
    await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/other');
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Other DB');

    // Populate the prompt/SQL/results the same way translate-execute.spec.js
    // does, then confirm they're actually showing before asserting they get
    // cleared below.
    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() =>
      page.evaluate(() => {
        const wrapper = document.querySelector('.CodeMirror');
        if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
        const textarea = document.getElementById('sqlQuery');
        return textarea ? textarea.value : null;
      })
    ).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
    await expect(page.locator('#aiPrompt')).toHaveValue('list users');

    // Now switch back to the default preset - this is the connection
    // change the prompt/SQL/results should be wiped on.
    await openConfigModal(page);
    await page.locator('#modalDbRadioGroup input[type="radio"]').first().check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await expect(page.locator('#aiPrompt')).toHaveValue('');
    expect(
      await page.evaluate(() => {
        const wrapper = document.querySelector('.CodeMirror');
        if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
        const textarea = document.getElementById('sqlQuery');
        return textarea ? textarea.value : null;
      })
    ).toBe('');
    await expect(page.locator('#resultsBody')).toBeEmpty();
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
  });

  test('re-saving the same active connection does not clear the NL prompt, SQL, or results', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() =>
      page.evaluate(() => {
        const wrapper = document.querySelector('.CodeMirror');
        if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
        const textarea = document.getElementById('sqlQuery');
        return textarea ? textarea.value : null;
      })
    ).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);

    // Open the modal and save again without changing the selection (e.g.
    // just toggling auto-execute) - the active connection identity is
    // unchanged, so nothing should be cleared.
    await openConfigModal(page);
    await page.locator('#modalDbRadioGroup input[type="radio"]').first().check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await expect(page.locator('#aiPrompt')).toHaveValue('list users');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
  });

  test('the Save button stays visible without scrolling even with many custom connections', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    // Each added row starts expanded (see the collapse-state tests above),
    // so this reliably produces enough height to overflow the modal.
    for (let i = 0; i < 12; i++) {
      const nameInput = await addCustomDbRow(page);
      await nameInput.fill(`DB ${i}`);
      await page.locator('.custom-db-url-input').last().fill(`postgresql://user:pass@localhost:5432/db${i}`);
    }

    // The DB connection list actually overflows and scrolls internally -
    // this isn't a meaningful test of the fix unless it does.
    const scrollContent = page.locator('#configForm .modal-scroll-content');
    const isScrollable = await scrollContent.evaluate(el => el.scrollHeight > el.clientHeight);
    expect(isScrollable).toBe(true);

    // Yet the Save button - outside that scrolling region, in the pinned
    // footer - is still fully visible within the modal card without any
    // scrolling.
    await expect(page.locator('#configSaveBtn')).toBeVisible();
    const saveBox = await page.locator('#configSaveBtn').boundingBox();
    const cardBox = await page.locator('#configModal .modal-card').boundingBox();
    expect(saveBox.y).toBeGreaterThanOrEqual(cardBox.y);
    expect(saveBox.y + saveBox.height).toBeLessThanOrEqual(cardBox.y + cardBox.height + 1);
  });

  test('the type dropdown offers PostgreSQL, MySQL, BigQuery, Snowflake, Databricks, and Oracle', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);

    const options = await page.locator('.custom-db-type-select').last().locator('option').allTextContents();
    expect(options).toEqual(['PostgreSQL', 'MySQL', 'BigQuery', 'Snowflake', 'Databricks', 'Oracle']);
  });

  test('adding a custom MySQL connection persists it and updates the badge', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);

    const nameInput = await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mysql');
    const urlInput = page.locator('.custom-db-url-input').last();
    await nameInput.fill('My MySQL DB');
    await urlInput.fill('mysql://user:pass@localhost:3306/mydb');

    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('My MySQL DB');

    // Re-opening confirms it round-tripped through the real server/state
    // store as a MySQL connection specifically, not silently as Postgres
    // (see config_routes.py's now-fixed dialect-fallback branches). The
    // URL field redisplays with its password masked (maskConnectionUrl in
    // client.js) - same as every other dialect's URL field - so the
    // expected value here has the password starred out too.
    await openConfigModal(page);
    await expect(page.locator('.custom-db-name-input').first()).toHaveValue('My MySQL DB');
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('mysql');
    await expect(page.locator('.custom-db-url-input').first()).toHaveValue('mysql://user:******@localhost:3306/mydb');
  });

  test('a MySQL custom row shows a labeled Name field on line 1 and a labeled URL field on line 2', async ({ page }) => {
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('mysql');

    const card = page.locator('.custom-db-card').last();
    await expect(card.locator('.custom-db-header-row label.custom-db-field-label')).toHaveText('Name:');
    await expect(card.locator('.custom-db-field-row label.custom-db-field-label')).toHaveText('URL:');
    await expect(card.locator('.custom-db-url-input')).toHaveAttribute('placeholder', /^mysql:\/\//);
  });

  test('switching a custom connection between Postgres and MySQL and Postgres again preserves the right type each time', async ({ page }) => {
    // Regression test for the bug this dialect's addition surfaced:
    // client.js used to treat "anything that isn't BigQuery/Snowflake" as
    // implicitly Postgres, so a MySQL selection would silently save as a
    // mislabeled Postgres connection (see triggerConfigSave's now-fixed
    // isCompleteSimpleUrlDb branch).
    await gotoApp(page);
    await openConfigModal(page);

    const nameInput = await addCustomDbRow(page);
    await nameInput.fill('Switchy DB');
    await page.locator('.custom-db-type-select').last().selectOption('mysql');
    await page.locator('.custom-db-url-input').last().fill('mysql://user:pass@localhost:3306/switchy');
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await openConfigModal(page);
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('mysql');

    // Switch it to Postgres and save again.
    await page.locator('.custom-db-type-select').first().selectOption('postgres');
    await page.locator('.custom-db-name-input').first().fill('Switchy DB');
    await page.locator('.custom-db-url-input').first().fill('postgresql://user:pass@localhost:5432/switchy');
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await openConfigModal(page);
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
    await openConfigModal(page);

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
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows).toHaveCount(4);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Server Hostname:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText(['HTTP Path:']);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['Catalog: (optional)', 'Schema: (optional)']);
    await expect(fieldRows.nth(3).locator('.custom-db-field-label')).toHaveText(['Access Token:']);
  });

  test('an incomplete custom Databricks row (no HTTP path / token) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake cases above
    // (triggerConfigSave's isCompleteDatabricks()) - and since the row
    // never makes it into the request, this never triggers the
    // real-network-call risk described above either (database_type stays
    // 'postgres', falling back to the untouched default preset).
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('databricks');
    await page.locator('.custom-db-dbx-hostname').last().fill('dbc-x.cloud.databricks.com');
    // Deliberately leave HTTP path/access token blank.

    await page.locator('#configSaveBtn').click();

    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await openConfigModal(page);
    await expect(page.locator('.custom-db-dbx-hostname')).toHaveCount(0);
  });

  test('the Databricks access token never round-trips back into the page', async ({ page }) => {
    // Mirrors the BigQuery service-account-key test above: an access token
    // is a credential, never redisplayed once saved, same as every other
    // dialect's credential field(s). The row is never actually submitted
    // here (see the network-call caution above) - this just pins that the
    // field starts, and stays, blank rather than ever showing a value.
    await gotoApp(page);
    await openConfigModal(page);
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
    await openConfigModal(page);

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
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    const card = page.locator('.custom-db-card').last();
    const fieldRows = card.locator('.custom-db-field-row');
    await expect(fieldRows).toHaveCount(4);
    await expect(fieldRows.nth(0).locator('.custom-db-field-label')).toHaveText(['Host:', 'Port:']);
    await expect(fieldRows.nth(1).locator('.custom-db-field-label')).toHaveText([
      'Service Name:', 'SID: (legacy)', 'Schema: (optional)',
    ]);
    await expect(fieldRows.nth(2).locator('.custom-db-field-label')).toHaveText(['User:', 'Password:']);
    await expect(fieldRows.nth(3)).toContainText('Use TLS (required for Oracle Cloud)');
  });

  test('the Oracle "Use TLS" checkbox starts checked (by default) and toggles off on click', async ({ page }) => {
    // Defaults ON - most Oracle connections added through this dialog
    // target Oracle Cloud, which requires TLS (see backends/oracle.py's
    // module docstring); a plain on-prem/XE listener is the exception,
    // not the common case, so it's opt-out rather than opt-in here.
    await gotoApp(page);
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    const sslCheckbox = page.locator('.custom-db-ora-ssl').last();
    await expect(sslCheckbox).toBeChecked();
    await sslCheckbox.click();
    await expect(sslCheckbox).not.toBeChecked();
  });

  test('an incomplete custom Oracle row (no service name/sid / password) is never submitted', async ({ page }) => {
    // Same client-side gate as the BigQuery/Snowflake/Databricks cases
    // above (triggerConfigSave's isCompleteOracle()) - and since the row
    // never makes it into the request, this never triggers the
    // real-network-call risk described above either (database_type stays
    // 'postgres', falling back to the untouched default preset).
    await gotoApp(page);
    await openConfigModal(page);

    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');
    await page.locator('.custom-db-ora-host').last().fill('db.example.com');
    await page.locator('.custom-db-ora-user').last().fill('alice');
    // Deliberately leave service name/SID and password blank.

    await page.locator('#configSaveBtn').click();

    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await openConfigModal(page);
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
    await openConfigModal(page);
    await addCustomDbRow(page);
    await page.locator('.custom-db-type-select').last().selectOption('oracle');

    await expect(page.locator('.custom-db-ora-password').last()).toHaveValue('');
  });
});
