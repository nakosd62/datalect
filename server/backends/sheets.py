"""
backends/sheets.py

SheetsBackend: talks to a single tab of a Google Sheet via Google's public
"gviz" endpoint (the same machinery behind a spreadsheet's own =QUERY()
formula), instead of a DB-API driver - there's no real "database" here, just
one HTTP GET per query against
    https://docs.google.com/spreadsheets/d/<spreadsheet_id>/gviz/tq
The query language accepted by this endpoint (the Google Visualization API
Query Language, "GViz") is SQL-like but genuinely distinct: no FROM clause
at all (the data source/tab is always implicit), no joins/subqueries/CASE,
and columns are addressed by spreadsheet letter (A, B, C, ...) by position,
never by header/label text. See translate_routes.py's dialect prompt intro
for the full list of what this grammar does and doesn't support.

A Sheets descriptor looks like:
    {"type": "sheets", "url": "sheets://<spreadsheet_id>/<tab_name>",
     "spreadsheet_id": "...", "tab_name": "...", "credentials_json": "..."}

Credential model: a Sheets connection is credential-less by default - a
spreadsheet genuinely shared as "Anyone with the link can view" (or
published to the web) needs nothing at all, and this remains the common,
zero-config case with connect() staying pure/I/O-free for it (see connect()
below). A connection MAY also carry an optional "credentials_json" (a
pasted service-account key, JSON-encoded) - the same field name/shape
BigQuery's custom connections already use - for reaching a PRIVATE
spreadsheet the sheet's owner has explicitly shared with that service
account's email (Viewer access, exactly like sharing with any person).
This is deliberately NOT a per-signed-in-user OAuth flow: the app still
never asks a yDyL user to grant it broad access to their own Google
account (see auth.py - the only OAuth concept there remains verifying a
signed-in user's ID token, never minting an access token on their behalf).
The only "auth" step is the sheet owner sharing with a specific
service-account email, entirely outside this app.

Verified against a live private sheet + real service account (2026-08):
gviz/tq DOES accept a service-account bearer token for private-sheet
access - but only under certain scopes, which is why
SHEETS_CREDENTIAL_SCOPES below is "drive.readonly", not the seemingly-more-
correct "spreadsheets.readonly". A real test against all four plausible
scopes found: "spreadsheets.readonly" -> gviz rejects it with a plain
HTTP 401 (no body reason given) even though the real Sheets API v4
(spreadsheets.values.get) accepts that exact same token/scope
successfully; "spreadsheets" (full, not read-only), "drive.readonly", and
"drive" all work fine against gviz. So this isn't a sharing/ACL problem
and isn't specific to service accounts vs. real users after all - it's
that gviz, being a legacy endpoint, simply doesn't recognize the newer,
narrower "spreadsheets.readonly" scope as valid, while it does recognize
the older/broader ones. "drive.readonly" is the least-privileged scope of
the three that actually work, so that's what's used - if a future test
ever shows gviz rejecting it too (e.g. Google tightens this further),
"drive" is the next thing to try, though at that point switching the
credentialed path over to the real, documented Sheets API v4
(spreadsheets.values.get - confirmed working above, but with no query
language of its own, unlike gviz) is probably the better fix rather than
reaching for progressively broader legacy-endpoint scopes.

Ambient (shared, app-wide) identity: pasting the SAME service-account key
into every preset/custom connection that wants private-sheet access is
real, avoidable duplication - one service account is normally shared with
many spreadsheets, not one per key. So connect() also supports a single
key configured once for the whole app, via the SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE
env var (a path to a downloaded service-account JSON key file) - used as a
fallback whenever a connection descriptor (preset or custom, doesn't
matter which) doesn't carry its own "credentials_json". Precedence is
simple: an explicit per-connection credentials_json always wins; the
ambient key is only ever a fallback, so a specific connection can still
opt into a different/dedicated service account by supplying its own.

This mirrors BigQuery presets' own "authenticate as the app's ambient
identity" pattern (Application Default Credentials) - but deliberately
through a SEPARATE, Sheets-specific env var rather than reusing the
standard GOOGLE_APPLICATION_CREDENTIALS ADC path: that variable is truly
global to every Google client library in the process, so pointing it at a
Sheets-only key would silently also become BigQuery presets' ADC identity
(and vice versa) - two conceptually unrelated identities that happen to
share a "file path in an env var" shape. See _ambient_credentials_json()
below for the read/parse/failure-logging details. Deliberately NOT cached
at import time - see that function's own docstring for why.

Nothing about this is persisted anywhere: the ambient key never gets
written into a saved custom connection's config or a preset's descriptor -
it's resolved fresh, at connect()-time, every time a connection doesn't
supply its own. This is what keeps rotating the shared key a one-file
change rather than a find-and-replace across every preset/saved connection
that relies on it.

One tab is one "table" - there is no multi-table concept for this dialect,
so get_schema()/execute() never need the date-shard-family/table-count-cap
machinery every other backend's get_schema() uses (backends/base.py's
group_date_sharded_tables/cap_kept_tables) - only cap_schema_text as a
final length backstop, same as everywhere else.

First-pass, narrow limitations worth flagging plainly (same posture as
Oracle's/Databricks'/mssql's own docstrings): a genuinely private/
inaccessible-but-existing spreadsheet's exact HTTP response shape was not
independently verified against a live example - handled defensively with a
generic "make sure it's shared" message rather than a shape-specific one. A
completely empty tab (zero rows) against `liveness_sql`/get_schema()'s
sample query is also untested against a live example.
"""

import json
import logging
import os

import requests
from google.oauth2 import service_account
from google.auth.transport import requests as google_requests  # same alias auth.py uses

from .base import Backend, DB_CONNECT_TIMEOUT_SECONDS, cap_schema_text
from sheets_util import column_letter


# How long a single gviz HTTP GET may take to fetch/download its response,
# once the TCP+TLS handshake (bounded separately by DB_CONNECT_TIMEOUT_SECONDS,
# see below) has already completed. Kept as its own env-configurable knob,
# not folded into DB_CONNECT_TIMEOUT_SECONDS - base.py's own docstring is
# explicit that constant bounds *only* connection establishment, never query
# execution, and requests.get(timeout=<single float>) would bound the
# *entire* request (connect + full response download) if used alone. A
# generous default since a legitimately large public sheet can take a real
# few seconds for Google to serialize and for us to download.
SHEETS_READ_TIMEOUT_SECONDS = int(os.environ.get("SHEETS_READ_TIMEOUT_SECONDS", 30))

# How many rows get_schema() samples to infer/describe each column's type
# and show a few example values - bounded so introspecting an enormous
# sheet stays cheap and fast, same rationale as every other backend's
# schema-introspection caps (see backends/base.py).
SHEETS_SCHEMA_SAMPLE_ROWS = int(os.environ.get("SHEETS_SCHEMA_SAMPLE_ROWS", 50))

_GVIZ_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"

# Confirmed against a live private sheet + real service account - see the
# module docstring's "Verified against a live private sheet" paragraph for
# the full story: "spreadsheets.readonly" (the seemingly-correct, narrower
# scope) gets a bare HTTP 401 from gviz specifically, even though the real
# Sheets API v4 accepts it fine. "drive.readonly" is the least-privileged
# scope that actually works against gviz.
SHEETS_CREDENTIAL_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_logger = logging.getLogger(__name__)


def _ambient_credentials_json():
    """Returns the JSON text of the shared, app-wide service-account key
    named by SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE, or None if that env
    var isn't set, or its file can't be read, or its contents aren't valid
    JSON - logging a warning in the latter two cases so a misconfiguration
    is visible immediately rather than only discovered the first time
    someone hits a "should be private but isn't reachable" surprise.

    Deliberately NOT cached/read-once-at-import: there's no connection
    pooling anywhere in this app (every request calls backend.connect()
    fresh - see db.py's module docstring), so this already runs once per
    request regardless of caching, and a single local file read is not a
    meaningful cost next to that. Staying uncached also means the env var
    takes effect without restarting the process, and makes this trivially
    testable via monkeypatch rather than needing to reload the module."""
    path = os.environ.get("SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE")
    if not path:
        return None
    try:
        with open(path, "r") as f:
            text = f.read()
        json.loads(text)  # fail loud now rather than surface a confusing error later
        return text
    except Exception as e:
        _logger.warning(
            "SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE=%r is set but couldn't be read as "
            "a valid service-account key JSON file (%s) - every Sheets connection "
            "without its own credentials_json will stay public-sheet-only until "
            "this is fixed.",
            path, e,
        )
        return None


def _parse_gviz_response(text):
    """gviz's `tqx=out:json` response may come back as bare JSON or as a
    JSONP-style callback wrapper (`google.visualization.Query.setResponse(
    {...});`) - handled defensively for both shapes since this wasn't
    pinned down to a single one ahead of time. When wrapped, the first '('
    is unambiguously the call's opening paren (the callback name itself
    contains no parens) and the last ')' in the text is unambiguously its
    closing paren too: a cell value can legitimately contain a literal ')'
    inside a string (e.g. "Q3 (final)"), but nothing in a well-formed
    response can appear after the JSON body's own closing '}' except that
    wrapper's ')' and an optional trailing ';'/whitespace - so using
    index()/rindex() rather than a naive split is what keeps this correct
    even when cell data contains parens of its own."""
    text = text.strip()
    if text.startswith("{"):
        body = text
    else:
        start = text.index("(") + 1
        end = text.rindex(")")
        body = text[start:end]
    return json.loads(body)


class SheetsBackend(Backend):
    dialect_name = "Google Visualization API Query Language"

    # A truly empty tab against "select * limit 1" is untested live - see
    # module docstring - but this is the same one-row liveness probe every
    # other backend uses and is valid GViz syntax regardless.
    liveness_sql = "select * limit 1"

    def connect(self, descriptor):
        """Pure validation, no I/O for the credential-less (public sheet)
        path - there's no separate "dial" phase for an HTTP-based backend
        the way a DB-API driver has a real TCP connect/login step, so
        nothing meaningful to do here beyond checking the descriptor is
        well-formed. See identity_label() below for where the one real "is
        this reachable" network check lives for that path.

        When a credentials_json (service-account key) IS in play - either
        the descriptor's own, or the app-wide ambient one (see
        _ambient_credentials_json() above) - this deliberately DOES perform
        real I/O: it mints a bearer token via credentials.refresh(), making
        connect() the live "does this credential actually work" check for
        the credentialed path - mirroring every other credentialed dialect,
        where a bad password/key fails immediately at connect() time rather
        than surfacing only on the first query. This doesn't affect the
        fully credential-less path (no descriptor credential AND no ambient
        one configured) at all - see test_connect_performs_no_http_request."""
        descriptor = descriptor or {}
        spreadsheet_id = (descriptor.get("spreadsheet_id") or "").strip()
        tab_name = (descriptor.get("tab_name") or "").strip()
        if not spreadsheet_id:
            raise ValueError("Google Sheets connection requires a spreadsheet - none was provided.")
        if not tab_name:
            raise ValueError("Google Sheets connection requires a tab name - none was provided.")

        connection = {"spreadsheet_id": spreadsheet_id, "tab_name": tab_name}

        # Explicit per-connection credential always wins; the ambient,
        # app-wide one (if configured) is only ever a fallback - see the
        # module docstring's "ambient identity" paragraph.
        credentials_json = descriptor.get("credentials_json") or _ambient_credentials_json()
        if credentials_json:
            try:
                info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=SHEETS_CREDENTIAL_SCOPES,
                )
                credentials.refresh(google_requests.Request())
            except Exception as e:
                raise ValueError(f"Couldn't authenticate with the provided service-account key: {e}") from e
            connection["_bearer_token"] = credentials.token
            # Stashed for identity_label()'s display string below - connect()
            # is the only place that ever sees the parsed key itself.
            connection["_service_account_email"] = info.get("client_email")

        return connection

    def close(self, connection):
        # No live resource to release - connect() returns a plain dict.
        pass

    def cache_key(self, descriptor):
        descriptor = descriptor or {}
        spreadsheet_id = descriptor.get("spreadsheet_id") or "unknown"
        tab_name = descriptor.get("tab_name") or "unknown"
        return f"sheets:{spreadsheet_id}/{tab_name}"

    def identity_label(self, connection):
        """Unlike connect(), this DOES perform one minimal live fetch.
        /api/config's identity probe (config_routes.py) calls only
        connect() then identity_label() for the active custom connection -
        never execute() - so if both were I/O-free here, a broken/private/
        deleted spreadsheet would silently report "connected" in the config
        UI and only fail later, on the first real /api/translate or
        /api/execute call. Any failure here propagates exactly like every
        other dialect's connect()-failure already does (the caller wraps
        this in try/except and just logs + shows "Unknown")."""
        self._fetch(connection, "select A limit 1")
        if connection.get("_bearer_token"):
            identity = connection.get("_service_account_email") or "service account (private sheet)"
        else:
            identity = "anonymous (public access)"
        return connection["tab_name"], identity

    def _fetch(self, connection, query_text):
        """The one shared low-level GET both get_schema() and execute()
        use. `headers=1` is sent on every call here - not just the schema
        sample - so gviz's column-type inference can never disagree between
        a schema fetch and a real query (e.g. a numeric column read back as
        `string` because a query omitted headers=1 and gviz saw literal
        header text in row 1), which could otherwise silently break a
        numeric predicate the model wrote against the schema it was shown."""
        params = {
            "tq": query_text,
            "sheet": connection["tab_name"],
            "headers": "1",
            "tqx": "out:json",
        }
        url = _GVIZ_URL_TEMPLATE.format(spreadsheet_id=connection["spreadsheet_id"])
        # Named request_headers (not "headers") to avoid confusion with the
        # unrelated params["headers"] = "1" gviz option above (that one
        # tells gviz "row 1 is a header row"; this one is the actual HTTP
        # Authorization header for a credentialed connection).
        request_headers = (
            {"Authorization": f"Bearer {connection['_bearer_token']}"}
            if connection.get("_bearer_token") else None
        )
        try:
            resp = requests.get(
                url,
                params=params,
                headers=request_headers,
                # (connect_timeout, read_timeout) tuple, not a single float -
                # see SHEETS_READ_TIMEOUT_SECONDS's comment above for why.
                timeout=(DB_CONNECT_TIMEOUT_SECONDS, SHEETS_READ_TIMEOUT_SECONDS),
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Couldn't reach this spreadsheet: {e}") from e

        sharing_hint = (
            "make sure the service account has been given Viewer access on "
            "this spreadsheet's Share dialog (a scope that's too narrow for "
            "this endpoint can also produce this - see SHEETS_CREDENTIAL_SCOPES)"
            if connection.get("_bearer_token")
            else "make sure it's shared as \"Anyone with the link can view\""
        )
        if resp.status_code == 404:
            raise ValueError(f"Spreadsheet not found - check the URL and {sharing_hint}.")
        if resp.status_code != 200:
            # Covers the not-independently-verified private/403 case
            # defensively - exact shape unconfirmed, but this message is
            # generically correct regardless of which 4xx/5xx it turns out
            # to be. A 401/403 on a credentialed connection is genuinely
            # ambiguous between "not shared with this identity" and "scope
            # too narrow" - the hint above calls out both possibilities
            # rather than guessing.
            raise RuntimeError(f"Couldn't reach this spreadsheet (HTTP {resp.status_code}) - {sharing_hint}.")

        payload = _parse_gviz_response(resp.text)
        if payload.get("status") == "error":
            errors = payload.get("errors") or [{}]
            detail = errors[0].get("detailed_message") or errors[0].get("message") or "invalid query"
            raise ValueError(f"Invalid Google Sheets query: {detail}")
        return payload["table"]

    def get_schema(self, connection):
        try:
            table = self._fetch(connection, f"select * limit {SHEETS_SCHEMA_SAMPLE_ROWS}")
        except Exception:
            return None
        cols = table.get("cols") or []
        if not cols:
            return None
        rows = table.get("rows") or []

        lines = [
            f"Tab: {connection['tab_name']} (query this as the implicit data source - "
            "this dialect's query language has no FROM clause)"
        ]
        for i, col in enumerate(cols):
            letter = column_letter(i + 1)
            label = col.get("label") or f"Column {letter}"
            col_type = col.get("type") or "string"
            samples = []
            for row in rows[:3]:
                cells = row.get("c") or []
                if i < len(cells) and cells[i] is not None:
                    cell = cells[i]
                    value = cell.get("f") if cell.get("f") is not None else cell.get("v")
                    if value is not None:
                        samples.append(str(value))
            sample_text = f" (e.g. {', '.join(samples)})" if samples else ""
            lines.append(f"  {letter}: \"{label}\" ({col_type}){sample_text}")
        lines.append(
            "IMPORTANT: reference columns ONLY by the letter shown above (A, B, "
            "C, ...) - never by their header/label text - this query language "
            "addresses columns positionally, by spreadsheet column letter, not "
            "by name."
        )
        return cap_schema_text("\n".join(lines))

    def execute(self, connection, sql_text):
        # Deliberately NOT sqlparse.split() - this grammar has no
        # multi-statement concept at all (no semicolon-separated batch
        # syntax), and sqlparse isn't built to tokenize it correctly since
        # it isn't real SQL. The whole input is always exactly one query;
        # only one optional trailing semicolon is stripped.
        query_text = (sql_text or "").strip()
        if query_text.endswith(";"):
            query_text = query_text[:-1].strip()
        if not query_text:
            return [{"statement": "", "columns": None, "rows": None, "rowCount": 0}]

        table = self._fetch(connection, query_text)
        cols = table.get("cols") or []
        columns = [c.get("label") or column_letter(i + 1) for i, c in enumerate(cols)]
        col_types = [c.get("type") for c in cols]

        rows = []
        for row in table.get("rows") or []:
            cells = row.get("c") or []
            row_dict = {}
            for i, col_name in enumerate(columns):
                cell = cells[i] if i < len(cells) else None
                if cell is None:
                    row_dict[col_name] = None
                    continue
                if col_types[i] in ("date", "datetime", "timeofday"):
                    row_dict[col_name] = cell.get("f") if cell.get("f") is not None else cell.get("v")
                else:
                    row_dict[col_name] = cell.get("v")
            rows.append(row_dict)

        return [{
            "statement": query_text,
            "columns": columns if columns else None,
            "rows": rows,
            "rowCount": len(rows),
        }]

