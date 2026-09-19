"""
backends/mysql.py, driven entirely against a fake PyMySQL-shaped
connection/cursor (see helpers.make_fake_mysql_connection) - no real MySQL
needed.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its queries unconditionally and in a fixed order
(several of the new sections are individually try/except-wrapped for
graceful degradation - see mysql.py itself), so responses are queued in the
exact order it issues them:
  1. table names        2. columns             3. constraints
  4. indexes            5. views                6. grants
  7. triggers           8. table comments/rows (new)
  9. routines (new)     10. session settings (new)

get_schema() (deep) then runs _build_shallow_schema_parts() (the ten queries
above) and appends its own Phase 2 queries on a fresh cursor use, per kept
table (in order): live COUNT(*), an optional combined MIN()/MAX() query (if
it has numeric/date columns), and - per eligible categorical column, up to
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE - one COUNT(DISTINCT ...) gate
query followed by a frequent-value GROUP BY query - see
test_get_schema_deep_* below for worked examples of this second phase's
exact response queue.

connect()'s own URL-parsing/kwarg-building logic is tested separately
against helpers.install_fake_pymysql_connect, which patches
backends.mysql's pymysql.connect() and records the kwargs it was called
with - mirroring how backends/snowflake.py's connect() dispatch is tested.
"""

import os
import ssl
import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.mysql import MySQLBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import (
    make_fake_mysql_connection, install_fake_pymysql_connect, make_self_signed_ca_cert_pem,
)


def _pad_column_row(row):
    """A columns_rows tuple may still be the pre-EXTRA/COLUMN_COMMENT
    5-tuple (table_name, column_name, data_type, is_nullable, column_default)
    that every test predating the Phase 1 identity/comment attributes
    already uses - padded here to the real 7-column shape
    _build_shallow_schema_parts()'s columns query now selects
    (..., EXTRA, COLUMN_COMMENT), defaulting to ""/None (not an
    auto_increment column, no comment), so none of those existing tests
    need to be rewritten just because two more columns joined the SELECT
    list. Mirrors backends/postgres.py's own _pad_column_row exactly, just
    with MySQL's EXTRA/COLUMN_COMMENT pair instead of Postgres's
    is_identity/identity_generation pair."""
    row = list(row)
    while len(row) < 7:
        row.append("" if len(row) == 5 else None)
    return tuple(row)


def _schema_responses(
    table_names, columns_rows, constraints=(), indexes=(), views=(), grants=(), triggers=(),
    table_meta=(), routines=(), session_settings=("SYSTEM", "utf8mb4_general_ci"),
):
    return [
        ([(n,) for n in table_names], None, -1),
        ([_pad_column_row(r) for r in columns_rows], None, -1),
        (list(constraints), None, -1),
        (list(indexes), None, -1),
        (list(views), None, -1),
        (list(grants), None, -1),
        (list(triggers), None, -1),
        (list(table_meta), None, -1),
        (list(routines), None, -1),
        ([session_settings] if session_settings is not None else [], None, -1),
    ]


# --- get_schema ----------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_mysql_connection([([], None, -1)])
    backend = MySQLBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_tables_with_columns():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "int", "NO", None),
            ("customers", "name", "varchar", "YES", None),
        ],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id int NOT NULL" in schema
    assert "name varchar NULL" in schema


def test_get_schema_includes_column_default():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "varchar", "NO", "pending")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "DEFAULT pending" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "int", "NO", None)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Table family: events_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert f"{members[0]} .. {members[-1]}" in schema
    assert "Table: events_20240102" not in schema


def test_get_schema_views_section_is_not_scoped_to_kept_names():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
        views=[("customer_orders", "select * from orders join customers ...")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_indexes_grants_triggers():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        constraints=[("orders", "PRIMARY", "PRIMARY KEY", "id", None, None)],
        indexes=[("orders", "PRIMARY", "id", 0, 1)],
        grants=[("app_user@%", "orders", "SELECT")],
        triggers=[("orders", "trg_audit", "INSERT", "CALL audit()")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema and "PRIMARY" in schema
    assert "Indexes:" in schema and "UNIQUE" in schema and "id" in schema
    assert "Grants:" in schema and "Grant SELECT on orders to app_user@%" in schema
    assert "Triggers:" in schema and "trg_audit" in schema


def test_get_schema_non_unique_index_labeled_index_not_unique():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        indexes=[("orders", "idx_customer", "customer_id", 1, 1)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "idx_customer (INDEX): customer_id" in schema


def test_get_schema_multi_column_index_lists_all_columns():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "a", "int", "NO", None)],
        indexes=[
            ("orders", "idx_ab", "a", 1, 1),
            ("orders", "idx_ab", "b", 1, 2),
        ],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "idx_ab (INDEX): a, b" in schema


def test_get_schema_foreign_key_constraint_format():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        constraints=[("orders", "orders_customer_fk", "FOREIGN KEY", "customer_id", "customers", "id")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "customer_id -> customers(id)" in schema


def test_get_schema_scan_query_uses_configured_scan_cap():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO", None)],
    ))
    backend = MySQLBackend()
    backend.get_schema(conn)
    first_sql, first_params = cursor.calls[0]
    assert "information_schema.TABLES" in first_sql
    assert first_params[0] > 0  # SCHEMA_MAX_TABLE_NAMES_SCANNED


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_auto_increment_marker_renders():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[
            ("users", "id", "int", "NO", None, "auto_increment", None),
            ("users", "email", "varchar", "NO", None, "", None),
        ],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id int NOT NULL AUTO_INCREMENT" in schema
    # The non-auto_increment column's own line must not pick up a marker.
    email_line = [l for l in schema.splitlines() if l.strip().startswith("email")][0]
    assert "AUTO_INCREMENT" not in email_line


def test_get_schema_shallow_auto_increment_absent_by_default():
    """Old-style 5-tuple columns_rows (predating EXTRA/COLUMN_COMMENT) must
    be padded to "not auto_increment, no comment" - see _pad_column_row - so
    no existing test needs rewriting."""
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "AUTO_INCREMENT" not in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "varchar", "NO", None, "", "Order lifecycle state.")],
        table_meta=[("orders", "Customer purchase orders.", 500)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" in schema
    assert "[table] orders: Customer purchase orders." in schema
    assert "[column] orders.status: Order lifecycle state." in schema


def test_get_schema_shallow_comments_section_absent_when_no_comments():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None, "", "")],
        table_meta=[("orders", "", 500)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_row_count_estimate_renders():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        table_meta=[("orders", None, 1234)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "orders: ~1234 rows (estimate" in schema


def test_get_schema_shallow_row_count_estimate_renders_zero_not_skipped():
    """Unlike Postgres's reltuples (-1 sentinel for "never analyzed"), MySQL's
    TABLE_ROWS genuinely reports 0 for an empty (or not-yet-populated) InnoDB
    table - that's a real, if approximate, value and must still render, not
    be treated as a missing-estimate sentinel."""
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["fresh_table"],
        columns_rows=[("fresh_table", "id", "int", "NO", None)],
        table_meta=[("fresh_table", None, 0)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "fresh_table: ~0 rows (estimate" in schema


def test_get_schema_shallow_row_count_estimate_skips_null_table_rows():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["mystery_table"],
        columns_rows=[("mystery_table", "id", "int", "NO", None)],
        table_meta=[("mystery_table", None, None)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" not in schema


def test_get_schema_shallow_routines_render_name_and_signature_without_body():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        routines=[("total_for_customer", "customer_id int", "decimal", "SELECT SUM(amount) ...")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_for_customer(customer_id int) -> decimal" in schema
    assert "SELECT SUM(amount)" not in schema


def test_get_schema_shallow_routines_procedure_with_no_return_type():
    """A PROCEDURE (as opposed to a FUNCTION) has no return type - r.DATA_TYPE
    comes back NULL/empty for it, so the rendered line must omit the
    "-> ..." suffix entirely rather than showing "-> None"."""
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        routines=[("archive_old_orders", "cutoff_date date", None, "DELETE FROM orders ...")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "archive_old_orders(cutoff_date date)" in schema
    assert "->" not in schema


def test_get_schema_shallow_session_settings_render():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        session_settings=("America/New_York", "utf8mb4_unicode_ci"),
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Session: timezone=America/New_York; default collation=utf8mb4_unicode_ci" in schema


# --- get_schema_shallow() must never include Phase 2 (deep-only) content -----

def test_get_schema_shallow_excludes_full_view_and_routine_bodies_and_phase2_sections():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", None),
            ("orders", "status", "varchar", "NO", None),
        ],
        views=[("v", "select 1 from orders")],
        routines=[("get_total", "p1 int", "int", "SELECT 1;")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema_shallow(conn)
    assert "View v" in schema
    assert "select 1 from orders" not in schema
    assert "View definitions:" not in schema
    assert "get_total" in schema
    assert "SELECT 1;" not in schema
    assert "Routine definitions:" not in schema
    assert "Live row counts:" not in schema
    assert "Column value samples:" not in schema
    assert "Likely relationships" not in schema
    # Exactly the ten Phase 1 queries - no Phase 2 query was ever issued.
    assert len(cursor.calls) == 10


# --- get_schema() (deep): Phase 2 additions on top of the shallow content ----

def _base_deep_responses():
    return _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", None),
            ("orders", "status", "varchar", "NO", None),
        ],
        views=[("v", "select 1 from orders")],
        routines=[("get_total", "p1 int", "int", "SELECT 1;")],
        table_meta=[("orders", None, 500)],
    ) + [
        ([(500, 2_000_000, 1)], None, -1),  # new schema-wide dataset-size query
    ]


def _phase2_sampling_responses():
    return [
        ([(42,)], None, -1),                              # live count for orders
        ([(1, 100)], None, -1),                            # min/max for id
        ([(2,)], None, -1),                                # COUNT(DISTINCT status)
        ([("active", 30), ("inactive", 12)], None, -1),   # frequent values for status
    ]


def test_get_schema_deep_is_superset_of_shallow_plus_phase2_sampling():
    conn, cursor = make_fake_mysql_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = MySQLBackend()
    schema = backend.get_schema(conn)

    # Shallow content still present (Phase 1 catalog-only sections).
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "get_total(p1 int) -> int" in schema
    assert "~500 rows (estimate" in schema

    # Phase 2 additions on top.
    assert "View definitions:" in schema and "View v: select 1 from orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "status: frequent values = active (30), inactive (12)" in schema

    # New schema-wide dataset-size line (Dataset size summary section).
    assert "Estimated dataset size: ~1.9 MB" in schema

    assert len(cursor.calls) == 1 + 10 + 4


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    """A COUNT(DISTINCT ...) close to the live row count means the column is
    near-unique - sampling "frequent values" for it wouldn't be meaningful,
    so that column's GROUP BY query must never even be issued."""
    responses = _base_deep_responses() + [
        ([(42,)], None, -1),   # live count for orders
        ([(1, 100)], None, -1),  # min/max for id
        ([(40,)], None, -1),   # COUNT(DISTINCT status) - 40/42 rows distinct
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 1 + 10 + 3


def test_get_schema_deep_naming_convention_relationships_section():
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "varbinary", "NO", None),
            ("orders", "customer_id", "varbinary", "NO", None),
        ],
    ) + [
        ([(30, 3_000, 2)], None, -1),  # new schema-wide dataset-size query
        ([(10,)], None, -1),   # live count: customers
        ([(20,)], None, -1),   # live count: orders
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "orders.customer_id -> likely relationship (unconfirmed): references customers" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_mysql_connection(_schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "varbinary", "NO", None),
            ("orders", "customer_id", "varbinary", "NO", None),
        ],
    ))
    shallow = backend.get_schema_shallow(conn2)
    assert "Likely relationships" not in shallow


def test_get_schema_deep_skips_sampling_for_wide_tables_but_keeps_live_count():
    """A table with more columns than MAX_COLUMNS_FOR_SAMPLING still gets a
    live row count, just no per-column sampling - bounding the "explosion of
    tiny queries" the cap exists to prevent."""
    from backends.mysql import MAX_COLUMNS_FOR_SAMPLING

    columns_rows = [
        ("wide", f"col_{i}", "int", "NO", None)
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["wide"],
        columns_rows=columns_rows,
    ) + [
        ([(7, 1_000, 1)], None, -1),  # new schema-wide dataset-size query
        ([(7,)], None, -1),   # live count for wide
        # no min/max response queued - it must not be requested
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "wide: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 1 + 10 + 1


def test_get_schema_deep_dataset_size_line_uses_schema_wide_totals_not_kept_names_scope():
    """The new dataset-size query aggregates over EVERY base table in
    DATABASE() (information_schema.TABLES, filtered only by TABLE_TYPE, no
    table-name filter) - unlike the neighboring per-table "Row count
    estimates" query (Phase 1), which is deliberately scoped to kept_names
    (the capped/bounded subset of tables) via a `TABLE_NAME IN (...)`
    filter. Asserts on the actual SQL text/params executed rather than
    behavior alone, since a kept_names-scoped total would still "work" but
    silently misreport genuine schema-wide scale for any schema where table
    capping kicked in."""
    conn, cursor = make_fake_mysql_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = MySQLBackend()
    backend.get_schema(conn)

    dataset_size_calls = [c for c in cursor.calls if "DATA_LENGTH + INDEX_LENGTH" in c[0]]
    assert len(dataset_size_calls) == 1
    sql_text, params = dataset_size_calls[0]
    assert params is None
    assert "IN (" not in sql_text

    # Contrast with the neighboring per-table row-count-estimate query
    # (Phase 1), which DOES filter by table name via kept_names.
    row_estimate_calls = [c for c in cursor.calls if "TABLE_NAME, TABLE_COMMENT, TABLE_ROWS" in c[0]]
    assert len(row_estimate_calls) == 1
    row_estimate_sql, row_estimate_params = row_estimate_calls[0]
    assert "IN (" in row_estimate_sql
    assert row_estimate_params is not None


def test_get_schema_deep_dataset_size_query_failure_does_not_break_the_rest_of_the_fetch():
    """The dataset-size query is best-effort (try/except-wrapped) - a
    failure there must not take down the rest of the deep fetch, and must
    simply produce no "Estimated dataset size:" line rather than a partial
    or garbled one."""
    schema_responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", None),
            ("orders", "status", "varchar", "NO", None),
        ],
        views=[("v", "select 1 from orders")],
        routines=[("get_total", "p1 int", "int", "SELECT 1;")],
        table_meta=[("orders", None, 500)],
    )
    responses = schema_responses + [Exception("boom")] + _phase2_sampling_responses()
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    schema = backend.get_schema(conn)

    # Every other section is still present and complete.
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "View definitions:" in schema and "View v: select 1 from orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema

    # ...but no dataset-size line at all.
    assert "Estimated dataset size" not in schema


# --- cache_key -------------------------------------------------------------------

def test_cache_key_parses_user_host_port_and_database():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb"})
    assert key == "alice@host:3306/mydb"
    assert "secret" not in key


def test_cache_key_differs_across_hosts_with_same_user_and_database():
    # Regression coverage for the real bug this fixes: two entirely
    # different MySQL servers can easily share both a username and a
    # database name - the old host-blind derivation ("alice@mydb" for
    # both) would collide them onto the same schema_cache.py entry,
    # silently serving one server's schema back for the other's
    # /api/translate calls.
    backend = MySQLBackend()
    key_a = backend.cache_key({"url": "mysql://alice:secret@server-a.example.com:3306/mydb"})
    key_b = backend.cache_key({"url": "mysql://alice:secret@server-b.example.com:3306/mydb"})
    assert key_a != key_b


def test_cache_key_differs_across_ports_on_the_same_host():
    backend = MySQLBackend()
    key_3306 = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb"})
    key_3307 = backend.cache_key({"url": "mysql://alice:secret@host:3307/mydb"})
    assert key_3306 != key_3307


def test_cache_key_defaults_port_to_3306_when_omitted():
    backend = MySQLBackend()
    key_explicit = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb"})
    key_omitted = backend.cache_key({"url": "mysql://alice:secret@host/mydb"})
    assert key_explicit == key_omitted == "alice@host:3306/mydb"


def test_cache_key_percent_decodes_credentials():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://ali%40ce:secret@host:3306/mydb"})
    assert key == "ali@ce@host:3306/mydb"


def test_cache_key_strips_query_string_from_database():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb?ssl=true"})
    assert key == "alice@host:3306/mydb"


def test_cache_key_handles_missing_url():
    backend = MySQLBackend()
    assert backend.cache_key({}) == "unknown@unknown"
    assert backend.cache_key(None) == "unknown@unknown"


# --- connect ---------------------------------------------------------------------

def test_connect_parses_url_into_pymysql_kwargs(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3307/mydb"})
    assert len(harness.calls) == 1
    kwargs = harness.calls[0]
    assert kwargs["host"] == "dbhost"
    assert kwargs["port"] == 3307
    assert kwargs["user"] == "alice"
    assert kwargs["password"] == "secret"
    assert kwargs["database"] == "mydb"
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - tied to
    # the same shared knob every other dialect uses, rather than left to
    # PyMySQL's own (coincidentally identical) built-in default.
    assert kwargs["connect_timeout"] == DB_CONNECT_TIMEOUT_SECONDS


def test_connect_percent_decodes_username_and_password(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://ali%40ce:pa%23ss@host:3306/db"})
    kwargs = harness.calls[0]
    assert kwargs["user"] == "ali@ce"
    assert kwargs["password"] == "pa#ss"


def test_connect_defaults_port_when_absent(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@host/mydb"})
    assert harness.calls[0]["port"] == 3306


# --- Cloud SQL unix-socket connections ------------------------------------------
# Regression coverage for a real bug: a Cloud SQL preset URL of the form
# mysql://user:pass@/dbname?unix_socket=/cloudsql/<connection_name> (the
# same convention GCP's own docs/SQLAlchemy examples use for a PyMySQL
# connection string) has no real host - the original version of connect()
# ignored the query string entirely and fell back to "localhost", which on
# Cloud Run fails with "Can't connect to MySQL server on 'localhost'
# ([Errno 111] Connection refused)" since there's no local MySQL and no
# TCP path to the Cloud SQL instance at all, only the socket mount.

def test_connect_uses_unix_socket_when_present_in_query_string(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance",
    })
    assert len(harness.calls) == 1
    kwargs = harness.calls[0]
    assert kwargs["unix_socket"] == "/cloudsql/proj:us-east1:instance"
    assert kwargs["user"] == "trial"
    assert kwargs["password"] == "FooBar"
    assert kwargs["database"] == "classicmodels"
    # No host/port sent alongside unix_socket - see connect()'s comment for
    # why (avoid any ambiguity about which one PyMySQL actually uses).
    assert "host" not in kwargs
    assert "port" not in kwargs


def test_connect_omits_unix_socket_kwarg_for_an_ordinary_tcp_url(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3306/mydb"})
    assert "unix_socket" not in harness.calls[0]
    assert harness.calls[0]["host"] == "dbhost"


# --- connect(): sslmode / ca_cert_pem ---------------------------------------
# Coverage for the "verify-ca"/"verify-full" CA-certificate support added to
# connect() - mirrors backends/postgres.py's equivalent tests, adapted for
# PyMySQL's very different TLS API (an ssl.SSLContext object, not a
# filesystem-path kwarg libpq parses itself - see module docstring).

def test_connect_with_no_sslmode_adds_no_ssl_kwarg_at_all(monkeypatch):
    """Regression guard: a descriptor with no sslmode in the URL at all
    (the overwhelming common case, and every existing connection prior to
    this feature) must produce byte-identical connect() behavior - no
    "ssl" kwarg, nothing extra. See module docstring for why this does NOT
    mean "no TLS ever" - PyMySQL's own default (no ssl_* kwargs) already
    opportunistically attempts TLS - this test just confirms connect()
    itself isn't adding anything on top of that default."""
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3306/mydb"})
    assert "ssl" not in harness.calls[0]


def test_connect_sslmode_disable_adds_no_ssl_kwarg(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=disable"})
    assert "ssl" not in harness.calls[0]


def test_connect_sslmode_require_encrypts_without_verifying(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=require"})
    ctx = harness.calls[0]["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_connect_sslmode_require_needs_no_ca_cert(monkeypatch):
    # require is encrypt-only - no ca_cert_pem needed or used, unlike
    # verify-ca/verify-full below.
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=require",
    })
    assert isinstance(harness.calls[0]["ssl"], ssl.SSLContext)


def test_connect_sslmode_verify_ca_checks_cert_but_not_hostname(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=verify-ca",
        "ca_cert_pem": make_self_signed_ca_cert_pem(),
    })
    ctx = harness.calls[0]["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


def test_connect_sslmode_verify_full_checks_cert_and_hostname(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=verify-full",
        "ca_cert_pem": make_self_signed_ca_cert_pem(),
    })
    ctx = harness.calls[0]["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_connect_verify_full_without_ca_cert_falls_back_to_system_trust_store(monkeypatch):
    # No ca_cert_pem supplied at all - must not raise, and must still turn
    # on cert+hostname verification (against whatever this machine's own
    # system trust store contains), same as backends/postgres.py's
    # verify-full-with-no-sslrootcert behavior.
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=verify-full",
    })
    ctx = harness.calls[0]["ssl"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_connect_ca_cert_pem_tempfile_is_deleted_after_connect(monkeypatch):
    """The tempfile connect() writes ca_cert_pem to must not linger on disk
    once connect() has returned - only needed for SSLContext construction,
    not for the life of the connection."""
    import backends.mysql as mysqlmod

    written_paths = []
    real_materialize = mysqlmod.materialize_ca_cert_tempfile

    def _spy(pem_text):
        path = real_materialize(pem_text)
        written_paths.append(path)
        return path

    monkeypatch.setattr(mysqlmod, "materialize_ca_cert_tempfile", _spy)
    install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://alice:secret@dbhost:3306/mydb?sslmode=verify-full",
        "ca_cert_pem": make_self_signed_ca_cert_pem(),
    })
    assert len(written_paths) == 1
    assert not os.path.exists(written_paths[0])


def test_connect_unix_socket_ignores_sslmode_entirely(monkeypatch):
    """TLS is a TCP-only concept in the MySQL wire protocol (same as
    Postgres - see module docstring): a unix_socket connection must never
    get an "ssl" kwarg at all, even if sslmode is also present in the same
    URL (a user combining the two doesn't silently get half-applied TLS
    settings)."""
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance&sslmode=verify-full",
        "ca_cert_pem": make_self_signed_ca_cert_pem(),
    })
    assert harness.calls[0]["unix_socket"] == "/cloudsql/proj:us-east1:instance"
    assert "ssl" not in harness.calls[0]


def test_cache_key_works_for_a_unix_socket_url():
    backend = MySQLBackend()
    key = backend.cache_key({
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance",
    })
    assert key == "trial@/cloudsql/proj:us-east1:instance/classicmodels"
    assert "FooBar" not in key


def test_cache_key_differs_across_unix_sockets_with_same_user_and_database():
    # Regression coverage: _parse_mysql_url() defaults "host" to the
    # meaningless "localhost" for a socket connection (see its own
    # docstring), so a host:port-based key (even a *correctly* host-aware
    # one) would still collide two different Cloud SQL instances - only
    # reachable via two different unix_socket paths - onto the same
    # "user@localhost:3306/db" key.
    backend = MySQLBackend()
    key_a = backend.cache_key({
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance-a",
    })
    key_b = backend.cache_key({
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance-b",
    })
    assert key_a != key_b


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_db_and_user():
    conn, cursor = make_fake_mysql_connection([([("mydb", "alice")], None, -1)])
    backend = MySQLBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "mydb"
    assert username == "alice"


# --- execute -------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [
        ([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2),
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert len(results) == 1
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2
    # PyMySQL's autocommit is a *method*, not a settable attribute (unlike
    # psycopg2's) - see helpers.FakeMySQLConnection's docstring.
    assert conn.autocommit_calls == [True]


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]  # no description -> DML path
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = 1;")
    assert results[0]["columns"] is None
    assert results[0]["rows"] is None
    assert results[0]["rowCount"] == 3


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [
        ([], None, 1),
        ([(1,)], [("id",)], 1),
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "UPDATE users SET x=1; SELECT id FROM users;")
    assert len(results) == 2
    assert results[0]["rowCount"] == 1
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    responses = [
        ([], None, 1),  # statement 1 succeeds
        RuntimeError("You have an error in your SQL syntax"),  # statement 2 fails
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE users SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.results[0]["rowCount"] == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "You have an error in your SQL syntax" in str(err)


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_ignores_blank_statements_between_semicolons():
    responses = [([], None, 0)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT 1;;;")
    assert len(results) == 1


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale - this just proves MySQLBackend routes through the same shared
# fetch_capped_rows() (backends/base.py) instead of its own fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]


# --- dialect_name ----------------------------------------------------------------

def test_dialect_name_is_mysql():
    assert MySQLBackend().dialect_name == "MySQL"
