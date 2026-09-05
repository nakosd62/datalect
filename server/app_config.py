"""
app_config.py

Environment parsing, GCP/Cloud Run detection, Flask app + CORS setup, and
state-store construction. This module owns every process-wide singleton
(the Flask `app`, `state_store`, `CONFIGURED_DBS`, etc.) so that the route
modules can just `from app_config import app, state_store, ...` without
needing to know how any of it was built or in what order.

Import this module first (server.py does, and every route module imports
from it) - it has the side effects of creating the Flask app and, on
Cloud Run, connecting to Firestore.
"""

import hashlib
import json
import logging
import os

from flask import Flask
from flask_cors import CORS
from google.cloud import firestore

from state_store import SqliteStateStore, FirestoreStateStore, is_db_config_encryption_configured
from sheets_util import extract_spreadsheet_id

from dotenv import load_dotenv

# YDYL_SKIP_DOTENV lets an isolated harness (see playwright.config.js's
# webServer block, used by the e2e test suite) guarantee this process never
# picks up a real local .env. load_dotenv() with no explicit path walks UP
# the directory tree from this file's own location looking for a file named
# .env, so it would find and load the real repo-root .env - real API keys,
# real DATABASE_PRESETS connection strings - regardless of the child
# process's cwd or any env vars explicitly passed to it (override=True
# means .env's values win over those). Left unset (the default for every
# normal run, e.g. via run_server.sh), behavior is unchanged.
if os.environ.get("YDYL_SKIP_DOTENV") != "1":
    load_dotenv(override=True)

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Keep third-party libraries (google-auth, google-genai, urllib3, etc.) quiet -
# they all feed into the root logger config above, so without this line the
# fix for leaking exception details ended up making local logs noisier, not
# cleaner. Only our own app logger gets the more verbose level.
logger = logging.getLogger("ydyl")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# python-tds (import name `pytds`, used by backends/mssql.py) logs a
# WARNING on every connection retry attempt that overran its own allotted
# sub-timeout: "Work attempt exceeded it's allocated time X, actual time
# was Y." Per backends/mssql.py's _connect_with_hard_timeout docstring,
# this isn't a one-off - it's a known quirk in pytds's own retry/backoff
# accounting (it tracks elapsed time by each attempt's ALLOTTED time, not
# how long the attempt actually took), so EVERY attempt against a slow or
# unreachable SQL Server logs one of these, and they carry no information
# this app doesn't already report itself (the eventual connect failure is
# logged, with a real traceback, via this app's own `logger.exception`
# call in db.py's _fetch_database_schema/execute_routes.py). Raised to
# ERROR rather than silenced outright (no logger.disabled/NullHandler)
# so a genuine pytds-side ERROR - if it ever logs one - still surfaces.
logging.getLogger("pytds").setLevel(logging.ERROR)


def log_and_generalize_error(context, exc):
    """
    Logs the full exception server-side (with traceback) under a short
    error id, and returns a generic, safe-to-display message for the
    client that includes that id for support/correlation purposes.
    Never put str(exc) directly into a client-facing response - DB/API
    errors can leak schema, table/column names, hostnames, etc.
    """
    import uuid
    error_id = uuid.uuid4().hex[:8]
    logger.exception("[%s] %s", error_id, context)
    return f"{context}. Please try again or contact support (ref: {error_id})."


# --- 1. Environment & Cloud Run Detection -----------------------------------
# GCP_PROJECT_ID picks which project's Firestore is used for state storage
# (sessions, custom connections, translation history) - see the Firestore
# Initialization section below. It is NOT a BigQuery billing default - there
# is deliberately no env var that provides one. A BigQuery preset that reads
# data outside this app's own project (e.g. a public dataset) MUST set its
# own "billing_project_id" explicitly in DATABASE_PRESETS (see below); a
# user's custom BigQuery connection must always supply its own
# billing_project_id AND its own service-account key (see config_routes.py)
# - this app's own project never silently pays for either. Leave this unset
# locally to keep local state in SQLite.
GCP_PROJECT_ID = (
    os.environ.get("GCP_PROJECT_ID")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
    or os.environ.get("GCP_PROJECT")
)
IS_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))

# --- 2. Firestore Initialization --------------------------------------------
firestore_client = None
if GCP_PROJECT_ID:
    try:
        firestore_client = firestore.Client(project=GCP_PROJECT_ID, database="ydyl")
        logger.info("Initialized Firestore client for project '%s'.", GCP_PROJECT_ID)
    except Exception:
        logger.exception("Error initializing Firestore client")

# --- Authentication & State Configuration -----------------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)

# Startup / Module Scope Guard
if IS_CLOUD_RUN and not firestore_client:
    raise RuntimeError(
        f"CRITICAL: Service running on Cloud Run (K_SERVICE={os.environ.get('K_SERVICE')}), "
        f"but Firestore client failed to initialize (GCP_PROJECT_ID={GCP_PROJECT_ID}). "
        "Halting startup to prevent ephemeral SQLite fallback."
    )

# Saved connections (database_config - passwords, service-account keys,
# private keys, CA certificates, ...) are encrypted at rest using a key
# read from DB_CONFIG_ENCRYPTION_KEY (see state_store.py's encryption-at-
# rest comment for the full design). Locally, an unset/invalid key just
# means database_config is stored unencrypted - convenient for zero-config
# dev, same posture as GOOGLE_CLIENT_ID being unset. On Cloud Run, where
# real users' real credentials are actually at stake, that same silent
# fallback would be a genuine security regression nobody would notice
# until it mattered - so, same as the Firestore guard just above, this
# halts startup rather than allowing it.
if IS_CLOUD_RUN and not is_db_config_encryption_configured():
    raise RuntimeError(
        f"CRITICAL: Service running on Cloud Run (K_SERVICE={os.environ.get('K_SERVICE')}), "
        "but DB_CONFIG_ENCRYPTION_KEY is not set to a valid Fernet key. Saved connection "
        "details (passwords, service-account keys, private keys, CA certificates, ...) "
        "would be stored in plaintext in Firestore. Halting startup rather than silently "
        "storing credentials unencrypted. Generate a key with: "
        'python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" '
        "and set it as DB_CONFIG_ENCRYPTION_KEY."
    )

# --- Flask app ---------------------------------------------------------------
app = Flask(__name__, static_folder='../webClient', static_url_path='')
# Support credentials for authenticated CORS requests
CORS(app, supports_credentials=True)

# --- Client build id ---------------------------------------------------------
# Lets the frontend detect "a reload would pick up new code" without nagging
# on every server restart - see config_routes.py's GET /api/client-version
# docstring for how client.js actually uses this. Hashes the CONTENT of the
# static files that actually affect what the browser runs (index.html/
# client.js/style.css), not their mtimes (a fresh `git checkout`/container
# rebuild resets mtimes to "now" regardless of whether a file's own bytes
# changed - see the earlier device-bridge git-lock/reload conversation this
# feature grew out of) and not the server process's own start time either,
# which would report "new version" after EVERY restart even when nothing
# under webClient/ changed at all (a backend-only fix, an env var tweak, a
# database credential rotation - exactly the "not every restart requires a
# client reload" distinction this constant exists to make). Computed once,
# at import time: these are a handful of small text files, so hashing them
# costs nothing worth caching more cleverly than a plain module-level
# constant re-read on every request.
def _compute_client_build_id():
    hasher = hashlib.sha256()
    for filename in ("index.html", "client.js", "style.css"):
        path = os.path.join(app.static_folder, filename)
        try:
            with open(path, "rb") as f:
                hasher.update(f.read())
        except OSError:
            # Missing file (a stripped-down dev checkout, a build step that
            # hasn't produced it yet) - still yields SOME id rather than
            # crashing startup over what's ultimately just a "please
            # reload" nicety; the fixed sentinel keeps a missing file from
            # hashing identically to an empty one.
            hasher.update(b"\0missing:" + filename.encode())
    return hasher.hexdigest()[:12]


CLIENT_BUILD_ID = _compute_client_build_id()

DEFAULT_CONN = "postgresql://postgres:password@host:23456/defaultdb?sslmode=verify-full"

# --- Admin-preset database connections ---------------------------------------
# DATABASE_PRESETS_FILE holds the *path* to a JSON file containing an array
# of preset-connection objects, one per preset. This replaced an earlier
# design (a DATABASE_PRESETS env var holding the JSON *inline*) which itself
# had replaced the original positional CSV lists (DATABASE_NAMES/
# DATABASE_URLS for Postgres, BIGQUERY_NAMES/BIGQUERY_PROJECT_IDS/
# BIGQUERY_DATASETS for BigQuery). The inline-env-var form forced the whole
# array onto one line (env vars can't contain real newlines in most
# deployment tooling) - unreadable and unreviewable once there were more
# than one or two presets. A file has no such restriction: write it
# multi-line, indented, with real JSON formatting, and just point
# DATABASE_PRESETS_FILE at its path. Every object needs "type" and "name",
# and should also carry an "id" - a short, admin-chosen string, unique
# across this file, that never changes for that preset's lifetime (e.g.
# "ecommerce-prod", not a UUID - it's never sent to an anonymous visitor
# either, so there's no reason to make it opaque). This is what a session
# actually pins its active preset to (see config_routes.py's handle_config)
# - unlike matching by "url" (ambiguous once a custom connection can share
# a preset's URL) or by position in this file (silently wrong the moment
# a preset is reordered or removed), "id" is a stable identity an admin
# controls directly. "id" is optional only for backward compatibility -
# a preset missing it falls back to "{type}+{name}" (e.g. "postgres+Demo
# DB"), which still moves with the preset if the file gets reordered
# (unlike a position-based fallback), but breaks the moment "name" or
# "type" is edited - so this is still just a migration aid, not a
# substitute for adding a real explicit "id" to every preset here as soon
# as convenient. Two presets sharing the same "id" is a config error: the
# later one is skipped entirely (logged as a warning), exactly like any
# other malformed preset below (missing "name"/"url"/credential) - it never
# ends up in CONFIGURED_DBS at all, rather than loading anyway and quietly
# activating the WRONG connection whenever its (collided) radio is clicked.
# The rest of each object's shape is dialect-specific:
#   Postgres:  {"type": "postgres", "name": "...", "url": "postgresql://...",
#               "schema": "..."} ("schema" optional - see below)
#   MySQL:     {"type": "mysql", "name": "...", "url": "mysql://..."}
#   BigQuery:  {"type": "bigquery", "name": "...", "project_id": "...", "dataset": "..."}
#   Snowflake: {"type": "snowflake", "name": "...", "account": "...", "user": "...",
#               "warehouse": "...", "database": "...", "schema": "...", "role": "...",
#               "password": "..."} (or "private_key"/"private_key_passphrase"
#               instead of "password" - see below)
#   Databricks: {"type": "databricks", "name": "...", "server_hostname": "...",
#               "http_path": "...", "access_token": "...", "catalog": "...",
#               "schema": "..."} ("catalog"/"schema" optional - see below)
#   Oracle:     {"type": "oracle", "name": "...", "host": "...", "port": 1521,
#               "service_name": "..." (or "sid" instead), "user": "...",
#               "password": "...", "schema": "...", "ssl": false}
#               ("port"/"schema"/"ssl" optional - see below)
#   Redshift:   {"type": "redshift", "name": "...", "host": "...", "port": 5439,
#               "database": "...", "user": "...", "password": "...", "schema": "..."}
#               ("port"/"schema" optional - see below)
#   SQL Server: {"type": "mssql", "name": "...", "host": "...", "port": 1433,
#               "database": "...", "user": "...", "password": "...", "schema": "...",
#               "encrypt": true}
#               ("port"/"schema"/"encrypt" optional - see below)
#   Google Sheets: {"type": "sheets", "name": "...", "spreadsheet_url": "..."
#               (or "spreadsheet_id" instead), "tab_name": "...",
#               "credentials_json": "..." (optional - see below)}
#   MongoDB Atlas SQL: {"type": "MongoDB" (case-insensitive on input; the
#               "type" this app then stores/exposes is always the exact
#               "MongoDB" spelling below, same as every other type here
#               canonicalizes to its own fixed spelling), "name": "...",
#               "url": "mongodb://...", "database": "...", "user": "...",
#               "password": "..."} ("url"/"database"/"user"/"password" all
#               required - see below). Unlike Oracle/Redshift/SQL Server
#               above, "url" here IS a real, driver-parsed value (a bare
#               mongodb:// URI, same as Postgres/MySQL's own "url") - it's
#               just that database/user/password are separate fields
#               instead of being packed into that same string (see
#               backends/mongodb_sql.py's module docstring for why: this
#               dialect's actual ODBC connection string also needs a fixed
#               "Driver={...}" clause and a "Uri=" key name neither of
#               which are this file's concern - connect() injects both).
#               This dialect is also read-only (SELECT-only - no write
#               statements exist for it).
# Example file contents:
#   [
#     {
#       "type": "postgres",
#       "name": "E-Commerce Store",
#       "url": "postgresql://demo:FooBar@127.0.0.1:5432/helloworld-vector-1785975353349"
#     },
#     {
#       "type": "bigquery",
#       "name": "Analytics",
#       "project_id": "my-project",
#       "dataset": "analytics"
#     },
#     {
#       "type": "snowflake",
#       "name": "Sample Data",
#       "account": "myorg-myaccount",
#       "user": "svc_ydyl",
#       "warehouse": "COMPUTE_WH",
#       "database": "SNOWFLAKE_SAMPLE_DATA",
#       "schema": "TPCH_SF1",
#       "password": "..."
#     },
#     {
#       "type": "databricks",
#       "name": "Sales Lakehouse",
#       "server_hostname": "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
#       "http_path": "/sql/1.0/warehouses/0123456789abcdef",
#       "access_token": "...",
#       "catalog": "main",
#       "schema": "sales"
#     },
#     {
#       "type": "oracle",
#       "name": "Orders (Oracle)",
#       "host": "db.example.com",
#       "port": 1521,
#       "service_name": "ORCLPDB1",
#       "user": "svc_ydyl",
#       "password": "...",
#       "ssl": true
#     },
#     {
#       "type": "redshift",
#       "name": "Warehouse (Redshift)",
#       "host": "my-cluster.abc123.us-east-1.redshift.amazonaws.com",
#       "port": 5439,
#       "database": "dev",
#       "user": "svc_ydyl",
#       "password": "..."
#     },
#     {
#       "type": "mssql",
#       "name": "Orders (SQL Server)",
#       "host": "my-server.database.windows.net",
#       "port": 1433,
#       "database": "orders",
#       "user": "svc_ydyl",
#       "password": "...",
#       "encrypt": true
#     },
#     {
#       "type": "sheets",
#       "name": "Team Roster (Sheet)",
#       "spreadsheet_url": "https://docs.google.com/spreadsheets/d/1AbCdEf.../edit",
#       "tab_name": "Roster"
#     }
#   ]
# Unlike Postgres presets (a connection string with embedded credentials),
# admin-preset BigQuery connections carry no credential at all: the app
# authenticates as its own Cloud Run service account (Application Default
# Credentials) - an admin who wants to preset a BigQuery connection is
# expected to have granted that service account the appropriate IAM role on
# the project/dataset. Per-user custom BigQuery connections (with their own
# pasted service-account key) are a separate, user-scoped concept handled in
# state_store.py/config_routes.py, not here.
#
# Snowflake presets are different again: Snowflake has no ADC-equivalent
# ambient identity at all (see backends/snowflake.py's module docstring),
# so a Snowflake preset MUST carry its own explicit "password" or
# "private_key" right here in the file - there's no service-account-style
# fallback to grant IAM on instead. "account"/"user"/"warehouse"/"database"
# are required; "schema"/"role" are optional (omitted = the account's own
# defaults). Exactly one of "password"/"private_key" must be present -
# "private_key_passphrase" is only meaningful alongside "private_key", for
# a key that was itself encrypted at generation time.
#
# Databricks presets are the same story as Snowflake's - no ADC-equivalent
# ambient identity, so a Databricks preset MUST carry its own explicit
# "access_token" (a Personal Access Token) right here in the file. This
# first pass only supports PAT auth - not OAuth (user SSO or a service-
# principal client-credentials flow) - see backends/databricks.py's module
# docstring. "server_hostname"/"http_path"/"access_token" are required;
# "catalog"/"schema" are optional (omitted = whatever the workspace/
# warehouse's own default namespace is).
#
# Oracle presets are the same story again - no ADC-equivalent ambient
# identity, so an Oracle preset MUST carry its own explicit "password" right
# here in the file (this first pass is plain username/password only, not
# Oracle Autonomous Database's wallet-based mTLS - see backends/oracle.py's
# module docstring). "host"/"user"/"password" are required, and exactly one
# of "service_name"/"sid" must be present to identify which (pluggable)
# database to connect to. "port" defaults to 1521 (Oracle's standard
# listener port) when omitted; "schema" is optional (omitted = the
# connecting user's own schema/objects - see backends/oracle.py's module
# docstring for why a "schema" in Oracle is actually a user, not a separate
# namespace). "ssl" is optional, defaulting to false - set it true for
# Oracle Cloud/Autonomous Database targets, whose listeners are TLS-only
# (see backends/oracle.py's module docstring for what this does and why a
# plain-TCP connect() attempt against one fails with a confusing
# "DPY-4011: the database or network closed the connection" rather than a
# normal auth error).
#
# Postgres presets' optional "schema" field is the one exception to "the
# rest of each object's shape is dialect-specific" above - it isn't packed
# into the "url" connection string at all, but a separate field alongside
# it. It works exactly like Redshift's own "schema" (Redshift IS Postgres,
# wire-protocol-wise - see backends/redshift.py's module docstring): omitted
# (every preset that predates this field, and the overwhelming common case)
# behaves exactly as before - the connecting user's own default search_path,
# ordinarily "public". Given, connect() runs `SET search_path TO <schema>,
# public` right after connecting - see backends/postgres.py's module
# docstring for why get_schema()'s introspection queries are scoped via
# current_schema() rather than a hardcoded 'public' to make them follow it.
#
# Redshift presets are the same "no ambient identity" story as Databricks'/
# Oracle's - a Redshift preset MUST carry its own explicit "password" right
# here in the file (this first pass is plain username/password only, over
# TLS - always required, not opt-in the way Oracle's "ssl" flag is - see
# backends/redshift.py's module docstring; AWS IAM temporary credentials
# and the Redshift Data API are deferred follow-up, not supported here).
# "host"/"database"/"user"/"password" are required; "port" defaults to 5439
# (Redshift's standard port) when omitted; "schema" is optional (omitted =
# the connecting user's own default search_path - unlike Oracle, Redshift
# has genuine Postgres-style schemas, so this really is a separate
# namespace, not a stand-in for a user - see backends/redshift.py's module
# docstring).
#
# SQL Server presets are the same "no ambient identity" story again - an
# mssql preset MUST carry its own explicit "password" right here in the file
# (this first pass is plain SQL Login username/password only, not Windows/AD/
# Azure AD auth - see backends/mssql.py's module docstring). "host"/
# "database"/"user"/"password" are required; "port" defaults to 1433 (SQL
# Server's standard port) when omitted; "schema" is optional (omitted = the
# connecting login's own default schema, commonly "dbo" - see
# backends/mssql.py's module docstring for why the schema is applied by
# scoping every introspection query rather than by a session-level SET, the
# way Oracle's/Redshift's schema is). "encrypt" is optional, defaulting to
# true when omitted (mirrors Oracle's "ssl" flag, but opposite default - most
# real SQL Server deployments, and Azure SQL Database in particular, require
# encryption outright - see backends/mssql.py's module docstring for the
# "cafile"/certifi mechanics and its self-signed-CA limitation).
#
# Google Sheets presets are architecturally different from every dialect
# above: by default there's no credential at all - a "sheets" preset with
# no "credentials_json" reaches only a spreadsheet genuinely shared as
# "Anyone with the link can view" (or published to the web), since this
# app has no Google identity of its own to act on anyone's behalf (see
# backends/sheets.py's module docstring). An entry MAY optionally carry
# its own "credentials_json" (a pasted service-account key, pasted here in
# full just like a Snowflake preset already carries its own "password") to
# reach a PRIVATE spreadsheet explicitly shared with that service
# account's email - passed through verbatim below, no resolver needed
# since presets aren't user-editable/re-saved through the app the way
# custom connections are.
#
# There IS one ambient identity available, though, unlike every dialect
# above (which either always or never have one): SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE,
# a single service-account key configured once for the whole app, used as
# a fallback by ANY Sheets connection (preset or custom) that doesn't
# supply its own "credentials_json" - see backends/sheets.py's module
# docstring for the full mechanism. That fallback is resolved entirely
# inside backend.connect(), not here, so a preset entry with no
# "credentials_json" of its own may still reach a private spreadsheet in
# practice if that env var is set - this file doesn't need to know either
# way for its own parsing to be correct.
#
# "spreadsheet_url" (a full pasted Sheets URL) or "spreadsheet_id" (the
# bare id) and "tab_name" are required - the tab is selected by its
# display name, not a numeric index, so renaming a tab in the spreadsheet
# breaks a preset pointed at the old name.
#
# No implicit default path: if DATABASE_PRESETS_FILE isn't set, presets are
# empty (same "no presets configured" fallback as before - see below).
# Nothing loads silently off disk unless explicitly pointed at it, matching
# how GCP_PROJECT_ID/billing_project_id already work in this app - see the
# comment above GCP_PROJECT_ID.
#
# Relative paths resolve against the process's current working directory,
# same as TRANSLATION_STATS_DB_PATH below - run from the repo root (as
# README.md's Quick start section already asks for) and a path like
# "./database_presets.json" just works, matching the Docker image's
# WORKDIR /app too.
DATABASE_PRESETS_FILE = os.environ.get("DATABASE_PRESETS_FILE", "").strip()

raw_db_presets = ""
if DATABASE_PRESETS_FILE:
    try:
        with open(DATABASE_PRESETS_FILE, "r", encoding="utf-8") as f:
            raw_db_presets = f.read()
    except OSError as e:
        logger.error(
            "Failed to read DATABASE_PRESETS_FILE '%s', ignoring it entirely: %s",
            DATABASE_PRESETS_FILE, e,
        )

CONFIGURED_DBS = []
if raw_db_presets.strip():
    try:
        parsed_presets = json.loads(raw_db_presets)
        if not isinstance(parsed_presets, list):
            raise ValueError("DATABASE_PRESETS_FILE contents must be a JSON array")
    except (ValueError, TypeError) as e:
        logger.error(
            "Failed to parse DATABASE_PRESETS_FILE ('%s') contents, ignoring it entirely: %s",
            DATABASE_PRESETS_FILE, e,
        )
        parsed_presets = []

    _seen_preset_ids = set()

    for entry in parsed_presets:
        if not isinstance(entry, dict):
            logger.warning("Skipping database preset entry that is not a JSON object: %r", entry)
            continue

        db_type = (entry.get("type") or "postgres").strip().lower()
        name = (entry.get("name") or "").strip()
        if not name:
            logger.warning("Skipping database preset entry with no 'name': %r", entry)
            continue

        # See the DATABASE_PRESETS_FILE comment above for what "id" is and
        # why it exists - a preset missing it falls back to "{type}+{name}"
        # rather than its position in this file, so it survives the file
        # being reordered (though not a later rename of "name"/"type"
        # itself); this is purely a migration aid, not the recommended
        # long-term state for any preset here. Never collides with a saved
        # custom connection's own identity (state_store.py's
        # compute_connection_key(), a sha256 hex digest) - those live in a
        # completely separate lookup (a different DB table, keyed by
        # (user_id, connection_key)), never compared against CONFIGURED_DBS
        # ids anywhere in this codebase.
        preset_id = (entry.get("id") or "").strip() or f"{db_type}+{name}"
        if preset_id in _seen_preset_ids:
            # Same treatment as every other malformed preset in this loop
            # (missing "name"/"url"/credential, below) - skipped entirely,
            # never added to CONFIGURED_DBS. Loading it anyway would mean
            # its config-modal radio silently activates whichever earlier
            # preset actually owns this id when clicked, which is worse
            # than just not offering it at all.
            logger.warning(
                "Skipping database preset '%s' (type=%s): its id %r collides with "
                "an earlier preset's - each preset needs a unique id (see the "
                "DATABASE_PRESETS_FILE comment above). Add an explicit \"id\" to "
                "one of them to fix this.",
                name, db_type, preset_id,
            )
            continue
        _seen_preset_ids.add(preset_id)

        if db_type == "postgres":
            url = (entry.get("url") or "").strip()
            if not url:
                logger.warning("Skipping Postgres preset '%s': missing 'url'.", name)
                continue
            preset = {"id": preset_id, "name": name, "type": "postgres", "url": url}
            # Optional, mirrors Redshift's/Oracle's/Snowflake's/Databricks'/
            # SQL Server's own "schema" field - see backends/postgres.py's
            # module docstring for the SET search_path mechanism this
            # drives. Omitted the same way every other dialect's own
            # optional "schema" is omitted here when blank.
            schema = (entry.get("schema") or "").strip()
            if schema:
                preset["schema"] = schema
            CONFIGURED_DBS.append(preset)

        elif db_type == "mysql":
            # Same shape as a Postgres preset - a single connection-string
            # URL carries everything (host, port, credentials, database) -
            # MySQL has no BigQuery-style ambient identity or Snowflake-
            # style always-explicit multi-field credential to worry about
            # here (see backends/mysql.py's module docstring).
            url = (entry.get("url") or "").strip()
            if not url:
                logger.warning("Skipping MySQL preset '%s': missing 'url'.", name)
                continue
            CONFIGURED_DBS.append({"id": preset_id, "name": name, "type": "mysql", "url": url})

        elif db_type == "mongodb":
            # Structured fields (see backends/mongodb_sql.py's module
            # docstring and this file's shape comment above): "url" is a
            # clean, real mongodb:// URI - unlike Oracle's/Redshift's
            # synthetic display-only "url" below, this one IS what gets
            # used as-is elsewhere (backends/mongodb_sql.py's connect()
            # injects the fixed Driver= clause and the Uri= key name, not
            # this file), same as Postgres/MySQL's real url. "database"/
            # "user"/"password" are ordinary required fields, same shape
            # as Redshift's - MongoDB Atlas SQL has no ADC-equivalent
            # ambient identity to fall back to either.
            url = (entry.get("url") or "").strip()
            database = (entry.get("database") or "").strip()
            mongo_user = (entry.get("user") or "").strip()
            password = entry.get("password") or ""
            if not (url and database and mongo_user and password):
                logger.warning(
                    "Skipping MongoDB Atlas SQL preset '%s': requires 'url', 'database', "
                    "'user', and 'password' - MongoDB Atlas SQL has no ADC-equivalent "
                    "ambient identity to fall back to (see backends/mongodb_sql.py's "
                    "module docstring).",
                    name,
                )
                continue
            # Stored/exposed "type" is the literal "MongoDB" (matching
            # backends/__init__.py's _BACKENDS dict key exactly, case-
            # sensitively) even though the comparison just above is against
            # the lowercased db_type - same "canonicalize on the way out,
            # case-insensitive on the way in" pattern this whole loop
            # already applies to every other dialect, just more visible
            # here since "MongoDB" (unlike "mysql"/"postgres"/etc.) isn't
            # already all-lowercase.
            CONFIGURED_DBS.append({
                "id": preset_id,
                "name": name,
                "type": "MongoDB",
                "url": url,
                "database": database,
                "user": mongo_user,
                "password": password,
            })

        elif db_type == "bigquery":
            project_id = (entry.get("project_id") or "").strip()
            dataset = (entry.get("dataset") or "").strip()
            if not (project_id and dataset):
                logger.warning(
                    "Skipping BigQuery preset '%s': requires both 'project_id' and 'dataset'.",
                    name,
                )
                continue
            # billing_project_id is who pays for/executes the query job -
            # deliberately not always project_id, since project_id/dataset
            # here just say where the data lives, and that's allowed to be
            # a project this app has no billing rights on at all (a public
            # dataset, a partner's shared project, etc). Presets authenticate
            # via this app's own ambient identity (ADC - the Cloud Run
            # service account, or local `gcloud auth application-default
            # login`), so an admin who wants a preset to read data outside
            # this app's own project MUST say explicitly who pays for it, via
            # "billing_project_id" in this preset's entry in the presets file -
            # there is deliberately no env var fallback for this (previously
            # there was; it was removed on purpose, so nothing bills against
            # this app's own project without an admin explicitly opting a
            # specific preset into it). See backends/bigquery.py's module
            # docstring for why conflating billing_project_id with project_id
            # causes a "does not have bigquery.jobs.create permission" 403
            # the moment project_id points at data you don't own.
            billing_project_id = (entry.get("billing_project_id") or "").strip()
            if not billing_project_id:
                logger.warning(
                    "BigQuery preset '%s' has no 'billing_project_id' set - "
                    "queries will bill against project_id ('%s') itself, "
                    "which will fail with a 403 unless this app's own "
                    "identity has billing rights there. Set 'billing_project_id' "
                    "explicitly on this preset in the presets file if project_id "
                    "is data you don't own (e.g. a public dataset).",
                    name, project_id,
                )
                billing_project_id = project_id
            CONFIGURED_DBS.append({
                "id": preset_id,
                "name": name,
                "type": "bigquery",
                # Synthetic identifier, not a credential - used for UI matching/
                # display and as the schema-cache key, same role a Postgres URL
                # plays elsewhere. Safe to log or send to the frontend as-is.
                "url": f"bigquery://{project_id}/{dataset}",
                "project_id": project_id,
                "dataset": dataset,
                "billing_project_id": billing_project_id,
            })

        elif db_type == "snowflake":
            account = (entry.get("account") or "").strip()
            sf_user = (entry.get("user") or "").strip()
            warehouse = (entry.get("warehouse") or "").strip()
            database = (entry.get("database") or "").strip()
            if not (account and sf_user and warehouse and database):
                logger.warning(
                    "Skipping Snowflake preset '%s': requires 'account', 'user', "
                    "'warehouse', and 'database'.",
                    name,
                )
                continue
            schema = (entry.get("schema") or "").strip()
            role = (entry.get("role") or "").strip()
            password = entry.get("password") or ""
            private_key = entry.get("private_key") or ""
            private_key_passphrase = entry.get("private_key_passphrase") or ""
            # Unlike BigQuery, Snowflake has no ambient/shared identity a
            # preset could fall back to (see backends/snowflake.py's module
            # docstring) - every Snowflake preset must carry its own
            # explicit credential right here in the presets file, exactly
            # one of "password" or "private_key". Presets (unlike per-user
            # custom connections) are visible in full to every
            # authenticated user of this deployment once selected - see
            # config_routes.py's handle_config, same as a Postgres preset's
            # embedded URL password already is today - so this file's
            # access control (kept out of version control, same as .env)
            # IS this credential's access control.
            if bool(password) == bool(private_key):
                logger.warning(
                    "Skipping Snowflake preset '%s': requires exactly one of "
                    "'password' or 'private_key', not %s.",
                    name, "both" if (password and private_key) else "neither",
                )
                continue
            preset = {
                "id": preset_id,
                "name": name,
                "type": "snowflake",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _snowflake_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                "url": f"snowflake://{account}/{database}" + (f"/{schema}" if schema else ""),
                "account": account,
                "user": sf_user,
                "warehouse": warehouse,
                "database": database,
            }
            if schema:
                preset["schema"] = schema
            if role:
                preset["role"] = role
            if password:
                preset["password"] = password
            else:
                preset["private_key"] = private_key
                if private_key_passphrase:
                    preset["private_key_passphrase"] = private_key_passphrase
            CONFIGURED_DBS.append(preset)

        elif db_type == "databricks":
            server_hostname = (entry.get("server_hostname") or "").strip()
            http_path = (entry.get("http_path") or "").strip()
            access_token = entry.get("access_token") or ""
            if not (server_hostname and http_path and access_token):
                logger.warning(
                    "Skipping Databricks preset '%s': requires 'server_hostname', "
                    "'http_path', and 'access_token' - Databricks has no ADC-equivalent "
                    "ambient identity to fall back to (see backends/databricks.py's "
                    "module docstring).",
                    name,
                )
                continue
            catalog = (entry.get("catalog") or "").strip()
            schema = (entry.get("schema") or "").strip()
            preset = {
                "id": preset_id,
                "name": name,
                "type": "databricks",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _databricks_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                "url": f"databricks://{server_hostname}{http_path}",
                "server_hostname": server_hostname,
                "http_path": http_path,
                "access_token": access_token,
            }
            if catalog:
                preset["catalog"] = catalog
            if schema:
                preset["schema"] = schema
            CONFIGURED_DBS.append(preset)

        elif db_type == "oracle":
            host = (entry.get("host") or "").strip()
            service_name = (entry.get("service_name") or "").strip()
            sid = (entry.get("sid") or "").strip()
            user = (entry.get("user") or "").strip()
            password = entry.get("password") or ""
            if not (host and (service_name or sid) and user and password):
                logger.warning(
                    "Skipping Oracle preset '%s': requires 'host', 'user', 'password', "
                    "and one of 'service_name'/'sid' - Oracle has no ADC-equivalent "
                    "ambient identity to fall back to (see backends/oracle.py's "
                    "module docstring).",
                    name,
                )
                continue
            port = entry.get("port") or 1521
            schema = (entry.get("schema") or "").strip()
            preset = {
                "id": preset_id,
                "name": name,
                "type": "oracle",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _oracle_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                "url": f"oracle://{host}:{port}/{service_name or sid}",
                "host": host,
                "port": port,
                "user": user,
                "password": password,
            }
            # service_name takes precedence over sid when both are somehow
            # present, matching backends/oracle.py's connect() - not
            # treated as an ambiguous/rejected combination the way
            # Snowflake's password-vs-private_key is, since Oracle's own
            # driver already has well-defined precedence here.
            if service_name:
                preset["service_name"] = service_name
            else:
                preset["sid"] = sid
            if schema:
                preset["schema"] = schema
            if entry.get("ssl"):
                preset["ssl"] = True
            CONFIGURED_DBS.append(preset)

        elif db_type == "redshift":
            host = (entry.get("host") or "").strip()
            database = (entry.get("database") or "").strip()
            rs_user = (entry.get("user") or "").strip()
            password = entry.get("password") or ""
            if not (host and database and rs_user and password):
                logger.warning(
                    "Skipping Redshift preset '%s': requires 'host', 'database', 'user', "
                    "and 'password' - Redshift has no ADC-equivalent ambient identity to "
                    "fall back to (see backends/redshift.py's module docstring).",
                    name,
                )
                continue
            port = entry.get("port") or 5439
            schema = (entry.get("schema") or "").strip()
            preset = {
                "id": preset_id,
                "name": name,
                "type": "redshift",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _redshift_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                "url": f"redshift://{host}:{port}/{database}",
                "host": host,
                "port": port,
                "database": database,
                "user": rs_user,
                "password": password,
            }
            if schema:
                preset["schema"] = schema
            CONFIGURED_DBS.append(preset)

        elif db_type == "mssql":
            host = (entry.get("host") or "").strip()
            database = (entry.get("database") or "").strip()
            ms_user = (entry.get("user") or "").strip()
            password = entry.get("password") or ""
            if not (host and database and ms_user and password):
                logger.warning(
                    "Skipping SQL Server preset '%s': requires 'host', 'database', 'user', "
                    "and 'password' - SQL Server has no ADC-equivalent ambient identity to "
                    "fall back to (see backends/mssql.py's module docstring).",
                    name,
                )
                continue
            port = entry.get("port") or 1433
            schema = (entry.get("schema") or "").strip()
            preset = {
                "id": preset_id,
                "name": name,
                "type": "mssql",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _mssql_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                "url": f"mssql://{host}:{port}/{database}",
                "host": host,
                "port": port,
                "database": database,
                "user": ms_user,
                "password": password,
            }
            if schema:
                preset["schema"] = schema
            # "encrypt" defaults to True inside backends/mssql.py's connect()
            # when the key is absent entirely - so only set it here when the
            # preset entry actually specifies a value, letting an explicit
            # "encrypt": false opt out without this layer inventing its own
            # separate default (mirrors config_routes.py's parsing branch).
            if "encrypt" in entry:
                preset["encrypt"] = bool(entry.get("encrypt"))
            CONFIGURED_DBS.append(preset)

        elif db_type == "sheets":
            spreadsheet_url_or_id = (
                entry.get("spreadsheet_url") or entry.get("spreadsheet_id") or ""
            ).strip()
            tab_name = (entry.get("tab_name") or "").strip()
            if not (spreadsheet_url_or_id and tab_name):
                logger.warning(
                    "Skipping Google Sheets preset '%s': requires 'spreadsheet_url' "
                    "(or 'spreadsheet_id') and 'tab_name' - and the sheet must be "
                    "shared as \"Anyone with the link can view\", since this app has "
                    "no Google identity of its own (see backends/sheets.py's module "
                    "docstring).",
                    name,
                )
                continue
            spreadsheet_id = extract_spreadsheet_id(spreadsheet_url_or_id)
            if not spreadsheet_id:
                logger.warning(
                    "Skipping Google Sheets preset '%s': couldn't parse a spreadsheet "
                    "id out of 'spreadsheet_url'/'spreadsheet_id'.",
                    name,
                )
                continue
            sheets_preset = {
                "id": preset_id,
                "name": name,
                "type": "sheets",
                # Synthetic, display-only identifier for this preset - unlike
                # config_routes.py's CUSTOM-connection _sheets_identity (which is
                # purely internal and never stored as a url any more - see
                # that module's docstring), an admin-configured preset like this
                # one is out of scope for that change and still carries its own
                # synthetic url, built independently here rather than imported
                # (config_routes.py imports FROM this module - see the comment
                # above DATABASE_PRESETS_FILE).
                # extract_spreadsheet_id itself IS imported (from
                # sheets_util, not backends.sheets) since it's not a
                # one-liner worth tripling - see sheets_util.py's docstring
                # for why it lives in its own dependency-free module rather
                # than in backends/sheets.py.
                "url": f"sheets://{spreadsheet_id}/{tab_name}",
                "spreadsheet_id": spreadsheet_id,
                "tab_name": tab_name,
            }
            # Optional - present only for a preset the admin explicitly
            # wants pointed at a private spreadsheet (see the comment above
            # DATABASE_PRESETS_FILE). Pasted in verbatim, same as a
            # Snowflake preset's own "password" field above - no resolver
            # needed since presets aren't user-editable/re-saved.
            credentials_json = (entry.get("credentials_json") or "").strip()
            if credentials_json:
                sheets_preset["credentials_json"] = credentials_json
            CONFIGURED_DBS.append(sheets_preset)

        else:
            logger.warning("Skipping database preset '%s': unsupported type %r.", name, db_type)


# Ensure at least one default fallback exists
if not CONFIGURED_DBS:
    CONFIGURED_DBS = [{"id": "postgres+Default DB", "name": "Default DB", "type": "postgres", "url": DEFAULT_CONN}]

# First *Postgres* preset is the default fallback connection - db.py's
# resolve_active_descriptor (a blank/unresolvable session connection_id)
# and this module's own DEFAULT_CONN both assume a plain Postgres URL, so
# this must stay Postgres-typed even if a BigQuery preset happens to be
# listed first in CONFIGURED_DBS. Falls back to the original env-derived
# DEFAULT_CONN if no Postgres preset exists at all (e.g. a deployment
# configured with only BigQuery presets).
_postgres_presets = [db for db in CONFIGURED_DBS if db.get("type") == "postgres"]
if _postgres_presets:
    DEFAULT_CONN = _postgres_presets[0]["url"]

# The preset "id" (see this file's DATABASE_PRESETS_FILE comment) that
# DEFAULT_CONN above actually came from, or None if no Postgres preset
# exists (DEFAULT_CONN is then the hardcoded fallback string, which never
# matches any preset in CONFIGURED_DBS at all). This is what lets a brand-
# new session (connection_id == "", nothing ever explicitly selected)
# report the SAME preset as "active" in the config modal that its
# connection actually resolves to - see db.py's resolve_active_descriptor
# and config_routes.py's handle_config, both of which fall back to this
# id, not to a bare "nothing selected" null, whenever connection_id is
# blank.
DEFAULT_PRESET_ID = _postgres_presets[0]["id"] if _postgres_presets else None

# Multi-database question-answering (see translate_routes.py's module
# docstring): the ONE cap on how many database connections are involved in
# a single question's response, at every stage that concept comes up -
# how many a user may mark "in scope" at once (config_routes.py's POST
# validation of in_scope_preset_ids/in_scope_custom_connection_keys) AND
# how many of those in-scope connections "all databases" mode's triage may
# ever select for one response (connection_router.py's
# triage_all_mode_question, clamped to this regardless of what the model
# returns). These used to be two separate constants
# (MAX_IN_SCOPE_CONNECTIONS and connection_router.py's own
# MAX_DATABASES_PER_QUERY, defaulting to 5) on the theory that "how many
# databases could a question ever pick from" and "how many can one
# question actually use" were different concerns worth tuning
# independently - in practice that was just two knobs to remember for one
# mental model ("how many databases," full stop), so this is now the
# single source of truth for both. Lives here, not in config_routes.py
# (where it originated) or connection_router.py, specifically so both
# modules can import the same constant without a circular import:
# config_routes.py already imports FROM translate_routes.py, which
# imports FROM connection_router.py - a chain that would make
# connection_router.py importing back from config_routes.py circular.
# app_config.py sits underneath all three, imported by each, imported by
# none of them.
MAX_IN_SCOPE_CONNECTIONS = int(os.environ.get("MAX_IN_SCOPE_CONNECTIONS", 20))

# Shared LLM-call retry policy - originated in translate_routes.py (its
# single-connection retry loop and generate_sql_for_connection both use
# these), moved here for the exact same reason MAX_IN_SCOPE_CONNECTIONS
# just above did: connection_router.py's triage_all_mode_question now
# needs its own real retry policy too (see that function's docstring),
# and translate_routes.py already imports FROM connection_router.py, so
# the reverse import would be circular. app_config.py sits underneath
# both, imported by each, imported by neither.
MAX_TRANSLATION_ATTEMPTS = int(os.environ.get("MAX_TRANSLATION_ATTEMPTS", 5))
TRANSLATION_RETRY_DELAY_SECONDS = float(os.environ.get("TRANSLATION_RETRY_DELAY_SECONDS", 1))

# --- Issue reporting ("Report Error"/"Report Wrong Result", see
# report_routes.py) ----------------------------------------------------------
# Lets a user flag either a raw/uncategorized error execute_routes.py
# returned verbatim (see that module's docstring - typically a database
# driver's own cryptic message for SQL the model generated incorrectly) or
# a "wrong result" (a successful response - a table, a plain-text reply, an
# all-databases summarization - the user believes is wrong or misleading).
# Deliberately NOT for LLM system errors (translate_routes.py's
# format_llm_error_for_user() output) - those already carry an app-authored,
# human-readable explanation, so there's nothing new for a report of one to
# tell a reviewer that str(exc) itself wouldn't already have.
#
# Reports are emailed out via real SMTP, sent by this app itself
# (report_routes.py) rather than a mailto: link, so reporting never depends
# on the user having a local mail client configured - the user still always
# reviews the exact email content client-side before Send is ever clicked
# (see webClient/index.html's #reportIssueModal).
#
# ISSUE_REPORT_TO_EMAIL is the one field a deployer is REQUIRED to set for
# the feature to activate at all. Left unset (the default - same "opt-in,
# nothing loads silently" posture as DATABASE_PRESETS_FILE above), the
# feature stays fully inert: ISSUE_REPORTING_ENABLED below is False,
# config_routes.py's 'issue_reporting_enabled' field reports that to the
# client, and client.js never shows a Report button to any user at all.
ISSUE_REPORT_TO_EMAIL = os.environ.get("ISSUE_REPORT_TO_EMAIL", "").strip()

# The outbound SMTP connection this app authenticates with to actually send
# that email - no default host, since (unlike, say, a corporate LAN) there's
# no "ambient" mail relay to assume inside a Cloud Run/Docker environment.
# Port defaults to 587 (STARTTLS) - the common submission port for both
# self-hosted mail servers and every major provider (Gmail, SendGrid, SES,
# etc.). Username/password are optional (some internal relays allow
# anonymous submission from trusted IPs) - report_routes.py only calls
# smtp.login() when a username is actually configured.
ISSUE_REPORT_SMTP_HOST = os.environ.get("ISSUE_REPORT_SMTP_HOST", "").strip()
ISSUE_REPORT_SMTP_PORT = int(os.environ.get("ISSUE_REPORT_SMTP_PORT", "587"))
ISSUE_REPORT_SMTP_USERNAME = os.environ.get("ISSUE_REPORT_SMTP_USERNAME", "").strip()
ISSUE_REPORT_SMTP_PASSWORD = os.environ.get("ISSUE_REPORT_SMTP_PASSWORD", "")
# Envelope/header From address - falls back to the SMTP username (the
# common case: the account authenticated as IS the address mail is sent
# from) so a deployer using a provider like that doesn't need to set this
# separately, but can still override it explicitly (e.g. a provider where
# the SMTP username is an opaque API-key-shaped string, not a real mailbox
# address to send FROM).
ISSUE_REPORT_SMTP_FROM = os.environ.get("ISSUE_REPORT_SMTP_FROM", "").strip() or ISSUE_REPORT_SMTP_USERNAME
# STARTTLS (upgrade a plaintext connection) is the default and what port
# 587 expects; set this to "0" only for a relay that expects a plaintext/
# already-implicit-TLS connection instead (report_routes.py does not
# separately implement implicit TLS via smtplib.SMTP_SSL - pair "0" with a
# relay/port combination that doesn't require it).
ISSUE_REPORT_SMTP_USE_TLS = os.environ.get("ISSUE_REPORT_SMTP_USE_TLS", "1") != "0"

# The one thing config_routes.py's GET /api/config actually needs to expose
# to the client - whether the feature is configured at ALL, never any of
# the credential fields above. Requires a recipient AND enough to actually
# open a connection and identify a sender (host + a from-address);
# username/password are intentionally not required here (see the comment
# above ISSUE_REPORT_SMTP_USERNAME).
ISSUE_REPORTING_ENABLED = bool(
    ISSUE_REPORT_TO_EMAIL and ISSUE_REPORT_SMTP_HOST and ISSUE_REPORT_SMTP_FROM
)

# Model configuration for all three LLM providers (Google included) lives
# in translate_routes.py now, not here - each provider's own single
# *_MODELS env var (GOOGLE_MODELS/ANTHROPIC_MODELS/OPENAI_MODELS) is the
# full list the model-selection modal offers for that provider (see
# LlmProvider.preset_models in that module), and a separate, single
# DEFAULT_MODEL env var (also read in translate_routes.py - see
# LlmProvider.default_model/get_llm_provider()) picks which one model is
# actually used by default, across all three providers, instead of
# whichever *_MODELS list's first entry always winning. This used to be a
# Gemini-only DEFAULT_MODEL/PRESET_MODELS pair here - Gemini being the
# original/only provider before the multi-provider refactor - with
# PRESET_MODELS hardcoded and never actually read from any env var despite
# older docs claiming it was, and DEFAULT_MODEL itself never wired into
# anything the client used either. Both names have since been reintroduced
# properly in translate_routes.py: PRESET_MODELS as each provider's own
# *_MODELS var, DEFAULT_MODEL as the one fleet-wide, cross-provider model
# override described above - this is not the same variable as the old
# Gemini-only one, just the same name reused for a design that actually
# works now.

# --- State DB file in local mode ---------------------------------------------
TRANSLATION_STATS_DB_PATH = "state/ydyl_state.db"

# --- State Store: Firestore on Cloud Run, SQLite locally ---------------------
if firestore_client:
    state_store = FirestoreStateStore(firestore_client)
else:
    state_store = SqliteStateStore(TRANSLATION_STATS_DB_PATH)