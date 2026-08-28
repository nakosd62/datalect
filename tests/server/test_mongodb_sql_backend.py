"""
backends/mongodb_sql.py, driven entirely against local fakes (no real
pyodbc/unixODBC network path needed, since pyodbc itself is a thin ctypes-
style wrapper - only its top-level `connect()` function and the connection/
cursor objects it hands back need faking, same spirit as
helpers.install_fake_postgres_connect/install_fake_mssql_connect but kept
local to this file since pyodbc's shape - tables()/columns() catalog
functions, connection.getinfo() - is different enough from every psycopg2-
shaped fake already in helpers.py that reusing those wouldn't save much).

Covers: _packed_url()'s reassembly of the current four-field descriptor
shape (url/database/user/password) plus its backward-compatible pass-
through of an older all-in-one packed "url", connect()'s core kwargs,
close()'s None-tolerance, cache_key()'s credential-free Uri=/Database=
extraction, identity_label() via getinfo(), get_schema() built from
cursor.tables()/cursor.columns() (the ODBC catalog functions, not a
Mongo-specific query), and execute()'s read-only enforcement (the one
thing genuinely unique to this backend - every other dialect in this app
allows writes) plus its normal statement-execution/type-conversion path.
"""

import sys
from datetime import date
from decimal import Decimal

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.mongodb_sql import MongoSqlBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS, SqlExecutionError
import backends.mongodb_sql as mongodb_sql_module


# --- fakes -------------------------------------------------------------------

class _FakeRow:
    """pyodbc's cursor.tables()/cursor.columns() rows support attribute
    access (row.table_name, row.column_name, ...) per the ODBC catalog
    function spec - a plain namespace stands in fine, no need for pyodbc's
    real Row type."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeConnection:
    def __init__(self, tables_rows=(), columns_by_table=None, statement_responses=None,
                 getinfo_values=None, raise_on_getinfo=False):
        self._tables_rows = list(tables_rows)
        self._columns_by_table = columns_by_table or {}
        self._statement_responses = list(statement_responses or [])
        self._getinfo_values = getinfo_values or {}
        self._raise_on_getinfo = raise_on_getinfo
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def getinfo(self, info_type):
        if self._raise_on_getinfo:
            raise RuntimeError("driver doesn't support getinfo")
        return self._getinfo_values.get(info_type)

    def close(self):
        self.closed = True


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._next_rows = []
        self.closed = False

    def tables(self, tableType=None):
        return list(self._conn._tables_rows)

    def columns(self, table=None):
        return list(self._conn._columns_by_table.get(table, []))

    def execute(self, stmt):
        # Each call consumes the next queued (columns, rows) or raises, in
        # order - mirrors helpers.py's FakePgCursor-style scripted-response
        # pattern used throughout tests/server/.
        response = self._conn._statement_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        columns, rows = response
        if columns is None:
            self.description = None
            self._next_rows = []
        else:
            self.description = [(c,) for c in columns]
            self._next_rows = list(rows)

    def fetchall(self):
        return self._next_rows

    def close(self):
        self.closed = True


def _install_fake_pyodbc_connect(monkeypatch, connection):
    calls = []

    def fake_connect(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return connection

    monkeypatch.setattr(mongodb_sql_module.pyodbc, "connect", fake_connect)
    return calls


# --- dialect_name / liveness_sql ---------------------------------------------

def test_dialect_name_is_mongodb_atlas_sql():
    # Must match the _DIALECT_PROMPT_INTROS key in translate_routes.py.
    assert MongoSqlBackend.dialect_name == "MongoDB Atlas SQL"


def test_liveness_sql_is_unmodified_bare_select_1():
    assert MongoSqlBackend.liveness_sql == "SELECT 1"


# --- _packed_url() -------------------------------------------------------------

def test_packed_url_reassembles_the_four_current_fields():
    from backends.mongodb_sql import _packed_url
    packed = _packed_url({
        "url": "mongodb://h/?ssl=true", "database": "d", "user": "u", "password": "p",
    })
    assert packed == "mongodb://h/?ssl=true;Database=d;User=u;Password=p"


def test_packed_url_omits_absent_fields():
    from backends.mongodb_sql import _packed_url
    assert _packed_url({"url": "mongodb://h/?ssl=true", "database": "d"}) == "mongodb://h/?ssl=true;Database=d"


def test_packed_url_passes_an_old_all_in_one_url_through_unchanged():
    # No "database"/"user"/"password" keys at all on the descriptor -> this
    # is an older saved custom connection/preset from before this dialect
    # had separate fields, and "url" alone already carries everything.
    from backends.mongodb_sql import _packed_url
    old_style = "mongodb://h/?ssl=true;Database=d;User=u;Password=p"
    assert _packed_url({"url": old_style}) == old_style


def test_packed_url_tolerates_none_and_empty_descriptor():
    from backends.mongodb_sql import _packed_url
    assert _packed_url(None) == ""
    assert _packed_url({}) == ""


# --- connect() ----------------------------------------------------------------

def test_connect_passes_url_and_timeout(monkeypatch):
    # The stored/descriptor shape is now four separate fields (see this
    # module's docstring) - no "Driver={...}" clause, no leading "Uri="
    # key name, and no Database=/User=/Password= baked into "url" itself;
    # connect() reassembles all of that via _packed_url() +
    # _build_connection_string(), so the string pyodbc actually receives
    # is considerably longer than any single field on the descriptor.
    backend = MongoSqlBackend()
    calls = _install_fake_pyodbc_connect(monkeypatch, _FakeConnection())
    backend.connect({
        "type": "MongoDB", "url": "mongodb://h/?ssl=true",
        "database": "d", "user": "u", "password": "p",
    })
    assert calls[-1]["url"] == "Driver={MongoDB Atlas SQL ODBC Driver};Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p"
    assert calls[-1]["timeout"] == DB_CONNECT_TIMEOUT_SECONDS
    assert calls[-1]["autocommit"] is True


def test_connect_still_accepts_an_older_all_in_one_packed_url(monkeypatch):
    # Backward compatibility: an older saved custom connection/preset from
    # before this dialect had separate database/user/password fields still
    # has everything packed into "url" alone (no "database"/"user"/
    # "password" keys on the descriptor at all) - _packed_url() must pass
    # that straight through, and connect() must still finish it off with
    # the same Driver=/Uri= injection as the current four-field shape.
    backend = MongoSqlBackend()
    calls = _install_fake_pyodbc_connect(monkeypatch, _FakeConnection())
    backend.connect({"type": "MongoDB", "url": "mongodb://h/?ssl=true;Database=d;User=u;Password=p"})
    assert calls[-1]["url"] == "Driver={MongoDB Atlas SQL ODBC Driver};Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p"


def test_connect_still_accepts_the_fully_explicit_uri_form(monkeypatch):
    # Same older all-in-one shape as above, but for a url that already
    # spells out "Uri=" (Atlas's own connect-modal snippet does write it
    # explicitly) - must produce the exact same connection string as the
    # bare-URI-first shape, never double up the key.
    backend = MongoSqlBackend()
    calls = _install_fake_pyodbc_connect(monkeypatch, _FakeConnection())
    backend.connect({"type": "MongoDB", "url": "Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p"})
    assert calls[-1]["url"] == "Driver={MongoDB Atlas SQL ODBC Driver};Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p"
    assert calls[-1]["url"].count("Uri=") == 1


def test_connect_strips_any_existing_driver_clause_before_injecting_the_canonical_one(monkeypatch):
    # Defensive tolerance: a url pasted straight from Atlas's own
    # connect-modal snippet, or saved by a custom connection from before
    # this was made automatic, might still carry its own (possibly
    # different) Driver=... clause - connect() must replace it, never end
    # up sending pyodbc two Driver= clauses.
    backend = MongoSqlBackend()
    calls = _install_fake_pyodbc_connect(monkeypatch, _FakeConnection())
    backend.connect({
        "type": "MongoDB",
        "url": "Driver={Some Stale Driver Name};Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p",
    })
    assert calls[-1]["url"] == "Driver={MongoDB Atlas SQL ODBC Driver};Uri=mongodb://h/?ssl=true;Database=d;User=u;Password=p"
    assert calls[-1]["url"].count("Driver=") == 1


def test_connect_tolerates_missing_url(monkeypatch):
    # descriptor.get("url") can be None (e.g. an empty custom-connection
    # row) - connect() must not raise on that itself; it should still
    # produce the bare Driver= clause and let pyodbc surface its own
    # connection error rather than this backend crashing first.
    backend = MongoSqlBackend()
    calls = _install_fake_pyodbc_connect(monkeypatch, _FakeConnection())
    backend.connect({"type": "MongoDB", "url": None})
    assert calls[-1]["url"] == "Driver={MongoDB Atlas SQL ODBC Driver};"

# --- close() --------------------------------------------------------------

def test_close_tolerates_none():
    MongoSqlBackend().close(None)  # must not raise


def test_close_calls_underlying_close():
    conn = _FakeConnection()
    MongoSqlBackend().close(conn)
    assert conn.closed is True


# --- cache_key() --------------------------------------------------------------

def test_cache_key_extracts_uri_and_database_from_the_four_field_shape():
    backend = MongoSqlBackend()
    key = backend.cache_key({
        "url": "mongodb://atlas-sql-abc.mongodb.net/?ssl=true",
        "database": "mydb", "user": "u", "password": "secret",
    })
    assert key == "mongodb://atlas-sql-abc.mongodb.net/?ssl=true/mydb"
    assert "secret" not in key
    assert "u" != key  # sanity - User must never leak into the key either


def test_cache_key_also_accepts_an_older_all_in_one_packed_url():
    # Backward compatibility: no "database"/"user"/"password" keys on the
    # descriptor at all - an older saved connection's "url" alone still
    # carries everything, and cache_key() must extract identically.
    backend = MongoSqlBackend()
    key = backend.cache_key({
        "url": "mongodb://atlas-sql-abc.mongodb.net/?ssl=true;database=mydb;user=u;password=secret",
    })
    assert key == "mongodb://atlas-sql-abc.mongodb.net/?ssl=true/mydb"
    assert "secret" not in key


def test_cache_key_also_accepts_the_fully_explicit_uri_form_case_insensitively():
    # Same older all-in-one shape as above, but for a url that already
    # spells out "uri=" - must extract identically.
    backend = MongoSqlBackend()
    key = backend.cache_key({
        "url": "uri=mongodb://atlas-sql-abc.mongodb.net/?ssl=true;database=mydb;user=u;password=secret",
    })
    assert key == "mongodb://atlas-sql-abc.mongodb.net/?ssl=true/mydb"


def test_cache_key_never_raises_on_malformed_url():
    backend = MongoSqlBackend()
    assert backend.cache_key({"url": "not an odbc string at all"}) == "unknown/unknown"
    assert backend.cache_key({}) == "unknown/unknown"
    assert backend.cache_key(None) == "unknown/unknown"


# --- identity_label() ----------------------------------------------------------

def test_identity_label_uses_getinfo():
    conn = _FakeConnection(getinfo_values={16: "mydb", 47: "myuser"})
    db_name, username = MongoSqlBackend().identity_label(conn)
    assert (db_name, username) == ("mydb", "myuser")


def test_identity_label_falls_back_to_unknown_on_error():
    conn = _FakeConnection(raise_on_getinfo=True)
    db_name, username = MongoSqlBackend().identity_label(conn)
    assert (db_name, username) == ("Unknown", "Unknown")


# --- get_schema() ---------------------------------------------------------------

def test_get_schema_returns_none_with_no_tables():
    conn = _FakeConnection(tables_rows=[])
    assert MongoSqlBackend().get_schema(conn) is None


def test_get_schema_builds_text_from_catalog_functions():
    conn = _FakeConnection(
        tables_rows=[_FakeRow(table_name="orders"), _FakeRow(table_name="customers")],
        columns_by_table={
            "orders": [
                _FakeRow(column_name="_id", type_name="VARCHAR", is_nullable="NO"),
                _FakeRow(column_name="total", type_name="DOUBLE", is_nullable="YES"),
            ],
            "customers": [
                _FakeRow(column_name="_id", type_name="VARCHAR", is_nullable="NO"),
            ],
        },
    )
    schema = MongoSqlBackend().get_schema(conn)
    assert "Table: orders" in schema
    assert "_id VARCHAR NOT NULL" in schema
    assert "total DOUBLE NULL" in schema
    assert "Table: customers" in schema


def test_get_schema_caps_at_schema_max_tables(monkeypatch):
    monkeypatch.setattr(mongodb_sql_module, "SCHEMA_MAX_TABLES", 2)
    conn = _FakeConnection(
        tables_rows=[_FakeRow(table_name=n) for n in ["a", "b", "c"]],
        columns_by_table={n: [_FakeRow(column_name="_id", type_name="VARCHAR", is_nullable="NO")] for n in ["a", "b", "c"]},
    )
    schema = MongoSqlBackend().get_schema(conn)
    assert "Table: a" in schema
    assert "Table: b" in schema
    assert "Table: c" not in schema
    assert "1 more table(s) not shown" in schema


# --- execute(): read-only enforcement -----------------------------------------

@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM orders WHERE 1=1",
    "UPDATE orders SET total = 0",
    "INSERT INTO orders VALUES (1)",
    "DROP TABLE orders",
    "-- a sneaky comment\nDELETE FROM orders",
])
def test_execute_rejects_non_select_statements(bad_sql):
    conn = _FakeConnection(statement_responses=[])
    with pytest.raises(SqlExecutionError) as exc_info:
        MongoSqlBackend().execute(conn, bad_sql)
    assert "read-only" in str(exc_info.value)


def test_execute_allows_with_cte_select():
    conn = _FakeConnection(statement_responses=[(["n"], [(1,)])])
    results = MongoSqlBackend().execute(conn, "WITH x AS (SELECT 1 AS n) SELECT n FROM x")
    assert results[0]["rows"] == [{"n": 1}]


def test_execute_read_only_rejection_preserves_prior_results():
    # First statement (a real SELECT) succeeds and should still be reported
    # even though the second statement in the same script is rejected - same
    # partial-results contract every other backend's SqlExecutionError use
    # honors (see backends/base.py's docstring).
    conn = _FakeConnection(statement_responses=[(["n"], [(1,)])])
    with pytest.raises(SqlExecutionError) as exc_info:
        MongoSqlBackend().execute(conn, "SELECT 1 AS n; DELETE FROM orders;")
    err = exc_info.value
    assert len(err.results) == 1
    assert err.results[0]["rows"] == [{"n": 1}]
    assert err.statement_index == 1
    assert err.total_statements == 2


# --- execute(): normal statement execution + type conversion -------------------

def test_execute_returns_columns_rows_and_row_count():
    conn = _FakeConnection(statement_responses=[(["id", "name"], [(1, "Alice"), (2, "Bob")])])
    results = MongoSqlBackend().execute(conn, "SELECT id, name FROM customers")
    assert len(results) == 1
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_converts_date_bytes_and_decimal():
    conn = _FakeConnection(statement_responses=[
        (["d", "b", "amt"], [(date(2024, 1, 15), b"hello", Decimal("9.99"))]),
    ])
    results = MongoSqlBackend().execute(conn, "SELECT d, b, amt FROM t")
    row = results[0]["rows"][0]
    assert row["d"] == "2024-01-15"
    assert row["b"] == "hello"
    assert row["amt"] == 9.99
    assert isinstance(row["amt"], float)


def test_execute_runs_multiple_select_statements_independently():
    conn = _FakeConnection(statement_responses=[
        (["n"], [(1,)]),
        (["n"], [(2,)]),
    ])
    results = MongoSqlBackend().execute(conn, "SELECT 1 AS n; SELECT 2 AS n;")
    assert len(results) == 2
    assert results[0]["rows"] == [{"n": 1}]
    assert results[1]["rows"] == [{"n": 2}]


def test_execute_wraps_a_real_driver_error_mid_script():
    conn = _FakeConnection(statement_responses=[
        (["n"], [(1,)]),
        RuntimeError("connection reset by peer"),
    ])
    with pytest.raises(SqlExecutionError) as exc_info:
        MongoSqlBackend().execute(conn, "SELECT 1 AS n; SELECT 2 AS n;")
    err = exc_info.value
    assert len(err.results) == 1
    assert "connection reset" in str(err)
