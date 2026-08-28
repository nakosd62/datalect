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

  test('the tour includes a step spotlighting the model-selection badge', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    // Steps ahead of it (prompt/quick-prompts/sql/results/history-nav/db
    // badge) vary slightly by state (e.g. whether the quick-prompts chips
    // are visible) - clicking Next until the model-badge step's own title
    // appears is what makes this robust to that, rather than hardcoding an
    // index. getTourSteps() places it right after the DB connection badge
    // step - bounded to 10 clicks so a real regression (the step never
    // showing at all) still fails instead of looping forever.
    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent())?.includes('AI model')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toHaveText('This is the AI model translating your questions');
    await expect(page.locator('#tourTooltipBody')).toContainText('Google, Anthropic, OpenAI');

    // The spotlight is actually positioned over the model badge, not some
    // other element - regression guard against the step existing but
    // pointing at the wrong target. #tourSpotlight animates into place via
    // a 0.3s CSS transition (see .tour-spotlight in style.css) - its
    // getBoundingClientRect() reflects wherever that animation currently
    // is, not its assigned style.top/left/width/height, so this polls
    // until the animation has actually settled rather than reading the
    // box the instant the step changes (which would still show the
    // *previous* step's box, mid-transition).
    const badgeBox = await page.locator('#modelTriggerBadge').boundingBox();
    await expect(async () => {
      const spotlightBox = await page.locator('#tourSpotlight').boundingBox();
      expect(Math.abs(spotlightBox.x - badgeBox.x)).toBeLessThan(10);
      expect(Math.abs(spotlightBox.y - badgeBox.y)).toBeLessThan(10);
    }).toPass({ timeout: 2000 });
  });
});
