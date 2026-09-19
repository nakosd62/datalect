"""
backends/mssql.py, driven two ways:
  - connect(): against the fake pytds.connect harness
    (helpers.install_fake_mssql_connect) - verifies the core kwargs, the
    encrypt-flag-to-cafile dispatch, required-field validation, and that the
    descriptor's "schema" value is stashed on the returned connection
    (mssql_schema) rather than applied via any session-level statement -
    without opening a real connection.
  - _build_shallow_schema_parts()/get_schema_shallow()/get_schema()/
    execute()/identity_label()/cache_key(): against the same psycopg2-shaped
    fake cursor/connection tests/test_postgres_backend.py uses
    (helpers.make_fake_mssql_connection, itself built on FakePgCursor) -
    pytds implements the same PEP 249 DB-API cursor shape, so no
    mssql-specific cursor fake is needed for these, just a connection
    wrapper that also carries the mssql_schema attribute get_schema() reads.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its queries unconditionally for tables/columns, then
best-effort (try/except) for every other section - see backends/mssql.py:
  1. table names        2. columns             3. constraints (best-effort)
  4. views (best-effort) 5. identity (new)      6. comments (new)
  7. row count estimates (new)                  8. routines (new)
  9. session facts/collation (new)              10. grants (new)
  11. RLS flags (new)                           12. external tables flag (new)
Indexes/Triggers are still no queries at all (deferred, same status
backends/oracle.py's/backends/redshift.py's own first-pass gaps have) -
Grants is no longer deferred, see backends/mssql.py's module docstring for
why.

get_schema() (deep) then runs _build_shallow_schema_parts() (the twelve
queries above) and appends its own Phase 2 queries on the same cursor use:
  per kept table (in order): live COUNT(*), an optional combined MIN()/MAX()
  query (if it has numeric/date columns), and up to
  MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE (cardinality-gate COUNT(DISTINCT),
  frequent-value TOP-N GROUP BY) query pairs (if it has eligible categorical
  columns) - see test_get_schema_deep_* below for worked examples of this
  second phase's exact response queue.

pytds's declared DB-API paramstyle is "pyformat" (confirmed against the
installed package) - the dynamic IN (...) clause tests below check for
plain %s-per-item placeholders (same shape backends/mysql.py's tests
check), not Oracle's named :name style.

Also covers the pytds/pyOpenSSL compatibility shim near the bottom of this
file: importing backends.mssql replaces pytds.tls.validate_host (its own
TLS hostname check, which calls a pyOpenSSL method removed in 26.2.0) with
an equivalent built on the "cryptography" library - see that section's own
comment and backends/mssql.py's module docstring for the full story.
"""

import sys
import threading
import time
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import backends.mssql as mssql_module
from backends.mssql import MssqlBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import install_fake_mssql_connect, make_fake_mssql_connection


def _ms(monkeypatch):
    harness = install_fake_mssql_connect(monkeypatch)
    return MssqlBackend(), harness


def _schema_responses(
    table_names, columns_rows, constraints=(), views=(),
    identity_columns=(), comments=(), row_count_estimates=(), routines=(),
    session_collation=("SQL_Latin1_General_CP1_CI_AS",), grants=(), rls_flags=(),
    external_tables=(),
):
    """Queues _build_shallow_schema_parts()'s twelve Phase 1 responses in
    the exact order backends/mssql.py issues them (see this file's module
    docstring). `session_collation` defaults to a real one-row tuple
    (like backends/postgres.py's own `session_settings` default) so tests
    that don't care about the Session line still get harmless, deterministic
    output rather than needing to pass it every time; `None` simulates the
    query itself returning zero rows."""
    return [
        ([(n,) for n in table_names], None, -1),
        (list(columns_rows), None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
        (list(identity_columns), None, -1),
        (list(comments), None, -1),
        (list(row_count_estimates), None, -1),
        (list(routines), None, -1),
        ([session_collation] if session_collation is not None else [], None, -1),
        (list(grants), None, -1),
        (list(rls_flags), None, -1),
        (list(external_tables), None, -1),
    ]


# --- liveness_sql ------------------------------------------------------------

def test_liveness_sql_is_unmodified_bare_select_1():
    # SQL Server supports a bare "SELECT 1" (no FROM required) - unlike
    # Oracle, this dialect needs no override of the base class's default.
    assert MssqlBackend.liveness_sql == "SELECT 1"


def test_dialect_name_is_microsoft_sql_server():
    # Must match the _DIALECT_PROMPT_INTROS key in translate_routes.py
    # exactly - that lookup is keyed by this attribute.
    assert MssqlBackend.dialect_name == "Microsoft SQL Server"


# --- connect(): required fields + core kwargs -------------------------------

def test_connect_passes_core_kwargs(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "port": 1433,
        "database": "sales", "user": "alice", "password": "hunter2",
        "encrypt": False,
    })
    call = harness.calls[-1]
    assert call["server"] == "db.example.com"
    assert call["port"] == 1433
    assert call["database"] == "sales"
    assert call["user"] == "alice"
    assert call["password"] == "hunter2"
    # autocommit is a connect-time constructor kwarg for pytds, not a
    # post-connect attribute assignment - see module docstring.
    assert call["autocommit"] is True
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - a wrong/
    # unreachable host must fail fast rather than hang indefinitely. This is
    # pytds's login_timeout, not its separate (query-scoped) "timeout" kwarg.
    assert call["login_timeout"] == DB_CONNECT_TIMEOUT_SECONDS
    assert "timeout" not in call


# --- connect(): hard external timeout enforcement (_connect_with_hard_timeout) ---
#
# pytds's own login_timeout kwarg (asserted above) is NOT a reliable hard
# deadline - see backends/mssql.py's _connect_with_hard_timeout docstring
# for the real accounting bug that lets it run well past the configured
# budget. These tests drive that external ThreadPoolExecutor/
# future.result(timeout=...) enforcement directly, via
# FakeMssqlConnectHarness's `delay`/`raise_exc` (a slow real connect() is
# simulated with time.sleep(), same pattern test_execute_routes.py's
# _FakeBackend uses for SQL_EXECUTE_TIMEOUT_SECONDS).

def test_connect_raises_friendly_timeout_error_when_pytds_connect_blocks_past_the_configured_budget(monkeypatch):
    backend, harness = _ms(monkeypatch)
    monkeypatch.setattr(mssql_module, "DB_CONNECT_TIMEOUT_SECONDS", 0.2)
    harness.delay = 2  # far longer than the 0.2s budget below

    start = time.perf_counter()
    with pytest.raises(TimeoutError, match="timed out after 0.2 seconds"):
        backend.connect({
            "type": "mssql", "host": "db.example.com", "database": "sales",
            "user": "alice", "password": "hunter2", "encrypt": False,
        })
    elapsed = time.perf_counter() - start

    # Bounded by the configured budget, not by harness.delay (2s) - the
    # whole point of wrapping this externally.
    assert elapsed < 1.0


def test_connect_still_succeeds_normally_when_pytds_connect_returns_within_the_budget(monkeypatch):
    backend, harness = _ms(monkeypatch)
    monkeypatch.setattr(mssql_module, "DB_CONNECT_TIMEOUT_SECONDS", 5)
    harness.delay = 0

    connection = backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2", "encrypt": False,
    })
    assert connection is harness.connections[-1]
    assert connection.closed is False


def test_connect_zero_timeout_disables_the_wrapper_and_calls_pytds_connect_directly(monkeypatch):
    # Mirrors SQL_EXECUTE_TIMEOUT_SECONDS's own "<= 0 disables entirely"
    # escape hatch in execute_routes.py - a slow harness.delay is still
    # honored in full (no external deadline enforced at all) rather than
    # raising immediately.
    backend, harness = _ms(monkeypatch)
    monkeypatch.setattr(mssql_module, "DB_CONNECT_TIMEOUT_SECONDS", 0)
    harness.delay = 0.3

    start = time.perf_counter()
    connection = backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2", "encrypt": False,
    })
    elapsed = time.perf_counter() - start

    assert connection is harness.connections[-1]
    assert elapsed >= 0.3


def test_connect_closes_a_connection_that_arrives_late_after_the_timeout_already_fired(monkeypatch):
    # The abandoned background thread isn't joined (see
    # _connect_with_hard_timeout's docstring - the caller must not block
    # waiting for it), but if pytds.connect() eventually DOES succeed after
    # we've already given up and raised, that live connection must still
    # get closed rather than leaking a real, open server-side session
    # forever with no reference left to close it.
    backend, harness = _ms(monkeypatch)
    monkeypatch.setattr(mssql_module, "DB_CONNECT_TIMEOUT_SECONDS", 0.2)
    harness.delay = 0.6
    harness.connect_finished = threading.Event()

    with pytest.raises(TimeoutError):
        backend.connect({
            "type": "mssql", "host": "db.example.com", "database": "sales",
            "user": "alice", "password": "hunter2", "encrypt": False,
        })

    # The background connect() call was still in flight when we raised -
    # wait for it to actually finish (well under its own 0.6s delay) rather
    # than sleeping blindly, then confirm the late-arriving connection got
    # closed by the done-callback.
    assert harness.connect_finished.wait(timeout=2), "background connect() never finished"
    assert len(harness.connections) == 1
    assert harness.connections[-1].closed is True


def test_connect_does_not_crash_when_the_late_arriving_connect_call_itself_fails(monkeypatch):
    # Symmetric case: the abandoned attempt eventually raises (its own
    # unrelated connection failure) rather than succeeding late - the
    # done-callback must swallow that quietly too, not propagate it
    # anywhere (there's no caller left waiting for this background
    # thread's outcome by the time it finishes).
    backend, harness = _ms(monkeypatch)
    monkeypatch.setattr(mssql_module, "DB_CONNECT_TIMEOUT_SECONDS", 0.2)
    harness.delay = 0.5
    harness.raise_exc = Exception("simulated late connection failure")
    harness.connect_finished = threading.Event()

    with pytest.raises(TimeoutError):
        backend.connect({
            "type": "mssql", "host": "db.example.com", "database": "sales",
            "user": "alice", "password": "hunter2", "encrypt": False,
        })

    assert harness.connect_finished.wait(timeout=2), "background connect() never finished"
    assert harness.connections == []  # never got far enough to construct one


def test_connect_defaults_port_to_1433_when_omitted(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2", "encrypt": False,
    })
    assert harness.calls[-1]["port"] == 1433


def test_connect_raises_when_no_host_given(monkeypatch):
    backend, harness = _ms(monkeypatch)
    try:
        backend.connect({"type": "mssql", "database": "sales", "user": "alice", "password": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_raises_when_no_database_given(monkeypatch):
    backend, harness = _ms(monkeypatch)
    try:
        backend.connect({"type": "mssql", "host": "db.example.com", "user": "alice", "password": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_raises_when_user_or_password_missing(monkeypatch):
    backend, harness = _ms(monkeypatch)
    try:
        backend.connect({"type": "mssql", "host": "db.example.com", "database": "sales", "user": "alice"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


# --- connect(): "encrypt" descriptor field -> cafile kwarg ------------------
# Regression coverage for the real-world failure this flag addresses: Azure
# SQL Database requires encryption, and pytds only attempts TLS at all when
# handed a CA bundle (cafile) to validate against - with none given, it sends
# ENCRYPT_NOT_SUP and the server-required-encryption case fails outright. See
# backends/mssql.py's module docstring.

def test_connect_defaults_to_encrypted_when_flag_is_absent(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2",
    })
    call = harness.calls[-1]
    assert "cafile" in call
    assert call["cafile"]  # a real path string, not empty/None


def test_connect_with_encrypt_true_passes_cafile(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "sql.database.windows.net", "database": "sales",
        "user": "alice", "password": "hunter2", "encrypt": True,
    })
    assert "cafile" in harness.calls[-1]


def test_connect_with_encrypt_false_passes_no_cafile(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2", "encrypt": False,
    })
    assert "cafile" not in harness.calls[-1]


# --- connect(): "schema" is stashed on the connection, not session-mutated -
# Unlike Oracle's ALTER SESSION SET CURRENT_SCHEMA or Redshift's SET
# search_path, T-SQL has no version-stable single statement to change a
# session's default schema - see module docstring for why connect() issues
# NO extra SQL statement for "schema" at all.

def test_connect_stashes_schema_on_connection_object(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2", "schema": "reporting",
    })
    conn = harness.connections[-1]
    assert conn.mssql_schema == "reporting"


def test_connect_without_schema_stashes_none(monkeypatch):
    backend, harness = _ms(monkeypatch)
    backend.connect({
        "type": "mssql", "host": "db.example.com", "database": "sales",
        "user": "alice", "password": "hunter2",
    })
    conn = harness.connections[-1]
    assert conn.mssql_schema is None


# --- cache_key ---------------------------------------------------------------

def test_cache_key_is_host_port_slash_database_dot_schema():
    backend = MssqlBackend()
    key = backend.cache_key({
        "host": "db.example.com", "port": 1433, "database": "sales", "schema": "reporting",
    })
    assert key == "db.example.com:1433/sales.reporting"


def test_cache_key_defaults_schema_to_dbo():
    backend = MssqlBackend()
    key = backend.cache_key({"host": "db.example.com", "port": 1433, "database": "sales"})
    assert key == "db.example.com:1433/sales.dbo"


def test_cache_key_handles_missing_fields():
    backend = MssqlBackend()
    assert backend.cache_key({}) == "unknown:unknown/unknown.dbo"


def test_cache_key_never_includes_credentials():
    backend = MssqlBackend()
    key = backend.cache_key({
        "host": "db.example.com", "port": 1433, "database": "sales", "password": "hunter2",
    })
    assert "hunter2" not in key


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_database_and_user():
    conn, cursor = make_fake_mssql_connection([([("sales", "alice")], None, -1)])
    backend = MssqlBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "sales"
    assert username == "alice"
    assert "DB_NAME()" in cursor.calls[0][0]
    assert "SYSTEM_USER" in cursor.calls[0][0]


# --- get_schema ------------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_mssql_connection([([], None, -1)])
    backend = MssqlBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_table_with_columns():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "int", "NO", None),
            ("customers", "name", "varchar", "YES", None),
        ],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id int NOT NULL" in schema
    assert "name varchar NULL" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "int", "NO", None)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Table family: events_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert "Table: events_20240102" not in schema


def test_get_schema_views_section_not_scoped_to_kept_names():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
        views=[("customer_orders", "SELECT * FROM orders JOIN customers ...")],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_section_says_enforced():
    # Unlike Redshift/Snowflake/Databricks, SQL Server DOES enforce PK/FK/
    # UNIQUE at write time - the wording must say so, not reuse those
    # dialects' "declared only, never enforced" caption.
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        constraints=[("orders", "orders_pk", "PRIMARY KEY", "id", None, None)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Constraints (enforced at write time):" in schema
    assert "orders_pk" in schema
    assert "declared only" not in schema.lower()


def test_get_schema_constraints_resolves_foreign_key_target():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        constraints=[("orders", "fk_customer", "FOREIGN KEY", "customer_id", "customers", "id")],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "customer_id -> customers(id)" in schema


def test_get_schema_survives_constraints_query_failure():
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "TABLE_CONSTRAINTS" in sql:
                raise Exception("permission denied on INFORMATION_SCHEMA.TABLE_CONSTRAINTS")

        def fetchall(self):
            last_sql = self.calls[-1][0]
            if "INFORMATION_SCHEMA.TABLES" in last_sql:
                return [("orders",)]
            if "INFORMATION_SCHEMA.COLUMNS" in last_sql:
                return [("orders", "id", "int", "NO", None)]
            return []

    class RaisingConnection:
        mssql_schema = None

        def cursor(self):
            return RaisingCursor()

    backend = MssqlBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: orders" in schema
    assert "Constraints" not in schema


def test_get_schema_survives_views_query_failure():
    class RaisingCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            if "INFORMATION_SCHEMA.VIEWS" in sql:
                raise Exception("permission denied on INFORMATION_SCHEMA.VIEWS")

        def fetchall(self):
            last_sql = self.calls[-1][0]
            if "INFORMATION_SCHEMA.TABLES" in last_sql:
                return [("orders",)]
            if "INFORMATION_SCHEMA.COLUMNS" in last_sql:
                return [("orders", "id", "int", "NO", None)]
            if "TABLE_CONSTRAINTS" in last_sql:
                return []
            return []

    class RaisingConnection:
        mssql_schema = None

        def cursor(self):
            return RaisingCursor()

    backend = MssqlBackend()
    schema = backend.get_schema(RaisingConnection())
    assert "Table: orders" in schema
    assert "Views:" not in schema


def test_get_schema_scopes_columns_query_with_pyformat_placeholders_not_string_formatting():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["tbl_a", "tbl_b"],
        columns_rows=[("tbl_a", "id", "int", "NO", None), ("tbl_b", "id", "int", "NO", None)],
    ))
    backend = MssqlBackend()
    backend.get_schema(conn)

    columns_sql, columns_params = cursor.calls[1]
    assert "INFORMATION_SCHEMA.COLUMNS" in columns_sql
    assert "tbl_a" not in columns_sql  # never string-formatted directly into SQL
    assert "tbl_b" not in columns_sql
    assert isinstance(columns_params, tuple)
    assert "tbl_a" in columns_params and "tbl_b" in columns_params
    assert columns_sql.count("%s") == 3  # 1 for the schema COALESCE + 2 for the IN-clause


def test_get_schema_uses_coalesce_schema_name_when_no_explicit_schema():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO", None)],
    ), schema=None)
    backend = MssqlBackend()
    backend.get_schema(conn)
    table_names_sql, table_names_params = cursor.calls[0]
    assert "COALESCE(%s, SCHEMA_NAME())" in table_names_sql
    assert table_names_params[-1] is None


def test_get_schema_uses_explicit_schema_when_given():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO", None)],
    ), schema="reporting")
    backend = MssqlBackend()
    backend.get_schema(conn)
    table_names_sql, table_names_params = cursor.calls[0]
    assert table_names_params[-1] == "reporting"


# --- get_schema(): schema-qualified names when an override is configured ---
# Regression coverage for a real-world bug this fixes: T-SQL has no
# session-level statement to change a session's default schema (see
# backends/mssql.py's module docstring), so an unqualified table/view name
# in generated SQL always resolves against the connecting login's own
# default schema, never this connection's configured "schema" override.
# Whenever those two differ - the whole reason to set "schema" explicitly -
# unqualified SQL silently targets the wrong place and fails with "Invalid
# object name". get_schema() addresses this by rendering every table/view
# name schema-qualified so Gemini reuses the exact qualified form shown,
# rather than needing to reason about which schema an unqualified name
# would land in.

def test_get_schema_qualifies_plain_table_heading_when_schema_configured():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
    ), schema="reporting")
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Table: reporting.customers" in schema
    assert "Table: customers" not in schema


def test_get_schema_leaves_table_heading_unqualified_when_no_schema_configured():
    # No override configured - an unqualified reference already resolves
    # correctly (into the same default schema this introspection itself
    # just queried via SCHEMA_NAME()), so qualifying would add nothing.
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
    ), schema=None)
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema


def test_get_schema_qualifies_date_sharded_family_heading_when_schema_configured():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "int", "NO", None)],
    ), schema="reporting")
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Table family: reporting.events_<date>" in schema
    assert "e.g. reporting.events_20240101 .. reporting.events_20240105" in schema


def test_get_schema_qualifies_constraint_table_names_when_schema_configured():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        constraints=[("orders", "fk_customer", "FOREIGN KEY", "customer_id", "customers", "id")],
    ), schema="reporting")
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "[reporting.orders]" in schema
    assert "reporting.customer_id -> reporting.customers(id)" not in schema  # sanity: only table names qualified, not columns
    assert "customer_id -> reporting.customers(id)" in schema


def test_get_schema_qualifies_view_name_when_schema_configured():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
        views=[("customer_orders", "SELECT * FROM orders JOIN customers ...")],
    ), schema="reporting")
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "View reporting.customer_orders:" in schema


def test_get_schema_table_name_query_uses_top_not_limit():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO", None)],
    ))
    backend = MssqlBackend()
    backend.get_schema(conn)
    table_names_sql, _ = cursor.calls[0]
    assert "TOP (%s)" in table_names_sql
    assert "LIMIT" not in table_names_sql


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_identity_column_marker_renders():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[
            ("users", "id", "int", "NO", None),
            ("users", "email", "varchar", "NO", None),
        ],
        identity_columns=[("users", "id")],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id int NOT NULL IDENTITY" in schema
    # The non-identity column's own line must not pick up a marker.
    email_line = [l for l in schema.splitlines() if l.strip().startswith("email")][0]
    assert "IDENTITY" not in email_line


def test_get_schema_shallow_identity_marker_absent_by_default():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "IDENTITY" not in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "varchar", "NO", None)],
        comments=[
            ("orders", None, "Customer purchase orders."),
            ("orders", "status", "Order lifecycle state."),
        ],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" in schema
    assert "[table] orders: Customer purchase orders." in schema
    assert "[column] orders.status: Order lifecycle state." in schema


def test_get_schema_shallow_comments_section_absent_when_no_comments():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        comments=[("orders", None, None), ("orders", "id", "")],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_row_count_estimate_renders():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        row_count_estimates=[("orders", 1234)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "orders: ~1234 rows (estimate)" in schema


def test_get_schema_shallow_row_count_estimate_skips_null_sum():
    """SUM(ps.row_count) comes back NULL when a table has no
    sys.dm_db_partition_stats rows at all (shouldn't normally happen for a
    real table, but a caller-crafted None must not render "~None rows")."""
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["fresh_table"],
        columns_rows=[("fresh_table", "id", "int", "NO", None)],
        row_count_estimates=[("fresh_table", None)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" not in schema


def test_get_schema_shallow_routines_render_name_and_signature_without_body():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        routines=[
            ("total_for_customer", "SQL_SCALAR_FUNCTION", "customer_id", 1, "int",
             "SELECT SUM(amount) FROM orders WHERE customer_id = @customer_id;"),
        ],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_for_customer(customer_id int) [SQL_SCALAR_FUNCTION]" in schema
    assert "SELECT SUM(amount)" not in schema


def test_get_schema_shallow_routines_aggregates_multiple_parameters():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        routines=[
            ("adjust_price", "SQL_STORED_PROCEDURE", "p_id", 1, "int", None),
            ("adjust_price", "SQL_STORED_PROCEDURE", "p_amount", 2, "money", None),
        ],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "adjust_price(p_id int, p_amount money) [SQL_STORED_PROCEDURE]" in schema


def test_get_schema_shallow_session_facts_render_collation():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        session_collation=("Latin1_General_100_CI_AS_SC",),
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Session: default collation=Latin1_General_100_CI_AS_SC" in schema
    # No fabricated timezone value - see backends/mssql.py's module
    # docstring on why this dialect has no real session-timezone concept.
    assert "timezone" not in schema.lower()


def test_get_schema_shallow_grants_render():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        grants=[("app_user", "orders", "SELECT")],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants:" in schema
    assert "Grant SELECT on orders to app_user" in schema


def test_get_schema_shallow_rls_and_external_table_flags_render_when_true():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["accounts", "remote_orders"],
        columns_rows=[
            ("accounts", "id", "int", "NO", None),
            ("remote_orders", "id", "int", "NO", None),
        ],
        rls_flags=[("accounts",)],
        external_tables=[("remote_orders",)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / external tables:" in schema
    assert "accounts: [RLS enabled]" in schema
    assert "remote_orders: [external table]" in schema


def test_get_schema_shallow_rls_section_absent_when_all_flags_false():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["accounts"],
        columns_rows=[("accounts", "id", "int", "NO", None)],
    ))
    backend = MssqlBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / external tables:" not in schema


# --- get_schema_shallow() must never include Phase 2 (deep-only) content -----

def test_get_schema_shallow_excludes_full_view_and_routine_bodies_and_phase2_sections():
    conn, cursor = make_fake_mssql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", None),
            ("orders", "status", "varchar", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "SQL_SCALAR_FUNCTION", "p1", 1, "int", "SELECT 1;")],
    ))
    backend = MssqlBackend()
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
    # Exactly the twelve Phase 1 queries - no Phase 2 query was ever issued.
    assert len(cursor.calls) == 12


# --- get_schema() (deep): Phase 2 additions on top of the shallow content ----

def _base_deep_responses():
    return _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "int", "NO", None),
            ("orders", "status", "varchar", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "SQL_SCALAR_FUNCTION", "p1", 1, "int", "SELECT 1;")],
        row_count_estimates=[("orders", 500)],
    ) + [
        # New (deep-only): schema-wide dataset size aggregate over
        # sys.dm_db_partition_stats, issued right after phase2_ctx is
        # unpacked and before any Phase 2 query - 1 table, ~500 rows,
        # ~2MB, matching format_dataset_size_line(500, 2_000_000, 1).
        ([(500, 2_000_000, 1)], None, -1),
    ]


def _phase2_sampling_responses():
    return [
        ([(42,)], None, -1),                                # live count for orders
        ([(1, 100)], None, -1),                              # min/max for id
        ([(2,)], None, -1),                                  # COUNT(DISTINCT status) gate
        ([("active", 30), ("inactive", 12)], None, -1),      # frequent values for status
    ]


def test_get_schema_deep_is_superset_of_shallow_plus_phase2_sampling():
    conn, cursor = make_fake_mssql_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = MssqlBackend()
    schema = backend.get_schema(conn)

    # Shallow content still present (Phase 1 catalog-only sections).
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "get_total(p1 int) [SQL_SCALAR_FUNCTION]" in schema
    assert "~500 rows (estimate)" in schema

    # Phase 2 additions on top.
    assert "View definitions:" in schema and "View v: SELECT 1 FROM orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "status: frequent values = active (30), inactive (12)" in schema

    # New (deep-only): schema-wide "Estimated dataset size" line, built from
    # the same numbers _base_deep_responses() queues for the new query.
    assert "Estimated dataset size: ~1.9 MB" in schema

    assert len(cursor.calls) == 12 + 1 + 4


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    """A categorical column whose live COUNT(DISTINCT ...) is at least
    NEAR_UNIQUE_DISTINCT_RATIO of the table's own live row count is treated
    as near-unique - sampling "frequent values" for it wouldn't be
    meaningful, so that column's GROUP BY query must never even be
    issued."""
    responses = _base_deep_responses() + [
        ([(42,)], None, -1),   # live count
        ([(1, 100)], None, -1),  # min/max for id
        ([(40,)], None, -1),   # COUNT(DISTINCT status) gate: 40/42 ~ 0.95, near-unique
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 12 + 1 + 3


def test_get_schema_deep_naming_convention_relationships_section():
    # "varbinary" is deliberately outside both NUMERIC_OR_DATE_TYPES and
    # CATEGORICAL_TYPES (an ill-fitting type for MIN()/MAX() or a frequent-
    # value GROUP BY - see those frozensets' own comment), so each table
    # gets only its live row count query, no sampling - keeping this test
    # focused on the naming-convention pass alone.
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "varbinary", "NO", None),
            ("orders", "customer_id", "varbinary", "NO", None),
        ],
    ) + [
        ([(10,)], None, -1),  # live count: customers (no numeric/categorical cols to sample)
        ([(20,)], None, -1),  # live count: orders
    ]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "orders.customer_id -> likely relationship (unconfirmed): references customers" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_mssql_connection(_schema_responses(
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
    from backends.mssql import MAX_COLUMNS_FOR_SAMPLING

    columns_rows = [
        ("wide", f"col_{i}", "int", "NO", None)
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["wide"],
        columns_rows=columns_rows,
    ) + [
        ([(7, 1000, 1)], None, -1),  # new: schema-wide dataset size aggregate
        ([(7,)], None, -1),   # live count for wide
        # no min/max or gate/frequent-value response queued - must not be requested
    ]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "wide: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 12 + 1 + 1


def test_get_schema_deep_qualifies_live_row_count_and_sample_lines_when_schema_configured():
    """Phase 2's own rendered lines (live counts, sample blocks) must be
    schema-qualified too when an override is configured - not just Phase
    1's headings - so a query built from this schema text stays consistent
    throughout (see module docstring's schema-qualification rationale)."""
    responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
    ) + [
        ([(5, 1000, 1)], None, -1),  # new: schema-wide dataset size aggregate
        ([(5,)], None, -1),     # live count
        ([(1, 5)], None, -1),   # min/max for id
    ]
    conn, cursor = make_fake_mssql_connection(responses, schema="reporting")
    backend = MssqlBackend()
    schema = backend.get_schema(conn)
    assert "reporting.orders: 5 rows (live, authoritative)" in schema
    assert "Table: reporting.orders" in schema
    live_count_sql = [c for c, _p in cursor.calls if c.startswith("SELECT COUNT(*)")][0]
    assert "[reporting].[orders]" in live_count_sql


def test_get_schema_deep_dataset_size_query_is_schema_wide_not_scoped_to_kept_names():
    """The new sys.dm_db_partition_stats aggregate must have no per-table
    filter at all - unlike the neighboring Phase 1 "Row count estimates"
    query against the same DMV, which is deliberately scoped to
    kept_names (a capped subset). Scoping the new query the same way would
    just re-total the same capped subset, defeating its whole "true
    schema-wide size even when most tables got capped out" purpose."""
    conn, cursor = make_fake_mssql_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = MssqlBackend()
    backend.get_schema(conn)

    size_sql = [c for c, _p in cursor.calls if "COUNT(DISTINCT t.object_id)" in c][0]
    assert "t.name IN" not in size_sql

    estimate_sql = [c for c, _p in cursor.calls if c.strip().startswith("SELECT t.name, SUM(ps.row_count)")][0]
    assert "t.name IN" in estimate_sql


def test_get_schema_deep_dataset_size_query_failure_leaves_rest_of_schema_intact():
    """A failure in the new best-effort dataset-size query (e.g. missing
    VIEW SERVER STATE permission) must not corrupt or truncate anything
    else in the schema - just omit the "Estimated dataset size" line."""
    responses = _base_deep_responses()
    responses[-1] = Exception("VIEW SERVER STATE permission denied")
    responses = responses + _phase2_sampling_responses()
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    schema = backend.get_schema(conn)

    assert "Table: orders" in schema
    assert "View definitions:" in schema and "View v: SELECT 1 FROM orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "status: frequent values = active (30), inactive (12)" in schema
    assert "Estimated dataset size" not in schema


# --- execute ---------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_does_not_touch_autocommit_attribute():
    # Unlike Oracle's/Redshift's execute() (which set connection.autocommit
    # = True directly), pytds's autocommit is already a connect()-time
    # constructor kwarg (see backends/mssql.py's connect()) - execute() must
    # not assume or require a settable .autocommit attribute at all.
    responses = [([], None, 1)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    backend.execute(conn, "UPDATE t SET x=1;")
    assert not hasattr(conn, "autocommit")


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = 1;")
    assert results[0]["columns"] is None
    assert results[0]["rowCount"] == 3


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [([], None, 1), ([(1,)], [("id",)], 1)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "UPDATE t SET x=1; SELECT id FROM t;")
    assert len(results) == 2
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    responses = [([], None, 1), RuntimeError("Incorrect syntax near 'bad'")]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "Incorrect syntax near 'bad'" in str(err)


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale - this just proves MssqlBackend routes through the same shared
# fetch_capped_rows() (backends/base.py) instead of its own fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_mssql_connection(responses)
    backend = MssqlBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]


# --- pytds/pyOpenSSL compatibility shim (TLS hostname validation) -----------
# pytds's own pytds.tls.validate_host calls pyOpenSSL's X509.get_extension(),
# which was removed in pyOpenSSL 26.2.0 (confirmed directly against the
# installed package's source - present-but-deprecated in 26.1.0, gone by
# 26.2.0) - meaning every encrypt=true mssql connection (the default) would
# fail with "'X509' object has no attribute 'get_extension'" on any
# pyOpenSSL >= 26.2.0 without this module's fix. Importing backends.mssql
# replaces pytds.tls.validate_host with an equivalent built on the
# "cryptography" library instead - these tests exercise that replacement
# directly against real (self-signed, in-memory) certificates, not fakes,
# since the whole point is to prove it behaves like a real TLS hostname
# check would, not just that it doesn't crash.

def _self_signed_cert(common_name, san_dns_names=()):
    """Builds a real self-signed X.509 certificate (via the "cryptography"
    library) and wraps it as a pyOpenSSL X509 object - i.e. exactly the
    shape connection.get_peer_certificate() would hand back mid-handshake -
    so these tests exercise the actual object types/methods involved, not
    a hand-rolled stand-in for them."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from OpenSSL import crypto

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
    )
    if san_dns_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in san_dns_names]),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    return crypto.X509.from_cryptography(cert)


def test_importing_backend_replaces_pytds_validate_host():
    import pytds
    from backends.mssql import _validate_host_via_cryptography
    assert pytds.tls.validate_host is _validate_host_via_cryptography


def test_pyopenssl_x509_no_longer_has_get_extension_on_this_install():
    # Pins down *why* the shim above is needed, on whatever pyOpenSSL
    # version this environment actually has installed - if this ever
    # starts failing (pyOpenSSL restored get_extension, or reworked its
    # API again), it's a signal to re-evaluate whether the shim is still
    # necessary, not evidence the shim itself is broken.
    from OpenSSL import crypto
    assert not hasattr(crypto.X509, "get_extension")
    assert hasattr(crypto.X509, "get_extension_count")


def test_validate_host_matches_common_name():
    from backends.mssql import _validate_host_via_cryptography
    cert = _self_signed_cert(u"db.example.com")
    assert _validate_host_via_cryptography(cert, b"db.example.com") is True


def test_validate_host_matches_subject_alternative_name():
    from backends.mssql import _validate_host_via_cryptography
    cert = _self_signed_cert(u"unrelated-cn.example.com", san_dns_names=[u"db.example.com"])
    assert _validate_host_via_cryptography(cert, b"db.example.com") is True


def test_validate_host_matches_single_label_wildcard_san():
    from backends.mssql import _validate_host_via_cryptography
    cert = _self_signed_cert(u"unrelated-cn.example.com", san_dns_names=[u"*.example.com"])
    assert _validate_host_via_cryptography(cert, b"db.example.com") is True
    # Only a single label - "*.example.com" must not match "a.b.example.com".
    assert _validate_host_via_cryptography(cert, b"a.b.example.com") is False


def test_validate_host_rejects_mismatched_host():
    from backends.mssql import _validate_host_via_cryptography
    cert = _self_signed_cert(u"db.example.com", san_dns_names=[u"db.example.com"])
    assert _validate_host_via_cryptography(cert, b"someone-else.example.com") is False


def test_validate_host_handles_certificate_with_no_san_extension():
    # Some certs (this self-signed one, with no add_extension call) carry
    # no subjectAltName at all - must fall through to "no match" via the
    # ExtensionNotFound path, not raise.
    from backends.mssql import _validate_host_via_cryptography
    cert = _self_signed_cert(u"unrelated-cn.example.com")
    assert _validate_host_via_cryptography(cert, b"db.example.com") is False
