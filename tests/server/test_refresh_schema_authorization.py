"""
POST /api/config/refresh-schema's authorization split: kind="custom"
stays refreshable by anyone (already scoped to the requester's own saved
connections - see test_config_refresh_schema.py), but kind="preset" and
kind="group" are shared, admin-configured resources with no single
owner, so refreshing THEM is a superuser-only action (auth.py's
is_superuser(), wired in by config_routes.py's handle_refresh_schema()
and exposed to the frontend via GET/POST /api/config's own
'is_superuser' field).

is_superuser() is deliberately gated on IS_CLOUD_RUN, not just
AUTH_ENABLED: a LOCAL deployment has no concept of "untrusted other
user" at all (there's only ever the one operator running it, same trust
posture presets.json's own plain-file editability already has), so every
session there is a superuser regardless of which identity it happens to
resolve to. Only on Cloud Run does SUPERUSER_EMAILS (app_config.py) -
an explicit, logged-in-only allowlist - actually gate anything; see
test_config_cloud_run_anonymous_redaction.py's own cloud_run_env-style
fixture, mirrored here (not imported - no shared load-order dependency
between test files, same convention test_group_schema_overview.py's
module docstring documents).
"""

import pytest

from helpers import (
    login_as, write_database_presets_file, FAKE_DB_CONFIG_ENCRYPTION_KEY,
    FAKE_SESSION_SIGNING_KEY,
)


def _presets_and_group_path(tmp_path):
    return write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
        {"id": "grp-ab", "name": "AB Group", "type": "dataset_group", "dataset_list": ["pg-a", "pg-b"]},
    ])


def _cloud_run_env(tmp_path, superuser_emails=""):
    return {
        "K_SERVICE": "ydyl-service",
        "DB_CONFIG_ENCRYPTION_KEY": FAKE_DB_CONFIG_ENCRYPTION_KEY,
        "SESSION_SIGNING_KEY": FAKE_SESSION_SIGNING_KEY,
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": _presets_and_group_path(tmp_path),
        "SUPERUSER_EMAILS": superuser_emails,
    }


def _never_called(*args, **kwargs):
    raise AssertionError("should never be called - the superuser gate should have short-circuited first")


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


# --- Local deployment: everyone is a superuser -----------------------------

def test_local_deployment_any_logged_in_identity_can_refresh_a_preset(app_factory, tmp_path, monkeypatch):
    env = app_factory(env={"DATABASE_PRESETS_FILE": _presets_and_group_path(tmp_path)})
    login_as(env.client, "nobody-special@example.com")
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", lambda descriptor, user_id=None: (True, None))

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "preset", "id": "pg-a"})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_local_deployment_any_logged_in_identity_can_refresh_a_group_overview(app_factory, tmp_path, monkeypatch):
    env = app_factory(env={"DATABASE_PRESETS_FILE": _presets_and_group_path(tmp_path)})
    login_as(env.client, "nobody-special@example.com")
    monkeypatch.setattr(env.config_routes, "_generate_and_cache_group_schema_overview", lambda group_id, user_id: True)

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "grp-ab"})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_local_deployment_get_config_reports_is_superuser_true(app_env):
    assert app_env.client.get('/api/config').get_json()['is_superuser'] is True


# --- Cloud Run deployment: explicit, logged-in-only allowlist --------------

def test_cloud_run_anonymous_visitor_cannot_refresh_a_preset(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", _never_called)

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "preset", "id": "pg-a"})

    assert resp.status_code == 403
    assert resp.get_json()['success'] is False


def test_cloud_run_logged_in_non_superuser_cannot_refresh_a_preset(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    login_as(env.client, "bob@example.com")
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", _never_called)

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "preset", "id": "pg-a"})

    assert resp.status_code == 403
    assert resp.get_json()['success'] is False


def test_cloud_run_logged_in_non_superuser_cannot_refresh_a_group_overview(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    login_as(env.client, "bob@example.com")
    monkeypatch.setattr(env.config_routes, "_generate_and_cache_group_schema_overview", _never_called)

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "grp-ab"})

    assert resp.status_code == 403
    assert resp.get_json()['success'] is False


def test_cloud_run_superuser_can_refresh_a_preset(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    login_as(env.client, "alice@example.com")
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", lambda descriptor, user_id=None: (True, None))

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "preset", "id": "pg-a"})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_cloud_run_superuser_can_refresh_a_group_overview(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    login_as(env.client, "alice@example.com")
    monkeypatch.setattr(env.config_routes, "_generate_and_cache_group_schema_overview", lambda group_id, user_id: True)

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "grp-ab"})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_cloud_run_superuser_emails_match_is_case_insensitive(app_factory, tmp_path, monkeypatch):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="Alice@Example.COM"), mock_firestore=True)
    login_as(env.client, "alice@example.com")
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", lambda descriptor, user_id=None: (True, None))

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "preset", "id": "pg-a"})

    assert resp.status_code == 200


def test_cloud_run_logged_in_non_superuser_can_still_refresh_their_own_custom_connection(app_factory, tmp_path, monkeypatch):
    # The whole point of scoping this gate to kind in ('preset', 'group')
    # rather than the whole route: a custom connection is always this
    # user's own, so it must stay refreshable by anyone, superuser or not.
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)
    login_as(env.client, "bob@example.com")
    connection_key = _save_custom_postgres_connection(env.client, "Bob's DB", "postgresql://u:p@host/bob")
    monkeypatch.setattr(env.config_routes, "prime_schema_cache_with_reason", lambda descriptor, user_id=None: (True, None))

    resp = env.client.post('/api/config/refresh-schema', json={"kind": "custom", "id": connection_key})

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_cloud_run_get_config_reports_is_superuser_for_superuser_and_non_superuser(app_factory, tmp_path):
    env = app_factory(env=_cloud_run_env(tmp_path, superuser_emails="alice@example.com"), mock_firestore=True)

    login_as(env.client, "alice@example.com")
    assert env.client.get('/api/config').get_json()['is_superuser'] is True

    login_as(env.client, "bob@example.com")
    assert env.client.get('/api/config').get_json()['is_superuser'] is False
