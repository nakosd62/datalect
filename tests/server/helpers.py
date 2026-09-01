"""
tests/server/helpers.py

Shared test infrastructure for the backend suite. Not a test file itself
(no test_ prefix - pytest won't collect it), just the machinery every
test_*.py file in this directory imports.

Why this exists: app_config.py has real import-time side effects (builds
the Flask app, parses DATABASE_PRESETS_FILE, tries to connect to Firestore if
GCP_PROJECT_ID is set, picks SqliteStateStore vs FirestoreStateStore) and
every other server module imports shared singletons from it. Different
tests need different environments (auth on/off, different presets,
Cloud Run vs local, ...), so each test that cares gets a *fresh* import of
these modules under its own controlled environment rather than reusing
whatever the first test happened to configure - Python only imports a
module once per process, so without this, test order would silently
determine behavior.
"""

import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from google.cloud import firestore

# server/ is not a package (no __init__.py) - it's run as the entrypoint's
# own directory (`python server/server.py`), so its modules import each
# other with bare names ("from app_config import app", not
# "from server.app_config import app"). Tests need the same sys.path setup
# to import them the same way the real app does.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", ".."))
SERVER_DIR = os.path.join(REPO_ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

# Every module (by name) that needs to be dropped from sys.modules before a
# fresh_import() so the next import re-executes app_config.py's top-level
# side effects (and everything downstream of it) under the new environment,
# rather than returning the previous test's cached module object.
_APP_MODULE_NAMES = [
    "app_config", "auth", "config_routes", "execute_routes",
    "translate_routes", "history_routes", "db", "schema_cache", "state_store",
    "connection_router", "cancel_registry",
]

# Every env var any of the above modules reads at import or request time.
# fresh_import() clears all of these before applying a test's own overrides,
# so a variable set in the developer's real shell/.env (or left over from
# dotenv loading the repo's real .env) never leaks into a test.
_ENV_VARS_TO_CLEAR = [
    "GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "K_SERVICE",
    "GOOGLE_CLIENT_ID", "DATABASE_PRESETS_FILE", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_PRESET_KEYS", "GOOGLE_MODELS", "SCHEMA_CACHE_TTL_SECONDS",
    "SCHEMA_MAX_TABLES", "SCHEMA_MAX_TABLE_NAMES_SCANNED",
    "SCHEMA_MAX_SCHEMA_CHARS", "SCHEMA_SHARD_MIN_GROUP_SIZE", "LOG_LEVEL",
    "CRBOT_HOSTNAME", "CRBOT_PORT", "MAX_TRANSLATION_ATTEMPTS", "TRANSLATION_RETRY_DELAY_SECONDS",
    "TRANSLATION_TIMEOUT_SECONDS", "SQL_EXECUTE_TIMEOUT_SECONDS",
    "HISTORY_RESULT_MAX_ROWS", "HISTORY_MAX_TURNS",
    # Anthropic (Claude) provider path (translate_routes.py's
    # AnthropicProvider - note there's no LLM_PROVIDER env var anymore to
    # select it; see helpers.select_llm_provider()) - cleared for the same
    # reason as the Google vars above: a developer's real shell (or CI)
    # plausibly has ANTHROPIC_API_KEY set (e.g. for using Claude Code
    # itself), which would otherwise leak into any test that doesn't
    # explicitly pass its own `env`.
    "CLAUDE_PRESET_KEYS", "ANTHROPIC_API_KEY", "ANTHROPIC_MODELS",
    # OpenAI provider path (translate_routes.py's OpenAiProvider) - cleared
    # for the same reason as the Google/Anthropic vars above.
    "OPENAI_API_KEY", "OPENAI_PRESET_KEYS", "OPENAI_MODELS",
    # state_store.py's database_config encryption-at-rest key (see its
    # module docstring section) - cleared for the same reason as every
    # other secret-shaped var above: a developer's real shell/.env
    # plausibly has this set for their own local Cloud Run testing.
    "DB_CONFIG_ENCRYPTION_KEY",
]

# A syntactically valid (but obviously throwaway, fixed/shared) Fernet
# key - for tests that need SOME valid DB_CONFIG_ENCRYPTION_KEY to satisfy
# app_config.py's Cloud Run startup guard (see its "Startup / Module Scope
# Guard" section) but aren't testing the encryption mechanism itself, so
# a fresh key per call would just be unnecessary noise. Tests that DO care
# about the encryption mechanism (e.g. "data encrypted under key A can't
# be read back under key B") should call make_fernet_key() below instead
# to get a fresh, distinct key.
FAKE_DB_CONFIG_ENCRYPTION_KEY = Fernet.generate_key().decode()


def make_fernet_key():
    """Returns a fresh, valid Fernet key string - see
    FAKE_DB_CONFIG_ENCRYPTION_KEY's docstring above for when to use that
    fixed constant instead of this."""
    return Fernet.generate_key().decode()


def fresh_import(monkeypatch, tmp_path, env=None, register_blueprints=True, mock_firestore=False):
    """The one entry point every test file uses to get a clean, isolated
    instance of the app under a specific environment.

    - Chdir's into `tmp_path` first: state_store.py's local SQLite path
      ("state/ydyl_state.db") is a hardcoded *relative* path, resolved
      against whatever the process's cwd happens to be - not configurable
      via an env var. Without this, every test would share (and race on)
      the real repo's state/ydyl_state.db. tmp_path is a fresh, empty
      directory pytest gives each test, so each test gets its own SQLite
      file and no cleanup is needed afterwards.
    - Clears every env var the app reads, then applies `env`, so tests are
      hermetic regardless of what's in the developer's actual shell/.env.
    - Neutralizes app_config.py's `load_dotenv(override=True)` call - it
      would otherwise search upward from cwd for a real .env file and
      stomp the env vars just set above (unlikely to find one under a
      pytest tmp dir, but this makes it deterministic rather than
      "unlikely").
    - Drops every app module from sys.modules so the next `import
      app_config` (and everything it/the blueprints pull in) re-executes
      from scratch under the new environment.
    - Optionally wires up the auth guard + all blueprints exactly as
      server.py does, and returns a ready-to-use Flask test client.

    `mock_firestore=True` patches google.cloud.firestore.Client (BEFORE
    importing app_config) with a constructor that returns a fresh
    FakeFirestoreClient - use this whenever a test needs
    IS_CLOUD_RUN/GCP_PROJECT_ID to actually select FirestoreStateStore
    without erroring: this sandbox has no real GCP credentials, so the
    real firestore.Client(...) call always fails (caught internally,
    logged, and silently left as SqliteStateStore) - and if K_SERVICE is
    set at the same time, app_config.py's own startup guard turns that
    same failure into a hard RuntimeError ("Halting startup to prevent
    ephemeral SQLite fallback"). The returned SimpleNamespace's
    `.firestore_client` is the FakeFirestoreClient instance actually wired
    up as `app_config.state_store.client`, when applicable.

    Returns a SimpleNamespace with at least `.app_config`; when
    register_blueprints=True (the default) also `.auth`, `.config_routes`,
    `.execute_routes`, `.translate_routes`, `.history_routes`,
    `.cancel_registry`, and `.client` (a Flask test client with
    state_store.init() already called).
    """
    os.makedirs(tmp_path, exist_ok=True)
    monkeypatch.chdir(tmp_path)

    for var in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)

    for mod_name in list(sys.modules):
        if mod_name in _APP_MODULE_NAMES or mod_name.startswith("backends"):
            del sys.modules[mod_name]

    fake_firestore_holder = {}
    if mock_firestore:
        from google.cloud import firestore as firestore_module

        def _make_fake_firestore_client(*args, **kwargs):
            client = FakeFirestoreClient()
            fake_firestore_holder["client"] = client
            return client

        monkeypatch.setattr(firestore_module, "Client", _make_fake_firestore_client)

    import app_config
    ns = types.SimpleNamespace(app_config=app_config)
    ns.firestore_client = fake_firestore_holder.get("client")

    if register_blueprints:
        import auth
        import config_routes
        import execute_routes
        import translate_routes
        import history_routes
        import cancel_registry

        app_config.app.before_request(auth.enforce_authentication)
        for bp in (
            auth.auth_bp, config_routes.config_bp, execute_routes.execute_bp,
            translate_routes.translate_bp, history_routes.history_bp,
        ):
            app_config.app.register_blueprint(bp)

        ns.auth = auth
        ns.config_routes = config_routes
        ns.execute_routes = execute_routes
        ns.translate_routes = translate_routes
        ns.history_routes = history_routes
        ns.cancel_registry = cancel_registry

        # Mirrors server.py's own '/' route registration (serves the SPA
        # shell) - not a blueprint, so it's not picked up above, but it's
        # part of "the real app" and EXEMPT_ENDPOINTS/enforce_authentication
        # both special-case endpoint name 'index', so tests that touch auth
        # gating need this route to actually exist.
        if "index" not in app_config.app.view_functions:
            from flask import send_from_directory

            @app_config.app.route('/')
            def index():
                return send_from_directory(app_config.app.static_folder, 'index.html')

    app_config.state_store.init()
    app_config.app.config["TESTING"] = True
    ns.client = app_config.app.test_client()
    return ns


def login_as(test_client, email):
    """Sets the auth cookie get_current_user_identity() checks (auth.py's
    step 3), giving a stable non-anonymous, non-"global" identity for a
    Flask test client without mocking Google ID-token verification.

    NOTE: Flask/Werkzeug's test client's own cookie jar takes precedence
    over a manually-supplied `headers={"Cookie": ...}` dict on an
    individual request (it gets silently dropped) - this is the one
    reliable way to set a cookie for `client.get()`/`client.post()` calls."""
    test_client.set_cookie("crbot_user_id", email)


def select_llm_provider(env, provider_name):
    """Pre-seeds the local-dev "global" identity's saved llm_provider
    choice, so a test can exercise a specific LLM provider's /api/translate
    code path without a per-fleet LLM_PROVIDER env var - there isn't one
    anymore (see translate_routes.py's module docstring): a fresh session
    with nothing saved now falls back to the one hardcoded default,
    Google/gemini-3.6-flash, rather than an env-configurable provider.

    Call this AFTER app_factory() (it needs `env.app_config.state_store`,
    initialized by fresh_import()) and BEFORE the first /api/translate
    request that should see this provider. Every test that uses this helper
    runs in the default local-dev environment (no GOOGLE_CLIENT_ID/
    IS_CLOUD_RUN), where get_current_user_identity() resolves to the
    constant "global" for every request regardless of cookies - so unlike
    login_as() above, there's no cookie/session-id plumbing needed here."""
    env.app_config.state_store.set_session("global", llm_provider=provider_name)


def parse_translate_stream(resp):
    """/api/translate streams newline-delimited JSON (NDJSON) rather than a
    single JSON body - see translate_routes.py's module docstring: zero or
    more {"status": "retrying", ...} progress lines, then exactly one
    terminal {"status": "done", success, sql/error, ...} line carrying the
    same fields the route used to return as its whole body before
    streaming existed. resp.get_json() can't be used here - it calls
    json.loads() on the whole response body, which raises on anything but
    a single JSON value, and a body with even one retry line already has
    two.

    Returns (retry_events, final_data): `retry_events` is the list of
    parsed "retrying" lines in order (empty if no retry occurred),
    `final_data` is the parsed terminal line (or {} if the body was
    somehow empty). A request that fails validation before streaming
    starts (missing prompt/API key) returns a single plain JSON object,
    not NDJSON - that still parses correctly here as a one-line body whose
    single line becomes `final_data`, with an empty `retry_events` list."""
    lines = [ln for ln in resp.get_data(as_text=True).splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]
    retry_events = [p for p in parsed if p.get("status") == "retrying"]
    final_data = next((p for p in reversed(parsed) if p.get("status") != "retrying"), parsed[-1] if parsed else {})
    return retry_events, final_data


def parse_translate_stream_events(resp):
    """Like parse_translate_stream above, but returns EVERY parsed NDJSON
    line in arrival order, unfiltered - needed by tests asserting on the
    "all databases" mode streaming events (`phase_a_route`/
    `phase_b_connection_done` - see translate_routes.py's
    stream_translation() docstring) that parse_translate_stream's
    retry_events/final_data split doesn't surface at all (it only ever
    looks at "retrying" lines and the true last line). The terminal
    {"status": "done", ...} line is always parsed[-1] here, same
    guarantee parse_translate_stream relies on."""
    lines = [ln for ln in resp.get_data(as_text=True).splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def write_database_presets_file(tmp_path, presets, filename="database_presets.json"):
    """Writes `presets` (a list of preset dicts) as JSON to a file under
    `tmp_path` and returns its absolute path string, ready to hand to
    app_factory as env={"DATABASE_PRESETS_FILE": ...}. Replaces the old
    database_presets_env() helper from when presets were a single inline
    DATABASE_PRESETS env var - now app_config.py reads the JSON from a file
    instead (see its comment above DATABASE_PRESETS_FILE).

    Pass the SAME tmp_path fixture instance a test's app_factory call will
    use (pytest caches fixtures per test, so a test function that takes
    both `app_factory` and `tmp_path` as parameters gets the one shared
    instance) - fresh_import() chdir's into tmp_path before importing
    app_config, so a relative filename would resolve too, but returning an
    absolute path here removes any doubt about that ordering."""
    path = tmp_path / filename
    path.write_text(json.dumps(presets), encoding="utf-8")
    return str(path)


def write_database_presets_file_raw(tmp_path, raw_text, filename="database_presets.json"):
    """Like write_database_presets_file() but writes `raw_text` verbatim
    instead of JSON-encoding a Python object - for tests that need to feed
    app_config.py deliberately malformed/non-array file contents (e.g.
    "{not valid json" or a JSON object instead of an array)."""
    path = tmp_path / filename
    path.write_text(raw_text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Fake service-account key (BigQuery custom-connection credentials_json)
# ---------------------------------------------------------------------------

def make_service_account_key(project_id="fake-project", client_email=None):
    """Returns a syntactically-real (but obviously not a real credential)
    service-account key dict, suitable for json.dumps()-ing into a
    credentials_json string and handed to
    google.oauth2.service_account.Credentials.from_service_account_info(),
    which validates the PEM structure - a hand-typed fake private_key
    string won't parse. Generates a fresh throwaway RSA keypair per call
    (cheap enough for tests; never reused, never a real credential)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    email = client_email or f"svc@{project_id}.iam.gserviceaccount.com"
    return {
        "type": "service_account",
        "project_id": project_id,
        "private_key_id": "fake-key-id",
        "private_key": private_pem,
        "client_email": email,
        "client_id": "111111111111111111111",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{email}",
    }


def make_service_account_key_json(project_id="fake-project", client_email=None):
    return json.dumps(make_service_account_key(project_id, client_email))


# ---------------------------------------------------------------------------
# Fake CA certificate (Postgres/MySQL ca_cert_pem)
# ---------------------------------------------------------------------------

def make_self_signed_ca_cert_pem(common_name="ydyl-test-ca"):
    """Returns a syntactically-real (but obviously throwaway) self-signed
    CA certificate as a PEM string - unlike Postgres's connect()-level
    tests (backends.postgres's psycopg2.connect() is faked, so it never
    actually parses ca_cert_pem's content at all), backends.mysql's
    connect() constructs a real ssl.SSLContext via
    ssl.create_default_context(cafile=...) itself, BEFORE ever reaching
    the (faked) pymysql.connect() call - Python's real ssl module parses
    and validates that file's PEM content immediately, eagerly, at
    SSLContext-construction time, so a hand-typed placeholder string like
    "-----BEGIN CERTIFICATE-----\\nFAKE\\n-----END CERTIFICATE-----" fails
    with `ssl.SSLError: [X509] PEM lib` well before pymysql.connect() is
    ever reached. This generates a fresh throwaway keypair + self-signed
    cert per call (cheap enough for tests; never reused, never a real CA)
    so that code path has something it can actually load."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime(2024, 1, 1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


# ---------------------------------------------------------------------------
# Fake BigQuery client harness
# ---------------------------------------------------------------------------
# Mirrors the shape of google-cloud-bigquery's own objects just enough for
# backends/bigquery.py to work against: bigquery.Client(project=,
# credentials=), .query(sql, job_config=).result() -> iterable of rows
# supporting both attribute access (row.table_name, as get_schema() uses)
# and item access (row[col], as execute() uses), plus .schema (a list of
# objects with a .name attribute).

class FakeBQField:
    def __init__(self, name):
        self.name = name


class FakeBQRow:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)


class FakeBQRowIterator:
    def __init__(self, rows, schema):
        self._rows = rows
        self.schema = schema

    def __iter__(self):
        return iter(self._rows)


class FakeBQQueryJob:
    def __init__(self, rows=None, columns=None, num_dml_affected_rows=None):
        """`rows`: list of plain dicts. `columns`: explicit column-name
        order (defaults to the keys of the first row, or [] if no rows -
        matching how a real empty result set still has a schema when it's
        an information_schema query with a WHERE that matched nothing;
        pass columns explicitly for that case)."""
        row_dicts = rows or []
        if columns is None:
            columns = list(row_dicts[0].keys()) if row_dicts else []
        self._schema = [FakeBQField(c) for c in columns] if columns else []
        self._rows = [FakeBQRow(r) for r in row_dicts]
        self.num_dml_affected_rows = num_dml_affected_rows

    def result(self):
        return FakeBQRowIterator(self._rows, self._schema)


class FakeBQDatasetReference:
    def __init__(self, project, dataset):
        self.project = project
        self.dataset = dataset


class FakeBQQueryJobConfig:
    def __init__(self, default_dataset=None, query_parameters=None):
        self.default_dataset = default_dataset
        self.query_parameters = query_parameters or []


class FakeBQArrayQueryParameter:
    def __init__(self, name, type_, values):
        self.name = name
        self.type_ = type_
        self.values = values


class FakeBigQueryHarness:
    """Installed via install_fake_bigquery(monkeypatch) below. `handler` is
    a callable (sql_text, job_config) -> FakeBQQueryJob that each test
    supplies to decide what a given query should return - matching on
    substrings in `sql_text` (e.g. "INFORMATION_SCHEMA.COLUMNS") is the
    simplest approach and is robust to backends/bigquery.py's actual call
    order/try-except structure changing over time.
    """

    def __init__(self):
        self.handler = None
        self.client_calls = []  # list of {"project", "credentials"} dicts
        self.query_calls = []  # list of (sql_text, job_config)

    def set_handler(self, handler):
        self.handler = handler

    def make_client_class(harness):
        class _FakeClient:
            def __init__(self, project=None, credentials=None):
                self.project = project
                self.credentials = credentials
                harness.client_calls.append({"project": project, "credentials": credentials})

            def query(self, sql_text, job_config=None):
                harness.query_calls.append((sql_text, job_config))
                if harness.handler is None:
                    return FakeBQQueryJob(rows=[])
                return harness.handler(sql_text, job_config)

            def close(self):
                pass

        return _FakeClient


def install_fake_bigquery(monkeypatch):
    """Patches backends.bigquery's bigquery.{Client,DatasetReference,
    QueryJobConfig,ArrayQueryParameter} with fakes and returns the
    FakeBigQueryHarness controlling them. Must be called *after* the
    module has been imported (i.e. after fresh_import(), or after a bare
    `import backends.bigquery`) since it patches the already-imported
    module object's `bigquery` attribute in place."""
    import backends.bigquery as bqmod

    harness = FakeBigQueryHarness()
    monkeypatch.setattr(bqmod.bigquery, "Client", harness.make_client_class())
    monkeypatch.setattr(bqmod.bigquery, "DatasetReference", FakeBQDatasetReference)
    monkeypatch.setattr(bqmod.bigquery, "QueryJobConfig", FakeBQQueryJobConfig)
    monkeypatch.setattr(bqmod.bigquery, "ArrayQueryParameter", FakeBQArrayQueryParameter)
    return harness


def schema_query_handler(tables=(), columns=(), views=(), constraints=()):
    """Builds a handler for install_fake_bigquery()'s harness.set_handler()
    that answers backends/bigquery.py's get_schema() query sequence based
    on matching a marker substring in the SQL text - good enough for schema
    tests without needing to hardcode call order.

    - tables: list of table-name strings (INFORMATION_SCHEMA.TABLES)
    - columns: list of (table_name, column_name, data_type, is_nullable)
    - views: list of (table_name, view_definition)
    - constraints: list of (table_name, constraint_name, constraint_type, column_name)
    """
    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA.TABLES" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": t} for t in tables], columns=["table_name"]
            )
        if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
            return FakeBQQueryJob(
                rows=[
                    {"table_name": t, "column_name": c, "data_type": d, "is_nullable": n}
                    for (t, c, d, n) in columns
                ],
                columns=["table_name", "column_name", "data_type", "is_nullable"],
            )
        if "INFORMATION_SCHEMA.VIEWS" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": t, "view_definition": d} for (t, d) in views],
                columns=["table_name", "view_definition"],
            )
        if "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in sql_text:
            return FakeBQQueryJob(
                rows=[
                    {"table_name": t, "constraint_name": n, "constraint_type": ty, "column_name": c}
                    for (t, n, ty, c) in constraints
                ],
                columns=["table_name", "constraint_name", "constraint_type", "column_name"],
            )
        return FakeBQQueryJob(rows=[])
    return handler


# ---------------------------------------------------------------------------
# Fake Postgres (psycopg2-shaped) connection/cursor
# ---------------------------------------------------------------------------
# backends/postgres.py issues a fixed sequence of `with connection.cursor()
# as cursor: cursor.execute(sql, params); cursor.fetchall()` calls. This
# fake answers each execute() call from an ordered queue of canned
# responses supplied by the test, in the exact order PostgresBackend issues
# them - simpler than content-sniffing since Postgres's query sequence
# (unlike BigQuery's, which has try/except-guarded optional sections) is
# unconditional.

class FakePgCursor:
    def __init__(self, responses):
        """`responses`: list of (rows, description, rowcount) tuples,
        consumed one per execute() call. `description`: None for
        get_schema()'s queries (never read), or a list of 1-tuples/objects
        whose [0] is the column name for execute()'s DML/SELECT queries
        (mirrors psycopg2's cursor.description shape - execute_routes.py's
        row-shaping only ever reads desc[0]).

        An entry may also just be a bare Exception instance instead of a
        3-tuple - execute() raises it directly rather than treating it as
        a response, simulating a statement partway through a multi-
        statement script failing (e.g. a syntax error on statement 2 of
        3). This is what backends/base.py's SqlExecutionError wraps - see
        each backend test file's test_execute_*_statement_failure test."""
        self._responses = list(responses)
        self.calls = []
        self.description = None
        self.rowcount = -1
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self._responses:
            item = self._responses.pop(0)
        else:
            item = ([], None, -1)
        if isinstance(item, Exception):
            raise item
        rows, description, rowcount = item
        self._rows = rows
        self.description = description
        self.rowcount = rowcount if rowcount is not None else -1

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakePgConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def make_fake_pg_connection(responses):
    cursor = FakePgCursor(responses)
    return FakePgConnection(cursor), cursor


# ---------------------------------------------------------------------------
# Fake Postgres connect() (backends/postgres.py's own connect(), as opposed
# to the make_fake_pg_connection() fake above, which drives get_schema()/
# execute()/identity_label() directly and bypasses connect() entirely)
# ---------------------------------------------------------------------------
# Unlike every structured-descriptor dialect's connect() (Redshift/Oracle/
# Snowflake/Databricks/MySQL, all called as pure **kwargs), psycopg2.connect()
# for a plain Postgres URL is called as connect(dsn, **kwargs) - one
# positional DSN string plus keyword args (see backends/postgres.py's
# connect()) - so this fake records both instead of just a kwargs dict.

class FakePostgresConnectHarness:
    def __init__(self):
        self.calls = []  # list of (dsn, kwargs) tuples, one per connect() call
        # Snapshotted here, not read by the test afterward, because
        # backends.postgres.connect()'s ca_cert_pem support deletes the
        # sslrootcert tempfile in a `finally` immediately after this fake
        # returns - by the time a test gets control back, the path in
        # kwargs["sslrootcert"] no longer exists on disk. One entry per
        # connect() call, in the same order as self.calls; None when that
        # call's kwargs had no "sslrootcert" at all.
        self.sslrootcert_contents = []

    def connect(self, dsn, **kwargs):
        self.calls.append((dsn, kwargs))
        sslrootcert_path = kwargs.get("sslrootcert")
        if sslrootcert_path:
            with open(sslrootcert_path, "r") as f:
                self.sslrootcert_contents.append(f.read())
        else:
            self.sslrootcert_contents.append(None)
        return object()  # backend.connect() just returns this straight through


def install_fake_postgres_connect(monkeypatch):
    """Patches backends.postgres's psycopg2.connect with a fake that
    records the DSN and kwargs it was called with instead of opening a real
    connection, and returns the FakePostgresConnectHarness controlling it.
    Must be called *after* the module has been imported, same caveat as
    install_fake_oracle_connect above. A separate fake from
    backends.redshift's own `import psycopg2` - patching backends.postgres's
    module-level reference has no effect on backends.redshift's, even
    though both ultimately name the same third-party package, since each
    module holds its own name binding from its own `import psycopg2`
    statement (see install_fake_redshift_connect's own docstring)."""
    import backends.postgres as pgmod

    harness = FakePostgresConnectHarness()
    monkeypatch.setattr(pgmod.psycopg2, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake MySQL connection (get_schema()/execute()/identity_label())
# ---------------------------------------------------------------------------
# PyMySQL implements the same PEP 249 DB-API cursor shape (execute/
# description/fetchall/rowcount, cursor as a context manager) psycopg2
# does, so FakePgCursor above is reused directly here - same approach
# already used for Snowflake (see the comment above
# FakeSnowflakeConnectHarness). The one real difference: PyMySQL's
# Connection.autocommit is a *method* (`connection.autocommit(True)`),
# not a settable attribute the way psycopg2's/FakePgConnection's is - so
# this needs its own small connection fake rather than reusing
# FakePgConnection verbatim, or backends/mysql.py's execute() calling
# connection.autocommit(True) would raise "'bool' object is not callable"
# against the Postgres fake.

class FakeMySQLConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit_calls = []
        self.closed = False

    def cursor(self):
        return self._cursor

    def autocommit(self, value):
        self.autocommit_calls.append(value)

    def close(self):
        self.closed = True


def make_fake_mysql_connection(responses):
    cursor = FakePgCursor(responses)
    return FakeMySQLConnection(cursor), cursor


class FakePyMySQLConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return object()  # backend.connect() just returns this straight through


def install_fake_pymysql_connect(monkeypatch):
    """Patches backends.mysql's pymysql.connect with a fake that records
    its kwargs instead of opening a real connection, and returns the
    FakePyMySQLConnectHarness controlling it. Must be called *after* the
    module has been imported (same caveat as install_fake_snowflake_connect
    above)."""
    import backends.mysql as mysqlmod

    harness = FakePyMySQLConnectHarness()
    monkeypatch.setattr(mysqlmod.pymysql, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake pyodbc connect() (backends/mongodb_sql.py)
# ---------------------------------------------------------------------------
# Unlike every fake above, pyodbc.connect()'s first arg is positional (the
# connection string), not all-kwargs like pymysql's - recorded separately as
# "url" rather than folded into the kwargs dict, so a test can assert on it
# by name the same way it would any other captured field.

class FakePyodbcConnectHarness:
    def __init__(self):
        self.calls = []  # list of {"url": ..., **kwargs} dicts, one per connect() call

    def connect(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return object()  # backend.connect() just returns this straight through


def install_fake_pyodbc_connect(monkeypatch):
    """Patches backends.mongodb_sql's pyodbc.connect with a fake that
    records the connection string + kwargs instead of opening a real ODBC
    connection, and returns the FakePyodbcConnectHarness controlling it.
    Must be called *after* the module has been imported (same caveat as
    install_fake_snowflake_connect above)."""
    import backends.mongodb_sql as mongo_sql_mod

    harness = FakePyodbcConnectHarness()
    monkeypatch.setattr(mongo_sql_mod.pyodbc, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake Snowflake connect()
# ---------------------------------------------------------------------------
# backends/snowflake.py's connect() is the one piece of Snowflake-specific
# logic worth testing directly - deciding password vs key-pair auth and
# building the connector kwargs accordingly. get_schema()/execute()/
# identity_label() don't need a separate fake at all: snowflake-connector-
# python implements the same PEP 249 DB-API cursor shape
# (execute/description/fetchall/rowcount) psycopg2 does, so those are
# tested against the very same FakePgCursor/FakePgConnection/
# make_fake_pg_connection above, driven directly (bypassing connect()
# entirely) - same approach test_postgres_backend.py already uses.

class FakeSnowflakeConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return object()  # backend.connect() just returns this straight through


def install_fake_snowflake_connect(monkeypatch):
    """Patches backends.snowflake's snowflake.connector.connect with a fake
    that records its kwargs instead of opening a real connection, and
    returns the FakeSnowflakeConnectHarness controlling it. Must be called
    *after* the module has been imported (i.e. after fresh_import(), or
    after a bare `import backends.snowflake`), same caveat as
    install_fake_bigquery above."""
    import backends.snowflake as sfmod

    harness = FakeSnowflakeConnectHarness()
    monkeypatch.setattr(sfmod.snowflake.connector, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake Databricks connect()
# ---------------------------------------------------------------------------
# backends/databricks.py's connect() is the one piece of Databricks-specific
# logic worth testing directly - building the connector kwargs (and raising
# when no access_token is given). get_schema()/execute()/identity_label()
# don't need a separate fake either: databricks-sql-connector implements the
# same PEP 249 DB-API cursor shape (execute/description/fetchall/rowcount,
# cursor as a context manager) psycopg2 does, so those are tested against
# the very same FakePgCursor/FakePgConnection/make_fake_pg_connection above,
# driven directly (bypassing connect() entirely) - same approach used for
# Snowflake/MySQL. The one real wrinkle: the connector's declared paramstyle
# is "named" (:name, not %s), so get_schema()'s IN (...) queries pass a
# *dict* of params rather than a list/tuple - FakePgCursor.execute() doesn't
# care about params' type (it just records whatever it's given), so no
# separate fake is needed for that either.

class FakeDatabricksConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        return object()  # backend.connect() just returns this straight through


def install_fake_databricks_connect(monkeypatch):
    """Patches backends.databricks's databricks.sql.connect with a fake that
    records its kwargs instead of opening a real connection, and returns the
    FakeDatabricksConnectHarness controlling it. Must be called *after* the
    module has been imported, same caveat as install_fake_snowflake_connect
    above."""
    import backends.databricks as dbxmod

    harness = FakeDatabricksConnectHarness()
    monkeypatch.setattr(dbxmod.databricks_sql, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake Oracle connect()
# ---------------------------------------------------------------------------
# backends/oracle.py's connect() is the one piece of Oracle-specific logic
# worth testing directly - building the connector kwargs (service_name vs
# sid dispatch, and raising on missing host/user/password/service-or-sid).
# get_schema()/execute()/identity_label() don't need a separate fake
# either: python-oracledb implements the same PEP 249 DB-API cursor shape
# (execute/description/fetchall/rowcount, cursor as a context manager)
# psycopg2 does, so those are tested against the very same FakePgCursor/
# FakePgConnection/make_fake_pg_connection above, driven directly
# (bypassing connect() entirely) - same approach used for Snowflake/MySQL/
# Databricks. Unlike those, connect() itself can issue a SQL statement
# (ALTER SESSION SET CURRENT_SCHEMA, when a "schema" descriptor field is
# given - see _set_current_schema) against the connection it just opened,
# so this fake's connect() returns a lightweight connection stand-in with
# its own recording cursor (FakeOracleConnection), not a bare `object()`
# the way FakeSnowflakeConnectHarness/FakeDatabricksConnectHarness do -
# tests that care what _set_current_schema actually executed can inspect
# harness.connections[-1].cursor_calls.

class _FakeOracleCursor:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))


class FakeOracleConnection:
    def __init__(self):
        self.cursor_calls = []

    def cursor(self):
        return _FakeOracleCursor(self.cursor_calls)


class FakeOracleConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call
        self.connections = []  # the FakeOracleConnection returned by each call

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        connection = FakeOracleConnection()
        self.connections.append(connection)
        return connection


def install_fake_oracle_connect(monkeypatch):
    """Patches backends.oracle's oracledb.connect with a fake that records
    its kwargs instead of opening a real connection, and returns the
    FakeOracleConnectHarness controlling it. Must be called *after* the
    module has been imported, same caveat as install_fake_snowflake_connect
    above."""
    import backends.oracle as oramod

    harness = FakeOracleConnectHarness()
    monkeypatch.setattr(oramod.oracledb, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake Redshift connect() (config_routes.py's connect()/identity_label()/
# close() probe, at POST/GET /api/config - not backends/redshift.py's own
# get_schema()/execute() unit tests, which reuse FakePgConnection/
# FakePgCursor above directly instead, since RedshiftBackend talks the same
# psycopg2 DB-API shape backends/postgres.py does)
# ---------------------------------------------------------------------------
# Deliberately a real lightweight class, not a bare object() the way
# FakePyMySQLConnectHarness's connect() returns - RedshiftBackend.connect()
# sets `connection.autocommit = True` directly (an attribute assignment,
# not a method call the way PyMySQL's Connection.autocommit is), which a
# bare object() instance can't support (no __dict__), so this needs its own
# small class the same way FakeOracleConnection does. No fetchone() on the
# cursor - same minimal-fake precedent as _FakeOracleCursor: identity_label()
# calling it against this fake raises, which config_routes.py's GET handler
# already catches and degrades to "Unknown"/"Unknown" rather than failing
# the request, and no test here asserts on that value (see
# test_config_oracle.py's equivalent tests).

class _FakeRedshiftCursor:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))


class FakeRedshiftConnection:
    def __init__(self):
        self.cursor_calls = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeRedshiftCursor(self.cursor_calls)

    def close(self):
        self.closed = True


class FakeRedshiftConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call
        self.connections = []  # the FakeRedshiftConnection returned by each call

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        connection = FakeRedshiftConnection()
        self.connections.append(connection)
        return connection


def install_fake_redshift_connect(monkeypatch):
    """Patches backends.redshift's psycopg2.connect with a fake that
    records its kwargs instead of opening a real connection, and returns
    the FakeRedshiftConnectHarness controlling it. Must be called *after*
    the module has been imported, same caveat as install_fake_oracle_connect
    above. A separate fake from backends.postgres's own `import psycopg2` -
    patching backends.redshift's module-level reference has no effect on
    backends.postgres's, even though both ultimately name the same
    third-party package, since each module holds its own name binding from
    its own `import psycopg2` statement."""
    import backends.redshift as rsmod

    harness = FakeRedshiftConnectHarness()
    monkeypatch.setattr(rsmod.psycopg2, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake SQL Server (pytds-shaped) connection/cursor
# ---------------------------------------------------------------------------
# pytds implements the same PEP 249 DB-API cursor shape (execute/
# description/fetchall/rowcount, cursor as a context manager) psycopg2
# does, so FakePgCursor above is reused directly for get_schema()/execute()/
# identity_label() tests - same approach already used for Snowflake/MySQL
# (see the comments above FakeSnowflakeConnectHarness/FakeMySQLConnection).
# Unlike Oracle's/Redshift's connection fakes, no autocommit attribute
# assignment support is needed here - backends/mssql.py's connect() passes
# autocommit as a pytds connect()-time kwarg rather than setting it as a
# post-connect attribute (see that module's docstring), so execute() never
# touches connection.autocommit at all. What this fake DOES need to support
# that FakePgConnection doesn't: an arbitrary `mssql_schema` attribute,
# since backends/mssql.py's connect() stashes the descriptor's "schema"
# value directly on the connection object for get_schema() to read back
# (there's no session-level "SET schema"-equivalent statement to bake it
# into the session context the way Oracle's/Redshift's connect() do - see
# that module's docstring) - a plain class with a real __dict__ (not a
# bare object()) supports this with no special handling needed.

class FakeMssqlConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor
        self.closed = False
        self.mssql_schema = None

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def make_fake_mssql_connection(responses, schema=None):
    cursor = FakePgCursor(responses)
    connection = FakeMssqlConnection(cursor)
    connection.mssql_schema = schema
    return connection, cursor


class FakeMssqlConnectHarness:
    def __init__(self):
        self.calls = []  # list of kwargs dicts, one per connect() call
        self.connections = []  # the FakeMssqlConnection returned by each call
        # Seconds connect() blocks before returning/raising - 0 (the
        # default) is instant, same as every other test using this
        # harness. Set nonzero only by the hard-timeout tests in
        # test_mssql_backend.py, which simulate pytds.connect() itself
        # hanging past DB_CONNECT_TIMEOUT_SECONDS (see backends/mssql.py's
        # _connect_with_hard_timeout) - mirrors _FakeBackend's own `delay`
        # param in test_execute_routes.py.
        self.delay = 0
        # Exception connect() raises after `delay` (instead of returning a
        # connection) - unset (None) by default. Lets a test simulate a
        # connect() that eventually fails on its own, distinct from one
        # that eventually succeeds late (see connect() below).
        self.raise_exc = None
        # Set once connect()'s sleep actually finishes - lets a timeout
        # test confirm the abandoned background thread really did keep
        # running (and, on the "late success" path, that the resulting
        # connection was actually closed by _close_late_connection) without
        # the test itself needing to sleep any longer than the timeout
        # it's testing. Mirrors _FakeBackend.execute_finished in
        # test_execute_routes.py.
        self.connect_finished = None

    def connect(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.connect_finished is not None:
            self.connect_finished.set()
        if self.raise_exc:
            raise self.raise_exc
        connection = FakeMssqlConnection()
        self.connections.append(connection)
        return connection


def install_fake_mssql_connect(monkeypatch):
    """Patches backends.mssql's pytds.connect with a fake that records its
    kwargs instead of opening a real connection, and returns the
    FakeMssqlConnectHarness controlling it. Must be called *after* the
    module has been imported, same caveat as install_fake_oracle_connect
    above."""
    import backends.mssql as msmod

    harness = FakeMssqlConnectHarness()
    monkeypatch.setattr(msmod.pytds, "connect", harness.connect)
    return harness


# ---------------------------------------------------------------------------
# Fake Google Sheets ("gviz") HTTP layer
# ---------------------------------------------------------------------------
# Unlike every other backend above, backends/sheets.py talks to a real DB-API
# driver at all - there's no connect()-returning-a-live-object shape to fake
# here. Instead it issues one requests.get() per query against Google's gviz
# endpoint, so this fake patches backends.sheets's module-level `requests`
# reference with a fake `.get` that records each call's url/params/timeout
# and returns a queued canned HTTP response (status_code + text) built to
# look exactly like gviz's real JSONP-wrapped JSON body - confirmed live
# against the real endpoint during this feature's design (see
# backends/sheets.py's module docstring), not guessed at.

def make_gviz_table_json(cols, rows, wrapped=True):
    """Builds gviz response text for a successful query. `cols` is a list of
    {"label", "type"} dicts (matching gviz's own per-column shape); `rows`
    is a list of lists of raw cell values - a plain value becomes {"v":
    value}, and a (v, f) tuple becomes {"v": v, "f": f} (the "formatted
    string" gviz attaches to date/datetime/timeofday cells - see
    backends/sheets.py's f-vs-v handling). `wrapped=True` (the default)
    produces the real JSONP-callback-wrapped form
    (`google.visualization.Query.setResponse({...});`); `wrapped=False`
    produces bare JSON - both forms are handled defensively by
    backends.sheets._parse_gviz_response, and both are exercised across
    this file's tests since which one `tqx=out:json` actually returns
    wasn't pinned down to just one ahead of time."""
    def _cell(value):
        if isinstance(value, tuple):
            v, f = value
            return {"v": v, "f": f}
        if value is None:
            return None
        return {"v": value}

    body = {
        "version": "0.6", "reqId": "0", "status": "ok",
        "table": {
            "cols": list(cols),
            "rows": [{"c": [_cell(v) for v in row]} for row in rows],
        },
    }
    text = json.dumps(body)
    if wrapped:
        return f"google.visualization.Query.setResponse({text});"
    return text


def make_gviz_error_json(detailed_message, wrapped=True):
    """Builds gviz response text for a semantically-bad query (e.g. a
    nonexistent column) - HTTP 200 with a status:"error" body, exactly as
    confirmed live against the real endpoint (see backends/sheets.py's
    module docstring) - NOT an HTTP error status."""
    body = {
        "version": "0.6", "reqId": "0", "status": "error",
        "errors": [{"reason": "invalid_query", "message": "INVALID_QUERY", "detailed_message": detailed_message}],
    }
    text = json.dumps(body)
    if wrapped:
        return f"google.visualization.Query.setResponse({text});"
    return text


class FakeSheetsResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSheetsRequestsHarness:
    """Records every requests.get() call (url/params/timeout) and returns
    queued canned responses in order - queue_response() appends one;
    calling .get() with an empty queue raises, so every test is explicit
    about what each call should return rather than silently reusing a
    stale default."""

    def __init__(self):
        self.calls = []
        self._queue = []

    def queue_response(self, status_code=200, text=""):
        self._queue.append(FakeSheetsResponse(status_code=status_code, text=text))

    def queue_table(self, cols, rows, wrapped=True):
        self.queue_response(status_code=200, text=make_gviz_table_json(cols, rows, wrapped=wrapped))

    def queue_error(self, detailed_message, wrapped=True):
        self.queue_response(status_code=200, text=make_gviz_error_json(detailed_message, wrapped=wrapped))

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        if not self._queue:
            raise AssertionError(
                "FakeSheetsRequestsHarness.get() called with an empty response "
                "queue - call queue_response()/queue_table()/queue_error() first."
            )
        return self._queue.pop(0)


def install_fake_sheets_requests(monkeypatch):
    """Patches backends.sheets's module-level `requests` reference so its
    .get is the fake above, and returns the FakeSheetsRequestsHarness
    controlling it. Must be called *after* the module has been imported,
    same ordering caveat as install_fake_oracle_connect/etc. above."""
    import backends.sheets as shmod

    harness = FakeSheetsRequestsHarness()
    monkeypatch.setattr(shmod.requests, "get", harness.get)
    return harness


# ---------------------------------------------------------------------------
# Fake Firestore client
# ---------------------------------------------------------------------------
# A hand-built fake that reproduces real Firestore semantics closely enough
# to catch the class of bug that motivated it: `.set(data, merge=True)`
# (boolean) performs a *recursive* merge of nested map fields (a key
# missing from a new nested map is left as whatever the old document had),
# while `.set(data, merge=[<field paths>])` (a list) replaces each named
# top-level field atomically, including whole nested maps, and leaves any
# other top-level field alone. See state_store.py's FirestoreStateStore.
# set_session for why this distinction matters in this app specifically.

class _FakeFirestoreDoc:
    def __init__(self, doc_id, data=None):
        self.id = doc_id
        self._data = dict(data) if data else None

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeFirestoreDocRef:
    def __init__(self, store, collection_name, doc_id):
        self._store = store
        self._collection_name = collection_name
        self._doc_id = doc_id

    def get(self):
        coll = self._store._collections.setdefault(self._collection_name, {})
        return _FakeFirestoreDoc(self._doc_id, coll.get(self._doc_id))

    def set(self, data, merge=False):
        coll = self._store._collections.setdefault(self._collection_name, {})
        existing = coll.get(self._doc_id)
        clean_data = {k: v for k, v in data.items() if k != "updated_at"}
        # Real Firestore's firestore.DELETE_FIELD sentinel means "remove
        # this top-level field from the document" rather than "set it to
        # this value" - used by state_store.py's lazy session migration to
        # actually scrub old fields (database_url/database_type/
        # database_config/custom_connection_key) rather than just leaving
        # them sitting alongside the new connection_id field. Only
        # meaningful for merge calls (merge=True or a field-path list) -
        # real Firestore doesn't accept it in a plain overwrite .set()
        # either, so this fake doesn't need to handle that case.
        delete_fields = {k for k, v in clean_data.items() if v is firestore.DELETE_FIELD}
        clean_data = {k: v for k, v in clean_data.items() if v is not firestore.DELETE_FIELD}

        if merge is True:
            # Real Firestore boolean-merge semantics: recursively merge
            # nested dicts (a key present in the old nested dict but absent
            # from the new one survives), shallow-replace everything else.
            def deep_merge(old, new):
                if old is None:
                    return dict(new)
                merged = dict(old)
                for k, v in new.items():
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k] = deep_merge(merged[k], v)
                    else:
                        merged[k] = v
                return merged
            merged = deep_merge(existing, clean_data)
            for field in delete_fields:
                merged.pop(field, None)
            coll[self._doc_id] = merged
        elif isinstance(merge, (list, tuple, set)):
            # Field-path merge: each named top-level field is replaced
            # atomically (no recursive merge into it); every other
            # existing top-level field is left untouched.
            merged = dict(existing) if existing else {}
            for field in merge:
                if field in clean_data:
                    merged[field] = clean_data[field]
                elif field in delete_fields:
                    merged.pop(field, None)
            coll[self._doc_id] = merged
        else:
            coll[self._doc_id] = dict(clean_data)

    def delete(self):
        coll = self._store._collections.setdefault(self._collection_name, {})
        coll.pop(self._doc_id, None)

    @property
    def reference(self):
        return self


class _FakeFirestoreQuery:
    def __init__(self, store, collection_name, filters=None, order=None, limit_n=None):
        self._store = store
        self._collection_name = collection_name
        self._filters = filters or []
        self._order = order
        self._limit = limit_n

    def where(self, field, op, value):
        assert op == "==", f"fake Firestore only supports '==' filters, got {op!r}"
        return _FakeFirestoreQuery(
            self._store, self._collection_name, self._filters + [(field, value)],
            self._order, self._limit,
        )

    def order_by(self, field, direction=None):
        return _FakeFirestoreQuery(
            self._store, self._collection_name, self._filters,
            (field, direction), self._limit,
        )

    def limit(self, n):
        return _FakeFirestoreQuery(
            self._store, self._collection_name, self._filters, self._order, n,
        )

    def stream(self):
        coll = self._store._collections.setdefault(self._collection_name, {})
        docs = [
            _FakeFirestoreDoc(doc_id, data) for doc_id, data in coll.items()
            if all(data.get(f) == v for f, v in self._filters)
        ]
        # Give each returned doc a working .reference for
        # purge_translation_history()'s batch.delete(doc.reference).
        for d in docs:
            d.reference = _FakeFirestoreDocRef(self._store, self._collection_name, d.id)
        if self._order:
            field, direction = self._order
            reverse = str(direction).endswith("DESCENDING") if direction is not None else False
            docs.sort(key=lambda d: d.to_dict().get(field) or "", reverse=reverse)
        if self._limit is not None:
            docs = docs[: self._limit]
        return docs


class _FakeFirestoreCollection:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _FakeFirestoreDocRef(self._store, self._name, doc_id)

    def where(self, field, op, value):
        return _FakeFirestoreQuery(self._store, self._name).where(field, op, value)

    def add(self, data):
        import uuid
        doc_id = uuid.uuid4().hex
        coll = self._store._collections.setdefault(self._name, {})
        coll[doc_id] = dict(data)
        return None, _FakeFirestoreDocRef(self._store, self._name, doc_id)

    def stream(self):
        return _FakeFirestoreQuery(self._store, self._name).stream()


class _FakeFirestoreBatch:
    def __init__(self, store):
        self._store = store
        self._deletes = []

    def delete(self, ref):
        self._deletes.append(ref)

    def commit(self):
        for ref in self._deletes:
            ref.delete()
        self._deletes = []


class FakeFirestoreClient:
    """A minimal, in-memory stand-in for google.cloud.firestore.Client -
    enough surface area for state_store.py's FirestoreStateStore, with
    accurate merge=True vs merge=[...] semantics (see module comment
    above). Data lives in self._collections: {collection_name: {doc_id:
    data_dict}} - inspect it directly in tests when convenient."""

    def __init__(self):
        self._collections = {}

    def collection(self, name):
        return _FakeFirestoreCollection(self, name)

    def batch(self):
        return _FakeFirestoreBatch(self)


class _FakeFirestoreQueryModule:
    DESCENDING = "DESCENDING"
    ASCENDING = "ASCENDING"


def days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)
