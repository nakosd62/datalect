// tests/e2e/schema-viewer.spec.js
//
// The read-only Schema Viewer modal (openSchemaViewer()/
// loadSchemaViewerConnection() in client.js, backed by GET /api/schema -
// config_routes.py's handle_get_schema()) - previously untested at the e2e
// layer entirely. These specs mock GET /api/schema directly at the network
// layer (its own real backend/get_schema() query behavior against every
// dialect is covered by the server-side backend test files instead - see
// e.g. test_postgres_backend.py's "get_schema() (deep)" section), and focus
// on what the CLIENT does with that response: the modal title format, the
// short "Data Size: ~...;   Schema Size: ... tokens" facts line under it
// (built from schema size, converted to an approximate token count at 4
// characters per token, and the deep fetch's own best-effort "Estimated
// dataset size" line - the connection's separate "Session: ..." info is
// parsed the same way but deliberately never shown anywhere, per an
// explicit request to keep this line short), the draggable LHS/RHS
// divider, and
// the warnings block (SCHEMA_MAX_TABLES/SCHEMA_MAX_SCHEMA_CHARS) still
// shown at the top of the Overview tab.

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
    // line under the title - that specific element is gone (replaced by
    // #schemaViewerFactsLine, covered by the tests below), so it must not
    // exist at all.
    await expect(page.locator('#schemaViewerSessionInfo')).toHaveCount(0);
    // The facts line (schema-token-count only here, since this mock's
    // default schema text has no Estimated dataset size line) lives
    // under the title, inside the same title block.
    const factsLine = page.locator('.schema-viewer-title-block #schemaViewerFactsLine');
    await expect(factsLine).toBeVisible();
    await expect(factsLine).toContainText('Schema Size:');
  });

  test('the facts line under the title is shortened to just "Data Size: ...;   Schema Size: ... tokens" - session info is parsed but not shown', async ({ page }) => {
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

    // Moved out of the Overview tab's own content and into a persistent
    // line under the modal title (see index.html's own comment on
    // #schemaViewerFactsLine) - the server's own "Estimated dataset
    // size:" label is relabeled "Data Size:" at render time, joined with
    // "Schema Size: ... tokens" via a semicolon followed by three
    // non-breaking spaces (SCHEMA_VIEWER_FACTS_SEPARATOR in client.js);
    // the token figure is the raw schema character count divided by 4
    // (SCHEMA_VIEWER_CHARS_PER_TOKEN in client.js), then quantized UP to
    // the nearest 100 (SCHEMA_VIEWER_TOKEN_QUANTUM in client.js) rather
    // than rounded to the nearest integer, since this is only ever a
    // rough cost estimate - the server-side schema_text (also fed to the
    // LLM) keeps its own original wording/length untouched, this is
    // purely a display-time conversion. The "Session: ..." line is deliberately
    // left out of this shortened line entirely - it's still parsed (see
    // parseSchemaSessionInfo()), just not displayed anywhere - so neither
    // its raw text nor its old "Other settings:" label should appear.
    const factsLine = page.locator('#schemaViewerFactsLine');
    await expect(factsLine).toBeVisible();
    const expectedTokenCount = 100 * Math.ceil((schemaText.length / 4) / 100);
    await expect(factsLine).toHaveText(`Data Size: ~2.4 GB;   Schema Size: ${expectedTokenCount.toLocaleString()} tokens`);
    await expect(factsLine).not.toContainText('Other settings');
    await expect(factsLine).not.toContainText('Session:');
    await expect(factsLine).not.toContainText('timezone=UTC');
    // No longer inside the Overview-tab stats box at all - that box is
    // warnings-only now (see the "no cheap dataset-size estimate" test
    // below and renderSchemaViewerStatsBlockHtml()'s own comment).
    await expect(page.locator('.schema-viewer-overview-stats')).toHaveCount(0);
    // The AI-written prose still renders in the Overview tab.
    await expect(page.locator('.schema-viewer-overview-prose')).toHaveText('This database tracks customer orders.');
  });

  test('the LHS/RHS divider can be dragged to resize the two panes, and Home resets it back to the default split', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page);
    await page.setViewportSize({ width: 1600, height: 1000 });
    await openSchemaViewer(page);

    const resizer = page.locator('#schemaViewerPanesResizer');
    const listPane = page.locator('#schemaViewerListPane');
    const wrap = page.locator('#schemaViewerPanesWrap');

    // Starts at the default 20% split (see .schema-viewer-list-pane's own
    // CSS comment) - no inline style override applied yet.
    const wrapBoxBefore = await wrap.boundingBox();
    const listBoxBefore = await listPane.boundingBox();
    expect(Math.round((listBoxBefore.width / wrapBoxBefore.width) * 100)).toBe(20);

    // Drag the resizer 200px to the right - a real mouse down/move/up
    // sequence, not a synthetic DOM event, so this also exercises
    // initSchemaViewerPanesResizer()'s actual mousedown/mousemove/mouseup
    // wiring end to end.
    const resizerBox = await resizer.boundingBox();
    const startX = resizerBox.x + resizerBox.width / 2;
    const startY = resizerBox.y + resizerBox.height / 2;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(startX + 200, startY, { steps: 10 });
    await page.mouse.up();

    const listBoxAfter = await listPane.boundingBox();
    expect(listBoxAfter.width).toBeGreaterThan(listBoxBefore.width + 150);

    // Home (keyboard equivalent, for anyone not using a mouse - the
    // resizer is a focusable role="separator") clears the inline override
    // and snaps back to the default 20% split.
    await resizer.focus();
    await page.keyboard.press('Home');
    const wrapBoxReset = await wrap.boundingBox();
    const listBoxReset = await listPane.boundingBox();
    expect(Math.round((listBoxReset.width / wrapBoxReset.width) * 100)).toBe(20);
  });

  test('the facts line renders even when no overview has been generated yet, and survives selecting a different tree entry', async ({ page }) => {
    await gotoApp(page);
    const schemaText = 'Table: orders\n  id integer NOT NULL\n\nEstimated dataset size: ~42 rows';
    await mockSchema(page, { schemaText, overview: null });
    await openSchemaViewer(page);

    const factsLine = page.locator('#schemaViewerFactsLine');
    await expect(factsLine).toBeVisible();
    await expect(factsLine).toContainText('Data Size: ~42 rows');
    await expect(page.locator('.schema-viewer-overview-empty')).toContainText('No overview has been generated yet');

    // The whole point of moving this under the title rather than leaving
    // it inside the Overview tab's own content: it stays visible no
    // matter which tree entry is currently selected, not just while
    // Overview itself is. Tables group starts collapsed - expand it first.
    await page.locator('.schema-viewer-group-header', { hasText: 'Tables' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'orders' }).click();
    await expect(factsLine).toBeVisible();
    await expect(factsLine).toContainText('Data Size: ~42 rows');
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

  test('a dialect with no cheap dataset-size estimate (e.g. MongoDB Atlas SQL, Google Sheets) omits that part rather than showing a misleading zero, and never shows session info at all', async ({ page }) => {
    await gotoApp(page);
    // No "Estimated dataset size:" line at all in the schema text - see
    // backends/mongodb_sql.py's/backends/sheets.py's own comments on why
    // neither backend ever emits one. The "Session: ..." line IS present
    // here, to prove it's parsed without error but still never rendered.
    const schemaText = 'Table: orders\n  id integer NOT NULL\n\nSession: timezone=UTC';
    await mockSchema(page, { dialect: 'MongoDB Atlas SQL', schemaText, truncated: false, hasOmittedTables: false });
    await openSchemaViewer(page);

    const factsLine = page.locator('#schemaViewerFactsLine');
    await expect(factsLine).toBeVisible();
    const expectedTokenCount = 100 * Math.ceil((schemaText.length / 4) / 100);
    await expect(factsLine).toHaveText(`Schema Size: ${expectedTokenCount.toLocaleString()} tokens`);
    await expect(factsLine).not.toContainText('timezone=UTC');
    await expect(factsLine).not.toContainText('Data Size:');
    // No warnings, and no dataset-size figure at all - the stats box
    // (warnings-only now) has nothing to show and isn't rendered.
    await expect(page.locator('.schema-viewer-overview-warning')).toHaveCount(0);
    await expect(page.locator('.schema-viewer-overview-stats')).toHaveCount(0);
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

  test('the ER diagram renders at the bottom of the Overview tab, after the suggested questions', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n  customer_id integer NOT NULL\n\n' +
          'Constraints:\n  [orders] fk_orders_customer (FOREIGN KEY): customer_id -> customers(id)'
        ) },
        { name: 'customers', heading: 'Table: customers', text: 'Table: customers\n  id integer NOT NULL' },
      ],
      overview: {
        prose: 'Orders reference customers.',
        questions: ['How many orders per customer?'],
        generated_at: '2026-01-01T00:00:00Z',
      },
    });
    await openSchemaViewer(page);

    // Prose, then the suggested-questions block, then the diagram last -
    // the diagram is the most visually heavy element here, so it goes at
    // the very bottom rather than between the prose and the questions.
    const children = await page.locator('#schemaViewerOverviewWrap > *').evaluateAll(
      (els) => els.map((el) => el.className),
    );
    const proseIndex = children.findIndex((c) => c.includes('schema-viewer-overview-prose'));
    const questionsIndex = children.findIndex((c) => c.includes('schema-viewer-overview-questions-block'));
    const diagramIndex = children.findIndex((c) => c.includes('schema-viewer-overview-diagram-wrap'));
    expect(proseIndex).toBeGreaterThanOrEqual(0);
    expect(questionsIndex).toBeGreaterThan(proseIndex);
    expect(diagramIndex).toBeGreaterThan(questionsIndex);
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

  test('the "Grants:" global section gets its own tree group, instead of being left at the bottom of the last table', async ({ page }) => {
    // Regression test for a real bug: Grants used to be an entirely
    // unrecognized global section (unlike Constraints/Indexes/Views/
    // Routines, which all already have their own promoted treatment) -
    // it just rode along in whichever table entry's own raw-text
    // "remainder" happened to be shown last, reading as if it belonged to
    // that one table rather than describing the whole connection.
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'customers', heading: 'Table: customers', text: 'Table: customers\n  id integer NOT NULL' },
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n\n' +
          'Grants:\n' +
          '  Grant SELECT on orders to app_readonly\n' +
          '  Grant INSERT on orders to app_writer'
        ) },
      ],
    });
    await openSchemaViewer(page);

    // Not left behind in the "orders" table's own raw-text pane any more.
    await page.locator('.schema-viewer-group-header', { hasText: 'Tables' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'orders' }).click();
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('Grant SELECT');
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('Grants:');

    // Its own separate, collapsible "Grants (2)" group instead.
    const grantsHeader = page.locator('.schema-viewer-group-header', { hasText: 'Grants' });
    await expect(grantsHeader).toContainText('Grants (2)');
    await grantsHeader.click();
    const grantItems = page.locator('.schema-viewer-entry-item', { hasText: 'orders:' });
    await expect(grantItems).toHaveCount(2);
    await expect(grantItems.nth(0)).toContainText('orders: SELECT → app_readonly');
    await expect(grantItems.nth(1)).toContainText('orders: INSERT → app_writer');

    await grantItems.nth(0).click();
    await expect(page.locator('#schemaViewerDetailHeading')).toHaveText('orders → app_readonly');
    await expect(page.locator('#schemaViewerDetailText')).toHaveText('Grant SELECT on orders to app_readonly');
  });

  test('Snowflake\'s differently-shaped "Grants (current role):" lines parse into the same Grants group', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      dialect: 'Snowflake',
      entries: [
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n\n' +
          'Grants (current role):\n' +
          '  orders: SELECT, INSERT (role ACCOUNTADMIN)'
        ) },
      ],
    });
    await openSchemaViewer(page);

    const grantsHeader = page.locator('.schema-viewer-group-header', { hasText: 'Grants' });
    await expect(grantsHeader).toContainText('Grants (1)');
    await grantsHeader.click();
    await expect(page.locator('.schema-viewer-entry-item[data-category="grants"]')).toContainText('orders: SELECT, INSERT → role ACCOUNTADMIN');
  });

  test('a dialect with no Grants section at all (e.g. BigQuery) leaves the Grants group out of the tree entirely', async ({ page }) => {
    await gotoApp(page);
    await mockSchema(page, {
      dialect: 'BigQuery',
      entries: [
        { name: 'orders', heading: 'Table: orders', text: 'Table: orders\n  id integer NOT NULL' },
      ],
    });
    await openSchemaViewer(page);
    await expect(page.locator('.schema-viewer-group-header', { hasText: 'Grants' })).toHaveCount(0);
  });

  test('the "Comments:" global section is folded into the table/column display, instead of being left at the bottom of the last table', async ({ page }) => {
    // Regression test for a real bug (Oracle/etc. schemas): table and
    // column catalog comments used to be an entirely unrecognized global
    // section - they just rode along in whichever table entry's own
    // raw-text "remainder" happened to be shown last, reading as if they
    // belonged to that one table rather than describing the whole
    // connection.
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'customers', heading: 'Table: customers', text: 'Table: customers\n  id integer NOT NULL' },
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n  status text NOT NULL\n\n' +
          'Comments:\n' +
          '  [table] orders: Sales orders placed by customers.\n' +
          '  [column] orders.status: One of pending/shipped/cancelled.'
        ) },
      ],
    });
    await openSchemaViewer(page);

    await page.locator('.schema-viewer-group-header', { hasText: 'Tables' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'orders' }).click();

    // Not left behind in the "orders" table's own raw-text pane any more.
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('Comments:');
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('One of pending/shipped/cancelled');

    // The table-level comment shows as its own note under the heading...
    await expect(page.locator('#schemaViewerTableCommentText')).toHaveText('Sales orders placed by customers.');

    // ...and the column-level comment shows in that column's own row of
    // the structured columns table, rather than as raw text.
    const statusRow = page.locator('.schema-viewer-columns-table tbody tr', { hasText: 'status' });
    await expect(statusRow).toContainText('One of pending/shipped/cancelled.');

    // A table with no comment of its own shows no note at all.
    await page.locator('.schema-viewer-entry-item', { hasText: 'customers' }).click();
    await expect(page.locator('#schemaViewerTableCommentText')).toHaveClass(/hidden/);
  });

  test('the "Likely relationships (naming convention, unconfirmed):" global section gets its own tree group, instead of being left at the bottom of the last table', async ({ page }) => {
    // Regression test for the same class of bug as Grants/Comments above -
    // this section had no parser of its own at all before this, so it
    // fell through unrecognized to the bottom of the last table entry's
    // raw text, describing the whole connection but looking like it only
    // applied to that one table.
    await gotoApp(page);
    await mockSchema(page, {
      entries: [
        { name: 'customers', heading: 'Table: customers', text: 'Table: customers\n  id integer NOT NULL' },
        { name: 'orders', heading: 'Table: orders', text: (
          'Table: orders\n  id integer NOT NULL\n  customer_id integer NOT NULL\n  region_id integer NOT NULL\n\n' +
          'Likely relationships (naming convention, unconfirmed):\n' +
          '  orders.customer_id -> likely relationship (unconfirmed): references customers, based on column naming convention only - no enforced foreign key found.\n' +
          '  orders.region_id -> likely relationship (unconfirmed): same column name also appears in shipments, based on column naming convention only - no enforced foreign key found.'
        ) },
      ],
    });
    await openSchemaViewer(page);

    // Not left behind in the "orders" table's own raw-text pane any more.
    await page.locator('.schema-viewer-group-header', { hasText: 'Tables' }).click();
    await page.locator('.schema-viewer-entry-item', { hasText: 'orders' }).click();
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('Likely relationships');
    await expect(page.locator('#schemaViewerDetailText')).not.toContainText('likely relationship (unconfirmed)');

    // Its own separate, collapsible "Likely Relationships (2)" group
    // instead - both heuristics, not just the ER diagram's own narrower
    // "references <table>" one.
    const relHeader = page.locator('.schema-viewer-group-header', { hasText: 'Likely Relationships' });
    await expect(relHeader).toContainText('Likely Relationships (2)');
    await relHeader.click();
    const relItems = page.locator('.schema-viewer-entry-item[data-category="relationships"]');
    await expect(relItems).toHaveCount(2);
    await expect(relItems.nth(0)).toContainText('orders.customer_id');
    await expect(relItems.nth(1)).toContainText('orders.region_id');

    await relItems.nth(0).click();
    await expect(page.locator('#schemaViewerDetailHeading')).toHaveText('orders.customer_id');
    await expect(page.locator('#schemaViewerDetailText')).toContainText('references customers');
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

// The dataset-group variant (client.js's openGroupSchemaViewer()/
// loadGroupSchemaViewer(), backed by GET /api/schema/group -
// config_routes.py's handle_get_group_schema()/db.py's
// build_group_schema_summaries()) - shown instead of the above when the
// "i" icon on the dataset badge is clicked while a dataset group, not a
// single preset/custom connection, is the selected option. GET /api/config
// is mocked here (unlike the plain "schema viewer" describe block above,
// which runs against the real single-preset local-dev server) since a
// dataset group requires its own configured_database_groups/in_scope_mode/
// in_scope_group_id session state - same reasoning multi-database.spec.js's
// own buildConfigState()/mockConfig() gives for doing the same thing.
test.describe('dataset group schema viewer', () => {
  function buildGroupConfigState(overrides) {
    return {
      auth_enabled: false,
      session_id: 'e2e-session',
      user_id: 'global',
      authenticated: false,
      is_cloud_run: false,
      configured_databases: [
        { id: 'p-a', name: 'Sales Postgres', type: 'postgres' },
        { id: 'p-b', name: 'Marketing Postgres', type: 'postgres' },
      ],
      configured_database_groups: [
        { id: 'grp-ab', name: 'Sales & Marketing', dataset_list: ['p-a', 'p-b'] },
      ],
      active_preset_id: 'p-a',
      default_database_url: '',
      active_database_url: '',
      active_database_type: 'postgres',
      active_is_custom: false,
      active_custom_connection_key: '',
      active_uses_custom_credentials: false,
      database_name: 'Sales Postgres',
      custom_database_name: '',
      custom_database_url: '',
      custom_databases: [],
      auto_sql_execute: false,
      in_scope_preset_ids: ['p-a', 'p-b'],
      in_scope_custom_connection_keys: [],
      in_scope_mode: 'group',
      in_scope_group_id: 'grp-ab',
      max_in_scope_connections: 20,
      ...overrides,
    };
  }

  /** Same stateful GET+POST /api/config mock multi-database.spec.js's own
   * mockConfig() uses (see that file's docstring on it) - a plain GET-only
   * mock would work for every test below except the last one, which saves
   * a new selection through the real config-modal Save flow and needs
   * that POST to actually be reflected back, not silently fall through to
   * the real (group-less) local-dev server. Returns the live `state`
   * object in case a test wants to inspect what was last saved. */
  async function mockConfig(page, initial) {
    const state = initial || buildGroupConfigState();
    await page.route('**/api/config', async (route) => {
      const method = route.request().method();
      if (method === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
        return;
      }
      if (method === 'POST') {
        const body = route.request().postDataJSON() || {};
        if (body.in_scope_mode !== undefined) state.in_scope_mode = body.in_scope_mode;
        if (body.in_scope_group_id !== undefined) state.in_scope_group_id = body.in_scope_group_id;
        if (body.preset_id !== undefined) {
          state.active_preset_id = body.preset_id;
          state.active_is_custom = false;
          state.active_custom_connection_key = '';
          const matched = state.configured_databases.find((db) => db.id === body.preset_id);
          state.database_name = matched ? matched.name : state.database_name;
        }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state) });
        return;
      }
      return route.fallback();
    });
    return state;
  }

  async function mockGroupSchema(page, { id = 'grp-ab', name = 'Sales & Marketing', datasets, status, error } = {}) {
    const finalDatasets = datasets || [
      { id: 'p-a', name: 'Sales Postgres', type: 'PostgreSQL', data_size: '~2.4 GB', schema_size_tokens: 320, available: true },
      { id: 'p-b', name: 'Marketing Postgres', type: 'PostgreSQL', data_size: null, schema_size_tokens: 150, available: true },
    ];
    await page.route('**/api/schema/group*', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      if (error !== undefined) {
        await route.fulfill({
          status: status || 502, contentType: 'application/json',
          body: JSON.stringify({ success: false, error }),
        });
        return;
      }
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ success: true, kind: 'group', id, name, datasets: finalDatasets }),
      });
    });
  }

  async function openSchemaViewerBtn(page) {
    await page.locator('#datasetSchemaViewerBtn').click();
    await expect(page.locator('#schemaViewerModal')).not.toHaveClass(/hidden/);
  }

  test('title is "<group name> (Dataset Group)", with no data-size/schema-size subheader at all', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page);
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    await expect(page.locator('#schemaViewerModalTitleText')).toHaveText('Sales & Marketing (Dataset Group)');
    await expect(page.locator('#schemaViewerFactsLine')).toHaveClass(/hidden/);
    await expect(page.locator('#schemaViewerFactsLine')).toBeEmpty();
  });

  test('the main area is a flat name/type/data-size/schema-size table, not the LHS/RHS panes', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page);
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    await expect(page.locator('#schemaViewerPanesWrap')).toHaveClass(/hidden/);
    await expect(page.locator('#schemaViewerGroupTableWrap')).not.toHaveClass(/hidden/);

    const headers = page.locator('.schema-viewer-group-table th');
    await expect(headers).toHaveText(['Name', 'Type', 'Data Size', 'Schema Size (tokens)']);

    const rows = page.locator('#schemaViewerGroupTableBody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0).locator('td')).toHaveText(['Sales Postgres', 'PostgreSQL', '~2.4 GB', '320']);
    // p-b's mocked data_size is null - shown as an em-dash, never a blank
    // cell or a misleading "null"/"0".
    await expect(rows.nth(1).locator('td')).toHaveText(['Marketing Postgres', 'PostgreSQL', '—', '150']);
  });

  test('a dataset whose schema fetch failed shows em-dashes and a dimmed row, without breaking the rest of the table', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page, {
      datasets: [
        { id: 'p-a', name: 'Sales Postgres', type: 'PostgreSQL', data_size: null, schema_size_tokens: null, available: false },
        { id: 'p-b', name: 'Marketing Postgres', type: 'PostgreSQL', data_size: '~500 MB', schema_size_tokens: 90, available: true },
      ],
    });
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    const rows = page.locator('#schemaViewerGroupTableBody tr');
    await expect(rows.nth(0)).toHaveClass(/schema-viewer-group-row--unavailable/);
    await expect(rows.nth(0).locator('td')).toHaveText(['Sales Postgres', 'PostgreSQL', '—', '—']);
    await expect(rows.nth(1)).not.toHaveClass(/schema-viewer-group-row--unavailable/);
    await expect(rows.nth(1).locator('td')).toHaveText(['Marketing Postgres', 'PostgreSQL', '~500 MB', '90']);
  });

  test('a group with no datasets shows a plain empty-state row instead of an empty table', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page, { datasets: [] });
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    await expect(page.locator('#schemaViewerGroupTableBody')).toContainText('This dataset group has no datasets in it.');
  });

  test('a fetch error surfaces in the notice bar, same as the single-connection viewer', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page, { error: 'Dataset group not found.', status: 404 });
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    await expect(page.locator('#schemaViewerNotice')).toContainText('Dataset group not found.');
    await expect(page.locator('#schemaViewerNotice')).not.toHaveClass(/hidden/);
  });

  test('the Refresh Schema button and its status line stay in the header, but the button is disabled with an explanatory tooltip', async ({ page }) => {
    await mockConfig(page);
    await mockGroupSchema(page);
    await gotoApp(page);
    await openSchemaViewerBtn(page);

    const refreshBtn = page.locator('#schemaViewerRefreshBtn');
    await expect(refreshBtn).toBeVisible();
    await expect(refreshBtn).toBeDisabled();
    await expect(refreshBtn).toHaveAttribute('title', /isn't available yet/);
    // No single "last refreshed" timestamp exists for a whole group at
    // once - the status line stays in the layout (see index.html's own
    // comment) but shows nothing here.
    await expect(page.locator('#schemaViewerRefreshStatus')).toHaveText('');
  });

  test('opening the group viewer, closing it, then opening a specific preset\'s own viewer shows the LHS/RHS panes again, not a leftover group table', async ({ page }) => {
    // Regression guard for the shared-modal layout switch (openSchemaViewer()'s
    // own reset of schemaViewerGroupTableWrap/schemaViewerPanesWrap) - this
    // modal instance persists across opens, so a single-connection open
    // right after a group one must not still be showing the previous
    // group's flat table underneath, with the LHS/RHS panes still hidden.
    await mockConfig(page, { ...buildGroupConfigState(), in_scope_mode: 'single', in_scope_group_id: '' });
    await mockGroupSchema(page);
    await mockSchema(page, { name: 'Sales Postgres', dialect: 'PostgreSQL' });
    await gotoApp(page);

    // Switches into group mode via the real config-modal Save flow (the
    // same path a real user takes), rather than poking at client.js's
    // module-scoped state directly (IN_SCOPE_MODE/IN_SCOPE_GROUP_ID aren't
    // reachable from the page's own `window`, and this test is about the
    // shared modal's own layout bookkeeping either way, not about how
    // group mode gets selected in the first place - covered above).
    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#configModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
    await page.locator('input[name="db_connection_option"][value="group:grp-ab"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await openSchemaViewerBtn(page);
    await expect(page.locator('#schemaViewerGroupTableWrap')).not.toHaveClass(/hidden/);
    await page.locator('#schemaViewerModalCloseBtn').click();
    await expect(page.locator('#schemaViewerModal')).toHaveClass(/hidden/);

    // Switch to the specific preset, then reopen the "i" icon.
    await page.locator('#configTriggerBadge').click();
    await expect(page.locator('#configModal')).not.toHaveClass(/hidden/, { timeout: 15_000 });
    await page.locator('input[name="db_connection_option"][value="preset:p-a"]').check();
    await page.locator('#configSaveBtn').click();
    await expect(page.locator('#configModal')).toHaveClass(/hidden/);

    await openSchemaViewerBtn(page);
    await expect(page.locator('#schemaViewerGroupTableWrap')).toHaveClass(/hidden/);
    await expect(page.locator('#schemaViewerPanesWrap')).not.toHaveClass(/hidden/);
    await expect(page.locator('#schemaViewerModalTitleText')).toContainText('Sales Postgres in PostgreSQL');
  });
});
