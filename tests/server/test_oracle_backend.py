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

Also covers execute()'s PL/SQL-aware statement splitting
(_split_oracle_script/_split_oracle_chunk, near the bottom of this file) -
regression coverage for a real user-reported bug: sqlparse.split() (the
generic semicolon-based splitter the rest of execute() relies on) has no
notion of PL/SQL block structure, so a whole anonymous block like

    DECLARE
      v_count NUMBER;
    BEGIN
      ...
    END;
    /

got fragmented at the semicolon ending "DECLARE v_count NUMBER;", sending
Oracle two separate, individually incomplete statements instead of one
complete block - surfacing as Oracle's own "PLS-00103: Encountered the
symbol 'end-of-file' ... not null range default character", the
unmistakable signature of a DECLARE section whose closing BEGIN/semicolon
never arrived. The fix (see backends/oracle.py) splits on a bare "/" line
first (Oracle's own PL/SQL-block boundary marker - the dialect prompt in
translate_routes.py now instructs the model to always emit one) and sends
a DECLARE/BEGIN/CREATE-PROCEDURE-etc. chunk to Oracle whole, exactly as
written.

Also covers execute()'s DBMS_OUTPUT capture (_enable_dbms_output/
_drain_dbms_output, near the bottom of this file) - a follow-up to the
splitting fix above: DBMS_OUTPUT.PUT_LINE (what the model's own PL/SQL
write-test blocks use to report success/failure) writes into a session
buffer a plain cursor.execute() never sees, so without this the block ran
correctly but the user saw no feedback text at all. These tests use a
purpose-built FakeDbmsOutputCursor (below) rather than
helpers.make_fake_pg_connection's generic FakePgCursor, since the real
FakePgCursor has no callproc()/arrayvar()/var() at all - useful in its own
right as a regression guard that a callproc-less cursor (which is what
every OTHER test in this file drives execute() with) degrades to "just no
notices key", never an error, via _enable_dbms_output's/
_drain_dbms_output's own try/except.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.oracle import (
    OracleBackend, _set_current_schema, _IDENTIFIER_RE,
    _split_oracle_script, _split_oracle_chunk,
    _DBMS_OUTPUT_CHUNK_SIZE,
)
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


# --- execute(): PL/SQL block splitting (bare "/" terminator) ----------------
# See this file's module docstring for the real-world bug this section
# guards against.

WRITE_TEST_BLOCK = """DECLARE
  v_count NUMBER;
BEGIN
  -- Step 1: create temporary test table
  EXECUTE IMMEDIATE 'CREATE TABLE WRITE_TEST_TMP (ID NUMBER, TEST_VALUE VARCHAR2(50))';
  -- Step 2: insert a test record
  EXECUTE IMMEDIATE 'INSERT INTO WRITE_TEST_TMP (ID, TEST_VALUE) VALUES (1, ''write test'')';
  -- Step 3: verify the record was written
  EXECUTE IMMEDIATE 'SELECT COUNT(*) FROM WRITE_TEST_TMP WHERE ID = 1' INTO v_count;
  IF v_count = 1 THEN
    DBMS_OUTPUT.PUT_LINE('WRITE TEST: SUCCESS - test record inserted successfully.');
  ELSE
    DBMS_OUTPUT.PUT_LINE('WRITE TEST: FAILED - record not found after insert.');
  END IF;
  -- Step 4: clean up - drop the table entirely (removes table + row, purge avoids recycle bin trace)
  EXECUTE IMMEDIATE 'DROP TABLE WRITE_TEST_TMP PURGE';
  DBMS_OUTPUT.PUT_LINE('CLEANUP: SUCCESS - temporary table WRITE_TEST_TMP removed, no trace remains.');
EXCEPTION
  WHEN OTHERS THEN
    DBMS_OUTPUT.PUT_LINE('WRITE TEST: FAILED - ' || SQLERRM);
    -- Attempt cleanup even if something failed partway, ignoring errors if table doesn't exist
    BEGIN
      EXECUTE IMMEDIATE 'DROP TABLE WRITE_TEST_TMP PURGE';
      DBMS_OUTPUT.PUT_LINE('CLEANUP: SUCCESS - temporary table removed after error.');
    EXCEPTION
      WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('CLEANUP: NOT NEEDED - table was never created.');
    END;
END;
/
"""


def test_write_test_block_becomes_a_single_statement():
    """The real bug report: this whole block - nested BEGIN/EXCEPTION/END
    included - must survive as ONE statement, not get fragmented at the
    semicolon ending "DECLARE v_count NUMBER;"."""
    statements = _split_oracle_script(WRITE_TEST_BLOCK)
    assert len(statements) == 1
    stmt = statements[0]
    assert stmt.startswith("DECLARE")
    # The final END; (with its semicolon) must be intact - not stripped,
    # not truncated - and the trailing "/" must NOT be part of it.
    assert stmt.endswith("END;")
    assert "/" not in stmt.splitlines()[-1]
    assert "DECLARE" in stmt and "EXCEPTION" in stmt and "SQLERRM" in stmt


def test_write_test_block_executes_as_one_cursor_call():
    """End-to-end through OracleBackend.execute(): cursor.execute() must
    be called exactly once with the untouched block text - what actually
    reaches python-oracledb - not twice with two broken fragments (the
    pre-fix behavior)."""
    responses = [([], None, 0)]  # one call: an anonymous block has no result set
    conn, cursor = make_fake_pg_connection(responses)
    backend = OracleBackend()

    results = backend.execute(conn, WRITE_TEST_BLOCK)

    assert len(cursor.calls) == 1
    sql_sent, params = cursor.calls[0]
    assert sql_sent.startswith("DECLARE")
    assert sql_sent.endswith("END;")
    assert params is None
    assert len(results) == 1
    assert results[0]["rowCount"] == 0


def test_plsql_block_with_no_trailing_slash_still_treated_as_one_unit():
    """The dialect prompt (translate_routes.py) now asks the model to
    always emit a trailing '/', but a block that's simply the last (or
    only) thing in the script - with no '/' at all - must still come out
    as one atomic statement, not silently regress to the old broken
    behavior."""
    sql = "DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;"
    assert _split_oracle_script(sql) == ["DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;"]


def test_create_procedure_body_is_one_unit():
    """CREATE OR REPLACE PROCEDURE/FUNCTION/PACKAGE/TRIGGER/TYPE bodies are
    exactly the same category of problem as an anonymous block - their
    bodies are also full of internal semicolons sqlparse.split() would
    otherwise fragment."""
    sql = (
        "CREATE OR REPLACE PROCEDURE bump_counter AS\n"
        "  v NUMBER;\n"
        "BEGIN\n"
        "  v := 1;\n"
        "  UPDATE counters SET n = n + v;\n"
        "END;\n"
        "/\n"
    )
    statements = _split_oracle_script(sql)
    assert len(statements) == 1
    assert statements[0].startswith("CREATE OR REPLACE PROCEDURE")
    assert statements[0].endswith("END;")


def test_plsql_block_followed_by_plain_statement_after_slash():
    """A script isn't always ALL PL/SQL - a block terminated by '/'
    followed by an ordinary trailing SELECT should yield two statements:
    the block whole, and the SELECT split normally."""
    sql = (
        "DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;\n"
        "/\n"
        "SELECT COUNT(*) FROM WRITE_TEST_TMP;"
    )
    statements = _split_oracle_script(sql)
    assert len(statements) == 2
    assert statements[0].startswith("DECLARE") and statements[0].endswith("END;")
    assert statements[1] == "SELECT COUNT(*) FROM WRITE_TEST_TMP;"


def test_plain_multi_statement_sql_still_splits_on_semicolons():
    """The whole point of scoping the fix to PL/SQL-shaped chunks: a plain
    script with no procedural block at all must keep splitting into one
    statement per semicolon, exactly as before (see also
    test_execute_multiple_statements_returns_one_result_per_statement
    above, which pins this same guarantee at the execute() level)."""
    sql = "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL;"
    assert _split_oracle_script(sql) == ["SELECT 1 FROM DUAL;", "SELECT 2 FROM DUAL;"]


def test_single_statement_no_semicolon_unaffected():
    assert _split_oracle_script("SELECT * FROM employees") == ["SELECT * FROM employees"]


def test_empty_and_whitespace_only_chunks_are_dropped():
    assert _split_oracle_chunk("   \n  ") == []
    assert _split_oracle_script("\n/\n\n/\n") == []


# --- execute(): DBMS_OUTPUT capture ------------------------------------------
# See this file's module docstring for why FakeDbmsOutputCursor exists
# instead of reusing make_fake_pg_connection's generic FakePgCursor.

class _FakeDbmsOutputVar:
    """Stands in for whatever cursor.arrayvar()/cursor.var() return - just
    enough of a real oracledb Var object's surface (setvalue/getvalue) for
    _drain_dbms_output()/_enable_dbms_output() to work against."""
    def __init__(self, initial=None):
        self._value = initial

    def setvalue(self, pos, value):
        self._value = value

    def getvalue(self, pos=0):
        return self._value


class FakeDbmsOutputCursor:
    """Simulates the slice of python-oracledb's cursor.execute()/
    callproc()/arrayvar()/var() surface _enable_dbms_output()/
    _drain_dbms_output() (backends/oracle.py) actually use, against a
    pre-seeded queue of "already buffered" DBMS_OUTPUT lines -
    `pending_lines`. GET_LINES is modeled realistically: each call drains
    up to the caller's requested chunk size (read off the array var's own
    current length, exactly like real GET_LINES sizes its reply to the
    bind array's declared size) and reports the true count actually
    available via `num_lines_var`, so _drain_dbms_output's "stop once a
    chunk comes back short" loop condition is exercised the same way it
    would be against a real connection - not just asserted from the test
    side.

    Doesn't model per-statement production of those lines (there's no
    concept of "this statement's own PUT_LINE calls" here) - each test
    seeds exactly the lines relevant to what it's checking and calls
    execute() once; multi-statement/interleaved-output scenarios are
    covered structurally by _drain_dbms_output's own "drain after every
    statement" placement in execute(), not by this fake's plumbing.
    """
    def __init__(self, responses, pending_lines=()):
        self._responses = list(responses)
        self._pending_lines = list(pending_lines)
        self.calls = []
        self.callproc_calls = []
        self.description = None
        self.rowcount = -1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._responses:
            item = self._responses.pop(0)
        else:
            item = ([], None, -1)
        if isinstance(item, Exception):
            raise item
        rows, description, rowcount = item
        self._rows = rows
        self.description = description
        self.rowcount = rowcount if rowcount is not None else -1

    def fetchall(self):
        return self._rows

    def arrayvar(self, typ, size):
        return _FakeDbmsOutputVar([None] * size)

    def var(self, typ):
        return _FakeDbmsOutputVar()

    def callproc(self, name, args=None):
        self.callproc_calls.append((name, args))
        if name == "dbms_output.enable":
            return None
        if name == "dbms_output.get_lines":
            lines_var, num_lines_var = args
            chunk_size = len(lines_var.getvalue())
            chunk = self._pending_lines[:chunk_size]
            self._pending_lines = self._pending_lines[chunk_size:]
            lines_var.setvalue(0, chunk + [None] * (chunk_size - len(chunk)))
            num_lines_var.setvalue(0, len(chunk))
            return None
        raise NotImplementedError(f"FakeDbmsOutputCursor: unexpected callproc {name!r}")


def _fake_dbms_output_connection(responses, pending_lines=()):
    cursor = FakeDbmsOutputCursor(responses, pending_lines)

    class _Conn:
        autocommit = False

        def cursor(self):
            return cursor

    return _Conn(), cursor


def test_execute_captures_dbms_output_and_attaches_as_notices():
    conn, cursor = _fake_dbms_output_connection(
        responses=[([], None, 0)],
        pending_lines=[
            "WRITE TEST: SUCCESS - test record inserted successfully.",
            "CLEANUP: SUCCESS - temporary table WRITE_TEST_TMP removed, no trace remains.",
        ],
    )
    backend = OracleBackend()

    results = backend.execute(conn, WRITE_TEST_BLOCK)

    assert len(results) == 1
    assert results[0]["notices"] == [
        "WRITE TEST: SUCCESS - test record inserted successfully.",
        "CLEANUP: SUCCESS - temporary table WRITE_TEST_TMP removed, no trace remains.",
    ]
    # dbms_output.enable is called once, before any statement executes.
    assert cursor.callproc_calls[0][0] == "dbms_output.enable"


def test_execute_omits_notices_key_entirely_when_nothing_was_written():
    """Never an empty list - see backends/base.py's execute() docstring
    for the "notices" key's contract."""
    conn, cursor = _fake_dbms_output_connection(
        responses=[([], None, 1)], pending_lines=[],
    )
    backend = OracleBackend()
    results = backend.execute(conn, "UPDATE t SET x = 1;")
    assert "notices" not in results[0]


def test_execute_drains_more_than_one_chunk():
    """GET_LINES is called in _DBMS_OUTPUT_CHUNK_SIZE-sized chunks - a
    buffer with more lines than that must still come back complete and in
    order, via more than one callproc() round-trip."""
    many_lines = [f"line {i}" for i in range(_DBMS_OUTPUT_CHUNK_SIZE + 7)]
    conn, cursor = _fake_dbms_output_connection(
        responses=[([], None, 0)], pending_lines=many_lines,
    )
    backend = OracleBackend()
    results = backend.execute(conn, "BEGIN\n  NULL;\nEND;")
    assert results[0]["notices"] == many_lines
    get_lines_calls = [c for c in cursor.callproc_calls if c[0] == "dbms_output.get_lines"]
    assert len(get_lines_calls) == 2  # one full chunk, then the short remainder


def test_execute_notices_scoped_to_the_statement_that_produced_them():
    """Output is drained right after each statement, so a later statement
    with nothing new to say must not inherit an earlier statement's
    already-drained lines."""
    conn, cursor = _fake_dbms_output_connection(
        responses=[([], None, 0), ([], None, 0)],
        pending_lines=["only the first statement's output"],
    )
    backend = OracleBackend()
    results = backend.execute(conn, "BEGIN\n  NULL;\nEND;\n/\nBEGIN\n  NULL;\nEND;\n/\n")
    assert len(results) == 2
    assert results[0]["notices"] == ["only the first statement's output"]
    assert "notices" not in results[1]


def test_execute_against_callproc_less_cursor_never_raises_and_omits_notices():
    """Regression guard: every OTHER test in this file drives execute()
    with helpers.make_fake_pg_connection's generic FakePgCursor, which has
    no callproc()/arrayvar()/var() at all. _enable_dbms_output()/
    _drain_dbms_output()'s try/except must absorb the resulting
    AttributeError silently - a callproc-less cursor is never a reason for
    an otherwise-successful statement to fail."""
    conn, cursor = make_fake_pg_connection([([(1,)], [("x",)], 1)])
    backend = OracleBackend()
    results = backend.execute(conn, "SELECT 1 AS x FROM DUAL;")
    assert results[0]["rows"] == [{"x": 1}]
    assert "notices" not in results[0]
