// tests/e2e/analytics.spec.js
//
// Custom GA4 events fired via client.js's trackEvent() (a thin wrapper
// around gtag('event', name, params) - see that function's own header
// comment for the full list: translate_submitted, sql_executed,
// error_shown, report_submitted, database_selected, model_selected,
// help_viewed, history_viewed, history_nav_clicked, preferences_viewed,
// login, logout, mic_used, quick_prompt_clicked).
//
// Rather than stubbing/spying on window.gtag itself, these tests read
// window.dataLayer directly - index.html's own inline snippet defines
// `function gtag(){dataLayer.push(arguments)}` as a plain top-level
// function declaration, which would silently clobber any pre-injected
// stub the moment that script runs. gtag('event', name, params) always
// ends up pushing the exact ['event', name, params] arguments tuple into
// dataLayer regardless, real GA library loaded or not (it's blocked/never
// requested in this sandboxed test run either way) - so reading that array
// back is a robust, stub-free way to assert on what was tracked.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Every {..params} object gtag('event', name, params) pushed for the
 * given event name, in firing order. */
async function trackedEvents(page, name) {
  return page.evaluate((eventName) => {
    return (window.dataLayer || [])
      .filter((entry) => entry && entry[0] === 'event' && entry[1] === eventName)
      .map((entry) => entry[2] || {});
  }, name);
}

async function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

async function setSqlBox(page, sql) {
  await page.evaluate((value) => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) {
      wrapper.CodeMirror.setValue(value);
    } else {
      const textarea = document.getElementById('sqlQuery');
      if (textarea) textarea.value = value;
    }
  }, sql);
}

async function mockIssueReportingEnabled(page, enabled) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    json.issue_reporting_enabled = enabled;
    await route.fulfill({ response, json });
  });
}

function mockReportIssue(page) {
  page.route('**/api/report-issue', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
  });
}

test.describe('analytics: query flow', () => {
  test('translate_submitted fires once, with the mode and database - no prompt text', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const events = await trackedEvents(page, 'translate_submitted');
    expect(events.length).toBe(1);
    // No `prompt` field - the NL prompt text itself must never reach GA.
    expect(events[0].prompt).toBeUndefined();
    expect(events[0].mode).toBe('single');
    expect(typeof events[0].database_name).toBe('string');
    expect(events[0].database_name.length).toBeGreaterThan(0);
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('sql_executed fires with a "manual" trigger and database info on a direct Execute click - no SQL text', async ({ page }) => {
    await mockExecute(page, {
      results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }],
    });
    await gotoApp(page);

    await setSqlBox(page, 'SELECT 42 AS n;');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsBody tr')).toHaveCount(1);

    const events = await trackedEvents(page, 'sql_executed');
    expect(events.length).toBe(1);
    // No `sql` field - the generated SQL text itself must never reach GA.
    expect(events[0].sql).toBeUndefined();
    expect(events[0].trigger).toBe('manual');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('sql_executed fires with an "auto" trigger when auto-execute runs it', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT id, name FROM users;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Ada' }], rowCount: 1 }],
    });
    await gotoApp(page);

    // Enable auto-execute first - see preferences-modal.spec.js for the
    // same checkbox/save button this drives. Waiting for the modal itself
    // to be visible (not just clicking the checkbox immediately after
    // #prefsBtn) matters here - the click handler awaits
    // fetchBackendConfig() before loadPreferencesIntoUI() sets the
    // checkbox's initial state, and checking it too early gets clobbered
    // right back to unchecked once that async load resolves.
    await page.locator('#prefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);
    await page.locator('#autoSqlExecuteCheckbox').check();
    await page.locator('#preferencesSaveBtn').click();

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);

    const events = await trackedEvents(page, 'sql_executed');
    expect(events.length).toBe(1);
    expect(events[0].trigger).toBe('auto');
  });

  test('error_shown fires with category "translation" for a translation error', async ({ page }) => {
    await mockTranslate(page, { error: 'The model could not understand that request.', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('do something impossible');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#resultsBody')).toContainText('Translation Error');

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('translation');
    expect(events[0].message).toContain('could not understand');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('error_shown fires with category "execution" for an execution error', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsBody')).toContainText('Execution Error');

    const events = await trackedEvents(page, 'error_shown');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('execution');
    expect(events[0].message).toContain('does_not_exist');
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('quick_prompt_clicked fires with the chip label and prompt, and still submits a translation', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await gotoApp(page);

    const chip = page.locator('.example-chip').first();
    const chipLabel = (await chip.textContent()).trim();
    await chip.click();
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    const events = await trackedEvents(page, 'quick_prompt_clicked');
    expect(events.length).toBe(1);
    expect(events[0].chip_label).toBe(chipLabel);
    expect(events[0].prompt.length).toBeGreaterThan(0);

    // The chip click still drives a real translation - one submitted event
    // downstream of it, same as typing the prompt in by hand would.
    const submitted = await trackedEvents(page, 'translate_submitted');
    expect(submitted.length).toBe(1);
  });
});

test.describe('analytics: report/feedback', () => {
  test('report_submitted fires with the report category on a successful send', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await mockExecute(page, { error: 'relation "does_not_exist" does not exist', status: 400 });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('query a table that does not exist');
    await page.locator('#aiPrompt').press('Enter');
    await page.locator('#runBtn').click();
    await page.locator('.report-issue-inline-btn').click();
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('error');
  });

  test('report_submitted fires for a "wrong_sql" report too', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    mockReportIssue(page);
    await gotoApp(page);

    await page.locator('#reportSqlBtn').click();
    await page.locator('#reportIssueDetails').fill('This looks wrong.');
    await page.locator('#reportIssueSendBtn').click();
    await expect(page.locator('#reportIssueModal')).toBeHidden();

    const events = await trackedEvents(page, 'report_submitted');
    expect(events.length).toBe(1);
    expect(events[0].category).toBe('wrong_sql');
  });
});

test.describe('analytics: connection/model/nav', () => {
  test('database_selected fires with the newly-selected database name', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#configModal')).not.toHaveClass(/hidden/);
    await page.locator('#modalDbRadioGroup input[name="db_connection_option"][value^="preset:"]').first().check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'database_selected');
    expect(events.length).toBe(1);
    expect(events[0].database_name).toBe(await page.locator('#connDbName').textContent());
    expect(typeof events[0].database_type).toBe('string');
    expect(events[0].database_type.length).toBeGreaterThan(0);
  });

  test('model_selected fires with the newly-selected provider and model', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#modelTriggerBadge').click();
    await expect(page.locator('#modelModal')).not.toHaveClass(/hidden/);
    await page.locator('input[name="llm_model_option"][value="anthropic::claude-sonnet-5"]').check();
    await page.locator('#modelSaveBtn').click();
    await expect(page.locator('#modelModal')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'model_selected');
    expect(events.length).toBe(1);
    expect(events[0].provider).toBe('anthropic');
    expect(events[0].model).toBe('claude-sonnet-5');
  });

  test('help_viewed fires when the Doc button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'help_viewed')).length).toBe(1);
  });

  test('history_viewed fires when the History button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#historyBtn').click();
    await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'history_viewed')).length).toBe(1);
  });

  test('history_nav_clicked fires with the turn offset when stepping back and forward', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT 1;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('first question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT 1');

    // A second turn - #goBackBtn/#goForwardBtn only enable with more than
    // one turn in chatStore (see updateHistoryNavButtons()'s own comment on
    // why one remaining turn already counts as "oldest").
    await mockTranslate(page, { sql: 'SELECT 2;' });
    await page.locator('#aiPrompt').fill('second question');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT 2');

    // Current turn is 0; stepping back once lands on -1, forward again
    // returns to 0 - see chatStore.turnOffset()'s own comment.
    await page.locator('#goBackBtn').click();
    await expect.poll(() => currentSql(page)).toContain('SELECT 1');
    await page.locator('#goForwardBtn').click();
    await expect.poll(() => currentSql(page)).toContain('SELECT 2');

    const events = await trackedEvents(page, 'history_nav_clicked');
    expect(events.length).toBe(2);
    expect(events[0].turn_offset).toBe(-1);
    expect(events[1].turn_offset).toBe(0);
  });

  test('preferences_viewed fires when the Preferences button is clicked', async ({ page }) => {
    await gotoApp(page);
    await page.locator('#prefsBtn').click();
    await expect(page.locator('#preferencesModal')).not.toHaveClass(/hidden/);

    expect((await trackedEvents(page, 'preferences_viewed')).length).toBe(1);
  });
});

// Reuses auth-clears-state.spec.js's own Google Identity Services stub -
// see that file's header comment for exactly what is/isn't real here.
test.describe('analytics: auth', () => {
  function fakeIdToken(email) {
    const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
    const payload = Buffer.from(JSON.stringify({
      email,
      exp: Math.floor(Date.now() / 1000) + 3600,
      picture: '',
    })).toString('base64url');
    return `${header}.${payload}.fake-signature`;
  }

  const CLOUD_RUN_CONFIG_PAYLOAD = {
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
  };

  async function stubGoogleIdentityServices(page) {
    await page.route('**/gsi/client**', (route) => route.fulfill({
      status: 200,
      contentType: 'application/javascript',
      body: '/* stubbed for e2e - see auth-clears-state.spec.js */',
    }));
    await page.addInitScript(() => {
      window.google = {
        accounts: {
          id: {
            initialize(opts) { window.__gisCallback = opts.callback; },
            renderButton(container) {
              if (container) container.innerHTML = '<button id="fakeGsiButton">Sign in</button>';
            },
            disableAutoSelect() {},
          },
        },
      };
    });
  }

  async function mockCloudRunConfig(page) {
    await page.route('**/api/config', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(CLOUD_RUN_CONFIG_PAYLOAD),
      });
    });
  }

  test('login fires once when Google Sign-In completes', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('newuser@example.com')
    );

    expect((await trackedEvents(page, 'login')).length).toBe(1);
  });

  test('logout fires once when the user signs out', async ({ page }) => {
    await stubGoogleIdentityServices(page);
    await mockCloudRunConfig(page);
    await gotoApp(page);

    await page.evaluate(
      (token) => window.__gisCallback({ credential: token }),
      fakeIdToken('user@example.com')
    );
    await expect(page.locator('#authAvatarBtn')).toBeVisible();

    await page.locator('#authAvatarBtn').click();
    await page.locator('#logoutBtn').click();

    expect((await trackedEvents(page, 'logout')).length).toBe(1);
  });
});
