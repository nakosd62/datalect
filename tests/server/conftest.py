"""
tests/server/conftest.py

Thin pytest-fixture wrappers around helpers.py's fresh_import() /
install_fake_bigquery() / make_fake_pg_connection() / etc. See helpers.py
for the actual mechanics and why they're needed (app_config.py's
import-time side effects, the hardcoded relative SQLite path, ...).
"""

import pytest

from helpers import fresh_import, install_fake_bigquery


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
    specific DATABASE_PRESETS/auth/Cloud Run configuration just want this."""
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
