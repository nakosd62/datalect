"""
Custom (user-saved) database connections through /api/config: multiple
connections that share a project/dataset but carry different
service-account keys (must not collide/overwrite each other - see
compute_connection_key's docstring in state_store.py), the
has_custom_credentials/active_uses_custom_credentials indicators, the
"leave the key field blank to keep the previously-saved one" UX, and that
credentials_json never round-trips back to the frontend under any
circumstance. Also covers anonymous (Cloud Run, signed-out) visitors being
able to save/select their OWN custom connections, stored under their own
per-session "anonymous:<session_id>" identity (see auth.py's
ANONYMOUS_USER_ID_PREFIX) and fully isolated from every other anonymous
visitor and from authenticated users - the same state_store-layer isolation
every other per-user identity in this app relies on (e.g. the translations
audit log and chat-history buckets).
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


# --- anonymous users can save/select their OWN custom connections --------------

def test_anonymous_user_can_save_a_custom_connection(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['name'] == "My DB"
    assert data['active_is_custom'] is True
    assert data['active_database_url'] == "postgresql://u:p@h/db"


def test_anonymous_user_can_submit_a_custom_databases_list(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "My DB", "is_custom": True,
        "custom_databases": [{"name": "My DB", "type": "postgres", "url": "postgresql://u:p@h/db", "config": {}}],
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1


def test_two_anonymous_visitors_dont_see_each_others_custom_connections(app_factory):
    # Two different browsers/sessions -> two different
    # "anonymous:<session_id>" identities (see auth.py's
    # ANONYMOUS_USER_ID_PREFIX) - one visitor's self-saved connection must
    # never be visible to, or overwritten by, another's, the same isolation
    # every other per-user identity in this app relies on.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    browser_one.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/one",
        "database_name": "Browser One DB", "is_custom": True,
    })

    assert browser_one.get('/api/config').get_json()['custom_databases'][0]['name'] == "Browser One DB"
    assert browser_two.get('/api/config').get_json()['custom_databases'] == []


def test_authenticated_user_still_never_sees_an_anonymous_visitors_custom_connection(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "Anon DB", "is_custom": True,
    })
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'] == []


# --- connection_key stability across a Settings-only rename --------------------

def test_settings_only_rename_preserves_connection_key_and_stays_active(app_env):
    """Phase 1 of the "Bring Your Own Data" relocation: savePreferences()
    (webClient's Preferences dialog) now sends a POST with ONLY
    `custom_databases` - no database_name/database_type/is_custom/other
    active-selection field at all (see config_routes.py's handle_config():
    the saved-connection LIST and "which connection is active this
    request" are fully independent concerns). Renaming the CURRENTLY
    ACTIVE custom connection through exactly that payload shape must reuse
    its own already-saved connection_key (see
    _parse_incoming_custom_databases()'s _stable_key()) rather than
    recomputing a fresh one from the new name - otherwise the session's
    stored connection_id (and in_scope_custom_connection_keys) would
    silently point at a connection_key that no longer exists in the saved
    list, orphaning the active connection the moment a GET is made, with
    no error anywhere. Contrast with
    test_connection_scope.py's test_post_config_reconciles_stale_in_scope_key_when_renaming_the_scoped_custom_connection,
    which covers the OLD combined-request shape (rename bundled with
    database_name/is_custom in the SAME POST, and no connection_key on the
    custom_databases row) - there, a fresh key is expected and the in-scope
    array is reconciled to it. Here, the row resends its own connection_key
    (exactly as client.js's buildCustomDatabasesPayload() does for an
    already-saved row - see its own "connection_key" comment), so the key
    must NOT change at all."""
    login_as(app_env.client, "alice@example.com")

    # Step (a): create AND activate a custom Postgres connection - plain
    # test setup, via an ordinary combined save.
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "Original Name", "is_custom": True,
        "custom_databases": [
            {"name": "Original Name", "type": "postgres", "url": "postgresql://u:p@h/db", "config": {}},
        ],
    })
    assert resp.status_code == 200
    before = app_env.client.get('/api/config').get_json()
    old_key = before["active_custom_connection_key"]
    assert old_key
    assert before["custom_database_name"] == "Original Name"
    assert before["active_is_custom"] is True

    # Step (b): a Settings-only rename - ONLY `custom_databases`, resending
    # this row's own connection_key, exactly what savePreferences() sends
    # now. No database_name/database_type/is_custom/auto_sql_execute/
    # in_scope_* field anywhere in this payload.
    resp2 = app_env.client.post('/api/config', json={
        "custom_databases": [
            {"connection_key": old_key, "name": "Renamed In Settings", "type": "postgres",
             "url": "postgresql://u:p@h/db", "config": {}},
        ],
    })
    assert resp2.status_code == 200

    # Step (c): the connection_key is unchanged, and it's still resolvable
    # as the active connection - not orphaned, not silently fallen back to
    # the default.
    after = app_env.client.get('/api/config').get_json()
    assert len(after["custom_databases"]) == 1
    assert after["custom_databases"][0]["connection_key"] == old_key
    assert after["custom_databases"][0]["name"] == "Renamed In Settings"
    assert after["active_custom_connection_key"] == old_key
    assert after["active_is_custom"] is True
    assert after["custom_database_name"] == "Renamed In Settings"
    assert after["in_scope_custom_connection_keys"] == [old_key]


# NOTE: the GET-side redaction of OTHER PEOPLE's preset connection strings
# (configured_databases without URLs, etc.) for anonymous visitors is gated
# on IS_CLOUD_RUN specifically, not just an anonymous identity, and stays in
# place regardless of whether that visitor also has their own custom
# connection - see test_config_cloud_run_anonymous_redaction.py, which
# requires mocking Firestore too, since IS_CLOUD_RUN=True without a working
# Firestore client is a hard startup RuntimeError by design (see
# app_config.py).
