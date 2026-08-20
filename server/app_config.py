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

import json
import logging
import os

from flask import Flask
from flask_cors import CORS
from google.cloud import firestore

from state_store import SqliteStateStore, FirestoreStateStore

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

# --- Flask app ---------------------------------------------------------------
app = Flask(__name__, static_folder='../webClient', static_url_path='')
# Support credentials for authenticated CORS requests
CORS(app, supports_credentials=True)

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
# DATABASE_PRESETS_FILE at its path. Every object needs "type" and "name";
# the rest of the shape is dialect-specific:
#   Postgres:  {"type": "postgres", "name": "...", "url": "postgresql://..."}
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

    for entry in parsed_presets:
        if not isinstance(entry, dict):
            logger.warning("Skipping database preset entry that is not a JSON object: %r", entry)
            continue

        db_type = (entry.get("type") or "postgres").strip().lower()
        name = (entry.get("name") or "").strip()
        if not name:
            logger.warning("Skipping database preset entry with no 'name': %r", entry)
            continue

        if db_type == "postgres":
            url = (entry.get("url") or "").strip()
            if not url:
                logger.warning("Skipping Postgres preset '%s': missing 'url'.", name)
                continue
            CONFIGURED_DBS.append({"name": name, "type": "postgres", "url": url})

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
            CONFIGURED_DBS.append({"name": name, "type": "mysql", "url": url})

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
                "name": name,
                "type": "snowflake",
                # Synthetic identifier, not a credential - mirrors
                # config_routes.py's _snowflake_url (duplicated here rather
                # than imported, since config_routes.py imports FROM this
                # module - see the comment above DATABASE_PRESETS_FILE).
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
                "name": name,
                "type": "databricks",
                # Synthetic identifier, not a credential - mirrors
                # config_routes.py's _databricks_url (duplicated here rather
                # than imported, since config_routes.py imports FROM this
                # module - see the comment above DATABASE_PRESETS_FILE).
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
                "name": name,
                "type": "oracle",
                # Synthetic identifier, not a credential - mirrors
                # config_routes.py's _oracle_url (duplicated here rather
                # than imported, since config_routes.py imports FROM this
                # module - see the comment above DATABASE_PRESETS_FILE).
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
                "name": name,
                "type": "redshift",
                # Synthetic identifier, not a credential - mirrors
                # config_routes.py's _redshift_url (duplicated here rather
                # than imported, since config_routes.py imports FROM this
                # module - see the comment above DATABASE_PRESETS_FILE).
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

        else:
            logger.warning("Skipping database preset '%s': unsupported type %r.", name, db_type)


# Ensure at least one default fallback exists
if not CONFIGURED_DBS:
    CONFIGURED_DBS = [{"name": "Default DB", "type": "postgres", "url": DEFAULT_CONN}]

# First *Postgres* preset is the default fallback connection - state_store's
# "no session row yet" fallback and this module's own DEFAULT_CONN both
# assume a plain Postgres URL, so this must stay Postgres-typed even if a
# BigQuery preset happens to be listed first in CONFIGURED_DBS. Falls back
# to the original env-derived DEFAULT_CONN if no Postgres preset exists at
# all (e.g. a deployment configured with only BigQuery presets).
_postgres_presets = [db for db in CONFIGURED_DBS if db.get("type") == "postgres"]
if _postgres_presets:
    DEFAULT_CONN = _postgres_presets[0]["url"]

# --- Model Configuration -----------------------------------------------------
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
PRESET_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]
if DEFAULT_MODEL not in PRESET_MODELS:
    PRESET_MODELS.insert(0, DEFAULT_MODEL)

# --- State DB file in local mode ---------------------------------------------
TRANSLATION_STATS_DB_PATH = "state/ydyl_state.db"

# --- State Store: Firestore on Cloud Run, SQLite locally ---------------------
if firestore_client:
    state_store = FirestoreStateStore(firestore_client, DEFAULT_CONN)
else:
    state_store = SqliteStateStore(TRANSLATION_STATS_DB_PATH, DEFAULT_CONN)