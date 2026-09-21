// tests/e2e/chat-history-persistence.spec.js
//
// Proves the actual server-backed chat-history round trip works end to
// end: client.js's persistChatBucket() really reaches the real Flask
// server + real SqliteStateStore (chat_history table), and
// hydrateChatHistoryFromServer() really restores it on a fresh page load.
//
// Every OTHER e2e spec that touches this area either mocks
// /api/chat-history entirely (auth-clears-state.spec.js, deliberately -
// see its own comment on why: the local test server has no
// GOOGLE_CLIENT_ID, so its fake per-test "signed-in" identities all
// collapse onto the same real server identity, which would otherwise let
// one test's persisted history bleed into another's) or doesn't exercise
// it at all. None of those prove the real save-then-restore path itself
// works, which is this file's only job - so, unlike most of this suite,
// /api/chat-history*/ is intentionally left UNMOCKED here. /api/translate
// and /api/execute are still mocked as usual (mockTranslate/mockExecute) -
// this file has nothing to do with real LLM/DB behavior.
//
// fixtures.js's shared `test` fixture now installs a fast default mock for
// GET /api/chat-history and GET /api/chat-history/summary (so the rest of
// the suite doesn't pay for a real round trip it doesn't care about - see
// that file's own comment). Both tests below call page.unroute() on those
// two patterns before their first gotoApp(), which removes that default
// and restores plain pass-through to the real backend for the whole test -
// without it, hydrateChatHistoryFromServer() and loadChatHistorySummary()
// would silently get the fixture's empty-history mock instead of what this
// file actually saved, and every assertion below would fail.
//
// This runs under the shared "global" identity (no auth configured
// locally), the same identity every other unmocked spec in this suite
// runs under - so the turn pushed here is explicitly cleaned up (bucket
// saved back to `turns: []`) at the end of the test, mirroring
// preferences-modal.spec.js's own "restore original value so this test
// doesn't leak state" convention. Without that cleanup, a later suite run
// could see this stale turn served back by a real, unmocked
// hydrateChatHistoryFromServer() call in some other spec file.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Mirrors translate-execute.spec.js's own currentSql()/normalizedSql() -
 * not exported from fixtures.js, so redefined locally here too. */
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

test.describe('chat history persistence', () => {
  test('a translated turn is saved to the real server and restored after a reload', async ({ page }) => {
    // See the module comment above: opt this test out of the fixture's
    // default chat-history mocks before the first gotoApp(), so both the
    // initial and the post-save-reload hydrateChatHistoryFromServer()
    // calls below hit the real server.
    await page.unroute('**/api/chat-history');
    await page.unroute('**/api/chat-history/summary');
    await mockTranslate(page, { sql: 'SELECT * FROM users LIMIT 5;' });
    await mockExecute(page, {
      results: [{ columns: ['id'], rows: [{ id: 1 }], rowCount: 1 }],
    });
    await gotoApp(page);

    const prompt = `chat history persistence e2e ${Date.now()}`;

    // Real (unmocked) POST fired by client.js's persistChatBucket(), from
    // createChatHistoryStore()'s onPersist hook, itself triggered by
    // chatStore.pushTurn() as soon as translate succeeds - see
    // translatePrompt()'s plain (non-router_route) branch, which pushes
    // the turn before execution even starts.
    const saveRequestPromise = page.waitForRequest(
      (req) => req.url().includes('/api/chat-history/save') && req.method() === 'POST'
    );

    await page.locator('#aiPrompt').fill(prompt);
    await page.locator('#aiPrompt').press('Enter');

    const saveRequest = await saveRequestPromise;
    const savedBody = saveRequest.postDataJSON();
    const bucketKey = savedBody.bucket_key;
    expect(bucketKey).toBeTruthy();

    await expect.poll(() => normalizedSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsBody')).toContainText('1');

    try {
      // Confirm the real server actually has it, not just that the
      // request was sent - GET /api/chat-history is real (unmocked) too.
      const afterSave = await page.request.get('/api/chat-history');
      const afterSaveBody = await afterSave.json();
      const savedTurns = afterSaveBody.buckets[bucketKey];
      expect(Array.isArray(savedTurns)).toBe(true);
      expect(savedTurns.some((t) => t.role === 'user' && t.text === prompt)).toBe(true);
      expect(savedTurns.some((t) => t.role === 'model' && (t.text || '').includes('SELECT'))).toBe(true);

      // The actual proof: a completely fresh page load (no in-memory
      // client state survives this) still shows the restored turn - the
      // prompt box and SQL editor are repopulated purely from
      // hydrateChatHistoryFromServer()'s real GET /api/chat-history call
      // and reconcileActiveHistoryBucket()'s subsequent restoreLatestTurn(),
      // not from anything left over in the page.
      await gotoApp(page);

      await expect(page.locator('#aiPrompt')).toHaveValue(prompt);
      await expect.poll(() => normalizedSql(page)).toContain('SELECT');
      expect(await normalizedSql(page)).toContain('users');
    } finally {
      // Clean up: clear this bucket server-side so this test doesn't leak
      // a persisted turn into the shared "global" identity's default
      // connection bucket for any other spec file's future run.
      await page.request.post('/api/chat-history/save', {
        data: { bucket_key: bucketKey, turns: [] },
      });
    }
  });

  // Regression test for chatStore.persistCurrent() (see its own docstring):
  // a turn is pushed to the server as soon as translate() returns (bare SQL,
  // no results yet - see translatePrompt()'s chatStore.setPending() call),
  // then executeSql() fills in that SAME turn's results IN PLACE once
  // execution finishes. Before persistCurrent() existed, that in-place edit
  // never triggered a second save - the server's copy of the turn was stuck
  // with whatever was true at push time (no results at all), and only ever
  // caught up by accident if some LATER turn's own pushTurn() happened to
  // re-save the whole (by-then-mutated) history array first. A reload right
  // after running a query - with no later turn to paper over it - reproduced
  // this exactly: the SQL editor restored fine, but the results table came
  // back empty, as if the query had never been run.
  test("a turn's results survive a reload, not just its SQL - regression for the in-place-mutation persistence gap", async ({ page }) => {
    await page.unroute('**/api/chat-history');
    await page.unroute('**/api/chat-history/summary');
    await mockTranslate(page, { sql: 'SELECT id, name FROM widgets;' });
    await mockExecute(page, {
      results: [{ columns: ['id', 'name'], rows: [{ id: 1, name: 'Gadget' }], rowCount: 1 }],
    });
    // Single-connection mode's post-execution summarization call - left
    // unmocked, this hits the real /api/summarize-result endpoint, which
    // then tries a real LLM call that's unreachable in a sandboxed test
    // environment and only gives up after a real retry/backoff delay.
    // chatStore.persistCurrent() (what this test is actually checking for)
    // fires AFTER that call settles either way, so mocking it keeps this
    // deterministic and fast. Whether this succeeds or "fails" (a failure
    // still produces its own apology summary text - see the "apology tab"
    // test in translate-execute.spec.js), a leading Summary tab ends up
    // persisted and active either way (prependSingleModeSummaryTab) - this
    // test clicks past it to the actual query-results tab below rather
    // than trying to avoid it.
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: false, error: 'summarization disabled for this test' }),
      });
    });
    await gotoApp(page);

    const prompt = `chat history results-persist e2e ${Date.now()}`;

    // Collects EVERY /api/chat-history/save POST this turn causes - there
    // should be at least two: pushTurn()'s own save the moment translate()
    // returns (bare SQL, no results yet - see translatePrompt()'s
    // chatStore.setPending() call), and a SECOND one from
    // chatStore.persistCurrent() once executeSql() fills in that SAME
    // turn's results in place. Collected via a running listener (not one
    // `waitForRequest` per phase) since this app's default is auto-execute
    // ON - the second save can already be in flight, or done, by the time
    // this test would otherwise get around to registering a wait for it.
    const saveRequests = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/chat-history/save') && req.method() === 'POST') {
        saveRequests.push(req);
      }
    });

    // Read the real, current auto-execute preference DIRECTLY, rather than
    // racing it: if it's on (this app's default), translate() alone will
    // trigger the execution (and the second save this test is checking
    // for) internally - clicking Run too would fire a genuinely separate,
    // second execution and push a SECOND turn instead of exercising the
    // in-place-mutation path this test exists to cover. If it's off, this
    // turn is left as bare, unexecuted SQL - awaiting a manual click, same
    // as a real user would do.
    const configResp = await page.request.get('/api/config');
    const autoExecuteEnabled = (await configResp.json()).auto_sql_execute !== false;

    await page.locator('#aiPrompt').fill(prompt);
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => normalizedSql(page)).toContain('SELECT');

    expect(saveRequests.length).toBeGreaterThan(0);
    const bucketKey = saveRequests[0].postDataJSON().bucket_key;

    try {
      if (!autoExecuteEnabled) {
        await page.locator('#runBtn').click();
      }

      // The actual regression check: without chatStore.persistCurrent(),
      // there is only ever the one, pre-execution save above - this turn's
      // results never reach the server at all until some LATER, unrelated
      // turn's own pushTurn() happens to re-save the whole (by-then-
      // mutated) history array first, purely by accident.
      await expect.poll(() => saveRequests.length, {
        message: 'expected a second /api/chat-history/save once results were filled in (chatStore.persistCurrent())',
      }).toBeGreaterThanOrEqual(2);

      // The mocked (failed) summarization above still prepends its own
      // leading, active "Summary" tab (see this test's own comment above)
      // - switch to the actual query-results tab (index 1) to see the
      // real table underneath it.
      await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
      await expect(page.locator('#resultsBody')).toContainText('Gadget');

      // Confirm the real server actually has the results now, not just
      // that some POST fired.
      const afterExecute = await page.request.get('/api/chat-history');
      const afterExecuteBody = await afterExecute.json();
      const savedTurns = afterExecuteBody.buckets[bucketKey];
      const savedModelTurn = savedTurns.find((t) => t.role === 'model');
      expect(Array.isArray(savedModelTurn.results)).toBe(true);
      expect(savedModelTurn.results[0].rows).toEqual([{ id: 1, name: 'Gadget' }]);

      // The actual proof: a completely fresh page load restores the
      // RESULTS TABLE, not just the SQL editor - this is exactly what came
      // back empty before persistCurrent() existed.
      await gotoApp(page);

      await expect(page.locator('#aiPrompt')).toHaveValue(prompt);
      await expect.poll(() => normalizedSql(page)).toContain('SELECT');
      // Same persisted Summary tab as before reload (the failed-
      // summarization apology text is part of what got persisted too) -
      // switch past it to the real query-results tab again.
      await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
      await expect(page.locator('#resultsHeader th')).toHaveText(['id', 'name']);
      await expect(page.locator('#resultsBody')).toContainText('Gadget');
    } finally {
      await page.request.post('/api/chat-history/save', {
        data: { bucket_key: bucketKey, turns: [] },
      });
    }
  });

  test('the History modal lists a real saved turn, and deleting it clears both the server and the visible turn', async ({ page }) => {
    // /api/chat-history/summary is real (unmocked) here too - this is the
    // one test in the suite proving the whole "list databases with saved
    // turns, delete one" feature actually works end to end, not just that
    // the right requests are fired (see analytics.spec.js's
    // chat_history_delete_clicked tests for that, with a mocked summary).
    await page.unroute('**/api/chat-history');
    await page.unroute('**/api/chat-history/summary');
    await mockTranslate(page, { sql: 'SELECT * FROM widgets;' });
    await mockExecute(page, {
      results: [{ columns: ['id'], rows: [{ id: 1 }], rowCount: 1 }],
    });
    await gotoApp(page);

    const prompt = `chat history delete e2e ${Date.now()}`;
    const saveRequestPromise = page.waitForRequest(
      (req) => req.url().includes('/api/chat-history/save') && req.method() === 'POST'
    );
    await page.locator('#aiPrompt').fill(prompt);
    await page.locator('#aiPrompt').press('Enter');
    const bucketKey = (await saveRequestPromise).postDataJSON().bucket_key;
    await expect.poll(() => normalizedSql(page)).toContain('SELECT');

    try {
      // This connection's own bucket now shows up in the real, resolved
      // list - by name/type (the local dev fallback preset, "Default DB"/
      // postgres - see app_config.py's own CONFIGURED_DBS fallback), not
      // just as a bare bucket_key.
      await page.locator('#historyBtn').click();
      const row = page.locator('.chat-history-bucket-row', { hasText: 'Default DB' });
      await expect(row).toBeVisible();
      await expect(row.locator('.chat-history-bucket-type')).toHaveText('postgres');
      await expect(row.locator('.chat-history-bucket-count')).toHaveText('1 turn');

      await row.locator('.chat-history-bucket-delete-btn').click();
      await expect(page.locator('#confirmModal')).not.toHaveClass(/hidden/);
      await page.locator('#confirmModalOkBtn').click();

      // Gone from the list once the real DELETE-via-empty-save round trip
      // and the list's own refetch both settle.
      await expect(page.locator('.chat-history-bucket-row', { hasText: 'Default DB' })).toHaveCount(0);

      // The server row really is empty now, not just hidden client-side.
      const afterDelete = await page.request.get('/api/chat-history');
      const afterDeleteBody = await afterDelete.json();
      expect(afterDeleteBody.buckets[bucketKey] || []).toEqual([]);

      // This WAS the active bucket - deleting it from the modal blanks the
      // still-open turn behind it immediately, not just on a future reload
      // (see clearChatHistoryBucket()'s own docstring on why this needs an
      // explicit in-memory eviction, not only the server-side save).
      await page.locator('#historyModalCloseBtn').click();
      await expect(page.locator('#aiPrompt')).toHaveValue('');
      await expect.poll(() => normalizedSql(page)).toBe('');
    } finally {
      // Redundant with the delete this test itself performs in the
      // success path, but cheap insurance if an assertion above throws
      // first - same leak-prevention reasoning as the test above.
      await page.request.post('/api/chat-history/save', {
        data: { bucket_key: bucketKey, turns: [] },
      });
    }
  });

  // Regression test for renderChatHistoryBucketList()'s own filter: a
  // bucket whose preset/custom connection/dataset group has since been
  // deleted (chat_history_routes.py's _resolve_bucket_display() resolves
  // it to {available: false}) used to still render as its own row, labeled
  // "Unavailable preset"/"Unavailable connection"/"Unavailable dataset
  // group" - meaningless to a user browsing this list, since there's
  // nothing left to resume, rename, or identify by name. It's now
  // cross-referenced away from the rendered list entirely, while the
  // server keeps reporting it (so #deleteAllChatHistoryBtn can still reach
  // and clear it - see that button's own comment in client.js).
  test('an orphaned bucket (its preset no longer exists) is left out of the History modal, not shown as "Unavailable ..."', async ({ page }) => {
    await page.unroute('**/api/chat-history');
    await page.unroute('**/api/chat-history/summary');
    await gotoApp(page);

    // Seeded directly via the real save endpoint - no UI turn needed. A
    // "preset:" bucket_key suffix that can't match any of this local dev
    // server's real CONFIGURED_DBS ids is exactly the shape
    // _resolve_bucket_display() resolves to {kind: "preset", available:
    // false} for.
    const orphanBucketKey = `preset:does-not-exist-e2e-${Date.now()}`;
    await page.request.post('/api/chat-history/save', {
      data: {
        bucket_key: orphanBucketKey,
        turns: [
          { role: 'user', text: 'orphaned turn' },
          { role: 'model', text: 'SELECT 1;' },
        ],
      },
    });

    try {
      // The server still resolves and reports this bucket (available:
      // false) - proves the fix is a client-side display filter, not data
      // loss on the server's own summary endpoint.
      const summaryResp = await page.request.get('/api/chat-history/summary');
      const summaryBody = await summaryResp.json();
      const orphanEntry = summaryBody.buckets.find((b) => b.bucket_key === orphanBucketKey);
      expect(orphanEntry).toBeTruthy();
      expect(orphanEntry.kind).toBe('preset');
      expect(orphanEntry.available).toBe(false);

      await page.locator('#historyBtn').click();
      await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);

      // Never rendered as its own row, under any label - not the generic
      // "Unavailable preset" text, and no delete button wired to its
      // bucket_key either.
      await expect(page.locator('.chat-history-bucket-row', { hasText: 'Unavailable preset' })).toHaveCount(0);
      await expect(page.locator(`.chat-history-bucket-delete-btn[data-bucket-key="${orphanBucketKey}"]`)).toHaveCount(0);
    } finally {
      await page.request.post('/api/chat-history/save', {
        data: { bucket_key: orphanBucketKey, turns: [] },
      });
    }
  });

  // Regression test for a second instance of the exact same bug: the fixed
  // "all" bucket_key (a fossil from the removed "all mode" feature - see
  // _resolve_bucket_display's own comment) used to be hardcoded as
  // {available: true} specifically so it would render with its special
  // label instead of falling through to "Unknown connection" - but "all
  // mode" is gone, so there's no more a live feature behind THIS bucket_key
  // than there is behind a deleted preset. It's now hardcoded
  // {available: false} too, so it's cross-referenced away by the exact same
  // client-side filter as any other orphan, not shown as "All
  // Pre-Configured Datasets (combined)" for a feature that no longer
  // exists.
  test('the fixed "all" bucket (a fossil from the removed "all mode" feature) is left out of the History modal too', async ({ page }) => {
    await page.unroute('**/api/chat-history');
    await page.unroute('**/api/chat-history/summary');
    await gotoApp(page);

    await page.request.post('/api/chat-history/save', {
      data: {
        bucket_key: 'all',
        turns: [
          { role: 'user', text: 'all-mode turn' },
          { role: 'model', text: 'SELECT 1;' },
        ],
      },
    });

    try {
      const summaryResp = await page.request.get('/api/chat-history/summary');
      const summaryBody = await summaryResp.json();
      const allEntry = summaryBody.buckets.find((b) => b.bucket_key === 'all');
      expect(allEntry).toBeTruthy();
      expect(allEntry.kind).toBe('all');
      expect(allEntry.name).toBe('All Pre-Configured Datasets (combined)');
      expect(allEntry.available).toBe(false);

      await page.locator('#historyBtn').click();
      await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);

      await expect(page.locator('.chat-history-bucket-row', { hasText: 'All Pre-Configured Datasets (combined)' })).toHaveCount(0);
      await expect(page.locator('.chat-history-bucket-delete-btn[data-bucket-key="all"]')).toHaveCount(0);
    } finally {
      await page.request.post('/api/chat-history/save', {
        data: { bucket_key: 'all', turns: [] },
      });
    }
  });
});
