"""
backends/redshift.py, driven two ways:
  - connect(): against the fake psycopg2.connect harness
    (helpers.install_fake_redshift_connect) - verifies required-field
    validation, the default port, sslmode="require" always being passed
    (never opt-in the way Oracle's "ssl" flag is), and the SET search_path
    call a "schema" descriptor field triggers, without opening a real
    connection.
  - get_schema()/execute()/identity_label()/cache_key(): against the same
    fake psycopg2-shaped cursor/connection tests/test_postgres_backend.py
    uses (helpers.make_fake_pg_connection) - RedshiftBackend talks the
    exact same psycopg2 DB-API shape backends/postgres.py does, so no
    Redshift-specific fake is needed for these.

get_schema()'s query order is unconditional for tables/columns and views,
then best-effort (try/except) for constraints and distribution/sort keys -
see backends/redshift.py:
  1. table names   2. columns   3. constraints (best-effort)
  4. distkey/sortkey (best-effort, via svv_table_info)   5. views
No indexes/grants/triggers queries at all - Redshift has no index or
trigger concept, and grants support is left for follow-up (see module
docstring).
"""

import sys
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.redshift import RedshiftBackend
from helpers import install_fake_redshift_connect, make_fake_pg_connection


def _rs(monkeypatch):
    harness = install_fake_redshift_connect(monkeypatch)
    return RedshiftBackend(), harness


def _schema_responses(table_names, columns_rows, constraints=(), layout=(), views=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(layout), None, -1),
        (list(views), None, -1),
    ]


# --- liveness_sql / dialect_name ---------------------------------------------

def test_liveness_sql_is_the_base_class_default():
    # Unlike Oracle (ORA-00923: no SELECT-without-FROM form), Redshift is
    # Postgres-derived and supports a bare "SELECT 1" - no override needed.
    assert RedshiftBackend.liveness_sql == "SELECT 1"


def test_dialect_name_is_amazon_redshift_sql():
    assert RedshiftBackend.dialect_name == "Amazon Redshift SQL"


# --- connect(): required fields, defaults, always-on TLS --------------------

def test_connect_passes_core_kwargs_and_requires_sslmode(monkeypatch):
    backend, harness = _rs(monkeypatch)
    backend.connect({
        "type": "redshift", "host": "my-cluster.abc123.us-east-1.redshift.amazonaws.com",
        "port": 5439, "database": "dev", "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert call["host"] == "my-cluster.abc123.us-east-1.redshift.amazonaws.com"
    assert call["port"] == 5439
    assert call["dbname"] == "dev"
    assert call["user"] == "alice"
    assert call["password"] == "hunter2"
    # Always required, never opt-in (unlike Oracle's "ssl" descriptor flag) -
    # see the module docstring.
    assert call["sslmode"] == "require"


def test_connect_defaults_port_to_5439_when_omitted(monkeypatch):
    backend, harness = _rs(monkeypatch)
    backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
    })
    assert harness.calls[-1]["port"] == 5439


def test_connect_sets_autocommit_true(monkeypatch):
    backend, harness = _rs(monkeypatch)
    connection = backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
    })
    assert connection.autocommit is True


def test_connect_raises_when_host_missing(monkeypatch):
    backend, harness = _rs(monkeypatch)
    try:
        backend.connect({"type": "redshift", "database": "dev", "user": "alice", "password": "x"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "host" in str(e)


def test_connect_raises_when_database_missing(monkeypatch):
    backend, harness = _rs(monkeypatch)
    try:
        backend.connect({"type": "redshift", "host": "h", "user": "alice", "password": "x"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "database" in str(e)


def test_connect_raises_when_user_or_password_missing(monkeypatch):
    backend, harness = _rs(monkeypatch)
    try:
        backend.connect({"type": "redshift", "host": "h", "database": "dev", "user": "alice"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "user and password" in str(e)


def test_connect_with_schema_issues_set_search_path(monkeypatch):
    backend, harness = _rs(monkeypatch)
    connection = backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
        "schema": "sales",
    })
    calls = connection.cursor_calls
    assert len(calls) == 1
    sql_text, params = calls[0]
    # sql.SQL(...).format(sql.Identifier(...)) produces a Composed object,
    # not a plain string - stringify it (psycopg2's Composed supports str())
    # to check the identifier landed correctly, quoted.
    assert "SET search_path TO" in str(sql_text)
    assert "sales" in str(sql_text)


def test_connect_without_schema_issues_no_set_search_path(monkeypatch):
    backend, harness = _rs(monkeypatch)
    connection = backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
    })
    assert connection.cursor_calls == []


# --- close() -------------------------------------------------------------------

def test_close_calls_connection_close(monkeypatch):
    backend, harness = _rs(monkeypatch)
    connection = backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
    })
    backend.close(connection)
    assert connection.closed is True


def test_close_tolerates_none():
    RedshiftBackend().close(None)  # must not raise


# --- cache_key() -----------------------------------------------------------

def test_cache_key_format():
    backend = RedshiftBackend()
    key = backend.cache_key({"host": "h", "port": 5439, "database": "dev", "schema": "sales"})
    assert key == "h:5439/dev.sales"


def test_cache_key_defaults_schema_to_public():
    backend = RedshiftBackend()
    key = backend.cache_key({"host": "h", "port": 5439, "database": "dev"})
    assert key == "h:5439/dev.public"


def test_cache_key_never_includes_password():
    backend = RedshiftBackend()
    key = backend.cache_key({"host": "h", "port": 5439, "database": "dev", "password": "hunter2"})
    assert "hunter2" not in key


# --- identity_label() -------------------------------------------------------

def test_identity_label_reads_current_database_and_user():
    conn, cursor = make_fake_pg_connection([([("dev", "alice")], None, -1)])
    backend = RedshiftBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "dev"
    assert username == "alice"


# --- get_schema() ------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = RedshiftBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_tables_with_columns():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "integer", "NO", None),
            ("customers", "name", "character varying", "YES", None),
        ],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id integer NOT NULL" in schema
    assert "name character varying NULL" in schema


def test_get_schema_constraints_are_labeled_as_not_enforced():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        constraints=[("orders", "orders_pkey", "PRIMARY KEY", "id", None, None)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "never enforces these at write time" in schema
    assert "[orders] orders_pkey (PRIMARY KEY): id" in schema


def test_get_schema_constraints_query_errors_are_swallowed():
    # get_db_connections-style best-effort: a catalog-access error on the
    # constraints query degrades to "skip this section", not a failed
    # schema fetch - matches backends/oracle.py's own precedent.
    class ExplodingCursor:
        def __init__(self):
            self._n = 0
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            self._n += 1
            if self._n == 3:
                raise RuntimeError("permission denied")
        def fetchall(self):
            if self._n == 1:
                return [("t",)]
            if self._n == 2:
                return [("t", "id", "integer", "NO", None)]
            return []

    class ExplodingConnection:
        def cursor(self):
            return ExplodingCursor()

    backend = RedshiftBackend()
    schema = backend.get_schema(ExplodingConnection())
    assert "Table: t" in schema
    assert "never enforces" not in schema  # constraints section skipped


def test_get_schema_distribution_sort_keys_section():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        layout=[("orders", "KEY(customer_id)", "order_date")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "Distribution/Sort Keys" in schema
    assert "no index concept" in schema
    assert "[orders] DISTSTYLE KEY(customer_id), SORTKEY(order_date)" in schema


def test_get_schema_has_no_indexes_or_triggers_sections():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t"],
        columns_rows=[("t", "id", "integer", "NO", None)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "Indexes:" not in schema
    assert "Triggers:" not in schema


def test_get_schema_views_section():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "integer", "NO", None)],
        views=[("customer_orders", "SELECT * FROM orders JOIN customers ...")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "View customer_orders" in schema


def test_get_schema_scopes_to_current_schema_not_hardcoded_public():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t"],
        columns_rows=[("t", "id", "integer", "NO", None)],
    ))
    backend = RedshiftBackend()
    backend.get_schema(conn)
    first_query = cursor.calls[0][0]
    assert "current_schema()" in first_query
    assert "'public'" not in first_query


# --- execute() ---------------------------------------------------------------

def test_execute_returns_rows_and_columns():
    conn, cursor = make_fake_pg_connection([
        ([(1, "Alice")], [("id",), ("name",)], -1),
    ])
    backend = RedshiftBackend()
    results = backend.execute(conn, "SELECT id, name FROM customers;")
    assert len(results) == 1
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}]
    assert conn.autocommit is True


def test_execute_converts_decimal_and_date_values():
    conn, cursor = make_fake_pg_connection([
        ([(Decimal("9.99"), date(2024, 1, 15))], [("price",), ("d",)], -1),
    ])
    backend = RedshiftBackend()
    results = backend.execute(conn, "SELECT price, d FROM t;")
    row = results[0]["rows"][0]
    assert row["price"] == 9.99
    assert row["d"] == "2024-01-15"


def test_execute_runs_multiple_statements():
    conn, cursor = make_fake_pg_connection([
        (None, None, 1),
        (None, None, 2),
    ])
    backend = RedshiftBackend()
    results = backend.execute(conn, "UPDATE t SET x=1; UPDATE t SET y=2;")
    assert len(results) == 2
    assert results[0]["rowCount"] == 1
    assert results[1]["rowCount"] == 2
