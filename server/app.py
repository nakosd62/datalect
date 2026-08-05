import os
import random
import re
import time
import uuid
from urllib.parse import urlparse, urlunparse
from flask import Flask, request, jsonify, make_response, send_from_directory
import psycopg2
import sqlparse
import sqlite3
from flask_cors import CORS
from google import genai
from google.genai import types
from google.cloud import storage

app = Flask(__name__, static_folder='../webClient', static_url_path='')
CORS(app)

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
raw_models_env = os.environ.get("GEMINI_AVAILABLE_MODELS", "")
AVAILABLE_MODELS = [m.strip() for m in raw_models_env.split(",") if m.strip()]
if not AVAILABLE_MODELS:
    AVAILABLE_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL") or AVAILABLE_MODELS[0]

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
DB_FILENAME = os.environ.get("TRANSLATION_STATS_DB_FILENAME", "crbot_state.db")
if GCS_BUCKET_NAME:
    TRANSLATION_STATS_DB_PATH = os.path.join("/tmp", DB_FILENAME)
else:
    TRANSLATION_STATS_DB_PATH = "state/crbot_state.db"

# --- Session Management via SQLite ---

def get_or_create_session_id():
    session_id = request.cookies.get('crbot_session_id') or request.headers.get('X-Session-ID')
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id

def set_session_db_url(session_id, db_url):
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sessions (session_id, database_url, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    database_url = excluded.database_url,
                    updated_at = CURRENT_TIMESTAMP;
            """, (session_id, db_url))
            conn.commit()

        upload_db_to_gcs()
    except Exception as e:
        print(f"Error saving session to SQLite: {e}")

def get_session_db_url(session_id):
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT database_url FROM sessions WHERE session_id = ?", (session_id,))
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
    try:
        download_db_from_gcs()

        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS translations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            conn.commit()

        upload_db_to_gcs()

    except Exception as e:
        print(f"Error initializing SQLite stats DB: {e}")

def get_gemini_api_keys():
    """Collect Gemini API keys from env (preset list + optional single key)."""
    keys = []
    
    # 1. Check for comma-separated list of keys
    preset_keys_env = os.environ.get("GEMINI_PRESET_KEYS", "")
    if preset_keys_env:
        keys.extend(k.strip() for k in preset_keys_env.split(",") if k.strip())
        
    # 2. Check for standard single key as a fallback/addition
    single_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if single_key and single_key.strip() not in keys:
        keys.append(single_key.strip())
        
    return keys

def pick_gemini_api_key():
    keys = get_gemini_api_keys()
    if not keys:
        return None
    return random.choice(keys)

def redact_connection_url(conn_str):
    if not conn_str:
        return conn_str
    match = re.match(r'^(postgresql://)([^:]+):([^@]+)(@.+)$', conn_str)
    if match:
        return f"{match.group(1)}{match.group(2)}:****{match.group(4)}"
    return conn_str

def mask_db_url(url_str):
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:*****@', url_str)

def resolve_conn_str(conn_str):
    if not conn_str or "****" in conn_str:
        return DEFAULT_CONN
    return conn_str

def record_translation(conn_str, nl_prompt, sql_command, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens):
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO translations (
                    connect_string, 
                    nl_prompt, sql_command, 
                    model, 
                    duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                redact_connection_url(conn_str), 
                nl_prompt, sql_command, 
                gemini_model, 
                duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
            ))
            conn.commit()

        upload_db_to_gcs()

    except Exception as e:
        print(f"Error recording translation: {e}")

def get_db_connection(conn_str=None):
    return psycopg2.connect(resolve_conn_str(conn_str))

def get_database_schema(conn_str=None):
    conn = None
    try:
        conn = get_db_connection(conn_str)
        schema_parts = []
        
        with conn.cursor() as cursor:
            # 1. Fetch Tables and Columns (including Defaults & Nullability)
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

            # 2. Primary Keys, Foreign Keys, Unique & Check Constraints
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

            # 3. Indexes (from pg_catalog)
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

            # 4. Views and View Definitions
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

            # 5. Table Grants / Permissions
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
    conn_str = data.get('database_url') or get_session_db_url(session_id)

    history = data.get('history', [])[-10:]

    try:
        schema = get_database_schema(conn_str)
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
            "return your response enclosed as follows: SELECT '<your response>' as General_Knowledge;\n"
            "If you cannot respond at all with reasonable confidence, return the following: SELECT 'I am not able to respond to your prompt' as Regrets;\n"
            "If you run into any error, return the error enclosed as follows: SELECT 'I ran into this error: <the error>' as Error;\n"
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

        record_translation(conn_str, prompt, generated_sql, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens)
            
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
    
    if request.method == 'POST':
        data = request.get_json() or {}
        new_db_url = data.get('database_url')
        
        if new_db_url and '****' in new_db_url:
            pass 
        elif new_db_url:
            set_session_db_url(session_id, new_db_url)
        else:
            set_session_db_url(session_id, DEFAULT_CONN)

    active_conn_str = get_session_db_url(session_id)
    
    db_name, username = "Unknown", "Unknown"
    conn = None
    try:
        conn = get_db_connection(active_conn_str)
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

    resp = jsonify({
        'session_id': session_id,
        'configured_databases': CONFIGURED_DBS,
        'default_database_url': DEFAULT_CONN,
        'active_database_url': mask_db_url(active_conn_str),
        'default_model': DEFAULT_MODEL,
        'available_models': AVAILABLE_MODELS,
        'database_name': db_name,
        'username': username
    })
    return apply_session_cookie(resp, session_id)

@app.route('/api/execute', methods=['POST'])
def execute_query():
    session_id = get_or_create_session_id()
    data = request.get_json() or {}
    
    conn_str = data.get('database_url') or get_session_db_url(session_id)
    
    raw_query = (data.get('sql') or data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'Query cannot be empty'}), 400

    conn = None
    start_time = time.time()
    
    try:
        conn = get_db_connection(conn_str)
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

@app.route('/api/history', methods=['GET'])
def get_translation_history():
    try:
        with sqlite3.connect(TRANSLATION_STATS_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT nl_prompt, sql_command, created_at
                FROM translations
                ORDER BY created_at DESC
                LIMIT 20
            """)
            rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT 
                    DATE(created_at) as day_date,
                    COUNT(*) as total_translations,
                    SUM(total_tokens) as sum_total_tokens,
                    SUM(input_tokens) as sum_input_tokens
                FROM translations
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at) ASC
            """)
            stats = [dict(row) for row in cursor.fetchall()]

            return jsonify({
                'success': True, 
                'history': rows,
                'stats': stats
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    hostname = os.environ.get("CRBOT_HOSTNAME", "0.0.0.0")
    port = int(os.environ.get("CRBOT_PORT", 3000))
    init_state_db()
    app.run(host=hostname, port=port, debug=False, use_reloader=False)