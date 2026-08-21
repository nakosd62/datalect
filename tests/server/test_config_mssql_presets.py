"""
Admin-configured SQL Server presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute - the SQL Server counterpart to
test_config_redshift_presets.py: like Databricks/Oracle/Redshift (and
unlike BigQuery), SQL Server has no ambient/shared identity to
authenticate presets with, so an mssql preset carries its own explicit
password right in the presets file - and that credential has to be copied
into the session's db_config wherever a preset gets selected, or every
query against it fails with "requires a user and password" - resolved
fresh from CONFIGURED_DBS every time (db.py's resolve_active_descriptor),
never persisted on the session. Both anonymous and authenticated users
select a preset the same way, by its stable "preset_id" (see
config_routes.py's unified preset branch in handle_config) - covered here
for both user types. Unlike Redshift's presets (TLS always on, never a
flag), an mssql preset's "encrypt" is a plain optional boolean, same as
Oracle's "ssl" - covered here too. See test_config_mssql.py for the
separate custom (user-saved) connection flow.
"""

from helpers import install_fake_mssql_connect, login_as, write_database_presets_file


def _preset_payload(encrypt=None):
    preset = {
        "type": "mssql", "name": "Orders (SQL Server)", "host": "server-preset.example.com",
        "port": 1433, "database": "orders", "user": "svc_ydyl", "schema": "sales",
        "password": "preset-password",
    }
    if encrypt is not None:
        preset["encrypt"] = encrypt
    return [preset]


def test_authenticated_preset_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_mssql_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    # Mirrors what client.js's triggerConfigSave() posts for a preset radio
    # selection: just the preset's stable id, same as an anonymous visitor
    # (see config_routes.py's unified preset branch in handle_config).
    resp = env.client.post('/api/config', json={"preset_id": "mssql+Orders (SQL Server)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.calls[-1]["password"] == "preset-password"
    assert harness.calls[-1]["server"] == "server-preset.example.com"


def test_preset_credential_never_appears_in_config_response(app_factory, tmp_path, monkeypatch):
    # A preset's own credential is redacted out of CONFIGURED_DBS for EVERY
    # visitor - authenticated or anonymous alike, regardless of environment
    # (see config_routes.py's handle_config comment on configured_dbs) -
    # being signed in earns no special access to another admin's secret.
    # Same status quo as test_config_redshift_presets.py's equivalent test.
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'][0] == {"id": "mssql+Orders (SQL Server)", "name": "Orders (SQL Server)", "type": "mssql"}
    assert "password" not in data['configured_databases'][0]


def test_anonymous_preset_id_selection_connects_with_presets_password(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })
    harness = install_fake_mssql_connect(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_id": "mssql+Orders (SQL Server)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    call = harness.calls[-1]
    assert call["password"] == "preset-password"
    assert call["server"] == "server-preset.example.com"
    assert call["database"] == "orders"
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
    assert resp.get_json()['configured_databases'] == [{"id": "mssql+Orders (SQL Server)", "name": "Orders (SQL Server)", "type": "mssql"}]


def test_preset_encrypt_false_omits_cafile(app_factory, tmp_path, monkeypatch):
    # Regression coverage: an admin preset can opt out of encryption just
    # like a custom connection can - "encrypt": false in the presets file
    # must actually reach connect() as "no cafile supplied", not be
    # silently defaulted back to True by this layer.
    path = write_database_presets_file(tmp_path, _preset_payload(encrypt=False))
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_mssql_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "mssql+Orders (SQL Server)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert "cafile" not in harness.calls[-1]


def test_preset_encrypt_true_includes_cafile(app_factory, tmp_path, monkeypatch):
    path = write_database_presets_file(tmp_path, _preset_payload(encrypt=True))
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_mssql_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "mssql+Orders (SQL Server)"})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert "cafile" in harness.calls[-1]
