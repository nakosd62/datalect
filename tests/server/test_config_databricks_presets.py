"""
Admin-configured Databricks presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the Databricks counterpart to
test_config_snowflake_presets.py: like Snowflake (and unlike BigQuery),
Databricks has no ambient/shared identity to authenticate presets with, so
a Databricks preset carries its own explicit access_token right in the
presets file - and that credential has to be copied into the session's
db_config wherever a preset gets selected, or every query against it fails
with "requires an access_token" - resolved fresh from CONFIGURED_DBS every
time (db.py's resolve_active_descriptor), never persisted on the session.
Both anonymous and authenticated users select a preset the same way, by
its stable "preset_id" (see config_routes.py's unified preset branch in
handle_config) - covered here for both user types. See
test_config_databricks.py for the separate custom (user-saved) connection
flow.
"""

from helpers import install_fake_databricks_connect, login_as, write_database_presets_file


def _preset_payload():
    return [{
        "type": "databricks", "name": "Sample Lakehouse", "server_hostname": "dbc-preset.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/preset123", "catalog": "main", "schema": "sales",
        "access_token": "preset-token",
    }]


def test_authenticated_preset_selection_connects_with_presets_token(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_databricks_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    # Mirrors what client.js's triggerConfigSave() posts for a preset radio
    # selection: just the preset's stable id, same as an anonymous visitor
    # (see config_routes.py's unified preset branch in handle_config).
    resp = env.client.post('/api/config', json={"preset_id": "databricks+Sample Lakehouse"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.calls[-1]["access_token"] == "preset-token"
    assert harness.calls[-1]["server_hostname"] == "dbc-preset.cloud.databricks.com"


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # A preset's own credential is redacted out of CONFIGURED_DBS for EVERY
    # visitor - authenticated or anonymous alike, regardless of environment
    # (see config_routes.py's handle_config comment on configured_dbs) -
    # being signed in earns no special access to another admin's secret.
    # Same status quo as test_config_snowflake_presets.py's equivalent test.
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'][0] == {"id": "databricks+Sample Lakehouse", "name": "Sample Lakehouse", "type": "databricks"}
    assert "access_token" not in data['configured_databases'][0]


def test_anonymous_preset_id_selection_connects_with_presets_token(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_databricks_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_id": "databricks+Sample Lakehouse"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["access_token"] == "preset-token"
    assert call["server_hostname"] == "dbc-preset.cloud.databricks.com"
    assert call["http_path"] == "/sql/1.0/warehouses/preset123"
    assert call["catalog"] == "main"
    assert call["schema"] == "sales"


def test_anonymous_visitor_never_receives_the_presets_credential(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "K_SERVICE": "ydyl-service",
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": path,
    }, mock_firestore=True)
    resp = env.client.get('/api/config')
    assert "preset-token" not in resp.get_data(as_text=True)
    assert resp.get_json()['configured_databases'] == [{"id": "databricks+Sample Lakehouse", "name": "Sample Lakehouse", "type": "databricks"}]
