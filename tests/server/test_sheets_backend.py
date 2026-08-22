"""
backends/sheets.py, driven against helpers.FakeSheetsRequestsHarness (patches
backends.sheets's module-level `requests.get` - see helpers.py's own comment
for why this is a different shape from every other backend's fake: there's
no DB-API connect()-returning-a-live-object here, just one HTTP GET per
query). Canned response bodies are built with helpers.make_gviz_table_json/
make_gviz_error_json, matching the real gviz JSONP-wrapped JSON shape
confirmed live during this feature's design (see backends/sheets.py's module
docstring) - both the wrapped-callback and bare-JSON forms are exercised,
since which one `tqx=out:json` actually returns wasn't pinned to just one.

Coverage mirrors test_mssql_backend.py's shape where it makes sense, adapted
for an HTTP backend: connect() (pure validation, zero I/O - asserted
directly), cache_key/dialect_name/liveness_sql, identity_label() (DOES call
out, unlike connect()), get_schema() (letter/label/type/sample mapping,
>26-column rollover, failure/empty-cols -> None), execute() (single-
statement-only, f-vs-v date handling, gviz status:error -> ValueError with
detailed_message, HTTP 404 -> "not found", other non-200 -> generic message,
always a one-element result list), and the headers=1/params= consistency
requirements from the design's corrections.
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from google.oauth2 import service_account

from backends.sheets import SheetsBackend
from backends.base import DB_CONNECT_TIMEOUT_SECONDS
from helpers import install_fake_sheets_requests, make_service_account_key_json


def _sheets(monkeypatch):
    harness = install_fake_sheets_requests(monkeypatch)
    return SheetsBackend(), harness


def _conn():
    return {"spreadsheet_id": "abc123", "tab_name": "Sheet1"}


# --- dialect_name / liveness_sql ---------------------------------------------

def test_dialect_name_matches_translate_routes_key():
    # Must match the _DIALECT_PROMPT_INTROS key in translate_routes.py
    # exactly - that lookup is keyed by this attribute.
    assert SheetsBackend.dialect_name == "Google Visualization API Query Language"


def test_liveness_sql_is_a_bounded_select():
    assert SheetsBackend.liveness_sql == "select * limit 1"


# --- connect(): pure validation, zero I/O ------------------------------------

def test_connect_performs_no_http_request(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    backend.connect({"type": "sheets", "spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert harness.calls == []


def test_connect_returns_stripped_fields(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    conn = backend.connect({"spreadsheet_id": "  abc123  ", "tab_name": "  Sheet1  "})
    assert conn == {"spreadsheet_id": "abc123", "tab_name": "Sheet1"}


def test_connect_raises_when_spreadsheet_id_missing(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    try:
        backend.connect({"tab_name": "Sheet1"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


def test_connect_raises_when_tab_name_missing(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    try:
        backend.connect({"spreadsheet_id": "abc123"})
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert harness.calls == []


# --- connect(): credentials_json (service-account) path ----------------------
# Deliberately DOES perform real (mocked) work here, unlike the
# credential-less path above - see the module docstring's "connect() is
# now the live check for a credentialed connection" reasoning. The token
# refresh itself goes through google.auth.transport.requests' own internal
# session, a completely different code path from this module's own
# `requests` (used for the gviz GET) - so patching Credentials.refresh
# directly, not requests.get, is what actually intercepts it.

def test_connect_with_credentials_mints_a_bearer_token_and_stores_email(monkeypatch):
    backend, harness = _sheets(monkeypatch)

    def fake_refresh(self, request):
        self.token = "fake-bearer-token"
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)

    key_json = make_service_account_key_json(client_email="svc@proj.iam.gserviceaccount.com")
    conn = backend.connect({
        "spreadsheet_id": "abc123", "tab_name": "Sheet1", "credentials_json": key_json,
    })
    assert conn["_bearer_token"] == "fake-bearer-token"
    assert conn["_service_account_email"] == "svc@proj.iam.gserviceaccount.com"
    # Minting a token is real I/O, but it's a *different* HTTP call than the
    # gviz fetch this harness fakes - connect() itself must still never hit
    # the gviz endpoint.
    assert harness.calls == []


def test_connect_credential_less_path_stays_zero_io_alongside_the_credentialed_one(monkeypatch):
    # Documents that adding the credentialed branch above didn't merge the
    # two paths - a plain descriptor with no credentials_json still does
    # nothing but validate, exactly as test_connect_performs_no_http_request
    # already asserts on its own.
    backend, harness = _sheets(monkeypatch)
    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert "_bearer_token" not in conn
    assert harness.calls == []


def test_connect_raises_valueerror_on_bad_credentials_json(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    try:
        backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1", "credentials_json": "not json"})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "service-account key" in str(e)


# --- connect(): ambient (app-wide) service-account fallback -------------------
# SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE lets ONE key cover every preset/
# custom connection that doesn't carry its own credentials_json, instead of
# pasting the same key into each one - see the module docstring's "ambient
# identity" paragraph and _ambient_credentials_json()'s own docstring for
# why this is read fresh (via a plain env var + file, not cached at import)
# rather than baked in at module-load time.

def test_connect_falls_back_to_ambient_credentials_when_descriptor_has_none(monkeypatch, tmp_path):
    backend, harness = _sheets(monkeypatch)

    def fake_refresh(self, request):
        self.token = "ambient-token"
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)

    key_path = tmp_path / "ambient-key.json"
    key_path.write_text(make_service_account_key_json(client_email="ambient@proj.iam.gserviceaccount.com"))
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(key_path))

    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert conn["_bearer_token"] == "ambient-token"
    assert conn["_service_account_email"] == "ambient@proj.iam.gserviceaccount.com"
    assert harness.calls == []


def test_connect_prefers_descriptor_credential_over_ambient(monkeypatch, tmp_path):
    backend, harness = _sheets(monkeypatch)

    def fake_refresh(self, request):
        self.token = "whichever-token"
    monkeypatch.setattr(service_account.Credentials, "refresh", fake_refresh)

    ambient_path = tmp_path / "ambient-key.json"
    ambient_path.write_text(make_service_account_key_json(client_email="ambient@proj.iam.gserviceaccount.com"))
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(ambient_path))

    own_key = make_service_account_key_json(client_email="own@proj.iam.gserviceaccount.com")
    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1", "credentials_json": own_key})
    # The descriptor's OWN key wins - the ambient one is only a fallback.
    assert conn["_service_account_email"] == "own@proj.iam.gserviceaccount.com"


def test_connect_stays_public_when_no_env_var_is_set(monkeypatch):
    # Baseline: with SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE unset (the
    # normal case, and every other test in this file), a descriptor with no
    # credentials_json of its own stays fully public/credential-less -
    # unchanged from before this fallback existed.
    monkeypatch.delenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", raising=False)
    backend, harness = _sheets(monkeypatch)
    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert "_bearer_token" not in conn
    assert harness.calls == []


def test_connect_ignores_ambient_file_that_does_not_exist(monkeypatch):
    # A misconfigured/typo'd path logs a warning (not asserted here) and is
    # treated the same as "not configured" - never crashes, never raises
    # out of connect().
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", "/no/such/file.json")
    backend, harness = _sheets(monkeypatch)
    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert "_bearer_token" not in conn


def test_connect_ignores_ambient_file_with_invalid_json(monkeypatch, tmp_path):
    bad_path = tmp_path / "not-json.json"
    bad_path.write_text("not actually json")
    monkeypatch.setenv("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE", str(bad_path))
    backend, harness = _sheets(monkeypatch)
    conn = backend.connect({"spreadsheet_id": "abc123", "tab_name": "Sheet1"})
    assert "_bearer_token" not in conn


# --- cache_key ----------------------------------------------------------------

def test_cache_key_shape():
    backend = SheetsBackend()
    assert backend.cache_key({"spreadsheet_id": "abc123", "tab_name": "Sheet1"}) == "sheets:abc123/Sheet1"


def test_cache_key_handles_missing_fields():
    backend = SheetsBackend()
    assert backend.cache_key({}) == "sheets:unknown/unknown"


# --- identity_label(): DOES perform one live fetch, unlike connect() -------

def test_identity_label_performs_a_live_fetch_and_returns_tab_and_label(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "Name", "type": "string"}], rows=[["Reza"]])
    db_name, username = backend.identity_label(_conn())
    assert db_name == "Sheet1"
    assert username == "anonymous (public access)"
    assert len(harness.calls) == 1


def test_identity_label_propagates_on_failure(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(status_code=404, text="")
    try:
        backend.identity_label(_conn())
        assert False, "expected an exception"
    except ValueError:
        pass


def test_identity_label_reports_service_account_email_when_credentialed(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "Name", "type": "string"}], rows=[["Reza"]])
    conn = {
        "spreadsheet_id": "abc123", "tab_name": "Sheet1",
        "_bearer_token": "tok123", "_service_account_email": "svc@proj.iam.gserviceaccount.com",
    }
    db_name, username = backend.identity_label(conn)
    assert db_name == "Sheet1"
    assert username == "svc@proj.iam.gserviceaccount.com"


# --- _fetch(): Authorization header only when credentialed -------------------

def test_fetch_attaches_bearer_header_when_token_present(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    conn = {"spreadsheet_id": "abc123", "tab_name": "Sheet1", "_bearer_token": "tok123"}
    backend.execute(conn, "select A")
    assert harness.calls[-1]["headers"] == {"Authorization": "Bearer tok123"}


def test_fetch_omits_header_when_no_token(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.execute(_conn(), "select A")
    assert harness.calls[-1]["headers"] is None


def test_fetch_error_message_mentions_service_account_sharing_when_credentialed(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(status_code=403, text="")
    conn = {"spreadsheet_id": "abc123", "tab_name": "Sheet1", "_bearer_token": "tok123"}
    try:
        backend.execute(conn, "select A")
        assert False, "expected an exception"
    except RuntimeError as e:
        assert "service account" in str(e).lower()
        assert "Anyone with the link" not in str(e)


# --- _fetch() request shape: params=, headers=1, timeout tuple ---------------

def test_fetch_uses_params_not_hand_concatenated_url(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.execute({"spreadsheet_id": "abc123", "tab_name": "My Tab With Spaces"}, "select A")
    call = harness.calls[-1]
    assert call["url"] == "https://docs.google.com/spreadsheets/d/abc123/gviz/tq"
    assert call["params"]["sheet"] == "My Tab With Spaces"
    assert call["params"]["tq"] == "select A"


def test_fetch_sends_headers_1_on_execute_not_just_schema(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.execute(_conn(), "select A")
    assert harness.calls[-1]["params"]["headers"] == "1"


def test_fetch_sends_headers_1_on_schema_sample(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.get_schema(_conn())
    assert harness.calls[-1]["params"]["headers"] == "1"


def test_fetch_uses_connect_read_timeout_tuple_not_single_float(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.execute(_conn(), "select A")
    timeout = harness.calls[-1]["timeout"]
    assert isinstance(timeout, tuple)
    assert timeout[0] == DB_CONNECT_TIMEOUT_SECONDS
    assert timeout[1] > timeout[0]  # a more generous read timeout, not bounded by the connect one


# --- get_schema ----------------------------------------------------------------

def test_get_schema_lists_letter_label_type_and_samples(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(
        cols=[{"label": "Name", "type": "string"}, {"label": "Age", "type": "number"}],
        rows=[["Reza", 28], ["Amy", 34]],
    )
    schema = backend.get_schema(_conn())
    assert 'A: "Name" (string)' in schema
    assert 'B: "Age" (number)' in schema
    assert "Reza" in schema
    assert "no FROM clause" in schema
    assert "ONLY by the letter" in schema


def test_get_schema_uses_f_for_date_like_samples(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(
        cols=[{"label": "Created", "type": "date"}],
        rows=[[("Date(2024,0,12)", "2024-01-12")]],
    )
    schema = backend.get_schema(_conn())
    assert "2024-01-12" in schema
    assert "Date(2024,0,12)" not in schema


def test_get_schema_returns_none_when_cols_empty(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[], rows=[])
    assert backend.get_schema(_conn()) is None


def test_get_schema_returns_none_on_fetch_failure(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(status_code=404, text="")
    assert backend.get_schema(_conn()) is None


def test_get_schema_handles_more_than_26_columns(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    cols = [{"label": f"Col{i}", "type": "string"} for i in range(28)]
    harness.queue_table(cols=cols, rows=[])
    schema = backend.get_schema(_conn())
    assert 'AA: "Col26"' in schema
    assert 'AB: "Col27"' in schema


# --- execute --------------------------------------------------------------------

def test_execute_shapes_rows_as_dicts(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(
        cols=[{"label": "Name", "type": "string"}, {"label": "Age", "type": "number"}],
        rows=[["Reza", 28], ["Amy", 34]],
    )
    results = backend.execute(_conn(), "select A, B")
    assert results[0]["columns"] == ["Name", "Age"]
    assert results[0]["rows"] == [{"Name": "Reza", "Age": 28}, {"Name": "Amy", "Age": 34}]
    assert results[0]["rowCount"] == 2
    assert len(results) == 1


def test_execute_strips_exactly_one_trailing_semicolon(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[["x"]])
    backend.execute(_conn(), "select A;")
    assert harness.calls[-1]["params"]["tq"] == "select A"


def test_execute_uses_f_over_v_for_date_columns(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(
        cols=[{"label": "Created", "type": "datetime"}],
        rows=[[("Date(2024,0,12,9,30,0)", "2024-01-12 09:30:00")]],
    )
    results = backend.execute(_conn(), "select A")
    assert results[0]["rows"][0]["Created"] == "2024-01-12 09:30:00"


def test_execute_uses_v_for_non_date_columns(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "Age", "type": "number"}], rows=[[28]])
    results = backend.execute(_conn(), "select A")
    assert results[0]["rows"][0]["Age"] == 28


def test_execute_handles_null_cells(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_table(cols=[{"label": "A", "type": "string"}], rows=[[None]])
    results = backend.execute(_conn(), "select A")
    assert results[0]["rows"][0]["A"] is None


def test_execute_raises_valueerror_with_detailed_message_on_gviz_status_error(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_error("Invalid query: NO_COLUMN: ZZZNOTACOLUMN")
    try:
        backend.execute(_conn(), "select ZZZNOTACOLUMN")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "NO_COLUMN" in str(e)


def test_execute_raises_not_found_on_http_404(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(status_code=404, text="")
    try:
        backend.execute(_conn(), "select A")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "not found" in str(e).lower()


def test_execute_raises_generic_message_on_other_non_200(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(status_code=403, text="")
    try:
        backend.execute(_conn(), "select A")
        assert False, "expected an exception"
    except RuntimeError as e:
        assert "Anyone with the link" in str(e)


def test_execute_handles_bare_json_response_not_just_jsonp_wrapped(monkeypatch):
    from helpers import make_gviz_table_json
    backend, harness = _sheets(monkeypatch)
    harness.queue_response(
        status_code=200,
        text=make_gviz_table_json(cols=[{"label": "A", "type": "string"}], rows=[["x"]], wrapped=False),
    )
    results = backend.execute(_conn(), "select A")
    assert results[0]["rows"] == [{"A": "x"}]


def test_execute_blank_query_returns_zero_row_result_without_a_fetch(monkeypatch):
    backend, harness = _sheets(monkeypatch)
    results = backend.execute(_conn(), "   ")
    assert results == [{"statement": "", "columns": None, "rows": None, "rowCount": 0}]
    assert harness.calls == []
