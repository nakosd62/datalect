"""
db.py

Connection resolution and schema introspection, dispatched by dialect
through the backends/ package (see backends/base.py for the interface).
This module no longer talks to psycopg2 directly - that lives in
backends/postgres.py. Adding a new dialect (BigQuery, Snowflake,
Databricks, ...) means adding a new backends/*.py file and registering it
in backends/__init__.py; nothing in this module needs to change.

Every connection is identified by a "descriptor" dict, e.g.
{"type": "postgres", "url": "postgresql://..."}. Today every descriptor
in the app is built here by wrapping a plain Postgres connection-string -
state_store, the frontend, and CONFIGURED_DBS all still deal purely in
URL strings (see _to_descriptor below). That's a deliberate scoping
choice: propagating richer descriptors (type selection, non-URL
credentials) up through state_store/the API/the UI is follow-up work for
when a second backend actually needs it.

Also owns `record_translation`, a thin wrapper around state_store that
derives a non-sensitive cache/log identifier (via the resolved backend's
cache_key()) before logging a translation event - so raw connection
strings/credentials never end up in the translation-history table.
"""

from app_config import DEFAULT_CONN, CONFIGURED_DBS, state_store, logger
from backends import get_backend
import schema_cache

_SCHEMA_FETCH_FAILED = "No schema description available."


def _to_descriptor(conn_str):
    """Normalizes a raw connection string (or an already-built descriptor)
    into a descriptor dict. Used for the explicit-override case - a caller
    passing a bare string (e.g. a per-request database_url override, or
    the module-level DEFAULT_CONN fallback) is always a plain Postgres URL,
    so that's the only case handled here; this is the single place that
    assumption lives. A caller that already has a richer descriptor (e.g.
    a BigQuery {"type": "bigquery", ...} dict) can pass it straight through."""
    if conn_str is None:
        return None
    if isinstance(conn_str, dict):
        return conn_str
    return {"type": "postgres", "url": conn_str}


def session_to_descriptor(session):
    """Builds a connection descriptor from a state_store session record
    (see StateStore.get_session) - merges the type-specific
    database_config fields (e.g. BigQuery's project_id/dataset/
    credentials_json) on top of the {"type", "url"} base. Public because
    config_routes.py also needs to resolve a backend for the *active*
    session connection (for the /api/config "which DB am I connected to"
    identity check) without going through a fresh resolve_conn_str() call."""
    descriptor = {
        "type": session.get("database_type") or "postgres",
        "url": session.get("database_url"),
    }
    descriptor.update(session.get("database_config") or {})
    return descriptor


def resolve_conn_str(conn_str=None, user_id=None):
    """Resolves to a connection descriptor: the explicit conn_str/descriptor
    if given, else the user's saved session connection (type + config,
    via session_to_descriptor), else the app default."""
    if conn_str:
        return _to_descriptor(conn_str)
    if user_id:
        return session_to_descriptor(state_store.get_session(user_id))
    return _to_descriptor(DEFAULT_CONN)


def get_conn_identifier(conn_str):
    """Non-sensitive cache/log identifier for a connection (descriptor or
    legacy raw string) - delegates to the resolved backend's cache_key()
    so each dialect can derive this however makes sense for it (Postgres:
    user@dbname parsed from the URL; a future BigQuery backend:
    project.dataset)."""
    descriptor = _to_descriptor(conn_str)
    if not descriptor:
        return "unknown@unknown"
    try:
        return get_backend(descriptor).cache_key(descriptor)
    except Exception:
        return "unknown@unknown"


def _resolve_database_name(descriptor, user_id):
    """Best-effort human-readable name for a connection descriptor, for
    translation-history logging: the admin-configured preset name if the
    URL matches one in CONFIGURED_DBS, else the user's own saved custom-
    connection name if it matches one of those, else the backend's
    non-sensitive cache key (e.g. "user@dbname" for Postgres) as a last
    resort - never blank, so history rows always show something readable."""
    url = (descriptor or {}).get("url")
    if url:
        for db in CONFIGURED_DBS:
            if db.get("url") == url:
                return db["name"]
        if user_id:
            try:
                for db in state_store.get_db_connections(user_id):
                    if db.get("url") == url:
                        return db.get("name") or "Custom"
            except Exception:
                logger.exception("Error resolving custom database name for translation history")
    return get_conn_identifier(descriptor)


def record_translation(user_id, conn_str, nl_prompt, sql_command, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens):
    descriptor = _to_descriptor(conn_str)
    db_type = (descriptor or {}).get("type") or "postgres"
    db_name = _resolve_database_name(descriptor, user_id)
    state_store.record_translation(
        user_id, db_type, db_name, nl_prompt, sql_command, gemini_model,
        duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
    )


def get_db_connection(conn_str=None, user_id=None):
    descriptor = resolve_conn_str(conn_str, user_id)
    return get_backend(descriptor).connect(descriptor)


def get_database_schema(conn_str=None, user_id=None, force_refresh=False):
    """
    Returns the schema introspection text for the resolved connection,
    using a short-TTL in-memory cache (see schema_cache.py) so repeated
    /api/translate calls in the same chat session don't re-run the
    backend's introspection queries every time.

    Pass force_refresh=True to bypass and repopulate the cache - e.g. when
    the frontend knows the user just changed database/schema and wants an
    immediate refresh rather than waiting out the TTL.
    """
    descriptor = resolve_conn_str(conn_str, user_id)
    cache_key = get_conn_identifier(descriptor)

    if not force_refresh:
        cached = schema_cache.get(cache_key)
        if cached is not None:
            return cached

    schema_text = _fetch_database_schema(descriptor)
    # Don't cache the failure fallback - a transient connection hiccup
    # shouldn't get "frozen in" as the answer for the rest of the TTL
    # window once the DB is reachable again.
    if schema_text != _SCHEMA_FETCH_FAILED:
        schema_cache.set(cache_key, schema_text)
    return schema_text


def _fetch_database_schema(descriptor):
    """The actual DB-hitting introspection logic, delegated to whichever
    backend matches the descriptor's type. Always fetches live - call
    get_database_schema() instead unless you specifically need to bypass
    the cache layer."""
    backend = get_backend(descriptor)
    connection = None
    try:
        connection = backend.connect(descriptor)
        schema_text = backend.get_schema(connection)
        return schema_text if schema_text else _SCHEMA_FETCH_FAILED
    except Exception:
        logger.exception("Error fetching schema")
        return _SCHEMA_FETCH_FAILED
    finally:
        if connection:
            backend.close(connection)