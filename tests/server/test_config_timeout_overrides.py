"""
Per-dataset "connect_timeout_seconds"/"execute_timeout_seconds" overrides
for CUSTOM connections through /api/config - the custom-connection-side
counterpart to app_config.py's admin-preset "connect_timeout_seconds"/
"execute_timeout_seconds" support (see that module's DATABASE_PRESETS_FILE
comment and test_app_config_presets.py's own coverage), and the shared
config_routes.py mechanism (_timeout_override_kwargs) both the single
active-connection form (_parse_incoming_connection) and the custom_databases
list form (_parse_incoming_custom_databases) spread into every dialect's
own config dict literal.

This file focuses on config_routes.py's parsing/storage of the two fields
(both forms, across more than one dialect, to prove the mechanism is
genuinely dialect-agnostic) plus one true end-to-end dispatch test
confirming a "connect_timeout_seconds" override actually reaches
backends.postgres.PostgresBackend.connect() as psycopg2's own
"connect_timeout" kwarg - the connect()-level unit coverage of
resolve_timeout_seconds() itself lives in test_backend_base_helpers.py.
"""

from helpers import login_as


def test_custom_postgres_connection_persists_both_timeout_overrides(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True,
        "connect_timeout_seconds": 20, "execute_timeout_seconds": 120,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    config = data['custom_databases'][0]['config']
    assert config['connect_timeout_seconds'] == 20
    assert config['execute_timeout_seconds'] == 120


def test_custom_connection_without_timeout_overrides_still_saves_fine(app_env):
    # Regression guard: both fields are optional - a plain connection with
    # neither (the overwhelming common case) must keep working exactly as
    # it did before this feature existed, with no key silently defaulted in.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    config = data['custom_databases'][0]['config']
    assert "connect_timeout_seconds" not in config
    assert "execute_timeout_seconds" not in config


def test_only_one_override_set_persists_just_that_one(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True, "execute_timeout_seconds": 90,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    config = data['custom_databases'][0]['config']
    assert "connect_timeout_seconds" not in config
    assert config['execute_timeout_seconds'] == 90


def test_custom_databases_list_form_persists_timeout_overrides_for_a_non_default_dialect(app_env):
    # Proven against Oracle here (not just Postgres, above) - the fields
    # are read once, generically, before config_routes.py's own per-dialect
    # dispatch (see _timeout_override_kwargs), same "dialect-agnostic"
    # treatment app_config.py's presets-file loader already gives these
    # two fields.
    login_as(app_env.client, "alice@example.com")
    payload = [{
        "type": "oracle", "name": "Oracle Conn", "host": "db.example.com",
        "service_name": "ORCLPDB1", "user": "svc", "password": "secret",
        "connect_timeout_seconds": 15, "execute_timeout_seconds": 200,
    }]
    resp = app_env.client.post('/api/config', json={"custom_databases": payload})
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    by_name = {c["name"]: c for c in data['custom_databases']}
    config = by_name["Oracle Conn"]["config"]
    assert config['connect_timeout_seconds'] == 15
    assert config['execute_timeout_seconds'] == 200


def test_connect_dispatches_connect_timeout_override_through_to_backend_connect(app_env, postgres_harness):
    # The real end-to-end check: not just that config_routes.py stores
    # connect_timeout_seconds, but that it actually reaches
    # backends.postgres.PostgresBackend.connect() as psycopg2's own
    # "connect_timeout" kwarg on a real /api/execute call, overriding the
    # shared DB_CONNECT_TIMEOUT_SECONDS default.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://alice:secret@dbhost:5432/salesdb",
        "database_name": "PG Conn", "is_custom": True, "connect_timeout_seconds": 42,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # >=1 rather than an exact count: the /api/config POST above also
    # triggers its own best-effort connect() for schema/identity purposes -
    # see test_config_mysql.py's matching comment for the equivalent MySQL
    # case. The assertion only cares that /api/execute's own connect() call
    # carried the override through correctly.
    assert len(postgres_harness.calls) >= 1
    _, kwargs = postgres_harness.calls[-1]
    assert kwargs.get("connect_timeout") == 42
    # Not just an equality check: psycopg2 stringifies whatever's passed
    # here straight into the DSN it hands libpq, which requires a strict
    # integer - a float (60 == 60.0 in Python, so an equality-only
    # assertion here would have silently passed) produces a string like
    # "42.0" that libpq rejects outright as an "invalid integer value" -
    # a real bug a user hit against a live Redshift preset before
    # backends/postgres.py's/backends/redshift.py's connect() started
    # rounding to a real int. See test_postgres_backend.py's/
    # test_redshift_backend.py's own dedicated regression tests for the
    # unit-level version of this same assertion.
    assert isinstance(kwargs.get("connect_timeout"), int)
