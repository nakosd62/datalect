// tests/e2e/chart-toggle.spec.js
//
// Single-connection mode's Table/Chart toggle - see client.js's
// renderTableResult() (the toggle-button/canvas-visibility logic, near the
// bottom of its tabular-success branch), renderResultChart()/
// buildResultsChartConfig() (the Chart.js config builder), and
// setActiveResultChartView() (the toggle click handler + its chatStore
// persistence). Server-side: translate_routes.py's
// _SINGLE_SUMMARY_SYSTEM_INSTRUCTION/_clean_visualization, already covered
// by tests/server/test_translate_routes.py - this file only exercises what
// the CLIENT does with an already-validated `visualization` object riding
// along on /api/summarize-result's response.
//
// Chart.js itself is never the real library here - fixtures.js's shared
// `test` fixture (see its own CHART.JS NETWORK ISOLATION comment) replaces
// the real cdn.jsdelivr.net script with a tiny instrumented fake `Chart`
// constructor that records whatever config it was asked to draw onto
// `window.__lastChartConfig`, and how many chart instances are currently
// "mounted" onto `window.__chartInstanceCount` - real enough for client.js's
// own construct/destroy calls to succeed, without a real <canvas> render or
// any real network access.

const { test, expect, gotoApp, mockTranslate, mockExecute } = require('./fixtures');

/** Intercept POST /api/summarize-result - same shape as translate-execute.
 * spec.js's own local helper of the same name, extended with an optional
 * `visualization` field (translate_routes.py's own already-validated
 * {chart_type, x_column, y_columns, series_column} object, or omitted/null
 * for "not chartable"/"model chose a table"). */
async function mockSummarizeResult(page, { summary, visualization, error } = {}) {
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = error !== undefined
      ? { success: false, error }
      : { success: true, summary, visualization: visualization !== undefined ? visualization : null };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

const CHARTABLE_RESULTS = [{
  columns: ['day', 'signups'],
  rows: [{ day: 'Mon', signups: 10 }, { day: 'Tue', signups: 14 }, { day: 'Wed', signups: 9 }],
  rowCount: 3,
}];

const CHARTABLE_VISUALIZATION = { chart_type: 'line', x_column: 'day', y_columns: ['signups'], series_column: null };

test.describe('single-connection mode: Table/Chart toggle', () => {
  test('a chartable result renders as a chart by default, with the toggle showing "Chart" active', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });

    // Summary tab (active) + the one data tab - switch to the data tab,
    // which is the only one that ever shows the toggle/chart at all.
    const tabs = page.locator('#resultsTabsNav .result-tab-btn');
    await expect(tabs).toHaveCount(2);
    await tabs.nth(1).click();

    await expect(page.locator('#resultsViewToggle')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsTableWrapper')).toHaveClass(/hidden/);
    await expect(page.locator('.results-view-toggle-btn[data-view="chart"]')).toHaveClass(/active/);
    await expect(page.locator('.results-view-toggle-btn[data-view="table"]')).not.toHaveClass(/active/);

    const config = await page.evaluate(() => window.__lastChartConfig);
    expect(config.type).toBe('line');
    expect(config.data.labels).toEqual(['Mon', 'Tue', 'Wed']);
    expect(config.data.datasets).toHaveLength(1);
    expect(config.data.datasets[0].data).toEqual([10, 14, 9]);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);
  });

  test('clicking Table switches to the table view (and destroys the chart); clicking Chart switches back', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);

    await page.locator('.results-view-toggle-btn[data-view="table"]').click();
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsChartWrapper')).toHaveClass(/hidden/);
    await expect(page.locator('.results-view-toggle-btn[data-view="table"]')).toHaveClass(/active/);
    // The real rows are still there underneath - switching views never
    // loses/re-fetches the data itself.
    await expect(page.locator('#resultsBody tr')).toHaveCount(3);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(0);

    await page.locator('.results-view-toggle-btn[data-view="chart"]').click();
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsTableWrapper')).toHaveClass(/hidden/);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);
  });

  test('no toggle and no chart at all when the result is not chartable', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    // Below _CHART_MIN_ROWS server-side, so the real server would always
    // send `visualization: null` here regardless of what the model said -
    // mirrored directly in the mock rather than re-deriving that gate
    // client-side (client.js trusts the server's decision at face value,
    // same as every other server response - see requestSingleModeResultsSummary's
    // own docstring).
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThere are 42 signups.',
      visualization: null,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('42 signups', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    await expect(page.locator('#resultsViewToggle')).toHaveClass(/hidden/);
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsChartWrapper')).toHaveClass(/hidden/);
    expect(await page.evaluate(() => window.__lastChartConfig)).toBeNull();
  });

  // Regression guard for this feature's own persistence requirement: the
  // user's manual override must survive stepping away to a different turn
  // and back (chatStore's undo()/redo()) without silently reverting to the
  // model's own default - see setActiveResultChartView()'s own comment on
  // why the live tab and the persisted chatStore turn are different
  // objects that both need updating.
  test('a manual Table/Chart choice survives navigating to another turn and back', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);
    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });

    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    await page.locator('.results-view-toggle-btn[data-view="table"]').click();
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);

    // A second, unrelated turn - somewhere to navigate back FROM.
    await mockTranslate(page, { sql: 'SELECT 2 AS n;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 2 }], rowCount: 1 }] });
    await mockSummarizeResult(page, { summary: '*** NO SQL *** Results Summary\n\nHere is two.', visualization: null });
    await page.locator('#aiPrompt').fill('give me two');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Here is two', { timeout: 10000 });

    await page.locator('#goBackBtn').click();
    await expect(page.locator('.response-text')).toContainText('Signups trended upward');
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    // Still Table, not reverted to the model's own Chart default.
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsChartWrapper')).toHaveClass(/hidden/);
    await expect(page.locator('.results-view-toggle-btn[data-view="table"]')).toHaveClass(/active/);
  });

  // Regression guard: clearResultsDisplay() (called at the very start of
  // translatePrompt(), before a new prompt's own /api/translate call even
  // goes out - see that function's own docstring) used to hand-clear only
  // resultsHeader/resultsBody/the tabs strip, bypassing renderTableResult()'s
  // own toggle/chart reset entirely (see that function's own comment on
  // why). A chart left showing from the PREVIOUS turn kept right on
  // rendering - stale data, on top of an otherwise "cleared" results area -
  // until whatever the new turn eventually produced replaced it. Proven
  // here by inspecting the moment BETWEEN submitting the second prompt and
  // its response arriving (the translate call is deliberately delayed),
  // which is exactly the window that used to leak the old chart.
  test('submitting a new prompt immediately clears a chart left over from the previous turn, before the new turn\'s own results arrive', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);
    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);

    // A second, unrelated prompt - deliberately held open so the moment
    // right after submission (before ANY new data is back) is inspectable.
    let resolveTranslate;
    const translateStarted = new Promise((resolve) => { resolveTranslate = resolve; });
    await page.route('**/api/translate', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      resolveTranslate();
      await new Promise((r) => setTimeout(r, 1000));
      await route.fulfill({
        status: 200, contentType: 'application/x-ndjson',
        body: JSON.stringify({ status: 'done', success: true, sql: 'SELECT 2 AS n;' }) + '\n',
      });
    });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 2 }], rowCount: 1 }] });
    await mockSummarizeResult(page, { summary: '*** NO SQL *** Results Summary\n\nHere is two.', visualization: null });

    await page.locator('#aiPrompt').fill('give me two');
    await page.locator('#aiPrompt').press('Enter');
    await translateStarted;

    // Right now: the old turn's results were just wiped, the new turn's
    // haven't arrived yet (still mid-delay - /api/translate itself won't
    // resolve for another ~1s) - no stale chart should be visible, and its
    // Chart.js instance must already be destroyed, not just hidden behind
    // the (also reset) table wrapper. Deliberately ONE-SHOT reads
    // (page.evaluate(), not expect(locator).toHaveClass()'s own auto-
    // retrying poll) - an auto-retrying assertion here would happily wait
    // out the full ~1s delay and pass once the second turn's OWN real
    // results finish rendering (which independently resets these same
    // classes via renderTableResult()'s own top-of-function reset - see
    // that function's identical comment), silently missing the bug this
    // test exists to catch: a snapshot taken RIGHT NOW, before that
    // happens, is the only way to prove clearResultsDisplay() itself -
    // not just eventual re-rendering - is what did the resetting.
    const midFlightState = await page.evaluate(() => ({
      chartWrapperHidden: document.getElementById('resultsChartWrapper').classList.contains('hidden'),
      viewToggleHidden: document.getElementById('resultsViewToggle').classList.contains('hidden'),
      tableWrapperHidden: document.getElementById('resultsTableWrapper').classList.contains('hidden'),
      chartInstanceCount: window.__chartInstanceCount,
    }));
    expect(midFlightState).toEqual({
      chartWrapperHidden: true,
      viewToggleHidden: true,
      tableWrapperHidden: false,
      chartInstanceCount: 0,
    });

    // Let the turn actually finish, so the test doesn't leave an in-flight
    // request hanging past its own end.
    await expect(page.locator('.response-text')).toContainText('Here is two', { timeout: 10000 });
  });
});

// Chart discoverability: the Summary tab takes over focus the instant a
// turn's results arrive (see prependSingleModeSummaryTab()), so a chart
// sitting on the other, now-inactive data tab is otherwise invisible unless
// the user happens to click around the tab strip. See client.js's
// buildResultsTabsNav() (the tab-strip badge) and summaryChartCalloutHtml()/
// jumpToChartableResultTab() (the Summary tab's own callout).
test.describe('single-connection mode: chart discoverability (tab badge + Summary callout)', () => {
  test('a chartable result gets a badge on its own tab, and a "View as chart" callout under the Summary text', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });

    // Summary tab is active by default - the callout must already be
    // visible right here, with no click needed to discover the chart.
    const callout = page.locator('.summary-chart-callout-btn');
    await expect(callout).toBeVisible();
    await expect(callout).toContainText('View as chart');

    // The data tab (index 1) carries the badge - both the CSS hook and the
    // literal emoji in its own label text (see buildResultsTabsNav()'s
    // comment on why the badge isn't color-only).
    const dataTab = page.locator('#resultsTabsNav .result-tab-btn').nth(1);
    await expect(dataTab).toHaveClass(/result-tab-btn--chartable/);
    await expect(dataTab).toContainText('📊');
  });

  test('clicking "View as chart" jumps to the chartable tab, already showing the chart', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups;' });
    await mockExecute(page, { results: CHARTABLE_RESULTS });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups trended upward this week.',
      visualization: CHARTABLE_VISUALIZATION,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how did signups trend this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });

    // Manually flip the (as-yet-unvisited) data tab to Table first, so the
    // callout's own "force chart view" behavior (see
    // jumpToChartableResultTab()'s comment) is actually exercised rather
    // than coincidentally matching the model's own Chart-by-default choice.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    await page.locator('.results-view-toggle-btn[data-view="table"]').click();
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);

    // Back to the Summary tab, then click the callout.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(0).click();
    await page.locator('.summary-chart-callout-btn').click();

    await expect(page.locator('#resultsTabsNav .result-tab-btn').nth(1)).toHaveClass(/active/);
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsTableWrapper')).toHaveClass(/hidden/);
    await expect(page.locator('.results-view-toggle-btn[data-view="chart"]')).toHaveClass(/active/);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);
  });

  test('no badge and no callout at all when the result is not chartable', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT COUNT(*) AS n FROM signups;' });
    await mockExecute(page, { results: [{ columns: ['n'], rows: [{ n: 42 }], rowCount: 1 }] });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nThere are 42 signups.',
      visualization: null,
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how many signups');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('42 signups', { timeout: 10000 });

    await expect(page.locator('.summary-chart-callout-btn')).toHaveCount(0);
    const dataTab = page.locator('#resultsTabsNav .result-tab-btn').nth(1);
    await expect(dataTab).not.toHaveClass(/result-tab-btn--chartable/);
    await expect(dataTab).not.toContainText('📊');
  });
});
