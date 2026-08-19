"""
Custom (user-saved) database connections through /api/config: multiple
connections that share a project/dataset but carry different
service-account keys (must not collide/overwrite each other - see
compute_connection_key's docstring in state_store.py), the
has_custom_credentials/active_uses_custom_credentials indicators, the
"leave the key field blank to keep the previously-saved one" UX, and that
credentials_json never round-trips back to the frontend under any
circumstance. Also covers anonymous users being forbidden from the whole
concept of a custom connection.
"""

import pytest

from helpers import install_fake_bigquery, make_service_account_key_json, login_as


@pytest.fixture
def two_key_pair():
    return make_service_account_key_json(project_id="project-a"), make_service_account_key_json(project_id="project-b")


def _custom_databases_payload(key_a, key_b):
    return [
        {"type": "bigquery", "name": "Conn A", "project_id": "shared-proj", "dataset": "shared_ds",
         "credentials_json": key_a, "billing_project_id": "project-a"},
        {"type": "bigquery", "name": "Conn B", "project_id": "shared-proj", "dataset": "shared_ds",
         "credentials_json": key_b, "billing_project_id": "project-b"},
    ]


def test_two_connections_sharing_project_dataset_but_different_keys_both_persist(app_env, monkeypatch, two_key_pair):
    install_fake_bigquery(monkeypatch)
    key_a, key_b = two_key_pair
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn A", "credentials_json": key_a, "billing_project_id": "project-a",
        "is_custom": True, "custom_databases": _custom_databases_payload(key_a, key_b),
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    keys = {c["name"]: c["connection_key"] for c in data['custom_databases']}
    assert keys["Conn A"] != keys["Conn B"]
    assert data['active_custom_connection_key'] == keys["Conn A"]
    assert data['custom_database_name'] == "Conn A"


def test_switching_active_connection_between_two_that_share_a_url_bills_the_right_project(app_env, monkeypatch, two_key_pair):
    harness = install_fake_bigquery(monkeypatch)
    key_a, key_b = two_key_pair
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload(key_a, key_b)

    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn A", "credentials_json": key_a, "billing_project_id": "project-a",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "project-a"

    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn B", "credentials_json": key_b, "billing_project_id": "project-b",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "project-b"


def test_reselecting_a_connection_with_blank_key_reuses_its_own_saved_key_not_the_others(app_env, monkeypatch, two_key_pair):
    # The "leave blank to keep the saved key" UX - re-picking Conn A with
    # an empty credentials_json field must resolve back to Conn A's OWN
    # key (name-disambiguated - see _resolve_bigquery_credentials), never
    # silently reuse whichever connection was active most recently.
    harness = install_fake_bigquery(monkeypatch)
    key_a, key_b = two_key_pair
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload(key_a, key_b)

    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn A", "credentials_json": key_a, "billing_project_id": "project-a",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn B", "credentials_json": key_b, "billing_project_id": "project-b",
        "is_custom": True, "custom_databases": payload,
    })
    # Switch back to Conn A, credentials_json left blank.
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "database_name": "Conn A", "credentials_json": "", "billing_project_id": "project-a",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    assert resp.get_json()["custom_database_name"] == "Conn A"

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "project-a"


# --- has_custom_credentials / active_uses_custom_credentials -------------------

def test_has_custom_credentials_true_for_bigquery_connection_with_key(app_env, monkeypatch):
    install_fake_bigquery(monkeypatch)
    key_json = make_service_account_key_json()
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "p", "dataset": "d", "database_name": "BQ Conn",
        "credentials_json": key_json, "billing_project_id": "p", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


def test_has_custom_credentials_false_for_plain_postgres_custom_connection(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "PG Conn", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is False
    assert data['active_uses_custom_credentials'] is False


def test_active_uses_custom_credentials_false_when_active_is_a_preset(app_env):
    login_as(app_env.client, "alice@example.com")
    data = app_env.client.get('/api/config').get_json()
    assert data['active_uses_custom_credentials'] is False


# --- credentials never leak ----------------------------------------------------

def test_credentials_json_never_appears_anywhere_in_config_response(app_env, monkeypatch):
    install_fake_bigquery(monkeypatch)
    key_json = make_service_account_key_json()
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "p", "dataset": "d", "database_name": "BQ Conn",
        "credentials_json": key_json, "billing_project_id": "p", "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert key_json not in resp.get_data(as_text=True)
    for db in resp.get_json()['custom_databases']:
        assert "credentials_json" not in (db.get("config") or {})


# --- anonymous users cannot save/select custom connections at all --------------

def test_anonymous_user_cannot_save_a_custom_connection(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "Sneaky", "is_custom": True,
    })
    assert resp.status_code == 403


def test_anonymous_user_cannot_submit_a_custom_databases_list(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    resp = env.client.post('/api/config', json={
        "custom_databases": [{"name": "x", "type": "postgres", "url": "postgresql://u:p@h/db", "config": {}}],
    })
    assert resp.status_code == 403


def test_anonymous_user_never_sees_custom_databases_in_response(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'] == []


# NOTE: the GET-side redaction of preset connection strings/custom
# connections (configured_databases without URLs, custom_databases always
# [], etc.) is gated on IS_CLOUD_RUN specifically, not just an anonymous
# identity - see test_config_cloud_run_anonymous_redaction.py for that
# behavior with K_SERVICE actually set (it requires mocking Firestore too,
# since IS_CLOUD_RUN=True without a working Firestore client is a hard
# startup RuntimeError by design - see app_config.py).
