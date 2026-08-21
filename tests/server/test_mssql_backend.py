"""
backends/mssql.py, driven two ways:
  - connect(): against the fake pytds.connect harness
    (helpers.install_fake_mssql_connect) - verifies the core kwargs, the
    encrypt-flag-to-cafile dispatch, required-field validation, and that the
    descriptor's "schema" value is stashed on the returned connection
    (mssql_schema) rather than applied via any session-level statement -
    without opening a real connection.
  - get_schema()/execute()/identity_label()/cache_key(): against the same
    psycopg2-shaped fake cursor/connection tests/test_postgres_backend.py
    uses (helpers.make_fake_mssql_connection, itself built on FakePgCursor) -
    pytds implements the same PEP 249 DB-API cursor shape, so no
    mssql-specific cursor fake is needed for these, just a connection
    wrapper that also carries the mssql_schema attribute get_schema() reads.

get_schema()'s query order is unconditional for tables/columns, then
best-effort (try/except) for constraints/views - see backends/mssql.py:
  1. table names   2. columns   3. constraints (best-effort)   4. views (best-effort)
No indexes/grants/triggers queries at all (deferred, same status
backends/oracle.py's/backends/redshift.py's own first-pass gaps have).

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
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.mssql import MssqlBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS
from helpers import install_fake_mssql_connect, make_fake_mssql_connection


def _ms(monkeypatch):
    harness = install_fake_mssql_connect(monkeypatch)
    return MssqlBackend(), harness


def _schema_responses(table_names, columns_rows, constraints=(), views=()):
    return [
        ([(n,) for n in table_names], None, -1),
        (columns_rows, None, -1),
        (list(constraints), None, -1),
        (list(views), None, -1),
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
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
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
