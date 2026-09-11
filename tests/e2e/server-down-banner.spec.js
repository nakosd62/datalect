// tests/e2e/server-down-banner.spec.js
//
// "Datalect server is currently not available" outage banner (see
// client.js's markServerUnreachable()/markServerReachable(),
// fetchClientBuildId()/checkForNewClientVersion() - the same periodic
// /api/client-version poll new-version-banner.spec.js covers - and the
// catch blocks in translatePrompt()/executeSql() that also call
// markServerUnreachable() directly).
//
// Two independent things raise #serverDownBanner: the periodic poll (any
// non-2xx response OR a thrown fetch exception - see fetchClientBuildId()'s
// own comment on why /api/client-version's plain, auth-free posture makes
// a 4xx meaningful there in a way it wouldn't be for /api/translate or
// /api/execute) and a translate/execute request that itself couldn't reach
// the server. Symmetrically, it's cleared either by the periodic poll
// succeeding again OR by any translate/execute request reaching the server
// and getting a real response back (success or an ordinary app-level
// error - see markServerReachable()'s own call sites for why "reached the
// server at all" is the bar, not "this particular query/prompt succeeded").
//
// Every actual false->true/true->false transition also fires a GA event -
// 'server_down'/'server_up' (see markServerUnreachable()'s/
// markServerReachable()'s own comments) - regardless of which of the above
// found it, tagged with a `source` param (poll/translate/execute/
// execute_all_mode) saying which one did. Deliberately edge-triggered, not
// level-triggered: markServerReachable() in particular runs on every
// successful translate/execute, which would otherwise fire a 'server_up'
// on nearly every ordinary query.
//
// Mirrors new-version-banner.spec.js's own mocking/Clock conventions
// (mockClientVersion below is copied from that file rather than shared,
// since it's a small, self-contained helper and the two files are
// deliberately independent).

const { test, expect, gotoApp, mockTranslate } = require('./fixtures');

const FIVE_MINUTES_MS = 5 * 60 * 1000;

/** Mirrors chat-history-persistence.spec.js's own currentSql() - not
 * exported from fixtures.js, so redefined locally here too. */
function currentSql(page) {
  return page.evaluate(() => {
    const wrapper = document.querySelector('.CodeMirror');
    if (wrapper && wrapper.CodeMirror) return wrapper.CodeMirror.getValue();
    const textarea = document.getElementById('sqlQuery');
    return textarea ? textarea.value : null;
  });
}

/** Mirrors analytics.spec.js's own trackedEvents() - not exported from
 * fixtures.js, so redefined locally here too. */
async function trackedEvents(page, name) {
  return page.evaluate((eventName) => {
    return (window.dataLayer || [])
      .filter((entry) => entry && entry[0] === 'event' && entry[1] === eventName)
      .map((entry) => entry[2] || {});
  }, name);
}

/** Installs a GET /api/client-version mock. Each entry in `responses` is
 * either a build-id string (200 OK) or {status} for a non-ok response; the
 * Nth request gets `responses[N-1]` (clamped to the last entry once
 * requests run past the list). */
async function mockClientVersion(page, responses) {
  let callCount = 0;
  await page.route('**/api/client-version', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    callCount += 1;
    const entry = responses[Math.min(callCount, responses.length) - 1];
    if (typeof entry === 'object' && entry !== null && 'status' in entry) {
      await route.fulfill({ status: entry.status, contentType: 'application/json', body: '{}' });
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ client_build_id: entry }),
      });
    }
  });
}

test.describe('Server-down banner', () => {
  test('stays hidden on a normal page load and after a poll that keeps succeeding', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await page.clock.install();
    await gotoApp(page);

    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    await page.clock.runFor(FIVE_MINUTES_MS + 1000);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);
  });

  test('appears immediately at page load if the server is unreachable from the very first poll', async ({ page }) => {
    // Every request to /api/client-version fails outright (fetch itself
    // rejects) - including the un-awaited startup fetch fired before
    // fetchBackendConfig(), so this needs no clock advance at all: the
    // banner should already be up by the time gotoApp() resolves.
    await page.route('**/api/client-version', (route) => route.abort());
    await gotoApp(page);

    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);
    await expect(page.locator('#serverDownBanner')).toContainText(
      'Datalect server is currently not available. Please try again later. We apologize for the inconvenience.'
    );
    // Unlike the new-version nudge, there is nothing to click to hide it.
    await expect(page.locator('#serverDownBanner button')).toHaveCount(0);
  });

  test('a later poll that starts failing (network exception) raises the banner, and a poll that succeeds again clears it', async ({ page }) => {
    await mockClientVersion(page, ['build-v1']);
    // First (startup) request succeeds via the route above; then swap to a
    // hard failure for every request after, then back to success.
    let phase = 'up';
    await page.unroute('**/api/client-version');
    let callCount = 0;
    await page.route('**/api/client-version', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      callCount += 1;
      if (callCount === 1 || phase === 'up') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ client_build_id: 'build-v1' }),
        });
      }
      return route.abort();
    });
    await page.clock.install();
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    phase = 'down';
    await page.clock.runFor(FIVE_MINUTES_MS + 1000);
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);

    phase = 'up';
    await page.clock.runFor(FIVE_MINUTES_MS);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);
  });

  test('a non-ok (4xx/5xx) response from a poll counts as down too, not just a thrown network exception', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', { status: 503 }]);
    await page.clock.install();
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    await page.clock.runFor(FIVE_MINUTES_MS + 1000);
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);
  });

  test('a translate request that cannot reach the server shows the banner immediately, without waiting for the next poll', async ({ page }) => {
    // The periodic poll itself keeps succeeding throughout - proving this
    // signal is independent of it, not a side effect of some poll tick
    // that happened to land around the same time.
    await mockClientVersion(page, ['build-v1', 'build-v1', 'build-v1']);
    await page.route('**/api/translate', (route) => route.abort());
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);
  });

  test('an execute request that cannot reach the server also shows the banner', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await mockTranslate(page, { sql: 'SELECT * FROM users;' });
    await page.route('**/api/execute', (route) => route.abort());
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');

    await page.locator('#runBtn').click();

    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);
  });

  test('a successful translate request clears an already-showing banner, without waiting for the next poll', async ({ page }) => {
    // The poll never runs again during this test (5 minutes never
    // elapses) - proving the clear comes from the translate call itself,
    // not from some poll tick sneaking in.
    await page.route('**/api/client-version', (route) => route.abort());
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);

    await mockTranslate(page, { sql: 'SELECT * FROM users;' });
    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');

    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);
  });

  test('an execute request that reaches the server also clears an already-showing banner, even when the SQL itself fails', async ({ page }) => {
    // Reaching the server and getting back an ordinary app-level error
    // (bad SQL, in this case) still proves the server itself is up - see
    // markServerReachable()'s own comment on why "reached the server" is
    // the bar, not "this particular query succeeded".
    await page.route('**/api/client-version', (route) => route.abort());
    await mockTranslate(page, { sql: 'SELECT * FROM does_not_exist;' });
    await page.route('**/api/execute', (route) => route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ success: false, error: 'relation "does_not_exist" does not exist' }),
    }));
    await gotoApp(page);
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);

    await page.locator('#aiPrompt').fill('list nothing');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();

    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);
    // The execution error itself is still shown normally - the banner
    // clearing doesn't paper over it.
    await expect(page.locator('#resultsBody')).toContainText('does not exist');
  });
});

test.describe('server_down / server_up GA events', () => {
  test('a poll that finds the server down at startup fires exactly one server_down event, tagged source: poll', async ({ page }) => {
    await page.route('**/api/client-version', (route) => route.abort());
    await gotoApp(page);

    const events = await trackedEvents(page, 'server_down');
    expect(events.length).toBe(1);
    expect(events[0].source).toBe('poll');
    // The mirror-image event never fires just because nothing has
    // succeeded yet - only an actual up-detection should raise it.
    expect((await trackedEvents(page, 'server_up')).length).toBe(0);
  });

  test('repeated poll failures fire server_down only once (edge-triggered, not level-triggered)', async ({ page }) => {
    await page.route('**/api/client-version', (route) => route.abort());
    await page.clock.install();
    await gotoApp(page);

    await page.clock.runFor(FIVE_MINUTES_MS);
    await page.clock.runFor(FIVE_MINUTES_MS);
    await page.clock.runFor(FIVE_MINUTES_MS);

    expect((await trackedEvents(page, 'server_down')).length).toBe(1);
  });

  test('a poll that finds the server up again after being down fires server_up, tagged source: poll', async ({ page }) => {
    let down = true;
    await page.route('**/api/client-version', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      if (down) return route.abort();
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ client_build_id: 'build-v1' }),
      });
    });
    await page.clock.install();
    await gotoApp(page);
    expect((await trackedEvents(page, 'server_down')).length).toBe(1);
    expect((await trackedEvents(page, 'server_up')).length).toBe(0);

    down = false;
    await page.clock.runFor(FIVE_MINUTES_MS + 1000);
    // The poll's own real (mocked) network round trip needs a moment to
    // land - the banner going hidden is the same event loop turn as the
    // trackEvent() call, so waiting on it (Playwright's auto-retrying
    // expect) also guarantees the dataLayer push has already happened by
    // the time the plain page.evaluate() below runs.
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    const upEvents = await trackedEvents(page, 'server_up');
    expect(upEvents.length).toBe(1);
    expect(upEvents[0].source).toBe('poll');
  });

  test('a translate request that cannot reach the server fires server_down tagged source: translate, and does not fire it a second time on a later poll failure', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await page.route('**/api/translate', (route) => route.abort());
    await page.clock.install();
    await gotoApp(page);
    expect((await trackedEvents(page, 'server_down')).length).toBe(0);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    // See the poll test's identical comment above for why this wait (not a
    // bare page.evaluate) is what makes reading dataLayer next reliable.
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'server_down');
    expect(events.length).toBe(1);
    expect(events[0].source).toBe('translate');
  });

  test('a successful translate request after being down fires server_up tagged source: translate', async ({ page }) => {
    await page.route('**/api/client-version', (route) => route.abort());
    await gotoApp(page);
    expect((await trackedEvents(page, 'server_down')).length).toBe(1);

    await mockTranslate(page, { sql: 'SELECT * FROM users;' });
    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('#serverDownBanner')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'server_up');
    expect(events.length).toBe(1);
    expect(events[0].source).toBe('translate');
  });

  test('an execute request that cannot reach the server fires server_down tagged source: execute', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await mockTranslate(page, { sql: 'SELECT * FROM users;' });
    await page.route('**/api/execute', (route) => route.abort());
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#serverDownBanner')).not.toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'server_down');
    expect(events.length).toBe(1);
    expect(events[0].source).toBe('execute');
  });

  test('ordinary successful queries never fire server_down or server_up when the server was never down', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await mockTranslate(page, { sql: 'SELECT * FROM users;' });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('list users');
    await page.locator('#aiPrompt').press('Enter');
    await expect.poll(() => currentSql(page)).toContain('SELECT');
    await page.locator('#runBtn').click();
    await expect(page.locator('#resultsBody')).toBeVisible();

    expect((await trackedEvents(page, 'server_down')).length).toBe(0);
    expect((await trackedEvents(page, 'server_up')).length).toBe(0);
  });
});
