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
# A single DATABASE_PRESETS env var holds a JSON array of preset-connection
# objects, one per preset - replacing the old positional CSV lists
# (DATABASE_NAMES/DATABASE_URLS for Postgres, BIGQUERY_NAMES/
# BIGQUERY_PROJECT_IDS/BIGQUERY_DATASETS for BigQuery), which were easy to
# misalign once there was more than one dialect. Every object needs "type"
# and "name"; the rest of the shape is dialect-specific:
#   Postgres: {"type": "postgres", "name": "...", "url": "postgresql://..."}
#   BigQuery: {"type": "bigquery", "name": "...", "project_id": "...", "dataset": "..."}
# Example:
#   DATABASE_PRESETS=[{"type":"postgres","name":"E-Commerce Store","url":"postgresql://demo:FooBar@127.0.0.1:5432/helloworld-vector-1785975353349"},{"type":"bigquery","name":"Analytics","project_id":"my-project","dataset":"analytics"}]
# Unlike Postgres presets (a connection string with embedded credentials),
# admin-preset BigQuery connections carry no credential at all: the app
# authenticates as its own Cloud Run service account (Application Default
# Credentials) - an admin who wants to preset a BigQuery connection is
# expected to have granted that service account the appropriate IAM role on
# the project/dataset. Per-user custom BigQuery connections (with their own
# pasted service-account key) are a separate, user-scoped concept handled in
# state_store.py/config_routes.py, not here.
raw_db_presets = os.environ.get("DATABASE_PRESETS", "")

CONFIGURED_DBS = []
if raw_db_presets.strip():
    try:
        parsed_presets = json.loads(raw_db_presets)
        if not isinstance(parsed_presets, list):
            raise ValueError("DATABASE_PRESETS must be a JSON array")
    except (ValueError, TypeError) as e:
        logger.error("Failed to parse DATABASE_PRESETS, ignoring it entirely: %s", e)
        parsed_presets = []

    for entry in parsed_presets:
        if not isinstance(entry, dict):
            logger.warning("Skipping DATABASE_PRESETS entry that is not a JSON object: %r", entry)
            continue

        db_type = (entry.get("type") or "postgres").strip().lower()
        name = (entry.get("name") or "").strip()
        if not name:
            logger.warning("Skipping DATABASE_PRESETS entry with no 'name': %r", entry)
            continue

        if db_type == "postgres":
            url = (entry.get("url") or "").strip()
            if not url:
                logger.warning("Skipping Postgres preset '%s': missing 'url'.", name)
                continue
            CONFIGURED_DBS.append({"name": name, "type": "postgres", "url": url})

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
            # via this app's own ambient identity (ADC - ADC - the Cloud Run
            # service account, or local `gcloud auth application-default
            # login`), which lives in GCP_PROJECT_ID, so that's the sensible
            # default; an admin can still override per-preset (e.g. to bill
            # a dedicated BigQuery-only project) via an explicit
            # "billing_project_id" in this preset's DATABASE_PRESETS entry.
            # See backends/bigquery.py's module docstring for why conflating
            # the two causes a "does not have bigquery.jobs.create
            # permission" 403 the moment project_id points at data you don't
            # own.
            billing_project_id = (entry.get("billing_project_id") or "").strip() or GCP_PROJECT_ID or project_id
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

        else:
            logger.warning("Skipping DATABASE_PRESETS entry '%s': unsupported type %r.", name, db_type)


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