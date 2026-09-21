"""
POST /api/config/refresh-schema: the new endpoint behind the DB
connections dialog's "Refresh Schema" button (see webClient/client.js's
handleRefreshSchemaClick and the plan this implements). A logged-in
user's saved custom connection is identified by connection_key (never a
raw descriptor/credentials - the same convention every other single-
custom-connection lookup in this app uses), resolved via
state_store.get_db_connections() scoped to THIS request's own identity,
then handed to db.py's prime_schema_cache_with_reason() (force-refetches
and pins the deep schema-cache entry - see db.py's own docstring; this
used to also refetch a second, independent "shallow" entry, removed since
nothing reads one anymore - and also reports WHY a failure failed, so this
route can pick between its two different error messages - see its own
comment on SCHEMA_FETCH_FAILURE_REASON_EMPTY vs. the generic one).

These tests monkeypatch config_routes.prime_schema_cache_with_reason
directly rather than driving a real backend connect()/get_schema()
round-trip - the endpoint's own job (resolving connection_key ->
descriptor, handling success/failure/exception, picking the right error
message, scoping to the right user) is what's under test here, not schema
introspection itself (already covered by test_db_schema_fetch.py and the
per-backend test files).
"""

from helpers import login_as


def _save_custom_postgres_connection(client, name, url):
    resp = client.post('/api/config', json={
        "database_type": "postgres", "database_url": url,
        "database_name": name, "is_custom": True,
        "custom_databases": [{"name": name, "type": "postgres", "url": url, "config": {}}],
    })
    assert resp.status_code == 200
    data = client.get('/api/config').get_json()
    matches = [db for db in data['custom_databases'] if db['name'] == name]
    assert len(matches) == 1
    return matches[0]['connection_key']


def test_missing_connection_key_returns_400(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config/refresh-schema', json={})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_unknown_connection_key_returns_404(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": "does-not-exist"})
    assert resp.status_code == 404
    assert resp.get_json()['success'] is False


def test_successful_refresh_calls_prime_schema_cache_and_returns_success(app_env, monkeypatch):
    login_as(app_env.client, "alice@example.com")
    connection_key = _save_custom_postgres_connection(app_env.client, "My DB", "postgresql://u:p@host/db")

    calls = []

    def _fake_prime_schema_cache_with_reason(descriptor, user_id=None):
        calls.append((descriptor, user_id))
        return True, None

    monkeypatch.setattr(app_env.config_routes, "prime_schema_cache_with_reason", _fake_prime_schema_cache_with_reason)

    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": connection_key})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert len(calls) == 1
    descriptor, user_id = calls[0]
    assert descriptor["type"] == "postgres"
    assert descriptor["url"] == "postgresql://u:p@host/db"


def test_failed_refresh_with_a_real_error_returns_502_with_the_generic_message(app_env, monkeypatch):
    # reason=None (or SCHEMA_FETCH_FAILURE_REASON_TIMEOUT/_FATAL) - a
    # genuine connect()/query failure - gets the original "check
    # reachability/credentials" wording, since that's actually the right
    # advice here.
    login_as(app_env.client, "alice@example.com")
    connection_key = _save_custom_postgres_connection(app_env.client, "My DB", "postgresql://u:p@host/db")

    monkeypatch.setattr(
        app_env.config_routes, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (False, None),
    )

    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": connection_key})

    assert resp.status_code == 502
    data = resp.get_json()
    assert data['success'] is False
    assert 'reachable' in data['error']
    assert 'credentials' in data['error']


def test_failed_refresh_with_no_tables_returns_a_specific_not_generic_message(app_env, monkeypatch):
    # reason=SCHEMA_FETCH_FAILURE_REASON_EMPTY - the connection worked
    # fine, there's just nothing to describe (views-only schema, empty
    # database, or no table-level privileges) - telling the user to go
    # "check reachability/credentials" here would send them looking in
    # the wrong place, so this gets its own, different wording instead.
    import db as db_module

    login_as(app_env.client, "alice@example.com")
    connection_key = _save_custom_postgres_connection(app_env.client, "My DB", "postgresql://u:p@host/db")

    monkeypatch.setattr(
        app_env.config_routes, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (False, db_module.SCHEMA_FETCH_FAILURE_REASON_EMPTY),
    )

    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": connection_key})

    assert resp.status_code == 502
    data = resp.get_json()
    assert data['success'] is False
    assert 'no tables' in data['error']
    # Must NOT tell the user to check reachability/credentials - that's
    # specifically the misleading advice this message exists to avoid.
    assert 'reachable' not in data['error']
    assert 'credentials' not in data['error']


def test_exception_during_refresh_is_caught_and_returns_502(app_env, monkeypatch):
    login_as(app_env.client, "alice@example.com")
    connection_key = _save_custom_postgres_connection(app_env.client, "My DB", "postgresql://u:p@host/db")

    def _raise(descriptor, user_id=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(app_env.config_routes, "prime_schema_cache_with_reason", _raise)

    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": connection_key})

    assert resp.status_code == 502
    assert resp.get_json()['success'] is False


def test_cannot_refresh_another_users_connection(app_env, monkeypatch):
    # alice saves a connection, bob tries to refresh it by key - must be
    # treated exactly like an unknown key (404), never resolved, since the
    # lookup is scoped to bob's own identity's saved connections.
    login_as(app_env.client, "alice@example.com")
    connection_key = _save_custom_postgres_connection(app_env.client, "Alice's DB", "postgresql://u:p@host/alice")

    monkeypatch.setattr(
        app_env.config_routes, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (_ for _ in ()).throw(AssertionError("should never be called")),
    )

    login_as(app_env.client, "bob@example.com")
    resp = app_env.client.post('/api/config/refresh-schema', json={"connection_key": connection_key})

    assert resp.status_code == 404
    assert resp.get_json()['success'] is False
