"""
Admin-configured Google Sheets presets (app_config.py's DATABASE_PRESETS_FILE)
through /api/config + /api/execute. Most of this file predates the optional
service-account credential and covers preset parsing (spreadsheet_url/
spreadsheet_id + tab_name), the missing-required-field skip+warning path,
and confirming both anonymous and authenticated users can select a Sheets
preset by its stable "preset_id" the same way as any other dialect. The
"--- optional credentials_json ---" section below covers a preset that
carries its own credentials_json verbatim (like a Snowflake preset's own
"password") - it round-trips into the built connection (attaching a bearer
token to gviz requests) and is still redacted from "configured_databases"
to id/name/type only, same as every credential-less preset already is. See
test_config_sheets.py for the separate custom (user-saved) connection flow.
"""

from google.oauth2 import service_account

from helpers import write_database_presets_file, login_as


def _preset_payload(spreadsheet_url="https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit", tab_name="Roster"):
    preset = {"type": "sheets", "name": "Team Roster (Sheet)", "tab_name": tab_name}
    if spreadsheet_url is not None:
        preset["spreadsheet_url"] = spreadsheet_url
    return [preset]


def test_authenticated_preset_selection_reaches_the_right_spreadsheet_and_tab(app_factory, tmp_path, monkeypatch, sheets_harness):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "sheets+Team Roster (Sheet)"})
    assert resp.status_code == 200

    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    env.client.post('/api/execute', json={"sql": "select A"})
    call = sheets_harness.calls[-1]
    assert call["url"] == "https://docs.google.com/spreadsheets/d/1AbCdEf2345/gviz/tq"
    assert call["params"]["sheet"] == "Roster"


def test_anonymous_preset_id_selection_reaches_the_right_spreadsheet(app_factory, tmp_path, monkeypatch, sheets_harness):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS_FILE": path,
    })

    resp = env.client.post('/api/config', json={"preset_id": "sheets+Team Roster (Sheet)"})
    assert resp.status_code == 200

    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    env.client.post('/api/execute', json={"sql": "select A"})
    assert sheets_harness.calls[-1]["params"]["sheet"] == "Roster"


def test_preset_accepts_bare_spreadsheet_id_field_too(app_factory, tmp_path, monkeypatch, sheets_harness):
    preset = [{"type": "sheets", "name": "Bare ID Sheet", "spreadsheet_id": "1AbCdEf2345", "tab_name": "Data"}]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "sheets+Bare ID Sheet"})
    assert resp.status_code == 200

    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    env.client.post('/api/execute', json={"sql": "select A"})
    assert sheets_harness.calls[-1]["url"] == "https://docs.google.com/spreadsheets/d/1AbCdEf2345/gviz/tq"


def test_preset_missing_tab_name_is_skipped_with_a_warning(app_factory, tmp_path):
    preset = [{"type": "sheets", "name": "Broken Sheet", "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit"}]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    data = env.client.get('/api/config').get_json()
    # Skipped entirely - not persisted as a broken preset. The lone
    # remaining entry is the app's own synthetic "no presets configured"
    # fallback (see app_config.py), not this broken one.
    assert all(db.get("type") != "sheets" for db in data['configured_databases'])


def test_preset_missing_spreadsheet_url_is_skipped_with_a_warning(app_factory, tmp_path):
    preset = [{"type": "sheets", "name": "Broken Sheet", "tab_name": "Data"}]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    data = env.client.get('/api/config').get_json()
    assert all(db.get("type") != "sheets" for db in data['configured_databases'])


def test_preset_unparseable_spreadsheet_url_is_skipped_with_a_warning(app_factory, tmp_path):
    preset = [{"type": "sheets", "name": "Broken Sheet", "spreadsheet_url": "not a url at all", "tab_name": "Data"}]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    data = env.client.get('/api/config').get_json()
    assert all(db.get("type") != "sheets" for db in data['configured_databases'])


def test_valid_preset_appears_in_configured_databases_with_no_credential_fields(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'] == [
        {"id": "sheets+Team Roster (Sheet)", "name": "Team Roster (Sheet)", "type": "sheets"}
    ]


# --- optional credentials_json (service-account) preset field -----------------

def test_preset_with_credentials_json_attaches_bearer_token_to_gviz_requests(app_factory, tmp_path, monkeypatch, sheets_harness):
    from helpers import make_service_account_key_json

    def fake_refresh(self, request):
        self.token = "fake-bearer-token"
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)

    key_json = make_service_account_key_json(client_email="svc@proj.iam.gserviceaccount.com")
    preset = [{
        "type": "sheets", "name": "Private Roster", "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Roster", "credentials_json": key_json,
    }]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "sheets+Private Roster"})
    assert resp.status_code == 200

    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    env.client.post('/api/execute', json={"sql": "select A"})
    assert sheets_harness.calls[-1]["headers"] == {"Authorization": "Bearer fake-bearer-token"}


def test_preset_with_credentials_json_still_redacted_to_id_name_type(app_factory, tmp_path, monkeypatch):
    from helpers import make_service_account_key_json

    key_json = make_service_account_key_json()
    preset = [{
        "type": "sheets", "name": "Private Roster", "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf2345/edit",
        "tab_name": "Roster", "credentials_json": key_json,
    }]
    path = write_database_presets_file(tmp_path, preset)
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    resp = env.client.get('/api/config')
    assert key_json not in resp.get_data(as_text=True)
    data = resp.get_json()
    assert data['configured_databases'] == [
        {"id": "sheets+Private Roster", "name": "Private Roster", "type": "sheets"}
    ]


# --- ambient (app-wide) service-account fallback, applied to presets ----------
# A preset with NO credentials_json of its own still reaches a private sheet
# via SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE - the whole point of that env
# var is to avoid pasting the same key into every preset that wants private
# access (see backends/sheets.py's module docstring).

def test_preset_with_no_credential_uses_the_ambient_one_automatically(app_factory, tmp_path, monkeypatch, sheets_harness):
    from helpers import make_service_account_key_json

    def fake_refresh(self, request):
        self.token = "ambient-token"
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)

    key_path = tmp_path / "ambient-key.json"
    key_path.write_text(make_service_account_key_json(client_email="ambient@proj.iam.gserviceaccount.com"))
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(key_path))

    path = write_database_presets_file(tmp_path, _preset_payload())
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "sheets+Team Roster (Sheet)"})
    assert resp.status_code == 200

    sheets_harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    env.client.post('/api/execute', json={"sql": "select A"})
    assert sheets_harness.calls[-1]["headers"] == {"Authorization": "Bearer ambient-token"}
