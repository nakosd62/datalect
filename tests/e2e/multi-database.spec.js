// tests/e2e/multi-database.spec.js
//
// Multi-database question-answering (see server/translate_routes.py's
// module docstring): the connection picker's binary single-select choice
// (one specific connection, or "All configured databases" - see
// renderDbRadioButtons() in client.js), the per-tab database labeling and
// '-- database: ...' SQL comments a multi-connection /api/translate
// response drives (its own disclosure mechanism - there is no separate
// banner), a follow-up request echoing back the prior turn's pin, and
// picking a specific connection away from a currently-pinned one
// mid-conversation clearing the active query state.
//
// GET/POST /api/config are both fully mocked here (same "no real-network-
// risk" reasoning config-modal.spec.js's anonymous-visitor tests already
// use - see that file's module docstring) rather than exercised against
// the real single-preset local-dev server this suite otherwise runs
// against (see playwright.config.js: one shared server process, no
// DATABASE_PRESETS_FILE, so genuinely 2+ real presets aren't available
// here) - this is a client-only exercise of the checkbox/banner/pin
// wiring, with the real per-field validation already covered server-side
// by tests/server/test_connection_scope.py.

const { test, expect, gotoApp } = require('./fixtures');

function buildConfigState() {
  return {
    auth_enabled: false,
    session_id: 'e2e-session',
    user_id: 'global',
    authenticated: false,
    is_cloud_run: false,
    configured_databases: [
      { id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
      { id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
    ],
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
    in_scope_preset_ids: ['p-a', 'p-b'],
    in_scope_custom_connection_keys: [],
    in_scope_mode: 'all',
    max_in_scope_connections: 20,
  };
}

/** Wires up GET/POST /api/config against an in-memory `state` object that
 * starts as buildConfigState() (or whatever `initial` overrides) - POST
 * merges in_scope_preset_ids/in_scope_custom_connection_keys/in_scope_mode
 * (each independently, when present in the request body) into `state` and
 * returns it, so a test can Save from the modal and then re-open it (or
 * trigger a translate call) against the just-saved scope, same round-trip
 * shape the real server gives, without needing genuinely-configured
 * presets. Returns the live `state` object so a test can inspect what was
 * last saved - including `state._lastPostBody`, the raw request body of
 * the most recent POST. Selecting "All" saves with neither
 * in_scope_preset_ids nor in_scope_custom_connection_keys present in the
 * body at all (mirroring config_routes.py's real "both absent means leave
 * the existing scope alone" behavior) but DOES send in_scope_mode: 'all' -
 * see client.js's triggerConfigSave() - which this mock mirrors into
 * `state.in_scope_mode` exactly like the real backend now persists it (see
 * server/config_routes.py's in_scope_mode handling), since that field, not
 * the in-scope arrays' length, is what isAllConnectionsSelected() actually
 * reads. */
async function mockConfig(page, initial) {
  const state = initial || buildConfigState();
  await page.route('**/api/config', async (route) => {
    const method = route.request().method();
    if (method === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
      return;
    }
    if (method === 'POST') {
      const body = route.request().postDataJSON() || {};
      state._lastPostBody = body;
      if (body.in_scope_preset_ids !== undefined) state.in_scope_preset_ids = body.in_scope_preset_ids;
      if (body.in_scope_custom_connection_keys !== undefined) {
        state.in_scope_custom_connection_keys = body.in_scope_custom_connection_keys;
      }
      if (body.in_scope_mode !== undefined) state.in_scope_mode = body.in_scope_mode;
      // Mirrors config_routes.py's real POST handler just enough for the
      // badge/primary-connection fields to reflect a preset switch (see
      // client.js's triggerConfigSave(), which always sends preset_id for
      // a preset pick, "all" synthesized down to the first configured
      // preset included) - without this, picking a different preset in
      // the modal wouldn't change active_preset_id/database_name in the
      // mocked GET/POST response that follows.
      if (body.preset_id !== undefined) {
        state.active_preset_id = body.preset_id;
        state.active_is_custom = false;
        state.active_custom_connection_key = '';
        const matched = state.configured_databases.find(db => db.id === body.preset_id);
        state.database_name = matched ? matched.name : state.database_name;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
      return;
    }
    return route.fallback();
  });
  return state;
}

async function openConfigModal(page) {
  await page.locator('#configTriggerBadge').click();
  // See config-modal.spec.js's own openConfigModal() for why this needs a
  // longer-than-default timeout: the click handler awaits a real, unmocked
  // fetch('/api/config') before revealing the modal, and the default 8s
  // expect timeout can occasionally be too tight for that one real
  // round trip under a loaded/constrained environment even though nothing
  // is actually stuck.
  await expect(page.locator('#configModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
}

function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

/** client.js's setSqlQuery() also runs generated SQL through sql-formatter
 * (a CDN script - see index.html) when it's available, pretty-printing it
 * onto multiple lines (e.g. "SELECT 1;" becomes "SELECT\n  1;") - see
 * translate-execute.spec.js's identical helper. Whether that happens is
 * purely a function of whether that CDN script loaded, not anything this
 * suite controls, so any assertion checking for more than one token of
 * generated SQL (as opposed to a single word like 'SELECT', or a whole
 * "-- database: ..." comment line, which the formatter leaves alone) needs
 * to go through this normalized-whitespace form instead of currentSql()
 * directly. */
async function normalizedSql(page) {
  return (await currentSql(page) || '').replace(/\s+/g, ' ').trim();
}

test.describe('multi-database question answering', () => {
  test('"All configured databases" and a specific preset are mutually exclusive radios, each saving the right scope', async ({ page }) => {
    const state = await mockConfig(page, {
      ...buildConfigState(), in_scope_preset_ids: ['p-a'], in_scope_custom_connection_keys: [], in_scope_mode: 'single',
    });
    await gotoApp(page);
    await openConfigModal(page);

    // "All" (rendered last, below the custom connections - see
    // renderDbRadioButtons() in client.js) + the two presets (p-a, p-b) - a
    // true single-select radio group again, not the checkbox picker this
    // replaced.
    const allRadio = page.locator('input[name="db_connection_option"][value="all"]');
    const boxes = page.locator('input[name="db_connection_option"]');
    const presetA = page.locator('input[name="db_connection_option"][value="preset:p-a"]');
    const presetB = page.locator('input[name="db_connection_option"][value="preset:p-b"]');
    await expect(boxes).toHaveCount(3);
    await expect(allRadio).not.toBeChecked();
    await expect(presetA).toBeChecked(); // p-a, today's only in-scope preset

    // Picking "All" unchecks whichever specific preset was selected -
    // plain native radio exclusivity, no client bookkeeping involved.
    await allRadio.check();
    await expect(presetA).not.toBeChecked();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    expect(state._lastPostBody.in_scope_mode).toBe('all');

    // Picking a specific preset again narrows straight back down to just
    // that one - in_scope_mode flips back to 'single' and the in-scope
    // arrays are sent as exactly that one connection.
    await openConfigModal(page);
    await presetB.check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    expect(state._lastPostBody.in_scope_mode).toBe('single');
    expect(state.in_scope_preset_ids).toEqual(['p-b']);
    expect(state.in_scope_custom_connection_keys).toEqual([]);
  });

  test('the connection badge reads "All databases" whenever 2+ are in scope, and the single name once only one remains', async ({ page }) => {
    const state = await mockConfig(page);
    await gotoApp(page);

    // buildConfigState() starts with both p-a and p-b in scope (as if a
    // prior "All" save, or a session that predates this binary choice) -
    // the badge should say "All databases", not just the primary's
    // ("Sales Postgres") name, since showing one name would hide that the
    // other connection is also in play for this session's questions.
    await expect(page.locator('#connDbName')).toHaveText('All databases');
    await expect(page.locator('#configTriggerBadge')).toHaveAttribute(
      'title', 'In scope: Sales Postgres, Marketing Postgres (Click to configure)');

    // Picking a specific preset (p-b) narrows scope back down to just that
    // one connection and reverts the badge to its actual name.
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="preset:p-b"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    expect(state.in_scope_preset_ids).toEqual(['p-b']);

    await expect(page.locator('#connDbName')).toHaveText('Marketing Postgres');
    await expect(page.locator('#configTriggerBadge')).toHaveAttribute(
      'title', 'Connected to: Marketing Postgres (Click to configure)');
  });

  test('the badge reads "All databases" for a real in_scope_mode "all" session even when the leftover in-scope arrays are short', async ({ page }) => {
    // Regression guard: a session that saved "All" leaves
    // in_scope_preset_ids/in_scope_custom_connection_keys untouched (see
    // triggerConfigSave() - "all" mode ignores them entirely, see db.py's
    // resolve_in_scope_descriptors), so they can be arbitrarily short - even
    // a single leftover entry from whatever was picked before "All" was
    // last selected. The badge must still read "All databases" here,
    // straight off in_scope_mode, not off those arrays' length (which is
    // exactly what summarizeInScopeConnections() once got wrong).
    await mockConfig(page, {
      ...buildConfigState(),
      in_scope_mode: 'all',
      in_scope_preset_ids: ['p-a'],
      in_scope_custom_connection_keys: [],
    });
    await gotoApp(page);

    await expect(page.locator('#connDbName')).toHaveText('All databases');
    await expect(page.locator('#configTriggerBadge')).toHaveAttribute(
      'title', 'In scope: Sales Postgres, Marketing Postgres (Click to configure)');

    await openConfigModal(page);
    const allRadio = page.locator('input[name="db_connection_option"][value="all"]');
    await expect(allRadio).toBeChecked();
  });

  test('a mocked multi-connection translate response labels result tabs by database and tags the SQL with database comments', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    expect(await currentSql(page)).toContain('-- database: preset:p-a (Sales Postgres)');
    expect(await currentSql(page)).toContain('-- database: preset:p-b (Marketing Postgres)');

    await page.locator('#runBtn').click();
    // Scoped to #resultsTabsNav rather than an unscoped .result-tab-btn
    // locator - kept explicit even though the History modal's own former
    // tab switcher (which used to share this same class) is gone now, so a
    // future feature reusing .result-tab-btn elsewhere doesn't silently
    // make this assertion pass for the wrong reason again.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('Sales Postgres');
    await expect(tabs.nth(1)).toContainText('Marketing Postgres');
  });

  // Narrow viewport, wrapped in its own describe/test.use, so the two
  // longer-named tabs below are guaranteed to add up to wider than the
  // available header width regardless of the host machine's font
  // rendering - the whole point of this test is the overflow, so it can't
  // depend on how much room a default-sized viewport happens to leave.
  test.describe('database-name tab labels (width/wrapping)', () => {
    test.use({ viewport: { width: 420, height: 700 } });

    test('a long database name renders on its own unwrapped line, without brackets, and the tab strip scrolls instead of shrinking tabs to fit', async ({ page }) => {
      await mockConfig(page);
      await gotoApp(page);

      const longName = 'Marketing Analytics Data Warehouse Postgres Production';
      await page.route('**/api/translate', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            sql:
              `-- database: preset:p-a (${longName})\nSELECT 1;\n\n` +
              '-- database: preset:p-b (Marketing Postgres)\nSELECT 2;',
            connection_selection: [
              { kind: 'preset', id: 'p-a', name: longName },
              { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
            ],
          }),
        });
      });
      await page.route('**/api/execute', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            success: true,
            results: [
              { statement: 'SELECT 1', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
                database: { kind: 'preset', id: 'p-a', name: longName } },
              { statement: 'SELECT 2', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
                database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
            ],
          }),
        });
      });

      await page.locator('#aiPrompt').fill('long db name check');
      await page.locator('#aiPrompt').press('Enter');
      await expect.poll(() => currentSql(page)).toContain('SELECT');
      await page.locator('#runBtn').click();

      // Scoped to #resultsTabsNav - see the identical note on the
      // "labels result tabs by database" test above.
      const tabs = page.locator('#resultsTabsNav .result-tab-btn');
      await expect(tabs).toHaveCount(2);

      // Change 1: no brackets around the database name.
      await expect(tabs.nth(0)).toContainText(longName);
      await expect(tabs.nth(0)).not.toContainText('[');
      await expect(tabs.nth(0)).not.toContainText(']');

      // The name sits on its own first line, in full - not wrapped
      // mid-word the way it would be if still squeezed into a fixed
      // max-width box.
      const firstLine = await tabs.nth(0).evaluate((el) => el.textContent.split('\n')[0]);
      expect(firstLine).toBe(longName);

      // Change 2: the tab itself is at least as wide as the name it's
      // showing - a clamped tab would instead stay pinned at min-width
      // regardless of how long the name is.
      const measured = await tabs.nth(0).evaluate((el) => {
        const span = document.createElement('span');
        const cs = getComputedStyle(el);
        span.style.font = cs.font;
        span.style.whiteSpace = 'pre';
        span.style.position = 'absolute';
        span.style.visibility = 'hidden';
        span.textContent = el.textContent.split('\n')[0];
        document.body.appendChild(span);
        const textWidth = span.getBoundingClientRect().width;
        span.remove();
        return { tabWidth: el.getBoundingClientRect().width, textWidth };
      });
      expect(measured.tabWidth).toBeGreaterThanOrEqual(measured.textWidth);

      // ...and with two such tabs now wider than this narrow viewport can
      // show side by side, the tab strip scrolls horizontally rather than
      // squeezing either of them down to fit.
      const overflows = await page.locator('#resultsTabsNav').evaluate(
        (el) => el.scrollWidth > el.clientWidth
      );
      expect(overflows).toBe(true);
    });
  });

  test('a follow-up question echoes back pinned_connections matching the prior turn\'s pick', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    let translateCallCount = 0;
    const capturedBodies = [];
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      capturedBodies.push(route.request().postDataJSON());
      translateCallCount += 1;
      if (translateCallCount === 1) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            sql: '-- database: preset:p-a (Sales Postgres)\nSELECT 1;',
            connection_selection: [{ kind: 'preset', id: 'p-a', name: 'Sales Postgres' }],
          }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            sql: '-- database: preset:p-a (Sales Postgres)\nSELECT 2;',
            connection_selection: [{ kind: 'preset', id: 'p-a', name: 'Sales Postgres' }],
          }),
        });
      }
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, results: [] }) });
    });

    await page.locator('#aiPrompt').fill('first question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 1');

    await page.locator('#aiPrompt').fill('follow-up question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT 2');

    expect(capturedBodies).toHaveLength(2);
    expect(capturedBodies[0].pinned_connections).toEqual([]);
    expect(capturedBodies[1].pinned_connections).toEqual([{ kind: 'preset', id: 'p-a' }]);
  });

  test('narrowing scope away from a currently-pinned connection mid-conversation clears prompt/SQL/results', async ({ page }) => {
    const state = await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          connection_selection: [{ kind: 'preset', id: 'p-b', name: 'Marketing Postgres' }],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 1 }], rowCount: 1 }],
        }),
      });
    });
    // The translate response above resolves to a single connection (p-b),
    // not a genuine multi-connection fan-out - so post-execution, client.js
    // takes single-connection mode's summarization path
    // (POST /api/summarize-result, singular) rather than all-mode's Phase C
    // (/api/summarize-results, plural, mocked elsewhere in this file for
    // the genuinely-multi-connection tests). Left unmocked, that's a real,
    // unmocked LLM call that keeps #configTriggerBadge disabled until it
    // settles - the openConfigModal() click below would silently no-op if
    // it lands while that's still in flight.
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    await page.locator('#aiPrompt').fill('campaigns question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['x']);
    await expect(page.locator('#aiPrompt')).toHaveValue('campaigns question');

    // Now pick p-a specifically, narrowing scope away from "All" down to
    // just p-a - the pin from above (p-b) no longer describes an in-scope
    // connection.
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="preset:p-a"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    expect(state.in_scope_preset_ids).toEqual(['p-a']);

    await expect(page.locator('#aiPrompt')).toHaveValue('');
    expect(await currentSql(page)).toBe('');
    await expect(page.locator('#resultsBody')).toBeEmpty();
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
  });

  // --- "all databases" mode's 2-phase triage/Phase-B redesign: router_route ---
  //
  // These mock /api/translate's NEW "route" outcome shape (see
  // translate_routes.py's module docstring and connection_router.py's
  // triage_all_mode_question): `router_route: true`, `routing_message`,
  // `database_notes`, `generation_failures`, alongside the existing
  // `connection_selection`/`sql` fields the test above already covers.
  // Distinct from that "answer"/legacy shape - a `router_route: true`
  // response is byte-different (new fields), not just a different `sql`
  // value.

  test('a router_route response with real SQL for two databases plus a note renders a leading Summary tab, a Note tab, and both result tabs', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres, Marketing Postgres, and Support Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [
            { kind: 'preset', id: 'p-c', name: 'Support Postgres', text: 'Support Postgres has nothing relevant to this question.' },
          ],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
            { kind: 'preset', id: 'p-c', name: 'Support Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals, campaigns, and support tickets');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(4);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Support Postgres');
    await expect(tabs.nth(1)).toContainText('Note');
    await expect(tabs.nth(2)).toContainText('Sales Postgres');
    await expect(tabs.nth(3)).toContainText('Marketing Postgres');

    // Default tab (no failures at all here) is the leading Summary tab,
    // showing the routing message as plain text.
    await expect(page.locator('.response-text')).toContainText(
      'Checking Sales Postgres, Marketing Postgres, and Support Postgres.');

    // The Note tab shows its own database-tagged text.
    await tabs.nth(1).click();
    await expect(page.locator('.response-text')).toContainText('Support Postgres has nothing relevant');
  });

  test('after execution, a Phase C summary is fetched and appended underneath the Summary tab\'s routing message', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 200 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });

    let summarizeRequestBody = null;
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Combined revenue across both databases is $700.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    // The request Phase C actually received carries the ORIGINAL prompt
    // (not either database's own rewritten instruction) and one entry
    // per real per-database result.
    await expect.poll(() => summarizeRequestBody).not.toBeNull();
    expect(summarizeRequestBody.prompt).toBe('combined revenue across sales and marketing');
    expect(summarizeRequestBody.database_results).toHaveLength(2);
    // Gap 4 of "Turn History Handling in Datalect": Phase C's request now
    // carries each database's own executed SQL (`.statement` from
    // /api/execute's response), not just its results - previously this
    // field was never sent at all.
    expect(summarizeRequestBody.database_results[0]).toMatchObject({
      name: 'Sales Postgres', sql: 'SELECT * FROM deals', rowCount: 1,
    });
    expect(summarizeRequestBody.database_results[1]).toMatchObject({
      name: 'Marketing Postgres', sql: 'SELECT * FROM campaigns', rowCount: 1,
    });

    // The Summary tab (still the default active tab - no failures here)
    // shows BOTH the routing message and, once Phase C resolves, the new
    // summary text underneath it - the "*** NO SQL *** " prefix is
    // stripped the same way any other no-SQL reply's is before display.
    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Checking Sales Postgres and Marketing Postgres.');
    await expect(summaryText).toContainText('Combined revenue across both databases is $700.');
  });

  test('a failed database\'s attempted SQL is also sent to Phase C, not just its error', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
          ],
          failures: [
            { failedStatement: 'SELECT * FROM campaigns', error: 'relation "campaigns" does not exist',
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });

    let summarizeRequestBody = null;
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Sales is $500; Marketing failed.' }),
      });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    await expect.poll(() => summarizeRequestBody).not.toBeNull();
    const marketingEntry = summarizeRequestBody.database_results.find((e) => e.name === 'Marketing Postgres');
    expect(marketingEntry).toMatchObject({
      sql: 'SELECT * FROM campaigns', error: 'relation "campaigns" does not exist',
    });
  });

  // Regression guard for a real bug report: Phase C used to be skipped
  // entirely whenever NO database in the turn came back with a real
  // result - requestAllModeResultsSummary() only checked for a `columns`
  // entry, so a turn where every single connection failed (no successes,
  // no notes, only errors) never even asked the model to summarize,
  // leaving the user with nothing but the bare error tabs and no attempt
  // to explain what went wrong. Both connections fail here on purpose -
  // this is the "everything errored" case specifically, not a mix of
  // success and failure (already covered by the test below this one).
  test('when every database fails to execute (no successes, no notes at all), Phase C still runs and explains the failures', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          results: [],
          failures: [
            { failedStatement: 'SELECT * FROM deals', error: 'permission denied for table deals',
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { failedStatement: 'SELECT * FROM campaigns', error: 'relation "campaigns" does not exist',
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });

    let summarizeCallCount = 0;
    let summarizeRequestBody = null;
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeCallCount += 1;
      summarizeRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary:
            '*** NO SQL *** Results Summary\n\n**Sales Postgres:** The query failed due to a permissions problem - ' +
            'the app\'s database user likely lacks SELECT access on the deals table.\n\n' +
            '**Marketing Postgres:** The query failed because the campaigns table does not exist.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    // Phase C actually ran - both failures were sent as `error` entries,
    // not silently dropped for lack of any successful result.
    await expect.poll(() => summarizeRequestBody).not.toBeNull();
    expect(summarizeCallCount).toBe(1);
    expect(summarizeRequestBody.database_results).toHaveLength(2);
    expect(summarizeRequestBody.database_results[0]).toMatchObject({
      name: 'Sales Postgres', error: 'permission denied for table deals',
    });
    expect(summarizeRequestBody.database_results[1]).toMatchObject({
      name: 'Marketing Postgres', error: 'relation "campaigns" does not exist',
    });

    // With two failures and nothing successful, the default active tab is
    // the first failure (same "surface what needs attention" default the
    // empty-sql test above already covers) rather than the Summary tab -
    // switch to it explicitly to check triage's routing message plus
    // Phase C's explanation of BOTH failures landed there.
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Checking Sales Postgres and Marketing Postgres.');
    await expect(summaryText).toContainText('permissions problem');
    await expect(summaryText).toContainText('campaigns table does not exist');
  });

  // Regression guard for "Turn History Handling in Datalect" Gap 1: a
  // partial (or total) execute failure in "all databases" mode still gets
  // added to chatStore's history - previously this non-streaming
  // (pendingAllModeNotes) fallback branch of executeSql() never called
  // chatStore.pushTurn()/captureAllModeHistory() at all on a failure, only
  // on success. Same setup as the "every database fails to execute" test
  // above; this one instead proves the turn survived into the NEXT
  // question's `history` payload, with Phase C's explanation of the
  // failures attached (Gap 2's fix - allMode.routingMessage is now read
  // server-side too).
  test('a turn where every database fails to execute is still added to history, with Phase C\'s explanation visible to the next question', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          results: [],
          failures: [
            { failedStatement: 'SELECT * FROM deals', error: 'permission denied for table deals',
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { failedStatement: 'SELECT * FROM campaigns', error: 'relation "campaigns" does not exist',
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary:
            '*** NO SQL *** Results Summary\n\nBoth databases failed: Sales Postgres due to a permissions ' +
            'problem, and Marketing Postgres because the campaigns table does not exist.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('Both databases failed');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 1;' }),
      });
    });
    await page.locator('#aiPrompt').fill('what should we do about those failures');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    expect(Array.isArray(secondRequestBody.history)).toBe(true);
    const failedTurn = secondRequestBody.history.find((m) => m.role === 'model' && m.allMode);
    expect(failedTurn).toBeTruthy();
    expect(failedTurn.allMode.routingMessage).toContain('Both databases failed');
    expect(failedTurn.allMode.executeFailures).toHaveLength(2);
  });

  // Chunk 3 of "splitting SQL/summary per in-scope database": the server
  // now sends two additional structured shapes alongside the plain,
  // already-joined `sql`/`summary` text this suite's other tests exercise -
  // translate_routes.py's terminal-line `sql_blocks` (one {kind, id, name,
  // sql} entry per database that got real SQL - see stream_translation()'s
  // "route" branch) and /api/summarize-results' own `database_summaries`/
  // `cross_database_summary` (see that route's docstring). This chunk is
  // recording-only (per the confirmed scope) - nothing renders differently
  // yet, so this test's only job is to prove client.js actually captures
  // both structured shapes onto the turn's history entry (`allMode.
  // databaseSql`/`.databaseSummaries`/`.crossDatabaseSummary`), the same
  // way the test just above proves `executeFailures` survives onto history.
  // Exercises the LIVE STREAMING path (auto-execute on, real NDJSON
  // "phase_a_route"/"phase_b_connection_done" events - see maybeFinalize()
  // in client.js) since that's this app's main-line "all databases" mode
  // flow today.
  test('the structured per-database SQL and summaries (sql_blocks/database_summaries/cross_database_summary) are recorded onto the turn\'s history entry', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
          sql_blocks: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
              sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
              sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const body = route.request().postDataJSON();
      const isA = body.sql.includes('preset:p-a');
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            isA
              ? { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }
              : { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 100 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary:
            '*** NO SQL *** Results Summary\n\n**Sales Postgres:** Revenue is $500.\n\n' +
            '**Marketing Postgres:** Campaign spend is $100.\n\nCombined, total activity is $600.',
          database_summaries: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Campaign spend is $100.' },
          ],
          cross_database_summary: 'Combined, total activity is $600.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsTabsNav .result-tab-btn')).toHaveCount(3);
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 1;' }),
      });
    });
    await page.locator('#aiPrompt').fill('a follow-up question');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    const turn = secondRequestBody.history.find((m) => m.role === 'model' && m.allMode);
    expect(turn).toBeTruthy();
    expect(turn.allMode.databaseSql).toEqual([
      { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
        sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
      { kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
        sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;' },
    ]);
    expect(turn.allMode.databaseSummaries).toEqual([
      { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
      { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Campaign spend is $100.' },
    ]);
    expect(turn.allMode.crossDatabaseSummary).toBe('Combined, total activity is $600.');
  });

  // Same idea as the streaming test just above, for the OTHER path that
  // records history: the "pendingAllModeNotes" batched fallback
  // (translatePrompt() never received any live "phase_a_route"/
  // "phase_b_connection_done" events - see that path's own declaration
  // comment in client.js) that this whole file's other tests already
  // exercise, since they mock /api/translate with one plain JSON body
  // rather than a real NDJSON stream. Only `crossDatabaseSummary` is
  // exercised as null here (no cross-database field returned) - the
  // streaming test above already covers a non-null one - so together these
  // two tests cover both paths AND both cross_database_summary shapes.
  test('the batched (non-streaming) fallback also records structured per-database SQL and summaries onto history', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [{ kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant.' }],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
          sql_blocks: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
              sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
                      database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Results Summary\n\n**Sales Postgres:** Revenue is $500.\n\n' +
            '**Marketing Postgres:** Nothing relevant.',
          database_summaries: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant.' },
          ],
          cross_database_summary: null,
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('Revenue is $500');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 1;' }),
      });
    });
    await page.locator('#aiPrompt').fill('a follow-up question');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    const turn = secondRequestBody.history.find((m) => m.role === 'model' && m.allMode);
    expect(turn).toBeTruthy();
    expect(turn.allMode.databaseSql).toEqual([
      { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
        sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
    ]);
    expect(turn.allMode.databaseSummaries).toEqual([
      { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
      { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant.' },
    ]);
    expect(turn.allMode.crossDatabaseSummary).toBeNull();
  });

  // Regression guard for the gap this closes: /api/summarize-results' own
  // retry loop (Phase C) used to be entirely invisible to the client -
  // one plain JSON body, returned only once the whole retry loop had
  // already finished. It now streams NDJSON exactly like /api/translate
  // already does (readNdjsonStream() is shared by both) - mirrors
  // translate-execute.spec.js's own retry-line regression tests for
  // /api/translate and /api/summarize-result.
  test('a summarize-results response with a retry line ahead of the terminal line still resolves to the summary, with no lingering retry banner', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 200 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson =
        JSON.stringify({ status: 'retrying', attempt: 2, maxAttempts: 5, delaySeconds: 1, rotatedKey: false }) + '\n' +
        JSON.stringify({ status: 'done', success: true, summary: '*** NO SQL *** Combined revenue is $700.' }) + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    await expect(page.locator('.response-text')).toContainText('Combined revenue is $700.', { timeout: 10000 });
    await expect(page.locator('#resultsRetryStatus')).toHaveClass(/hidden/);
  });

  // Summary tab feedback (thumbs up/down) - see report-issue.spec.js's own
  // "Summary tab feedback" describe block for the single-connection-mode
  // coverage of this same feature (server/report_routes.py's
  // 'summary_thumbs_up'/'summary_thumbs_down' categories, client.js's
  // summaryFeedbackButtonsHtml()). Both modes render the exact same
  // {isText:true, tabLabel:'Summary'} shape, so this is here purely to
  // prove the SAME buttons show up on "all databases" mode's own Summary
  // tab too, not to re-cover ground report-issue.spec.js already owns.
  test('the Summary tab\'s thumbs up/down buttons also appear for an "all databases" mode turn, and post with no routing/summary content', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), issue_reporting_enabled: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 200 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: '*** NO SQL *** Combined revenue is $700.' }),
      });
    });
    let reportBody = null;
    await page.route('**/api/report-issue', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      reportBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    await page.locator('#aiPrompt').fill('combined revenue across sales and marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Combined revenue is $700.');

    await expect(page.locator('.summary-feedback-btn--up')).toBeVisible();
    await expect(page.locator('.summary-feedback-btn--down')).toBeVisible();

    await page.locator('.summary-feedback-btn--down').click();
    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssuePreviewSection')).toBeHidden();
    await page.locator('#reportIssueDetails').fill('The combined total looked off.');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    // Neither the routing message nor Phase C's own summary text (both
    // currently on screen) ever reach the request body - same privacy
    // posture as the single-connection variant.
    expect(reportBody).toEqual({
      category: 'summary_thumbs_down',
      details: 'The combined total looked off.',
    });
  });

  // Regression test for a real bug report: the Summary tab's thumbs up/down
  // feedback prompt was appearing the moment triage's own routing message
  // rendered - well before Phase C's real Results Summary had rendered
  // underneath it - instead of waiting for that summary to actually appear
  // (see client.js's `summaryPending` flag on the Summary tab entry,
  // renderAllModeCombinedResults'/startAllModeStreaming's own comments on
  // it, and appendPhaseCSummaryToSummaryTab/settleSummaryTabPending, which
  // clear it once Phase C has settled one way or another). Uses the live
  // streaming path (auto-execute on) with a manually-gated
  // /api/summarize-results route so the test can assert the "in between"
  // state Playwright's atomic route.fulfill() can't otherwise expose - see
  // this file's own "progressive/streaming redesign" section comment above
  // for that limitation, and translate-execute.spec.js's identical
  // manually-gated-route pattern for holding a response open on purpose.
  test('the Summary tab\'s feedback buttons stay hidden until Phase C\'s Results Summary actually renders, not as soon as the Triage message does', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), issue_reporting_enabled: true, auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking Sales Postgres.',
          connection_selection: [{ kind: 'preset', id: 'p-a', name: 'Sales Postgres' }],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'done', success: true, router_route: true,
          routing_message: 'Checking Sales Postgres.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [], generation_failures: [],
          connection_selection: [{ kind: 'preset', id: 'p-a', name: 'Sales Postgres' }],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
            database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }],
        }),
      });
    });

    // Held open until releaseSummarize() below - lets this test observe
    // the real "triage rendered, Phase C still in flight" window instead of
    // Phase C resolving before the test can even check.
    let releaseSummarize;
    const summarizeGate = new Promise((resolve) => { releaseSummarize = resolve; });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await summarizeGate;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, summary: 'Sales Postgres shows total revenue of $500.' }),
      });
    });

    await page.locator('#aiPrompt').fill('total revenue');
    await page.locator('#aiPrompt').press('Enter');

    // Triage's own routing message is up (the Summary tab has rendered),
    // but Phase C hasn't resolved yet - the feedback prompt must NOT be
    // showing at this point, which was exactly the bug.
    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Checking Sales Postgres.');
    await expect(page.locator('.summary-feedback-btn--up')).toBeHidden();
    await expect(page.locator('.summary-feedback-btn--down')).toBeHidden();

    // Let Phase C resolve - only now, once the Results Summary text has
    // actually rendered, should the feedback prompt appear.
    releaseSummarize();
    await expect(summaryText).toContainText('Sales Postgres shows total revenue of $500.');
    await expect(page.locator('.summary-feedback-btn--up')).toBeVisible();
    await expect(page.locator('.summary-feedback-btn--down')).toBeVisible();
  });

  // "All databases" mode's own "answer" outcome (see translate_routes.py's
  // module docstring): triage decided no database data was needed at all,
  // so /api/translate's terminal response carries a "*** NO SQL ***" reply
  // directly - no `router_route`, no /api/execute, no /api/summarize-
  // results. Renders through the exact same renderNoSqlResponse() call as
  // single-connection mode's own plain reply (report-issue.spec.js's
  // "Summary tab feedback" describe block covers that variant) - this is
  // here purely to prove the same thumbs-up/down prompt appears for THIS
  // mode's own flavor too (with the leading-label convention active, since
  // IN_SCOPE_MODE is 'all' here - see buildConfigState()).
  test('an "all databases" mode "answer" outcome (a NO-SQL reply, not a routed query) also gets the thumbs up/down feedback prompt', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), issue_reporting_enabled: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          sql: '*** NO SQL *** Neither Sales Postgres nor Marketing Postgres has data relevant to that question.',
        }),
      });
    });
    let reportBody = null;
    await page.route('**/api/report-issue', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      reportBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    await page.locator('#aiPrompt').fill('what is the weather today');
    await page.locator('#aiPrompt').press('Enter');

    const responseText = page.locator('.response-text');
    await expect(responseText).toContainText('Neither Sales Postgres nor Marketing Postgres has data relevant to that question.');

    const up = page.locator('.summary-feedback-btn--up');
    const down = page.locator('.summary-feedback-btn--down');
    await expect(up).toBeVisible();
    await expect(down).toBeVisible();

    await up.click();
    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await page.locator('#reportIssueDetails').fill('Correctly declined to make something up.');
    await page.locator('#reportIssueSendBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeHidden();
    expect(reportBody).toEqual({
      category: 'summary_thumbs_up',
      details: 'Correctly declined to make something up.',
    });
  });

  // Regression guard for a real bug report: an "all databases" mode
  // "answer" outcome whose text is just ONE sentence with nothing after it
  // - exactly the shape of translate_routes.py's own _TRIAGE_FAILURE_TEXT
  // ("*** NO SQL *** I am not able to respond to your prompt.", the fixed
  // apology all-mode triage falls back to when its response couldn't be
  // parsed at all, api_error=False - see that constant's own docstring) -
  // used to leak the literal word "SUMMARY_BLOCK" glued onto the front of
  // the visible text. renderNoSqlResponse() marks every all-mode "answer"
  // outcome with SUMMARY_TAB_BLOCK_MARKER expecting the usual "<label>
  // \n\nbody" shape, but renderMarkdownLiteSummaryTab()'s regex used to
  // require that trailing blank line unconditionally to strip the marker -
  // a label with no body at all (nothing follows it) never matched, so the
  // marker's own NUL characters (invisible once rendered) were left behind
  // with the literal text "SUMMARY_BLOCK" sitting right in front of the
  // apology, no space in between. Uses the real fixed apology string
  // verbatim, not a paraphrase, since the exact text (not just its shape)
  // is what actually reached production.
  test('an all-mode "answer" outcome that is a single sentence with no body never leaks the internal SUMMARY_BLOCK marker text', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          sql: '*** NO SQL *** I am not able to respond to your prompt.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('gibberish question');
    await page.locator('#aiPrompt').press('Enter');

    const responseText = page.locator('.response-text');
    await expect(responseText).toHaveText('I am not able to respond to your prompt.');

    const rawHtml = await responseText.evaluate((el) => el.innerHTML);
    expect(rawHtml).not.toContain('SUMMARY_BLOCK');
  });

  // Regression guard for the "Triage"/"Result Summary" section labels
  // becoming language-agnostic (see connection_router.py's
  // is_label_only_response and client.js's renderMarkdownLiteSummaryTab):
  // the server now translates each label into the user's own question's
  // language rather than always sending the literal English word, so the
  // client can no longer detect what to bold by matching specific text -
  // it has to work purely by POSITION (see those two functions'
  // docstrings). Mocks both labels in Spanish specifically to prove this
  // isn't hardcoded to English words anymore - a bug here would leave
  // both labels rendered as plain, un-bolded text instead.
  test('non-English triage and results-summary labels are still bolded, by position rather than by matching English words', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Diagnóstico\n\nComprobando Sales Postgres y Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 200 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Resumen de resultados\n\nLos ingresos combinados son $700.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('ingresos combinados de ventas y marketing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Comprobando Sales Postgres y Marketing Postgres.');
    await expect(summaryText).toContainText('Los ingresos combinados son $700.');

    // Both labels - triage's own leading one, and Phase C's own leading
    // one after the join - are real bolded+underlined headings, not left
    // as plain text the way an English-only word-matching regex would
    // have left them.
    const boldedLabels = summaryText.locator('strong u');
    await expect(boldedLabels).toHaveCount(2);
    await expect(boldedLabels.nth(0)).toHaveText('Diagnóstico');
    await expect(boldedLabels.nth(1)).toHaveText('Resumen de resultados');
  });

  test('a Phase C summary with one bold-named paragraph per database renders as separate paragraphs with the names bolded', async ({ page }) => {
    // The prompt (server/translate_routes.py's _SUMMARY_SYSTEM_INSTRUCTION)
    // asks the LLM for "**Name:** ..." paragraphs separated by a blank
    // line - nothing client-side enforces that shape, so this just proves
    // the RENDERING pipeline (renderMarkdownLite's bold handling plus
    // .response-text's white-space: pre-wrap) actually honors it when the
    // model does produce it, the same way any other real reply would.
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          // Leading label lines included on both, matching the real
          // shape _TRIAGE_SYSTEM_INSTRUCTION/_SUMMARY_SYSTEM_INSTRUCTION
          // ask the model for - see the dedicated label test above for
          // why that matters to this rendering pipeline now.
          routing_message: 'Triage\n\nChecking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['total'], rows: [{ total: 200 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary:
            '*** NO SQL *** Results Summary\n\n**Sales Postgres:** Revenue was $500.\n\n' +
            '**Marketing Postgres:** No campaigns ran this period.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('how did each side do');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Revenue was $500.');

    // Each database's name rendered bold (a real <strong>, not literal
    // "**" asterisks leaking through), and the two paragraphs are still
    // separated by a blank line in the underlying text - .response-text's
    // white-space: pre-wrap is what turns that into a genuine visible gap
    // between them rather than one run-on paragraph. Scoped to exclude
    // the two <strong><u>...</u></strong> section-heading labels (see the
    // dedicated label test above) - this test is specifically about the
    // per-database bold name convention.
    await expect(summaryText.locator('strong:not(:has(u))')).toHaveText(['Sales Postgres:', 'Marketing Postgres:']);
    const rawText = await summaryText.evaluate((el) => el.textContent);
    expect(rawText).toContain('Revenue was $500.\n\nMarketing Postgres:');
  });

  test('a router_route response with empty sql (every database noted or failed) renders immediately with no /api/execute call, but still runs Phase C over the generation failure', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    let executeCallCount = 0;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql: '',
          database_notes: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Sales Postgres has nothing relevant to this question.' },
          ],
          generation_failures: [
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', error: 'Simulated generation failure.' },
          ],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      executeCallCount += 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, results: [] }) });
    });
    // Regression coverage (see requestAllModeResultsSummary's own
    // docstring): this turn has no successful result at all - one note,
    // one generation failure - which used to mean Phase C was never even
    // attempted for this particular router_route shape (no /api/execute
    // call happens at all when data.sql comes back empty). Phase C is
    // still worth running here purely to explain the failure, so this
    // must now actually be called.
    let summarizeCallCount = 0;
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      summarizeCallCount += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/x-ndjson',
        body: JSON.stringify({
          status: 'done', success: true,
          summary: '*** NO SQL *** Results Summary\n\n**Marketing Postgres:** The query could not be generated because of a permissions problem.',
        }) + '\n',
      });
    });

    await page.locator('#aiPrompt').fill('something neither database can answer');
    await page.locator('#aiPrompt').press('Enter');

    // Rendered directly off the translate response - a Summary tab, a Note
    // tab, and a failure tab, no execution round-trip involved at all.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(1)).toContainText('Note');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');

    // A generation failure is its own error tab - and since it's the only
    // failure present, it's the one shown by default (same "surface what
    // needs attention" default as the existing execute-failure renderers).
    await expect(page.locator('.error-cell')).toContainText('Marketing Postgres');
    await expect(page.locator('.error-cell')).toContainText('Simulated generation failure.');

    expect(executeCallCount).toBe(0);
    expect(await currentSql(page)).toBe('');

    // Phase C actually ran (not skipped) and its explanation landed on the
    // Summary tab underneath triage's own routing message.
    expect(summarizeCallCount).toBe(1);
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('The query could not be generated because of a permissions problem.');
  });

  test('a router_route response with a generation failure for one database still shows the other database\'s real result, in its own tab', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [],
          generation_failures: [
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', error: 'Simulated generation failure.' },
          ],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
          ],
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    // Summary + pg-a's real result + pg-b's generation-failure tab.
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');
    await expect(tabs.nth(2)).toHaveClass(/result-tab-btn--error/);

    // The failure tab is shown by default (it's the only one present).
    await expect(page.locator('.error-cell')).toContainText('Marketing Postgres');
    await expect(page.locator('.error-cell')).toContainText('Simulated generation failure.');

    // pg-a's real result is still there, in its own tab.
    await tabs.nth(1).click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['x']);
  });

  test('stepping back and then forward through an all-mode turn restores every tab, including the Phase C summary', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    // Turn 1: a plain meta-question, answered directly (no routing at all)
    // - matches "which databases have customer data", the first question
    // in the real bug report this regression test is guarding against.
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          sql: '*** NO SQL *** Sales Postgres and Marketing Postgres both have customer data.',
        }),
      });
    });
    await page.locator('#aiPrompt').fill('which databases have customer data');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('both have customer data');

    // Turn 2: a real router_route turn - two databases, real SQL executed
    // against each, and a Phase C summary appended underneath the routing
    // message. Same shape as the "Phase C summary" test above.
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT count(*) AS total FROM customers;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT count(*) AS total FROM customers;',
          database_notes: [],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT count(*) AS total FROM customers', columns: ['total'], rows: [{ total: 42 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT count(*) AS total FROM customers', columns: ['total'], rows: [{ total: 17 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Sales Postgres has 42 customers and Marketing Postgres has 17.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('how many customers are in each database, and show me a few from each one');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect(page.locator('.response-text')).toContainText('Sales Postgres has 42 customers');

    // Step back to turn 1 - the plain answer, no tabs at all.
    await page.locator('#goBackBtn').click();
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    await expect(page.locator('.response-text')).toContainText('both have customer data');

    // Step forward again - this is the exact bug report: every tab (the
    // Summary tab WITH its Phase C summary text, plus both per-database
    // result tabs, correctly labeled) must come back exactly as it was,
    // not disappear.
    await page.locator('#goForwardBtn').click();
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');
    await expect(page.locator('.response-text')).toContainText('Checking Sales Postgres and Marketing Postgres.');
    await expect(page.locator('.response-text')).toContainText('Sales Postgres has 42 customers and Marketing Postgres has 17.');

    await tabs.nth(1).click();
    await expect(page.locator('#resultsBody td')).toHaveText(['42']);
    await tabs.nth(2).click();
    await expect(page.locator('#resultsBody td')).toHaveText(['17']);
  });

  // --- "all databases" mode's progressive/streaming redesign: phase_a_route
  // and phase_b_connection_done NDJSON events (see translate_routes.py's
  // stream_translation() docstring) ---
  //
  // Playwright's route.fulfill() delivers a mocked body atomically (see
  // translate-execute.spec.js's own retry-line test for the same
  // limitation already documented in this suite) - these specs can't prove
  // real incremental ARRIVAL TIMING, only the two things that matter for a
  // regression gate here: how many /api/execute calls the client actually
  // makes (one per real-SQL connection when auto-execute is on, instead of
  // one batched call), and that the final rendered tab set is correct
  // regardless of which NDJSON events preceded the terminal line.

  test('with auto-execute on, a router_route stream settles a note immediately and fires exactly one /api/execute call for the one real-SQL connection', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        // Arrives in COMPLETION order - p-b (a note, no execution needed)
        // settles before p-a (real SQL) even though p-a was listed first
        // in connection_selection above.
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'note', text: 'Marketing Postgres has nothing relevant.',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'done', success: true, router_route: true,
          routing_message: 'Checking Sales Postgres and Marketing Postgres.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Marketing Postgres has nothing relevant.' },
          ],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    const executeCalls = [];
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      executeCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
            database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals vs campaigns');
    await page.locator('#aiPrompt').press('Enter');

    // Placeholder tabs keep connection_selection's ORIGINAL order (Sales
    // first, then Marketing) regardless of which one settled first - only
    // the CONTENT of each tab is filled in as its own event/execute call
    // resolves, never the tab's position.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');
    await expect(tabs.nth(2)).toContainText('Note');

    await expect.poll(() => executeCalls.length).toBe(1);
    expect(executeCalls[0].sql).toContain('preset:p-a');
    expect(executeCalls[0].sql).not.toContain('preset:p-b');
  });

  test('with auto-execute on, a router_route stream fires N separate /api/execute calls for N real-SQL connections, not one batched call', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    const executeCalls = [];
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const body = route.request().postDataJSON();
      executeCalls.push(body);
      const isA = body.sql.includes('preset:p-a');
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            isA
              ? { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }
              : { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect.poll(() => executeCalls.length).toBe(2);
    expect(executeCalls.some((c) => c.sql.includes('preset:p-a'))).toBe(true);
    expect(executeCalls.some((c) => c.sql.includes('preset:p-b'))).toBe(true);
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');
  });

  // Regression test for a real bug report: a connection whose own generated
  // script has MORE THAN ONE statement only ever showed the first
  // statement's tab, silently dropping every statement after it - single-
  // connection mode already shows one tab per statement
  // (renderMultiTurnResults), "all databases" mode's live per-connection
  // execute path (executeOneAllModeConnection in client.js) did not.
  test('with auto-execute on, a connection whose script has multiple statements gets one tab per statement, not just the first', async ({ page }) => {
    await mockConfig(page, { ...buildConfigState(), auto_sql_execute: true });
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals; SELECT * FROM leads;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals; SELECT * FROM leads;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const body = route.request().postDataJSON();
      const isA = body.sql.includes('preset:p-a');
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          // Sales Postgres' own script has TWO statements - both must
          // survive as their own tabs, tagged with the SAME database.
          results: isA
            ? [
                { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
                { statement: 'SELECT * FROM leads', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
              ]
            : [
                { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 3 }], rowCount: 1,
                  database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
              ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals, leads, and campaigns');
    await page.locator('#aiPrompt').press('Enter');

    // Summary + Sales Postgres' TWO statement tabs + Marketing Postgres'
    // one tab = 4 total, not 3 (which is what the bug produced - Sales
    // Postgres' second statement silently dropped).
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(4);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Sales Postgres');
    await expect(tabs.nth(3)).toContainText('Marketing Postgres');

    await tabs.nth(1).click();
    await expect(page.locator('#resultsBody td').first()).toHaveText('1');
    await tabs.nth(2).click();
    await expect(page.locator('#resultsBody td').first()).toHaveText('2');
    await tabs.nth(3).click();
    await expect(page.locator('#resultsBody td').first()).toHaveText('3');
  });

  // Regression test for the same underlying bug, on the manual-Execute-
  // button (auto-execute off) batched path - settleAllModeBatchedResults()
  // in client.js groups a single /api/execute response's results by
  // connection before turning them into tabs, so this path needs its own
  // coverage independent of executeOneAllModeConnection() above.
  test('with auto-execute off, a manual Execute click also gives one tab per statement for a connection with multiple statements', async ({ page }) => {
    await mockConfig(page); // auto_sql_execute: false (buildConfigState()'s default)
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals; SELECT * FROM leads;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals; SELECT * FROM leads;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    const executeCalls = [];
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      executeCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            // Both of Sales Postgres' statements come back in the SAME
            // batched response, grouped with Marketing Postgres' one
            // statement in between - settleAllModeBatchedResults() must
            // group these back together by database, not by array position.
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 3 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
            { statement: 'SELECT * FROM leads', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals, leads, and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3); // Summary + 2 "Ready to execute" placeholders
    expect(executeCalls).toHaveLength(0);

    await page.locator('#runBtn').click();
    await expect.poll(() => executeCalls.length).toBe(1);

    // Summary + Sales Postgres' TWO statement tabs + Marketing Postgres'
    // one tab = 4 total.
    await expect(tabs).toHaveCount(4);
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Sales Postgres');
    await expect(tabs.nth(3)).toContainText('Marketing Postgres');
  });

  test('with auto-execute off, a router_route stream renders "Ready to execute" placeholders, and a manual Execute click still fires exactly one batched /api/execute call', async ({ page }) => {
    await mockConfig(page); // auto_sql_execute: false (buildConfigState()'s default)
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });

    const executeCalls = [];
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      executeCalls.push(route.request().postDataJSON());
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
            { statement: 'SELECT * FROM campaigns', columns: ['x'], rows: [{ x: 2 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true, summary: '' }) });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(1)).toContainText('Ready to execute');
    await expect(tabs.nth(2)).toContainText('Ready to execute');
    expect(executeCalls).toHaveLength(0);

    await page.locator('#runBtn').click();
    await expect.poll(() => executeCalls.length).toBe(1);
    expect(executeCalls[0].sql).toContain('preset:p-a');
    expect(executeCalls[0].sql).toContain('preset:p-b');
    await expect(tabs.nth(1)).toContainText('Sales Postgres');
    await expect(tabs.nth(2)).toContainText('Marketing Postgres');
  });

  // Regression guard for "Turn History Handling in Datalect" Gap 1, on the
  // STREAMING path this time: a router_route turn whose placeholders
  // arrived via phase_a_route/phase_b_connection_done events (so
  // allModeStreamState, not pendingAllModeNotes, is what executeSql()'s
  // failure branch sees) must still persist history when the batched
  // "Ready to execute" call comes back with a partial failure - previously
  // this branch ran Phase C and threw the result away without ever
  // reaching maybeFinalize()/chatStore.pushTurn() (see this branch's own,
  // now-removed comment describing that as deliberate pre-streaming
  // behavior).
  test('with auto-execute off, a streamed router_route turn whose manual Execute click partially fails is still added to history', async ({ page }) => {
    await mockConfig(page); // auto_sql_execute: false (buildConfigState()'s default)
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      const ndjson = [
        {
          status: 'phase_a_route', routing_message: 'Checking both.',
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-a', name: 'Sales Postgres',
          outcome: 'sql', sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
        },
        {
          status: 'phase_b_connection_done', kind: 'preset', id: 'p-b', name: 'Marketing Postgres',
          outcome: 'sql', sql: '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
        },
        {
          status: 'done', success: true, router_route: true, routing_message: 'Checking both.',
          sql:
            '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;\n\n' +
            '-- database: preset:p-b (Marketing Postgres)\nSELECT * FROM campaigns;',
          database_notes: [], generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' },
          ],
        },
      ].map((e) => JSON.stringify(e)).join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: ndjson });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: false,
          results: [
            { statement: 'SELECT * FROM deals', columns: ['x'], rows: [{ x: 1 }], rowCount: 1,
              database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } },
          ],
          failures: [
            { failedStatement: 'SELECT * FROM campaigns', error: 'relation "campaigns" does not exist',
              database: { kind: 'preset', id: 'p-b', name: 'Marketing Postgres' } },
          ],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Results Summary\n\nSales Postgres has 1 deal; Marketing Postgres failed because campaigns is missing.',
        }),
      });
    });

    await page.locator('#aiPrompt').fill('deals and campaigns');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('Sales Postgres has 1 deal');

    let secondRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      secondRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 1;' }),
      });
    });
    await page.locator('#aiPrompt').fill('what about marketing');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => secondRequestBody).not.toBeNull();
    const failedTurn = secondRequestBody.history.find((m) => m.role === 'model' && m.allMode);
    expect(failedTurn).toBeTruthy();
    expect(failedTurn.allMode.routingMessage).toContain('Marketing Postgres failed');
    expect(failedTurn.allMode.executeFailures).toHaveLength(1);
    // The one database that DID succeed is preserved too, not just the
    // failure - same summarizeResultForHistory shape a fully successful
    // turn already gets.
    expect(failedTurn.results).toHaveLength(1);
    expect(failedTurn.results[0].rows).toEqual([{ x: 1 }]);
  });

  // Chunk 4 of "splitting SQL/summary per in-scope database" (see
  // client.js's captureAllModeHistory()/fanOutAllModeHistoryPerDatabase()
  // docstrings for the full multi-window design history): an all-mode
  // turn's own shared history entry (asserted by the two tests above) is
  // ALSO fanned out, per in-scope database, into that database's own
  // single-connection-mode history bucket - identical to the one reached
  // by switching directly to it (computeBucketKey()'s (kind,id)-based
  // scheme - see that function's own docstring for why this now matches
  // regardless of which mode reaches the database). Covers all three
  // outcome shapes end to end: p-a got real SQL, executed, and its own
  // Phase C paragraph (the "sql" outcome); p-b only ever got a note (the
  // "note" outcome, no SQL/results/summary at all).
  test('switching to a specific database in single-connection mode after an all-mode turn shows that turn merged into its own back/forward history', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking both.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant to marketing here.' },
          ],
          generation_failures: [],
          // Each entry's own "prompt" (Chunk 4's new connection_selection
          // field - see translate_routes.py's entry_prompts docstring) is
          // deliberately DIFFERENT from both the original question below
          // and from each other, so a fanned-out turn showing the wrong
          // one (e.g. the original cross-database question, or the other
          // database's own rewrite) would be caught.
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', prompt: 'How are sales performing?' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', prompt: 'How is marketing performing?' },
          ],
          sql_blocks: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
              sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
                      database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Results Summary\n\n**Sales Postgres:** Revenue is $500.\n\n' +
            '**Marketing Postgres:** Nothing relevant to marketing here.',
          database_summaries: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant to marketing here.' },
          ],
          cross_database_summary: null,
        }),
      });
    });

    await page.locator('#aiPrompt').fill("how's business doing");
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('Revenue is $500');

    // Switch to Sales Postgres (p-a) specifically - the "sql" (executed)
    // outcome. reconcileActiveHistoryBucket() (triggered by saving) both
    // creates/finds that database's own bucket AND immediately restores
    // its latest turn - no back/forward click needed to SEE it land there
    // in the first place, only to prove it's really turn history (below).
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="preset:p-a"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    // The prompt shown is p-a's OWN triage-rewritten question, not the
    // original cross-database one and not p-b's rewrite either.
    await expect(page.locator('#aiPrompt')).toHaveValue('How are sales performing?');
    await expect.poll(() => currentSql(page)).toContain('FROM deals');
    // Summary tab (p-a's own Phase C paragraph) prepended and made active,
    // plus the one real result tab.
    await expect(page.locator('#resultsTabsNav .result-tab-btn')).toHaveCount(2);
    await expect(page.locator('.response-text')).toContainText('Revenue is $500.');

    // A second, genuinely single-connection turn asked directly against
    // p-a - this is what back/forward will actually be exercised against.
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT COUNT(*) FROM reps;' }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT COUNT(*) FROM reps', columns: ['count'], rows: [{ count: 5 }], rowCount: 1 }],
        }),
      });
    });
    // Single-connection mode's own post-execution summarization (see
    // requestSingleModeResultsSummary) fires unconditionally on a real
    // question's execution - stubbed out to a no-summary response so this
    // turn stays a clean, single-tab baseline to navigate back to.
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: false }) });
    });

    await page.locator('#aiPrompt').fill('how many reps do we have');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('COUNT');
    await page.locator('#runBtn').click();
    // Exactly one result tab - buildResultsTabsNav() hides the tab strip
    // entirely rather than showing a single-tab nav (see its own
    // length <= 1 guard), so the tab count is asserted via the table body
    // itself, not the (deliberately absent) nav.
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    await expect(page.locator('#resultsBody')).toContainText('5');

    // Back to the fanned-out all-mode turn - "merged into its own
    // back/forward history", not just visible on first switch-to.
    await page.locator('#goBackBtn').click();
    await expect(page.locator('#aiPrompt')).toHaveValue('How are sales performing?');
    await expect.poll(() => currentSql(page)).toContain('FROM deals');
    await expect(page.locator('#resultsTabsNav .result-tab-btn')).toHaveCount(2);
    await expect(page.locator('.response-text')).toContainText('Revenue is $500.');

    // ...and forward again, back to the direct single-connection turn.
    await page.locator('#goForwardBtn').click();
    await expect(page.locator('#aiPrompt')).toHaveValue('how many reps do we have');
    await expect.poll(() => currentSql(page)).toContain('COUNT');
    await expect(page.locator('#resultsTabsNav')).toHaveClass(/hidden/);
    await expect(page.locator('#resultsBody')).toContainText('5');

    // Now switch to Marketing Postgres (p-b) - the "note" outcome (no SQL
    // ever generated/run for it at all). Its own bucket independently
    // carries the SAME all-mode turn, fanned out as a plain
    // '*** NO SQL ***' reply - restoreLatestTurn()'s single-connection
    // no-SQL branch, not the tabbed-results branch p-a took above.
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="preset:p-b"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await expect(page.locator('#aiPrompt')).toHaveValue('How is marketing performing?');
    await expect(page.locator('.response-text')).toContainText('Nothing relevant to marketing here.');
    expect(await currentSql(page)).toBe('');
  });

  // Chunk 5 of "splitting SQL/summary per in-scope database" (see
  // client.js's captureAllModeHistory()/fanOutAllModeHistoryPerDatabase()/
  // buildInScopeConnectionHistories() docstrings for the earlier chunks):
  // a connection's history is now COMPLETELY MERGED regardless of which
  // mode each past turn came from, and that merged history is what an
  // "all databases" mode request sends (as connection_histories, keyed by
  // "preset:<id>"/"custom:<key>") for THAT SAME connection's own Phase B
  // SQL-generation call. Covers both directions: an all-mode turn's own
  // per-database fan-out (Chunk 4) feeding a LATER all-mode turn, and a
  // genuinely direct single-connection-mode turn ALSO feeding a later
  // all-mode turn for that same database.
  test('a database\'s merged history (from both all-mode fan-out and direct single-connection turns) is sent as connection_histories on the next all-mode request', async ({ page }) => {
    await mockConfig(page);
    await gotoApp(page);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          router_route: true,
          routing_message: 'Checking both.',
          sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;',
          database_notes: [
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant to marketing here.' },
          ],
          generation_failures: [],
          connection_selection: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', prompt: 'How are sales performing?' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', prompt: 'How is marketing performing?' },
          ],
          sql_blocks: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres',
              sql: '-- database: preset:p-a (Sales Postgres)\nSELECT * FROM deals;' },
          ],
        }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT * FROM deals', columns: ['total'], rows: [{ total: 500 }], rowCount: 1,
                      database: { kind: 'preset', id: 'p-a', name: 'Sales Postgres' } }],
        }),
      });
    });
    await page.route('**/api/summarize-results', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Results Summary\n\n**Sales Postgres:** Revenue is $500.\n\n' +
            '**Marketing Postgres:** Nothing relevant to marketing here.',
          database_summaries: [
            { kind: 'preset', id: 'p-a', name: 'Sales Postgres', text: 'Revenue is $500.' },
            { kind: 'preset', id: 'p-b', name: 'Marketing Postgres', text: 'Nothing relevant to marketing here.' },
          ],
          cross_database_summary: null,
        }),
      });
    });

    // Turn 1 - an "all databases" mode turn that fans out into both p-a's
    // and p-b's own buckets (Chunk 4).
    await page.locator('#aiPrompt').fill("how's business doing");
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await page.locator('.result-tab-btn').filter({ hasText: 'Summary' }).click();
    await expect(page.locator('.response-text')).toContainText('Revenue is $500');

    // Switch to Sales Postgres (p-a) directly and ask it a genuinely
    // single-connection-mode question - turn 2 in that SAME bucket.
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="preset:p-a"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT COUNT(*) FROM reps;' }),
      });
    });
    await page.route('**/api/execute', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          results: [{ statement: 'SELECT COUNT(*) FROM reps', columns: ['count'], rows: [{ count: 5 }], rowCount: 1 }],
        }),
      });
    });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: false }) });
    });

    await page.locator('#aiPrompt').fill('how many reps do we have');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('COUNT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsBody')).toContainText('5');

    // Switch back to "All configured databases" and ask a third, new
    // combined question - capture exactly what THIS request sends.
    await openConfigModal(page);
    await page.locator('input[name="db_connection_option"][value="all"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    let thirdRequestBody = null;
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      thirdRequestBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ sql: 'SELECT 3;' }),
      });
    });
    await page.locator('#aiPrompt').fill('another combined question');
    await page.locator('#aiPrompt').press('Enter');

    await expect.poll(() => thirdRequestBody).not.toBeNull();
    const histories = thirdRequestBody.connection_histories;
    expect(histories).toBeTruthy();

    // p-a's own bucket: turn 1 (fanned out from all-mode) THEN turn 2 (the
    // direct single-connection question) - both, merged, in that order.
    const pa = histories['preset:p-a'];
    expect(pa).toHaveLength(4);
    expect(pa[0]).toMatchObject({ role: 'user', text: 'How are sales performing?' });
    expect(pa[1].text).toContain('FROM deals');
    expect(pa[1].results[0].rows).toEqual([{ total: 500 }]);
    expect(pa[2]).toMatchObject({ role: 'user', text: 'how many reps do we have' });
    expect(pa[3].text).toContain('COUNT');
    expect(pa[3].results[0].rows).toEqual([{ count: 5 }]);

    // p-b's own bucket: only turn 1's note outcome - it was never visited
    // in single-connection mode at all.
    const pb = histories['preset:p-b'];
    expect(pb).toHaveLength(2);
    expect(pb[0]).toMatchObject({ role: 'user', text: 'How is marketing performing?' });
    expect(pb[1].text).toContain('Nothing relevant to marketing here.');
  });
});
