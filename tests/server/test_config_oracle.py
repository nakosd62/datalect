"""
Custom (user-saved) Oracle connections through /api/config: multiple
connections that share a host/service_name but carry different passwords
(must not collide/overwrite each other - see compute_connection_key's
docstring in state_store.py), the has_custom_credentials/
active_uses_custom_credentials indicators, the "leave the credential field
blank to keep the previously-saved one" UX, and that the password field
never round-trips back to the frontend under any circumstance. Every test
here goes through the custom-connection flow - mirrors
test_config_databricks.py's coverage, minus the catalog dimension (Oracle
has no equivalent) plus the service_name-vs-sid dimension Databricks
doesn't have. See test_config_oracle_presets.py for the separate
admin-preset path.
"""

from helpers import login_as, install_fake_oracle_connect


def _custom_databases_payload(pw_a, pw_b):
    return [
        {"type": "oracle", "name": "Conn A", "host": "db-shared.example.com",
         "service_name": "ORCLPDB1", "user": "alice", "schema": "sales", "password": pw_a},
        {"type": "oracle", "name": "Conn B", "host": "db-shared.example.com",
         "service_name": "ORCLPDB1", "user": "alice", "schema": "sales", "password": pw_b},
    ]


def test_two_connections_sharing_host_service_but_different_passwords_both_persist(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "schema": "sales", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": _custom_databases_payload("PASS_A", "PASS_B"),
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    keys = {c["name"]: c["connection_key"] for c in data['custom_databases']}
    assert keys["Conn A"] != keys["Conn B"]
    assert data['active_custom_connection_key'] == keys["Conn A"]
    assert data['custom_database_name'] == "Conn A"


def test_switching_active_connection_between_two_that_share_host_uses_the_right_password(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    assert oracle_harness.calls[-1]["password"] == "PASS_A"

    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    assert oracle_harness.calls[-1]["password"] == "PASS_B"


def test_reselecting_a_connection_with_blank_password_reuses_its_own_saved_password(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    # Switch back to Conn A, password left blank.
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db-shared.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Conn A", "password": "",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    assert resp.get_json()["custom_database_name"] == "Conn A"

    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    assert oracle_harness.calls[-1]["password"] == "PASS_A"


# --- service_name vs sid -----------------------------------------------------

def test_sid_used_when_service_name_not_given(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "sid": "XE",
        "user": "alice", "password": "hunter2", "database_name": "XE Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = oracle_harness.calls[-1]
    assert call["sid"] == "XE"
    assert "service_name" not in call


def test_default_port_is_1521_when_omitted(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    assert oracle_harness.calls[-1]["port"] == 1521


# --- "ssl" -> TLS kwargs ------------------------------------------------
# Regression coverage for the real bug this flag fixes: a custom Oracle
# Cloud/Autonomous Database connection with no way to request TLS would
# connect() over plain TCP against a TLS-only listener and fail with a
# confusing DPY-4011/DPY-6005 "connection reset" rather than a normal auth
# error - see backends/oracle.py's module docstring.

def test_ssl_true_reaches_connect_as_tcps_protocol(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "adb.us-ashburn-1.oraclecloud.com", "port": 1522,
        "service_name": "myatp_high.adb.oraclecloud.com", "user": "admin",
        "password": "hunter2", "database_name": "ADB Conn", "is_custom": True, "ssl": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = oracle_harness.calls[-1]
    assert call["protocol"] == "tcps"
    assert call["ssl_server_dn_match"] is True


def test_ssl_omitted_reaches_connect_with_no_tls_kwargs(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = oracle_harness.calls[-1]
    assert "protocol" not in call
    assert "ssl_server_dn_match" not in call


def test_ssl_flag_is_saved_on_the_custom_databases_list_entry(app_env, oracle_harness):
    # client.js always resends a custom row's full config (host/port/
    # service_name/user/schema/ssl) on every save - only the password is
    # ever specially left blank to reuse the saved one - so this checks the
    # saved list entry itself carries "ssl" forward via GET, the same way
    # has_custom_credentials is checked above.
    login_as(app_env.client, "alice@example.com")
    payload = [{
        "type": "oracle", "name": "ADB Conn", "host": "adb.us-ashburn-1.oraclecloud.com",
        "port": 1522, "service_name": "myatp_high.adb.oraclecloud.com", "user": "admin",
        "schema": "sales", "password": "hunter2", "ssl": True,
    }]
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "adb.us-ashburn-1.oraclecloud.com", "port": 1522,
        "service_name": "myatp_high.adb.oraclecloud.com", "user": "admin",
        "password": "hunter2", "database_name": "ADB Conn", "is_custom": True, "ssl": True,
        "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['config']['ssl'] is True


# --- has_custom_credentials / active_uses_custom_credentials ---------------

def test_has_custom_credentials_true_for_oracle_connection(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "hunter2", "database_name": "Ora Conn", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


# --- credentials never leak -------------------------------------------------

def test_no_password_ever_appears_anywhere_in_config_response(app_env, oracle_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "SUPER_SECRET_PASSWORD", "database_name": "Ora Conn",
        "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert "SUPER_SECRET_PASSWORD" not in resp.get_data(as_text=True)
    for db in resp.get_json()['custom_databases']:
        cfg = db.get("config") or {}
        assert "password" not in cfg


# --- validation --------------------------------------------------------------

def test_missing_credential_is_rejected_with_clear_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "database_name": "Ora Conn", "is_custom": True,
    })
    assert resp.status_code == 400
    assert "Oracle" in resp.get_json()["error"]


def test_missing_core_fields_is_treated_as_no_op_not_an_error(app_env):
    # Mirrors BigQuery's/Snowflake's/Databricks' "missing required
    # identifying fields" behavior - not enough to even identify a
    # connection, so this is a silent no-op rather than a validation error
    # (e.g. a fresh blank row that only has a name typed in so far).
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "database_name": "Ora Conn", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200


def test_missing_user_is_treated_as_no_op_not_an_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "database_name": "Ora Conn", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200


# --- anonymous users ---------------------------------------------------------

def test_anonymous_user_can_save_a_custom_oracle_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_oracle_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "oracle", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "alice", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'oracle'
