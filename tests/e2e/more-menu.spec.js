// tests/e2e/more-menu.spec.js
//
// Under a very narrow (mobile-portrait-width) viewport, the header's
// Help/History/Feedback/sign-in controls collapse into a single triple-dot
// "more" menu (see the @media (max-width: 480px) block in style.css and the
// MORE MENU section in client.js) so the connection/model status badges have
// room to render without being squashed. This only tests the collapse
// itself and that the menu's items forward to the real, existing
// Help/History/Feedback behavior - the individual modals' own contents are
// already covered by app-shell.spec.js (Help/History) and
// report-issue.spec.js (the feedback modal).

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

  test('collapses Help/History/sign-in into the more menu, hiding the individual controls', async ({ page }) => {
    await gotoApp(page);

    await expect(page.locator('#moreMenuBtn')).toBeVisible();
    await expect(page.locator('#helpBtn')).not.toBeVisible();
    await expect(page.locator('#historyBtn')).not.toBeVisible();

    // The whole point of the collapse - the status badges have to actually
    // fit, not just technically be present.
    await expect(page.locator('#configTriggerBadge')).toBeVisible();
    await expect(page.locator('#modelTriggerBadge')).toBeVisible();
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
