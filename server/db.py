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

import concurrent.futures

from app_config import DEFAULT_CONN, CONFIGURED_DBS, state_store, logger
from backends import get_backend
from backends.base import extract_entry_names_from_schema_text
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


def resolve_active_descriptor(session, user_id):
    """Builds a connection descriptor FRESH from a state_store session
    record (see StateStore.get_session) - the session itself holds only an
    identity reference (is_custom, connection_id), never the connection's
    actual details/credentials, so this is the one place that identity gets
    turned into something actually connectable, every time, from the single
    source of truth: CONFIGURED_DBS (app_config.py) for a preset, or
    state_store.get_db_connections() for a saved custom connection. Public
    because config_routes.py also needs to resolve a descriptor for the
    *active* session connection (for the /api/config "which DB am I
    connected to" identity check) without going through resolve_conn_str().

    Returns (descriptor, missing). missing=True means connection_id was
    set to something but it no longer resolves to anything real - the
    preset was removed/renamed from CONFIGURED_DBS, or the saved custom
    connection was deleted - in which case descriptor is still a usable
    one (the app default), so a caller that doesn't care about the
    distinction (query execution) can just use it as-is; config_routes.py's
    GET handler is the one caller that surfaces `missing` to the frontend.
    connection_id == "" (nothing ever explicitly selected - a brand-new
    session) is NOT "missing" - that's the ordinary/expected state for a
    first-time visitor, so it silently resolves to the default connection
    the same way, with missing=False."""
    connection_id = session.get("connection_id") or ""
    is_custom = bool(session.get("is_custom"))
    if not connection_id:
        return _to_descriptor(DEFAULT_CONN), False
    if is_custom:
        for db in state_store.get_db_connections(user_id, include_credentials=True):
            if db.get("connection_key") == connection_id:
                descriptor = {"type": db.get("type") or "postgres", "url": db.get("url")}
                descriptor.update(db.get("config") or {})
                return descriptor, False
        return _to_descriptor(DEFAULT_CONN), True
    for db in CONFIGURED_DBS:
        if db.get("id") == connection_id:
            # CONFIGURED_DBS entries already ARE full descriptors plus
            # "id"/"name" - stripping just those two is all that's needed,
            # no separate copy/merge step like the custom-connection branch
            # above (which has to reshape get_db_connections()'s
            # {"connection_key","name","type","url","config"} response
            # shape into a flat descriptor).
            return {k: v for k, v in db.items() if k not in ("id", "name")}, False
    return _to_descriptor(DEFAULT_CONN), True


def resolve_descriptor_by_reference(kind, ref_id, user_id):
    """Resolves one {kind, id} in-scope/pinned-connection reference to a
    fresh, credentialed descriptor plus its human-readable name - the
    shared resolution primitive for both the multi-database "pin"
    mechanism (translate_routes.py/execute_routes.py trust only
    {kind, id} references from the client, never raw descriptors/
    credentials) and for parsing a `-- database: preset:<id>`/
    `-- database: custom:<key>` marker back out of generated/edited SQL
    at execute time (execute_routes.py). Mirrors resolve_active_descriptor's
    two branches exactly (same CONFIGURED_DBS/get_db_connections() sources
    of truth), just addressed by an explicit kind+id instead of a
    session's single connection_id/is_custom pair.

    Returns (descriptor, name), or (None, None) if `ref_id` doesn't
    resolve to anything real for this kind (a preset that's been removed/
    renamed, or a custom connection the user has since deleted) - callers
    are expected to treat that the same way resolve_active_descriptor's
    missing=True is treated elsewhere: skip this one connection rather
    than fail the whole request (see resolve_in_scope_descriptors below)."""
    if kind == "custom":
        for db in state_store.get_db_connections(user_id, include_credentials=True):
            if db.get("connection_key") == ref_id:
                descriptor = {"type": db.get("type") or "postgres", "url": db.get("url")}
                descriptor.update(db.get("config") or {})
                return descriptor, db.get("name") or "Custom"
        return None, None
    if kind == "preset":
        for db in CONFIGURED_DBS:
            if db.get("id") == ref_id:
                return {k: v for k, v in db.items() if k not in ("id", "name")}, db.get("name") or ref_id
        return None, None
    return None, None


def resolve_in_scope_descriptors(session, user_id):
    """Resolves a session's whole in-scope connection set to a list of
    {"kind", "id", "name", "descriptor"} dicts, in stable order (presets
    first, then custom connections, each in the order stored) - the
    candidate pool connection_router.py's Phase A chooses from, and what
    determines whether a request even needs routing at all (see
    translate_routes.py: len(...) <= 1 is the byte-identical-to-today fast
    path).

    session["in_scope_mode"] == "all" (see StateStore.get_session's
    docstring) takes a completely different path here - see
    _resolve_all_configured_descriptors below - ignoring
    in_scope_preset_ids/in_scope_custom_connection_keys entirely in favor
    of a dynamic, resolved-fresh-every-request "every configured
    connection" set. Every other mode (the default "single", and any
    legacy session that saved an arbitrary multi-connection subset before
    the binary single/all choice existed) resolves the explicit
    in_scope_preset_ids/in_scope_custom_connection_keys lists below,
    exactly as this function always has.

    A reference that no longer resolves (resolve_descriptor_by_reference
    returned None - a removed preset, a deleted custom connection) is
    silently skipped, same leniency resolve_active_descriptor already
    applies to a single stale connection_id. Falls back to a single
    app-default entry only if EVERY reference fails to resolve, or the
    in-scope set is empty to begin with (a brand-new session, or one that
    predates this feature and has never explicitly saved a connection at
    all) - this is what guarantees the result is never empty, so callers
    never need their own separate empty-list fallback."""
    if session.get("in_scope_mode") == "all":
        return _resolve_all_configured_descriptors(user_id)
    entries = []
    for preset_id in session.get("in_scope_preset_ids") or []:
        descriptor, name = resolve_descriptor_by_reference("preset", preset_id, user_id)
        if descriptor is not None:
            entries.append({"kind": "preset", "id": preset_id, "name": name, "descriptor": descriptor})
    for custom_key in session.get("in_scope_custom_connection_keys") or []:
        descriptor, name = resolve_descriptor_by_reference("custom", custom_key, user_id)
        if descriptor is not None:
            entries.append({"kind": "custom", "id": custom_key, "name": name, "descriptor": descriptor})
    if not entries:
        return [{
            "kind": "preset", "id": "", "name": "Default connection",
            "descriptor": _to_descriptor(DEFAULT_CONN),
        }]
    return entries


def _resolve_all_configured_descriptors(user_id):
    """"All configured databases" (see webClient/client.js's
    renderDbRadioButtons()) - the dynamic candidate pool for a session in
    in_scope_mode == "all": EVERY currently-configured preset
    (CONFIGURED_DBS, read fresh on every call, so a preset added or
    removed since this was last true is immediately reflected - the whole
    point of "All" over the frozen, save-time-computed subset the old
    arbitrary checkbox picker produced) plus every one of this user's own
    saved custom connections. Each resolved via
    resolve_descriptor_by_reference exactly like the explicit-list branch
    in resolve_in_scope_descriptors above, so a connection that
    (implausibly, mid-request) stops resolving is silently skipped the
    same way, not a special case. Falls back to the single app-default
    entry only if there's nothing configured at all - CONFIGURED_DBS
    always has at least DEFAULT_CONN in practice (see app_config.py), so
    this is a defensive floor, not an expected path."""
    entries = []
    for db in CONFIGURED_DBS:
        preset_id = db.get("id")
        descriptor, name = resolve_descriptor_by_reference("preset", preset_id, user_id)
        if descriptor is not None:
            entries.append({"kind": "preset", "id": preset_id, "name": name, "descriptor": descriptor})
    for db in state_store.get_db_connections(user_id, include_credentials=True):
        custom_key = db.get("connection_key")
        if not custom_key:
            # A legacy custom connection saved before connection_key
            # existed (see get_db_connections' docstring) can't be
            # individually addressed by resolve_descriptor_by_reference at
            # all - same "nothing to do here" this feature's explicit-list
            # branch above always had for such a row.
            continue
        descriptor, name = resolve_descriptor_by_reference("custom", custom_key, user_id)
        if descriptor is not None:
            entries.append({"kind": "custom", "id": custom_key, "name": name, "descriptor": descriptor})
    if not entries:
        return [{
            "kind": "preset", "id": "", "name": "Default connection",
            "descriptor": _to_descriptor(DEFAULT_CONN),
        }]
    return entries


def build_router_candidate_summaries(in_scope_entries, user_id):
    """Builds compact, table-name-only summaries for connection_router.py's
    Phase A prompt - one {"name", "dialect", "table_names"} dict per entry
    in `in_scope_entries` (see resolve_in_scope_descriptors), in the same
    order, so Phase A's returned candidate indices line up positionally
    with this list.

    Deliberately never includes column-level schema - only enough for the
    router to guess relevance from table/tab names and dialect. Reuses the
    same TTL-cached get_database_schema() every other schema-aware code
    path already goes through (so this doesn't cost a second round-trip
    for a connection whose schema is already cached), reduced via
    backends/base.py's extract_entry_names_from_schema_text.

    Fetched in parallel (one worker per in-scope connection) via
    ThreadPoolExecutor, mirroring execute_routes.py's
    _execute_with_timeout precedent - a cold cache on several connections
    at once (e.g. right after the user adds a new connection to scope)
    shouldn't serialize one slow schema fetch behind another. A single
    connection's fetch failing degrades to an empty table_names list for
    just that entry (get_database_schema() already degrades to its own
    "schema fetch failed" placeholder text on failure, which
    extract_entry_names_from_schema_text then reduces to []) rather than
    failing the whole summary."""
    if not in_scope_entries:
        return []

    def _summarize(entry):
        schema_text = get_database_schema(entry["descriptor"], user_id)
        table_names = extract_entry_names_from_schema_text(schema_text)
        try:
            dialect = get_backend(entry["descriptor"]).dialect_name
        except Exception:
            dialect = "SQL"
        return {"name": entry["name"], "dialect": dialect, "table_names": table_names}

    results = [None] * len(in_scope_entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(in_scope_entries)) as pool:
        future_to_index = {pool.submit(_summarize, entry): i for i, entry in enumerate(in_scope_entries)}
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception:
                logger.exception("Error building router candidate summary")
                entry = in_scope_entries[index]
                results[index] = {"name": entry["name"], "dialect": "SQL", "table_names": []}
    return results


def resolve_conn_str(conn_str=None, user_id=None):
    """Resolves to a connection descriptor: the explicit conn_str/descriptor
    if given, else the user's active session connection - resolved fresh via
    resolve_active_descriptor, discarding whether it was actually found
    (query execution silently falls back to the app default either way; see
    that function's docstring) - else the app default."""
    if conn_str:
        return _to_descriptor(conn_str)
    if user_id:
        descriptor, _missing = resolve_active_descriptor(state_store.get_session(user_id), user_id)
        return descriptor
    return _to_descriptor(DEFAULT_CONN)


def get_conn_identifier(conn_str):
    """Non-sensitive cache/log identifier for a connection (descriptor or
    legacy raw string) - delegates to the resolved backend's cache_key()
    so each dialect can derive this however makes sense for it (Postgres:
    user@host:port/dbname parsed from the URL; BigQuery:
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
    non-sensitive cache key (e.g. "user@host:port/dbname" for Postgres) as
    a last resort - never blank, so history rows always show something
    readable.

    Postgres/MySQL custom connections still match by url, same as always
    - it's their real, distinguishing DSN. BigQuery/Snowflake/Databricks/
    Oracle/Redshift/MSSQL/Sheets custom connections have no real url of
    their own (config_routes.py's module docstring), so url is always
    None for those now; they're matched by comparing the descriptor's own
    config fields (the same ones resolve_active_descriptor merged onto it
    from the saved row in the first place) against each saved row's
    config instead. include_credentials=True on that lookup is required
    for this comparison, not just an option - resolve_active_descriptor
    built `descriptor` with credentials merged in, so a stripped
    (credential-free) config from get_db_connections() would never equal
    it."""
    descriptor = descriptor or {}
    url = descriptor.get("url")
    if url:
        for db in CONFIGURED_DBS:
            if db.get("url") == url:
                return db["name"]
    if user_id:
        try:
            db_type = descriptor.get("type")
            own_config = {k: v for k, v in descriptor.items() if k not in ("type", "url")}
            for db in state_store.get_db_connections(user_id, include_credentials=True):
                if db.get("type") != db_type:
                    continue
                if url:
                    if db.get("url") == url:
                        return db.get("name") or "Custom"
                elif (db.get("config") or {}) == own_config:
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


def record_all_databases_triage(user_id, nl_prompt, sql_command, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens):
    """Logs "all databases" mode's Phase A (triage) step to the same
    translations table record_translation() writes to, but tagged with the
    literal database_type/database_name "All Databases"/"All Databases"
    rather than any real connection descriptor - unlike every other row in
    this table, a triage call isn't "about" one specific database at all
    (it's the step that decides whether real data is even needed, and if
    so, which connection(s) to route to), so there's no real descriptor to
    resolve a db_type/db_name from the way record_translation() does above.

    Deliberately bypasses record_translation()'s _to_descriptor/
    _resolve_database_name resolution entirely rather than trying to feed
    it a synthetic descriptor - "All Databases" is a fixed, literal label,
    not a lookup result.

    Called once per "all databases" request regardless of triage's outcome
    (answer/failed/route - see translate_routes.py's router_only_all_mode
    branch), always with ONLY triage's own duration and LLM token usage -
    never folded in with any Phase B (per-database generation) numbers, so
    a "route" outcome's real, per-database translations-table row (logged
    separately, attributed to that specific connection) never double-counts
    the tokens/time this row already accounts for."""
    state_store.record_translation(
        user_id, "All Databases", "All Databases", nl_prompt, sql_command, gemini_model,
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