"""
backends/databricks.py, driven two ways:
  - connect(): against the fake databricks.sql.connect harness
    (helpers.install_fake_databricks_connect) - verifies the access_token
    requirement and the catalog/schema kwarg dispatch without opening a
    real connection.
  - get_schema()/execute()/identity_label()/cache_key(): against the same
    fake psycopg2-shaped cursor/connection tests/test_postgres_backend.py
    uses (helpers.make_fake_pg_connection) - databricks-sql-connector
    implements the same PEP 249 DB-API cursor shape, so no Databricks-
    specific fake is needed for these.

get_schema()'s query order is unconditional for tables/columns, then
best-effort (try/except) for constraints/views - see backends/databricks.py:
  1. table names   2. columns   3. constraints (best-effort)   4. views (best-effort)
No indexes/grants/triggers queries at all (Databricks has no user-managed
indexes or triggers - see that module's docstring).

Unlike Postgres/MySQL/Snowflake's connectors, databricks-sql-connector's
declared DB-API paramstyle is "named" (:name, not %s/pyformat) - the
dynamic IN (...) clause tests below check for that shape specifically
(a dict of params, :t0/:t1-style placeholders in the SQL text) rather than
reusing Snowflake's %s-array-style assertions.
"""

import sys
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.databricks import DatabricksBackend
from helpers import install_fake_databricks_connect, make_fake_pg_connection


def _dbx(monkeypatch):
    harness = install_fake_databricks_connect(monkeypatch)
    return DatabricksBackend(), harness


def _schema_responses(table_names, columns_rows, constraints=(), views=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
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
                return [("orders",)]
            if "information_schema.columns" in self.calls[-1][0]:
                return [("orders", "id", "int", "NO")]
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
