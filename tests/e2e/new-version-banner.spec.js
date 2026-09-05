// tests/e2e/new-version-banner.spec.js
//
// "A new version of Datalect is available" reload nudge (see server/
// app_config.py's CLIENT_BUILD_ID, config_routes.py's GET /api/client-
// version, and client.js's fetchClientBuildId()/checkForNewClientVersion()).
// client.js captures a build id once at startup, then polls the same
// endpoint every 5 minutes; if a later poll returns a DIFFERENT id, the
// banner (#newVersionBanner) appears with a Reload button, without ever
// forcing a reload itself. GET /api/client-version is mocked here (a
// counter-based route returns one id for the first request - the startup
// fetch - and either the SAME or a DIFFERENT id for every request after,
// depending on what each test wants to prove); every other endpoint is the
// real local server via playwright.config.js's webServer, same as the rest
// of this suite.
//
// Playwright's Clock API (page.clock) fakes the page's timers so the
// 5-minute setInterval can be fast-forwarded without an actual 5-minute
// wait - it only virtualizes Date/setTimeout/setInterval, not the real
// fetch/network stack, so the mocked GET /api/client-version calls the
// interval fires still resolve for real; the assertions below use Playwright's
// auto-retrying `expect(...)` (rather than asserting immediately after
// runFor) to give that real round trip a moment to land.

const { test, expect, gotoApp } = require('./fixtures');

const FIVE_MINUTES_MS = 5 * 60 * 1000;

/** Installs a GET /api/client-version mock: the Nth request gets `ids[N-1]`
 * (clamped to the last entry once requests run past the list) - e.g.
 * mockClientVersion(page, ['v1', 'v1']) keeps returning 'v1' forever,
 * mockClientVersion(page, ['v1', 'v2']) changes on the second request
 * onward. */
async function mockClientVersion(page, ids) {
  let callCount = 0;
  await page.route('**/api/client-version', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    callCount += 1;
    const id = ids[Math.min(callCount, ids.length) - 1];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ client_build_id: id }),
    });
  });
}

test.describe('New version reload banner', () => {
  test('stays hidden when the build id does not change across a poll', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v1']);
    await page.clock.install();
    await gotoApp(page);

    await expect(page.locator('#newVersionBanner')).toHaveClass(/hidden/);

    await page.clock.runFor(FIVE_MINUTES_MS + 1000);

    // Give the (real, mocked) fetch triggered by the interval a moment to
    // resolve, then confirm it changed nothing.
    await expect(page.locator('#newVersionBanner')).toHaveClass(/hidden/);
  });

  test('appears once a poll reports a changed build id, and Reload reloads the page', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v2']);
    await page.clock.install();
    await gotoApp(page);

    await expect(page.locator('#newVersionBanner')).toHaveClass(/hidden/);
    await expect(page.locator('#newVersionBanner')).toContainText('A new version of Datalect is available');

    await page.clock.runFor(FIVE_MINUTES_MS + 1000);

    await expect(page.locator('#newVersionBanner')).not.toHaveClass(/hidden/);

    // Reload never happens on its own - only via the explicit button - and
    // clicking it performs a real page reload rather than just hiding the
    // banner or mutating some in-memory flag.
    await page.evaluate(() => { window.__preReloadMarker = true; });
    await Promise.all([
      page.waitForNavigation(),
      page.locator('#newVersionReloadBtn').click(),
    ]);
    const markerSurvivedReload = await page.evaluate(() => window.__preReloadMarker);
    expect(markerSurvivedReload).toBeUndefined();
    // The reload lands on a working app, not a broken/blank page.
    await page.locator('#connDbName').waitFor({ state: 'attached' });
  });

  test('Dismiss hides the banner and it does not reappear on a later poll', async ({ page }) => {
    await mockClientVersion(page, ['build-v1', 'build-v2', 'build-v2', 'build-v2']);
    await page.clock.install();
    await gotoApp(page);

    await page.clock.runFor(FIVE_MINUTES_MS + 1000);
    await expect(page.locator('#newVersionBanner')).not.toHaveClass(/hidden/);

    await page.locator('#newVersionDismissBtn').click();
    await expect(page.locator('#newVersionBanner')).toHaveClass(/hidden/);

    // Further polls (id unchanged from the one that already triggered the
    // banner) must not un-dismiss it.
    await page.clock.runFor(FIVE_MINUTES_MS);
    await expect(page.locator('#newVersionBanner')).toHaveClass(/hidden/);
  });
});
