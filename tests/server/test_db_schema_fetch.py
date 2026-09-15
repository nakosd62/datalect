"""
db.py's _fetch_database_schema(): the thin wrapper around
backend.get_schema() that every dialect's own backend module funnels
through. Every backend's get_schema() (see e.g. backends/postgres.py's
"if not all_table_names: return None") treats "connected fine, ran the
introspection queries fine, there's just nothing to describe (e.g. a
schema made up entirely of views, with zero BASE TABLEs)" as a normal,
non-exceptional None/"" return - NOT an error. Before the fix this
covers, that fell through to _SCHEMA_FETCH_FAILED completely silently:
logger.exception is only ever reached from the `except Exception` branch
below it, which this path never raises into. A real-world case (a
Postgres schema made entirely of views) hit exactly this path and left
nothing in the logs at all - "no schema" with nothing to explain why.
"""

from helpers import SERVER_DIR

import sys
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


class _FakeBackend:
    """Stands in for a real dialect backend - only the three methods
    _fetch_database_schema/get_conn_identifier actually call."""

    def __init__(self, schema_text, raise_on_get_schema=None):
        self._schema_text = schema_text
        self._raise_on_get_schema = raise_on_get_schema
        self.connect_calls = []
        self.closed_connections = []

    def connect(self, descriptor):
        self.connect_calls.append(descriptor)
        return object()

    def get_schema(self, connection):
        if self._raise_on_get_schema:
            raise self._raise_on_get_schema
        return self._schema_text

    def cache_key(self, descriptor):
        return "fake-user@fake-host/fake-db"

    def close(self, connection):
        self.closed_connections.append(connection)


def _install_fake_backend(monkeypatch, schema_text=None, raise_on_get_schema=None):
    import db as db_module
    fake = _FakeBackend(schema_text, raise_on_get_schema=raise_on_get_schema)
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)
    return db_module, fake


def test_none_schema_text_logs_a_warning_naming_the_connection(app_factory, monkeypatch, caplog):
    app_factory()  # establishes db.py's own module-level deps (app_config etc.)
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "fake-user@fake-host/fake-db" in warnings[0].getMessage()
    # No exception was raised, so the pre-existing logger.exception branch
    # must NOT also fire for this path - only the new warning.
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_empty_string_schema_text_also_logs_a_warning(app_factory, monkeypatch, caplog):
    # "" is just as falsy as None and hits the exact same branch - a
    # backend could plausibly return either for "nothing to describe".
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="")

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_real_schema_text_logs_no_warning(app_factory, monkeypatch, caplog):
    # The common, successful case must stay exactly as quiet as before -
    # this fix only adds visibility for the previously-silent failure
    # path, not new log noise for every ordinary schema fetch.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: customers\n  id integer NOT NULL")

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == "Table: customers\n  id integer NOT NULL"
    assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)


def test_real_exception_still_logs_via_exception_not_the_new_warning(app_factory, monkeypatch, caplog):
    # Regression guard: a genuine connection/query failure must still take
    # the pre-existing `except Exception` -> logger.exception(...) path,
    # not get reclassified as the new "no schema text" warning just
    # because both end up returning _SCHEMA_FETCH_FAILED.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, raise_on_get_schema=RuntimeError("connection reset"))

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)
