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

// Real local-dev servers never have ISSUE_REPORT_* env vars configured (see
// playwright.config.js's webServer block), so sendFeedbackBtn/its tour step
// are hidden by default - same "force the flag via GET /api/config" trick
// report-issue.spec.js and analytics.spec.js already use.
async function mockIssueReportingEnabled(page, enabled) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    json.issue_reporting_enabled = enabled;
    await route.fulfill({ response, json });
  });
}

// Same window.dataLayer-reading approach as analytics.spec.js - see that
// file's header comment for why gtag() itself is never stubbed.
async function trackedEvents(page, name) {
  return page.evaluate((eventName) => {
    return (window.dataLayer || [])
      .filter((entry) => entry && entry[0] === 'event' && entry[1] === eventName)
      .map((entry) => entry[2] || {});
  }, name);
}

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
      if ((await title.textContent())?.includes('responding to your questions')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toHaveText('This is the model responding to your questions');
    await expect(page.locator('#tourTooltipBody')).toContainText('Google, Anthropic, and OpenAI');

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

  test('the tour includes a step spotlighting the "i" schema-info icon beside the DB badge', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    // Same "click Next until the title shows up" approach as the
    // model-badge test below - getTourSteps() places this step right
    // after the DB connection badge one, at every viewport (it's not part
    // of the narrow-header combined more-menu step, which only folds in
    // Help/History/Preferences/sign-in/feedback).
    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent())?.includes('dataset description')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toHaveText('Peek at the dataset description');
    await expect(page.locator('#tourTooltipBody')).toContainText('sample questions you may ask');

    // The spotlight is actually positioned over the small standalone info
    // button, not the badge next to it - same regression guard as the
    // model-badge/gear-button tests below, and specifically the point of
    // this step: it should call out the icon as its own target.
    const iconBox = await page.locator('#datasetSchemaViewerBtn').boundingBox();
    await expect(async () => {
      const spotlightBox = await page.locator('#tourSpotlight').boundingBox();
      expect(Math.abs(spotlightBox.x - iconBox.x)).toBeLessThan(10);
      expect(Math.abs(spotlightBox.y - iconBox.y)).toBeLessThan(10);
    }).toPass({ timeout: 2000 });
  });

  test('the tour includes a step spotlighting the Preferences gear button', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    // Same "click Next until the title shows up" approach as the
    // model-badge test above - getTourSteps() places this step right
    // after the model badge one, at desktop width (under the narrow-
    // header breakpoint it's folded into the combined more-menu step
    // instead - see that step's own body text, covered separately below).
    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent()) === 'Make it yours') break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toHaveText('Make it yours');
    await expect(page.locator('#tourTooltipBody')).toContainText('dark & light mode');

    // The spotlight is actually positioned over the gear button, not some
    // other element - same regression guard as the model-badge test.
    const gearBox = await page.locator('#prefsBtn').boundingBox();
    await expect(async () => {
      const spotlightBox = await page.locator('#tourSpotlight').boundingBox();
      expect(Math.abs(spotlightBox.x - gearBox.x)).toBeLessThan(10);
      expect(Math.abs(spotlightBox.y - gearBox.y)).toBeLessThan(10);
    }).toPass({ timeout: 2000 });
  });

  test('on a narrow (mobile) header, the combined more-menu tour step also mentions Preferences and switching the AI model', async ({ page }) => {
    await page.setViewportSize({ width: 420, height: 800 });
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent())?.includes('preferences')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toContainText('preferences');
    await expect(page.locator('#tourTooltipBody')).toContainText('your preferences');
    // The model badge is ALSO folded into this same combined step at this
    // width (see .header-actions > .model-picker-wrapper in style.css) -
    // its own always-separate wide-header step (checked below) never
    // appears here, so its mention has to live in this body text instead.
    await expect(page.locator('#tourTooltipBody')).toContainText('switch the model');
  });

  test('on a narrow (mobile) header, the model badge\'s own step never appears - it is folded into the combined more-menu step above', async ({ page }) => {
    await page.setViewportSize({ width: 420, height: 800 });
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    const title = page.locator('#tourTooltipTitle');
    const seenTitles = [];
    for (let i = 0; i < 10; i++) {
      seenTitles.push(await title.textContent());
      const nextBtn = page.locator('#tourNextBtn');
      if ((await nextBtn.textContent()) === 'Done') break;
      await nextBtn.click();
    }
    expect(seenTitles.some((t) => t === 'This is the model responding to your questions')).toBe(false);
  });

  test('the tour includes a step spotlighting the Send Feedback button, only when the feature is configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#sendFeedbackBtn')).toBeVisible();

    // getTourSteps() places this step last, right after the Help step - same
    // "click Next until the title shows up" approach as the model-badge/gear
    // tests above.
    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent())?.includes('Have feedback')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(title).toHaveText('Have feedback?');
    await expect(page.locator('#tourTooltipBody')).toContainText('Send us feedback');
    await expect(page.locator('#tourNextBtn')).toHaveText('Done');

    // Regression guard against the step existing but pointing at the wrong
    // target - same spotlight-position check the model-badge/gear tests use.
    const feedbackBox = await page.locator('#sendFeedbackBtn').boundingBox();
    await expect(async () => {
      const spotlightBox = await page.locator('#tourSpotlight').boundingBox();
      expect(Math.abs(spotlightBox.x - feedbackBox.x)).toBeLessThan(10);
      expect(Math.abs(spotlightBox.y - feedbackBox.y)).toBeLessThan(10);
    }).toPass({ timeout: 2000 });
  });

  test('the tour never spotlights Send Feedback, and never mentions it, when the feature is not configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, false);
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#sendFeedbackBtn')).toBeHidden();

    const title = page.locator('#tourTooltipTitle');
    const seenTitles = [];
    for (let i = 0; i < 10; i++) {
      seenTitles.push(await title.textContent());
      const nextBtn = page.locator('#tourNextBtn');
      if ((await nextBtn.textContent()) === 'Done') break;
      await nextBtn.click();
    }
    expect(seenTitles.some((t) => t?.includes('Have feedback'))).toBe(false);
  });

  test('on a narrow (mobile) header, the combined more-menu tour step mentions Send Feedback only when the feature is configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await page.setViewportSize({ width: 420, height: 800 });
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    const title = page.locator('#tourTooltipTitle');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent())?.includes('preferences')) break;
      await page.locator('#tourNextBtn').click();
    }
    await expect(page.locator('#tourTooltipBody')).toContainText('send feedback');
  });

  test('tour_exited fires with step 1 when skipped immediately', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#tourStepCounter')).toContainText('Step 1 of');

    await page.locator('#tourSkipBtn').click();
    await expect(page.locator('#tourOverlay')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'tour_exited');
    expect(events.length).toBe(1);
    expect(events[0].step).toBe(1);
  });

  test('tour_exited fires with the step the user had reached when skipped partway through', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    // Step forward a few times before bailing out - whatever step is
    // showing when Skip is clicked is what should be reported.
    await page.locator('#tourNextBtn').click();
    await page.locator('#tourNextBtn').click();
    const stepText = await page.locator('#tourStepCounter').textContent();
    const expectedStep = Number(stepText.match(/Step (\d+) of/)[1]);

    await page.locator('#tourSkipBtn').click();
    await expect(page.locator('#tourOverlay')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'tour_exited');
    expect(events.length).toBe(1);
    expect(events[0].step).toBe(expectedStep);
  });

  test('tour_exited fires with the final step number when the tour is completed via "Done"', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    const nextBtn = page.locator('#tourNextBtn');
    let totalSteps = null;
    for (let i = 0; i < 20; i++) {
      const stepText = await page.locator('#tourStepCounter').textContent();
      totalSteps = Number(stepText.match(/Step \d+ of (\d+)/)[1]);
      if ((await nextBtn.textContent()) === 'Done') break;
      await nextBtn.click();
    }
    await expect(nextBtn).toHaveText('Done');
    await nextBtn.click();
    await expect(page.locator('#tourOverlay')).toHaveClass(/hidden/);

    const events = await trackedEvents(page, 'tour_exited');
    expect(events.length).toBe(1);
    expect(events[0].step).toBe(totalSteps);
  });

  // Regression guard for a removed feature: this tour used to include a
  // dedicated step spotlighting the "Sample prompts" chip row above the
  // prompt box - removed when that whole section was removed from the app
  // (see app-shell.spec.js's own removal-guard test for the section
  // itself). getTourSteps() in client.js no longer builds that step at
  // all, but this walks every step's actual rendered title/body text as a
  // belt-and-braces check that nothing still references it by name, rather
  // than just trusting the step list was edited correctly.
  test('no step in the guided tour mentions the removed sample-prompts section', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);

    const nextBtn = page.locator('#tourNextBtn');
    const seenText = [];
    for (let i = 0; i < 20; i++) {
      seenText.push(await page.locator('#tourTooltipTitle').textContent());
      seenText.push(await page.locator('#tourTooltipBody').textContent());
      if ((await nextBtn.textContent()) === 'Done') break;
      await nextBtn.click();
    }

    expect(seenText.length).toBeGreaterThan(0);
    const combined = seenText.join(' ').toLowerCase();
    expect(combined).not.toContain('sample prompt');
    expect(combined).not.toContain('quick prompt');
    expect(combined).not.toContain('example prompt');
  });

  // "Show SQL" (client.js's SHOW_SQL_STORAGE_KEY) now defaults to hidden -
  // #editorPaneSql (and its .sql-bubble child the tour used to always
  // spotlight) stays in the DOM either way, just display:none, so
  // getTourSteps() has to actually check visibility rather than just
  // querySelector()-ing for it; these two tests cover both sides of that.
  test('the tour never spotlights the (hidden, default) SQL box, and the Preferences step covers it instead', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#editorPaneSql')).toHaveClass(/hidden/);

    // One single forward walk - checking the gear-button step's body
    // in-place when reached, rather than a second pass afterward, since
    // clicking Next/Done again once the tour has already reached its last
    // step closes it (finishTour()), leaving nothing left to click.
    const title = page.locator('#tourTooltipTitle');
    const nextBtn = page.locator('#tourNextBtn');
    const seenTitles = [];
    let sawPrefsStep = false;
    for (let i = 0; i < 20; i++) {
      const currentTitle = await title.textContent();
      seenTitles.push(currentTitle);
      if (currentTitle === 'Make it yours') {
        sawPrefsStep = true;
        await expect(page.locator('#tourTooltipBody')).toContainText('whether it is shown at all');
      }
      if ((await nextBtn.textContent()) === 'Done') break;
      await nextBtn.click();
    }
    expect(seenTitles).not.toContain("We'll turn that into SQL");
    expect(sawPrefsStep).toBe(true);
  });

  test('the tour spotlights the SQL box when Show SQL has already been turned on', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('datalectShowSql', '1');
    });
    await page.goto('/');
    await expect(page.locator('#tourOverlay')).not.toHaveClass(/hidden/);
    await expect(page.locator('#editorPaneSql')).not.toHaveClass(/hidden/);

    const title = page.locator('#tourTooltipTitle');
    const nextBtn = page.locator('#tourNextBtn');
    for (let i = 0; i < 10; i++) {
      if ((await title.textContent()) === "We'll turn that into SQL") break;
      await nextBtn.click();
    }
    await expect(title).toHaveText("We'll turn that into SQL");

    // Same spotlight-position regression guard as the model-badge/gear
    // tests above - the step exists AND points at the real, now-visible box.
    const sqlBox = await page.locator('.sql-bubble').boundingBox();
    await expect(async () => {
      const spotlightBox = await page.locator('#tourSpotlight').boundingBox();
      expect(Math.abs(spotlightBox.x - sqlBox.x)).toBeLessThan(10);
      expect(Math.abs(spotlightBox.y - sqlBox.y)).toBeLessThan(10);
    }).toPass({ timeout: 2000 });
  });
});
