"""
backends/snowflake.py, driven two ways:
  - connect(): against the fake snowflake.connector.connect harness
    (helpers.install_fake_snowflake_connect) - verifies the
    password-vs-key-pair kwarg dispatch without opening a real connection.
  - get_schema()/execute()/identity_label()/cache_key(): against the same
    fake psycopg2-shaped cursor/connection tests/test_postgres_backend.py
    uses (helpers.make_fake_pg_connection) - snowflake-connector-python
    implements the same PEP 249 DB-API cursor shape, so no Snowflake-
    specific fake is needed for these.

get_schema()'s query order is unconditional for tables/columns, then
best-effort (try/except) for constraints/views - see backends/snowflake.py:
  1. table names   2. columns   3. constraints (best-effort)   4. views (best-effort)
No indexes/grants/triggers queries at all (Snowflake has no user-managed
indexes or triggers - see that module's docstring).
"""

import sys
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.snowflake import SnowflakeBackend
from helpers import install_fake_snowflake_connect, make_fake_pg_connection


def _sf(monkeypatch):
    harness = install_fake_snowflake_connect(monkeypatch)
    return SnowflakeBackend(), harness


def _schema_responses(table_names, columns_rows, constraints=(), views=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
    ]


# --- connect(): password vs key-pair dispatch -----------------------------

def test_connect_password_auth_passes_password_no_authenticator_override(monkeypatch):
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert call["password"] == "hunter2"
    assert "authenticator" not in call
    assert "private_key" not in call


def test_connect_key_pair_auth_sets_jwt_authenticator(monkeypatch):
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db", "private_key": "-----BEGIN PRIVATE KEY-----...",
    })
    call = harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert call["private_key"] == "-----BEGIN PRIVATE KEY-----..."
    assert "password" not in call


def test_connect_key_pair_auth_includes_passphrase_when_given(monkeypatch):
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db",
        "private_key": "pem", "private_key_passphrase": "shh",
    })
    assert harness.calls[-1]["private_key_passphrase"] == "shh"


def test_connect_key_pair_wins_when_both_credentials_somehow_present(monkeypatch):
    # Shouldn't happen given config_routes.py's validation, but connect()
    # itself should still resolve deterministically rather than depend on
    # dict key iteration order.
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db",
        "password": "hunter2", "private_key": "pem",
    })
    call = harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert "password" not in call


def test_connect_raises_when_neither_credential_given(monkeypatch):
    backend, harness = _sf(monkeypatch)
    try:
        backend.connect({
            "type": "snowflake", "account": "acc1", "user": "alice",
            "warehouse": "wh", "database": "db",
        })
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_passes_optional_schema_and_role_when_given(monkeypatch):
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db", "schema": "public", "role": "analyst",
        "password": "x",
    })
    call = harness.calls[-1]
    assert call["schema"] == "public"
    assert call["role"] == "analyst"


def test_connect_omits_schema_and_role_when_not_given(monkeypatch):
    backend, harness = _sf(monkeypatch)
    backend.connect({
        "type": "snowflake", "account": "acc1", "user": "alice",
        "warehouse": "wh", "database": "db", "password": "x",
    })
    call = harness.calls[-1]
    assert "schema" not in call
    assert "role" not in call


# --- cache_key -------------------------------------------------------------

def test_cache_key_is_account_slash_database_dot_schema():
    backend = SnowflakeBackend()
    key = backend.cache_key({"account": "acc1", "database": "db", "schema": "public"})
    assert key == "acc1/db.public"


def test_cache_key_handles_missing_fields():
    backend = SnowflakeBackend()
    assert backend.cache_key({}) == "unknown/unknown.unknown"


def test_cache_key_never_includes_credentials():
    backend = SnowflakeBackend()
    key = backend.cache_key({
        "account": "acc1", "database": "db", "schema": "public",
        "password": "hunter2", "private_key": "-----BEGIN PRIVATE KEY-----secret",
    })
    assert "hunter2" not in key
    assert "secret" not in key


# --- identity_label ----------------------------------------------------------

def test_identity_label_returns_db_and_user():
    conn, cursor = make_fake_pg_connection([([("MYDB", "ALICE")], None, -1)])
    backend = SnowflakeBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "MYDB"
    assert username == "ALICE"


# --- get_schema --------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = SnowflakeBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_table_with_columns():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS"],
        columns_rows=[
            ("CUSTOMERS", "ID", "NUMBER", "NO"),
            ("CUSTOMERS", "NAME", "TEXT", "YES"),
        ],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Table: CUSTOMERS" in schema
    assert "ID NUMBER NOT NULL" in schema
    assert "NAME TEXT NULL" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"EVENTS_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Table family: EVENTS_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert "Table: EVENTS_20240102" not in schema


def test_get_schema_views_section_is_not_scoped_to_kept_names_regression():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS"],
        columns_rows=[("CUSTOMERS", "ID", "NUMBER", "NO")],
        views=[("CUSTOMER_ORDERS", "SELECT * FROM ORDERS JOIN CUSTOMERS ...")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "CUSTOMER_ORDERS" in schema


def test_get_schema_includes_constraints_section():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        constraints=[("ORDERS", "ORDERS_PK", "PRIMARY KEY", "ID")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema
    assert "ORDERS_PK" in schema


def test_get_schema_survives_constraints_query_failure():
    # Best-effort: some roles/accounts may lack visibility into
    # KEY_COLUMN_USAGE - that must degrade to "skip this section", not
    # fail the whole schema fetch (mirrors backends/bigquery.py's same
    # try/except around its constraints query).
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
                raise Exception("permission denied on KEY_COLUMN_USAGE")

        def fetchall(self):
            if "information_schema.tables" in self.calls[-1][0]:
                return [("ORDERS",)]
            if "information_schema.columns" in self.calls[-1][0]:
                return [("ORDERS", "ID", "NUMBER", "NO")]
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = SnowflakeBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: ORDERS" in schema
    assert "Constraints:" not in schema


def test_get_schema_scopes_columns_query_with_individually_bound_placeholders_not_string_formatting():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["T1", "T2"],
        columns_rows=[("T1", "ID", "NUMBER", "NO"), ("T2", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    backend.get_schema(conn)

    columns_sql, columns_params = cursor.calls[1]
    assert "information_schema.columns" in columns_sql
    assert "T1" not in columns_sql  # never string-formatted directly into SQL
    assert "T2" not in columns_sql
    assert set(columns_params) == {"T1", "T2"}


def test_get_schema_uses_current_schema_not_a_hardcoded_name():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["T1"],
        columns_rows=[("T1", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "CURRENT_SCHEMA()" in table_names_sql
    assert "'public'" not in table_names_sql  # not hardcoded to Postgres's default


# --- execute -------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = true;")
    assert results[0]["columns"] is None
    assert results[0]["rowCount"] == 3


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [([], None, 1), ([(1,)], [("id",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "UPDATE t SET x=1; SELECT id FROM t;")
    assert len(results) == 2
    assert results[1]["rows"] == [{"id": 1}]
