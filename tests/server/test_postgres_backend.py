"""
backends/postgres.py, driven entirely against a fake psycopg2-shaped
connection/cursor (see helpers.make_fake_pg_connection) - no real Postgres
needed.

_build_shallow_schema_parts() (called by both get_schema_shallow() and
get_schema()) issues its queries unconditionally and in a fixed order
(unlike BigQuery's try/except-guarded optional sections, though several of
*these* new sections are individually try/except-wrapped for graceful
degradation - see postgres.py itself), so responses are queued in the exact
order it issues them:
  1. table names        2. columns            3. constraints
  4. indexes             5. views               6. grants
  7. triggers            8. comments (new)      9. row count estimates (new)
  10. routines (new)     11. session settings (new)
  12. RLS/federation flags (new)

get_schema() (deep) then runs _build_shallow_schema_parts() (the twelve
queries above) and appends its own Phase 2 queries on a fresh cursor use:
  13. pg_stats n_distinct (shared, once)
  then per kept table (in order): live COUNT(*), an optional combined
  MIN()/MAX() query (if it has numeric/date columns), and up to
  MAX_CATEGORICAL_SAMPLE_COLUMNS_PER_TABLE frequent-value GROUP BY queries
  (if it has eligible categorical columns) - see test_get_schema_deep_*
  below for worked examples of this second phase's exact response queue.
"""

import os
import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.postgres import PostgresBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
from helpers import make_fake_pg_connection, install_fake_postgres_connect


def _pad_column_row(row):
    """A columns_rows tuple may still be the pre-identity 5-tuple
    (table_name, column_name, data_type, is_nullable, column_default) that
    every test predating the identity-column feature already uses - padded
    here to the real 7-column shape
    _build_shallow_schema_parts()'s columns query now selects
    (... , is_identity, identity_generation), defaulting to "NO"/None (not
    an identity column), so none of those existing tests need to be
    rewritten just because two more columns joined the SELECT list."""
    row = list(row)
    while len(row) < 7:
        row.append("NO" if len(row) == 5 else None)
    return tuple(row)


def _schema_responses(
    table_names, columns_rows, constraints=(), indexes=(), views=(), grants=(), triggers=(),
    comments=(), row_count_estimates=(), routines=(), session_settings=("UTC", "en_US.UTF-8"),
    rls_flags=(),
):
    return [
        ([(n,) for n in table_names], None, -1),
        ([_pad_column_row(r) for r in columns_rows], None, -1),
        (list(constraints), None, -1),
        (list(indexes), None, -1),
        (list(views), None, -1),
        (list(grants), None, -1),
        (list(triggers), None, -1),
        (list(comments), None, -1),
        (list(row_count_estimates), None, -1),
        (list(routines), None, -1),
        ([session_settings] if session_settings is not None else [], None, -1),
        (list(rls_flags), None, -1),
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


# Regression coverage for a real bug a user hit in production: a per-dataset
# "connect_timeout_seconds" override (resolve_timeout_seconds() - see
# backends/base.py) always returns a float when an override is actually set,
# and psycopg2 stringifies whatever's passed as connect_timeout straight into
# the DSN it hands libpq - which then rejects a float like "60.0" outright
# with "invalid integer value ... for connection option \"connect_timeout\""
# before ever dialing out. An equality check alone (`== 60`) would NOT have
# caught this, since 60 == 60.0 in Python - this asserts the actual type
# psycopg2 receives, not just its numeric value.
def test_connect_timeout_override_is_passed_as_a_real_int_not_a_float(monkeypatch):
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({
        "type": "postgres", "url": "postgresql://alice:secret@host:5432/mydb",
        "connect_timeout_seconds": 60,
    })
    dsn, kwargs = harness.calls[0]
    assert kwargs["connect_timeout"] == 60
    assert isinstance(kwargs["connect_timeout"], int)


# --- connect(): ca_cert_pem -> sslrootcert ----------------------------------
# Coverage for the "verify-ca"/"verify-full" CA-certificate support added to
# connect() - see backends/postgres.py's module docstring. sslmode itself is
# never touched by connect() (the user's own "?sslmode=..." in the URL is
# what actually turns verification on) - these tests only cover the
# ca_cert_pem -> sslrootcert tempfile plumbing.

def test_connect_with_no_ca_cert_pem_behaves_exactly_as_before(monkeypatch):
    """Regression guard: a descriptor with no ca_cert_pem at all (the
    overwhelming common case, and every existing connection prior to this
    feature) must produce byte-identical connect() behavior - no
    "sslrootcert" kwarg, nothing extra."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({"type": "postgres", "url": "postgresql://alice:secret@host:5432/mydb"})
    dsn, kwargs = harness.calls[0]
    assert "sslrootcert" not in kwargs
    assert harness.sslrootcert_contents[0] is None


def test_connect_with_ca_cert_pem_writes_it_to_sslrootcert(monkeypatch):
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    ca_cert_pem = "-----BEGIN CERTIFICATE-----\nFAKEFAKEFAKE\n-----END CERTIFICATE-----\n"
    backend.connect({
        "type": "postgres",
        "url": "postgresql://alice:secret@host:5432/mydb",
        "ca_cert_pem": ca_cert_pem,
    })
    dsn, kwargs = harness.calls[0]
    assert dsn == "postgresql://alice:secret@host:5432/mydb"
    assert "sslrootcert" in kwargs
    # The exact PEM text the caller supplied must have reached the file
    # connect() pointed sslrootcert at - captured by the fake at call time
    # (see FakePostgresConnectHarness's docstring for why it can't be read
    # back from disk afterward).
    assert harness.sslrootcert_contents[0] == ca_cert_pem


def test_connect_ca_cert_pem_tempfile_is_deleted_after_connect(monkeypatch):
    """The tempfile connect() writes ca_cert_pem to must not linger on disk
    once connect() has returned - it's derived from user-pasted PEM text
    and is only ever needed for the handshake inside psycopg2.connect()
    itself (see backends/postgres.py's connect() comments)."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({
        "type": "postgres",
        "url": "postgresql://alice:secret@host:5432/mydb",
        "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n",
    })
    _, kwargs = harness.calls[0]
    assert not os.path.exists(kwargs["sslrootcert"])


def test_connect_ca_cert_pem_ignored_when_url_already_specifies_sslrootcert(monkeypatch):
    """A self-hoster who already points sslrootcert at a file on their own
    machine (see backends/postgres.py's _url_already_specifies_sslrootcert
    docstring) must never have that silently overridden by a separately
    stored ca_cert_pem - their own explicit URL always wins."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({
        "type": "postgres",
        "url": "postgresql://alice:secret@host:5432/mydb?sslmode=verify-full&sslrootcert=/etc/ydyl/ca.pem",
        "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n",
    })
    dsn, kwargs = harness.calls[0]
    # connect() must not have injected its own sslrootcert kwarg at all -
    # psycopg2/libpq resolves it from the URL's own query string instead.
    assert "sslrootcert" not in kwargs
    assert dsn == "postgresql://alice:secret@host:5432/mydb?sslmode=verify-full&sslrootcert=/etc/ydyl/ca.pem"


def test_connect_with_no_descriptor_url_still_works(monkeypatch):
    """connect({}) (or connect(None)) must not raise just because
    ca_cert_pem support now reads descriptor.get("url") up front instead of
    only ever using descriptor["url"] positionally."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({})
    dsn, kwargs = harness.calls[0]
    assert dsn is None
    assert "sslrootcert" not in kwargs


# --- connect(): optional "schema" field --------------------------------------
# Mirrors backends/redshift.py's own "schema" descriptor field/test coverage -
# see backends/postgres.py's connect() for the SET search_path mechanism.

def test_connect_with_no_schema_runs_no_search_path_statement(monkeypatch):
    """Regression guard: a descriptor with no "schema" at all (every preset
    from before this feature existed, and the overwhelming common case)
    must not touch connection.cursor()/commit() at all - byte-identical to
    the old behavior."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({"type": "postgres", "url": "postgresql://alice:secret@host:5432/mydb"})
    connection = harness.connections[0]
    assert connection.search_path_calls == []
    assert connection.committed is False


def test_connect_with_schema_sets_search_path_and_commits(monkeypatch):
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({
        "type": "postgres",
        "url": "postgresql://alice:secret@host:5432/mydb",
        "schema": "golf",
    })
    connection = harness.connections[0]
    assert connection.search_path_calls == ['SET search_path TO "golf", public']
    assert connection.committed is True


def test_connect_with_blank_schema_runs_no_search_path_statement(monkeypatch):
    """An explicit but empty "schema" (e.g. "" from a stripped, all-blank
    admin-preset field) must behave the same as no "schema" key at all -
    not attempt `SET search_path TO "", public`."""
    harness = install_fake_postgres_connect(monkeypatch)
    backend = PostgresBackend()
    backend.connect({
        "type": "postgres",
        "url": "postgresql://alice:secret@host:5432/mydb",
        "schema": "",
    })
    connection = harness.connections[0]
    assert connection.search_path_calls == []
    assert connection.committed is False


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


def test_get_schema_survives_view_with_null_definition():
    # Regression test: Postgres returns a real SQL NULL (not "") for
    # information_schema.views.view_definition when the connected role
    # lacks the privilege to see a given view's definition - the bare
    # v[1].strip() this used to be raised AttributeError on that None and
    # aborted schema fetch for the WHOLE database (see db.py's
    # _fetch_database_schema, which lets this exception propagate as a 500
    # rather than a partial schema). One unreadable view must not take
    # down every other table/view this schema fetch would otherwise
    # successfully report - matches every other backend's own
    # `(v[1] or '').strip()` guard (see this fix's comment in
    # postgres.py's get_schema for the full list).
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "integer", "NO", None)],
        views=[("restricted_view", None), ("customer_orders", "SELECT * FROM orders")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "View restricted_view: " in schema
    assert "View customer_orders: SELECT * FROM orders" in schema


def test_get_schema_reindents_a_multiline_view_definition_rather_than_leaving_it_as_is():
    # pg_get_viewdef(oid, true) (what this now queries - see get_schema's
    # own comment on switching away from information_schema.views.
    # view_definition) pretty-prints real views across several lines, not
    # one - format_multiline_schema_entry_body() (backends/base.py) must
    # reindent every continuation line so webClient's Schema Viewer can
    # tell it apart from the start of the NEXT top-level schema section
    # (see that function's own docstring). A raw, un-reindented multi-line
    # body would still render here today (this backend doesn't parse its
    # own output back apart), but the exact reindented shape is what makes
    # the client-side parsing round-trip correctly - see
    # tests/e2e/schema-viewer.spec.js's own multi-line view definition
    # test for that half of this fix.
    raw_definition = "SELECT a.id,\n   a.name\n  FROM a;"
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["a"],
        columns_rows=[("a", "id", "integer", "NO", None)],
        views=[("v", raw_definition)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    # Each continuation line keeps its own original indentation, plus the
    # four guaranteed extra spaces format_multiline_schema_entry_body()
    # adds on top of it (see that function's own test coverage in
    # test_backend_base_helpers.py) - not flattened to one fixed amount.
    assert "View v: SELECT a.id,\n       a.name\n      FROM a;" in schema


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


def test_get_schema_shallow_every_query_is_scoped_via_current_schema_not_hardcoded_public():
    """Regression guard for the "schema" descriptor feature (see
    backends/postgres.py's connect()): every one of
    _build_shallow_schema_parts()'s twelve catalog-only queries must follow
    current_schema() - which reflects wherever connect()'s own `SET
    search_path` pointed, or plain 'public' when no override was ever set -
    rather than a literal 'public' that could never see a non-public schema
    regardless of what connect() did. A single query still hardcoding
    'public' would silently keep introspecting the default schema even once
    search_path had been overridden.

    The one deliberate exception is the new session-settings query (query
    #11): current_setting('TimeZone')/pg_database.datcollate describe the
    whole session/database, not a particular schema, so it has no
    current_schema() text to check - see postgres.py's own comment on that
    section."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        constraints=[("orders", "orders_pkey", "PRIMARY KEY", "id", None, None)],
        indexes=[("orders", "orders_pkey", "CREATE UNIQUE INDEX orders_pkey ON orders(id)")],
        views=[("v", "SELECT 1")],
        grants=[("app_user", "orders", "SELECT")],
        triggers=[("orders", "trg", "INSERT", "EXECUTE FUNCTION f()")],
    ))
    backend = PostgresBackend()
    backend.get_schema_shallow(conn)
    assert len(cursor.calls) == 12
    session_settings_calls = [c for c in cursor.calls if "current_setting" in c[0]]
    assert len(session_settings_calls) == 1
    for sql_text, _params in cursor.calls:
        if sql_text == session_settings_calls[0][0]:
            continue
        assert "current_schema()" in sql_text
        assert "'public'" not in sql_text


# --- Phase 1 (catalog-only, shallow) new attributes --------------------------

def test_get_schema_shallow_identity_column_marker_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[
            ("users", "id", "integer", "NO", None, "YES", "ALWAYS"),
            ("users", "email", "text", "NO", None, "NO", None),
        ],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id integer NOT NULL IDENTITY (ALWAYS)" in schema
    assert "email text NOT NULL" in schema
    # The non-identity column's own line must not pick up a marker.
    email_line = [l for l in schema.splitlines() if l.strip().startswith("email")][0]
    assert "IDENTITY" not in email_line


def test_get_schema_shallow_identity_marker_absent_by_default():
    """Old-style 5-tuple columns_rows (predating is_identity/
    identity_generation) must be padded to "not an identity column" -
    see _pad_column_row - so no existing test needs rewriting."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "integer", "NO", None)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "IDENTITY" not in schema


def test_get_schema_shallow_identity_marker_without_generation_type():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["users"],
        columns_rows=[("users", "id", "integer", "NO", None, "YES", None)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "id integer NOT NULL IDENTITY" in schema
    assert "IDENTITY (" not in schema


def test_get_schema_shallow_comments_render_table_and_column():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "text", "NO", None)],
        comments=[
            ("orders", None, "Customer purchase orders."),
            ("orders", "status", "Order lifecycle state."),
        ],
    ))
    backend = PostgresBackend()
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
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Comments:" not in schema


def test_get_schema_shallow_row_count_estimate_renders():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        row_count_estimates=[("orders", 1234.0)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "orders: ~1234 rows (estimate)" in schema


def test_get_schema_shallow_row_count_estimate_skips_never_analyzed_table():
    """reltuples reports -1 (or a caller-crafted None) for a table that has
    never been ANALYZEd - rendering "~-1 rows" would be actively
    misleading, so that row is skipped rather than shown."""
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["fresh_table"],
        columns_rows=[("fresh_table", "id", "integer", "NO", None)],
        row_count_estimates=[("fresh_table", -1.0)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" not in schema


def test_get_schema_shallow_routines_render_name_and_signature_without_body():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        routines=[("total_for_customer", "customer_id integer", "numeric", "SELECT SUM(amount) ...")],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_for_customer(customer_id integer) -> numeric" in schema
    assert "SELECT SUM(amount)" not in schema


def test_get_schema_shallow_session_settings_render():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "integer", "NO", None)],
        session_settings=("America/New_York", "en_US.UTF-8"),
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Session: timezone=America/New_York; default collation=en_US.UTF-8" in schema


def test_get_schema_shallow_rls_and_foreign_table_flags_render_when_true():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["accounts", "remote_orders"],
        columns_rows=[
            ("accounts", "id", "integer", "NO", None),
            ("remote_orders", "id", "integer", "NO", None),
        ],
        rls_flags=[
            ("accounts", True, "r", True),
            ("remote_orders", False, "f", False),
        ],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / federation:" in schema
    assert "accounts: [RLS enabled]" in schema
    assert "remote_orders: [foreign table]" in schema


def test_get_schema_shallow_rls_enabled_with_no_policies_is_flagged_as_deny_all():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["accounts"],
        columns_rows=[("accounts", "id", "integer", "NO", None)],
        rls_flags=[("accounts", True, "r", False)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "accounts: [RLS enabled, no policies - effectively deny-all]" in schema


def test_get_schema_shallow_rls_section_absent_when_all_flags_false():
    conn, cursor = make_fake_pg_connection(_schema_responses(
        table_names=["accounts"],
        columns_rows=[("accounts", "id", "integer", "NO", None)],
        rls_flags=[("accounts", False, "r", False)],
    ))
    backend = PostgresBackend()
    schema = backend.get_schema_shallow(conn)
    assert "Row-level security / federation:" not in schema


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
    backend = PostgresBackend()
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
            ("orders", "id", "integer", "NO", None),
            ("orders", "status", "character varying", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "p1 integer", "integer", "SELECT 1;")],
        row_count_estimates=[("orders", 500.0)],
    ) + [
        ([(500.0, 2_000_000, 1)], None, -1),  # new schema-wide dataset-size query
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
    backend = PostgresBackend()
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

    assert len(cursor.calls) == 1 + 12 + 4


def test_get_schema_deep_skips_frequent_values_for_near_unique_column():
    """pg_stats.n_distinct as a ratio (negative) close to -1 means the
    column is nearly unique - sampling "frequent values" for it wouldn't be
    meaningful, so that column's GROUP BY query must never even be issued."""
    responses = _base_deep_responses() + [
        ([("orders", "status", -0.98)], None, -1),  # near-unique ratio
        ([(42,)], None, -1),                          # live count
        ([(1, 100)], None, -1),                        # min/max for id
        # no frequent-value response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Column value samples:" in schema
    assert "id: range [1 .. 100]" in schema
    assert "frequent values" not in schema
    assert len(cursor.calls) == 1 + 12 + 3


def test_get_schema_deep_naming_convention_relationships_section():
    responses = _schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "bytea", "NO", None),
            ("orders", "customer_id", "bytea", "NO", None),
        ],
    ) + [
        ([(30.0, 3_000, 2)], None, -1),  # new schema-wide dataset-size query
        ([], None, -1),        # n_distinct (no categorical/numeric cols to gate)
        ([(10,)], None, -1),   # live count: customers
        ([(20,)], None, -1),   # live count: orders
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()

    deep = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in deep
    assert "orders.customer_id -> likely relationship (unconfirmed): references customers" in deep

    # The shallow fetch (fresh cursor/queue) must not include this section.
    conn2, cursor2 = make_fake_pg_connection(_schema_responses(
        table_names=["customers", "orders"],
        columns_rows=[
            ("customers", "id", "bytea", "NO", None),
            ("orders", "customer_id", "bytea", "NO", None),
        ],
    ))
    shallow = backend.get_schema_shallow(conn2)
    assert "Likely relationships" not in shallow


def test_get_schema_deep_skips_sampling_for_wide_tables_but_keeps_live_count():
    """A table with more columns than MAX_COLUMNS_FOR_SAMPLING still gets a
    live row count, just no per-column sampling - bounding the "explosion of
    tiny queries" the cap exists to prevent."""
    from backends.postgres import MAX_COLUMNS_FOR_SAMPLING

    columns_rows = [
        ("wide", f"col_{i}", "integer", "NO", None)
        for i in range(MAX_COLUMNS_FOR_SAMPLING + 1)
    ]
    responses = _schema_responses(
        table_names=["wide"],
        columns_rows=columns_rows,
    ) + [
        ([(7.0, 1_000, 1)], None, -1),  # new schema-wide dataset-size query
        ([], None, -1),       # n_distinct
        ([(7,)], None, -1),   # live count for wide
        # no min/max response queued - it must not be requested
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema and "wide: 7 rows (live, authoritative)" in schema
    assert "Column value samples:" not in schema
    assert len(cursor.calls) == 1 + 12 + 2


def test_get_schema_deep_dataset_size_line_uses_schema_wide_totals_not_kept_names_scope():
    """The new dataset-size query aggregates over EVERY base table in
    current_schema() (pg_class/pg_namespace, no table-name filter) - unlike
    the neighboring per-table "Row count estimates" query (Phase 1), which
    is deliberately scoped to kept_names (the capped/bounded subset of
    tables). Asserts on the actual SQL text/params executed rather than
    behavior alone, since a kept_names-scoped total would still "work" but
    silently misreport genuine schema-wide scale for any schema where table
    capping kicked in."""
    conn, cursor = make_fake_pg_connection(_base_deep_responses() + _phase2_sampling_responses())
    backend = PostgresBackend()
    backend.get_schema(conn)

    dataset_size_calls = [c for c in cursor.calls if "pg_total_relation_size" in c[0]]
    assert len(dataset_size_calls) == 1
    sql_text, params = dataset_size_calls[0]
    assert params is None
    assert "ANY(%s)" not in sql_text

    # Contrast with the neighboring per-table row-count-estimate query
    # (Phase 1), which DOES filter by table name via kept_names.
    row_estimate_calls = [c for c in cursor.calls if "c.relname, c.reltuples" in c[0]]
    assert len(row_estimate_calls) == 1
    row_estimate_sql, row_estimate_params = row_estimate_calls[0]
    assert "ANY(%s)" in row_estimate_sql
    assert row_estimate_params is not None


def test_get_schema_deep_dataset_size_query_failure_does_not_break_the_rest_of_the_fetch():
    """The dataset-size query is best-effort (try/except-wrapped) - a
    failure there must not take down the rest of the deep fetch, and must
    simply produce no "Estimated dataset size:" line rather than a partial
    or garbled one."""
    schema_responses = _schema_responses(
        table_names=["orders"],
        columns_rows=[
            ("orders", "id", "integer", "NO", None),
            ("orders", "status", "character varying", "NO", None),
        ],
        views=[("v", "SELECT 1 FROM orders")],
        routines=[("get_total", "p1 integer", "integer", "SELECT 1;")],
        row_count_estimates=[("orders", 500.0)],
    )
    responses = schema_responses + [Exception("boom")] + _phase2_sampling_responses()
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
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


# --- cache_key ---------------------------------------------------------------

def test_cache_key_parses_username_host_port_and_dbname():
    backend = PostgresBackend()
    key = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    assert key == "alice@host:5432/mydb"
    assert "secret" not in key


def test_cache_key_differs_across_hosts_with_same_user_and_dbname():
    # Regression coverage for the real bug this fixes: two entirely
    # different Postgres servers can easily share both a username and a
    # database name (e.g. two "demo"/"mydb" presets pointing at two
    # different customers' instances) - the old host-blind derivation
    # ("alice@mydb" for both) would collide them onto the same
    # schema_cache.py entry, silently serving one server's schema back for
    # the other's /api/translate calls.
    backend = PostgresBackend()
    key_a = backend.cache_key({"url": "postgresql://alice:secret@server-a.example.com:5432/mydb"})
    key_b = backend.cache_key({"url": "postgresql://alice:secret@server-b.example.com:5432/mydb"})
    assert key_a != key_b


def test_cache_key_differs_across_ports_on_the_same_host():
    # Same failure mode as the cross-host case above, but for two distinct
    # instances reachable on the same host at different ports (e.g. local
    # Docker containers each mapped to a different host port).
    backend = PostgresBackend()
    key_5432 = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    key_5433 = backend.cache_key({"url": "postgresql://alice:secret@host:5433/mydb"})
    assert key_5432 != key_5433


def test_cache_key_defaults_port_to_5432_when_omitted():
    # An omitted port and an explicit ":5432" name the same target - same
    # default psycopg2/libpq themselves fall back to - so these must
    # produce the identical key, not two different ones.
    backend = PostgresBackend()
    key_explicit = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    key_omitted = backend.cache_key({"url": "postgresql://alice:secret@host/mydb"})
    assert key_explicit == key_omitted == "alice@host:5432/mydb"


def test_cache_key_strips_query_string_from_dbname():
    backend = PostgresBackend()
    key = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb?sslmode=require"})
    assert key == "alice@host:5432/mydb"


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


def test_cache_key_appends_schema_when_present():
    # Two presets on the exact same host/port/dbname/user but pointed at
    # different schemas must not collide on schema_cache.py - the same
    # failure mode test_cache_key_differs_across_hosts_with_same_user_and_dbname
    # covers for host, applied to the new "schema" descriptor field.
    backend = PostgresBackend()
    key_golf = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb", "schema": "golf"})
    key_public = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb", "schema": "public"})
    key_none = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    assert key_golf == "alice@host:5432/mydb.golf"
    assert key_public == "alice@host:5432/mydb.public"
    assert key_none == "alice@host:5432/mydb"
    assert len({key_golf, key_public, key_none}) == 3


def test_cache_key_with_no_schema_is_byte_identical_to_before_this_feature():
    """Regression guard: every preset that predates the "schema" field must
    keep producing the exact same key it always did - no ".public" or any
    other suffix silently appended just because the field now exists."""
    backend = PostgresBackend()
    key = backend.cache_key({"url": "postgresql://alice:secret@host:5432/mydb"})
    assert key == "alice@host:5432/mydb"


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


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results():
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature: a failure on statement 2 of 3
    must not silently discard statement 1's already-collected result -
    see SqlExecutionError's docstring in backends/base.py."""
    responses = [
        ([], None, 1),  # statement 1 succeeds
        RuntimeError('syntax error at or near "SELEC"'),  # statement 2 fails
    ]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE users SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.results[0]["statement"] == "UPDATE users SET x=1"
    assert err.results[0]["rowCount"] == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert 'syntax error at or near "SELEC"' in str(err)


def test_execute_ignores_blank_statements_between_semicolons():
    responses = [([], None, 0)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT 1;;;")
    # sqlparse.split + the blank-statement guard should collapse the
    # trailing empty statements down to just the one real query.
    assert len(results) == 1


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# Regression coverage for the crash this cap exists to prevent - see
# backends/base.py's EXECUTE_RESULTS_MAX_ROWS/fetch_capped_rows docstrings.
# PostgresBackend.execute() must go through fetch_capped_rows() (a bare
# cursor.fetchall() on a real result set is exactly the unbounded-memory
# failure mode being fixed), so these exercise that wiring specifically,
# against the REAL default cap - fetch_capped_rows()'s own cap/truncation-
# detection logic is covered exhaustively (every row count relative to the
# cap) in test_backend_base_helpers.py; this just proves Postgres actually
# routes through it instead of its own inline fetchall() loop.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit():
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    rows = [(i,) for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    responses = [(rows, [("n",)], EXECUTE_RESULTS_MAX_ROWS + 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated():
    # "truncated" must be entirely ABSENT (not a present-but-False key) for
    # an ordinary, un-truncated result - see Backend.execute()'s own
    # docstring on why (mirrors the existing "notices" key's convention).
    responses = [([(1, "Alice")], [("id",), ("name",)], 1)]
    conn, cursor = make_fake_pg_connection(responses)
    backend = PostgresBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert "truncated" not in results[0]
