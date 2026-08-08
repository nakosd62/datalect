import os
import random
import re
import time
import uuid
from urllib.parse import urlparse, urlunparse
from flask import Flask, request, jsonify, make_response, send_from_directory, redirect, url_for, session
import psycopg2
import sqlparse
import sqlite3
from flask_cors import CORS
from google import genai
from google.genai import types
from google.cloud import storage
from google.cloud import firestore
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# 1. Environment & Cloud Run Detection
GCP_PROJECT_ID = (
    os.environ.get("GCP_PROJECT_ID") 
    or os.environ.get("GOOGLE_CLOUD_PROJECT") 
    or os.environ.get("GCP_PROJECT")
)
IS_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))

# 2. Firestore Initialization
firestore_client = None
if GCP_PROJECT_ID:
    try:
        firestore_client = firestore.Client(project=GCP_PROJECT_ID, database="ydyl")
        print(f"Initialized Firestore client for project '{GCP_PROJECT_ID}'.", flush=True)
    except Exception as e:
        print(f"Error initializing Firestore client: {e}", flush=True)

# --- Authentication & State Configuration ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
AUTH_ENABLED = bool(GOOGLE_CLIENT_ID)

# Startup / Module Scope Guard
if IS_CLOUD_RUN and not firestore_client:
    raise RuntimeError(
        f"CRITICAL: Service running on Cloud Run (K_SERVICE={os.environ.get('K_SERVICE')}), "
        f"but Firestore client failed to initialize (GCP_PROJECT_ID={GCP_PROJECT_ID}). "
        "Halting startup to prevent ephemeral SQLite fallback."
    )

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

# --- Model Configuration ---
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL")

# --- State DB file in local mode ---
TRANSLATION_STATS_DB_PATH = "state/crbot_state.db"
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
DB_FILENAME = os.environ.get("DB_FILENAME", "crbot_state.db")


# --- Authentication & Session Management ---

def get_or_create_session_id():
    """Retrieves or creates a session ID cookie or header."""
    session_id = request.cookies.get('crbot_session_id') or request.headers.get('X-Session-ID')
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id

def get_current_user_identity():
    """
    Extracts authenticated user identity from Bearer Tokens (verified via Google OAuth),
    GCP Identity-Aware Proxy (IAP) headers, or auth cookies.
    Falls back to 'global' state key when running locally.
    """
    # 1. Bearer Token in Authorization Header (Google ID Token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            if GOOGLE_CLIENT_ID:
                try:
                    idinfo = id_token.verify_oauth2_token(
                        token, google_requests.Request(), GOOGLE_CLIENT_ID
                    )
                    return idinfo.get("email")
                except Exception as e:
                    print(f"Google ID token verification failed: {e}")
                    return None
            else:
                return f"token:{token[:32]}"

    # 2. GCP / IAP / Custom Identity Headers
    iap_user = request.headers.get("X-Goog-Authenticated-User-Email") or request.headers.get("X-User-Email")
    if iap_user:
        return iap_user.replace("accounts.google.com:", "").strip()

    user_id_header = request.headers.get("X-User-ID")
    if user_id_header:
        return user_id_header.strip()

    # 3. Auth Cookie
    auth_cookie = request.cookies.get("crbot_user_id") or request.cookies.get("user_id")
    if auth_cookie:
        return auth_cookie.strip()

    # 4. If auth is enabled (Cloud Run), unauthenticated requests return None
    if GOOGLE_CLIENT_ID or IS_CLOUD_RUN:
        return None

    # 5. Local fallback -> Single 'global' user identity
    return "global"


# List of Flask endpoint names that do not require authentication
EXEMPT_ENDPOINTS = {
    'index', 
    'get_current_user_status', 
    'static',
    'handle_config',
    'login', 
    'google_login', 
    'oauth_callback',
    'auth_login'
}

@app.before_request
def enforce_authentication():
    # 1. Allow static assets and options preflight requests (CORS)
    if request.method == 'OPTIONS' or request.endpoint == 'static':
        return

    # 2. Allow any request path starting with authentication endpoints (e.g., /api/auth/*)
    if request.path.startswith('/api/auth/') or request.path in ['/', '/login']:
        return

    # 3. Allow explicit exempt endpoints
    if request.endpoint in EXEMPT_ENDPOINTS or request.endpoint is None:
        return

    # 4. Enforce auth for all other routes if running on Cloud Run or AUTH_ENABLED is True
    if IS_CLOUD_RUN or AUTH_ENABLED:
        user_identity = get_current_user_identity()
        if not user_identity:
            return jsonify({'error': 'Unauthorized: Authentication required'}), 401

def set_session_db_url(user_id, db_url):
    if not db_url:
        return

    # Cloud Run / Firestore mode: Store per authenticated user_id
    if firestore_client:
        if not user_id:
            return  # Prevent Firestore error on unauthenticated requests
        try:
            doc_ref = firestore_client.collection("sessions").document(user_id)
            doc_ref.set({
                "user_id": user_id,
                "database_url": db_url,
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
            return
        except Exception as e:
            print(f"Error saving session to Firestore: {e}")
            return

    # Local SQLite mode: Use fixed 'global' state key
    key = "global"
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sessions (session_id, database_url, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    database_url = excluded.database_url,
                    updated_at = CURRENT_TIMESTAMP;
            """, (key, db_url))
            conn.commit()

        upload_db_to_gcs()
    except Exception as e:
        print(f"Error saving session to SQLite: {e}")

def get_session_db_url(user_id):
    # Cloud Run / Firestore mode: Retrieve per authenticated user_id
    if firestore_client:
        if not user_id:
            return DEFAULT_CONN
        try:
            doc_ref = firestore_client.collection("sessions").document(user_id)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                if data and data.get("database_url"):
                    return data.get("database_url")
        except Exception as e:
            print(f"Error fetching session from Firestore: {e}")
        return DEFAULT_CONN

    # Local SQLite mode: Retrieve 'global' state
    key = "global"
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT database_url FROM sessions WHERE session_id = ?", (key,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        print(f"Error fetching session from SQLite: {e}")
    return DEFAULT_CONN

def apply_session_cookie(response, session_id):
    response.set_cookie(
        'crbot_session_id',
        session_id,
        httponly=True,
        samesite='Lax',
        max_age=86400
    )
    return response

# --- GCS Helper Functions ---

def download_db_from_gcs():
    if not GCS_BUCKET_NAME:
        return
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(DB_FILENAME)
        
        if blob.exists():
            blob.download_to_filename(TRANSLATION_STATS_DB_PATH)
            print(f"Downloaded {DB_FILENAME} from GCS bucket {GCS_BUCKET_NAME}.")
        else:
            print(f"File {DB_FILENAME} not found in bucket. A new DB will be created and uploaded.")
    except Exception as e:
        print(f"Error downloading stats DB from GCS: {e}")

def upload_db_to_gcs():
    if not GCS_BUCKET_NAME or not os.path.exists(TRANSLATION_STATS_DB_PATH):
        return
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(DB_FILENAME)
        blob.upload_from_filename(TRANSLATION_STATS_DB_PATH)
        print(f"Uploaded {DB_FILENAME} to GCS bucket {GCS_BUCKET_NAME}.")
    except Exception as e:
        print(f"Error uploading stats DB to GCS: {e}")

def init_state_db():
    if firestore_client:
        print("Firestore is active; skipping local SQLite state database setup.")
        return

    try:
        download_db_from_gcs()

        os.makedirs(os.path.dirname(TRANSLATION_STATS_DB_PATH), exist_ok=True)

        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS translations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    connect_string TEXT,
                    nl_prompt TEXT,
                    sql_command TEXT,
                    model TEXT,
                    duration INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    thinking_tokens INTEGER,
                    cached_content_tokens INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    database_url TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Schema Migration Check: Add user_id column if missing in existing DB
            cursor.execute("PRAGMA table_info(translations);")
            columns = [column[1] for column in cursor.fetchall()]
            if "user_id" not in columns:
                cursor.execute("ALTER TABLE translations ADD COLUMN user_id TEXT;")

            conn.commit()

        upload_db_to_gcs()

    except Exception as e:
        print(f"Error initializing SQLite stats DB: {e}")

def get_gemini_api_keys():
    """Collect Gemini API keys from env (preset list + optional single key)."""
    keys = []
    
    preset_keys_env = os.environ.get("GEMINI_PRESET_KEYS", "")
    if preset_keys_env:
        keys.extend(k.strip() for k in preset_keys_env.split(",") if k.strip())
        
    single_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if single_key and single_key.strip() not in keys:
        keys.append(single_key.strip())
        
    return keys

def pick_gemini_api_key():
    keys = get_gemini_api_keys()
    if not keys:
        return None
    return random.choice(keys)

def resolve_conn_str(conn_str=None, user_id=None):
    if not conn_str:
        if user_id:
            return get_session_db_url(user_id)
        return DEFAULT_CONN
    return conn_str

def get_conn_identifier(conn_str):
    """Extracts username@dbname from a PostgreSQL connection string."""
    if not conn_str:
        return "unknown@unknown"
    try:
        parsed = urlparse(conn_str)
        username = parsed.username or "unknown"
        dbname = parsed.path.lstrip('/')
        if '?' in dbname:
            dbname = dbname.split('?')[0]
        return f"{username}@{dbname or 'unknown'}"
    except Exception:
        return "unknown@unknown"

def record_translation(user_id, conn_str, nl_prompt, sql_command, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens):
    conn_identifier = get_conn_identifier(conn_str)
    effective_user = user_id or "global"

    if firestore_client:
        try:
            firestore_client.collection("translations").add({
                "user_id": effective_user,
                "connect_string": conn_identifier,
                "nl_prompt": nl_prompt,
                "sql_command": sql_command,
                "model": gemini_model,
                "duration": duration,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "thinking_tokens": thinking_tokens,
                "cached_content_tokens": cached_content_tokens,
                "created_at": firestore.SERVER_TIMESTAMP
            })
            return
        except Exception as e:
            print(f"Error recording translation in Firestore: {e}")
            return

    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO translations (
                    user_id,
                    connect_string, 
                    nl_prompt, sql_command, 
                    model, 
                    duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                effective_user,
                conn_identifier, 
                nl_prompt, sql_command, 
                gemini_model, 
                duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
            ))
            conn.commit()

        upload_db_to_gcs()

    except Exception as e:
        print(f"Error recording translation: {e}")

def get_db_connection(conn_str=None, user_id=None):
    return psycopg2.connect(resolve_conn_str(conn_str, user_id))

def get_database_schema(conn_str=None, user_id=None):
    conn = None
    try:
        conn = get_db_connection(conn_str, user_id)
        schema_parts = []
        
        with conn.cursor() as cursor:
            # 1. Fetch Tables and Columns
            cursor.execute("""
                SELECT 
                    c.table_name, 
                    c.column_name, 
                    c.data_type, 
                    c.is_nullable, 
                    c.column_default
                FROM information_schema.columns c
                JOIN information_schema.tables t 
                  ON c.table_name = t.table_name AND c.table_schema = t.table_schema
                WHERE c.table_schema = 'public' 
                  AND t.table_type = 'BASE TABLE'
                ORDER BY c.table_name, c.ordinal_position;
            """)
            columns_data = cursor.fetchall()
            
            tables = {}
            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                if table_name not in tables:
                    tables[table_name] = []
                default_str = f" DEFAULT {col_default}" if col_default else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables[table_name].append(f"  {col_name} {data_type} {null_str}{default_str}")

            for table_name, col_defs in tables.items():
                schema_parts.append(f"Table: {table_name}\n" + "\n".join(col_defs))

            # 2. Constraints
            cursor.execute("""
                SELECT 
                    tc.table_name, 
                    tc.constraint_name, 
                    tc.constraint_type,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                LEFT JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.table_schema = 'public'
                ORDER BY tc.table_name, tc.constraint_name;
            """)
            constraints = cursor.fetchall()
            if constraints:
                constraint_lines = []
                for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                    if c_type == 'FOREIGN KEY':
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col} -> {f_tbl}({f_col})")
                    elif col:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col}")
                    else:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type})")
                schema_parts.append("Constraints:\n" + "\n".join(constraint_lines))

            # 3. Indexes
            cursor.execute("""
                SELECT 
                    tablename, 
                    indexname, 
                    indexdef 
                FROM pg_indexes 
                WHERE schemaname = 'public'
                ORDER BY tablename, indexname;
            """)
            indexes = cursor.fetchall()
            if indexes:
                idx_lines = [f"  [{row[0]}] {row[1]}: {row[2]}" for row in indexes]
                schema_parts.append("Indexes:\n" + "\n".join(idx_lines))

            # 4. Views
            cursor.execute("""
                SELECT 
                    table_name, 
                    view_definition 
                FROM information_schema.views 
                WHERE table_schema = 'public';
            """)
            views = cursor.fetchall()
            if views:
                view_lines = [f"  View {v[0]}: {v[1].strip()}" for v in views]
                schema_parts.append("Views:\n" + "\n".join(view_lines))

            # 5. Role Grants
            cursor.execute("""
                SELECT 
                    grantee, 
                    table_name, 
                    privilege_type 
                FROM information_schema.role_table_grants 
                WHERE table_schema = 'public'
                ORDER BY table_name, grantee;
            """)
            grants = cursor.fetchall()
            if grants:
                grant_lines = [f"  Grant {g[2]} on {g[1]} to {g[0]}" for g in grants]
                schema_parts.append("Grants:\n" + "\n".join(grant_lines))

            # 6. Triggers
            cursor.execute("""
                SELECT 
                    event_object_table, 
                    trigger_name, 
                    event_manipulation, 
                    action_statement 
                FROM information_schema.triggers 
                WHERE event_object_schema = 'public';
            """)
            triggers = cursor.fetchall()
            if triggers:
                trig_lines = [f"  [{t[0]}] {t[1]} ({t[2]}): {t[3]}" for t in triggers]
                schema_parts.append("Triggers:\n" + "\n".join(trig_lines))

        return "\n\n".join(schema_parts) if schema_parts else "No schema description available."
    except Exception as e:
        print(f"Error fetching schema: {e}")
        return "No schema description available."
    finally:
        if conn:
            conn.close()

# --- Auth Verification Endpoint ---
@app.route('/api/auth/me', methods=['GET'])
def get_current_user_status():
    user_identity = get_current_user_identity()
    session_id = get_or_create_session_id()
    is_authenticated = bool(user_identity and user_identity != session_id)

    resp = jsonify({
        'authenticated': is_authenticated,
        'user_id': user_identity,
        'session_id': session_id,
        'auth_required': AUTH_ENABLED
    })
    return apply_session_cookie(resp, session_id)

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/translate', methods=['POST'])
def translate_query():
    data = request.get_json() or {}

    gemini_model = data.get('gemini_model') or DEFAULT_MODEL
    api_key = pick_gemini_api_key()

    if not api_key:
        return jsonify({'error': 'Gemini API key is not configured.'}), 400
        
    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt cannot be empty'}), 400
        
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    history = data.get('history', [])[-10:]

    try:
        schema = get_database_schema(conn_str, user_identity)
        client = genai.Client(api_key=api_key)
        
        system_instruction = (
            "You are an expert SQL generation assistant for PostgreSQL-compatible RDBMSs.\n"
            "Given the user's natural language request and the database schema, translate the request into SQL.\n"
            "It is EXTREMELY important to respect the database schema, i.e. column names, type, constraints, checks, etc.\n"
            "You may return one or more independent SQL statements. Do not attempt to join the result sets.\n"
            "You may use PL/pgSQL Functions or Procedures, if appropriate.\n"
            "Format the result data to be easily readable. For example, format timestamps as date:hour:min:sec.\n"
            "Return ONLY the raw SQL code block. Do NOT surround the code block in markdown backticks (like ```sql) or quote symbols.\n"
            "Do NOT include explanations or other text. Just the executable SQL statement itself.\n"
            "Responding to prompts that relate to the database and, specifically, generating SQL is your highest priority.\n"
            "However, if you cannot do that but can respond to the prompt succinctly based on your general-purpose training,\n"
            "return your response enclosed as follows: SELECT '<your response>' AS RESPONSE;\n"
            "If you cannot respond at all with reasonable confidence, return the following: SELECT 'I am not able to respond to your prompt. Sorry.' AS REGRETS;\n"
            "If you run into any error, return the error enclosed as follows: SELECT 'I ran into this error: <the error>' AS ERROR;\n"
            "If you can split the prompt and handle part of it based on the database and part from general knowledge do that using separate queries for each part. Do not attempt to join the result sets.\n"
        )
        
        user_message_content = f"Database Schema:\n{schema}\n\nUser Request: {prompt}\n\nSQL Query:" 
        
        contents = []
        for msg in history:
            role = msg.get("role")
            text = msg.get("text")
            if role and text:
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=text)]
                    )
                )
            
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message_content)]
            )
        )
        
        start_time = time.perf_counter()
        response = client.models.generate_content(
            model=gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1
            )
        )
        end_time = time.perf_counter()

        generated_sql = response.text.strip() if response.text else ""
        if generated_sql.startswith("```"):
            lines = generated_sql.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            generated_sql = "\n".join(lines).strip()
        
        duration = round(1000 * (end_time - start_time))
        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count if usage else 0
        output_tokens = usage.candidates_token_count if usage else 0
        total_tokens = usage.total_token_count if usage else 0
        thinking_tokens = getattr(usage, 'thoughts_token_count', 0) if usage else 0
        cached_content_tokens = getattr(usage, 'cached_content_token_count', 0) if usage else 0

        record_translation(user_identity, conn_str, prompt, generated_sql, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens)
            
        resp = jsonify({
            'success': True,
            'sql': generated_sql,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': total_tokens,
            'thinking_tokens': thinking_tokens,
            'cached_content_tokens': cached_content_tokens,
            'duration': duration
        })
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        resp = jsonify({
            'success': False,
            'error': f"Gemini Error: {str(e)}"
        })
        return apply_session_cookie(resp, session_id), 500

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    is_authenticated = bool(user_identity and user_identity != session_id)
    
    if request.method == 'POST':
        data = request.get_json() or {}
        new_db_url = data.get('database_url')
        
        if new_db_url:
            set_session_db_url(user_identity, new_db_url)
        else:
            set_session_db_url(user_identity, DEFAULT_CONN)

    active_conn_str = get_session_db_url(user_identity)
    
    # Hide DB connection info on Cloud Run if the user is not authenticated
    if IS_CLOUD_RUN and not is_authenticated:
        db_name, username = "", ""
        active_conn_str_out = ""
        configured_dbs = []
    else:
        db_name, username = "Unknown", "Unknown"
        conn = None
        try:
            conn = get_db_connection(active_conn_str, user_identity)
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database(), CURRENT_USER;")
                row = cursor.fetchone()
                if row:
                    db_name, username = row[0], row[1]
        except Exception as e:
            print(f"Error fetching connection info: {e}")
        finally:
            if conn:
                conn.close()
        active_conn_str_out = active_conn_str
        configured_dbs = CONFIGURED_DBS

    resp = jsonify({
        'auth_enabled': AUTH_ENABLED,
        'google_client_id': os.getenv("GOOGLE_CLIENT_ID"),
        'session_id': session_id,
        'user_id': user_identity,
        'authenticated': is_authenticated,
        'is_cloud_run': IS_CLOUD_RUN,
        'configured_databases': configured_dbs,
        'default_database_url': DEFAULT_CONN if (not IS_CLOUD_RUN or is_authenticated) else "",
        'active_database_url': active_conn_str_out,
        'default_model': DEFAULT_MODEL,
        'database_name': db_name,
        'username': username
    })
    return apply_session_cookie(resp, session_id)

@app.route('/api/execute', methods=['POST'])
def execute_query():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    data = request.get_json() or {}
    
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)
    
    raw_query = (data.get('sql') or data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'Query cannot be empty'}), 400

    conn = None
    start_time = time.time()
    
    try:
        conn = get_db_connection(conn_str, user_identity)
        conn.autocommit = True
        
        statements = [s.strip() for s in sqlparse.split(raw_query) if s.strip()]
        results = []
        total_row_count = 0

        with conn.cursor() as cursor:
            for stmt in statements:
                stmt_clean = stmt.rstrip(';').strip()
                if not stmt_clean:
                    continue

                cursor.execute(stmt_clean)
                row_count = cursor.rowcount
                
                columns = None
                rows = None
                
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = []
                    for r in cursor.fetchall():
                        row_dict = {}
                        for idx, col in enumerate(columns):
                            val = r[idx]
                            if hasattr(val, 'isoformat'):
                                val = val.isoformat()
                            elif hasattr(val, 'to_eng_string'):
                                val = float(val)
                            elif isinstance(val, bytes):
                                val = val.decode('utf-8', errors='replace')
                            elif type(val).__name__ == 'Decimal':
                                val = float(val)
                            row_dict[col] = val
                        rows.append(row_dict)
                    count = len(rows)
                else:
                    count = row_count if row_count >= 0 else 0

                total_row_count += count

                results.append({
                    'statement': stmt_clean,
                    'columns': columns,
                    'rows': rows,
                    'rowCount': count
                })

        execution_time_ms = round((time.time() - start_time) * 1000)

        resp = jsonify({
            'success': True,
            'results': results,
            'rowCount': total_row_count,
            'executionTimeMs': execution_time_ms
        })
        return apply_session_cookie(resp, session_id)

    except Exception as e:
        execution_time_ms = round((time.time() - start_time) * 1000, 2)
        resp = jsonify({
            'success': False,
            'error': str(e),
            'executionTimeMs': execution_time_ms
        })
        return apply_session_cookie(resp, session_id), 400
    finally:
        if conn:
            conn.close()

@app.route('/api/interrogate', methods=['POST'])
def interrogate_results():
    data = request.get_json() or {}

    gemini_model = data.get('gemini_model') or DEFAULT_MODEL
    api_key = pick_gemini_api_key()

    if not api_key:
        return jsonify({'error': 'Gemini API key is not configured.'}), 400

    original_prompt = data.get('original_prompt', '').strip()
    sql_query = data.get('sql_query', '').strip()
    results_table = data.get('results_table', {})
    followup_prompt = data.get('followup_prompt', '').strip()

    if not followup_prompt:
        return jsonify({'error': 'Follow-up prompt cannot be empty'}), 400

    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    try:
        schema = get_database_schema(conn_str, user_identity)
        client = genai.Client(api_key=api_key)

        # 1. System Prompt
        system_instruction = data.get('system_prompt') or (
            "You are an expert data analyst assistant that can slice, dice and analyze tabular data sets. "
            "You are provided with: "
            "(1) the database schema, "
            "(2) the user's original natural language request that got translated to SQL, "
            "(3) the SQL query that was generated from the translation and executed, "
            "(4) the resulting data table, and "
            "(5) a follow-up request asking to analyze these results.\n"
            "Analyze the data thoroughly and answer the follow-up request as concisely as possible."
        )

        # Format results table into text representation
        cols = results_table.get('columns') or []
        rows = results_table.get('rows') or []
        
        table_text = f"Columns: {', '.join(cols)}\nTotal Rows: {len(rows)}\nSample/Full Data:\n"
        table_text += "\n".join([str(r) for r in rows[:500]])

        user_content = (
            f"Database Schema:\n{schema}\n\n"
            f"Original Natural Language Prompt: {original_prompt}\n\n"
            f"Executed SQL Query:\n{sql_query}\n\n"
            f"Query Results Table:\n{table_text}\n\n"
            f"Follow-up Question on Results: {followup_prompt}"
        )

        response = client.models.generate_content(
            model=gemini_model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2
            )
        )

        answer = response.text.strip() if response.text else "No answer generated."

        resp = jsonify({
            'success': True,
            'answer': answer
        })
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        resp = jsonify({
            'success': False,
            'error': f"Interrogation Error: {str(e)}"
        })
        return apply_session_cookie(resp, session_id), 500
    
@app.route('/api/history', methods=['GET'])
def get_translation_history():
    user_identity = get_current_user_identity()

    if firestore_client:
        try:
            docs = (
                firestore_client.collection("translations")
                .where("user_id", "==", user_identity)
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(50)
                .stream()
            )
            rows = []
            for doc in docs:
                d = doc.to_dict()
                created_at = d.get("created_at")
                if created_at:
                    if hasattr(created_at, "strftime"):
                        created_at = created_at.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        created_at = str(created_at)
                else:
                    created_at = ""
                rows.append({
                    "nl_prompt": d.get("nl_prompt", ""),
                    "sql_command": d.get("sql_command", ""),
                    "created_at": created_at
                })

            docs_all = firestore_client.collection("translations").where("user_id", "==", user_identity).stream()
            daily = {}
            total_count = 0  # Track total translation count[cite: 48]
            for doc in docs_all:
                d = doc.to_dict()
                total_count += 1
                dt = d.get("created_at")
                if dt:
                    if hasattr(dt, "strftime"):
                        day_str = dt.strftime("%Y-%m-%d")
                    else:
                        day_str = str(dt)[:10]
                else:
                    continue

                if day_str not in daily:
                    daily[day_str] = {
                        "day_date": day_str,
                        "total_translations": 0,
                        "sum_total_tokens": 0,
                        "sum_input_tokens": 0
                    }
                daily[day_str]["total_translations"] += 1
                daily[day_str]["sum_total_tokens"] += d.get("total_tokens", 0) or 0
                daily[day_str]["sum_input_tokens"] += d.get("input_tokens", 0) or 0

            stats = sorted(daily.values(), key=lambda x: x["day_date"])

            return jsonify({
                'success': True,
                'history': rows,
                'stats': stats,
                'total_count': total_count  # Return total count[cite: 48]
            })
        except Exception as e:
            return jsonify({'success': False, 'error': f"Firestore error: {str(e)}"}), 500

    effective_user = user_identity or "global"

    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get total count of translation records[cite: 48]
            cursor.execute("""
                SELECT COUNT(*) as total_count
                FROM translations
                WHERE user_id = ?
            """, (effective_user,))
            total_row = cursor.fetchone()
            total_count = total_row["total_count"] if total_row else 0

            cursor.execute("""
                SELECT nl_prompt, sql_command, created_at
                FROM translations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 50
            """, (effective_user,))
            rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT 
                    DATE(created_at) as day_date,
                    COUNT(*) as total_translations,
                    SUM(total_tokens) as sum_total_tokens,
                    SUM(input_tokens) as sum_input_tokens
                FROM translations
                WHERE user_id = ?
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at) ASC
            """, (effective_user,))
            stats = [dict(row) for row in cursor.fetchall()]

            return jsonify({
                'success': True, 
                'history': rows,
                'stats': stats,
                'total_count': total_count  # Return total count[cite: 48]
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/history/purge', methods=['DELETE', 'POST'])
def purge_translation_history():
    user_identity = get_current_user_identity()
    effective_user = user_identity or "global"

    # 1. Firestore / Cloud Run Mode
    if firestore_client:
        try:
            docs = firestore_client.collection("translations").where("user_id", "==", effective_user).stream()
            batch = firestore_client.batch()
            count = 0
            for doc in docs:
                batch.delete(doc.reference)
                count += 1
                # Commit in batches to respect Firestore batch limits (max 500)
                if count >= 400:
                    batch.commit()
                    batch = firestore_client.batch()
                    count = 0
            if count > 0:
                batch.commit()
                
            return jsonify({
                'success': True, 
                'message': 'Translation history purged successfully from Firestore.'
            })
        except Exception as e:
            return jsonify({
                'success': False, 
                'error': f"Firestore purge error: {str(e)}"
            }), 500

    # 2. Local SQLite Mode
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM translations WHERE user_id = ?", (effective_user,))
            conn.commit()
        
        # Sync updated SQLite state to Google Cloud Storage if configured
        upload_db_to_gcs()
        
        return jsonify({
            'success': True, 
            'message': 'Translation history purged successfully from local database.'
        })
    except Exception as e:
        return jsonify({
            'success': False, 
            'error': f"SQLite purge error: {str(e)}"
        }), 500


if __name__ == '__main__':
    hostname = os.environ.get("CRBOT_HOSTNAME", "0.0.0.0")
    port = int(os.environ.get("CRBOT_PORT", 3000))
    init_state_db()
    app.run(host=hostname, port=port, debug=False, use_reloader=False)