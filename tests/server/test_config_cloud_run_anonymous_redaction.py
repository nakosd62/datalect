"""
config_routes.py's anonymous-visitor redaction on Cloud Run specifically:
IS_CLOUD_RUN (K_SERVICE set) is a stricter gate than just "auth is enabled"
(GOOGLE_CLIENT_ID set) - it's the actual Cloud Run detection, and it's what
config_routes.py's GET response redaction branches key off of (never
sending real preset connection strings, custom connections, or the active
connection string to a shared anonymous visitor). Requires mock_firestore,
since IS_CLOUD_RUN=True with no working Firestore client is a hard startup
RuntimeError by design (see app_config.py's "Halting startup to prevent
ephemeral SQLite fallback").
"""

import pytest

from helpers import login_as


CLOUD_RUN_ENV = {
    "K_SERVICE": "ydyl-service",
    "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
    "GCP_PROJECT_ID": "fake-project",
    "DATABASE_PRESETS": (
        '[{"type":"postgres","name":"Demo","url":"postgresql://realuser:realpass@realhost/realdb"}]'
    ),
}


def test_cloud_run_starts_up_successfully_with_mocked_firestore(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    assert env.app_config.IS_CLOUD_RUN is True
    assert type(env.app_config.state_store).__name__ == "FirestoreStateStore"


def test_anonymous_visitor_never_receives_real_preset_connection_strings(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'] == [{"name": "Demo", "type": "postgres"}]
    for db in data['configured_databases']:
        assert "url" not in db


def test_anonymous_visitor_active_connection_string_is_empty(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    data = env.client.get('/api/config').get_json()
    assert data['active_database_url'] == ""
    assert data['active_database_type'] == ""
    assert data['default_database_url'] == ""


def test_anonymous_visitor_selects_preset_by_index_not_url(app_factory):
    env = app_factory(env={
        **CLOUD_RUN_ENV,
        "DATABASE_PRESETS": (
            '[{"type":"postgres","name":"A","url":"postgresql://real/a"},'
            '{"type":"postgres","name":"B","url":"postgresql://real/b"}]'
        ),
    }, mock_firestore=True)
    resp = env.client.post('/api/config', json={"preset_index": 1})
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['database_name'] == "B"
    assert data['active_database_url'] == ""  # still never exposed


def test_authenticated_user_on_cloud_run_gets_real_connection_details(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['authenticated'] is True
    assert data['configured_databases'][0].get("url") == "postgresql://realuser:realpass@realhost/realdb"


def test_authenticated_user_on_cloud_run_can_save_a_custom_connection(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['name'] == "My DB"


def test_data_is_isolated_per_user_via_firestore(app_factory):
    env = app_factory(env=CLOUD_RUN_ENV, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/alice-db",
        "database_name": "Alice's DB", "is_custom": True,
    })
    login_as(env.client, "bob@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'] == []  # bob doesn't see alice's connection
