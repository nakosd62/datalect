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
 * for "not chartable"/"model chose a table").
 *
 * The server's own response field is "visualizations" - a {"<index>": {...},
 * ...} object keyed by 0-based statement_results index (see chart_helpers.py's
 * _clean_visualizations and summarize_routes.py's stream_summarize_result) -
 * not a single "visualization" value, since more than one Query Result can
 * now each get their own chart in the same turn. Every test in this file
 * only ever executes a single statement (see the `results` arrays passed to
 * mockExecute below - always exactly one entry), so this helper's own
 * `visualization` param still only ever needs to describe THAT one result,
 * at index "0" - it's wrapped into the real {"0": ...} shape here so every
 * call site below can keep passing the simpler singular shape. */
async function mockSummarizeResult(page, { summary, visualization, error } = {}) {
  await page.route('**/api/summarize-result', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    const body = error !== undefined
      ? { success: false, error }
      : { success: true, summary, visualizations: visualization ? { '0': visualization } : {} };
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

    // Axis labels are always shown (see buildResultsChartConfig()'s own
    // x/y title options); the legend is not, since there's only one
    // dataset here (no series_column, one y_column) - nothing for a legend
    // to distinguish.
    expect(config.options.scales.x.title).toEqual(expect.objectContaining({ display: true, text: 'day' }));
    expect(config.options.scales.y.title).toEqual(expect.objectContaining({ display: true, text: 'signups' }));
    expect(config.options.plugins.legend.display).toBe(false);
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

    // #resultsViewToggle (the toolbar row) stays visible here - it also
    // hosts the download button/menu, shown for every non-empty result
    // regardless of whether this one is chartable (see renderTableResult()'s
    // own comment on this in client.js). It's #resultsViewToggleButtons -
    // the actual Table/Chart pills - that stays hidden for a non-chartable
    // result, which is what "no toggle" here actually means.
    await expect(page.locator('#resultsViewToggle')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsViewToggleButtons')).toHaveClass(/hidden/);
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

// Axis labels and legend: every chart shows x/y axis titles named after the
// real columns being plotted (see buildResultsChartConfig()'s x/y `title`
// options), and a legend only when there's actually more than one dataset
// to distinguish - a single series/single y_column chart would just show a
// legend redundant with its own axis title, so it's suppressed there (see
// commonOptions.plugins.legend.display in client.js).
test.describe('chart axis labels and legend', () => {
  test('a scatter plot gets both axis titles, with no legend for a single series/y_column', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT weight, price FROM products;' });
    await mockExecute(page, {
      results: [{
        columns: ['weight', 'price'],
        rows: [{ weight: 1.2, price: 20 }, { weight: 2.4, price: 35 }, { weight: 0.8, price: 15 }],
        rowCount: 3,
      }],
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nHeavier products tend to cost more.',
      visualization: { chart_type: 'scatter', x_column: 'weight', y_columns: ['price'], series_column: null },
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how does weight relate to price');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Heavier products', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    const config = await page.evaluate(() => window.__lastChartConfig);
    expect(config.type).toBe('scatter');
    expect(config.options.scales.x.title).toEqual(expect.objectContaining({ display: true, text: 'weight' }));
    expect(config.options.scales.y.title).toEqual(expect.objectContaining({ display: true, text: 'price' }));
    expect(config.options.plugins.legend.display).toBe(false);
  });

  // signups peaks at 14, churned at 4 - under DUAL_Y_AXIS_RATIO (5x), so
  // this is deliberately the "share one axis" case; see the dedicated
  // "dual y-axes" describe block below for the >=5x split itself.
  test('multiple y_columns of a similar scale show a legend, and share one y-axis whose title names both', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, signups, churned FROM daily_stats;' });
    await mockExecute(page, {
      results: [{
        columns: ['day', 'signups', 'churned'],
        rows: [{ day: 'Mon', signups: 10, churned: 3 }, { day: 'Tue', signups: 14, churned: 4 }],
        rowCount: 2,
      }],
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nSignups outpaced churn both days.',
      visualization: { chart_type: 'line', x_column: 'day', y_columns: ['signups', 'churned'], series_column: null },
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('compare signups and churn by day');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('outpaced churn', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    const config = await page.evaluate(() => window.__lastChartConfig);
    expect(config.data.datasets).toHaveLength(2);
    expect(config.options.plugins.legend.display).toBe(true);
    expect(config.options.scales.x.title).toEqual(expect.objectContaining({ display: true, text: 'day' }));
    expect(config.options.scales.y.title).toEqual(expect.objectContaining({ display: true, text: 'signups / churned' }));
    expect(config.options.scales.y1).toBeUndefined();
    expect(config.data.datasets.every((d) => d.yAxisID === 'y')).toBe(true);
  });
});

// Dual y-axes: two y_columns whose real values differ wildly in scale (5x
// or more, peak-to-peak - see client.js's assignYAxisIds()) would otherwise
// squash the smaller one flat against zero on a single shared axis. See
// that function's own docstring for the exact rule and the "compare every
// column to the single largest peak" reasoning.
test.describe('chart dual y-axes for wildly different scales', () => {
  test('a >=5x scale difference splits onto a second, right-hand axis', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, revenue, orders FROM daily_sales;' });
    await mockExecute(page, {
      results: [{
        columns: ['day', 'revenue', 'orders'],
        // revenue peaks at 5000, orders at 40 - a 125x difference, well
        // past the 5x threshold.
        rows: [{ day: 'Mon', revenue: 3000, orders: 30 }, { day: 'Tue', revenue: 5000, orders: 40 }],
        rowCount: 2,
      }],
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nRevenue and orders both grew.',
      visualization: { chart_type: 'line', x_column: 'day', y_columns: ['revenue', 'orders'], series_column: null },
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('compare revenue and orders by day');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('both grew', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    const config = await page.evaluate(() => window.__lastChartConfig);
    // The big-magnitude column stays on the primary (left) axis...
    expect(config.options.scales.y.position).toBe('left');
    expect(config.options.scales.y.title).toEqual(expect.objectContaining({ display: true, text: 'revenue' }));
    // ...the small one gets its own right-hand axis, own title, and a grid
    // suppressed on the chart area (so it doesn't draw a second, misaligned
    // set of gridlines over the primary axis's own).
    expect(config.options.scales.y1).toBeTruthy();
    expect(config.options.scales.y1.position).toBe('right');
    expect(config.options.scales.y1.title).toEqual(expect.objectContaining({ display: true, text: 'orders' }));
    expect(config.options.scales.y1.grid.drawOnChartArea).toBe(false);

    const revenueDataset = config.data.datasets.find((d) => d.label === 'revenue');
    const ordersDataset = config.data.datasets.find((d) => d.label === 'orders');
    expect(revenueDataset.yAxisID).toBe('y');
    expect(ordersDataset.yAxisID).toBe('y1');
  });

  test('a scale difference just under 5x stays on one shared axis', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT day, revenue, orders FROM daily_sales;' });
    await mockExecute(page, {
      results: [{
        columns: ['day', 'revenue', 'orders'],
        // 196 / 40 = 4.9x - deliberately just under the 5x threshold.
        rows: [{ day: 'Mon', revenue: 150, orders: 30 }, { day: 'Tue', revenue: 196, orders: 40 }],
        rowCount: 2,
      }],
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nRevenue and orders both grew.',
      visualization: { chart_type: 'line', x_column: 'day', y_columns: ['revenue', 'orders'], series_column: null },
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('compare revenue and orders by day');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('both grew', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    const config = await page.evaluate(() => window.__lastChartConfig);
    expect(config.options.scales.y1).toBeUndefined();
    expect(config.options.scales.y.title).toEqual(expect.objectContaining({ display: true, text: 'revenue / orders' }));
    expect(config.data.datasets.every((d) => d.yAxisID === 'y')).toBe(true);
  });

  test('a scatter plot with two wildly different y_columns also splits its axes', async ({ page }) => {
    await mockTranslate(page, { sql: 'SELECT hour, revenue, orders FROM hourly_sales;' });
    await mockExecute(page, {
      results: [{
        columns: ['hour', 'revenue', 'orders'],
        rows: [{ hour: 1, revenue: 3000, orders: 30 }, { hour: 2, revenue: 5000, orders: 40 }],
        rowCount: 2,
      }],
    });
    await mockSummarizeResult(page, {
      summary: '*** NO SQL *** Results Summary\n\nBoth revenue and orders tracked together by hour.',
      visualization: { chart_type: 'scatter', x_column: 'hour', y_columns: ['revenue', 'orders'], series_column: null },
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('plot revenue and orders by hour');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('tracked together', { timeout: 10000 });
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();

    const config = await page.evaluate(() => window.__lastChartConfig);
    expect(config.type).toBe('scatter');
    expect(config.options.scales.y1).toBeTruthy();
    expect(config.options.scales.y1.title).toEqual(expect.objectContaining({ text: 'orders' }));
    const ordersDataset = config.data.datasets.find((d) => d.label === 'orders');
    expect(ordersDataset.yAxisID).toBe('y1');
  });
});

// Chart discoverability: the Summary tab takes over focus the instant a
// turn's results arrive (see prependSingleModeSummaryTab()), so a chart
// sitting on the other, now-inactive data tab is otherwise invisible unless
// the user happens to click around the tab strip. See client.js's
// buildResultsTabsNav() (the tab-strip badge) and renderSummaryInlineChart()/
// jumpToChartableResultTab() (the Summary tab's own small, clickable chart
// preview - rendered directly under the summary text, not a separate boxed
// callout elsewhere on the page).
test.describe('single-connection mode: chart discoverability (Summary inline chart preview)', () => {
  test('a chartable result gets a small chart preview at the end of the Summary text', async ({ page }) => {
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

    // Summary tab is active by default - the mini chart preview must
    // already be rendered right here, with no click needed to discover the
    // chart. It's a live Chart.js instance (the fake `Chart` from
    // fixtures.js's CHART.JS NETWORK ISOLATION setup), built from the exact
    // same visualization data the full-tab chart would use.
    const preview = page.locator('.summary-chart-inline-preview');
    await expect(preview).toBeVisible();
    await expect(page.locator('.summary-chart-inline-preview-canvas')).toHaveCount(1);
    expect(await page.evaluate(() => window.__lastChartConfig.type)).toBe('line');
    expect(await page.evaluate(() => window.__lastChartConfig.data.labels)).toEqual(['Mon', 'Tue', 'Wed']);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);
  });

  test('the compact preview drops axis titles that the full-tab chart shows', async ({ page }) => {
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

    // Summary tab's own compact preview, still on screen right now.
    const previewConfig = await page.evaluate(() => window.__lastChartConfig);
    expect(previewConfig.options.scales.x.title.display).toBe(false);
    expect(previewConfig.options.scales.y.title.display).toBe(false);
    expect(previewConfig.options.events).toEqual([]);

    // The full-tab chart for the very same data keeps its axis titles.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    const fullConfig = await page.evaluate(() => window.__lastChartConfig);
    expect(fullConfig.options.scales.x.title.display).toBe(true);
    expect(fullConfig.options.scales.y.title.display).toBe(true);
  });

  test('clicking the chart preview jumps to the chartable tab, already showing the chart', async ({ page }) => {
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
    // preview's own "force chart view" behavior (see
    // jumpToChartableResultTab()'s comment) is actually exercised rather
    // than coincidentally matching the model's own Chart-by-default choice.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(1).click();
    await page.locator('.results-view-toggle-btn[data-view="table"]').click();
    await expect(page.locator('#resultsTableWrapper')).not.toHaveClass(/hidden/);

    // Back to the Summary tab, then click its chart preview.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(0).click();
    await page.locator('.summary-chart-inline-preview').click();

    await expect(page.locator('#resultsTabsNav .result-tab-btn').nth(1)).toHaveClass(/active/);
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);
    await expect(page.locator('#resultsTableWrapper')).toHaveClass(/hidden/);
    await expect(page.locator('.results-view-toggle-btn[data-view="chart"]')).toHaveClass(/active/);
    expect(await page.evaluate(() => window.__chartInstanceCount)).toBe(1);
  });

  test('no inline chart preview at all when the result is not chartable', async ({ page }) => {
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

    await expect(page.locator('.summary-chart-inline-preview')).toHaveCount(0);
    await expect(page.locator('.summary-chart-inline-link')).toHaveCount(0);
  });

  test('a multi-statement script with two independently chartable results gets its own captioned preview for each, each jumping to its own tab', async ({ page }) => {
    // Regression coverage for the core multi-chart fix (see chart_helpers.
    // py's _pick_chartable_results): a two-statement script where BOTH
    // statements' results independently qualify for a chart must show TWO
    // previews on the Summary tab, not just one - and each preview must
    // jump to ITS OWN tab, not always the first (see
    // jumpToChartableResultTab()'s own `index` param).
    await mockTranslate(page, { sql: 'SELECT day, signups FROM daily_signups; SELECT region, revenue FROM regional_revenue;' });
    await mockExecute(page, {
      results: [
        { columns: ['day', 'signups'], rows: [{ day: 'Mon', signups: 10 }, { day: 'Tue', signups: 14 }, { day: 'Wed', signups: 9 }], rowCount: 3 },
        { columns: ['region', 'revenue'], rows: [{ region: 'east', revenue: 500 }, { region: 'west', revenue: 480 }], rowCount: 2 },
      ],
    });
    await page.route('**/api/summarize-result', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          summary: '*** NO SQL *** Signups trended upward, and revenue split evenly across regions.',
          visualizations: {
            '0': { chart_type: 'line', x_column: 'day', y_columns: ['signups'], series_column: null },
            '1': { chart_type: 'bar', x_column: 'region', y_columns: ['revenue'], series_column: null },
          },
        }),
      });
    });
    await gotoApp(page);

    await page.locator('#aiPrompt').fill('how did signups and revenue look this week');
    await page.locator('#aiPrompt').press('Enter');
    await expect(page.locator('.response-text')).toContainText('Signups trended upward', { timeout: 10000 });

    // Two previews on the Summary tab, each with its own distinguishing
    // caption (only shown at all once there's more than one preview).
    const previews = page.locator('.summary-chart-inline-preview');
    await expect(previews).toHaveCount(2);
    await expect(page.locator('.summary-chart-inline-preview-caption')).toHaveCount(2);
    await expect(previews.nth(0).locator('.summary-chart-inline-preview-caption')).toHaveText('Query 1');
    await expect(previews.nth(1).locator('.summary-chart-inline-preview-caption')).toHaveText('Query 2');

    // Clicking the SECOND preview jumps to the SECOND data tab (index 2:
    // Summary, Query 1, Query 2), not the first.
    await previews.nth(1).click();
    await expect(page.locator('#resultsTabsNav .result-tab-btn').nth(2)).toHaveClass(/active/);
    expect(await page.evaluate(() => window.__lastChartConfig.type)).toBe('bar');
    await expect(page.locator('#resultsChartWrapper')).not.toHaveClass(/hidden/);

    // Back to the Summary tab, then the FIRST preview jumps to the FIRST
    // data tab instead.
    await page.locator('#resultsTabsNav .result-tab-btn').nth(0).click();
    await previews.nth(0).click();
    await expect(page.locator('#resultsTabsNav .result-tab-btn').nth(1)).toHaveClass(/active/);
    expect(await page.evaluate(() => window.__lastChartConfig.type)).toBe('line');
  });
});
