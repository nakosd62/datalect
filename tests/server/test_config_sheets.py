"""
Custom (user-saved) Google Sheets connections through /api/config. Unlike
every other credentialed dialect's own test file, credentials_json here is
OPTIONAL rather than always-required or never-present: most of this file
covers the original credential-less shape (the "spreadsheet_url"/
"tab_name" fields round-trip correctly, a full URL is parsed down to a bare
spreadsheet id via extract_spreadsheet_id(), missing core fields is a
silent no-op, and /api/execute reaches backends.sheets's HTTP layer) - see
test_config_sheets_presets.py for the separate admin-preset path. The
"--- optional credentials_json ---" section below covers the newer
service-account credential path, mirroring test_config_custom_connections.py's
BigQuery credential-resolution tests (fresh-wins, falls back to saved,
never round-trips) but with the added "still optional" regression case
that must stay true: a Sheets connection with no credentials_json at all
remains a fully valid, public-sheet-only connection.
"""

from google.oauth2 import service_account

from helpers import login_as, make_service_account_key_json


def _sheets_payload(spreadsheet_url="https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit", tab_name="Sheet1", name="My Sheet"):
    return [{"type": "sheets", "name": name, "spreadsheet_url": spreadsheet_url, "tab_name": tab_name}]


def test_saving_a_custom_sheets_connection_round_trips_spreadsheet_id_and_tab_name(app_env, sheets_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit#gid=0",
        "tab_name": "Sheet1", "database_name": "My Sheet",
        "is_custom": True, "custom_databases": _sheets_payload(),
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_sheets_spreadsheet_id'] == "1AbCdEf2345"
    assert data['active_database_sheets_tab_name'] == "Sheet1"
    assert data['active_database_type'] == "sheets"


def test_bare_spreadsheet_id_is_accepted_directly(app_env, sheets_harness):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets", "spreadsheet_url": "1AbCdEf2345", "tab_name": "Sheet1",
        "database_name": "My Sheet", "is_custom": True,
        "custom_databases": _sheets_payload(spreadsheet_url="1AbCdEf2345"),
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_sheets_spreadsheet_id'] == "1AbCdEf2345"


def test_no_credential_dimension_saving_never_requires_a_password(app_env, sheets_harness):
    # Unlike every other structured dialect, there is nothing to reject
    # here for "missing credential" - a Sheets connection with both its
    # identifying fields present is always accepted outright.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
    })
    assert resp.status_code == 200


def test_missing_tab_name_is_treated_as_no_op_not_an_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "database_name": "My Sheet", "is_custom": True,
    })
    assert resp.status_code == 200


def test_missing_spreadsheet_url_is_treated_as_no_op_not_an_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets", "tab_name": "Sheet1",
        "database_name": "My Sheet", "is_custom": True,
    })
    assert resp.status_code == 200


def test_unparseable_spreadsheet_url_is_treated_as_no_op_not_an_error(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets", "spreadsheet_url": "https://example.com/not-a-sheet",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    # Never activated as a custom connection - falls back to the default.
    assert data['active_database_type'] != 'sheets'


def test_execute_reaches_the_resolved_spreadsheet_and_tab(app_env, sheets_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
    })
    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    resp = app_env.client.post('/api/execute', json={"sql": "select A"})
    assert resp.status_code == 200
    call = sheets_harness.calls[-1]
    assert call["url"] == "https://docs.google.com/spreadsheets/d/1AbCdEf2345/gviz/tq"
    assert call["params"]["sheet"] == "Sheet1"


def test_anonymous_user_can_save_a_custom_sheets_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    resp = env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'sheets'


def test_no_password_field_ever_appears_since_none_exists(app_env, sheets_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    for db in resp.get_json()['custom_databases']:
        cfg = db.get("config") or {}
        assert "password" not in cfg
        assert db.get("has_custom_credentials") in (False, None)


# --- optional credentials_json (service-account) path -------------------------
# GET /api/config's identity probe calls connect() for the active custom
# connection (see config_routes.py's module docstring) - for a credentialed
# Sheets connection that means a real (mocked) token refresh, so every test
# below that does a GET after saving a credentialed connection stubs
# Credentials.refresh the same way test_sheets_backend.py does.

def _stub_credentials_refresh(monkeypatch, token="fake-bearer-token"):
    def fake_refresh(self, request):
        self.token = token
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)


def test_public_sheets_connection_still_saves_with_no_credentials_json_key_present(app_env, sheets_harness):
    # The "must stay true" regression case: this dialect's whole point is
    # that a credential is optional, not that it's now required - a
    # connection with only spreadsheet_url/tab_name is still fully valid,
    # and its saved config must carry no credentials_json key at all.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
        "custom_databases": _sheets_payload(),
    })
    assert resp.status_code == 200
    assert resp.get_json().get("error") is None

    data = app_env.client.get('/api/config').get_json()
    for db in data['custom_databases']:
        assert "credentials_json" not in (db.get("config") or {})
        assert db.get("has_custom_credentials") in (False, None)


def test_saving_a_private_sheets_connection_persists_credentials_and_flags_it(app_env, monkeypatch, sheets_harness):
    _stub_credentials_refresh(monkeypatch)
    key_json = make_service_account_key_json(client_email="svc@proj.iam.gserviceaccount.com")
    login_as(app_env.client, "alice@example.com")
    payload = [{
        "type": "sheets", "name": "Private Sheet",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "credentials_json": key_json,
    }]
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "Private Sheet", "credentials_json": key_json,
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True
    assert data['active_uses_custom_credentials'] is True


def test_reselecting_a_sheets_connection_with_blank_key_reuses_its_own_saved_key(app_env, monkeypatch, sheets_harness):
    # Mirrors test_config_custom_connections.py's identical BigQuery test -
    # re-picking a saved private-sheet connection with credentials_json left
    # blank must resolve back to ITS OWN previously-saved key, not silently
    # drop it or reuse some other connection's key.
    _stub_credentials_refresh(monkeypatch)
    key_json = make_service_account_key_json(client_email="svc@proj.iam.gserviceaccount.com")
    login_as(app_env.client, "alice@example.com")
    payload = [{
        "type": "sheets", "name": "Private Sheet",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "credentials_json": key_json,
    }]
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "Private Sheet", "credentials_json": key_json,
        "is_custom": True, "custom_databases": payload,
    })
    # Re-save with credentials_json left blank - should reuse the saved key.
    resp = app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "Private Sheet", "credentials_json": "",
        "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['has_custom_credentials'] is True


def test_credentials_json_never_appears_anywhere_in_config_response_for_sheets(app_env, monkeypatch, sheets_harness):
    _stub_credentials_refresh(monkeypatch)
    key_json = make_service_account_key_json()
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "Private Sheet", "credentials_json": key_json,
        "is_custom": True,
    })
    resp = app_env.client.get('/api/config')
    assert key_json not in resp.get_data(as_text=True)


# --- ambient (app-wide) service-account fallback, applied to custom connections ---
# Confirms the SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE fallback (see
# backends/sheets.py's module docstring) reaches a user's own custom
# connection automatically - it's resolved fresh inside connect(), so
# nothing about /api/config's parsing/persistence needed to change for
# this to work.

def test_a_custom_connection_with_no_credential_uses_the_ambient_one_automatically(app_env, monkeypatch, tmp_path, sheets_harness):
    _stub_credentials_refresh(monkeypatch, token="ambient-token")
    key_path = tmp_path / "ambient-key.json"
    key_path.write_text(make_service_account_key_json(client_email="ambient@proj.iam.gserviceaccount.com"))
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(key_path))

    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "is_custom": True,
    })
    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    app_env.client.post('/api/execute', json={"sql": "select A"})
    assert sheets_harness.calls[-1]["headers"] == {"Authorization": "Bearer ambient-token"}


def test_a_custom_connections_own_credential_still_wins_over_the_ambient_one(app_env, monkeypatch, tmp_path, sheets_harness):
    _stub_credentials_refresh(monkeypatch, token="whichever-token")
    ambient_path = tmp_path / "ambient-key.json"
    ambient_path.write_text(make_service_account_key_json(client_email="ambient@proj.iam.gserviceaccount.com"))
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(ambient_path))

    own_key = make_service_account_key_json(client_email="own@proj.iam.gserviceaccount.com")
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "sheets",
        "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Sheet1", "database_name": "My Sheet", "credentials_json": own_key,
        "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['active_uses_custom_credentials'] is True
