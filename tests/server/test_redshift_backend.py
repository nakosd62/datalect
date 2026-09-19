"""
backends/redshift.py, driven two ways:
  - connect(): against the fake psycopg2.connect harness
    (helpers.install_fake_redshift_connect) - verifies required-field
    validation, the default port, sslmode="require" always being passed
    (never opt-in the way Oracle's "ssl" flag is), and the SET search_path
    call a "schema" descriptor field triggers, without opening a real
    connection.
  - get_schema()/get_schema_shallow()/execute()/identity_label()/
    cache_key(): against the same fake psycopg2-shaped cursor/connection
    tests/test_postgres_backend.py uses (helpers.make_fake_pg_connection) -
    RedshiftBackend talks the exact same psycopg2 DB-API shape
    backends/postgres.py does, so no Redshift-specific fake is needed for
    these.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its Phase 1 queries unconditionally and in a fixed
order (several of them individually try/except-wrapped for graceful
degradation - see backends/redshift.py itself), so responses are queued in
the exact order it issues them:
  1. table names        2. columns             3. constraints
  4. distkey/sortkey/tbl_rows/stats_off (svv_table_info, widened)
  5. views               6. comments (new)      7. routines (new)
  8. session timezone (new)   9. grants (new)   10. external tables (new)
No Indexes/Triggers queries at all - Redshift has no such concept. RLS is
also not a Redshift concept, so there's no RLS section/query to fake here
either (see backends/redshift.py's module docstring).

get_schema() (deep) then runs _build_shallow_schema_parts() (the ten
queries above) and appends its own Phase 2 queries on a fresh cursor use:
  11. pg_stats n_distinct (shared, once)
  then per kept table (in order): live COUNT(*), an optional combined
  MIN()/MAX() query (if it has numeric/date columns), and up to
  MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE frequent-value GROUP BY queries
  (if it has eligible categorical columns) - mirrors
  test_postgres_backend.py's own Phase 2 test shape.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.redshift import RedshiftBackend, MAX_COLUMNS_FOR_SAMPLING
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import install_fake_redshift_connect, make_fake_pg_connection


def _rs(monkeypatch):
    harness = install_fake_redshift_connect(monkeypatch)
    return RedshiftBackend(), harness


def _pad_layout_row(row):
    """A layout tuple may still be the pre-row-count-estimate 3-tuple
    (table, diststyle, sortkey1) that every test predating the tbl_rows/
    stats_off addition already uses - padded here to the real 5-column
    shape _build_shallow_schema_parts()'s svv_table_info query now selects
    (..., tbl_rows, stats_off), defaulting both new columns to None (no row
    count estimate rendered), so none of those existing tests need
    rewriting just because two more columns joined the SELECT list."""
    row = list(row)
    while len(row) < 5:
        row.append(None)
    return tuple(row)


def _schema_responses(
    table_names, columns_rows, constraints=(), layout=(), views=(),
    comments=(), routines=(), session_settings=("UTC",), grants=(),
    external_tables=(),
):
    return [
        ([(n,) for n in table_names], None, -1),
        (list(columns_rows), None, -1),
        (list(constraints), None, -1),
        ([_pad_layout_row(r) for r in layout], None, -1),
        (list(views), None, -1),
        (list(comments), None, -1),
        (list(routines), None, -1),
        ([session_settings] if session_settings is not None else [], None, -1),
        (list(grants), None, -1),
        (list(external_tables), None, -1),
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
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - a wrong/
    # unreachable Redshift host (bad DNS record, closed security group) must
    # fail fast rather than hang indefinitely.
    assert call["connect_timeout"] == DB_CONNECT_TIMEOUT_SECONDS


def test_connect_defaults_port_to_5439_when_omitted(monkeypatch):
    backend, harness = _rs(monkeypatch)
    backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
    })
    assert harness.calls[-1]["port"] == 5439


# Regression coverage for a real bug a user hit against a live Redshift
# Serverless workgroup: a per-dataset "connect_timeout_seconds" override
# (resolve_timeout_seconds() - see backends/base.py) always returns a float
# when an override is actually set, and psycopg2 stringifies whatever's
# passed as connect_timeout straight into the DSN it hands libpq - which
# then rejects a float like "60.0" outright with "invalid integer value ...
# for connection option \"connect_timeout\"" before ever dialing out (see
# backends/postgres.py's identical fix and test - this dialect shares
# psycopg2/libpq underneath). An equality check alone (`== 60`) would NOT
# have caught this, since 60 == 60.0 in Python - this asserts the actual
# type psycopg2 receives, not just its numeric value.
def test_connect_timeout_override_is_passed_as_a_real_int_not_a_float(monkeypatch):
    backend, harness = _rs(monkeypatch)
    backend.connect({
        "type": "redshift", "host": "h", "database": "dev", "user": "alice", "password": "x",
        "connect_timeout_seconds": 60,
    })
    call = harness.calls[-1]
    assert call["connect_timeout"] == 60
    assert isinstance(call["connect_timeout"], int)


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


def test_get_schema_svv_table_info_query_failure_is_logged_not_swallowed(caplog):
    # Distribution/Sort Keys and Row count estimates both come from this
    # one svv_table_info query (Phase 1) - a failure here must not break
    # the rest of the fetch (matches the constraints/routines/grants
    # query-failure tests elsewhere in this file), but it must also not be
    # swallowed silently: svv_table_info defaults to superuser-only
    # visibility in real Redshift, so the real, actionable cause (a
    # missing GRANT) is worth surfacing in the server logs rather than
    # discarding.
    responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
    )
    responses[3] = Exception("permission denied for relation svv_table_info")
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    with caplog.at_level("WARNING"):
        schema = backend.get_schema_shallow(conn)
    assert "Table: orders" in schema
    assert "Distribution/Sort Keys" not in schema
    assert "Row count estimates:" not in schema
    assert any(
        "svv_table_info query failed" in r.getMessage()
        and "GRANT SELECT ON svv_table_info" in r.getMessage()
        for r in caplog.records
    )


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


def test_get_schema_shallow_every_query_is_scoped_via_current_schema_not_hardcoded_public():
    """Regression guard for the "schema" descriptor feature (see
    backends/redshift.py's connect()): every one of
    _build_shallow_schema_parts()'s ten catalog-only queries must follow
    current_schema() rather than a literal 'public'. The one deliberate
    exception is the new session-timezone query (query #7):
    current_setting('TimeZone') describes the whole session, not a
    particular schema, so it has no current_schema() text to check - see
    backends/postgres.py's own identically-shaped test for the same
    exception."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        constraints=[("orders", "orders_pkey", "PRIMARY KEY", "id", None, None)],
        layout=[("orders", "KEY(customer_id)", "order_date")],
        views=[("v", "SELECT 1")],
        grants=[("app_user", "orders", "SELECT")],
    ))
    backend = RedshiftBackend()
    backend.get_schema_shallow(conn)
    assert len(cursor.calls) == 10
    session_calls = [c for c in cursor.calls if "current_setting" in c[0]]
    assert len(session_calls) == 1
    for sql_text, _params in cursor.calls:
        if sql_text == session_calls[0][0]:
            continue
        assert "current_schema()" in sql_text
        assert "'public'" not in sql_text


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_identity_column_marker_renders_with_seed_and_step():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[
            ("users", "id", "integer", "NO", '"identity"(387363, 0, \'1,1\'::text)'),
            ("users", "email", "character varying", "NO", None),
        ],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id integer NOT NULL IDENTITY(seed=1, step=1)" in schema
    # The raw internal identity-default expression must not be shown
    # verbatim once the human-readable marker already says the same thing.
    assert '"identity"(387363' not in schema
    email_line = [l for l in schema.splitlines() if l.strip().startswith("email")][0]
    assert "IDENTITY" not in email_line


def test_get_schema_shallow_identity_marker_falls_back_to_bare_marker_when_seed_step_unparseable():
    """A column_default that merely contains "identity" text without the
    documented "'<seed>,<step>'"-shaped substring still gets flagged as an
    identity column - see _identity_marker_from_column_default()'s
    docstring for why this degrades to a bare marker rather than silently
    missing the column altogether."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[("users", "id", "bigint", "NO", '"identity"(1, 0)')],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id bigint NOT NULL IDENTITY" in schema
    assert "IDENTITY(seed=" not in schema


def test_get_schema_shallow_identity_marker_absent_for_plain_default():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "status", "character varying", "NO", "'active'::character varying")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "IDENTITY" not in schema
    assert "DEFAULT 'active'" in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "character varying", "NO", None)],
        comments=[
            ("orders", None, "Customer purchase orders."),
            ("orders", "status", "Order lifecycle state."),
        ],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" in schema
    assert "[table] orders: Customer purchase orders." in schema
    assert "[column] orders.status: Order lifecycle state." in schema


def test_get_schema_shallow_comments_section_absent_when_no_comments():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        comments=[("orders", None, None), ("orders", "id", "")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_row_count_estimate_renders_from_widened_svv_table_info_query():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        layout=[("orders", "KEY(customer_id)", "order_date", 1234, 0)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "orders: ~1234 rows (estimate)" in schema
    assert "stats may be stale" not in schema
    # Same widened query still renders the pre-existing DISTSTYLE/SORTKEY
    # section too - this is one query, not two.
    assert "[orders] DISTSTYLE KEY(customer_id), SORTKEY(order_date)" in schema
    layout_calls = [c for c in cursor.calls if "svv_table_info" in c[0]]
    assert len(layout_calls) == 1
    assert "tbl_rows" in layout_calls[0][0]
    assert "stats_off" in layout_calls[0][0]


def test_get_schema_shallow_row_count_estimate_flags_stale_stats():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        layout=[("orders", None, None, 500, 42)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "orders: ~500 rows (estimate) (stats may be stale - 42% off since last ANALYZE)" in schema


def test_get_schema_shallow_row_count_estimate_absent_when_tbl_rows_is_none():
    """Pre-existing layout tuples (table, diststyle, sortkey1) pad
    tbl_rows/stats_off to None - see _pad_layout_row - so no existing test
    needs rewriting, and no misleading "~None rows" text is ever rendered."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        layout=[("orders", "KEY(customer_id)", "order_date")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" not in schema


def test_get_schema_shallow_routines_render_name_and_signature_without_body():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        routines=[("total_for_customer", "customer_id integer", "numeric", "SELECT SUM(amount) ...")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_for_customer(customer_id integer) -> numeric" in schema
    assert "SELECT SUM(amount)" not in schema


def test_get_schema_shallow_routines_query_error_is_swallowed():
    """Whether information_schema.routines/parameters behaves the same way
    on Redshift as it does on Postgres could not be verified from this
    sandbox (see module docstring) - a cluster/version where it errors must
    still get every other section."""
    # Replace the routines response with an Exception to simulate an
    # unsupported/erroring catalog view on this cluster.
    responses = _schema_responses(
        table_names=["t"],
        columns_rows=[("t", "id", "integer", "NO", None)],
    )
    routines_index = 6  # 0-based: table names, columns, constraints, layout, views, comments, routines
    responses[routines_index] = RuntimeError("routines catalog not supported")
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Table: t" in schema
    assert "Routines:" not in schema


def test_get_schema_shallow_session_timezone_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        session_settings=("America/New_York",),
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Session: timezone=America/New_York" in schema


def test_get_schema_shallow_grants_render_when_query_succeeds():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        grants=[("app_user", "orders", "SELECT")],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Grants" in schema
    assert "Grant SELECT on orders to app_user" in schema
    assert "role_table_grants support varies" in schema


def test_get_schema_shallow_grants_query_error_degrades_gracefully():
    """The exact concern the original module docstring raised (role_table_
    grants support is inconsistent across Redshift versions/configurations)
    - this test simulates that inconsistency actually manifesting as a
    query error, and confirms it degrades to "skip this section" rather
    than failing the whole schema fetch."""
    responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
    )
    grants_index = 8  # 0-based: ... comments, routines, session, grants
    responses[grants_index] = RuntimeError("permission denied for relation role_table_grants")
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Table: orders" in schema
    assert "Grants" not in schema


def test_get_schema_shallow_external_tables_render():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        external_tables=[("raw_events",)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "External tables (Redshift Spectrum):" in schema
    assert "raw_events: [external table - Redshift Spectrum]" in schema


def test_get_schema_shallow_external_tables_absent_when_none():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "External tables" not in schema


def test_get_schema_shallow_still_has_no_indexes_triggers_or_rls_sections():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["t"],
        columns_rows=[("t", "id", "integer", "NO", None)],
    ))
    backend = RedshiftBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Indexes:" not in schema
    assert "Triggers:" not in schema
    assert "Row-level security" not in schema


# --- get_schema_shallow() must never include Phase 2 (deep-only) content -----

def test_get_schema_shallow_excludes_full_view_and_routine_bodies_and_phase2_sections():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "integer", "NO", None),
            ("orders", "status", "character varying", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "p1 integer", "integer", "SELECT 1;")],
    ))
    backend = RedshiftBackend()
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
    # Exactly the ten Phase 1 queries - no Phase 2 query was ever issued.
    assert len(cursor.calls) == 10


# --- get_schema() (deep): Phase 2 additions on top of the shallow content ----

def _base_deep_responses():
    return _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "integer", "NO", None),
            ("orders", "status", "character varying", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "p1 integer", "integer", "SELECT 1;")],
        layout=[("orders", "KEY(id)", "id", 500, 0)],
    ) + [
        ([(500, 2_000_000, 1)], None, -1),  # new schema-wide dataset-size query
    ]


def _phase2_sampling_responses():
    return [
        ([("orders", "status", 5)], None, -1),                  # n_distinct (pg_stats)
        ([(42,)], None, -1),                                     # live count for orders
        ([(1, 100)], None, -1),                                  # min/max for id
        ([("active", 30), ("inactive", 12)], None, -1),          # frequent values for status
    ]


def test_get_schema_deep_is_superset_of_shallow_plus_phase2_sampling():
    conn, cursor = make_fake_pg_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)

    # Shallow content still present (Phase 1 catalog-only sections).
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "get_total(p1 integer) -> integer" in schema
    assert "~500 rows (estimate)" in schema

    # Phase 2 additions on top.
    assert "View definitions:" in schema and "View v: SELECT 1 FROM orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "status: frequent values = active (30), inactive (12)" in schema

    # New schema-wide dataset-size line (Dataset size summary section).
    assert "Estimated dataset size: ~1.9 MB" in schema

    assert len(cursor.calls) == 1 + 10 + 4


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    responses = _base_deep_responses() + [
        ([("orders", "status", -0.98)], None, -1),  # near-unique ratio
        ([(42,)], None, -1),                          # live count
        ([(1, 100)], None, -1),                        # min/max for id
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 1 + 10 + 3


def test_get_schema_deep_naming_convention_relationships_section():
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "bigint", "NO", None),
            ("orders", "customer_id", "bigint", "NO", None),
        ],
    ) + [
        ([(30, 3_000, 2)], None, -1),  # new schema-wide dataset-size query
        ([], None, -1),        # n_distinct (no categorical cols to gate)
        ([(10,)], None, -1),   # live count: customers
        ([(1, 10)], None, -1),  # min/max: customers.id
        ([(20,)], None, -1),   # live count: orders
        ([(1, 20)], None, -1),  # min/max: orders.customer_id
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "orders.customer_id -> likely relationship (unconfirmed): references customers" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_pg_connection(_schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "bigint", "NO", None),
            ("orders", "customer_id", "bigint", "NO", None),
        ],
    ))
    shallow = backend.get_schema_shallow(conn2)
    assert "Likely relationships" not in shallow


def test_get_schema_deep_skips_sampling_for_wide_tables_but_keeps_live_count():
    """A table with more columns than MAX_COLUMNS_FOR_SAMPLING still gets a
    live row count, just no per-column sampling - bounding the "explosion of
    tiny queries" the cap exists to prevent."""
    columns_rows = [
        ("wide", f"col_{i}", "integer", "NO", None)
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["wide"],
        columns_rows=columns_rows,
    ) + [
        ([(7, 1_000, 1)], None, -1),  # new schema-wide dataset-size query
        ([], None, -1),       # n_distinct
        ([(7,)], None, -1),   # live count for wide
        # no min/max response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "wide: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 1 + 10 + 2


def test_get_schema_deep_dataset_size_line_uses_schema_wide_totals_not_kept_names_scope():
    """The new dataset-size query aggregates svv_table_info over EVERY
    table in current_schema() (no table-name filter) - unlike the
    neighboring "diststyle/sortkey1/tbl_rows" layout query (Phase 1), which
    is deliberately scoped to kept_names (the capped/bounded subset of
    tables) via `"table" = ANY(%s)`. Asserts on the actual SQL text/params
    executed rather than behavior alone, since a kept_names-scoped total
    would still "work" but silently misreport genuine schema-wide scale for
    any schema where table capping kicked in."""
    conn, cursor = make_fake_pg_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = RedshiftBackend()
    backend.get_schema(conn)

    dataset_size_calls = [c for c in cursor.calls if "CAST(size AS BIGINT)" in c[0]]
    assert len(dataset_size_calls) == 1
    sql_text, params = dataset_size_calls[0]
    assert params is None
    assert "ANY(%s)" not in sql_text

    # Contrast with the neighboring per-table layout/row-count-estimate
    # query (Phase 1), which DOES filter by table name via kept_names.
    layout_calls = [c for c in cursor.calls if "diststyle, sortkey1" in c[0]]
    assert len(layout_calls) == 1
    layout_sql, layout_params = layout_calls[0]
    assert "ANY(%s)" in layout_sql
    assert layout_params is not None


def test_get_schema_deep_dataset_size_query_failure_does_not_break_the_rest_of_the_fetch(caplog):
    """The dataset-size query is best-effort (try/except-wrapped) - a
    failure there must not take down the rest of the deep fetch, and must
    simply produce no "Estimated dataset size:" line rather than a partial
    or garbled one. It must also not be swallowed silently - same
    real-world cause (svv_table_info needing an explicit GRANT) and same
    logging as the Phase 1 svv_table_info query failure covered by
    test_get_schema_svv_table_info_query_failure_is_logged_not_swallowed
    above, just for this separate schema-wide aggregate query."""
    schema_responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "integer", "NO", None),
            ("orders", "status", "character varying", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "p1 integer", "integer", "SELECT 1;")],
        layout=[("orders", "KEY(id)", "id", 500, 0)],
    )
    responses = schema_responses + [Exception("boom")] + _phase2_sampling_responses()
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)

    # Every other section is still present and complete.
    assert "Table: orders" in schema
    assert "View v" in schema
    assert "View definitions:" in schema and "View v: SELECT 1 FROM orders" in schema
    assert "Routine definitions:" in schema and "get_total: SELECT 1;" in schema
    assert "Live row counts:" in schema and "orders: 42 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema

    # ...but no dataset-size line at all.
    assert "Estimated dataset size" not in schema
    assert any(
        "svv_table_info dataset-size query failed" in r.getMessage()
        and "GRANT SELECT ON svv_table_info" in r.getMessage()
        for r in caplog.records
    )


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


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    conn, cursor = make_fake_pg_connection([
        (None, None, 1),
        RuntimeError('syntax error at or near "bad"'),
    ])
    backend = RedshiftBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert 'syntax error at or near "bad"' in str(err)


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale - this just proves RedshiftBackend routes through the same
# shared fetch_capped_rows() (backends/base.py) instead of its own
# fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = RedshiftBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]
