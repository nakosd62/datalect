"""
backends/postgres.py, driven entirely against a fake psycopg2-shaped
connection/cursor (see helpers.make_fake_pg_connection) - no real Postgres
needed. get_schema()'s query order is unconditional (unlike BigQuery's
try/except-guarded optional sections), so responses are queued in the
exact order PostgresBackend.get_schema() issues them:
  1. table names   2. columns   3. constraints   4. indexes
  5. views         6. grants    7. triggers
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
