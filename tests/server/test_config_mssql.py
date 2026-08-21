"""
Custom (user-saved) SQL Server connections through /api/config: multiple
connections that share a host/database but carry different passwords
(must not collide/overwrite each other - see compute_connection_key's
docstring in state_store.py), the has_custom_credentials/
active_uses_custom_credentials indicators, the "leave the credential field
blank to keep the previously-saved one" UX, and that the password field
never round-trips back to the frontend under any circumstance. Every test
here goes through the custom-connection flow - mirrors
test_config_redshift.py's coverage, minus the "TLS always on, never a
flag" dimension (SQL Server's "encrypt" IS a per-connection opt-in/out
flag, like Oracle's "ssl") - plus an "encrypt" round-trip test (including
the "explicit false must survive, not just be omitted" case - this
dialect's flag semantics genuinely differ from Oracle's, since connect()
itself defaults to True when the key is absent entirely) and a
default-port test. See test_config_mssql_presets.py for the separate
admin-preset path.
"""

from helpers import login_as, install_fake_mssql_connect


def _custom_databases_payload(pw_a, pw_b):
    return [
        {"type": "mssql", "name": "Conn A", "host": "server-shared.example.com",
         "database": "orders", "user": "alice", "schema": "sales", "password": pw_a},
        {"type": "mssql", "name": "Conn B", "host": "server-shared.example.com",
         "database": "orders", "user": "alice", "schema": "sales", "password": pw_b},
    ]


def test_two_connections_sharing_host_database_but_different_passwords_both_persist(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
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


def test_switching_active_connection_between_two_that_share_host_uses_the_right_password(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
        "user": "alice", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert mssql_harness.calls[-1]["password"] == "PASS_A"

    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
        "user": "alice", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert mssql_harness.calls[-1]["password"] == "PASS_B"


def test_reselecting_a_connection_with_blank_password_reuses_its_own_saved_password(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    payload = _custom_databases_payload("PASS_A", "PASS_B")

    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
        "user": "alice", "database_name": "Conn A", "password": "PASS_A",
        "is_custom": True, "custom_databases": payload,
    })
    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
        "user": "alice", "database_name": "Conn B", "password": "PASS_B",
        "is_custom": True, "custom_databases": payload,
    })
    # Switch back to Conn A, password left blank.
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server-shared.example.com", "database": "orders",
        "user": "alice", "database_name": "Conn A", "password": "",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    assert resp.get_json()["custom_database_name"] == "Conn A"

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert mssql_harness.calls[-1]["password"] == "PASS_A"


# --- default port --------------------------------------------------------

def test_default_port_is_1433_when_omitted(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert mssql_harness.calls[-1]["port"] == 1433


# --- "encrypt" flag round-tripping ----------------------------------------
# Regression coverage for the design decision that differs from Redshift's
# always-on TLS: "encrypt" is a per-connection opt-in/out boolean, like
# Oracle's "ssl" - but with the opposite absent-default (True, not False -
# see backends/mssql.py's module docstring). Critically, an explicit
# "encrypt": false must actually persist and reach connect() as "no cafile
# supplied", not just be silently omitted/forgotten - that's the one place
# this dialect's flag semantics genuinely differ from Oracle's ssl flag.

def test_encrypt_defaults_to_cafile_present_when_omitted(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert "cafile" in mssql_harness.calls[-1]


def test_explicit_encrypt_false_survives_and_omits_cafile(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
        "encrypt": False,
    })
    assert resp.status_code == 200
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert "cafile" not in mssql_harness.calls[-1]

    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_mssql_encrypt'] is False


def test_explicit_encrypt_true_survives_and_reports_true(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "hunter2", "database_name": "Conn", "is_custom": True,
        "encrypt": True,
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_mssql_encrypt'] is True


# --- has_custom_credentials / active_uses_custom_credentials ---------------

def test_has_custom_credentials_true_for_mssql_connection(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "hunter2", "database_name": "MS Conn", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


# --- credentials never leak -------------------------------------------------

def test_no_password_ever_appears_anywhere_in_config_response(app_env, mssql_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "SUPER_SECRET_PASSWORD", "database_name": "MS Conn",
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
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "database_name": "MS Conn", "is_custom": True,
    })
    assert resp.status_code == 400
    assert "SQL Server" in resp.get_json()["error"]


def test_missing_core_fields_is_treated_as_no_op_not_an_error(app_env):
    # Mirrors BigQuery's/Snowflake's/Databricks'/Oracle's/Redshift's
    # "missing required identifying fields" behavior - not enough to even
    # identify a connection, so this is a silent no-op rather than a
    # validation error (e.g. a fresh blank row that only has a name typed
    # in so far).
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "database_name": "MS Conn", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200


def test_missing_user_is_treated_as_no_op_not_an_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "database_name": "MS Conn", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200


# --- anonymous users ---------------------------------------------------------

def test_anonymous_user_can_save_a_custom_mssql_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_mssql_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "mssql", "host": "server.example.com", "database": "orders",
        "user": "alice", "password": "x", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'mssql'
