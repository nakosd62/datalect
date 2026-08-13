"""
db.py

Everything related to talking to the target Postgres database: resolving
which connection string to use for a given request/user, opening
connections, and introspecting schema (tables/constraints/indexes/views/
grants/triggers) to feed into the Gemini prompt.

Also owns `record_translation`, a thin wrapper around state_store that
derives a non-sensitive "user@dbname" identifier before logging a
translation event (so raw connection strings never end up in the
translation-history table).
"""

from urllib.parse import urlparse

import psycopg2

from app_config import DEFAULT_CONN, state_store, logger
import schema_cache

_SCHEMA_FETCH_FAILED = "No schema description available."


def resolve_conn_str(conn_str=None, user_id=None):
    if not conn_str:
        if user_id:
            db_url, _ = state_store.get_session(user_id)
            return db_url
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
    state_store.record_translation(
        user_id, conn_identifier, nl_prompt, sql_command, gemini_model,
        duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
    )


def get_db_connection(conn_str=None, user_id=None):
    return psycopg2.connect(resolve_conn_str(conn_str, user_id))


def get_database_schema(conn_str=None, user_id=None, force_refresh=False):
    """
    Returns the schema introspection text for the resolved connection,
    using a short-TTL in-memory cache (see schema_cache.py) so repeated
    /api/translate calls in the same chat session don't re-run six
    information_schema queries every time.

    Pass force_refresh=True to bypass and repopulate the cache - e.g. when
    the frontend knows the user just changed database/schema and wants an
    immediate refresh rather than waiting out the TTL.
    """
    resolved_conn_str = resolve_conn_str(conn_str, user_id)
    cache_key = get_conn_identifier(resolved_conn_str)

    if not force_refresh:
        cached = schema_cache.get(cache_key)
        if cached is not None:
            return cached

    schema_text = _fetch_database_schema(resolved_conn_str)
    # Don't cache the failure fallback - a transient connection hiccup
    # shouldn't get "frozen in" as the answer for the rest of the TTL
    # window once the DB is reachable again.
    if schema_text != _SCHEMA_FETCH_FAILED:
        schema_cache.set(cache_key, schema_text)
    return schema_text


def _fetch_database_schema(conn_str):
    """The actual DB-hitting introspection logic. Always fetches live -
    call get_database_schema() instead unless you specifically need to
    bypass the cache layer."""
    conn = None
    try:
        conn = get_db_connection(conn_str)
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

        return "\n\n".join(schema_parts) if schema_parts else _SCHEMA_FETCH_FAILED
    except Exception:
        logger.exception("Error fetching schema")
        return _SCHEMA_FETCH_FAILED
    finally:
        if conn:
            conn.close()