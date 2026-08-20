"""
Custom (user-saved) Snowflake connections through /api/config: multiple
connections that share an account/database/schema but carry different
credentials (must not collide/overwrite each other - see
compute_connection_key's docstring in state_store.py), both supported auth
methods (password and key-pair), the has_custom_credentials/
active_uses_custom_credentials indicators, the "leave the credential field
blank to keep the previously-saved one" UX, and that no credential field
ever round-trips back to the frontend under any circumstance. Every test
here goes through the custom-connection flow - mirrors
test_config_custom_connections.py's BigQuery coverage. See
test_config_snowflake_presets.py for the separate admin-preset path
(app_config.py's DATABASE_PRESETS_FILE), which has its own credential
handling since a Snowflake preset - unlike a BigQuery one - carries its own
explicit credential.
"""

import pytest

from helpers import login_as, install_fake_snowflake_connect


def _custom_databases_payload(pw_a, pw_b):
    return [
        {"type": "snowflake", "name": "Conn A", "account": "shared-acc", "user": "alice",
         "warehouse": "wh", "database": "shared_db", "schema": "public", "password": pw_a},
        {"type": "snowflake", "name": "Conn B", "account": "shared-acc", "user": "alice",
         "warehouse": "wh", "database": "shared_db", "schema": "public", "password": pw_b},
    ]


def test_two_connections_sharing_account_database_but_different_passwords_both_persist(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": _custom_databases_payload("PASS_A", "PASS_B"),
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    keys = {c["name"]: c["connection_key"] for c in data['custom_databases']}
    assert keys["Conn A"] != keys["Conn B"]
    assert data['active_custom_connection_key'] == keys["Conn A"]
    assert data['custom_database_name'] == "Conn A"


def test_switching_active_connection_between_two_that_share_account_uses_the_right_password(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert snowflake_harness.calls[-1]["password"] == "PASS_A"

    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert snowflake_harness.calls[-1]["password"] == "PASS_B"


def test_reselecting_a_connection_with_blank_password_reuses_its_own_saved_password(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    # Switch back to Conn A, password left blank.
    resp = app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "shared-acc", "user": "alice", "warehouse": "wh",
        "database": "shared_db", "schema": "public", "database_name": "Conn A", "password": "",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    assert resp.get_json()["custom_database_name"] == "Conn A"

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert snowflake_harness.calls[-1]["password"] == "PASS_A"


# --- key-pair auth ---------------------------------------------------------

def test_key_pair_connection_persists_and_connects_with_jwt_authenticator(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "KP Conn", "private_key": "-----BEGIN PRIVATE KEY-----fake",
        "private_key_passphrase": "shh", "is_custom": True,
    })
    assert resp.status_code == 200

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = snowflake_harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert call["private_key"] == "-----BEGIN PRIVATE KEY-----fake"
    assert call["private_key_passphrase"] == "shh"
    assert "password" not in call


def test_switching_from_password_to_key_pair_drops_the_old_password(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "Conn", "password": "PASS_A", "is_custom": True,
    })
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "Conn", "private_key": "PEM", "is_custom": True,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = snowflake_harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert "password" not in call


# --- has_custom_credentials / active_uses_custom_credentials ---------------

def test_has_custom_credentials_true_for_snowflake_password_connection(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "SF Conn", "password": "hunter2", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


def test_has_custom_credentials_true_for_snowflake_private_key_connection(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "SF Conn", "private_key": "PEM", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


# --- credentials never leak -------------------------------------------------

def test_no_credential_field_ever_appears_anywhere_in_config_response(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "SF Conn", "password": "SUPER_SECRET_PASSWORD",
        "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert "SUPER_SECRET_PASSWORD" not in resp.get_data(as_text=True)
    for db in resp.get_json()['custom_databases']:
        cfg = db.get("config") or {}
        assert "password" not in cfg
        assert "private_key" not in cfg
        assert "private_key_passphrase" not in cfg


def test_private_key_never_leaks_either(app_env, snowflake_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "SF Conn", "private_key": "TOTALLY_SECRET_PEM_TEXT",
        "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert "TOTALLY_SECRET_PEM_TEXT" not in resp.get_data(as_text=True)


# --- validation --------------------------------------------------------------

def test_missing_credential_is_rejected_with_clear_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "database_name": "SF Conn", "is_custom": True,
    })
    assert resp.status_code == 400
    assert "Snowflake" in resp.get_json()["error"]


def test_missing_core_fields_is_treated_as_no_op_not_an_error(app_env):
    # Mirrors BigQuery's "missing project_id/dataset" behavior - not enough
    # to even identify a connection, so this is a silent no-op rather than
    # a validation error (the account/user/warehouse/database fields
    # aren't filled in yet, e.g. a fresh blank row).
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "snowflake", "database_name": "SF Conn", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200


# --- anonymous users ---------------------------------------------------------

def test_anonymous_user_can_save_a_custom_snowflake_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_snowflake_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acc1", "user": "alice", "warehouse": "wh",
        "database": "db1", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'snowflake'
