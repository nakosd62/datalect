// tests/e2e/share.spec.js
//
// The header "Share" button (#shareBtn/#sharePopover) and what happens when
// someone opens the link it produces (#sharedViewBanner) - see share_routes.py's
// own module docstring and client.js's "7. SHARING A RESULT" section for the
// full feature. Unlike /api/translate and /api/execute, POST /api/share and
// GET /api/share/<id> are never mocked here - they hit the REAL Flask server
// + REAL SqliteStateStore (see fixtures.js's own header), since the whole
// point of this feature is a durable, anonymously-readable link, which a
// fake response can't meaningfully stand in for.
//
// A SECOND, intentionally cookie-less browser context (openAsNewVisitor()
// below) stands in for "someone else, who never touched this app before,
// opening a link they were sent" - no spec in this suite has needed a
// genuinely separate visitor until now (see fixtures.js's own
// crbot_user_id-per-test isolation, which gives every OTHER spec its own
// slice of server state but still only ever drives one page/session per
// test), so this file keeps that setup local rather than growing fixtures.js
// for a need only this one feature has so far.

// Uses testShowSqlVisible (aliased to `test`) rather than the plain `test`
// fixture - same reason translate-execute.spec.js does (see that fixture's
// own comment in fixtures.js): #runBtn stays disabled/not interactable
// until the SQL box itself has content, and client.js now hides that box
// by default.
const { testShowSqlVisible: test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Runs one real translate+execute turn so the screen has something
 * genuinely shareable (see client.js's turnIsShareable(): a SQL turn needs
 * actual results, not just generated SQL, before Share will do anything but
 * show its "nothing to share yet" error). Mirrors translate-execute.spec.js's
 * own "running the generated SQL renders a results table" test. */
/** Adds and saves a real custom Postgres connection through the config
 * modal (mirrors config-modal.spec.js's own addCustomDbRow()/save flow) -
 * never actually connected to (config_routes.py's POST handler only
 * validates and persists the descriptor), but real enough to flip
 * ACTIVE_IS_CUSTOM server-side, which is what makes buildShareDatasetRef()
 * share this turn as dataset_ref.kind 'custom' below. Leaves the modal
 * closed and #connDbName reading `${name} (postgres)`. */
async function addAndSaveCustomPostgresConnection(page, name) {
  await page.locator('#configTriggerBadge').click();
  await expect(page.locator('#configModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
  await page.locator('#addCustomDbBtn').click();
  const nameInput = page.locator('.custom-db-name-input').last();
  await expect(nameInput).toBeFocused();
  await nameInput.fill(name);
  await page.locator('.custom-db-url-input').last().fill('postgresql://user:pass@localhost:5432/mydb');
  await page.locator('#configSaveBtn').click();
  await expect(page.locator('#configModal')).toHaveClass(/hidden/);
  await expect(page.locator('#connDbName')).toHaveText(`${name} (postgres)`);
}

/** Directly seeds the SQL box, bypassing translate entirely - same
 * CodeMirror-or-textarea fallback as translate-execute.spec.js's own
 * setSqlBox(). Used below to drive #runBtn/executeSql() directly, without
 * ever going through translatePrompt() first. */
async function setSqlBox(page, sql) {
  await page.evaluate((value) => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) {
      wrapper.CodeMirror.setValue(value);
    } else {
      const textarea = document.getElementById('sqlQuery');
      if (textarea) {
        textarea.value = value;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
  }, sql);
}

/** Runs one real translate+execute turn so the screen has something
 * genuinely shareable (see client.js's turnIsShareable(): a SQL turn needs
 * actual results, not just generated SQL, before Share will do anything but
 * show its "nothing to share yet" error). Mirrors translate-execute.spec.js's
 * own "running the generated SQL renders a results table" test. */
async function runShareableTurn(page, { prompt = 'top 3 countries by population', sql = 'SELECT name, population FROM countries ORDER BY population DESC LIMIT 3;' } = {}) {
  await mockTranslate(page, { sql });
  await mockExecute(page, {
    results: [{
      columns: ['name', 'population'],
      rows: [
        { name: 'CountryA', population: 3000000 },
        { name: 'CountryB', population: 2000000 },
        { name: 'CountryC', population: 1000000 },
      ],
      rowCount: 3,
    }],
  });
  await gotoApp(page);
  await page.locator('#aiPrompt').fill(prompt);
  await page.locator('#aiPrompt').press('Enter');
  await page.locator('#runBtn').click();
  await expect(page.locator('#resultsBody tr')).toHaveCount(3);
}

/** Registers the same harmless-default mocks the `test` fixture gives its
 * own `page` (see fixtures.js) onto a SEPARATE, manually-created page - for
 * the "brand-new visitor" page below, which deliberately isn't built from
 * that fixture (it needs its own context, with no crbot_user_id cookie at
 * all, rather than the fixture's per-test isolated one). Kept to just the
 * handful of automatic background calls every page load makes regardless
 * of what a test is actually about (status ping, chat-history hydration,
 * results-summary) - this spec has no need for a working /api/translate or
 * /api/execute mock on this second page, since it never submits a new
 * question there, only opens a link. */
async function mockBackgroundDefaults(page) {
  await page.route('**/api/ping', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
  });
  await page.route('**/api/chat-history', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ success: true, buckets: {}, active_bucket_key: '' }),
    });
  });
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: JSON.stringify({ success: false }) + '\n' });
  });
}

/** Opens `path` in a fresh, cookie-less browser context - a visitor who has
 * never been to this app before, as opposed to this test's own `page`
 * (which already carries its own private crbot_user_id - see fixtures.js).
 * Returns the new page; the caller is responsible for closing its context. */
async function openAsNewVisitor(browser, baseURL, path) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem('ydylOnboardingSeen', '1');
      window.localStorage.setItem('ydylHelpPulseDismissed', '1');
      window.localStorage.setItem('ydylLiveDemoSeen', '1');
      // Same reason testShowSqlVisible sets this for the sharer's own
      // `page` (see fixtures.js) - a visitor who tries to drive #runBtn
      // directly (the private-datasource block test below) needs the SQL
      // box visible/interactable the same way, since client.js hides it by
      // default.
      window.localStorage.setItem('datalectShowSql', '1');
    } catch (e) { /* ignore */ }
  });
  await mockBackgroundDefaults(page);
  await page.goto(path);
  await page.locator('#connDbName').waitFor({ state: 'attached' });
  await expect.poll(async () => (await page.locator('#connDbName').textContent())?.trim()).not.toBe('');
  return page;
}

test.describe('sharing', () => {
  test('the Share button is visible on a blank slate, and clicking it with nothing to share shows an inline error without hiding the button', async ({ page }) => {
    await gotoApp(page);

    const shareBtn = page.locator('#shareBtn');
    await expect(shareBtn).toBeVisible();

    await shareBtn.click();

    const popover = page.locator('#sharePopover');
    await expect(popover).not.toHaveClass(/hidden/);
    await expect(popover).toHaveClass(/share-popover-error/);
    await expect(page.locator('#sharePopoverStatus')).toHaveText(
      "There's nothing to share yet - ask a question and get some results first."
    );
    // The link input/Copy row is hidden entirely in the error state (see
    // style.css's .share-popover-error rule) - there's no link to show yet.
    await expect(page.locator('.share-popover-row')).toBeHidden();

    // Still there, unconditionally - the whole point of this behavior (see
    // index.html's own comment on #shareBtn: "the button should always be
    // there" rather than appearing/disappearing with whether anything's
    // shareable right now).
    await expect(shareBtn).toBeVisible();
  });

  test('after New Turn is clicked, Share still treats the screen as a blank slate even though a previous turn exists', async ({ page }) => {
    // Regression coverage for a real bug caught while building this feature:
    // buildSharePayload() has to check viewingBlankSlate itself, since
    // chatStore.lastTurn() keeps returning the last REAL turn regardless of
    // whether #newTurnBtn's blank slate is currently showing (see
    // viewingBlankSlate's own docstring in client.js) - without that check,
    // clicking Share right after "New Turn" would silently share the
    // PREVIOUS turn while the screen shows an empty editor.
    await runShareableTurn(page);

    await page.locator('#newTurnBtn').click();
    await expect(page.locator('#aiPrompt')).toHaveValue('');

    await page.locator('#shareBtn').click();
    await expect(page.locator('#sharePopover')).toHaveClass(/share-popover-error/);
  });

  test('after running a question, Share creates a real link and shows it with a working Copy button', async ({ page, baseURL }) => {
    await runShareableTurn(page);

    await page.locator('#shareBtn').click();

    const popover = page.locator('#sharePopover');
    await expect(popover).not.toHaveClass(/hidden/);
    await expect(popover).not.toHaveClass(/share-popover-error/);

    const input = page.locator('#sharePopoverInput');
    // A real POST /api/share round trip against the real server - share_id
    // is SHARE_ID_ALPHABET (lowercase + digits, ambiguous chars removed),
    // SHARE_ID_LENGTH=12 (state_store.py).
    await expect.poll(() => input.inputValue()).toMatch(/^https?:\/\/[^/]+\/[a-z2-9]{12}$/);
    const url = await input.inputValue();
    expect(url.startsWith(baseURL)).toBe(true);

    await page.locator('#sharePopoverCopyBtn').click();
    // Either outcome is a real, working result - which one depends on
    // whether this browser context happened to grant Clipboard API
    // permission (see client.js's sharePopoverCopyBtn handler for both
    // branches); the link itself is already correct and selected either
    // way, so this isn't a test of which branch fires.
    await expect(page.locator('#sharePopoverStatus')).toHaveText(/^(Copied!|Press Ctrl\/Cmd\+C to copy\.)$/);
  });

  test('a fresh, cookie-less visitor who opens the link sees the same question, SQL, and results, and a banner saying the dataset was picked up', async ({ page, browser, baseURL }) => {
    await runShareableTurn(page, {
      prompt: 'top 3 countries by population',
      sql: 'SELECT name, population FROM countries ORDER BY population DESC LIMIT 3;',
    });
    await page.locator('#shareBtn').click();
    const input = page.locator('#sharePopoverInput');
    await expect.poll(() => input.inputValue()).toMatch(/^https?:\/\//);
    const url = await input.inputValue();
    const shareId = new URL(url).pathname.replace(/^\/+/, '');

    const visitorPage = await openAsNewVisitor(browser, baseURL, `/${shareId}`);
    try {
      // Rendered through the exact same path any real turn renders through
      // (restoreLatestTurn() - see maybeRenderSharedView()'s own comment) -
      // not a separate read-only view, so the prompt/results are ordinary,
      // live DOM, not a frozen snapshot the visitor can't interact with.
      await expect(visitorPage.locator('#aiPrompt')).toHaveValue('top 3 countries by population');
      await expect(visitorPage.locator('#resultsBody tr')).toHaveCount(3);
      await expect(visitorPage.locator('#resultsBody')).toContainText('CountryA');

      // This e2e suite's own local-dev server has exactly one configured
      // connection (the single synthetic "Default DB" preset - see
      // playwright.config.js's own comment on why DATABASE_PRESETS_FILE is
      // deliberately left unset) - the sharer's active connection is
      // necessarily that same preset, so a brand-new visitor's session can
      // always be switched to it too (dataset_ref.kind "preset", see
      // buildShareDatasetRef()), and the banner says so.
      const banner = visitorPage.locator('#sharedViewBanner');
      await expect(banner).not.toHaveClass(/hidden/);
      await expect(banner.locator('.shared-view-banner-text')).toHaveText(
        'You opened a result someone shared with you - picked up the same dataset, so you can pick up right where they left off.'
      );

      await visitorPage.locator('#sharedViewDismissBtn').click();
      await expect(banner).toHaveClass(/hidden/);
      // Dismissing only hides the banner - the turn itself is untouched.
      await expect(visitorPage.locator('#resultsBody tr')).toHaveCount(3);
    } finally {
      await visitorPage.context().close();
    }
  });

  test('an unknown/mistyped share link shows the "no longer works" banner instead of a blank or broken page', async ({ browser, baseURL }) => {
    // Well-formed per SHARE_ID_PATH_RE (12 lowercase/digit chars) but never
    // actually created - mirrors test_get_share_unknown_id_is_404 on the
    // server side.
    const visitorPage = await openAsNewVisitor(browser, baseURL, '/abcdefghjkmn');
    try {
      const banner = visitorPage.locator('#sharedViewBanner');
      await expect(banner).not.toHaveClass(/hidden/);
      await expect(banner.locator('.shared-view-banner-text')).toHaveText(
        "This shared link doesn't work anymore - it may have been mistyped, or no longer exists."
      );
    } finally {
      await visitorPage.context().close();
    }
  });

  test('a visitor who opens a link shared from a PRIVATE datasource sees the datasource name and the full shared question/SQL/results, but is blocked from issuing a new one', async ({ page, browser, baseURL }) => {
    // Regression coverage for a real bug report: a 'custom' dataset_ref
    // (see buildShareDatasetRef()) used to let the visitor's own next NL
    // question or manual SQL Execute quietly run against whatever dataset
    // THEIR session already had active - unrelated to the sharer's private
    // one, and never surfaced as such - rather than being blocked outright.
    // See sharedViewBlockedForPrivateDataset's own declaration comment in
    // client.js for the full reasoning.
    await gotoApp(page);
    await addAndSaveCustomPostgresConnection(page, 'My Private DB');
    await runShareableTurn(page, {
      prompt: 'top 3 countries by population',
      sql: 'SELECT name, population FROM countries ORDER BY population DESC LIMIT 3;',
    });

    await page.locator('#shareBtn').click();
    const input = page.locator('#sharePopoverInput');
    await expect.poll(() => input.inputValue()).toMatch(/^https?:\/\//);
    const url = await input.inputValue();
    const shareId = new URL(url).pathname.replace(/^\/+/, '');

    const visitorPage = await openAsNewVisitor(browser, baseURL, `/${shareId}`);
    try {
      // Registered BEFORE either blocked attempt below, with a deliberately
      // distinguishable "LEAKED" marker - if translatePrompt()/executeSql()
      // ever failed to block and let the request through, these would
      // render as real success content instead of the "Unauthorized
      // Access" tile, which the assertions below would then fail to find.
      // Proves the block stops the request client-side, rather than just
      // happening to resemble whatever the real (unmocked) backend would
      // have done with no Gemini key configured in this sandbox.
      await visitorPage.route('**/api/translate', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ success: true, sql: "SELECT 'LEAKED';" }),
        });
      });
      await visitorPage.route('**/api/execute', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({
            success: true,
            results: [{ columns: ['one'], rows: [{ one: 'LEAKED' }], rowCount: 1 }],
          }),
        });
      });

      // The banner names which private datasource this came from (part of
      // "what was shared", same as the prompt/SQL/results themselves) and
      // says the visitor may keep using the agent against their OWN
      // datasources instead - it does not claim this will keep working
      // against the sharer's, unlike the preset/group banner above.
      const banner = visitorPage.locator('#sharedViewBanner');
      await expect(banner).not.toHaveClass(/hidden/);
      await expect(banner.locator('.shared-view-banner-text')).toContainText(
        "You opened a result someone shared with you that uses a private datasource you don't have access to. "
        + "You may continue using the agent against the datasources you have access to."
      );
      await expect(banner.locator('.shared-view-banner-text')).toContainText('My Private DB (postgres)');

      // The shared question, SQL, and results are still fully visible -
      // viewing what was explicitly shared is fine even though the
      // underlying datasource itself isn't reachable.
      await expect(visitorPage.locator('#aiPrompt')).toHaveValue('top 3 countries by population');
      await expect(visitorPage.locator('#resultsBody tr')).toHaveCount(3);
      await expect(visitorPage.locator('#resultsBody')).toContainText('CountryA');

      // Trying to ask a NEW question is blocked outright, not silently run
      // against the visitor's own current dataset.
      await visitorPage.locator('#aiPrompt').fill('a totally different question');
      await visitorPage.locator('#aiPrompt').press('Enter');
      await expect(visitorPage.locator('.error-details strong')).toHaveText('Unauthorized Access');
      await expect(visitorPage.locator('#resultsBody')).not.toContainText('LEAKED');

      // Editing the SQL box directly and clicking Execute - the OTHER way
      // to issue a new query, with no translatePrompt() call in between -
      // is blocked the same way.
      await setSqlBox(visitorPage, "SELECT 'should not run';");
      await visitorPage.locator('#runBtn').click();
      await expect(visitorPage.locator('.error-details strong')).toHaveText('Unauthorized Access');
      await expect(visitorPage.locator('#resultsBody')).not.toContainText('LEAKED');

      // Once the visitor explicitly picks a dataset of their own (exactly
      // what the banner tells them to do) and saves it, the block lifts -
      // a real question against their own, now-current dataset works
      // normally again. Re-registering these same two routes (now with
      // real, non-"LEAKED" success content) takes priority over the ones
      // above for everything from here on - Playwright matches the most
      // recently added handler first.
      await visitorPage.route('**/api/translate', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({ success: true, sql: 'SELECT 1;' }),
        });
      });
      await visitorPage.route('**/api/execute', async (route) => {
        if (route.request().method() !== 'POST') return route.fallback();
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({
            success: true,
            results: [{ columns: ['one'], rows: [{ one: 1 }], rowCount: 1 }],
          }),
        });
      });
      await visitorPage.locator('#configTriggerBadge').click();
      await expect(visitorPage.locator('#configModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
      await visitorPage.locator('#configSaveBtn').click();
      await expect(visitorPage.locator('#configModal')).toHaveClass(/hidden/);

      await visitorPage.locator('#aiPrompt').fill('a totally different question');
      await visitorPage.locator('#aiPrompt').press('Enter');
      await visitorPage.locator('#runBtn').click();
      await expect(visitorPage.locator('#resultsBody tr')).toHaveCount(1);
      await expect(visitorPage.locator('#resultsBody')).toContainText('1');
    } finally {
      await visitorPage.context().close();
    }
  });
});
