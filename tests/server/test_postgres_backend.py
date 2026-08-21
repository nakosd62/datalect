"""
backends/postgres.py, driven entirely against a fake psycopg2-shaped
connection/cursor (see helpers.make_fake_pg_connection) - no real Postgres
needed. get_schema()'s query order is unconditional (unlike BigQuery's
try/except-guarded optional sections), so responses are queued in the
exact order PostgresBackend.get_schema() issues them:
  1. table names   2. columns   3. constraints   4. indexes
  5. views         6. grants    7. triggers
"""

import sys
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.postgres import PostgresBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS
from helpers import make_fake_pg_connection, install_fake_postgres_connect


def _schema_responses(table_names, columns_rows, constraints=(), indexes=(), views=(), grants=(), triggers=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(indexes), None, -1),
        (list(views), None, -1),
        (list(grants), None, -1),
        (list(triggers), None, -1),
    ]


# --- connect(): DSN + connect_timeout ---------------------------------------
# Regression coverage for the failure mode surfaced by live Redshift
# Serverless troubleshooting: connect() used to pass no timeout at all, so a
# wrong/unreachable host would hang for however long the OS's own TCP
# connect timeout happens to be (effectively unbounded), rather than failing
# fast - see backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring.

def test_connect_passes_url_as_dsn_and_sets_connect_timeout(monkeypatch):
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({"type": "postgres", "url": "postgresql://alice:secret@host:5432/mydb"})
    assert len(harness.calls) == 1
    dsn, kwargs = harness.calls[0]
    assert dsn == "postgresql://alice:secret@host:5432/mydb"
    assert kwargs["connect_timeout"] == DB_CONNECT_TIMEOUT_SECONDS


def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = PostgresBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_tables_with_columns():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "integer", "NO", None),
            ("customers", "name", "text", "YES", None),
        ],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id integer NOT NULL" in schema
    assert "name text NULL" in schema


def test_get_schema_includes_column_default():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "text", "NO", "'pending'::text")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "DEFAULT 'pending'::text" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "integer", "NO", None)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Table family: events_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert f"{members[0]} .. {members[-1]}" in schema
    # Individual shard members must not appear as their own "Table:" heading.
    assert "Table: events_20240102" not in schema


def test_get_schema_views_section_is_not_scoped_to_kept_names_regression():
    # Regression test: views were once (incorrectly) scoped to kept_names,
    # which only ever contains BASE TABLE names - a view could never
    # appear there, so the Views section always came back empty under the
    # bug. This view intentionally shares no name with any base table.
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "integer", "NO", None)],
        views=[("customer_orders", "SELECT * FROM orders JOIN customers ...")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_indexes_grants_triggers():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        constraints=[("orders", "orders_pkey", "PRIMARY KEY", "id", None, None)],
        indexes=[("orders", "orders_pkey", "CREATE UNIQUE INDEX orders_pkey ON orders(id)")],
        grants=[("app_user", "orders", "SELECT")],
        triggers=[("orders", "trg_audit", "INSERT", "EXECUTE FUNCTION audit()")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema and "orders_pkey" in schema
    assert "Indexes:" in schema and "CREATE UNIQUE INDEX" in schema
    assert "Grants:" in schema and "Grant SELECT on orders to app_user" in schema
    assert "Triggers:" in schema and "trg_audit" in schema


def test_get_schema_foreign_key_constraint_format():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "integer", "NO", None)],
        constraints=[("orders", "orders_customer_fk", "FOREIGN KEY", "customer_id", "customers", "id")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "customer_id -> customers(id)" in schema


def test_get_schema_scan_query_uses_configured_scan_cap():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "integer", "NO", None)],
    ))
    backend = PostgresBackend()
    backend.get_schema(conn)
    first_sql, first_params = cursor.calls[0]
    assert "information_schema.columns" in first_sql
    assert first_params[0] > 0  # SCHEMA_MAX_TABLE_NAMES_SCANNED


# --- cache_key ---------------------------------------------------------------

def test_cache_key_parses_username_and_dbname():
    backend = PostgresBackend()
    key = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    assert key == "alice@mydb"
    assert "secret" not in key


def test_cache_key_strips_query_string_from_dbname():
    backend = PostgresBackend()
    key = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb?sslmode=require"})
    assert key == "alice@mydb"


def test_cache_key_handles_missing_url():
    backend = PostgresBackend()
    assert backend.cache_key({}) == "unknown@unknown"
    assert backend.cache_key(None) == "unknown@unknown"


def test_cache_key_handles_unparseable_url():
    backend = PostgresBackend()
    # urlparse doesn't actually raise on most garbage, but this exercises
    # the except-Exception fallback path defensively.
    key = backend.cache_key({"url": None})
    assert key == "unknown@unknown"


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_db_and_user():
    conn, cursor = make_fake_pg_connection([([("mydb", "alice")], None, -1)])
    backend = PostgresBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "mydb"
    assert username == "alice"


# --- execute -------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [
        ([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2),
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert len(results) == 1
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2
    assert conn.autocommit is True


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]  # no description -> DML path
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = true;")
    assert results[0]["columns"] is None
    assert results[0]["rows"] is None
    assert results[0]["rowCount"] == 3


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [
        ([], None, 1),
        ([(1,)], [("id",)], 1),
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "UPDATE users SET x=1; SELECT id FROM users;")
    assert len(results) == 2
    assert results[0]["rowCount"] == 1
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_ignores_blank_statements_between_semicolons():
    responses = [([], None, 0)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT 1;;;")
    # sqlparse.split + the blank-statement guard should collapse the
    # trailing empty statements down to just the one real query.
    assert len(results) == 1
