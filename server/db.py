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
import threading

from app_config import DEFAULT_DESCRIPTOR, CONFIGURED_DBS, state_store, logger
from backends import get_backend
from backends.base import (
    extract_entry_names_from_schema_text, schema_text_was_truncated, schema_text_has_omitted_tables,
    SCHEMA_MAX_CHARS, SCHEMA_MAX_TABLES,
)
import schema_cache

_SCHEMA_FETCH_FAILED = "No schema description available."

# Three reasons a schema fetch can fail, threaded through
# get_database_schema_with_reason()/prime_schema_cache_with_reason() below
# to config_routes.py's /api/config/refresh-schema (so it can tell a user
# something more accurate than a blanket "could not fetch schema") and to
# prefetch_all_preset_schemas() below (so it can decide whether a preset
# that failed at startup stays in the dialog's list or gets dropped from
# it - see that function's own docstring). Never surfaced to the model or
# embedded in cached schema text itself - a failed fetch's actual
# returned/cached text is always exactly _SCHEMA_FETCH_FAILED's plain
# string, regardless of which reason produced it; this is purely additive
# metadata for a caller that wants more than that one bare sentinel. Plain
# string constants rather than an enum - simplest thing that works for
# three values with two readers today.
SCHEMA_FETCH_FAILURE_REASON_EMPTY = "empty"  # connected/queried fine - zero BASE TABLEs visible
# A raised connect()/get_schema() failure is further split into two
# sub-reasons - see _looks_like_timeout_error() below for how the split is
# made, and prefetch_all_preset_schemas()'s own docstring for why the
# split exists at all (a startup preset prefetch treats the two very
# differently): TIMEOUT means the attempt hit DB_CONNECT_TIMEOUT_SECONDS
# (or an equivalent query/read timeout) without ever getting a definitive
# answer - typically a slow/currently-unreachable host, worth trying again
# later. FATAL means the attempt got a definitive, fast rejection instead
# (wrong credentials, a missing driver, a malformed request, DNS
# resolution failure, a real query error) - retrying won't help until
# whatever's actually wrong is fixed.
SCHEMA_FETCH_FAILURE_REASON_TIMEOUT = "timeout"
SCHEMA_FETCH_FAILURE_REASON_FATAL = "fatal"


def _looks_like_timeout_error(exc):
    """Best-effort, cross-dialect check for "this connect()/get_schema()
    failure was specifically a timeout" rather than a definitive
    rejection - see SCHEMA_FETCH_FAILURE_REASON_TIMEOUT/_FATAL above for
    why the distinction matters.

    There's no single shared timeout exception type across this app's 8
    SQL backends' drivers to reliably isinstance()-check against instead:
    backends/mssql.py's own _connect_with_hard_timeout wrapper explicitly
    raises a plain built-in TimeoutError when pytds's own timeout handling
    can't be trusted (see its docstring) - a clean, unambiguous signal,
    checked first - but every other driver (psycopg2, pymysql, oracledb,
    the snowflake/databricks connectors, pyodbc) just raises its own
    generic connection-error exception class (OperationalError,
    DatabaseError, ...) for a connect_timeout/tcp_connect_timeout/
    login_timeout expiry, indistinguishable from any other connection
    failure except by message text. backends/sheets.py's requests-based
    calls are the other clean case (requests.exceptions.Timeout).

    For everything else, this falls back to a permissive, case-insensitive
    substring check for "timeout"/"timed out" in str(exc) - every driver's
    own timeout-expiry message includes one of those words in practice.
    Deliberately permissive: a false NEGATIVE here (calling a real timeout
    "fatal") is worse than a false positive (a genuinely fatal error that
    happens to mention "timeout" in unrelated text), since this feeds
    prefetch_all_preset_schemas()'s decision to permanently drop a preset
    from the dialog's list until the next restart - being overly eager to
    call something "fatal" would remove presets that just needed a retry,
    the worse of the two failure modes this feature exists to avoid."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        import requests
        if isinstance(exc, requests.exceptions.Timeout):
            return True
    except ImportError:
        pass
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message


# Preset ids that startup prefetch determined have a FATAL (non-timeout)
# schema-fetch failure - see prefetch_all_preset_schemas()'s own docstring
# for the full "fatal vs. timeout" design. Process-wide (presets have no
# per-user identity) and guarded by its own lock rather than reusing
# schema_cache.py's, since this is conceptually unrelated state (which
# PRESETS EXIST, not what their schema IS) - mirrors cancel_registry.py's
# own "one small lock-guarded module-level structure per independent
# concern" convention. Reset only by a process restart (CONFIGURED_DBS
# itself is rebuilt fresh at import time every restart too, and startup
# prefetch runs again from scratch, so a preset that was excluded gets
# another chance rather than being permanently banned).
_fatally_failed_preset_ids = set()
_fatally_failed_preset_ids_lock = threading.Lock()


def _mark_preset_fatally_failed(preset_id):
    with _fatally_failed_preset_ids_lock:
        _fatally_failed_preset_ids.add(preset_id)


def visible_configured_dbs():
    """CONFIGURED_DBS, minus any preset startup prefetch has since marked
    fatally failed (see prefetch_all_preset_schemas()) - the one filtered
    view every call site that resolves or lists SELECTABLE presets should
    read through instead of iterating CONFIGURED_DBS directly: this
    module's own resolve_active_descriptor/resolve_descriptor_by_reference/
    _resolve_all_configured_descriptors, and config_routes.py's preset-
    listing/preset-selection code in its GET/POST /api/config handler.

    Deliberately NOT used by call sites that need to resolve a preset for
    HISTORICAL purposes regardless of its current visibility (e.g.
    chat_history_routes.py labeling which preset a past translation ran
    against) - those still read CONFIGURED_DBS directly, since a preset
    excluded today shouldn't erase which one a past request actually used.

    A plain filter over the live CONFIGURED_DBS list (not a cached/
    snapshotted copy) for the same reason _resolve_all_configured_descriptors
    already reads CONFIGURED_DBS fresh on every call: an admin-configured
    preset set doesn't change within a process's lifetime today, but this
    keeps the same "read live" property that function already documents
    rather than introducing a second, potentially-stale view of it."""
    if not _fatally_failed_preset_ids:
        return CONFIGURED_DBS
    with _fatally_failed_preset_ids_lock:
        excluded = set(_fatally_failed_preset_ids)
    return [db for db in CONFIGURED_DBS if db.get("id") not in excluded]


def _to_descriptor(conn_str):
    """Normalizes a raw connection string (or an already-built descriptor)
    into a descriptor dict. Used for the explicit-override case - a caller
    passing a bare string (e.g. a per-request database_url override) is
    always a plain Postgres URL, so that's the only case handled here; this
    is the single place that assumption lives. A caller that already has a
    richer descriptor (e.g. a BigQuery {"type": "bigquery", ...} dict, or
    app_config.py's own module-level DEFAULT_DESCRIPTOR - see its callers
    below) can pass it straight through - copied defensively (a fresh dict,
    not the same reference) since DEFAULT_DESCRIPTOR in particular is a
    single shared object handed to every blank-connection_id session; a
    caller that ever mutated what it got back in place (none do today, but
    nothing stops a future one) would otherwise corrupt the app-wide
    default for every other session sharing it."""
    if conn_str is None:
        return None
    if isinstance(conn_str, dict):
        return dict(conn_str)
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
    the same way, with missing=False. That default is DEFAULT_DESCRIPTOR
    (app_config.py) - normally the first Postgres preset (or the hardcoded
    DEFAULT_CONN fallback), but overridable to any configured preset via
    the DATABASE_DEFAULT env var - see that module's own comment."""
    connection_id = session.get("connection_id") or ""
    is_custom = bool(session.get("is_custom"))
    if not connection_id:
        return _to_descriptor(DEFAULT_DESCRIPTOR), False
    if is_custom:
        for db in state_store.get_db_connections(user_id, include_credentials=True):
            if db.get("connection_key") == connection_id:
                descriptor = {"type": db.get("type") or "postgres", "url": db.get("url")}
                descriptor.update(db.get("config") or {})
                return descriptor, False
        return _to_descriptor(DEFAULT_DESCRIPTOR), True
    for db in visible_configured_dbs():
        if db.get("id") == connection_id:
            # CONFIGURED_DBS entries already ARE full descriptors plus
            # "id"/"name" - stripping just those two is all that's needed,
            # no separate copy/merge step like the custom-connection branch
            # above (which has to reshape get_db_connections()'s
            # {"connection_key","name","type","url","config"} response
            # shape into a flat descriptor).
            return {k: v for k, v in db.items() if k not in ("id", "name")}, False
    return _to_descriptor(DEFAULT_DESCRIPTOR), True


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
        for db in visible_configured_dbs():
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
    of a dynamic, resolved-fresh-every-request "every configured preset"
    set (presets only - see that function's own docstring for why custom
    connections are deliberately excluded from it). Every other mode (the
    default "single", and any legacy session that saved an arbitrary
    multi-connection subset before the binary single/all choice existed)
    resolves the explicit in_scope_preset_ids/in_scope_custom_connection_keys
    lists below, exactly as this function always has - that legacy
    explicit-subset path CAN still include custom connections, since it's
    a user-picked list, not "all".

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
            "descriptor": _to_descriptor(DEFAULT_DESCRIPTOR),
        }]
    return entries


def _resolve_all_configured_descriptors(user_id):
    """"All Pre-Configured Datasets" (see webClient/client.js's
    renderDbRadioButtons()) - the dynamic candidate pool for a session in
    in_scope_mode == "all": EVERY currently-configured preset that hasn't
    explicitly opted out (CONFIGURED_DBS, read fresh on every call, so a
    preset added or removed since this was last true is immediately
    reflected - the whole point of "All" over the frozen, save-time-computed
    subset the old arbitrary checkbox picker produced).

    A preset with "include_in_all_mode": false in DATABASE_PRESETS_FILE
    (see app_config.py's own comment on that field) is skipped here even
    though it's still a perfectly valid, individually-selectable preset
    everywhere else - this is the ONLY place that distinction matters, since
    every other code path that touches CONFIGURED_DBS (the explicit-list
    branch in resolve_in_scope_descriptors above, the single-connection
    radio picker, resolve_descriptor_by_reference) has no notion of "all
    mode" to exclude a preset from in the first place. Defaults to included
    (db.get("include_in_all_mode", True)) for any preset that predates this
    field or never sets it - unchanged behavior for everyone who hasn't
    opted a preset out.

    Deliberately PRESETS ONLY, never this user's own custom connections -
    unlike an earlier version of this feature (when it was still named/
    framed as "All Databases"), which folded in every one of the user's
    saved custom connections too. A user's custom connections are their
    own ad hoc, often one-off or credential-sensitive additions, not part
    of the curated set an admin actually intends "ask across everything"
    to mean - and silently including them meant a prompt like "how many
    customers do we have" could route to a personal scratch connection
    the user never meant to include in a broad, unscoped question. Each
    preset is resolved via resolve_descriptor_by_reference exactly like
    the explicit-list branch in resolve_in_scope_descriptors above, so a
    preset that (implausibly, mid-request) stops resolving is silently
    skipped the same way, not a special case. Falls back to the single
    app-default entry if there's nothing left in the candidate pool -
    either because nothing is configured at all (CONFIGURED_DBS always has
    at least DEFAULT_CONN in practice, see app_config.py, so this half is a
    defensive floor, not an expected path) or, now, because an admin has
    set "include_in_all_mode": false on every single configured preset
    (an unusual but legitimate config - "All" mode degrading to one default
    connection is a saner outcome than returning zero candidates)."""
    entries = []
    for db in visible_configured_dbs():
        if not db.get("include_in_all_mode", True):
            continue
        preset_id = db.get("id")
        descriptor, name = resolve_descriptor_by_reference("preset", preset_id, user_id)
        if descriptor is not None:
            entries.append({"kind": "preset", "id": preset_id, "name": name, "descriptor": descriptor})
    if not entries:
        return [{
            "kind": "preset", "id": "", "name": "Default connection",
            "descriptor": _to_descriptor(DEFAULT_DESCRIPTOR),
        }]
    return entries


def build_router_candidate_summaries(in_scope_entries, user_id):
    """Builds compact, table-name-only summaries for connection_router.py's
    Phase A prompt - one {"name", "dialect", "table_names"} dict per entry
    in `in_scope_entries` (see resolve_in_scope_descriptors), in the same
    order, so Phase A's returned candidate indices line up positionally
    with this list.

    Deliberately never includes column-level schema - only enough for the
    router to guess relevance from table/tab names and dialect. Calls
    get_database_schema(..., deep=False) - the Phase 1-only ("shallow")
    fetch - rather than the deep fetch every real generation path uses,
    so an all-dbs question against N in-scope connections doesn't pay
    Phase 2's live-query cost (sampling, min/max, live row counts, ...)
    for the N-1 connections the router doesn't end up selecting; a
    connection that IS selected gets its schema re-fetched deep, through
    the normal get_database_schema() call Phase B already makes, at which
    point it's a fresh cache lookup under a different key (see
    get_database_schema()'s cache_key suffixing) - not reused from here.
    Reduced via backends/base.py's extract_entry_names_from_schema_text,
    same as before this split existed.

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
        schema_text = get_database_schema(entry["descriptor"], user_id, deep=False)
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
    return _to_descriptor(DEFAULT_DESCRIPTOR)


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
    """Logs "All Pre-Configured Datasets" mode's Phase A (triage) step to the same
    translations table record_translation() writes to, but tagged with the
    literal database_type/database_name "All Pre-Configured Datasets"/"All Preset
    Datasets" rather than any real connection descriptor - unlike every
    other row in this table, a triage call isn't "about" one specific
    database at all (it's the step that decides whether real data is even
    needed, and if so, which connection(s) to route to), so there's no
    real descriptor to resolve a db_type/db_name from the way
    record_translation() does above.

    Deliberately bypasses record_translation()'s _to_descriptor/
    _resolve_database_name resolution entirely rather than trying to feed
    it a synthetic descriptor - "All Pre-Configured Datasets" is a fixed, literal
    label, not a lookup result.

    Called once per "All Pre-Configured Datasets" request regardless of triage's
    outcome (answer/failed/route - see translate_routes.py's
    router_only_all_mode branch), always with ONLY triage's own duration
    and LLM token usage - never folded in with any Phase B (per-database
    generation) numbers, so a "route" outcome's real, per-database
    translations-table row (logged separately, attributed to that specific
    connection) never double-counts the tokens/time this row already
    accounts for."""
    state_store.record_translation(
        user_id, "All Pre-Configured Datasets", "All Pre-Configured Datasets", nl_prompt, sql_command, gemini_model,
        duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens
    )


def get_db_connection(conn_str=None, user_id=None):
    descriptor = resolve_conn_str(conn_str, user_id)
    return get_backend(descriptor).connect(descriptor)


# Suffix appended to a connection's cache_key for the shallow (Phase 1
# only, catalog-only) schema-cache entry - kept independent of the plain
# cache_key (the deep/full-schema entry, unchanged from before this split
# existed) so a connection can have both cached at once, and so
# invalidating one doesn't accidentally read/clear the other. See
# invalidate_schema_cache() below, which clears both together - the only
# correct way to invalidate a connection's schema, now that there can be
# two entries for it.
_SHALLOW_CACHE_KEY_SUFFIX = "::shallow"


def get_database_schema(conn_str=None, user_id=None, force_refresh=False, deep=True):
    """
    Returns the schema introspection text for the resolved connection,
    using an in-memory cache (see schema_cache.py) so repeated
    /api/translate calls in the same chat session - or across an entire
    process's lifetime - don't re-run the backend's introspection queries
    every time.

    Every successful fetch is cached indefinitely (schema_cache.py has no
    TTL/expiry concept at all - see its own module docstring): a
    connection's schema only ever changes here via force_refresh=True (an
    explicit "fetch this now" request - the startup preset prefetch, the
    "Refresh Schema" button, or /api/translate's own in-conversation
    refresh_schema checkbox - see prime_schema_cache()/
    prefetch_all_preset_schemas() below and config_routes.py's
    /api/config/refresh-schema) or a process restart. force_refresh=True
    bypasses the cached read and re-fetches, and that fresh result is
    cached indefinitely too, exactly the same as any other successful
    fetch - there's no "temporary" cache tier to fall back to.

    deep=True (default - every pre-existing caller keeps getting exactly
    this) returns the full Phase 1 + Phase 2 ("deep") schema text, cached
    under this connection's plain cache_key exactly as before this
    parameter existed. deep=False returns the Phase 1-only ("shallow")
    text instead, cached separately under cache_key + "::shallow" - used
    by build_router_candidate_summaries() so an all-dbs triage pass over
    every in-scope connection doesn't pay Phase 2's live-query cost for
    connections the router may never actually select.

    Deliberately calls _fetch_database_schema() (the plain-text function),
    NOT _fetch_database_schema_with_reason() - this keeps
    _fetch_database_schema() as the one and only real-fetch seam every
    existing test monkeypatches (tests/server/test_connection_router.py,
    among others) to stand in for a real backend round-trip. Only
    get_database_schema_with_reason() (below) - used solely by
    prime_schema_cache_with_reason(), which nothing here monkeypatches
    this way - goes through the reason-returning path instead. Keeping
    these as two independent implementations (rather than one delegating
    to the other) duplicates a handful of cache-read/cache-write lines,
    but that's a deliberately small price for not silently routing every
    existing caller/test through a lower-level seam they were never
    written against.
    """
    descriptor = resolve_conn_str(conn_str, user_id)
    cache_key = get_conn_identifier(descriptor)
    if not deep:
        cache_key += _SHALLOW_CACHE_KEY_SUFFIX

    if not force_refresh:
        cached = schema_cache.get(cache_key)
        if cached is not None:
            return cached

    schema_text = _fetch_database_schema(descriptor, deep=deep)
    # Don't cache the failure fallback - a transient connection hiccup
    # shouldn't get "frozen in" as the answer forever just because the DB
    # happened to be unreachable at the moment of this one fetch; the
    # very next attempt (whenever that happens to be) tries live again.
    if schema_text != _SCHEMA_FETCH_FAILED:
        schema_cache.set(cache_key, schema_text)
    return schema_text


def get_database_schema_with_reason(conn_str=None, user_id=None, force_refresh=False, deep=True):
    """
    Same caching behavior get_database_schema() above documents (see its
    docstring - identical cache-key/TTL/deep-vs-shallow semantics), but
    also returns WHY a failed fetch failed, as a (schema_text, reason)
    pair: reason is None on a cache hit or a successful fetch, else
    SCHEMA_FETCH_FAILURE_REASON_EMPTY, _TIMEOUT, or _FATAL
    (see _fetch_database_schema_with_reason()'s own docstring for exactly
    what each means). Only prime_schema_cache_with_reason() below currently
    calls this - every other caller uses the plain get_database_schema()
    above, which goes through the separate, reason-less
    _fetch_database_schema() seam instead (see that function's own comment
    on why these two aren't just one delegating to the other).
    """
    descriptor = resolve_conn_str(conn_str, user_id)
    cache_key = get_conn_identifier(descriptor)
    if not deep:
        cache_key += _SHALLOW_CACHE_KEY_SUFFIX

    if not force_refresh:
        cached = schema_cache.get(cache_key)
        if cached is not None:
            return cached, None

    schema_text, reason = _fetch_database_schema_with_reason(descriptor, deep=deep)
    # Don't cache the failure fallback - same reasoning as
    # get_database_schema() above.
    if schema_text != _SCHEMA_FETCH_FAILED:
        schema_cache.set(cache_key, schema_text)
    return schema_text, reason


def prime_schema_cache(descriptor, user_id=None):
    """Force-fetches BOTH the deep and shallow schema cache entries for
    one connection - thin wrapper around prime_schema_cache_with_reason()
    (below) for the two pre-existing callers that only ever needed a bare
    success/failure signal (prefetch_all_preset_schemas, and
    config_routes.py's own-connection-config-changed branch), discarding
    the failure reason. See that function's docstring for the full
    deep+shallow/success semantics, unchanged here."""
    success, _reason = prime_schema_cache_with_reason(descriptor, user_id)
    return success


def prime_schema_cache_with_reason(descriptor, user_id=None):
    """Same deep+shallow force-fetch prime_schema_cache() above documents,
    shared by the startup preset prefetch (prefetch_all_preset_schemas,
    below), the connection-config-changed branch, and the "Refresh Schema"
    endpoint (config_routes.py's /api/config/refresh-schema) - but also
    returns a failure reason, as a (success, reason) pair: reason is None
    on success, else whatever get_database_schema_with_reason()'s deep
    fetch reported (SCHEMA_FETCH_FAILURE_REASON_EMPTY, _TIMEOUT, or _FATAL
    - see its own docstring). /api/config/refresh-schema reads this to
    tell a user something more specific than a blanket "could not fetch
    schema" when the connection actually worked fine but simply has
    nothing to describe; prefetch_all_preset_schemas() below reads it to
    decide whether a preset that failed at startup should stay in the
    dialog's list (EMPTY/TIMEOUT) or be dropped from it until the next
    restart (FATAL) - see that function's own docstring.

    Both are fetched (not just deep) because build_router_candidate_summaries()
    (all-dbs triage) uses the shallow entry - leaving it stale after an
    explicit refresh would mean triage still sees the OLD schema even
    though a real generation call would now see the new one.

    success is True if the (user-visible) deep fetch succeeded, False if
    it hit the _SCHEMA_FETCH_FAILED fallback - callers use this to decide
    success/failure. The shallow fetch is best-effort and only attempted
    if the deep fetch succeeded; a shallow-only failure never downgrades
    an otherwise-successful deep fetch back to an overall failure (and
    never produces its own reason - only the deep fetch's outcome is ever
    reported)."""
    deep_text, reason = get_database_schema_with_reason(descriptor, user_id, force_refresh=True, deep=True)
    success = deep_text != _SCHEMA_FETCH_FAILED
    if success:
        get_database_schema(descriptor, user_id, force_refresh=True, deep=False)
    return success, (reason if not success else None)


def prefetch_all_preset_schemas():
    """Warms every admin-configured preset's schema cache at server
    startup - called once, at module import time, from server.py (same
    precedent as state_store.init() - see that module's own comment on
    why it runs there rather than only under `if __name__ == '__main__':`),
    on its own background thread (see server.py's own comment on that
    call site) so it never blocks the server from serving requests.
    Presets have no per-user credentials (CONFIGURED_DBS entries are
    already complete, credentialed descriptors - see
    resolve_active_descriptor's preset branch above), so user_id is
    irrelevant here - None throughout.

    Fetches concurrently (ThreadPoolExecutor), mirroring
    build_router_candidate_summaries()'s existing pattern, so N slow/
    unreachable presets prefetch in parallel rather than serializing
    behind each other and delaying server startup further. This function
    never raises, so one bad preset can't take any other preset down with
    it.

    A preset's failure is handled differently depending on
    prime_schema_cache_with_reason()'s reported reason - see
    SCHEMA_FETCH_FAILURE_REASON_TIMEOUT/_FATAL's own comment for the full
    reasoning, summarized here:

    - EMPTY or TIMEOUT: logged and left alone, exactly as before this
      distinction existed. Since every successful fetch is cached
      indefinitely regardless of how it was triggered (see
      get_database_schema() above), the very next real request against
      this preset - once its DB is reachable again, or its schema
      actually has tables to describe - fetches and caches it exactly as
      if this prefetch had succeeded in the first place, with no separate
      retry/pending bookkeeping needed.
    - FATAL: this preset gets a definitive, fast rejection (bad
      credentials, a missing driver, a malformed request, ...) that a
      retry can't fix without an admin actually changing something - so
      unlike EMPTY/TIMEOUT, leaving it in the dialog's list would just
      mean every future request against it repeats the exact same
      failure forever. Instead it's marked via _mark_preset_fatally_failed()
      and disappears from visible_configured_dbs() - the filtered view
      resolve_active_descriptor/resolve_descriptor_by_reference/
      _resolve_all_configured_descriptors above and config_routes.py's
      preset-listing/selection code all read instead of CONFIGURED_DBS
      directly - until the next server restart re-runs this whole
      function from scratch and gives it another chance. A session
      already pointed at a preset that gets excluded this way resolves as
      "missing" (see resolve_active_descriptor's own docstring) the very
      next time it's resolved - which happens fresh on every request, not
      from anything cached - and gracefully falls back to the default
      connection, surfaced to the frontend via the existing
      active_connection_missing/_message fields (config_routes.py's GET
      /api/config handler) exactly as if the preset had been removed from
      DATABASE_PRESETS_FILE entirely. This is what closes the race where a
      user selects a preset (or already has it as their active
      connection) moments before prefetch determines it's fatal: nothing
      about that selection is trusted as still valid without re-checking
      visible_configured_dbs() fresh, so it can never wedge a user onto a
      connection that's since been excluded - the one gap that remains is
      a request that lands in the brief window before prefetch has
      reached this preset AT ALL, which gets one honest, live failed
      fetch instead of a clean "unavailable" message; that's no worse than
      what every connection's very first use has always risked, and
      isn't something prefetch running in the background can fully close
      without going back to blocking startup on every preset finishing
      first (see server.py's own comment on why that tradeoff was made).

    Custom connections are deliberately NOT covered here - see this
    task's plan for why ("presets only" was the chosen scope): each
    user's own custom connections still warm up lazily on first use,
    same as before this existed, and get cached indefinitely from that
    first successful fetch onward, same as everything else. (They also
    have no equivalent of this FATAL-exclusion behavior - a custom
    connection that fails is just left as the user's own problem to fix
    via "Refresh Schema", same as always; there's no shared "list of
    presets" for an individual custom connection to be removed from.)"""
    if not CONFIGURED_DBS:
        return

    def _prefetch_one(db):
        descriptor = {k: v for k, v in db.items() if k not in ("id", "name")}
        preset_label = db.get("name") or db.get("id")
        try:
            ok, reason = prime_schema_cache_with_reason(descriptor, user_id=None)
            if ok:
                return
            if reason == SCHEMA_FETCH_FAILURE_REASON_FATAL:
                _mark_preset_fatally_failed(db.get("id"))
                logger.warning(
                    "Startup schema prefetch failed for preset %r with a fatal "
                    "(non-timeout) error - removing it from the list of presets "
                    "in the DB connections dialog until the next server restart. "
                    "See the error logged just above this line for the actual "
                    "cause (bad credentials, a missing driver, a malformed "
                    "request, ...).",
                    preset_label,
                )
            else:
                logger.warning(
                    "Startup schema prefetch failed for preset %r (%s) - it "
                    "will be fetched (and cached) on its first successful real "
                    "request instead",
                    preset_label, reason or "unknown reason",
                )
        except Exception:
            # prime_schema_cache_with_reason() itself isn't expected to
            # raise (its own try/except already turns a fetch failure into
            # a (False, reason) pair) - this is a last-resort net for
            # something going wrong OUTSIDE that (e.g. a bug in this
            # function's own bookkeeping), so it's treated the same, safe
            # way EMPTY/TIMEOUT is: logged and left in the list rather than
            # assumed fatal, since it's not something SCHEMA_FETCH_FAILURE_
            # REASON_FATAL was ever actually able to confirm.
            logger.exception(
                "Error during startup schema prefetch for preset %r - it will be "
                "fetched (and cached) on its first successful real request instead",
                preset_label,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CONFIGURED_DBS)) as pool:
        list(pool.map(_prefetch_one, CONFIGURED_DBS))


def invalidate_schema_cache(cache_key):
    """Drops BOTH of a connection's schema-cache entries (deep and
    shallow) for the given plain cache_key (i.e. get_conn_identifier's
    return value, with no "::shallow" suffix - callers always pass the
    plain form, this function adds the suffix itself). Use this instead
    of calling schema_cache.invalidate(cache_key) directly wherever a
    connection's schema is known to have changed (e.g.
    config_routes.py's own-connection-edited path) - invalidating only
    the plain key would leave a stale shallow entry cached, now
    indefinitely (schema_cache.py has no TTL/expiry at all - see its own
    module docstring), rather than just for a bounded window, since the
    two entries are otherwise completely independent (see
    get_database_schema()'s own cache_key suffixing above)."""
    schema_cache.invalidate(cache_key)
    schema_cache.invalidate(cache_key + _SHALLOW_CACHE_KEY_SUFFIX)


def _fetch_database_schema(descriptor, deep=True):
    """The actual DB-hitting introspection logic - thin wrapper around
    _fetch_database_schema_with_reason() (below) for every caller that
    only wants the schema text itself and doesn't care why a failed fetch
    failed (which is every existing caller as of this writing: a real
    /api/translate schema fetch always just wants the text, or the
    _SCHEMA_FETCH_FAILED placeholder either way). Always fetches live -
    call get_database_schema() instead unless you specifically need to
    bypass the cache layer."""
    schema_text, _reason = _fetch_database_schema_with_reason(descriptor, deep=deep)
    return schema_text


def _fetch_database_schema_with_reason(descriptor, deep=True):
    """Same introspection logic _fetch_database_schema() above documents
    (deep=True: full Phase 1 + Phase 2 backend.get_schema(); deep=False:
    Phase 1-only backend.get_schema_shallow(), used by
    build_router_candidate_summaries() below) but also returns WHY a
    failed fetch failed, as a (schema_text, reason) pair: reason is None
    on success, else SCHEMA_FETCH_FAILURE_REASON_EMPTY (connected and
    queried fine, there's just nothing to describe - e.g. a views-only
    schema, a genuinely empty database, or a role with no table-level
    privileges - see the "no schema text" warning below), or - when the
    connect()/get_schema() call itself raised -
    SCHEMA_FETCH_FAILURE_REASON_TIMEOUT (the attempt hit
    DB_CONNECT_TIMEOUT_SECONDS or an equivalent read timeout without ever
    getting a definitive answer - see _looks_like_timeout_error() above)
    or SCHEMA_FETCH_FAILURE_REASON_FATAL (any other raised error - a
    definitive rejection: bad credentials, a missing driver, a malformed
    request, a real query error, ...).

    schema_text is always exactly _SCHEMA_FETCH_FAILED's plain placeholder
    string on failure regardless of which reason produced it - this
    function changes nothing about what ever gets embedded in a prompt or
    cached; the reason is purely additive metadata for
    get_database_schema_with_reason()/prime_schema_cache_with_reason() to
    hand up to a caller (today, only config_routes.py's
    /api/config/refresh-schema) that wants to tell a user something more
    specific than a blanket "could not fetch schema"."""
    backend = get_backend(descriptor)
    connection = None
    try:
        connection = backend.connect(descriptor)
        schema_text = backend.get_schema(connection) if deep else backend.get_schema_shallow(connection)
        if not schema_text:
            # get_schema() returning None/"" is a normal, NON-exceptional
            # return value for every backend (see e.g. backends/postgres.py's
            # own "if not all_table_names: return None" - the connection
            # succeeded and the query ran fine, there just wasn't a base
            # table to describe), so this used to fall through to
            # _SCHEMA_FETCH_FAILED completely silently - no logger.exception
            # call is reached on this path at all, unlike a real connection/
            # query error just below. That made "why does this one
            # connection always show 'No schema description available.'"
            # unanswerable from the logs alone (see this function's own
            # history: a schema made up entirely of views, with zero BASE
            # TABLEs, hit exactly this path with nothing recorded anywhere).
            # A warning here doesn't change the returned fallback text at
            # all - it just means the next time this happens, the log says
            # which connection and why, instead of nothing.
            logger.warning(
                "Schema fetch for %s returned no schema text (get_schema() "
                "gave back %r) - no exception was raised, so this is likely "
                "a schema with no BASE TABLEs (e.g. views-only) rather than "
                "a connection/query failure.",
                get_conn_identifier(descriptor), schema_text,
            )
            return _SCHEMA_FETCH_FAILED, SCHEMA_FETCH_FAILURE_REASON_EMPTY
        else:
            # The two checks below are independent, not mutually exclusive -
            # a schema can have more tables than SCHEMA_MAX_TABLES allows
            # AND still overflow SCHEMA_MAX_CHARS with just the tables it
            # DID keep, so both get checked (and, rarely, could both fire)
            # rather than treating one as ruling out the other.
            if schema_text_has_omitted_tables(schema_text):
                # Every backend's own "N more table(s)... not shown" note
                # (see e.g. backends/postgres.py's "if omitted_count:"
                # block) already tells the model directly, in-prompt, that
                # some tables were left out entirely - but until now that
                # was the only place it was visible: nothing recorded which
                # connection actually exceeds SCHEMA_MAX_TABLES, or how
                # often. Same visibility-only fix as the two warnings
                # around it - the returned schema_text is unchanged.
                logger.warning(
                    "Schema text for %s omits at least one table/table-family "
                    "because it exceeds the SCHEMA_MAX_TABLES limit (%d) - the "
                    "model is not seeing this connection's full table list. If "
                    "this happens often, raise SCHEMA_MAX_TABLES for this "
                    "connection's dataset.",
                    get_conn_identifier(descriptor), SCHEMA_MAX_TABLES,
                )
            if schema_text_was_truncated(schema_text):
                # cap_schema_text() (backends/base.py) already embeds a
                # truncation note directly in the prompt text itself, so the
                # model always sees it - but until now that was the ONLY
                # place it was visible: nothing recorded which connection
                # actually hits the SCHEMA_MAX_CHARS ceiling, or how often.
                # This doesn't change what's returned (still the same,
                # already-truncated schema_text) - it's purely a visibility
                # fix, same spirit as the "no schema text" warning above, so
                # "is this connection's schema actually getting cut off" is
                # answerable from server logs instead of only by noticing
                # the note buried in a model response.
                logger.warning(
                    "Schema text for %s was truncated to fit SCHEMA_MAX_CHARS "
                    "(%s characters) - the model is not seeing this "
                    "connection's full schema. If this happens often, raise "
                    "SCHEMA_MAX_SCHEMA_CHARS or narrow this connection's "
                    "SCHEMA_MAX_TABLES scope.",
                    get_conn_identifier(descriptor), f"{SCHEMA_MAX_CHARS:,}",
                )
        return schema_text, None
    except Exception as exc:
        logger.exception("Error fetching schema")
        reason = (
            SCHEMA_FETCH_FAILURE_REASON_TIMEOUT if _looks_like_timeout_error(exc)
            else SCHEMA_FETCH_FAILURE_REASON_FATAL
        )
        return _SCHEMA_FETCH_FAILED, reason
    finally:
        if connection:
            backend.close(connection)