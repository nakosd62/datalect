"""
Admin-configured Oracle presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the Oracle counterpart to
test_config_databricks_presets.py: like Databricks (and unlike BigQuery),
Oracle has no ambient/shared identity to authenticate presets with, so an
Oracle preset carries its own explicit password right in the presets file -
and that credential has to be copied into the session's db_config wherever
a preset gets selected, or every query against it fails with "requires a
user and password" - resolved fresh from CONFIGURED_DBS every time (db.py's
resolve_active_descriptor), never persisted on the session. Both anonymous
and authenticated users select a preset the same way, by its stable
"preset_id" (see config_routes.py's unified preset branch in
handle_config) - covered here for both user types. See
test_config_oracle.py for the separate custom (user-saved) connection
flow.
"""

from helpers import (
    install_fake_oracle_connect, login_as, write_database_presets_file, FAKE_DB_CONFIG_ENCRYPTION_KEY,
)


def _preset_payload():
    return [{
        "type": "oracle", "name": "Orders (Oracle)", "host": "db-preset.example.com",
        "port": 1521, "service_name": "ORCLPDB1", "user": "svc_ydyl", "schema": "sales",
        "password": "preset-password",
    }]


def test_authenticated_preset_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_oracle_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    # Mirrors what client.js's triggerConfigSave() posts for a preset radio
    # selection: just the preset's stable id, same as an anonymous visitor
    # (see config_routes.py's unified preset branch in handle_config).
    resp = env.client.post('/api/config', json={"preset_id": "oracle+Orders (Oracle)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    assert harness.calls[-1]["password"] == "preset-password"
    assert harness.calls[-1]["host"] == "db-preset.example.com"


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # A preset's own credential is redacted out of CONFIGURED_DBS for EVERY
    # visitor - authenticated or anonymous alike, regardless of environment
    # (see config_routes.py's handle_config comment on configured_dbs) -
    # being signed in earns no special access to another admin's secret.
    # Same status quo as test_config_databricks_presets.py's equivalent test.
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'][0] == {"id": "oracle+Orders (Oracle)", "name": "Orders (Oracle)", "type": "oracle"}
    assert "password" not in data['configured_databases'][0]


def test_anonymous_preset_id_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_oracle_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_id": "oracle+Orders (Oracle)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = harness.calls[-1]
    assert call["password"] == "preset-password"
    assert call["host"] == "db-preset.example.com"
    assert call["service_name"] == "ORCLPDB1"
    assert call["user"] == "svc_ydyl"


def _adb_preset_payload():
    return [{
        "type": "oracle", "name": "ADB (Oracle)", "host": "adb.us-ashburn-1.oraclecloud.com",
        "port": 1522, "service_name": "myatp_high.adb.oraclecloud.com", "user": "admin",
        "password": "preset-password", "ssl": True,
    }]


def test_authenticated_preset_selection_with_ssl_connects_over_tls(app_factory, tmp_path, monkeypatch):
    # Regression coverage for the real-world bug that motivated "ssl" -
    # without it threaded through the admin-preset-selection copy-over,
    # this preset would silently connect over plain TCP against a TLS-only
    # Autonomous Database listener and fail with DPY-4011/DPY-6005 (see
    # backends/oracle.py's module docstring).
    path = write_database_presets_file(tmp_path, _adb_preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_oracle_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "oracle+ADB (Oracle)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = harness.calls[-1]
    assert call["protocol"] == "tcps"
    assert call["ssl_server_dn_match"] is True


def test_anonymous_preset_id_selection_with_ssl_connects_over_tls(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _adb_preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_oracle_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_id": "oracle+ADB (Oracle)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1 FROM DUAL;"})
    call = harness.calls[-1]
    assert call["protocol"] == "tcps"
    assert call["ssl_server_dn_match"] is True


def test_anonymous_visitor_never_receives_the_presets_credential(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "K_SERVICE": "ydyl-service",
        "DB_CONFIG_ENCRYPTION_KEY": FAKE_DB_CONFIG_ENCRYPTION_KEY,
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": path,
    }, mock_firestore=True)
    resp = env.client.get('/api/config')
    assert "preset-password" not in resp.get_data(as_text=True)
    assert resp.get_json()['configured_databases'] == [{"id": "oracle+Orders (Oracle)", "name": "Orders (Oracle)", "type": "oracle"}]
