"""
backends/oracle.py, driven two ways:
  - connect(): against the fake oracledb.connect harness
    (helpers.install_fake_oracle_connect) - verifies the service_name-vs-sid
    kwarg dispatch, required-field validation, and the ALTER SESSION SET
    CURRENT_SCHEMA call a "schema" descriptor field triggers, without
    opening a real connection.
  - get_schema()/execute()/identity_label()/cache_key(): against the same
    fake psycopg2-shaped cursor/connection tests/test_postgres_backend.py
    uses (helpers.make_fake_pg_connection) - python-oracledb implements the
    same PEP 249 DB-API cursor shape, so no Oracle-specific fake is needed
    for these.

get_schema()'s query order is unconditional for tables/columns, then
best-effort (try/except) for constraints/views - see backends/oracle.py:
  1. table names   2. columns   3. constraints (best-effort)   4. views (best-effort)
No indexes/grants/triggers queries at all (left for follow-up, same status
backends/snowflake.py's/backends/databricks.py's own gaps have).

Unlike Postgres/MySQL/Snowflake/Databricks' connectors, python-oracledb's
declared DB-API paramstyle is "named" (:name, same as Databricks) - the
dynamic IN (...) clause tests below check for that shape specifically.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.oracle import OracleBackend, _set_current_schema, _IDENTIFIER_RE
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import install_fake_oracle_connect, make_fake_pg_connection


def _ora(monkeypatch):
    harness = install_fake_oracle_connect(monkeypatch)
    return OracleBackend(), harness


def _schema_responses(table_names, columns_rows, constraints=(), views=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
    ]


# --- liveness_sql ------------------------------------------------------------
# Regression coverage for the real-world bug this fixes: the app's
# connection-status "ping" used to be a hardcoded "SELECT 1;" - valid ANSI
# SQL, but Oracle has no SELECT-without-FROM form (ORA-00923), so a
# perfectly working Oracle connection always showed as disconnected. See
# backends/base.py's Backend.liveness_sql and execute_routes.py's
# /api/ping, which now asks the resolved backend for this instead of the
# client guessing a query string.

def test_liveness_sql_uses_select_1_from_dual_not_bare_select_1():
    assert OracleBackend.liveness_sql == "SELECT 1 FROM DUAL"


# --- connect(): required fields + service_name-vs-sid dispatch -------------

def test_connect_passes_service_name_and_core_kwargs(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "port": 1521,
        "service_name": "ORCLPDB1", "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert call["host"] == "db.example.com"
    assert call["port"] == 1521
    assert call["service_name"] == "ORCLPDB1"
    assert call["user"] == "alice"
    assert call["password"] == "hunter2"
    assert "sid" not in call
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - a wrong/
    # unreachable host must fail fast rather than hang indefinitely.
    assert call["tcp_connect_timeout"] == float(DB_CONNECT_TIMEOUT_SECONDS)


def test_connect_uses_sid_when_service_name_not_given(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "sid": "XE",
        "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert call["sid"] == "XE"
    assert "service_name" not in call


def test_connect_service_name_wins_when_both_service_name_and_sid_given(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1", "sid": "XE",
        "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert call["service_name"] == "ORCLPDB1"
    assert "sid" not in call


def test_connect_defaults_port_to_1521_when_omitted(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2",
    })
    assert harness.calls[-1]["port"] == 1521


# --- connect(): "ssl" descriptor field -> TLS kwargs ------------------------
# Regression coverage for the real-world bug this flag fixes: connect()
# defaulted to plain TCP unconditionally, which against an Oracle Cloud/
# Autonomous Database TLS-only listener doesn't surface as a normal auth
# error - the TCP connection itself gets reset the moment the (non-TLS)
# initial packet goes out, surfacing as oracledb's DPY-4011/DPY-6005. See
# backends/oracle.py's module docstring.

def test_connect_without_ssl_flag_passes_no_tls_kwargs(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert "protocol" not in call
    assert "ssl_server_dn_match" not in call


def test_connect_with_ssl_true_passes_tcps_protocol_and_dn_match(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "adb.us-ashburn-1.oraclecloud.com", "port": 1522,
        "service_name": "myatp_high.adb.oraclecloud.com", "user": "admin",
        "password": "hunter2", "ssl": True,
    })
    call = harness.calls[-1]
    assert call["protocol"] == "tcps"
    assert call["ssl_server_dn_match"] is True


def test_connect_with_ssl_false_passes_no_tls_kwargs(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2", "ssl": False,
    })
    call = harness.calls[-1]
    assert "protocol" not in call
    assert "ssl_server_dn_match" not in call


def test_connect_raises_when_no_host_given(monkeypatch):
    backend, harness = _ora(monkeypatch)
    try:
        backend.connect({
            "type": "oracle", "service_name": "ORCLPDB1", "user": "alice", "password": "x",
        })
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_raises_when_neither_service_name_nor_sid_given(monkeypatch):
    backend, harness = _ora(monkeypatch)
    try:
        backend.connect({"type": "oracle", "host": "db.example.com", "user": "alice", "password": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_raises_when_user_or_password_missing(monkeypatch):
    backend, harness = _ora(monkeypatch)
    try:
        backend.connect({"type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1", "user": "alice"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


# --- connect(): schema override via ALTER SESSION ---------------------------

def test_connect_with_schema_issues_alter_session_set_current_schema(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2", "schema": "sales",
    })
    conn = harness.connections[-1]
    assert len(conn.cursor_calls) == 1
    sql, params = conn.cursor_calls[0]
    assert "ALTER SESSION SET CURRENT_SCHEMA" in sql
    assert "SALES" in sql  # uppercased - see _set_current_schema's docstring
    assert params is None


def test_connect_without_schema_issues_no_alter_session(monkeypatch):
    backend, harness = _ora(monkeypatch)
    backend.connect({
        "type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2",
    })
    conn = harness.connections[-1]
    assert conn.cursor_calls == []


def test_set_current_schema_rejects_non_identifier_values():
    class DummyConn:
        def cursor(self):
            raise AssertionError("should never reach the driver for an invalid identifier")

    try:
        _set_current_schema(DummyConn(), "sales; drop table x")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_identifier_regex_accepts_plain_identifiers_rejects_the_rest():
    assert _IDENTIFIER_RE.match("sales")
    assert _IDENTIFIER_RE.match("SALES_2024")
    assert _IDENTIFIER_RE.match("a$b#c")
    assert not _IDENTIFIER_RE.match("1sales")  # can't start with a digit
    assert not _IDENTIFIER_RE.match("sales; drop table x")
    assert not _IDENTIFIER_RE.match("")


# --- cache_key ---------------------------------------------------------------

def test_cache_key_is_host_port_slash_service_dot_schema():
    backend = OracleBackend()
    key = backend.cache_key({
        "host": "db.example.com", "port": 1521, "service_name": "ORCLPDB1", "schema": "sales",
    })
    assert key == "db.example.com:1521/ORCLPDB1.sales"


def test_cache_key_falls_back_to_sid_when_no_service_name():
    backend = OracleBackend()
    key = backend.cache_key({"host": "db.example.com", "port": 1521, "sid": "XE"})
    assert key == "db.example.com:1521/XE.unknown"


def test_cache_key_handles_missing_fields():
    backend = OracleBackend()
    assert backend.cache_key({}) == "unknown:unknown/unknown.unknown"


def test_cache_key_never_includes_credentials():
    backend = OracleBackend()
    key = backend.cache_key({
        "host": "db.example.com", "port": 1521, "service_name": "ORCLPDB1",
        "password": "hunter2",
    })
    assert "hunter2" not in key


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_schema_and_user():
    conn, cursor = make_fake_pg_connection([([("SALES", "ALICE")], None, -1)])
    backend = OracleBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "SALES"
    assert username == "ALICE"
    assert "FROM DUAL" in cursor.calls[0][0]


# --- get_schema ------------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = OracleBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_table_with_columns():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS"],
        columns_rows=[
            ("CUSTOMERS", "ID", "NUMBER", "N"),
            ("CUSTOMERS", "NAME", "VARCHAR2", "Y"),
        ],
    ))
    backend = OracleBackend()
    schema = backend.get_schema(conn)
    assert "Table: CUSTOMERS" in schema
    assert "ID NUMBER NOT NULL" in schema
    assert "NAME VARCHAR2 NULL" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"EVENTS_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "ID", "NUMBER", "N")],
    ))
    backend = OracleBackend()
    schema = backend.get_schema(conn)
    assert "Table family: EVENTS_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert "Table: EVENTS_20240102" not in schema


def test_get_schema_views_section_uses_text_vc_and_is_not_scoped_to_kept_names():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS"],
        columns_rows=[("CUSTOMERS", "ID", "NUMBER", "N")],
        views=[("CUSTOMER_ORDERS", "SELECT * FROM ORDERS JOIN CUSTOMERS ...")],
    ))
    backend = OracleBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "CUSTOMER_ORDERS" in schema
    views_sql = cursor.calls[3][0]
    assert "text_vc" in views_sql


def test_get_schema_includes_constraints_section_with_readable_type_labels():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "N")],
        constraints=[("ORDERS", "ORDERS_PK", "P", "ID")],
    ))
    backend = OracleBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema
    assert "ORDERS_PK" in schema
    # Oracle's constraint_type is a single-letter code (P/U/R) - must be
    # mapped to a readable label, not shown raw, for consistency with every
    # other backend's spelled-out constraint type text (see
    # _CONSTRAINT_TYPE_LABELS).
    assert "PRIMARY KEY" in schema
    assert "(P)" not in schema


def test_get_schema_survives_constraints_query_failure():
    # Best-effort: a role without dictionary-view access on
    # ALL_CONSTRAINTS/ALL_CONS_COLUMNS must degrade to "skip this section",
    # not fail the whole schema fetch (mirrors backends/snowflake.py's/
    # backends/databricks.py's same try/except).
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "all_constraints" in sql:
                raise Exception("permission denied on ALL_CONSTRAINTS")

        def fetchall(self):
            if "all_tables" in self.calls[-1][0]:
                return [("ORDERS",)]
            if "all_tab_columns" in self.calls[-1][0]:
                return [("ORDERS", "ID", "NUMBER", "N")]
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = OracleBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: ORDERS" in schema
    assert "Constraints:" not in schema


def test_get_schema_survives_views_query_failure_on_older_oracle_versions():
    # TEXT_VC doesn't exist on every Oracle version this app might connect
    # to - a version without it must degrade to "skip this section", not
    # fail the whole schema fetch.
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "all_views" in sql:
                raise Exception("ORA-00904: TEXT_VC: invalid identifier")

        def fetchall(self):
            if "all_tables" in self.calls[-1][0]:
                return [("ORDERS",)]
            if "all_tab_columns" in self.calls[-1][0]:
                return [("ORDERS", "ID", "NUMBER", "N")]
            if "all_constraints" in self.calls[-1][0]:
                return []
            return []

    class RaisingConnection:
        def cursor(self):
            return RaisingCursor()

    backend = OracleBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: ORDERS" in schema
    assert "Views:" not in schema


def test_get_schema_scopes_columns_query_with_named_placeholders_not_string_formatting():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["TBL_A", "TBL_B"],
        columns_rows=[("TBL_A", "ID", "NUMBER", "N"), ("TBL_B", "ID", "NUMBER", "N")],
    ))
    backend = OracleBackend()
    backend.get_schema(conn)

    columns_sql, columns_params = cursor.calls[1]
    assert "all_tab_columns" in columns_sql
    assert "TBL_A" not in columns_sql  # never string-formatted directly into SQL
    assert "TBL_B" not in columns_sql
    assert isinstance(columns_params, dict)
    assert set(columns_params.values()) == {"TBL_A", "TBL_B"}
    assert ":t0" in columns_sql and ":t1" in columns_sql


def test_get_schema_uses_current_schema_context_not_a_hardcoded_owner():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["T1"],
        columns_rows=[("T1", "ID", "NUMBER", "N")],
    ))
    backend = OracleBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')" in table_names_sql
    assert "'PUBLIC'" not in table_names_sql  # not hardcoded to another dialect's default


def test_get_schema_table_name_query_excludes_mview_iot_and_nested_tables():
    # Regression-style test pinning the ALL_TABLES filtering this app
    # deliberately verified against Oracle's docs before writing (see
    # module docstring): naive "SELECT table_name FROM all_tables" would
    # also return materialized-view container tables, IOT overflow/mapping
    # segments, and nested-table storage tables mixed in with real ones.
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["T1"],
        columns_rows=[("T1", "ID", "NUMBER", "N")],
    ))
    backend = OracleBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "all_mviews" in table_names_sql
    assert "iot_type" in table_names_sql
    assert "nested" in table_names_sql
    assert "FETCH FIRST" in table_names_sql  # Oracle has no LIMIT clause


# --- execute ---------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_sets_autocommit_true():
    # Unlike Databricks (read-only property), Oracle's Connection.autocommit
    # is a normal settable property, same as Postgres's - execute() must set
    # it directly.
    responses = [([], None, 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    backend.execute(conn, "UPDATE t SET x=1;")
    assert conn.autocommit is True


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = 1;")
    assert results[0]["columns"] is None
    assert results[0]["rowCount"] == 3


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [([], None, 1), ([(1,)], [("id",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    results = backend.execute(conn, "UPDATE t SET x=1; SELECT id FROM t;")
    assert len(results) == 2
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    responses = [([], None, 1), RuntimeError("ORA-00933: SQL command not properly ended")]
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "ORA-00933" in str(err)
