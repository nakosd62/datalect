"""
tests/server/conftest.py

Thin pytest-fixture wrappers around helpers.py's fresh_import() /
install_fake_bigquery() / make_fake_pg_connection() / etc. See helpers.py
for the actual mechanics and why they're needed (app_config.py's
import-time side effects, the hardcoded relative SQLite path, ...).
"""

import pytest

from helpers import (
    fresh_import, install_fake_bigquery, install_fake_snowflake_connect,
    install_fake_pymysql_connect, install_fake_databricks_connect, install_fake_oracle_connect,
    install_fake_redshift_connect, install_fake_mssql_connect, install_fake_sheets_requests,
    install_fake_postgres_connect,
)


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
