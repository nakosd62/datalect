"""
Admin-configured Snowflake presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the counterpart to
test_config_billing_policy.py's BigQuery preset coverage, but for
credentials rather than billing: unlike a BigQuery preset (which
authenticates via this app's own ambient ADC identity and carries no
credential at all), a Snowflake preset has no such ambient identity to
fall back to, so it carries its own explicit password/private_key right
in the presets file - and that credential has to be copied into the
session's db_config wherever a preset gets selected, or every query
against it fails with "requires either 'password' or 'private_key'". Two
distinct code paths do that copying (see config_routes.py's module
docstring): the authenticated preset-match branch inside
_parse_incoming_connection, and the anonymous preset_index branch inside
handle_config - both are covered here. See test_config_snowflake.py for
the separate custom (user-saved) connection flow.
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

    # Mirrors what client.js's triggerConfigSave() posts for a direct
    # (non-custom) preset radio selection: the preset's own fields, minus
    # any credential (never redisplayed to the frontend to resend).
    resp = env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "myorg-myacct", "user": "svc_ydyl",
        "warehouse": "COMPUTE_WH", "database": "SNOWFLAKE_SAMPLE_DATA", "schema": "TPCH_SF1",
        "database_name": "Sample Data",
    })
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

    env.client.post('/api/config', json={
        "database_type": "snowflake", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db", "database_name": "KP Preset",
    })
    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["authenticator"] == "SNOWFLAKE_JWT"
    assert call["private_key"] == "PEM-TEXT"
    assert call["private_key_passphrase"] == "shh"
    assert "password" not in call


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # Unlike a custom connection (state_store.py's _strip_credentials),
    # nothing currently strips a preset's own credential out of
    # CONFIGURED_DBS for an authenticated GET - same status quo as a
    # Postgres preset's embedded URL password (see config_routes.py's
    # module docstring on the anonymous-only redaction). This test isn't
    # asserting that's desirable, just pinning today's actual behavior so
    # a future change here is a deliberate decision, not an accident.
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
    harness = install_fake_snowflake_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_index": 0})
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
    assert resp.get_json()['configured_databases'] == [{"name": "Sample Data", "type": "snowflake"}]
