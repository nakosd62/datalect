"""
Custom (user-saved) Databricks connections through /api/config: multiple
connections that share a server_hostname/http_path but carry different
access tokens (must not collide/overwrite each other - see
compute_connection_key's docstring in state_store.py), the
has_custom_credentials/active_uses_custom_credentials indicators, the
"leave the credential field blank to keep the previously-saved one" UX, and
that the access_token field never round-trips back to the frontend under
any circumstance. Every test here goes through the custom-connection flow -
mirrors test_config_snowflake.py's coverage, minus the two-auth-method
dimension (Databricks is PAT-only for this first pass - see
backends/databricks.py's module docstring). See
test_config_databricks_presets.py for the separate admin-preset path.
"""

from helpers import login_as, install_fake_databricks_connect


def _custom_databases_payload(token_a, token_b):
    return [
        {"type": "databricks", "name": "Conn A", "server_hostname": "dbc-shared.cloud.databricks.com",
         "http_path": "/sql/1.0/warehouses/shared", "catalog": "main", "schema": "sales", "access_token": token_a},
        {"type": "databricks", "name": "Conn B", "server_hostname": "dbc-shared.cloud.databricks.com",
         "http_path": "/sql/1.0/warehouses/shared", "catalog": "main", "schema": "sales", "access_token": token_b},
    ]


def test_two_connections_sharing_hostname_path_but_different_tokens_both_persist(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "catalog": "main", "schema": "sales",
        "database_name": "Conn A", "access_token": "TOKEN_A",
        "is_custom": True, "custom_databases": _custom_databases_payload("TOKEN_A", "TOKEN_B"),
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    keys = {c["name"]: c["connection_key"] for c in data['custom_databases']}
    assert keys["Conn A"] != keys["Conn B"]
    assert data['active_custom_connection_key'] == keys["Conn A"]
    assert data['custom_database_name'] == "Conn A"


def test_switching_active_connection_between_two_that_share_hostname_uses_the_right_token(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("TOKEN_A", "TOKEN_B")

    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "database_name": "Conn A", "access_token": "TOKEN_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert databricks_harness.calls[-1]["access_token"] == "TOKEN_A"

    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "database_name": "Conn B", "access_token": "TOKEN_B",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert databricks_harness.calls[-1]["access_token"] == "TOKEN_B"


def test_reselecting_a_connection_with_blank_token_reuses_its_own_saved_token(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("TOKEN_A", "TOKEN_B")

    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "database_name": "Conn A", "access_token": "TOKEN_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "database_name": "Conn B", "access_token": "TOKEN_B",
        "is_custom": True, "custom_databases": payload,
    })
    # Switch back to Conn A, token left blank.
    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-shared.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/shared", "database_name": "Conn A", "access_token": "",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    assert resp.get_json()["custom_database_name"] == "Conn A"

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert databricks_harness.calls[-1]["access_token"] == "TOKEN_A"


# --- optional catalog/schema -------------------------------------------------

def test_catalog_and_schema_are_optional_and_passed_through_when_given(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "catalog": "main", "schema": "sales",
        "database_name": "DBX Conn", "access_token": "tok", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = databricks_harness.calls[-1]
    assert call["catalog"] == "main"
    assert call["schema"] == "sales"


def test_connection_without_catalog_or_schema_omits_them(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "database_name": "DBX Conn", "access_token": "tok",
        "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = databricks_harness.calls[-1]
    assert "catalog" not in call
    assert "schema" not in call


# --- has_custom_credentials / active_uses_custom_credentials ---------------

def test_has_custom_credentials_true_for_databricks_connection(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "database_name": "DBX Conn", "access_token": "tok",
        "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


# --- credentials never leak -------------------------------------------------

def test_no_access_token_ever_appears_anywhere_in_config_response(app_env, databricks_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "database_name": "DBX Conn",
        "access_token": "SUPER_SECRET_DAPI_TOKEN", "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert "SUPER_SECRET_DAPI_TOKEN" not in resp.get_data(as_text=True)
    for db in resp.get_json()['custom_databases']:
        cfg = db.get("config") or {}
        assert "access_token" not in cfg


# --- validation --------------------------------------------------------------

def test_missing_credential_is_rejected_with_clear_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "database_name": "DBX Conn", "is_custom": True,
    })
    assert resp.status_code == 400
    assert "Databricks" in resp.get_json()["error"]


def test_missing_core_fields_is_treated_as_no_op_not_an_error(app_env):
    # Mirrors BigQuery's/Snowflake's "missing required identifying fields"
    # behavior - not enough to even identify a connection, so this is a
    # silent no-op rather than a validation error (e.g. a fresh blank row
    # that only has a name typed in so far).
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "databricks", "database_name": "DBX Conn", "access_token": "x", "is_custom": True,
    })
    assert resp.status_code == 200


# --- anonymous users ---------------------------------------------------------

def test_anonymous_user_can_save_a_custom_databricks_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_databricks_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "databricks", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "access_token": "x", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'databricks'
