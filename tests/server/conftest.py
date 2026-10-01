"""
tests/server/conftest.py

Thin pytest-fixture wrappers around helpers.py's fresh_import() /
install_fake_bigquery() / make_fake_pg_connection() / etc. See helpers.py
for the actual mechanics and why they're needed (app_config.py's
import-time side effects, the hardcoded relative SQLite path, ...).
"""

import os

# Set BEFORE anything else in this file, and before pytest collects a
# single test module in this directory - conftest.py is always imported
# first, so this is the earliest point this whole test session gets a
# chance to run any code at all. app_config.py's own module-level
# `if os.environ.get("YDYL_SKIP_DOTENV") != "1": load_dotenv(override=True)`
# is this exact escape hatch (see its own comment) - without setting it
# here, a real, unguarded `load_dotenv(override=True)` can still fire
# during COLLECTION, before fresh_import()'s per-test monkeypatch of
# dotenv.load_dotenv ever gets a chance to run: any test file anywhere in
# this directory that imports something pulling in app_config.py at its
# own top level (module scope, not inside a test function) - e.g. `from
# config_routes import _describe_config_diff` - triggers that real
# load_dotenv() at import time, with the process's cwd still the real
# repo root (pytest hasn't chdir'd into any test's tmp_path yet), so it
# finds and loads the REAL repo-root `.env` into this whole process's
# os.environ, permanently, for the rest of the run. Every _ENV_VARS_TO_
# CLEAR entry in helpers.py only helps INSIDE fresh_import() - it does
# nothing for a bare `.env` leak that happens before any test (or a
# backend test that never calls fresh_import at all, e.g.
# test_sheets_backend.py's direct `SheetsBackend().connect(...)` calls) -
# so this is the one guard that actually closes the leak at its source,
# regardless of which module happens to trigger the first real
# app_config import, and regardless of whether the affected test goes
# through fresh_import at all.
os.environ["YDYL_SKIP_DOTENV"] = "1"

import pytest

from helpers import (
    fresh_import, install_fake_bigquery, install_fake_snowflake_connect,
    install_fake_pymysql_connect, install_fake_databricks_connect, install_fake_oracle_connect,
    install_fake_redshift_connect, install_fake_mssql_connect, install_fake_sheets_requests,
    install_fake_postgres_connect, install_fake_pyodbc_connect,
)


@pytest.fixture(autouse=True)
def _isolate_state_store_cwd(monkeypatch, tmp_path):
    """Autouse for EVERY test collected under this directory, regardless of
    whether it uses app_factory/fresh_import at all - closes a real leak
    found in production: state_store.py's SqliteStateStore stores its db
    path as a plain relative string ("state/ydyl_state.db", see app_
    config.py's TRANSLATION_STATS_DB_PATH) and calls sqlite3.connect() on
    it FRESH on every single read/write, so it's the process's CURRENT cwd
    at call time - not whatever cwd was in effect when the SqliteStateStore
    object was constructed - that decides which file actually gets
    touched. fresh_import() already handles this for any test that goes
    through it (see its own docstring: chdir into a fresh tmp_path before
    importing app_config) - but several bare unit tests (e.g. tests/server/
    test_connection_router.py's several run_triage_call(...) tests) call
    straight into business-logic functions that log real usage via
    app_config.py's module-level `state_store` singleton, without ever
    calling app_factory/fresh_import at all. Those tests were writing
    real rows into the actual repo's real state/ydyl_state.db every time
    this suite ran (call_type="triage", model="m" - the exact placeholder
    those tests pass - root-caused from a real report of exactly that
    turning up in production).

    Chdir'ing into `tmp_path` (pytest's own fresh, empty per-test
    directory) before every single test closes this regardless of which
    test it is or whether it happens to call fresh_import() itself - and
    doing so is always safe to combine with that: fresh_import() receives
    the SAME tmp_path instance for this same test (pytest caches a
    fixture's value per test, however many other fixtures request it), so
    its own monkeypatch.chdir call is just a harmless re-chdir into the
    directory this fixture already moved into, not a conflicting second
    location. The "state" subdirectory is pre-created here too (mirroring
    what state_store.init() would otherwise do, but that method is never
    called at all by the bare tests this fixture exists for) so a bare
    test's own record_llm_usage()/etc. call actually succeeds into an
    isolated tmp file instead of silently failing (record_llm_usage
    swallows its own exceptions - see its own try/except - so this isn't
    required for isolation itself, only to avoid spurious "Error recording
    LLM usage" log noise during a normal test run)."""
    monkeypatch.chdir(tmp_path)
    os.makedirs(os.path.join(tmp_path, "state"), exist_ok=True)


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """Returns a callable `build(env=None, register_blueprints=True)` that
    gives a fresh, isolated app instance for the environment you pass -
    call it once per distinct environment a test needs. See
    helpers.fresh_import for the full contract."""
    def build(env=None, register_blueprints=True, mock_firestore=False):
        return fresh_import(
            monkeypatch, tmp_path, env=env, register_blueprints=register_blueprints,
            mock_firestore=mock_firestore,
        )
    return build


@pytest.fixture
def app_env(app_factory):
    """The common case: one app instance, local dev defaults (no auth, no
    GCP project -> SQLite state, no presets -> the single synthetic
    "Default DB" fallback preset). Most tests that don't care about a
    specific DATABASE_PRESETS_FILE/auth/Cloud Run configuration just want this."""
    return app_factory()


@pytest.fixture
def client(app_env):
    """Flask test client for the default local-dev app_env above."""
    return app_env.client


@pytest.fixture
def bigquery_harness(monkeypatch):
    """Patches backends.bigquery's google-cloud-bigquery objects with
    fakes. NOTE: call this AFTER app_factory/app_env in your test (or after
    any fresh_import) - it patches the currently-imported backends.bigquery
    module object, so if fresh_import() runs afterwards and re-imports
    backends.bigquery fresh, the patch is lost. Order in the test function
    matters: build the app first, then install this."""
    return install_fake_bigquery(monkeypatch)


@pytest.fixture
def snowflake_harness(monkeypatch):
    """Patches backends.snowflake's snowflake.connector.connect with a
    fake that records kwargs instead of opening a real connection. Same
    ordering caveat as bigquery_harness above: call this AFTER
    app_factory/app_env in your test, not before."""
    return install_fake_snowflake_connect(monkeypatch)


@pytest.fixture
def postgres_harness(monkeypatch):
    """Patches backends.postgres's psycopg2.connect with a fake that
    records the DSN and kwargs it was called with instead of opening a
    real connection. Same ordering caveat as bigquery_harness/
    snowflake_harness/mysql_harness above: call this AFTER app_factory/
    app_env in your test, not before."""
    return install_fake_postgres_connect(monkeypatch)


@pytest.fixture
def mysql_harness(monkeypatch):
    """Patches backends.mysql's pymysql.connect with a fake that records
    kwargs instead of opening a real connection. Same ordering caveat as
    bigquery_harness/snowflake_harness above: call this AFTER
    app_factory/app_env in your test, not before."""
    return install_fake_pymysql_connect(monkeypatch)


@pytest.fixture
def mongodb_sql_harness(monkeypatch):
    """Patches backends.mongodb_sql's pyodbc.connect with a fake that
    records the connection string + kwargs instead of opening a real ODBC
    connection. Same ordering caveat as bigquery_harness/snowflake_harness/
    mysql_harness above: call this AFTER app_factory/app_env in your test,
    not before."""
    return install_fake_pyodbc_connect(monkeypatch)


@pytest.fixture
def databricks_harness(monkeypatch):
    """Patches backends.databricks's databricks.sql.connect with a fake
    that records kwargs instead of opening a real connection. Same ordering
    caveat as bigquery_harness/snowflake_harness/mysql_harness above: call
    this AFTER app_factory/app_env in your test, not before."""
    return install_fake_databricks_connect(monkeypatch)


@pytest.fixture
def oracle_harness(monkeypatch):
    """Patches backends.oracle's oracledb.connect with a fake that records
    kwargs instead of opening a real connection. Same ordering caveat as
    bigquery_harness/snowflake_harness/mysql_harness/databricks_harness
    above: call this AFTER app_factory/app_env in your test, not before."""
    return install_fake_oracle_connect(monkeypatch)


@pytest.fixture
def redshift_harness(monkeypatch):
    """Patches backends.redshift's psycopg2.connect with a fake that
    records kwargs instead of opening a real connection. Same ordering
    caveat as bigquery_harness/snowflake_harness/mysql_harness/
    databricks_harness/oracle_harness above: call this AFTER app_factory/
    app_env in your test, not before."""
    return install_fake_redshift_connect(monkeypatch)


@pytest.fixture
def mssql_harness(monkeypatch):
    """Patches backends.mssql's pytds.connect with a fake that records
    kwargs instead of opening a real connection. Same ordering caveat as
    bigquery_harness/snowflake_harness/mysql_harness/databricks_harness/
    oracle_harness/redshift_harness above: call this AFTER app_factory/
    app_env in your test, not before."""
    return install_fake_mssql_connect(monkeypatch)


@pytest.fixture
def sheets_harness(monkeypatch):
    """Patches backends.sheets's module-level `requests` reference with a
    fake .get that records calls and returns queued canned gviz responses,
    instead of making a real HTTP request. Same ordering caveat as
    bigquery_harness/.../mssql_harness above: call this AFTER app_factory/
    app_env in your test, not before. Unlike every harness above, this one
    starts with an EMPTY response queue - queue_table()/queue_error()/
    queue_response() on the returned harness before triggering any call
    that reaches _fetch() (identity_label()/get_schema()/execute()), since
    there's no live connection object here to default the response from."""
    return install_fake_sheets_requests(monkeypatch)
