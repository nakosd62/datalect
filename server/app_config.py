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

import logging
import os

from flask import Flask
from flask_cors import CORS
from google.cloud import firestore

from state_store import SqliteStateStore, FirestoreStateStore

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

DEFAULT_CONN = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:password@host:23456/defaultdb?sslmode=verify-full"
)

# Parse pre-configured DB names and URLs from env variables
raw_db_names = os.environ.get("DATABASE_NAMES", "Default DB")
raw_db_urls = os.environ.get("DATABASE_URLS", DEFAULT_CONN)

PRESET_DB_NAMES = [n.strip() for n in raw_db_names.split(",") if n.strip()]
PRESET_DB_URLS = [u.strip() for u in raw_db_urls.split(",") if u.strip()]

# Pair positional name and address
CONFIGURED_DBS = []
for idx, name in enumerate(PRESET_DB_NAMES):
    url = PRESET_DB_URLS[idx] if idx < len(PRESET_DB_URLS) else DEFAULT_CONN
    CONFIGURED_DBS.append({"name": name, "url": url})

# Ensure at least one default fallback exists
if not CONFIGURED_DBS:
    CONFIGURED_DBS = [{"name": "Default DB", "url": DEFAULT_CONN}]

# First name/address pair is default
DEFAULT_CONN = CONFIGURED_DBS[0]["url"]

# --- Model Configuration -----------------------------------------------------
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
raw_preset_models = os.environ.get("GEMINI_PRESET_MODELS", "gemini-2.5-flash,gemini-2.5-pro")
PRESET_MODELS = [m.strip() for m in raw_preset_models.split(",") if m.strip()]
if DEFAULT_MODEL not in PRESET_MODELS:
    PRESET_MODELS.insert(0, DEFAULT_MODEL)

# --- State DB file in local mode ---------------------------------------------
TRANSLATION_STATS_DB_PATH = "state/ydyl_state.db"

# --- State Store: Firestore on Cloud Run, SQLite locally ---------------------
if firestore_client:
    state_store = FirestoreStateStore(firestore_client, DEFAULT_CONN, DEFAULT_MODEL)
else:
    state_store = SqliteStateStore(TRANSLATION_STATS_DB_PATH, DEFAULT_CONN, DEFAULT_MODEL)