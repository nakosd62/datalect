// tests/e2e/history-anonymous-access.spec.js
//
// Translation history is no longer gated behind sign-in on Cloud Run - now
// that every anonymous visitor gets their own per-session identity (see
// auth.py's ANONYMOUS_USER_ID_PREFIX), their history is already isolated
// from everyone else's, so client.js no longer disables the history
// button or blocks it with the "please sign in" modal for them (see
// updateAnonymousRestrictions() and the #historyBtn click handler).
//
// GET /api/config is mocked to report an anonymous Cloud Run visitor (see
// config-modal.spec.js / auth-clears-state.spec.js for the same real-
// network-risk reasoning behind not standing up a real Cloud Run +
// Firestore backend here) - this only exercises the client-side gating
// this change removed. The actual /api/history request underneath is left
// unmocked and hits the real local dev server, which - after this same
// change on the backend side - now happily answers it too (see
// tests/server/test_history_routes.py for that coverage).

const { test, expect, gotoApp } = require('./fixtures');

const ANONYMOUS_CLOUD_RUN_CONFIG_PAYLOAD = {
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
};

test('an anonymous Cloud Run visitor can open translation history without being blocked', async ({ page }) => {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(ANONYMOUS_CLOUD_RUN_CONFIG_PAYLOAD),
    });
  });

  await gotoApp(page);

  // Not disabled/greyed out, and no "please sign in" title left over from
  // the old gate.
  await expect(page.locator('#historyBtn')).not.toHaveClass(/icon-disabled/);
  await expect(page.locator('#historyBtn')).not.toHaveAttribute('title', /log in/i);

  await page.locator('#historyBtn').click();

  // Opens for real - no login-required interstitial in the way.
  await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);
  await expect(page.locator('#loginRequiredModal')).toHaveClass(/hidden/);
});
