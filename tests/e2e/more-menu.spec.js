// tests/e2e/more-menu.spec.js
//
// Under a narrow viewport, the header's Help/History/Preferences/Feedback/
// sign-in controls collapse into a single triple-dot "more" menu (see the
// @media (max-width: 900px) block in style.css - right before its "Natural
// Language & SQL Row Layouts" section - and the MORE MENU section in
// client.js) so the connection/model status badges have room to render
// without being squashed. That breakpoint is deliberately the same width
// where the NL/SQL panes switch from side-by-side to stacked, per explicit
// request - see the "collapses at the same width the SQL box stacks under
// the NL box" describe block below for the regression coverage of that
// specific coupling. This file otherwise only tests the collapse itself and
// that the menu's items forward to the real, existing Help/History/Feedback
// behavior - the individual modals' own contents are already covered by
// app-shell.spec.js (Help/History) and report-issue.spec.js (the feedback
// modal).

const { test, expect, gotoApp } = require('./fixtures');

/** Same GET /api/config interception report-issue.spec.js uses to force a
 * known 'issue_reporting_enabled' value - duplicated locally rather than
 * imported, matching this file's existing no-shared-helpers convention. */
async function mockIssueReportingEnabled(page, enabled) {
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const json = await response.json();
    json.issue_reporting_enabled = enabled;
    await route.fulfill({ response, json });
  });
}

test.describe('triple-dot more menu (narrow header)', () => {
  test.use({ viewport: { width: 375, height: 700 } });

  test('collapses Help/History/sign-in/model into the more menu, hiding the individual controls', async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator('#moreMenuBtn')).toBeVisible();
    await expect(page.locator('#helpBtn')).not.toBeVisible();
    await expect(page.locator('#historyBtn')).not.toBeVisible();
    // The model badge folds in here too (see model-selection.spec.js's own
    // "model selection under the more menu" describe block for its
    // dedicated coverage) - #moreMenuModelBtn is its replacement inside the
    // dropdown, not a header-level control, so it isn't checked here.
    await expect(page.locator('#modelTriggerBadge')).not.toBeVisible();

    // The whole point of the collapse - the status badge has to actually
    // fit, not just technically be present.
    await expect(page.locator('#configTriggerBadge')).toBeVisible();
  });

  test('opening the menu shows plain text items, not icons', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    const dropdown = page.locator('#moreMenuDropdown');
    await expect(dropdown).not.toHaveClass(/hidden/);
    await expect(page.locator('#moreMenuHelpBtn')).toHaveText('Doc');
    await expect(page.locator('#moreMenuHistoryBtn')).toHaveText('History');
  });

  test('clicking outside the open menu closes it', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuDropdown')).not.toHaveClass(/hidden/);
    await page.locator('.crbot-title-group').click();
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);
  });

  test('the "Doc" menu item forwards to the real help modal', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    await page.locator('#moreMenuHelpBtn').click();
    await expect(page.locator('#helpModal')).not.toHaveClass(/hidden/);
    // Forwarding via the real button's own click handler also closes the menu.
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);
  });

  test('the "History" menu item forwards to the real history modal', async ({ page }) => {
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    await page.locator('#moreMenuHistoryBtn').click();
    await expect(page.locator('#historyModal')).not.toHaveClass(/hidden/);
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);
  });

  test('the "Feedback" menu item is hidden when issue reporting is not configured', async ({ page }) => {
    await mockIssueReportingEnabled(page, false);
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuFeedbackBtn')).toBeHidden();
  });

  test('the "Feedback" menu item forwards to the send-feedback modal', async ({ page }) => {
    await mockIssueReportingEnabled(page, true);
    await gotoApp(page);

    await page.locator('#moreMenuBtn').click();
    await expect(page.locator('#moreMenuFeedbackBtn')).toBeVisible();
    await page.locator('#moreMenuFeedbackBtn').click();

    await expect(page.locator('#reportIssueModal')).toBeVisible();
    await expect(page.locator('#reportIssueModalTitle')).toHaveText('Send Feedback');
    // Forwarding via the real header button's own click handler also closes the menu.
    await expect(page.locator('#moreMenuDropdown')).toHaveClass(/hidden/);
  });
});

test.describe('header controls at desktop width', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('Help/History stay as their own buttons, and the more menu is not shown', async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator('#helpBtn')).toBeVisible();
    await expect(page.locator('#historyBtn')).toBeVisible();
    await expect(page.locator('#moreMenuBtn')).not.toBeVisible();
  });
});

// Regression coverage for the specific, deliberate coupling described in
// this file's header comment: the more-menu collapse and the NL/SQL panes'
// side-by-side -> stacked switch share the exact same breakpoint on
// purpose, per explicit request, rather than each having their own
// independently-chosen width.
test.describe('more-menu collapse happens at the same width the SQL box stacks under the NL box', () => {
  test('at 901px (one above the shared breakpoint) the panes sit side by side and the header icons stay uncollapsed', async ({ page }) => {
    await page.setViewportSize({ width: 901, height: 800 });
    await gotoApp(page);

    // No JS-toggled class marks the side-by-side/stacked switch - it's a
    // pure CSS media query on .editor-panes-row's flex-direction (see that
    // rule's own comment in style.css), so read the computed style directly
    // rather than asserting a class that doesn't exist.
    const flexDirection = await page.locator('#editorPanesRow').evaluate(
      (el) => getComputedStyle(el).flexDirection
    );
    expect(flexDirection).toBe('row');
    await expect(page.locator('#helpBtn')).toBeVisible();
    await expect(page.locator('#historyBtn')).toBeVisible();
    await expect(page.locator('#prefsBtn')).toBeVisible();
    await expect(page.locator('#modelTriggerBadge')).toBeVisible();
    await expect(page.locator('#moreMenuBtn')).not.toBeVisible();
  });

  test('at 900px (the shared breakpoint) the panes stack and the header icons - including the model badge - collapse into the more menu', async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 800 });
    await gotoApp(page);

    await expect(page.locator('#helpBtn')).not.toBeVisible();
    await expect(page.locator('#historyBtn')).not.toBeVisible();
    await expect(page.locator('#prefsBtn')).not.toBeVisible();
    // Folded in alongside the rest, per explicit follow-up request - see
    // model-selection.spec.js's own describe block for its dedicated
    // current-selection/submenu coverage.
    await expect(page.locator('#modelTriggerBadge')).not.toBeVisible();
    await expect(page.locator('#moreMenuBtn')).toBeVisible();

    // The dataset badge is the whole point of collapsing the rest - it
    // stays in the header at every width (it isn't folded into the menu
    // the way the other controls are).
    await expect(page.locator('#configTriggerBadge')).toBeVisible();
  });

  test('a mid-range width (700px) that used to leave the icons uncollapsed now folds them into the more menu too', async ({ page }) => {
    await page.setViewportSize({ width: 700, height: 800 });
    await gotoApp(page);

    // Feedback's own visibility is gated separately by issue_reporting_enabled
    // (see report-issue.spec.js) regardless of breakpoint, so it's left out
    // here - Help/History/Preferences are always-on and enough to prove the
    // collapse itself is now active at this width.
    await expect(page.locator('#helpBtn')).not.toBeVisible();
    await expect(page.locator('#historyBtn')).not.toBeVisible();
    await expect(page.locator('#prefsBtn')).not.toBeVisible();
    await expect(page.locator('#moreMenuBtn')).toBeVisible();
  });
});
