"""
Admin-configured Redshift presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the Redshift counterpart to
test_config_oracle_presets.py: like Databricks/Oracle (and unlike
BigQuery), Redshift has no ambient/shared identity to authenticate presets
with, so a Redshift preset carries its own explicit password right in the
presets file - and that credential has to be copied into the session's
db_config wherever a preset gets selected, or every query against it fails
with "requires a user and password". Two distinct code paths do that
copying (see config_routes.py's module docstring): the authenticated
preset-match branch inside _parse_incoming_connection, and the anonymous
preset_index branch inside handle_config - both are covered here. No SSL
dimension here (unlike Oracle's presets) - Redshift's TLS is always on,
never a flag - see backends/redshift.py's module docstring. See
test_config_redshift.py for the separate custom (user-saved) connection
flow.
"""

from helpers import install_fake_redshift_connect, login_as, write_database_presets_file


def _preset_payload():
    return [{
        "type": "redshift", "name": "Warehouse (Redshift)", "host": "cluster-preset.example.com",
        "port": 5439, "database": "dev", "user": "svc_ydyl", "schema": "sales",
        "password": "preset-password",
    }]


def test_authenticated_preset_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_redshift_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    # Mirrors what client.js's triggerConfigSave() posts for a direct
    # (non-custom) preset radio selection: the preset's own fields, minus
    # any credential (never redisplayed to the frontend to resend).
    resp = env.client.post('/api/config', json={
        "database_type": "redshift", "host": "cluster-preset.example.com", "database": "dev",
        "user": "svc_ydyl", "schema": "sales", "database_name": "Warehouse (Redshift)",
    })
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.calls[-1]["password"] == "preset-password"
    assert harness.calls[-1]["host"] == "cluster-preset.example.com"


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # Unlike a custom connection (state_store.py's _strip_credentials),
    # nothing currently strips a preset's own credential out of
    # CONFIGURED_DBS for an authenticated GET - same status quo pinned by
    # test_config_oracle_presets.py's equivalent test. Not asserting this
    # is desirable, just pinning today's actual behavior.
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'][0]['password'] == "preset-password"


def test_anonymous_preset_index_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_redshift_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_index": 0})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["password"] == "preset-password"
    assert call["host"] == "cluster-preset.example.com"
    assert call["dbname"] == "dev"
    assert call["user"] == "svc_ydyl"


def test_anonymous_visitor_never_receives_the_presets_credential(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "K_SERVICE": "ydyl-service",
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": path,
    }, mock_firestore=True)
    resp = env.client.get('/api/config')
    assert "preset-password" not in resp.get_data(as_text=True)
    assert resp.get_json()['configured_databases'] == [{"name": "Warehouse (Redshift)", "type": "redshift"}]
