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
});
