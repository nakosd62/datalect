// tests/e2e/onboarding.spec.js
//
// The first-run guided tour (client.js's startGuidedTour(), gated on the
// 'ydylOnboardingSeen' localStorage flag). Uses fixtures.js's
// `isolatedTest`, NOT the default `test` - the default pre-seeds that flag
// so every other spec doesn't have to fight the tour overlay for clicks,
// which defeats the purpose here. `isolatedTest` still gives each test its
// own private slice of server state (a unique crbot_user_id cookie - see
// fixtures.js's module docstring for why that matters even for a suite
// that's otherwise "just" checking a client-side localStorage flag), just
// without silently seeding that flag itself.

const { isolatedTest: test, expect } = require('./fixtures');

test.describe('first-run onboarding', () => {
  test('a brand-new visitor sees the guided tour, and can skip it', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#tourTooltipTitle')).not.toHaveText('');

    await page.locator('#tourSkipBtn').click();
    await expect(page.locator('#tourOverlay')).toHaveClass(/hidden/);
  });

  test('a returning visitor (onboarding already seen) does not see the tour', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('ydylOnboardingSeen', '1');
    });
    await page.goto('/');
    await expect(page.locator('#connDbName')).not.toHaveText('');
    await expect(page.locator('#tourOverlay')).toHaveClass(/hidden/);
  });

  test('the tour can be replayed from the Help modal', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('ydylOnboardingSeen', '1');
    });
    await page.goto('/');
    await page.locator('#helpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);

    await page.locator('#replayTourBtn').click();
    await expect(page.locator('#helpModal')).toHaveClass(/hidden/);
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
  });
});
