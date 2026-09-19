"""
backends/snowflake.py, driven two ways:
  - connect(): against the fake snowflake.connector.connect harness
    (helpers.install_fake_snowflake_connect) - verifies the
    password-vs-key-pair kwarg dispatch without opening a real connection.
  - get_schema()/get_schema_shallow()/execute()/identity_label()/cache_key():
    against the same fake psycopg2-shaped cursor/connection
    tests/test_postgres_backend.py uses (helpers.make_fake_pg_connection) -
    snowflake-connector-python implements the same PEP 249 DB-API cursor
    shape, so no Snowflake-specific fake is needed for these.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its queries unconditionally for tables/columns, then
best-effort (try/except) for every other section, in this fixed order:
  1. table names          2. columns             3. constraints (best-effort)
  4. views (best-effort)  5. table metadata: comment/row_count/clustering_key
     (new, best-effort)   6. external tables (new, best-effort)
  7. procedures (new, best-effort)   8. functions (new, best-effort)
  9. session facts: CURRENT_TIMEZONE() (new, best-effort)
  10. grants: CURRENT_ROLE() then SHOW GRANTS TO ROLE (new, best-effort - one
      or two queries depending on whether a role came back)
  11. row-level security / masking existence: SHOW ROW ACCESS POLICIES then
      SHOW MASKING POLICIES (new, best-effort, independently try/excepted)
No Indexes/Triggers queries at all (Snowflake has no user-managed indexes or
triggers - see that module's docstring); the old comment-only "automatic
micro-partition pruning/clustering instead" placeholder is now backed by a
real query (section 5's clustering_key column).

get_schema() (deep) then runs _build_shallow_schema_parts() (the eleven
query groups above) and appends its own Phase 2 queries on the same cursor,
per kept table (in order): one combined COUNT(*) + APPROX_COUNT_DISTINCT(...)
query (or a plain COUNT(*) alone for a too-wide table or one with no
categorical columns), an optional combined MIN()/MAX() query (if it has
numeric/date columns), and up to
MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE frequent-value GROUP BY queries (if
it has eligible categorical columns) - see test_get_schema_deep_* below for
worked examples of this second phase's exact response queue. Since the
production columns query now selects 7 columns (added is_identity/
identity_generation/comment), and _build_shallow_schema_parts() itself pads
a short columns_rows tuple back up to 7 (see backends/snowflake.py's
_pad_column_row) - not the test builder, unlike backends/postgres.py's own
choice to pad in the test file - every pre-existing test in this file that
still hands it a bare 4-tuple keeps working unmodified, including the
hand-rolled RaisingCursor fake below that isn't built via _schema_responses
at all.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.snowflake import SnowflakeBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import install_fake_snowflake_connect, make_fake_pg_connection


def _sf(monkeypatch):
    harness = install_fake_snowflake_connect(monkeypatch)
    return SnowflakeBackend(), harness


def _schema_responses(
    table_names, columns_rows, constraints=(), views=(),
    table_metadata=(), external_tables=(), procedures=(), functions=(),
    session_timezone="UTC", current_role="ANALYST_ROLE", grants=(),
    row_access_policies=(), masking_policies=(),
):
    responses = [
        ([(n,) for n in table_names], None, -1),
        (list(columns_rows), None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
        (list(table_metadata), None, -1),
        ([(t,) for t in external_tables], None, -1),
        (list(procedures), None, -1),
        (list(functions), None, -1),
        ([(session_timezone,)] if session_timezone is not None else [], None, -1),
    ]
    if current_role is not None:
        responses.append(([(current_role,)], None, -1))
        responses.append((list(grants), None, -1))
    else:
        responses.append(([], None, -1))  # CURRENT_ROLE() returns no row
    responses.append((list(row_access_policies), None, -1))
    responses.append((list(masking_policies), None, -1))
    return responses


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
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - bounds
    # only the connect/authenticate phase; network_timeout (which would
    # also cap query execution) is deliberately left unset.
    assert call["login_timeout"] == DB_CONNECT_TIMEOUT_SECONDS
    assert "network_timeout" not in call


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


# --- get_schema / get_schema_shallow: existing baseline behavior --------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = SnowflakeBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_shallow_returns_none_when_no_tables():
    conn, cursor = make_fake_pg_connection([([], None, -1)])
    backend = SnowflakeBackend()
    assert backend.get_schema_shallow(conn) is None


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
    # try/except). This fake also has no fetchone() at all and returns a
    # bare 4-tuple for any "information_schema.tables"-matching query
    # (which now also matches the new table-metadata/external-tables
    # queries, not just the original table-name scan) - every new section
    # that trips over that (a short-tuple unpack, or a missing fetchone())
    # is independently try/except-wrapped, so none of that takes down the
    # overall fetch; this test only asserts the two things it always has.
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


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_identity_column_marker_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["USERS"],
        columns_rows=[
            ("USERS", "ID", "NUMBER", "NO", "YES", "AUTOINCREMENT", None),
            ("USERS", "EMAIL", "TEXT", "NO", "NO", None, None),
        ],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "ID NUMBER NOT NULL IDENTITY (AUTOINCREMENT)" in schema
    assert "EMAIL TEXT NOT NULL" in schema
    email_line = [l for l in schema.splitlines() if l.strip().startswith("EMAIL")][0]
    assert "IDENTITY" not in email_line


def test_get_schema_shallow_identity_marker_absent_by_default():
    """Old-style 4-tuple columns_rows (predating is_identity/
    identity_generation/comment) must be padded to "not an identity
    column, no comment" by the backend itself - see
    backends/snowflake.py's _pad_column_row - so no existing test needs
    rewriting."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS"],
        columns_rows=[("CUSTOMERS", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "IDENTITY" not in schema


def test_get_schema_shallow_identity_marker_without_generation_type():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["USERS"],
        columns_rows=[("USERS", "ID", "NUMBER", "NO", "YES", None, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "ID NUMBER NOT NULL IDENTITY" in schema
    assert "IDENTITY (" not in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "STATUS", "TEXT", "NO", "NO", None, "Order lifecycle state.")],
        table_metadata=[("ORDERS", "Customer purchase orders.", None, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" in schema
    assert "[table] ORDERS: Customer purchase orders." in schema
    assert "[column] ORDERS.STATUS: Order lifecycle state." in schema


def test_get_schema_shallow_comments_section_absent_when_no_comments():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO", "NO", None, None)],
        table_metadata=[("ORDERS", None, None, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_row_count_estimate_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        table_metadata=[("ORDERS", None, 1234, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "ORDERS: ~1234 rows (estimate)" in schema


def test_get_schema_shallow_row_count_estimate_absent_when_null():
    """ROW_COUNT legitimately comes back NULL for a table Snowflake hasn't
    computed statistics for yet - skipped rather than rendered as a
    misleading "~None rows"."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["FRESH_TABLE"],
        columns_rows=[("FRESH_TABLE", "ID", "NUMBER", "NO")],
        table_metadata=[("FRESH_TABLE", None, None, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" not in schema


def test_get_schema_shallow_clustering_key_renders_when_present():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["EVENTS"],
        columns_rows=[("EVENTS", "ID", "NUMBER", "NO")],
        table_metadata=[("EVENTS", None, None, "LINEAR(EVENT_DATE)")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Clustering keys:" in schema
    assert "EVENTS: LINEAR(EVENT_DATE)" in schema


def test_get_schema_shallow_clustering_key_section_absent_when_no_table_has_one():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["EVENTS"],
        columns_rows=[("EVENTS", "ID", "NUMBER", "NO")],
        table_metadata=[("EVENTS", None, None, None)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Clustering keys:" not in schema


def test_get_schema_shallow_external_tables_render_when_present():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        external_tables=["RAW_S3_LOGS"],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "External tables:" in schema
    assert "RAW_S3_LOGS" in schema


def test_get_schema_shallow_external_tables_section_absent_when_none():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "External tables:" not in schema


def test_get_schema_shallow_routines_render_procedures_and_functions_without_body():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        procedures=[("SYNC_TOTALS", "CUSTOMER_ID NUMBER", "NULL", "CALL DO_SYNC();")],
        functions=[("TOTAL_FOR_CUSTOMER", "CUSTOMER_ID NUMBER", "NUMBER", "SELECT SUM(AMOUNT)...")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "[procedure] SYNC_TOTALS(CUSTOMER_ID NUMBER) -> NULL" in schema
    assert "[function] TOTAL_FOR_CUSTOMER(CUSTOMER_ID NUMBER) -> NUMBER" in schema
    assert "CALL DO_SYNC" not in schema
    assert "SELECT SUM(AMOUNT)" not in schema


def test_get_schema_shallow_routines_section_absent_when_none():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" not in schema


def test_get_schema_shallow_session_timezone_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        session_timezone="America/Los_Angeles",
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Session: timezone=America/Los_Angeles" in schema
    # No fabricated collation fact - see backends/snowflake.py's own
    # comment on why Snowflake has no single session-wide collation to
    # report the way Postgres's datcollate is.
    assert "collation" not in schema.lower()


def test_get_schema_shallow_grants_render_one_line_per_table_combining_privileges():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        current_role="ANALYST_ROLE",
        grants=[
            (None, "SELECT", "TABLE", "MYDB.PUBLIC.ORDERS", None, None, None, None),
            (None, "INSERT", "TABLE", "MYDB.PUBLIC.ORDERS", None, None, None, None),
        ],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants (current role):" in schema
    assert "ORDERS: INSERT, SELECT (role ANALYST_ROLE)" in schema


def test_get_schema_shallow_grants_filters_out_objects_not_in_kept_names():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        current_role="ANALYST_ROLE",
        grants=[
            (None, "SELECT", "TABLE", "MYDB.PUBLIC.SOME_OTHER_TABLE", None, None, None, None),
            (None, "USAGE", "SCHEMA", "MYDB.PUBLIC", None, None, None, None),
        ],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants (current role):" not in schema


def test_get_schema_shallow_grants_section_absent_when_no_current_role():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        current_role=None,
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants" not in schema
    # SHOW GRANTS TO ROLE must never even have been issued - only
    # CURRENT_ROLE() itself (which came back empty) - one fewer query than
    # the default-role case.
    assert len(cursor.calls) == 12


def test_get_schema_shallow_rls_and_masking_flags_render_when_present():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
        row_access_policies=[("REGION_POLICY",)],
        masking_policies=[("SSN_MASK",)],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / masking:" in schema
    assert "row access polic(ies)" in schema
    assert "masking polic(ies)" in schema


def test_get_schema_shallow_rls_and_masking_section_absent_when_none_defined():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[("ORDERS", "ID", "NUMBER", "NO")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / masking:" not in schema


# --- get_schema_shallow() must never include Phase 2 (deep-only) content -----

def test_get_schema_shallow_excludes_full_view_and_routine_bodies_and_phase2_sections():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["ORDERS"],
        columns_rows=[
            ("ORDERS", "ID", "NUMBER", "NO"),
            ("ORDERS", "STATUS", "TEXT", "NO"),
        ],
        views=[("V", "SELECT 1 FROM ORDERS")],
        functions=[("GET_TOTAL", "P1 NUMBER", "NUMBER", "SELECT 1;")],
    ))
    backend = SnowflakeBackend()
    schema = backend.get_schema_shallow(conn)
    assert "View V" in schema
    assert "SELECT 1 FROM ORDERS" not in schema
    assert "View definitions:" not in schema
    assert "GET_TOTAL" in schema
    assert "SELECT 1;" not in schema
    assert "Routine definitions:" not in schema
    assert "Live row counts:" not in schema
    assert "Column value samples:" not in schema
    assert "Likely relationships" not in schema
    # Exactly the thirteen Phase 1 queries (default current_role present) -
    # no Phase 2 query was ever issued.
    assert len(cursor.calls) == 13


# --- get_schema() (deep): Phase 2 additions on top of the shallow content ----

def _base_deep_responses():
    return _schema_responses(
        table_names=["ORDERS"],
        columns_rows=[
            ("ORDERS", "ID", "NUMBER", "NO"),
            ("ORDERS", "STATUS", "TEXT", "NO"),
        ],
        views=[("V", "SELECT 1 FROM ORDERS")],
        functions=[("GET_TOTAL", "P1 NUMBER", "NUMBER", "SELECT 1;")],
        table_metadata=[("ORDERS", None, 500, None)],
    ) + [
        # New (deep-only): schema-wide dataset size aggregate over
        # information_schema.tables, issued right after phase2_ctx is
        # unpacked and before any Phase 2 query - 1 table, ~500 rows,
        # ~2MB, matching format_dataset_size_line(500, 2_000_000, 1).
        ([(500, 2_000_000, 1)], None, -1),
    ]


def test_get_schema_deep_is_superset_of_shallow_plus_phase2_sampling():
    phase2_responses = [
        ([(42, 2)], None, -1),                            # COUNT(*), APPROX_COUNT_DISTINCT(STATUS)
        ([(1, 100)], None, -1),                            # min/max for ID
        ([("ACTIVE", 30), ("INACTIVE", 12)], None, -1),    # frequent values for STATUS
    ]
    conn, cursor = make_fake_pg_connection(_base_deep_responses() + phase2_responses)
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)

    # Shallow content still present (Phase 1 catalog-only sections).
    assert "Table: ORDERS" in schema
    assert "View V" in schema
    assert "GET_TOTAL(P1 NUMBER) -> NUMBER" in schema
    assert "~500 rows (estimate)" in schema

    # Phase 2 additions on top.
    assert "View definitions:" in schema and "View V: SELECT 1 FROM ORDERS" in schema
    assert "Routine definitions:" in schema and "GET_TOTAL: SELECT 1;" in schema
    assert "Live row counts:" in schema and "ORDERS: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "ID: range [1 .. 100]" in schema
    assert "STATUS: frequent values = ACTIVE (30), INACTIVE (12)" in schema

    # New (deep-only): schema-wide "Estimated dataset size" line, built from
    # the same numbers _base_deep_responses() queues for the new query.
    assert "Estimated dataset size: ~1.9 MB" in schema

    assert len(cursor.calls) == 13 + 1 + 3


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    """APPROX_COUNT_DISTINCT(status) / live COUNT(*) close to 1 means the
    column is nearly unique - sampling "frequent values" for it wouldn't be
    meaningful, so that column's GROUP BY query must never even be
    issued."""
    responses = _base_deep_responses() + [
        ([(42, 41)], None, -1),   # COUNT(*)=42, APPROX_COUNT_DISTINCT(STATUS)=41 (near-unique)
        ([(1, 100)], None, -1),   # min/max for ID
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "ID: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 13 + 1 + 2


def test_get_schema_deep_naming_convention_relationships_section():
    responses = _schema_responses(
        table_names=["CUSTOMERS", "ORDERS"],
        columns_rows=[
            ("CUSTOMERS", "ID", "NUMBER", "NO"),
            ("ORDERS", "CUSTOMER_ID", "NUMBER", "NO"),
        ],
    ) + [
        ([(10,)], None, -1),   # live count: CUSTOMERS (no categorical cols)
        ([(20,)], None, -1),   # live count: ORDERS (no categorical cols)
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "ORDERS.CUSTOMER_ID -> likely relationship (unconfirmed): references CUSTOMERS" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_pg_connection(_schema_responses(
        table_names=["CUSTOMERS", "ORDERS"],
        columns_rows=[
            ("CUSTOMERS", "ID", "NUMBER", "NO"),
            ("ORDERS", "CUSTOMER_ID", "NUMBER", "NO"),
        ],
    ))
    shallow = backend.get_schema_shallow(conn2)
    assert "Likely relationships" not in shallow


def test_get_schema_deep_skips_sampling_for_wide_tables_but_keeps_live_count():
    """A table with more columns than MAX_COLUMNS_FOR_SAMPLING still gets a
    plain live row count, just no per-column sampling - bounding the
    "explosion of tiny queries" the cap exists to prevent."""
    from backends.snowflake import MAX_COLUMNS_FOR_SAMPLING

    columns_rows = [
        ("WIDE", f"COL_{i}", "NUMBER", "NO")
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["WIDE"],
        columns_rows=columns_rows,
    ) + [
        ([(7, 1000, 1)], None, -1),  # new: schema-wide dataset size aggregate
        ([(7,)], None, -1),   # plain COUNT(*) for WIDE - too wide for sampling
        # no min/max or frequent-value response queued - must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "WIDE: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 13 + 1 + 1


def test_get_schema_deep_dataset_size_query_is_schema_wide_not_scoped_to_kept_names():
    """The new information_schema.tables aggregate must have no per-table
    filter at all - unlike the neighboring Phase 1 "Table metadata" query
    against the same view, which is deliberately scoped to kept_names (a
    capped subset). Scoping the new query the same way would just re-total
    the same capped subset, defeating its whole "true schema-wide size even
    when most tables got capped out" purpose."""
    phase2_responses = [
        ([(42, 2)], None, -1),
        ([(1, 100)], None, -1),
        ([("ACTIVE", 30), ("INACTIVE", 12)], None, -1),
    ]
    conn, cursor = make_fake_pg_connection(_base_deep_responses() + phase2_responses)
    backend = SnowflakeBackend()
    backend.get_schema(conn)

    size_sql = [c for c, _p in cursor.calls if "table_type = 'BASE TABLE'" in c][0]
    assert "table_name IN" not in size_sql

    metadata_sql = [c for c, _p in cursor.calls if "clustering_key" in c][0]
    assert "table_name IN" in metadata_sql


def test_get_schema_deep_dataset_size_query_failure_leaves_rest_of_schema_intact():
    """A failure in the new best-effort dataset-size query must not corrupt
    or truncate anything else in the schema - just omit the "Estimated
    dataset size" line."""
    responses = _base_deep_responses()
    responses[-1] = Exception("insufficient privileges on information_schema.tables")
    responses = responses + [
        ([(42, 2)], None, -1),
        ([(1, 100)], None, -1),
        ([("ACTIVE", 30), ("INACTIVE", 12)], None, -1),
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    schema = backend.get_schema(conn)

    assert "Table: ORDERS" in schema
    assert "View definitions:" in schema and "View V: SELECT 1 FROM ORDERS" in schema
    assert "Routine definitions:" in schema and "GET_TOTAL: SELECT 1;" in schema
    assert "Live row counts:" in schema and "ORDERS: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "ID: range [1 .. 100]" in schema
    assert "STATUS: frequent values = ACTIVE (30), INACTIVE (12)" in schema
    assert "Estimated dataset size" not in schema


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


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    responses = [([], None, 1), RuntimeError("SQL compilation error: syntax error")]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "SQL compilation error" in str(err)


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale - this just proves SnowflakeBackend routes through the same
# shared fetch_capped_rows() (backends/base.py) instead of its own
# fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = SnowflakeBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]
