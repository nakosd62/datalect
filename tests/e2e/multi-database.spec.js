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
  await expect(page.locator('#configModal')).not.toHaveClass(/hidden/);
}

function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

test.describe('multi-database question answering', () => {
  test('"All configured databases" and a specific preset are mutually exclusive radios, each saving the right scope', async ({ page }) => {
    const state = await mockConfig(page, {
      ...buildConfigState(), in_scope_preset_ids: ['p-a'], in_scope_custom_connection_keys: [], in_scope_mode: 'single',
    });
    await gotoApp(page);
    await openConfigModal(page);

    // "All" (index 0) + the two presets (p-a, p-b) - a true single-select
    // radio group again (see renderDbRadioButtons() in client.js), not the
    // checkbox picker this replaced.
    const allRadio = page.locator('input[name="db_connection_option"][value="all"]');
    const boxes = page.locator('input[name="db_connection_option"]');
    await expect(boxes).toHaveCount(3);
    await expect(allRadio).not.toBeChecked();
    await expect(boxes.nth(1)).toBeChecked(); // p-a, today's only in-scope preset

    // Picking "All" unchecks whichever specific preset was selected -
    // plain native radio exclusivity, no client bookkeeping involved.
    await allRadio.check();
    await expect(boxes.nth(1)).not.toBeChecked();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);
    expect(state._lastPostBody.in_scope_mode).toBe('all');

    // Picking a specific preset again narrows straight back down to just
    // that one - in_scope_mode flips back to 'single' and the in-scope
    // arrays are sent as exactly that one connection.
    await openConfigModal(page);
    await boxes.nth(2).check(); // p-b
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

    // Picking a specific preset (p-b, index 2 - "All" is index 0, p-a is
    // index 1) narrows scope back down to just that one connection and
    // reverts the badge to its actual name.
    await openConfigModal(page);
    const boxes = page.locator('input[name="db_connection_option"]');
    await boxes.nth(2).check();
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
    const tabs = page.locator('.result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await expect(tabs.nth(0)).toContainText('[Sales Postgres]');
    await expect(tabs.nth(1)).toContainText('[Marketing Postgres]');
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
    await expect.poll(() => currentSql(page)).toContain('SELECT 1');

    await page.locator('#aiPrompt').fill('follow-up question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT 2');

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

    await page.locator('#aiPrompt').fill('campaigns question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsHeader th')).toHaveText(['x']);
    await expect(page.locator('#aiPrompt')).toHaveValue('campaigns question');

    // Now pick p-a specifically (index 1 - "All" is index 0, p-b is index
    // 2), narrowing scope away from "All" down to just p-a - the pin from
    // above (p-b) no longer describes an in-scope connection.
    await openConfigModal(page);
    const boxes = page.locator('input[name="db_connection_option"]');
    await boxes.nth(1).check();
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
    await expect(tabs.nth(1)).toContainText('[Support Postgres]');
    await expect(tabs.nth(1)).toContainText('Note');
    await expect(tabs.nth(2)).toContainText('[Sales Postgres]');
    await expect(tabs.nth(3)).toContainText('[Marketing Postgres]');

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
    expect(summarizeRequestBody.database_results[0]).toMatchObject({ name: 'Sales Postgres', rowCount: 1 });

    // The Summary tab (still the default active tab - no failures here)
    // shows BOTH the routing message and, once Phase C resolves, the new
    // summary text underneath it - the "*** NO SQL *** " prefix is
    // stripped the same way any other no-SQL reply's is before display.
    const summaryText = page.locator('.response-text');
    await expect(summaryText).toContainText('Checking Sales Postgres and Marketing Postgres.');
    await expect(summaryText).toContainText('Combined revenue across both databases is $700.');
  });

  test('a router_route response with empty sql (every database noted or failed) renders immediately with no /api/execute call', async ({ page }) => {
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

    await page.locator('#aiPrompt').fill('something neither database can answer');
    await page.locator('#aiPrompt').press('Enter');

    // Rendered directly off the translate response - a Summary tab, a Note
    // tab, and a failure tab, no execution round-trip involved at all.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(3);
    await expect(tabs.nth(0)).toContainText('Summary');
    await expect(tabs.nth(1)).toContainText('[Sales Postgres]');
    await expect(tabs.nth(1)).toContainText('Note');
    await expect(tabs.nth(2)).toContainText('[Marketing Postgres]');

    // A generation failure is its own error tab - and since it's the only
    // failure present, it's the one shown by default (same "surface what
    // needs attention" default as the existing execute-failure renderers).
    await expect(page.locator('.error-cell')).toContainText('Marketing Postgres');
    await expect(page.locator('.error-cell')).toContainText('Simulated generation failure.');

    expect(executeCallCount).toBe(0);
    expect(await currentSql(page)).toBe('');
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
    await expect(tabs.nth(1)).toContainText('[Sales Postgres]');
    await expect(tabs.nth(2)).toContainText('[Marketing Postgres]');
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
    await expect(tabs.nth(1)).toContainText('[Sales Postgres]');
    await expect(tabs.nth(2)).toContainText('[Marketing Postgres]');
    await expect(page.locator('.response-text')).toContainText('Checking Sales Postgres and Marketing Postgres.');
    await expect(page.locator('.response-text')).toContainText('Sales Postgres has 42 customers and Marketing Postgres has 17.');

    await tabs.nth(1).click();
    await expect(page.locator('#resultsBody td')).toHaveText(['42']);
    await tabs.nth(2).click();
    await expect(page.locator('#resultsBody td')).toHaveText(['17']);
  });
});
