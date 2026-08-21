"""
backends/mysql.py, driven entirely against a fake PyMySQL-shaped
connection/cursor (see helpers.make_fake_mysql_connection) - no real MySQL
needed. get_schema()'s query order is unconditional, same staging as
backends/postgres.py's get_schema():
  1. table names   2. columns   3. constraints   4. indexes
  5. views         6. grants    7. triggers

connect()'s own URL-parsing/kwarg-building logic is tested separately
against helpers.install_fake_pymysql_connect, which patches
backends.mysql's pymysql.connect() and records the kwargs it was called
with - mirroring how backends/snowflake.py's connect() dispatch is tested.
"""

import sys
from decimal import Decimal
from datetime import date

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.mysql import MySQLBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS
from helpers import make_fake_mysql_connection, install_fake_pymysql_connect


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


# --- get_schema ----------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables():
    conn, cursor = make_fake_mysql_connection([([], None, -1)])
    backend = MySQLBackend()
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_tables_with_columns():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[
            ("customers", "id", "int", "NO", None),
            ("customers", "name", "varchar", "YES", None),
        ],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id int NOT NULL" in schema
    assert "name varchar NULL" in schema


def test_get_schema_includes_column_default():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "status", "varchar", "NO", "pending")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "DEFAULT pending" in schema


def test_get_schema_collapses_date_sharded_family():
    members = [f"events_2024010{i}" for i in range(1, 6)]
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=members,
        columns_rows=[(members[-1], "id", "int", "NO", None)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Table family: events_<date>" in schema
    assert "5 date-sharded tables" in schema
    assert f"{members[0]} .. {members[-1]}" in schema
    assert "Table: events_20240102" not in schema


def test_get_schema_views_section_is_not_scoped_to_kept_names():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["customers"],
        columns_rows=[("customers", "id", "int", "NO", None)],
        views=[("customer_orders", "select * from orders join customers ...")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_indexes_grants_triggers():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "id", "int", "NO", None)],
        constraints=[("orders", "PRIMARY", "PRIMARY KEY", "id", None, None)],
        indexes=[("orders", "PRIMARY", "id", 0, 1)],
        grants=[("app_user@%", "orders", "SELECT")],
        triggers=[("orders", "trg_audit", "INSERT", "CALL audit()")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema and "PRIMARY" in schema
    assert "Indexes:" in schema and "UNIQUE" in schema and "id" in schema
    assert "Grants:" in schema and "Grant SELECT on orders to app_user@%" in schema
    assert "Triggers:" in schema and "trg_audit" in schema


def test_get_schema_non_unique_index_labeled_index_not_unique():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        indexes=[("orders", "idx_customer", "customer_id", 1, 1)],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "idx_customer (INDEX): customer_id" in schema


def test_get_schema_multi_column_index_lists_all_columns():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "a", "int", "NO", None)],
        indexes=[
            ("orders", "idx_ab", "a", 1, 1),
            ("orders", "idx_ab", "b", 1, 2),
        ],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "idx_ab (INDEX): a, b" in schema


def test_get_schema_foreign_key_constraint_format():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["orders"],
        columns_rows=[("orders", "customer_id", "int", "NO", None)],
        constraints=[("orders", "orders_customer_fk", "FOREIGN KEY", "customer_id", "customers", "id")],
    ))
    backend = MySQLBackend()
    schema = backend.get_schema(conn)
    assert "customer_id -> customers(id)" in schema


def test_get_schema_scan_query_uses_configured_scan_cap():
    conn, cursor = make_fake_mysql_connection(_schema_responses(
        table_names=["t1"],
        columns_rows=[("t1", "id", "int", "NO", None)],
    ))
    backend = MySQLBackend()
    backend.get_schema(conn)
    first_sql, first_params = cursor.calls[0]
    assert "information_schema.TABLES" in first_sql
    assert first_params[0] > 0  # SCHEMA_MAX_TABLE_NAMES_SCANNED


# --- cache_key -------------------------------------------------------------------

def test_cache_key_parses_user_and_database():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb"})
    assert key == "alice@mydb"
    assert "secret" not in key


def test_cache_key_percent_decodes_credentials():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://ali%40ce:secret@host:3306/mydb"})
    assert key == "ali@ce@mydb"


def test_cache_key_strips_query_string_from_database():
    backend = MySQLBackend()
    key = backend.cache_key({"url": "mysql://alice:secret@host:3306/mydb?ssl=true"})
    assert key == "alice@mydb"


def test_cache_key_handles_missing_url():
    backend = MySQLBackend()
    assert backend.cache_key({}) == "unknown@unknown"
    assert backend.cache_key(None) == "unknown@unknown"


# --- connect ---------------------------------------------------------------------

def test_connect_parses_url_into_pymysql_kwargs(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3307/mydb"})
    assert len(harness.calls) == 1
    kwargs = harness.calls[0]
    assert kwargs["host"] == "dbhost"
    assert kwargs["port"] == 3307
    assert kwargs["user"] == "alice"
    assert kwargs["password"] == "secret"
    assert kwargs["database"] == "mydb"
    # See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring - tied to
    # the same shared knob every other dialect uses, rather than left to
    # PyMySQL's own (coincidentally identical) built-in default.
    assert kwargs["connect_timeout"] == DB_CONNECT_TIMEOUT_SECONDS


def test_connect_percent_decodes_username_and_password(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://ali%40ce:pa%23ss@host:3306/db"})
    kwargs = harness.calls[0]
    assert kwargs["user"] == "ali@ce"
    assert kwargs["password"] == "pa#ss"


def test_connect_defaults_port_when_absent(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@host/mydb"})
    assert harness.calls[0]["port"] == 3306


# --- Cloud SQL unix-socket connections ------------------------------------------
# Regression coverage for a real bug: a Cloud SQL preset URL of the form
# mysql://user:pass@/dbname?unix_socket=/cloudsql/<connection_name> (the
# same convention GCP's own docs/SQLAlchemy examples use for a PyMySQL
# connection string) has no real host - the original version of connect()
# ignored the query string entirely and fell back to "localhost", which on
# Cloud Run fails with "Can't connect to MySQL server on 'localhost'
# ([Errno 111] Connection refused)" since there's no local MySQL and no
# TCP path to the Cloud SQL instance at all, only the socket mount.

def test_connect_uses_unix_socket_when_present_in_query_string(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({
        "type": "mysql",
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance",
    })
    assert len(harness.calls) == 1
    kwargs = harness.calls[0]
    assert kwargs["unix_socket"] == "/cloudsql/proj:us-east1:instance"
    assert kwargs["user"] == "trial"
    assert kwargs["password"] == "FooBar"
    assert kwargs["database"] == "classicmodels"
    # No host/port sent alongside unix_socket - see connect()'s comment for
    # why (avoid any ambiguity about which one PyMySQL actually uses).
    assert "host" not in kwargs
    assert "port" not in kwargs


def test_connect_omits_unix_socket_kwarg_for_an_ordinary_tcp_url(monkeypatch):
    import backends.mysql as mysqlmod
    harness = install_fake_pymysql_connect(monkeypatch)
    backend = mysqlmod.MySQLBackend()
    backend.connect({"type": "mysql", "url": "mysql://alice:secret@dbhost:3306/mydb"})
    assert "unix_socket" not in harness.calls[0]
    assert harness.calls[0]["host"] == "dbhost"


def test_cache_key_works_for_a_unix_socket_url():
    backend = MySQLBackend()
    key = backend.cache_key({
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance",
    })
    assert key == "trial@classicmodels"
    assert "FooBar" not in key


# --- identity_label ------------------------------------------------------------

def test_identity_label_returns_db_and_user():
    conn, cursor = make_fake_mysql_connection([([("mydb", "alice")], None, -1)])
    backend = MySQLBackend()
    db_name, username = backend.identity_label(conn)
    assert db_name == "mydb"
    assert username == "alice"


# --- execute -------------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts():
    responses = [
        ([(1, "Alice"), (2, "Bob")], [("id",), ("name",)], 2),
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT id, name FROM users;")
    assert len(results) == 1
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2
    # PyMySQL's autocommit is a *method*, not a settable attribute (unlike
    # psycopg2's) - see helpers.FakeMySQLConnection's docstring.
    assert conn.autocommit_calls == [True]


def test_execute_dml_with_no_description_uses_rowcount():
    responses = [([], None, 3)]  # no description -> DML path
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "DELETE FROM users WHERE inactive = 1;")
    assert results[0]["columns"] is None
    assert results[0]["rows"] is None
    assert results[0]["rowCount"] == 3


def test_execute_multiple_statements_returns_one_result_per_statement():
    responses = [
        ([], None, 1),
        ([(1,)], [("id",)], 1),
    ]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "UPDATE users SET x=1; SELECT id FROM users;")
    assert len(results) == 2
    assert results[0]["rowCount"] == 1
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_converts_decimal_datetime_and_bytes():
    row = (Decimal("19.99"), date(2024, 1, 15), b"raw-bytes")
    responses = [([row], [("price",), ("d",), ("data",)], 1)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT price, d, data FROM t;")
    out_row = results[0]["rows"][0]
    assert out_row["price"] == 19.99
    assert isinstance(out_row["price"], float)
    assert out_row["d"] == "2024-01-15"
    assert out_row["data"] == "raw-bytes"


def test_execute_ignores_blank_statements_between_semicolons():
    responses = [([], None, 0)]
    conn, cursor = make_fake_mysql_connection(responses)
    backend = MySQLBackend()
    results = backend.execute(conn, "SELECT 1;;;")
    assert len(results) == 1


# --- dialect_name ----------------------------------------------------------------

def test_dialect_name_is_mysql():
    assert MySQLBackend().dialect_name == "MySQL"
