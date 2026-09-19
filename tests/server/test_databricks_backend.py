"""
backends/databricks.py, driven two ways:
  - connect(): against the fake databricks.sql.connect harness
    (helpers.install_fake_databricks_connect) - verifies the access_token
    requirement and the catalog/schema kwarg dispatch without opening a
    real connection.
  - get_schema()/get_schema_shallow()/execute()/identity_label()/cache_key():
    against the same fake psycopg2-shaped cursor/connection
    tests/test_postgres_backend.py uses (helpers.make_fake_pg_connection) -
    databricks-sql-connector implements the same PEP 249 DB-API cursor
    shape, so no Databricks-specific fake is needed for these.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its queries unconditionally and in a fixed order for
tables/columns, then best-effort (try/except) for the rest - see
backends/databricks.py:
  1. table names (+ table_type, comment)   2. columns (+ is_identity,
     comment, partition_index)             3. constraints (best-effort)
  4. views (best-effort)                    5. routines (best-effort, TWO
     queries: routines, then parameters)    6. grants (best-effort)
No Indexes/Triggers/session-timezone/row-count-estimate/RLS-existence
sections at all - see that module's own comments on why each was skipped
as too uncertain to fabricate from this sandbox.

get_schema() (deep) then runs _build_shallow_schema_parts() (the seven
queries above, up to nine when routines succeed) and appends its own
Phase 2 queries on a fresh cursor use, per kept table (in order): live
COUNT(*), an optional combined MIN()/MAX() query (if it has numeric/date
columns), an optional combined APPROX_COUNT_DISTINCT cardinality-gate query
(if it has categorical columns), and up to
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE frequent-value GROUP BY queries
for whichever categorical columns pass that gate - see
test_get_schema_deep_* below for worked examples of this second phase's
exact response queue.

Unlike Postgres/MySQL/Snowflake's connectors, databricks-sql-connector's
declared DB-API paramstyle is "named" (:name, not %s/pyformat) - the
dynamic IN (...) clause tests below check for that shape specifically
(a dict of params, :t0/:t1-style placeholders in the SQL text) rather than
reusing Snowflake's %s-array-style assertions.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.databricks import DatabricksBackend
from backends.base import SqlExecutionError, format_dataset_size_line
from helpers import install_fake_databricks_connect, make_fake_pg_connection


def _dbx(monkeypatch):
    harness = install_fake_databricks_connect(monkeypatch)
    return DatabricksBackend(), harness


def _pad_column_row(row):
    """A columns_rows tuple may still be the pre-Phase-1-attributes 4-tuple
    (table_name, column_name, data_type, is_nullable) that every test
    predating the identity/comment/partition-index columns already uses -
    padded here to the real 7-column shape
    _build_shallow_schema_parts()'s columns query now selects
    (..., is_identity, comment, partition_index), defaulting to "NO"/None/
    None (not an identity column, no comment, not a partition column), so
    none of those existing tests need to be rewritten just because three
    more columns joined the SELECT list. Mirrors
    tests/test_postgres_backend.py's own _pad_column_row for the identical
    reason."""
    row = list(row)
    defaults = ["NO", None, None]
    while len(row) < 7:
        row.append(defaults[len(row) - 4])
    return tuple(row)


def _schema_responses(
    table_names, columns_rows, constraints=(), views=(),
    routines=(), routine_params=(), grants=(),
    table_types=None, table_comments=None,
):
    """Builds the fixed-order response queue _build_shallow_schema_parts()
    issues: table names (with table_type/comment) -> columns -> constraints
    -> views -> routines -> routine params -> grants.

    `table_types`: optional {table_name: table_type} - defaults every name
    in `table_names` to 'MANAGED' (an ordinary table) unless overridden,
    e.g. table_types={"linked": "FOREIGN"} for a federated table.
    `table_comments`: optional {table_name: comment}."""
    table_types = table_types or {}
    table_comments = table_comments or {}
    table_rows = [
        (n, table_types.get(n, "MANAGED"), table_comments.get(n))
        for n in table_names
    ]
    return [
        (table_rows, None, -1),
        ([_pad_column_row(r) for r in columns_rows], None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
        (list(routines), None, -1),
        (list(routine_params), None, -1),
        (list(grants), None, -1),
    ]


# --- connect() ---------------------------------------------------------------

def test_connect_passes_required_kwargs(monkeypatch):
    backend, harness = _dbx(monkeypatch)
    backend.connect({
        "type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123", "access_token": "dapi-secret",
    })
    call = harness.calls[-1]
    assert call["server_hostname"] == "dbc-x.cloud.databricks.com"
    assert call["http_path"] == "/sql/1.0/warehouses/abc123"
    assert call["access_token"] == "dapi-secret"
    assert "catalog" not in call
    assert "schema" not in call


def test_connect_passes_optional_catalog_and_schema_when_given(monkeypatch):
    backend, harness = _dbx(monkeypatch)
    backend.connect({
        "type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123", "access_token": "dapi-secret",
        "catalog": "main", "schema": "sales",
    })
    call = harness.calls[-1]
    assert call["catalog"] == "main"
    assert call["schema"] == "sales"


def test_connect_raises_when_no_access_token_given(monkeypatch):
    backend, harness = _dbx(monkeypatch)
    try:
        backend.connect({
            "type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc123",
        })
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


# --- cache_key -----------------------------------------------------------------

def test_cache_key_is_host_slash_catalog_dot_schema():
    backend = DatabricksBackend()
    key = backend.cache_key({
        "server_hostname": "dbc-x.cloud.databricks.com", "catalog": "main", "schema": "sales",
    })
    assert key == "dbc-x.cloud.databricks.com/main.sales"


def test_cache_key_handles_missing_fields():
    backend = DatabricksBackend()
    assert backend.cache_key({}) == "unknown/unknown.unknown"


def test_cache_key_never_includes_credentials():
    backend = DatabricksBackend()
    key = backend.cache_key({
        "server_hostname": "dbc-x.cloud.databricks.com", "catalog": "main", "schema": "sales",
        "access_token": "dapi-secret-token",
    })
    assert "dapi-secret-token" not in key


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_catalog_and_user():
    conn, cursor = make_fake_pg_connection([([("main", "alice@example.com")], None, -1)])
    backend = DatabricksBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "main"
    assert username == "alice@example.com"


# --- get_schema ------------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = DatabricksBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_table_with_columns():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "int", "NO"),
            ("customers", "name", "string", "YES"),
        ],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id int NOT NULL" in schema
    assert "name string NULL" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Table family: events_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert "Table: events_20240102" not in schema


def test_get_schema_views_section_is_not_scoped_to_kept_names_regression():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO")],
        views=[("customer_orders", "SELECT * FROM orders JOIN customers ...")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_section():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
        constraints=[("orders", "orders_pk", "PRIMARY KEY", "id")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema
    assert "orders_pk" in schema


def test_get_schema_survives_constraints_query_failure():
    # Best-effort: a non-Unity-Catalog workspace may not expose
    # table_constraints at all - that must degrade to "skip this section",
    # not fail the whole schema fetch (mirrors backends/snowflake.py's/
    # backends/bigquery.py's same try/except).
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "table_constraints" in sql:
                raise Exception("table_constraints not available on this workspace")

        def fetchall(self):
            if "information_schema.tables" in self.calls[-1][0]:
                return [("orders", "MANAGED", None)]
            if "information_schema.columns" in self.calls[-1][0]:
                return [("orders", "id", "int", "NO", "NO", None, None)]
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = DatabricksBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: orders" in schema
    assert "Constraints:" not in schema


def test_get_schema_scopes_columns_query_with_named_placeholders_not_string_formatting():
    # Table names deliberately don't look like the generated :t0/:t1
    # placeholder names themselves (see _named_in_params), so the "never
    # string-formatted directly into SQL" assertion below can't accidentally
    # pass just because a table name happens to collide with a placeholder.
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["tbl_a", "tbl_b"],
        columns_rows=[("tbl_a", "id", "int", "NO"), ("tbl_b", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    backend.get_schema(conn)

    columns_sql, columns_params = cursor.calls[1]
    assert "information_schema.columns" in columns_sql
    assert "tbl_a" not in columns_sql  # never string-formatted directly into SQL
    assert "tbl_b" not in columns_sql
    # "named" paramstyle - a dict of :name -> value, not a %s-style list/tuple.
    assert isinstance(columns_params, dict)
    assert set(columns_params.values()) == {"tbl_a", "tbl_b"}
    assert ":t0" in columns_sql and ":t1" in columns_sql


def test_get_schema_uses_current_catalog_and_schema_not_hardcoded_names():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "current_catalog()" in table_names_sql
    assert "current_schema()" in table_names_sql
    assert "'public'" not in table_names_sql  # not hardcoded to Postgres's default


def test_get_schema_table_type_filter_uses_databricks_values_not_ansi_base_table():
    # Regression test: Databricks' information_schema.tables reports
    # ordinary tables as 'MANAGED'/'EXTERNAL' (or their shallow-clone
    # variants), NOT the ANSI-standard 'BASE TABLE' value every other
    # dialect here uses - filtering on 'BASE TABLE' silently matched zero
    # rows against a real workspace (get_schema() returning None even
    # though the connection worked and tables existed). See
    # https://docs.databricks.com/aws/en/sql/language-manual/information-schema/tables.
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "'BASE TABLE'" not in table_names_sql
    assert "'MANAGED'" in table_names_sql
    assert "'EXTERNAL'" in table_names_sql


# --- FOREIGN-table flip: surfaced with a flag instead of silently excluded ---
# Regression guard for the plan's explicit behavior change: a
# table_type='FOREIGN' row used to never match the old IN (...) filter at
# all, so such a table never appeared anywhere in schema text - not even a
# heading. It must now appear (counting toward kept_names/SCHEMA_MAX_TABLES
# like any other table) with its columns, annotated as external/foreign.

def test_get_schema_foreign_table_now_appears_flagged_instead_of_excluded():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["local_orders", "linked_customers"],
        columns_rows=[
            ("local_orders", "id", "int", "NO"),
            ("linked_customers", "id", "int", "NO"),
        ],
        table_types={"linked_customers": "FOREIGN"},
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Table: local_orders" in schema
    assert "Table: linked_customers" in schema
    assert "EXTERNAL/FOREIGN" in schema
    # The flag must be on the foreign table specifically, not the ordinary one.
    foreign_heading = [l for l in schema.splitlines() if l.startswith("Table: linked_customers")][0]
    assert "EXTERNAL/FOREIGN" in foreign_heading
    local_heading = [l for l in schema.splitlines() if l.startswith("Table: local_orders")][0]
    assert "EXTERNAL/FOREIGN" not in local_heading
    # Its columns are described too, not just a bare flagged heading.
    assert "id int NOT NULL" in schema


def test_get_schema_table_type_filter_now_includes_foreign():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "'FOREIGN'" in table_names_sql


def test_get_schema_foreign_table_counts_toward_schema_max_tables_cap():
    """A FOREIGN table must be subject to the exact same kept_names/
    SCHEMA_MAX_TABLES capping every other table type goes through - the
    flip only changes whether it's excluded outright, not whether it's
    still bounded like any other table."""
    from backends.base import SCHEMA_MAX_TABLES

    # "zzz_foreign_extra" sorts alphabetically AFTER every "tbl_*" name
    # below, so cap_kept_tables' deterministic (alphabetical) cap drops it
    # once the "tbl_*" names alone already fill SCHEMA_MAX_TABLES.
    table_names = [f"tbl_{i:04d}" for i in range(SCHEMA_MAX_TABLES)] + ["zzz_foreign_extra"]
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=table_names,
        columns_rows=[(table_names[0], "id", "int", "NO")],
        table_types={"zzz_foreign_extra": "FOREIGN"},
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "more table(s)" in schema
    # The cap keeps the SCHEMA_MAX_TABLES alphabetically-first entries -
    # "zzz_foreign_extra" (alphabetically after every "tbl_*" name) is
    # capped out, same as it would be for any other table_type.
    assert "Table: zzz_foreign_extra" not in schema
    assert "EXTERNAL/FOREIGN" not in schema


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_identity_column_marker_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[
            ("users", "id", "bigint", "NO", "YES", None, None),
            ("users", "email", "string", "NO", "NO", None, None),
        ],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id bigint NOT NULL IDENTITY" in schema
    assert "email string NOT NULL" in schema
    email_line = [l for l in schema.splitlines() if l.strip().startswith("email")][0]
    assert "IDENTITY" not in email_line


def test_get_schema_shallow_identity_marker_absent_by_default():
    """Old-style 4-tuple columns_rows (predating is_identity/comment/
    partition_index) must be padded to "not an identity column, no
    comment, not a partition column" - see _pad_column_row - so no
    existing test needs rewriting."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "IDENTITY" not in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "string", "NO", "NO", "Order lifecycle state.", None)],
        table_comments={"orders": "Customer purchase orders."},
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" in schema
    assert "[table] orders: Customer purchase orders." in schema
    assert "[column] orders.status: Order lifecycle state." in schema


def test_get_schema_shallow_comments_section_absent_when_no_comments():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_routines_render_name_and_signature_without_body():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
        routines=[("total_for_customer_1", "total_for_customer", "double", "SELECT SUM(amount) ...")],
        routine_params=[("total_for_customer_1", "customer_id", "bigint", 1)],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_for_customer(customer_id bigint) -> double" in schema
    assert "SELECT SUM(amount)" not in schema


def test_get_schema_shallow_routines_survive_when_parameters_query_fails():
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append(sql)
            if "information_schema.parameters" in sql:
                raise Exception("parameters view not available")

        def fetchall(self):
            last = self.calls[-1]
            if "information_schema.tables" in last:
                return [("orders", "MANAGED", None)]
            if "information_schema.columns" in last:
                return [("orders", "id", "int", "NO", "NO", None, None)]
            if "information_schema.routines" in last:
                return [("f_1", "f", "int", None)]
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(RaisingConnection())
    assert "Table: orders" in schema
    assert "Routines:" not in schema


def test_get_schema_shallow_partition_columns_render_ordered_by_partition_index():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["events"],
        columns_rows=[
            ("events", "id", "bigint", "NO", "NO", None, None),
            ("events", "region", "string", "NO", "NO", None, 1),
            ("events", "event_date", "date", "NO", "NO", None, 0),
        ],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Partition columns:" in schema
    assert "events: event_date, region" in schema


def test_get_schema_shallow_partition_columns_section_absent_when_none():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Partition columns:" not in schema


def test_get_schema_shallow_grants_section_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
        grants=[("analyst_role", "orders", "SELECT")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants:" in schema
    assert "Grant SELECT on orders to analyst_role" in schema


def test_get_schema_shallow_grants_section_absent_when_no_grants():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants:" not in schema


def test_get_schema_shallow_survives_grants_query_failure():
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append(sql)
            if "table_privileges" in sql:
                raise Exception("table_privileges not visible to this role")

        def fetchall(self):
            last = self.calls[-1]
            if "information_schema.tables" in last:
                return [("orders", "MANAGED", None)]
            if "information_schema.columns" in last:
                return [("orders", "id", "int", "NO", "NO", None, None)]
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(RaisingConnection())
    assert "Table: orders" in schema
    assert "Grants:" not in schema


# --- get_schema_shallow() must never include Phase 2 (deep-only) content -----

def test_get_schema_shallow_excludes_full_view_and_routine_bodies_and_phase2_sections():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO"),
            ("orders", "status", "string", "NO"),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total_1", "get_total", "int", "SELECT 1;")],
    ))
    backend = DatabricksBackend()
    schema = backend.get_schema_shallow(conn)
    assert "View v" in schema
    assert "SELECT 1 FROM orders" not in schema
    assert "View definitions:" not in schema
    assert "get_total" in schema
    assert "SELECT 1;" not in schema
    assert "Routine definitions:" not in schema
    assert "Live row counts:" not in schema
    assert "Column value samples:" not in schema
    assert "Likely relationships" not in schema
    # Exactly the seven Phase 1 queries (tables, columns, constraints, views,
    # routines, routine params, grants) - no Phase 2 query was ever issued.
    assert len(cursor.calls) == 7


# --- get_schema() (deep): Phase 2 additions on top of the shallow content ----

def _base_deep_responses():
    return _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", "NO", None, None),
            ("orders", "status", "string", "NO", "NO", None, None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total_1", "get_total", "int", "SELECT 1;")],
    )


def test_get_schema_deep_is_superset_of_shallow_plus_phase2_sampling():
    responses = _base_deep_responses() + [
        ([(42,)], None, -1),                                     # live count for orders
        ([(1, 100)], None, -1),                                  # min/max for id
        ([(2,)], None, -1),                                       # approx_count_distinct(status)
        ([("active", 30), ("inactive", 12)], None, -1),          # frequent values for status
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)

    # Shallow content still present (Phase 1 catalog-only sections).
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "get_total" in schema

    # Phase 2 additions on top.
    assert "View definitions:" in schema and "View v: SELECT 1 FROM orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "status: frequent values = active (30), inactive (12)" in schema

    assert len(cursor.calls) == 7 + 4


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    """A column whose APPROX_COUNT_DISTINCT() comes back close to the
    table's own live row count is treated as near-unique - sampling
    "frequent values" for it wouldn't be meaningful, so that column's
    GROUP BY query must never even be issued."""
    responses = _base_deep_responses() + [
        ([(42,)], None, -1),   # live count
        ([(1, 100)], None, -1),  # min/max for id
        ([(41,)], None, -1),    # approx_count_distinct(status) - 41 of 42 rows distinct
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 7 + 3


def test_get_schema_deep_naming_convention_relationships_section():
    # "binary" is deliberately neither a NUMERIC_OR_DATE nor a CATEGORICAL
    # type (see backends/databricks.py's _is_numeric_or_date_type/
    # _is_categorical_type) - so each table's Phase 2 pass issues only its
    # live COUNT(*) query, no min/max or cardinality-gate/frequent-value
    # queries, keeping this test's response queue to exactly one entry per
    # table (mirrors test_postgres_backend.py's own use of "bytea" for the
    # identical reason).
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "binary", "NO"),
            ("orders", "customer_id", "binary", "NO"),
        ],
    ) + [
        ([(10,)], None, -1),   # live count: customers
        ([(20,)], None, -1),   # live count: orders
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "orders.customer_id -> likely relationship (unconfirmed): references customers" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_pg_connection(_schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "binary", "NO"),
            ("orders", "customer_id", "binary", "NO"),
        ],
    ))
    shallow = backend.get_schema_shallow(conn2)
    assert "Likely relationships" not in shallow


# --- get_schema() (deep): "Estimated dataset size" line ----------------------
# Reuses live_counts (the per-table live COUNT(*) results the "Live row
# counts" loop just gathered) rather than any new query - see
# backends/databricks.py's own comment on why this is a live scan of only
# the shown/kept tables, never a true schema-wide catalog estimate, and why
# the `note` argument exists to make that caveat visible in the line itself.

def test_get_schema_deep_dataset_size_line_sums_live_counts():
    # "binary" keeps each table's Phase 2 pass to just its live COUNT(*)
    # query (no min/max or cardinality-gate queries) - see
    # test_get_schema_deep_naming_convention_relationships_section's own
    # comment for why.
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "binary", "NO"),
            ("orders", "customer_id", "binary", "NO"),
        ],
    ) + [
        ([(10,)], None, -1),  # live count: customers
        ([(32,)], None, -1),  # live count: orders
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)

    expected_line = format_dataset_size_line(
        total_rows=42,
        note=(
            "live count of the tables shown here only, not a "
            "schema-wide total - Databricks has no cheap "
            "catalog-only row-count statistic"
        ),
    )
    assert expected_line in schema
    # The caveat text must actually be visible in the rendered line, not
    # just correctly assembled by format_dataset_size_line() in isolation.
    assert "not a schema-wide total" in schema


def test_get_schema_deep_omits_dataset_size_line_when_every_live_count_fails():
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "binary", "NO"),
            ("orders", "customer_id", "binary", "NO"),
        ],
    ) + [
        Exception("live count failed"),  # customers
        Exception("live count failed"),  # orders
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" not in schema
    assert "Estimated dataset size" not in schema


def test_get_schema_deep_skips_sampling_for_wide_tables_but_keeps_live_count():
    """A table with more columns than MAX_COLUMNS_FOR_SAMPLING still gets a
    live row count, just no per-column sampling - bounding the "explosion of
    tiny queries" the cap exists to prevent."""
    from backends.databricks import MAX_COLUMNS_FOR_SAMPLING

    columns_rows = [
        ("wide", f"col_{i}", "int", "NO")
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["wide"],
        columns_rows=columns_rows,
    ) + [
        ([(7,)], None, -1),   # live count for wide
        # no min/max or cardinality-gate response queued - must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "wide: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 7 + 1


def test_get_schema_deep_uses_approx_count_distinct_not_exact_count_distinct():
    responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "string", "NO")],
    ) + [
        ([(5,)], None, -1),                                # live count
        ([(2,)], None, -1),                                # approx_count_distinct(status)
        ([("active", 3), ("inactive", 2)], None, -1),      # frequent values
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    backend.get_schema(conn)
    cardinality_sql = cursor.calls[7 + 1][0]
    assert "APPROX_COUNT_DISTINCT" in cardinality_sql
    assert "COUNT(DISTINCT" not in cardinality_sql


# --- execute ---------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = true;")
    assert results[0]["columns"] is None
    assert results[0]["rowCount"] == 3


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [([], None, 1), ([(1,)], [("id",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "UPDATE t SET x=1; SELECT id FROM t;")
    assert len(results) == 2
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    responses = [([], None, 1), RuntimeError("PARSE_SYNTAX_ERROR")]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "PARSE_SYNTAX_ERROR" in str(err)


def test_execute_never_calls_autocommit_setter_or_method():
    # Connection.autocommit is a read-only property on the real connector
    # (see module docstring) - execute() must not try to set or call it, or
    # this would raise against a fake that doesn't support either.
    class NoAutocommitConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

    conn, cursor = make_fake_pg_connection([([], None, 1)])
    bare_conn = NoAutocommitConnection(cursor)
    backend = DatabricksBackend()
    results = backend.execute(bare_conn, "UPDATE t SET x=1;")
    assert results[0]["rowCount"] == 1


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale - this just proves DatabricksBackend routes through the same
# shared fetch_capped_rows() (backends/base.py) instead of its own
# fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = DatabricksBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]
