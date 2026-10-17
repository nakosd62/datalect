// tests/e2e/dataset-picker.spec.js
//
// The DATASET PICK LIST (#datasetPickList, opened via #configTriggerBadge) -
// an in-place dropdown anchored directly under the badge, against the REAL
// Flask server and REAL SqliteStateStore - no mocking except where a test
// explicitly installs its own page.route() for /api/config's GET (several
// preset/dataset-group layout tests below do, to control exactly what
// CONFIGURED_DBS/CONFIGURED_DB_GROUPS the dropdown renders without needing a
// real presets file).
//
// This replaced the old #configModal popup dialog (formerly
// config-modal.spec.js, this file's own former name) per explicit request:
// clicking the badge no longer opens a modal with a two-column radio-button
// grid and a separate Save button - it drops a flat, directly-clickable
// option list right below the badge (built by renderDatasetPickList() in
// client.js), grouped under two headings, "Public" (every CONFIGURED_DBS
// preset, plus a "Composites" row that opens its own flyout submenu to the
// side listing every CONFIGURED_DB_GROUPS dataset group - see
// openDatasetPickCompositeSubmenu()/#datasetPickCompositeSubmenu, added per
// a later explicit request) and "Private" (this user's own saved custom
// connections) - same two-heading shape the old radio list used ("Public
// Datasources"/"Private Datasources"), just shorter headings and real
// `<button class="dataset-pick-option">` elements instead of radio inputs.
// The "Composites" row itself opens on hover alone (no click needed - a
// later explicit request), closing again a moment after the pointer leaves
// it and its flyout, or immediately once the pointer lands on a different
// datasource instead. Clicking ANY option (preset, private connection, or
// a group inside the "Composites" flyout) selects AND saves immediately
// (selectDataset() -> triggerConfigSave()) - there is no separate Save
// step/button any more at all. A second click on the badge, or a click
// outside #datasetPickerWrapper, closes the dropdown (and the "Composites"
// flyout with it) without changing anything.
//
// Since the "Bring Your Own Data" relocation (see webClient/client.js's
// renderDatasetPickList()/renderCustomDbRows() comments), the dropdown's own
// "Private" section is a plain, SELECT-ONLY list of names - same shape as
// the public options above it - so a user can still SELECT a saved custom
// connection here to activate it, but can no longer CREATE/EDIT/DELETE one
// from this dropdown. That happens exclusively in the Preferences dialog's
// own "Bring Your Own Data" section (#preferencesModal, opened via
// #prefsBtn) - see tests/e2e/preferences-byod.spec.js for all of that: every
// per-dialect field/label/incomplete-row/credential-never-round-trips/
// expand-collapse/remove-control test lives there, since none of it is
// reachable from this dropdown.
//
// What stays here is everything still genuinely about THIS dropdown: preset/
// dataset-group list contents (headings, order, tooltips), selecting/
// activating a connection (including a saved custom one, now only by name
// from its read-only row), "switching between connections preserves per-
// connection turn state" behavior, anonymous-visitor behavior, and the
// slow-liveness-check/busy-state/many-connections-scroll behaviors. A test
// here that needs an EXISTING saved custom connection to select against
// creates one first via createCustomConnectionViaPreferences() below (the
// Preferences-side save), then exercises this dropdown's own select-and-save
// behavior on it - mirroring the real two-step flow the UI requires: (1)
// save a connection's fields in Preferences, (2) separately select it by
// name from this dropdown to actually activate it.
//
// Saving a custom connection (even a BigQuery one with a throwaway fake
// service-account key, in the other file) never triggers a real network
// call to Postgres/BigQuery - config_routes.py's POST handler only
// validates and persists the descriptor, and the BigQuery Python client's
// constructor doesn't make a network call by itself (see
// backends/bigquery.py's connect()). This file sticks to plain Postgres/
// MySQL connections throughout, since nothing here needs a structured
// dialect's own fields - see preferences-byod.spec.js for that coverage.

// Uses testShowSqlVisible (aliased to `test`) rather than the plain `test`
// fixture - see that fixture's own comment in fixtures.js - since this
// suite drives the app through the SQL box itself (#runBtn), which
// client.js now hides by default.
const { testShowSqlVisible: test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Opens the dataset picker dropdown and waits for it to actually be
 * showing - #configTriggerBadge's click handler awaits a real, unmocked
 * fetch('/api/config') (fetchBackendConfig()) before it removes
 * #datasetPickList's hidden class, a genuine network round trip to the real
 * Flask dev server this suite runs against, not something page.route()
 * intercepts. The default 8s expect timeout is normally far more than that
 * needs, but in a loaded/constrained environment a single request can
 * occasionally take longer than that even though nothing is actually stuck -
 * give this one specific wait more headroom rather than loosening the
 * suite-wide default (a genuinely broken dropdown would still fail well
 * within this). */
async function openDatasetPickList(page) {
  await page.locator('#configTriggerBadge').click();
  await expect(page.locator('#datasetPickList')).not.toHaveClass(/hidden/, { timeout: 15_000 });
}

/** Opens the "Composites" flyout (#datasetPickCompositeSubmenu) from inside
 * an already-open dataset picker dropdown, and waits for it to actually be
 * showing - see client.js's openDatasetPickCompositeSubmenu(). Every
 * dataset-group option (data-value="group:...") renders inside this flyout
 * now, not inline among the ordinary presets - see renderDatasetPickList()'s
 * own comment on the "Composites" toggle row. Hovers rather than clicks -
 * opening on hover alone (no click needed) is the real, documented
 * behavior now (see that same comment) - a real test would miss a
 * regression back to click-only if it only ever used .click() here. */
async function openCompositeSubmenu(page) {
  await page.locator('#datasetPickCompositeToggle').hover();
  await expect(page.locator('#datasetPickCompositeSubmenu')).not.toHaveClass(/hidden/);
}

/** Creates ONE custom connection through the Preferences dialog's own
 * "Bring Your Own Data" section (the only place a custom connection can
 * still be created/edited since the Phase 1 relocation - see this file's
 * own module docstring) and saves it there, leaving #preferencesModal
 * closed again afterward. Deliberately does NOT select/activate it - the
 * dataset picker dropdown's own "Private" list is where a test then does
 * that, exactly mirroring the real two-step flow the UI requires. Only ever
 * used here with plain Postgres/MySQL connections (never a structured
 * dialect) - see preferences-byod.spec.js for every dialect's own field
 * coverage; this file only needs a connection to exist and be selectable,
 * never its particular fields. */
async function createCustomConnectionViaPreferences(page, { name, type = 'postgres', url = 'postgresql://user:pass@localhost:5432/mydb' } = {}) {
  await page.locator('#prefsBtn').click();
  await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });

  await page.locator('#addCustomDbBtn').click();
  const nameInput = page.locator('.custom-db-name-input').last();
  await expect(nameInput).toBeFocused();
  if (type !== 'postgres') {
    // Switching the type re-renders the row (see client.js's
    // custom-db-type-select change handler) - done BEFORE filling name/url
    // below so nothing typed is lost to that re-render.
    await page.locator('.custom-db-type-select').last().selectOption(type);
  }
  await page.locator('.custom-db-name-input').last().fill(name);
  await page.locator('.custom-db-url-input').last().fill(url);

  await page.locator('#preferencesSaveBtn').click();
  await expect(page.locator('#preferencesModal')).toHaveClass(/hidden/);
}

/** Intercept POST /api/summarize-result - single-connection mode's own
 * post-execution results summarization call (see translate-execute.spec.js's
 * own copy of this helper, and translate_routes.py's docstring on that
 * route). Any test here that runs a real translate+execute turn and then
 * reopens the dropdown afterward needs this mocked: client.js fires this as
 * a genuine follow-up LLM call once results land, and keeps
 * #configTriggerBadge disabled (its click handler no-ops while disabled)
 * until it settles - left unmocked, it's a real, slow/failing network call
 * in this suite's sandboxed environment, and a click on the still-disabled
 * badge right after execution silently does nothing, no matter how long a
 * test then waits for the dropdown that click was supposed to open. */
async function mockSummarizeResult(page) {
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
  });
}

test.describe('dataset picker', () => {
  test('opens showing at least the default preset, and closes', async ({ page }) => {
    await gotoApp(page);
    await openDatasetPickList(page);

    await expect(page.locator('#datasetPickOptions .dataset-pick-option').first()).toBeVisible();

    // A second click on the badge (same toggle pattern as the model pick
    // list) closes it again - there is no separate close button any more.
    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
  });

  test('presets and custom connections are grouped under their own headings, in that order', async ({ page }) => {
    await gotoApp(page);
    await createCustomConnectionViaPreferences(page, { name: 'My Private DB' });
    await openDatasetPickList(page);

    const optionsEl = page.locator('#datasetPickOptions');
    const headings = optionsEl.locator('.model-pick-group-heading');
    await expect(headings).toHaveText(['Public', 'Private']);

    // Not just present - actually in this order: the "Public" heading, then
    // its one preset option, then the "Private" heading, then its one
    // custom-connection option. Unlike the old modal's two-column
    // .preset-columns/.preset-column grid, this is a flat list now (see
    // renderDatasetPickList() in client.js - CONFIGURED_DBS/CONFIGURED_DB_
    // GROUPS/customDatabases are each just .map()+joined straight into the
    // same list, no column-splitting logic at all), so this checks the raw
    // child sequence directly rather than any column structure.
    const children = await optionsEl.evaluate(el => [...el.children].map(c => c.className));
    expect(children).toHaveLength(4);
    expect(children[0]).toBe('model-pick-group-heading');
    expect(children[1]).toContain('dataset-pick-option');
    expect(children[2]).toBe('model-pick-group-heading');
    expect(children[3]).toContain('dataset-pick-option');

    const presetLabels = optionsEl.locator('.dataset-pick-option[data-value^="preset:"] .model-pick-option-label');
    await expect(presetLabels).toHaveCount(1);
    const customLabels = optionsEl.locator('.dataset-pick-option[data-dbname] .model-pick-option-label');
    await expect(customLabels).toHaveText(['My Private DB']);
  });

  test('renders one option per configured preset, in CONFIGURED_DBS order', async ({ page }) => {
    // Replaces the old modal's four "preset columns split N/2 by count"
    // tests - that two-column LAYOUT no longer exists at all (the dropdown
    // is a flat list - see renderDatasetPickList() in client.js, which just
    // concatenates every preset then every dataset group into one list, no
    // column-splitting logic to test any more). What's left to verify is
    // simply that every configured preset renders, once each, in the
    // server's own order.
    const configuredDatabases = Array.from({ length: 5 }, (_, i) => ({
      id: `p-${i}`, name: `Playground ${i}`, type: 'postgres', dialect_name: 'PostgreSQL',
    }));
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          active_preset_id: 'p-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Playground 0',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    const presetLabels = page.locator('#datasetPickOptions .dataset-pick-option:not(.all-databases-option) .model-pick-option-label');
    await expect(presetLabels).toHaveText([
      'Playground 0', 'Playground 1', 'Playground 2', 'Playground 3', 'Playground 4',
    ]);
  });

  test('a dataset group option carries no explanatory text or tooltip of its own', async ({ page }) => {
    // This used to be a standalone <p class="all-databases-hint"> below the
    // two-column grid (back when this was the single "All Pre-Configured
    // Datasets" option), then a plain `title` attribute on each group's own
    // button once that moved out of the dropdown body - and then, per a
    // later explicit request made once the "Composites" flyout shipped, the
    // tooltip was dropped entirely: the flyout itself (opened from its own
    // clearly-labeled "Composites" row) already tells a group apart from an
    // ordinary preset, so no further on-hover explanation is needed.
    const configuredDatabases = [{ id: 'p-0', name: 'Solo Postgres', type: 'postgres', dialect_name: 'PostgreSQL' }];
    const configuredDatabaseGroups = [
      { id: 'grp-1', name: 'Everything', dataset_list: ['p-0'] },
    ];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          configured_database_groups: configuredDatabaseGroups,
          active_preset_id: 'p-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Solo Postgres',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    await expect(page.locator('#datasetPickOptions')).not.toContainText('Ask a question without picking a database first');
    await openCompositeSubmenu(page);
    await expect(page.locator('#datasetPickCompositeSubmenu')).not.toContainText('Ask a question without picking a database first');

    const groupOption = page.locator('.dataset-pick-option.all-databases-option');
    await expect(groupOption).not.toHaveAttribute('title', /.+/);
  });

  test('no dataset-group entries appear when no groups are configured', async ({ page }) => {
    // There is no built-in "every configured dataset" mode any more (see
    // server/app_config.py's "DATASET GROUPS" comment) - a deployment
    // with no dataset_group entries in DATABASE_PRESETS_FILE simply has no
    // combined-scope option at all, not a fallback "All" choice the way the
    // removed feature this replaces always offered one.
    const configuredDatabases = [
      { id: 'p-0', name: 'Solo Postgres', type: 'postgres', dialect_name: 'PostgreSQL' },
    ];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          active_preset_id: 'p-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Solo Postgres',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    const presetLabels = page.locator('#datasetPickOptions .dataset-pick-option:not(.all-databases-option) .model-pick-option-label');
    await expect(presetLabels).toHaveText(['Solo Postgres']);
    await expect(page.locator('.dataset-pick-option.all-databases-option')).toHaveCount(0);
    // No "Composites" toggle row either - renderDatasetPickList() omits it
    // entirely when CONFIGURED_DB_GROUPS is empty, rather than rendering an
    // empty flyout nothing can ever open into anything.
    await expect(page.locator('#datasetPickCompositeToggle')).toHaveCount(0);
  });

  test('one button renders per configured dataset group, inside the "Composites" flyout under Public', async ({ page }) => {
    // Groups no longer have any opt-in/opt-out flag on the member datasets
    // themselves (see server/app_config.py's "DATASET GROUPS" comment) -
    // whatever groups are configured each get their own button, named after
    // the group, regardless of how many datasets they each list.
    const configuredDatabases = [
      { id: 'p-0', name: 'Quarantined Postgres', type: 'postgres', dialect_name: 'PostgreSQL' },
      { id: 'p-1', name: 'Sales Postgres', type: 'postgres', dialect_name: 'PostgreSQL' },
    ];
    const configuredDatabaseGroups = [
      { id: 'grp-solo', name: 'Solo Group', dataset_list: ['p-0'] },
      { id: 'grp-both', name: 'Everything', dataset_list: ['p-0', 'p-1'] },
    ];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          configured_database_groups: configuredDatabaseGroups,
          active_preset_id: 'p-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Quarantined Postgres',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    // Still just one combined "Public" heading/list, with a single
    // "Composites" row for however many groups exist - groups don't each get
    // a heading/section of their own at the TOP level either (see
    // renderDatasetPickList()'s own comment in client.js on the toggle row).
    await expect(page.locator('#datasetPickOptions .model-pick-group-heading')).toHaveText(['Public', 'Private']);
    await expect(page.locator('#datasetPickCompositeToggle')).toHaveText('Composites');
    // The group buttons already exist in the DOM at this point (rendered
    // into #datasetPickCompositeOptions by renderDatasetPickList() up
    // front, same as everything else), just not visible - the flyout
    // that contains them is still collapsed, not absent.
    await expect(page.locator('.dataset-pick-option.all-databases-option').first()).not.toBeVisible();

    await openCompositeSubmenu(page);
    // Both groups render inside the flyout, no "(Composite)" suffix on
    // their names any more - the flyout itself is what marks them apart
    // from an ordinary preset now (per explicit request).
    await expect(page.locator('.dataset-pick-option.all-databases-option')).toHaveCount(2);
    await expect(page.locator('.dataset-pick-option.all-databases-option .model-pick-option-label')).toHaveText([
      'Solo Group', 'Everything',
    ]);
  });

  test('the "Composites" flyout opens on hover alone, with no click required', async ({ page }) => {
    const configuredDatabases = [
      { id: 'p-0', name: 'Quarantined Postgres', type: 'postgres', dialect_name: 'PostgreSQL' },
    ];
    const configuredDatabaseGroups = [
      { id: 'grp-1', name: 'Everything', dataset_list: ['p-0'] },
    ];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          configured_database_groups: configuredDatabaseGroups,
          active_preset_id: 'p-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Quarantined Postgres',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    await expect(page.locator('#datasetPickCompositeSubmenu')).toHaveClass(/hidden/);
    // Hover alone, no .click() anywhere in this test - see
    // openDatasetPickCompositeSubmenu()'s own mouseenter wiring in
    // client.js.
    await page.locator('#datasetPickCompositeToggle').hover();
    await expect(page.locator('#datasetPickCompositeSubmenu')).not.toHaveClass(/hidden/);
    await expect(page.locator('#datasetPickCompositeToggle')).toHaveAttribute('aria-expanded', 'true');

    // Moving the pointer onto a different datasource (an ordinary preset,
    // not the toggle) closes the flyout immediately - per explicit request.
    await page.locator('.dataset-pick-option[data-value="preset:p-0"]').hover();
    await expect(page.locator('#datasetPickCompositeSubmenu')).toHaveClass(/hidden/);
    await expect(page.locator('#datasetPickCompositeToggle')).toHaveAttribute('aria-expanded', 'false');

    // Re-opens on hovering the toggle again, and this time moving the
    // pointer INTO the flyout itself (not away to another datasource)
    // keeps it open rather than closing it.
    await page.locator('#datasetPickCompositeToggle').hover();
    await expect(page.locator('#datasetPickCompositeSubmenu')).not.toHaveClass(/hidden/);
    await page.locator('.dataset-pick-option.all-databases-option').first().hover();
    await expect(page.locator('#datasetPickCompositeSubmenu')).not.toHaveClass(/hidden/);
  });

  test('single-db presets stay inline under "Public" while the one dataset group sits in its own "Composites" flyout', async ({ page }) => {
    // A dataset group has no dialect of its own, and (per an explicit
    // request made alongside the "Composites" flyout's introduction) no more
    // "(Composite)" suffix on its name either - the flyout itself is what
    // marks it apart from an ordinary ("<type>"-suffix-free, per an earlier
    // explicit request) preset now, rather than a parenthetical on the name.
    const configuredDatabases = [
      { id: 'p-a', name: 'Sales Postgres', type: 'postgres', dialect_name: 'PostgreSQL' },
      { id: 'p-b', name: 'Marketing Warehouse', type: 'bigquery', dialect_name: 'Google BigQuery' },
    ];
    const configuredDatabaseGroups = [
      { id: 'grp-ab', name: 'Sales & Marketing', dataset_list: ['p-a', 'p-b'] },
    ];
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: configuredDatabases,
          configured_database_groups: configuredDatabaseGroups,
          active_preset_id: 'p-a',
          default_database_url: '',
          active_database_url: '',
          active_database_type: 'postgres',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Sales Postgres',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: [],
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    // No standalone "DATASET GROUPS" heading - just the one "Public"
    // heading covering presets and the "Composites" toggle alike, same as
    // when no groups are configured at all.
    await expect(page.locator('#datasetPickOptions .model-pick-group-heading')).toHaveText(['Public', 'Private']);

    const presetLabels = page.locator('#datasetPickOptions .dataset-pick-option:not(.all-databases-option) .model-pick-option-label');
    await expect(presetLabels).toHaveText(['Sales Postgres', 'Marketing Warehouse']);

    await openCompositeSubmenu(page);
    await expect(page.locator('.dataset-pick-option.all-databases-option .model-pick-option-label')).toHaveText(['Sales & Marketing']);
  });

  // NOTE: this used to also cover a "custom connections heading has a
  // security note that opens the Help modal" case - that whole note
  // (.custom-db-security-note/#customDbSecurityNoteHelpLink, "Your custom
  // connections are private and secure (see Documentation).") was removed
  // from index.html/client.js per explicit request during the Dataset ->
  // Datasource terminology rename, and nothing else in this dropdown opens
  // the Help modal from inside it - there's no longer a way to get the
  // dataset picker and #helpModal open at once through the real UI, so the
  // bringModalToFront()/z-index regression this test guarded is untestable
  // this way any more. This comment is left in its place rather than
  // silently dropping the coverage with no trace.

  test('an anonymous Cloud Run visitor sees both headings, with an empty-state nudge to Settings', async ({ page }) => {
    // Anonymous visitors can now save their own custom connections too
    // (see config_routes.py's handle_config) - the "Private" heading is no
    // longer hidden for this identity, even before they've saved one yet,
    // exactly like an authenticated user with zero saved connections still
    // sees the heading. There is no "+ Create new" control here any more at
    // all, though (see this file's own module docstring) - zero saved
    // connections renders the plain empty-state nudge toward Settings
    // instead (client.js's #openSettingsForByodBtn).
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
          configured_databases: [{ id: 'preset-0', name: 'Default DB', type: 'postgres' }],
          active_preset_id: 'preset-0',
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
    await openDatasetPickList(page);

    await expect(page.locator('.model-pick-group-heading', { hasText: 'Public' })).toBeVisible();
    await expect(page.locator('.model-pick-group-heading', { hasText: 'Private' })).toBeVisible();
    // #addCustomDbBtn still exists in the DOM (it's Preferences' own "+
    // Create new" button, rendered inside #customDbsContainer there - that
    // modal just isn't open) - scoped to #datasetPickOptions specifically
    // to confirm THIS dropdown never renders it.
    await expect(page.locator('#datasetPickOptions #addCustomDbBtn')).toHaveCount(0);
    const nudge = page.locator('#openSettingsForByodBtn');
    await expect(nudge).toBeVisible();

    // Clicking it closes the dataset picker and opens Preferences instead -
    // where creating a connection actually happens now.
    await nudge.click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
  });

  test("an anonymous Cloud Run visitor's own saved custom connection renders, selected, with its real name shown", async ({ page }) => {
    // Unlike an admin preset's connection string/credentials (still
    // withheld from anonymous visitors - see the redacted
    // configured_databases below), a visitor's OWN custom connection is not
    // a secret from them: its real name round-trips, it renders as the
    // selected option in the read-only list, and the connection badge shows
    // its real name rather than the generic preset-label fallback.
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
          configured_databases: [{ id: 'preset-0', name: 'Default DB', type: 'postgres' }],
          active_preset_id: null,
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
          // Lazily derived server-side from active_is_custom/
          // active_custom_connection_key above (see state_store.py's
          // _lazy_derive_in_scope) - this visitor's one active custom
          // connection is its sole in-scope entry, same as a real,
          // never-explicitly-saved session would report.
          in_scope_preset_ids: [],
          in_scope_custom_connection_keys: ['anon-key-1'],
          max_in_scope_connections: 20,
        }),
      });
    });

    await gotoApp(page);
    await expect(page.locator('#connDbName')).toHaveText('My Anonymous DB');

    await openDatasetPickList(page);
    // No more editable name field here (see this file's own module
    // docstring) - just the same plain, select-only option every preset
    // gets, already shown selected since it's the active connection.
    const customOption = page.locator('.dataset-pick-option[data-dbname="My Anonymous DB"]');
    await expect(customOption).toHaveClass(/selected/);
    await expect(customOption).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('.dataset-pick-option.selected')).toHaveCount(1);
  });

  test('selecting a saved private Postgres datasource by name activates it and updates the badge', async ({ page }) => {
    await gotoApp(page);
    await createCustomConnectionViaPreferences(page, { name: 'My Postgres DB' });

    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-dbname="My Postgres DB"]').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('My Postgres DB');
  });

  test('selecting a saved private MySQL datasource activates it, correctly identified as MySQL not Postgres', async ({ page }) => {
    // Regression test for the bug this dialect's addition surfaced:
    // triggerConfigSave()'s "custom-<index>" branch reconstructs the
    // dialect-specific payload fresh from customDatabases[index] every time
    // a saved connection is actually SELECTED here - used to treat
    // "anything that isn't BigQuery/Snowflake" as implicitly Postgres, so
    // selecting a saved MySQL connection could silently activate it as a
    // mislabeled Postgres connection (see its now-fixed
    // isCompleteSimpleUrlDb branch). The Preferences-side persistence half
    // of this same regression (saving it AS MySQL in the first place) is
    // covered in preferences-byod.spec.js.
    await gotoApp(page);
    await createCustomConnectionViaPreferences(page, {
      name: 'My MySQL DB', type: 'mysql', url: 'mysql://user:pass@localhost:3306/mydb',
    });

    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-dbname="My MySQL DB"]').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('My MySQL DB');

    // Reopening Preferences (the only place a saved row's own type/URL
    // still shows - see this file's own module docstring) confirms it's
    // genuinely stored, and now active, as MySQL specifically, not
    // Postgres.
    await page.locator('#prefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
    await page.locator('.custom-db-toggle-btn').first().click();
    await expect(page.locator('.custom-db-type-select').first()).toHaveValue('mysql');
  });

  // Turn history is now bucketed by (identity, active connection) - see
  // client.js's chatStoresByBucket/computeBucketKey()/
  // reconcileActiveHistoryBucket(). Switching the active connection no
  // longer force-clears the on-screen prompt/SQL/results the way it used
  // to (clearActiveQueryState() isn't called on a connection switch any
  // more); it switches to that connection's OWN bucket instead - blank if
  // this page load has never visited it before (which is what this test
  // below actually exercises, and still coincidentally looks identical to
  // the old "clears" behavior for that reason), or that connection's own
  // last turn, restored, if it has (see the test right after this one).
  test('switching to a connection not yet visited this page load starts with a blank NL prompt, SQL, and results', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await mockSummarizeResult(page);
    await gotoApp(page);

    // Save a second custom connection to switch to later (via
    // Preferences - see this file's own module docstring), then select and
    // activate it here - the default preset is already active, so this
    // gives us an actual "other" connection to change to.
    await createCustomConnectionViaPreferences(page, { name: 'Other DB', url: 'postgresql://user:pass@localhost:5432/other' });
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-dbname="Other DB"]').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
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
    // change the prompt/SQL/results should be wiped on. Matched by its
    // data-value, not .first() of everything - the group options (if any)
    // sort before/after presets in this merged list, so matching the
    // "preset:" prefix specifically keeps this test from depending on that
    // order.
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-value^="preset:"]').first().click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
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

  test('switching away from a connection and back restores that connection\'s own prior turn', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await mockSummarizeResult(page);
    await gotoApp(page);

    // Populate a turn on the default preset - the connection this test
    // switches away from and back to.
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

    // Switch to a second, different connection (saved via Preferences)
    // and give it its own turn.
    await createCustomConnectionViaPreferences(page, { name: 'Other DB', url: 'postgresql://user:pass@localhost:5432/other' });
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-dbname="Other DB"]').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Other DB');
    await expect(page.locator('#aiPrompt')).toHaveValue('');

    await mockTranslate(page, { sql: 'SELECT * FROM orders;' });
    await mockExecute(page, {
      results: [{ columns: ['order_id'], rows: [{ order_id: 7 }], rowCount: 1 }],
    });
    await page.locator('#aiPrompt').fill('list orders');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() =>
      page.evaluate(() => {
        const wrapper = document.querySelector('.CodeMirror');
        if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
        const textarea = document.getElementById('sqlQuery');
        return textarea ? textarea.value : null;
      })
    ).toContain('orders');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['order_id']);

    // Switch back to the default preset - its own earlier turn should
    // reappear exactly as it was left, not blank and not "Other DB"'s turn.
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-value^="preset:"]').first().click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Default DB');

    await expect(page.locator('#aiPrompt')).toHaveValue('list users');
    expect(
      await page.evaluate(() => {
        const wrapper = document.querySelector('.CodeMirror');
        if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
        const textarea = document.getElementById('sqlQuery');
        return textarea ? textarea.value : null;
      })
    ).toContain('users');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);

    // And switching back to "Other DB" once more should restore ITS turn.
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-dbname="Other DB"]').click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    await expect(page.locator('#connDbName')).toHaveText('Other DB');

    await expect(page.locator('#aiPrompt')).toHaveValue('list orders');
    await expect(page.locator('#resultsHeader th')).toHaveText(['order_id']);
  });

  test('re-saving the same active connection does not clear the NL prompt, SQL, or results', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await mockSummarizeResult(page);
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

    // Open the dropdown and click the already-active connection again - the
    // active connection identity is unchanged, so nothing should be
    // cleared. Matched by its data-value prefix, not .first() of
    // everything - see the test above for why.
    await openDatasetPickList(page);
    await page.locator('.dataset-pick-option[data-value^="preset:"]').first().click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);

    await expect(page.locator('#aiPrompt')).toHaveValue('list users');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
  });

  test("the dropdown's own option list scrolls internally with many custom connections, without the dropdown itself growing past its own max-height", async ({ page }) => {
    // The old modal's "Save button stays visible without scrolling" test
    // guarded a pinned-footer layout (a scrolling list region with a Save
    // button pinned outside it) that no longer exists - selection IS save
    // now, so there's no separate footer control to keep visible any more.
    // What's left to guard is simpler: #datasetPickList itself is one
    // simple `overflow-y: auto; max-height: ...` box (see .dataset-pick-list
    // in style.css) - it should scroll internally once there are enough
    // entries to overflow it, rather than growing without bound.
    const customDatabases = Array.from({ length: 40 }, (_, i) => ({
      connection_key: `key-${i}`, name: `DB ${i}`, type: 'postgres',
      url: `postgresql://user:pass@localhost:5432/db${i}`, config: {}, has_custom_credentials: false,
    }));
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          auth_enabled: false,
          session_id: 'e2e-session',
          user_id: 'global',
          authenticated: false,
          is_cloud_run: false,
          configured_databases: [{ id: 'preset-0', name: 'Default DB', type: 'postgres' }],
          active_preset_id: 'preset-0',
          default_database_url: '',
          active_database_url: '',
          active_database_type: '',
          active_is_custom: false,
          active_custom_connection_key: '',
          active_uses_custom_credentials: false,
          database_name: 'Default DB',
          custom_database_name: '',
          custom_database_url: '',
          custom_databases: customDatabases,
          auto_sql_execute: false,
        }),
      });
    });

    await gotoApp(page);
    await openDatasetPickList(page);

    const pickList = page.locator('#datasetPickList');
    // The dropdown actually overflows and scrolls internally - this isn't a
    // meaningful test of its own max-height unless it does.
    const isScrollable = await pickList.evaluate(el => el.scrollHeight > el.clientHeight);
    expect(isScrollable).toBe(true);

    // Yet the dropdown's own bounding box never exceeds its CSS max-height
    // (`min(400px, 70vh)`, see .dataset-pick-list in style.css) - the
    // overflow is absorbed by its internal scroll, not by the box itself
    // growing to fit every one of the 40 custom connections above.
    const box = await pickList.boundingBox();
    const maxHeightPx = await pickList.evaluate(el => parseFloat(getComputedStyle(el).maxHeight));
    expect(box.height).toBeLessThanOrEqual(maxHeightPx + 1);
  });

  // Regression coverage for the modal-sluggishness fix: client.js's
  // checkDbStatus() (the real /api/ping liveness check driving the header
  // dot) used to be `await`ed by updateConnectionDetails() - called both
  // when the dropdown opens and from triggerConfigSave() - so a slow/
  // unreachable connection made it hang open/closed for however long
  // /api/ping took. It's now fired without awaiting it, so the dropdown
  // opens/closes immediately and the dot updates whenever the check
  // actually finishes. These install a deliberately slow /api/ping (well
  // past any reasonable UI-blocking threshold) AFTER the initial page
  // load's own ping (mocked instantly by the `test` fixture's default) to
  // isolate what's being tested here from gotoApp()'s own wait on that
  // first ping.
  test('opening the dataset picker does not block on a slow liveness check', async ({ page }) => {
    await gotoApp(page);

    let resolvePing;
    const pingStarted = new Promise((resolve) => { resolvePing = resolve; });
    await page.route('**/api/ping', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      resolvePing();
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    const start = Date.now();
    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#datasetPickList')).not.toHaveClass(/hidden/);
    const elapsedMs = Date.now() - start;

    // The dropdown became visible well before the 3s slow ping could have
    // resolved - proves the open isn't gated on it.
    expect(elapsedMs).toBeLessThan(2000);

    // The slow ping is genuinely in flight (not skipped) - the dot should
    // be showing the "checking" state, not a stale connected/disconnected
    // one, until it resolves.
    await pingStarted;
    await expect(page.locator('#connDbDot')).toHaveClass(/checking/);

    // Once the slow ping actually completes, the dot updates in place.
    await expect(page.locator('#connDbDot')).toHaveClass(/connected/, { timeout: 5000 });
  });

  test('saving the config does not block on a slow liveness check', async ({ page }) => {
    await gotoApp(page);
    await openDatasetPickList(page);

    await page.route('**/api/ping', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await new Promise((r) => setTimeout(r, 3000));
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    const firstPreset = page.locator('.dataset-pick-option[data-value^="preset:"]').first();

    const start = Date.now();
    await firstPreset.click();
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
    const elapsedMs = Date.now() - start;

    // The dropdown closed well before the 3s slow ping could have resolved -
    // proves selecting an option isn't gated on it either.
    expect(elapsedMs).toBeLessThan(2000);
  });

  // New coverage introduced by this exact conversion: selectDataset() (the
  // click handler behind every option in this dropdown) shows a genuine
  // "busy" state - #datasetPickList gets a dataset-pick-list-busy class
  // (dimmed, pointer-events: none - see its own comment in style.css) and
  // #connDbDot flips to "checking" - for as long as triggerConfigSave()'s
  // own POST /api/config is actually in flight, unlike selectModel()'s
  // equivalent in the model pick list, which never shows a busy state at
  // all (a model switch is just a preference flip - picking a brand-new
  // custom connection can mean real schema introspection server-side, see
  // selectDataset()'s own docstring). Slowing down POST /api/config itself
  // (not just /api/ping, which the test above already covers) is what
  // actually keeps this busy window open long enough to observe.
  test('the dropdown shows a busy state while a selection is actually being saved, and clears it once the save lands', async ({ page }) => {
    await gotoApp(page);
    await openDatasetPickList(page);

    let resolveSaveStarted;
    const saveStarted = new Promise((resolve) => { resolveSaveStarted = resolve; });
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      resolveSaveStarted();
      await new Promise((r) => setTimeout(r, 1500));
      return route.fallback();
    });

    const firstPreset = page.locator('.dataset-pick-option[data-value^="preset:"]').first();
    await firstPreset.click();

    await saveStarted;
    await expect(page.locator('#datasetPickList')).toHaveClass(/dataset-pick-list-busy/);
    await expect(page.locator('#connDbDot')).toHaveClass(/checking/);

    // Once the slow save actually lands, the busy state clears and the
    // dropdown closes, same as any ordinary (fast) selection.
    await expect(page.locator('#datasetPickList')).not.toHaveClass(/dataset-pick-list-busy/, { timeout: 5000 });
    await expect(page.locator('#datasetPickList')).toHaveClass(/hidden/);
  });
});
