// tests/e2e/schema-viewer.spec.js
//
// The read-only Schema Viewer modal (openSchemaViewer()/
// loadSchemaViewerConnection() in client.js, backed by GET /api/schema -
// config_routes.py's handle_get_schema()) - previously untested at the e2e
// layer entirely. These specs mock GET /api/schema directly at the network
// layer (its own real backend/get_schema() query behavior against every
// dialect is covered by the server-side backend test files instead - see
// e.g. test_postgres_backend.py's "get_schema() (deep)" section), and focus
// on what the CLIENT does with that response: the modal title format, and
// the facts-and-warnings block now shown at the top of the Overview tab
// (session info, schema size, the deep fetch's own best-effort "Estimated
// dataset size" line, and the two SCHEMA_MAX_TABLES/SCHEMA_MAX_SCHEMA_CHARS
// limit warnings) - all added/moved there in the same change that dropped
// the old separate "Session:" line shown under the modal title.

const { test, expect, gotoApp } = require('./fixtures');

/** Intercepts GET /api/schema with a canned successful response. `entries`
 * defaults to one minimal table entry carrying whatever global one-liners
 * (Session:/Estimated dataset size:) the caller wants parsed out of it -
 * see split_schema_text_into_entries()'s own doc comment in backends/
 * base.py for why those always end up folded into the LAST entry's text
 * in a real response, mirrored here by putting them in the only entry. */
async function mockSchema(page, {
  name = 'Orders DB',
  dialect = 'PostgreSQL',
  entries,
  schemaText = 'Table: orders\n  id integer NOT NULL\n  status text NOT NULL',
  truncated = false,
  hasOmittedTables = false,
  overview = null,
  cachedAt = null,
} = {}) {
  const finalEntries = entries || [
    { name: 'orders', heading: 'Table: orders', text: schemaText },
  ];
  await page.route('**/api/schema*', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        success: true,
        kind: 'preset',
        id: 'default',
        name,
        dialect,
        truncated,
        has_omitted_tables: hasOmittedTables,
        entries: finalEntries,
        cached_at: cachedAt,
        overview,
      }),
    });
  });
}

async function openSchemaViewer(page) {
  await page.locator('#datasetSchemaViewerBtn').click();
  await expect(page.locator('#schemaViewerModal')).not.toHaveClass(/hidden/);
}

test.describe('schema viewer', () => {
  test('title becomes "<name> in <dialect>" once the fetch resolves, and the old separate session-info header is gone', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, { name: 'Orders DB', dialect: 'PostgreSQL' });
    await openSchemaViewer(page);

    await expect(page.locator('#schemaViewerModalTitleText')).toHaveText('Orders DB in PostgreSQL');
    // The header used to also show a dedicated "#schemaViewerSessionInfo"
    // line under the title - removed in favor of the Overview tab's own
    // stats block (see the next test) - so it must not exist at all now.
    await expect(page.locator('#schemaViewerSessionInfo')).toHaveCount(0);
  });

  test('Overview tab shows the dataset size and schema character count on one row, and other connection settings below it', async ({ page }) => {
    await gotoApp(page);
    const schemaText = (
      'Table: orders\n  id integer NOT NULL\n  status text NOT NULL\n\n' +
      'Session: timezone=UTC; default collation=en_US.UTF-8\n\n' +
      // Just the one headline figure - format_dataset_size_line() (backends/
      // base.py) prefers a byte-size estimate over a row count whenever
      // both are available, deliberately leaving the row/table counts out
      // of the rendered text (see that function's own docstring).
      'Estimated dataset size: ~2.4 GB'
    );
    await mockSchema(page, {
      name: 'Orders DB',
      dialect: 'PostgreSQL',
      schemaText,
      overview: { prose: 'This database tracks customer orders.', questions: [], generated_at: '2026-01-01T00:00:00Z' },
    });
    await openSchemaViewer(page);

    // The dialog opens landed on the pinned "Overview" entry by default -
    // no extra click needed to select it.
    const stats = page.locator('.schema-viewer-overview-stats');
    await expect(stats).toBeVisible();
    const rows = stats.locator('.schema-viewer-overview-stats-row');
    // Row 1: dataset size + schema character count side by side (see
    // renderSchemaViewerStatsBlockHtml()'s -split row) - both figures live
    // in the SAME row here, not one each like the old stacked layout.
    await expect(rows.nth(0)).toContainText('Estimated dataset size: ~2.4 GB');
    await expect(rows.nth(0)).toContainText(`Schema size: ${schemaText.length.toLocaleString()} characters`);
    // Row 2: the old "Session:" one-liner, relabeled "Other settings:" and
    // moved below the size row.
    await expect(rows.nth(1)).toHaveText('Other settings: timezone=UTC; default collation=en_US.UTF-8');
    // The AI-written prose still renders below the stats block.
    await expect(page.locator('.schema-viewer-overview-prose')).toHaveText('This database tracks customer orders.');
  });

  test('Overview tab stats block still renders (minus the AI prose) even when no overview has been generated yet', async ({ page }) => {
    await gotoApp(page);
    const schemaText = 'Table: orders\n  id integer NOT NULL\n\nEstimated dataset size: ~42 rows';
    await mockSchema(page, { schemaText, overview: null });
    await openSchemaViewer(page);

    const stats = page.locator('.schema-viewer-overview-stats');
    await expect(stats).toBeVisible();
    await expect(stats).toContainText('Estimated dataset size: ~42 rows');
    await expect(page.locator('.schema-viewer-overview-empty')).toContainText('No overview has been generated yet');
  });

  test('surfaces a warning when this connection exceeds SCHEMA_MAX_SCHEMA_CHARS or SCHEMA_MAX_TABLES', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, { truncated: true, hasOmittedTables: true });
    await openSchemaViewer(page);

    const warnings = page.locator('.schema-viewer-overview-warning');
    await expect(warnings).toHaveCount(2);
    await expect(warnings.nth(0)).toContainText('SCHEMA_MAX_SCHEMA_CHARS');
    await expect(warnings.nth(1)).toContainText('SCHEMA_MAX_TABLES');
  });

  test('a dialect with no cheap dataset-size estimate (e.g. MongoDB Atlas SQL, Google Sheets) omits that row rather than showing a misleading zero', async ({ page }) => {
    await gotoApp(page);
    // No "Estimated dataset size:" line at all in the schema text - see
    // backends/mongodb_sql.py's/backends/sheets.py's own comments on why
    // neither backend ever emits one.
    const schemaText = 'Table: orders\n  id integer NOT NULL\n\nSession: timezone=UTC';
    await mockSchema(page, { dialect: 'MongoDB Atlas SQL', schemaText, truncated: false, hasOmittedTables: false });
    await openSchemaViewer(page);

    const stats = page.locator('.schema-viewer-overview-stats');
    await expect(stats).toBeVisible();
    await expect(stats).toContainText('timezone=UTC');
    await expect(stats).not.toContainText('Estimated dataset size');
    await expect(page.locator('.schema-viewer-overview-warning')).toHaveCount(0);
  });

  test('the ER diagram section is omitted entirely when no relationships were detected, rather than shown empty', async ({ page }) => {
    await gotoApp(page);
    // Two independent tables, no "Constraints:" FK section and no "Likely
    // relationships" naming-convention section at all - buildSchemaErDiagram()
    // has nothing to draw.
    await mockSchema(page, {
      entries: [
        { name: 'orders', heading: 'Table: orders', text: 'Table: orders\n  id integer NOT NULL' },
        { name: 'products', heading: 'Table: products', text: 'Table: products\n  id integer NOT NULL' },
      ],
      overview: { prose: 'Two unrelated tables.', questions: [], generated_at: '2026-01-01T00:00:00Z' },
    });
    await openSchemaViewer(page);

    await expect(page.locator('.schema-viewer-overview-prose')).toBeVisible();
    await expect(page.locator('.schema-viewer-overview-diagram-wrap')).toHaveCount(0);
  });

  test('the ER diagram section renders when a real foreign key relationship exists', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n  customer_id integer NOT NULL\n\n' +
          'Constraints:\n  [orders] fk_orders_customer (FOREIGN KEY): customer_id -> customers(id)'
        ) },
        { name: 'customers', heading: 'Table: customers', text: 'Table: customers\n  id integer NOT NULL' },
      ],
      overview: { prose: 'Orders reference customers.', questions: [], generated_at: '2026-01-01T00:00:00Z' },
    });
    await openSchemaViewer(page);

    await expect(page.locator('.schema-viewer-overview-prose')).toBeVisible();
    await expect(page.locator('.schema-viewer-overview-diagram-wrap')).toHaveCount(1);
  });

  test('a multi-line view definition renders in full, not truncated to its own first line', async ({ page }) => {
    await gotoApp(page);
    // Mirrors what a real dialect actually sends: a view's SELECT text is
    // normally several lines (e.g. Postgres's pg_get_viewdef pretty-
    // printing it, reindented server-side by backends/base.py's
    // format_multiline_schema_entry_body() so every continuation line
    // stays indented) - a regression test for the bug where the client
    // only ever kept a definition's own first line ("SELECT o.id," here)
    // and silently dropped the rest.
    await mockSchema(page, {
      entries: [
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n\n' +
          'Views:\n  View order_totals\n\n' +
          'View definitions:\n  View order_totals: SELECT o.id,\n    SUM(i.amount) AS total\n    FROM orders o\n    JOIN items i ON i.order_id = o.id\n    GROUP BY o.id;'
        ) },
      ],
    });
    await openSchemaViewer(page);

    await page.locator('.schema-viewer-group-header', { hasText: 'Views' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'order_totals' }).click();
    const detailText = page.locator('#schemaViewerDetailText');
    await expect(detailText).toContainText('SELECT o.id,');
    await expect(detailText).toContainText('SUM(i.amount) AS total');
    await expect(detailText).toContainText('GROUP BY o.id;');
  });

  test('a view with no readable definition shows a dialect-specific reason naming the actual missing privilege', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      dialect: 'MySQL',
      entries: [
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n\n' +
          'Views:\n  View order_totals'
          // No "View definitions:" section at all - same "came back empty"
          // outcome as an explicit NULL, from the client's point of view.
        ) },
      ],
    });
    await openSchemaViewer(page);

    await page.locator('.schema-viewer-group-header', { hasText: 'Views' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'order_totals' }).click();
    await expect(page.locator('#schemaViewerDetailText')).toContainText('SHOW VIEW privilege');
  });

  test('large per-table row counts are shortened to K/M/B with 3-digit accuracy, in both the tree and the detail heading', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'events', heading: 'Table: events', text: (
          'Table: events\n  id integer NOT NULL\n\n' +
          'Live row counts:\n  events: 8452123 rows (live, authoritative)'
        ) },
      ],
    });
    await openSchemaViewer(page);

    // Tables tree row (expand the Tables group first - it starts collapsed).
    await page.locator('.schema-viewer-group-header', { hasText: 'Tables' }).click();
    const treeLabel = page.locator('.schema-viewer-entry-item', { hasText: 'events' });
    await expect(treeLabel).toContainText('8.45M rows');

    // The same table's detail-pane heading, once selected.
    await treeLabel.click();
    await expect(page.locator('#schemaViewerDetailHeading')).toContainText('8.45M rows');
  });

  test('refresh button keeps full opacity and a legible muted color while showing "Refreshing...", instead of being dimmed into illegibility', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page);
    await openSchemaViewer(page);

    let resolveRefresh;
    const refreshPromise = new Promise((resolve) => { resolveRefresh = resolve; });
    await page.route('**/api/config/refresh-schema', async (route) => {
      await refreshPromise;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ success: true }) });
    });

    const refreshBtn = page.locator('#schemaViewerRefreshBtn');
    await refreshBtn.click();
    await expect(page.locator('#schemaViewerRefreshBtnLabel')).toHaveText('Refreshing...');
    await expect(refreshBtn).toBeDisabled();
    await expect(refreshBtn).toHaveCSS('opacity', '1');

    resolveRefresh();
    await expect(refreshBtn).toBeEnabled();
  });
});
