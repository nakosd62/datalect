"""
Admin-configured Snowflake presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the counterpart to
test_config_billing_policy.py's BigQuery preset coverage, but for
credentials rather than billing: unlike a BigQuery preset (which
authenticates via this app's own ambient ADC identity and carries no
credential at all), a Snowflake preset has no such ambient identity to
fall back to, so it carries its own explicit password/private_key right
in the presets file - resolved fresh from CONFIGURED_DBS every time
(db.py's resolve_active_descriptor), never persisted on the session, or
every query against it fails with "requires either 'password' or
'private_key'". Both anonymous and authenticated users select a preset
the same way, by its stable "preset_id" (see config_routes.py's unified
preset branch in handle_config) - both are covered here. See
test_config_snowflake.py for the separate custom (user-saved) connection
flow.
"""

from helpers import install_fake_snowflake_connect, login_as, write_database_presets_file


def _preset_payload():
    return [{
        "type": "snowflake", "name": "Sample Data", "account": "myorg-myacct", "user": "svc_ydyl",
        "warehouse": "COMPUTE_WH", "database": "SNOWFLAKE_SAMPLE_DATA", "schema": "TPCH_SF1",
        "role": "ACCOUNTADMIN", "password": "preset-password",
    }]


def test_authenticated_preset_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_snowflake_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    # Mirrors what client.js's triggerConfigSave() posts for a preset radio
    # selection: just the preset's stable id, same as an anonymous visitor
    # (see config_routes.py's unified preset branch in handle_config).
    resp = env.client.post('/api/config', json={"preset_id": "snowflake+Sample Data"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.calls[-1]["password"] == "preset-password"
    assert harness.calls[-1]["account"] == "myorg-myacct"


def test_authenticated_preset_selection_with_private_key_connects_with_jwt_authenticator(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "KP Preset", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db", "private_key": "PEM-TEXT",
        "private_key_passphrase": "shh",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_snowflake_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    env.client.post('/api/config', json={"preset_id": "snowflake+KP Preset"})
    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert call["private_key"] == "PEM-TEXT"
    assert call["private_key_passphrase"] == "shh"
    assert "password" not in call


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # A preset's own credential is redacted out of CONFIGURED_DBS for EVERY
    # visitor - authenticated or anonymous alike, regardless of environment
    # (see config_routes.py's handle_config comment on configured_dbs) -
    # being signed in earns no special access to another admin's secret,
    # same as a Postgres preset's embedded URL password. This test isn't
    # just pinning status quo - it's asserting the actual guarantee.
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'][0] == {"id": "snowflake+Sample Data", "name": "Sample Data", "type": "snowflake"}
    assert "password" not in data['configured_databases'][0]


def test_anonymous_preset_id_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_snowflake_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_id": "snowflake+Sample Data"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["password"] == "preset-password"
    assert call["account"] == "myorg-myacct"
    assert call["warehouse"] == "COMPUTE_WH"
    assert call["database"] == "SNOWFLAKE_SAMPLE_DATA"
    assert call["schema"] == "TPCH_SF1"
    assert call["role"] == "ACCOUNTADMIN"


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
    assert resp.get_json()['configured_databases'] == [{"id": "snowflake+Sample Data", "name": "Sample Data", "type": "snowflake"}]
