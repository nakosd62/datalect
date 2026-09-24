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
import datetime
import json
import re
import threading

from app_config import DEFAULT_DESCRIPTOR, CONFIGURED_DBS, CONFIGURED_DB_GROUPS, DATABASE_PRESETS_FILE, state_store, logger
from backends import get_backend
from backends.base import (
    extract_entry_names_from_schema_text, schema_text_was_truncated, schema_text_has_omitted_tables,
    parse_dataset_size_line, SCHEMA_MAX_CHARS, SCHEMA_MAX_TABLES, SCHEMA_SIZE_CHARS_PER_TOKEN,
    quantize_schema_size_tokens,
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
    _resolve_group_configured_descriptors, and config_routes.py's preset-
    listing/preset-selection code in its GET/POST /api/config handler.

    Deliberately NOT used by call sites that need to resolve a preset for
    HISTORICAL purposes regardless of its current visibility (e.g.
    chat_history_routes.py labeling which preset a past translation ran
    against) - those still read CONFIGURED_DBS directly, since a preset
    excluded today shouldn't erase which one a past request actually used.

    A plain filter over the live CONFIGURED_DBS list (not a cached/
    snapshotted copy) for the same reason _resolve_group_configured_descriptors
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

    session["in_scope_mode"] == "group" (see StateStore.get_session's
    docstring) takes a completely different path here - see
    _resolve_group_configured_descriptors below - ignoring
    in_scope_preset_ids/in_scope_custom_connection_keys entirely in favor
    of session["in_scope_group_id"]'s own fixed, admin-curated
    "dataset_list" (app_config.py's CONFIGURED_DB_GROUPS - presets only,
    see that function's own docstring for why custom connections can never
    be a group member). Every other mode (the default "single", and any
    legacy session that saved an arbitrary multi-connection subset before
    the binary single/group choice existed) resolves the explicit
    in_scope_preset_ids/in_scope_custom_connection_keys lists below,
    exactly as this function always has - that legacy explicit-subset path
    CAN still include custom connections, since it's a user-picked list,
    not a group.

    A reference that no longer resolves (resolve_descriptor_by_reference
    returned None - a removed preset, a deleted custom connection) is
    silently skipped, same leniency resolve_active_descriptor already
    applies to a single stale connection_id. Falls back to a single
    app-default entry only if EVERY reference fails to resolve, or the
    in-scope set is empty to begin with (a brand-new session, or one that
    predates this feature and has never explicitly saved a connection at
    all) - this is what guarantees the result is never empty, so callers
    never need their own separate empty-list fallback."""
    if session.get("in_scope_mode") == "group":
        return _resolve_group_configured_descriptors(session.get("in_scope_group_id") or "", user_id)
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


def _resolve_group_configured_descriptors(group_id, user_id):
    """A dataset group (see webClient/client.js's renderDbRadioButtons()
    and app_config.py's own "DATASET GROUPS" comment) - the candidate pool
    for a session in in_scope_mode == "group": every preset id listed in
    that group's "dataset_list", looked up fresh in CONFIGURED_DB_GROUPS on
    every call (so a presets-file change since this session last saved its
    in_scope_group_id is immediately reflected, same "read live" property
    _resolve_group_configured_descriptors used to document for the old "all
    mode" this replaces).

    There is no dynamic "every configured preset" pool any more - a group
    only ever contains exactly the presets an admin explicitly listed in
    its "dataset_list" (app_config.py already dropped any id that didn't
    resolve to a real preset when CONFIGURED_DB_GROUPS was built, so every
    id read here is already known-valid). A group_id that no longer
    resolves to anything in CONFIGURED_DB_GROUPS (removed/renamed from
    DATABASE_PRESETS_FILE since this session picked it) falls through to
    the same single-default-entry fallback as an empty group.

    Deliberately PRESETS ONLY, never this user's own custom connections -
    a group's "dataset_list" can only ever name other entries in this same
    presets file (app_config.py's own validation enforces this at load
    time), so there's no separate "exclude custom connections" step needed
    here the way the old all-mode resolver had to document. Each member
    preset is resolved via resolve_descriptor_by_reference exactly like the
    explicit-list branch in resolve_in_scope_descriptors above, so a preset
    that (implausibly, mid-request) stops resolving is silently skipped the
    same way, not a special case. Falls back to the single app-default
    entry if there's nothing left in the candidate pool - group_id didn't
    match any configured group, or every one of its members failed to
    resolve."""
    group = next((g for g in CONFIGURED_DB_GROUPS if g.get("id") == group_id), None)
    entries = []
    if group:
        for preset_id in group.get("dataset_list") or []:
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
    router to guess relevance from table/tab names and dialect.

    Deliberately NEVER connects to or queries a real database, and never
    writes anything new to schema_cache.py. This reads ONLY the
    already-cached DEEP schema entry for each in-scope connection (a
    plain schema_cache.get(cache_key) - the very same durable entry a real
    /api/translate call would use) and reduces it in-memory via
    backends/base.py's extract_entry_names_from_schema_text. A connection
    whose deep schema hasn't been cached yet (never selected/used, or a
    preset whose startup prefetch hasn't finished) simply degrades to an
    empty table_names list for this one triage pass - it starts
    participating in triage the moment something else populates its deep
    cache entry (its own first real use, a preset prefetch, or an explicit
    "Refresh Schema"), same as a genuine fetch failure already degraded to
    [] before this change.

    This intentionally does NOT try to reconstruct a true Phase-1-only
    ("shallow") subset of the cached text - the deep entry's Phase 2
    sections (view/routine bodies, live row counts, sampling, ...) are
    simply left in and ignored by extract_entry_names_from_schema_text's
    heading-only regex, and whatever SCHEMA_MAX_CHARS truncation already
    applied to the deep entry applies here too, as-is. There used to be a
    genuinely separate, independently-fetched-and-cached "shallow" cache
    entry (cache_key + "::shallow") specifically for this function, so an
    all-dbs question wouldn't pay Phase 2's live-query cost per candidate
    connection - but since the deep text was always a superset of that
    Phase 1-only text anyway (same backends/base.py-shared two-phase
    design every dialect follows), fetching and caching it separately was
    pure waste: this reads the deep entry that's already sitting in the
    cache instead, for free, with zero live queries of its own. See
    get_schema_shallow() on each Backend subclass (still implemented,
    still exercised by real tests) and the old ::shallow cache-key suffix
    handling in get_database_schema()/get_database_schema_with_reason()
    below - both left in place, unused by this function now, in case a
    genuine independent shallow fetch is ever needed again for some other
    purpose."""
    if not in_scope_entries:
        return []

    def _summarize(entry):
        cache_key = get_conn_identifier(entry["descriptor"])
        schema_text = schema_cache.get(cache_key)
        table_names = extract_entry_names_from_schema_text(schema_text) if schema_text else []
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


def build_group_schema_summaries(group_id, user_id):
    """Builds the dataset-group Schema Viewer's own table (see webClient/
    client.js's openGroupSchemaViewer()/loadGroupSchemaViewer()) - one
    {"id", "name", "type", "data_size", "schema_size_tokens", "available"}
    dict per preset in the group's own "dataset_list", in that same order
    (app_config.py's CONFIGURED_DB_GROUPS is looked up fresh on every call,
    same "read live" property _resolve_group_configured_descriptors above
    documents).

    Returns None (not an empty list) when group_id doesn't match any
    configured group at all - the caller (config_routes.py's
    handle_get_group_schema()) treats that as a 404, distinct from a real,
    configured group that simply has no valid members left (an empty list),
    since those mean genuinely different things to the person who just
    clicked a specific group's own "i" icon. Deliberately does NOT fall
    back to the single app-default entry the way
    _resolve_group_configured_descriptors does for query routing - that
    fallback exists so a query always has SOME real connection to run
    against, but showing a fabricated "Default connection" row in a
    dataset-group's own schema table would misrepresent what's actually in
    it.

    Unlike build_router_candidate_summaries() above, this DOES call
    get_database_schema() (not a bare schema_cache.get() read) for each
    member - deliberately: that function fetches-and-caches on a cache
    miss exactly like GET /api/schema (config_routes.py's
    handle_get_schema()) already does for a single connection, so opening
    this dialog for a group whose members haven't all been prefetched yet
    (or one added to DATABASE_PRESETS_FILE after this server's own startup
    prefetch already ran) still fills in real numbers rather than leaving
    permanent blanks - a deliberate, occasional dialog open is a
    reasonable moment to pay a real fetch's cost, unlike Phase A triage
    (every single question), which is exactly why that function documents
    NOT doing this. Every member is still fetched concurrently (same
    ThreadPoolExecutor pattern as build_router_candidate_summaries above)
    so a slow/unreachable member doesn't serialize behind the others.

    "data_size" mirrors the Schema Viewer's own "Data Size: ..." fact for a
    single connection (backends/base.py's format_dataset_size_line()/
    parse_dataset_size_line() - the best-effort, schema-wide catalog
    estimate each dialect's deep get_schema() embeds, never a live scan) -
    None for a dialect with no cheap source for it (see backends/sheets.py,
    backends/mongodb_sql.py) or a member whose fetch failed outright.
    "schema_size_tokens" mirrors that same viewer's "Schema Size: ...
    tokens" fact - len(schema_text) / SCHEMA_SIZE_CHARS_PER_TOKEN, then
    quantized UP to the nearest SCHEMA_SIZE_TOKEN_QUANTUM (see
    quantize_schema_size_tokens()), the exact same flat approximation
    client.js's own facts line uses (see those constants' own comments in
    backends/base.py for why this is shared rather than independently-
    tuned figures) - None for a member whose fetch failed outright (there
    is no schema text to measure).
    "available" is False only for that failed-fetch case - a genuinely
    reachable connection with nothing to describe (SCHEMA_FETCH_FAILURE_
    REASON_EMPTY, in get_database_schema_with_reason()'s terms - an empty
    schema) still counts as "available" here with a real (zero-ish)
    schema_size_tokens and no data_size line, since it was still fetched
    successfully; this function uses the reason-less get_database_schema()
    and so can't distinguish EMPTY from FATAL/TIMEOUT the way GET
    /api/schema's own error messaging does - "available": False here just
    means "nothing to show for this one row", the table's own equivalent
    of that route's error message, without needing the finer-grained
    reason a single-connection dialog's dedicated error text does."""
    group = next((g for g in CONFIGURED_DB_GROUPS if g.get("id") == group_id), None)
    if group is None:
        return None

    member_ids = group.get("dataset_list") or []
    resolved = []
    for preset_id in member_ids:
        descriptor, name = resolve_descriptor_by_reference("preset", preset_id, user_id)
        if descriptor is not None:
            resolved.append({"id": preset_id, "name": name, "descriptor": descriptor})

    def _summarize(entry):
        try:
            dialect = get_backend(entry["descriptor"]).dialect_name
        except Exception:
            dialect = "SQL"
        schema_text = get_database_schema(entry["descriptor"], user_id, deep=True)
        if not schema_text or schema_text == _SCHEMA_FETCH_FAILED:
            return {
                "id": entry["id"], "name": entry["name"], "type": dialect,
                "data_size": None, "schema_size_tokens": None, "available": False,
            }
        return {
            "id": entry["id"], "name": entry["name"], "type": dialect,
            "data_size": parse_dataset_size_line(schema_text),
            "schema_size_tokens": quantize_schema_size_tokens(len(schema_text) / SCHEMA_SIZE_CHARS_PER_TOKEN),
            "available": True,
        }

    results = [None] * len(resolved)
    if resolved:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(resolved)) as pool:
            future_to_index = {pool.submit(_summarize, entry): i for i, entry in enumerate(resolved)}
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception:
                    logger.exception("Error building group schema summary")
                    entry = resolved[index]
                    results[index] = {
                        "id": entry["id"], "name": entry["name"], "type": "SQL",
                        "data_size": None, "schema_size_tokens": None, "available": False,
                    }
    return {"id": group.get("id"), "name": group.get("name"), "datasets": results}


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


def resolve_dataset_identity(conn_str, user_id=None):
    """(dataset_type, dataset_name) for a single connection - descriptor or
    legacy raw string, same as get_conn_identifier/_resolve_database_name
    above accept - e.g. ("postgres", "E-Commerce Store"). This is exactly
    the same (db_type, db_name) pair record_translation below has always
    resolved and logged for the "translations" table, pulled out into its
    own small public helper so record_llm_usage's own callers (triage,
    Phase B sqlgen fan-out, summary) can tag a usage row with the same
    human-readable dataset identity without duplicating this lookup or
    reaching into _resolve_database_name (a private helper) directly."""
    descriptor = _to_descriptor(conn_str)
    db_type = (descriptor or {}).get("type") or "postgres"
    db_name = _resolve_database_name(descriptor, user_id)
    return db_type, db_name


def resolve_group_identity(group_id):
    """(dataset_type, dataset_name) for a dataset GROUP - the "all
    databases" mode counterpart to resolve_dataset_identity above, for a
    call (triage's own multi-candidate call, or Phase C's multi-database
    summary call) that's attributable to a whole configured group rather
    than any one connection. dataset_type is always the fixed marker
    "Dataset Group" (matching webClient/client.js's own getBadgeTypeLabel
    - the header badge's identical "(Dataset Group)" suffix for a group-
    mode session), never a dialect name, since a group can span several
    dialects at once. dataset_name is the group's own configured "name"
    (e.g. "Sports"), or the same "Dataset Group" fallback when group_id
    doesn't match any configured group (removed/renamed mid-session - see
    _resolve_group_configured_descriptors' own docstring for this same
    "stale group_id" case)."""
    group = next((g for g in CONFIGURED_DB_GROUPS if g.get("id") == group_id), None)
    name = (group or {}).get("name") or "Dataset Group"
    return "Dataset Group", name


def record_translation(user_id, conn_str, nl_prompt, sql_command, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens):
    db_type, db_name = resolve_dataset_identity(conn_str, user_id)
    state_store.record_translation(
        user_id, db_type, db_name, nl_prompt, sql_command, gemini_model,
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
    using a durable cache (see schema_cache.py, backed by state_store.py -
    SQLite locally, Firestore on Cloud Run) so repeated /api/translate
    calls in the same chat session - or across an entire connection's
    lifetime, restarts and Cloud Run instances included - don't re-run the
    backend's introspection queries every time.

    Every successful fetch is cached indefinitely (schema_cache.py has no
    TTL/expiry concept at all - see its own module docstring): a
    connection's schema only ever changes here via force_refresh=True (an
    explicit "fetch this now" request - the startup preset prefetch, the
    "Refresh Schema" button, or /api/translate's own in-conversation
    refresh_schema checkbox - see prime_schema_cache()/
    prefetch_all_preset_schemas() below and config_routes.py's
    /api/config/refresh-schema) or invalidate_schema_cache() being called
    on it. force_refresh=True bypasses the cached read and re-fetches, and
    that fresh result is cached indefinitely too, exactly the same as any
    other successful fetch - there's no "temporary" cache tier to fall
    back to.

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
    """Force-fetches the deep schema cache entry for one connection - thin
    wrapper around prime_schema_cache_with_reason() (below) for the two
    pre-existing callers that only ever needed a bare success/failure
    signal (prefetch_all_preset_schemas, and config_routes.py's
    own-connection-config-changed branch), discarding the failure reason.
    See that function's docstring for the full semantics, unchanged here."""
    success, _reason = prime_schema_cache_with_reason(descriptor, user_id)
    return success


def prime_schema_cache_with_reason(descriptor, user_id=None):
    """Same deep force-fetch prime_schema_cache() above documents, shared
    by the startup preset prefetch (prefetch_all_preset_schemas, below),
    the connection-config-changed branch, and the "Refresh Schema"
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

    Only the deep entry is force-fetched here. This used to also force-
    fetch a second, independent "shallow" cache entry (deep entries and
    that old shallow entry were fetched and cached completely separately,
    under different cache_key suffixes) purely so
    build_router_candidate_summaries() (all-dbs triage) would see fresh
    data after a refresh - but that function no longer reads (or causes)
    any independent shallow fetch at all: it derives its compact,
    table-name-only summary directly from whichever deep entry is already
    sitting in the cache, in-memory, with no fetch or cache write of its
    own (see that function's own docstring). So refreshing the deep entry
    here is now sufficient to keep triage fresh too - there is no separate
    shallow entry left to go stale.

    success is True if the (user-visible) deep fetch succeeded, False if
    it hit the _SCHEMA_FETCH_FAILED fallback - callers use this to decide
    success/failure.

    Also (best-effort, never affecting this function's own return value)
    regenerates this connection's cached schema OVERVIEW - a short LLM-
    written {"prose", "questions"} pair, see
    _generate_and_cache_schema_overview() below and schema_cache.py's own
    module docstring for the full design. Runs once per successful deep
    fetch here, alongside every one of this function's own three callers
    (startup prefetch, connection-config-changed, Refresh Schema), so the
    overview is always regenerated in lockstep with the schema text
    itself rather than needing its own separate trigger.

    Also brackets the whole attempt with schema_cache.mark_fetch_pending()/
    mark_fetch_done() (same cache_key a caller would compute via
    get_conn_identifier(descriptor) themselves - see that helper's own
    docstring) so a caller that doesn't want to block on this (config_routes.py's
    connection-config-changed branch, run on its own background thread since
    this can take several seconds) can instead poll
    schema_cache.is_fetch_pending()/get_last_fetch_error() to learn when it's
    done and how it went. mark_fetch_done() always runs, success or failure
    (including an unexpected exception), via `finally`, so a key can never
    get stuck reporting "pending" forever."""
    cache_key = get_conn_identifier(descriptor)
    schema_cache.mark_fetch_pending(cache_key)
    # Defaults to FATAL so an unexpected exception raised before `reason` is
    # even assigned below (a real bug, not a normal fetch failure) still
    # reports SOME failure rather than mark_fetch_done() silently clearing
    # a previous error or leaving the dot stuck - then re-raises past this
    # function exactly as before this change (this function has never caught
    # exceptions from its own two schema calls; that's unchanged).
    fetch_error = SCHEMA_FETCH_FAILURE_REASON_FATAL
    try:
        deep_text, reason = get_database_schema_with_reason(descriptor, user_id, force_refresh=True, deep=True)
        success = deep_text != _SCHEMA_FETCH_FAILED
        if success:
            _generate_and_cache_schema_overview(descriptor, user_id, deep_text)
        fetch_error = None if success else reason
        return success, (reason if not success else None)
    finally:
        schema_cache.mark_fetch_done(cache_key, error=fetch_error)


# --- Schema overview (prose + suggested questions) --------------------------
# Replaces the old per-PROMPT "*** NO SQL *** ... include an ER diagram
# using ascii art" convention (translate_routes.py's _COMMON_FORMAT_RULES)
# for a "what's in this dataset?"-style question: that used to cost a real
# LLM call on every single such chat turn, produced throwaway ASCII art
# because a chat reply had no better rendering option, and was never
# cached. Now that question just opens webClient's Schema Viewer (see
# _COMMON_FORMAT_RULES' current wording), whose new "Overview" entry shows
# this cached prose + a handful of suggested example questions, plus a
# real ER diagram - built client-side, deterministically, from the same
# Constraints/naming-convention-relationship data already parsed out of
# the plain schema text (see buildSchemaErDiagram() in client.js), NOT
# generated by an LLM at all, so it can never hallucinate a relationship
# or get a cardinality wrong the way free-form model output occasionally
# can. The LLM is only asked for the two things a deterministic pass
# genuinely can't produce well: a natural-language description of the
# dataset's likely purpose, and specific, interesting example questions.

_SCHEMA_OVERVIEW_SYSTEM_INSTRUCTION = (
    "You are analyzing a database schema to prepare a short overview for "
    "someone about to explore this dataset in a database exploration tool.\n"
    "Respond with ONLY a single JSON object - no markdown code fences, no "
    "text before or after it - of exactly this shape:\n"
    '{"prose": "<2-4 sentence plain-English description of what this '
    'dataset is likely about and anything structurally notable>", '
    '"questions": ["<question 1>", "<question 2>", "<question 3>", '
    '"<question 4>"]}\n'
    "\"prose\" should read naturally and describe the dataset's likely "
    "purpose/domain at a glance - NOT a table-by-table listing (the tables "
    "and their exact columns are already shown separately in this tool, so "
    "do not repeat them here).\n"
    "\"questions\" should contain 3 to 5 specific, genuinely interesting "
    "natural-language questions a user could actually ask about THIS "
    "dataset - referencing its real table/column names where it reads "
    "naturally - each one concrete enough that it could be translated "
    "into a real query, not generic questions that could apply to any "
    "database.\n"
    "Most of these should read like a precise, analytical request, but "
    "include AT LEAST ONE - and ideally two - that a real person would "
    "actually say out loud instead: casual, first-person, or "
    "exploratory in phrasing rather than a technical restatement of a "
    "table or column, while still being something this exact dataset "
    "could plausibly help answer. For example, for a movie-rental "
    "dataset, prefer \"which movie should I watch tonight?\" alongside "
    "something like \"list the top 5 highest-rated movies\"; for an "
    "e-commerce dataset, prefer \"how can I improve sales?\" alongside "
    "something like \"what are total sales by product category?\". Do "
    "not label, flag, or otherwise call out which questions are which - "
    "just mix them in naturally within the list.\n"
    "Write both fields in English regardless of the language any table/"
    "column names happen to use.\n"
)


def _resolve_overview_llm_call(user_id):
    """Picks the (provider, model, api_key) triple for the schema-overview
    LLM call (see _generate_and_cache_schema_overview() below) - deferred
    import of translate_routes.get_llm_provider (see that function's own
    docstring for why this must be a deferred, not top-level, import).

    user_id present (a real request - the "Refresh Schema" button, or the
    connection-config-changed branch, both always run inside a request
    with a resolved user identity) resolves the SAME provider/model that
    user's own chat turns already use (state_store.get_session(), the
    identical lookup /api/translate itself performs), so the overview's
    quality/cost tracks whatever model they've already chosen for
    everything else, including their own BYOK key if they've set one.

    user_id absent (the startup preset prefetch - prefetch_all_preset_schemas()
    always calls with user_id=None, since presets have no per-user
    identity to look one up for) has no session to read a preference from
    at all, so it falls back to this app's ONE fleet-wide default (see
    get_llm_provider(None)/LlmProvider.default_model - honors the
    DEFAULT_MODEL env var when set) - exactly the "no user involved, use
    DEFAULT_MODEL" behavior asked for."""
    from translate_routes import get_llm_provider
    if user_id:
        session_data = state_store.get_session(user_id)
        provider = get_llm_provider(session_data.get('llm_provider'))
        model = session_data.get('llm_model') or provider.default_model
        api_key = state_store.get_llm_byok_key(user_id, provider.name) or provider.pick_api_key()
    else:
        provider = get_llm_provider(None)
        model = provider.default_model
        api_key = provider.pick_api_key()
    return provider, model, api_key


def _parse_schema_overview_response(raw_text):
    """Parses the schema-overview LLM call's raw text response into
    {"prose": str, "questions": [str, ...]}, or None if it doesn't match
    that shape at all (a non-JSON reply, a refusal, a missing/blank
    "prose" field, ...) - _generate_and_cache_schema_overview() below
    treats None as "nothing to cache this time," never as an error to
    surface anywhere. Strips a wrapping ```json ... ``` markdown fence
    first, in case the model adds one despite being told not to - models
    do this even when explicitly instructed otherwise, the same real-
    world behavior _COMMON_FORMAT_RULES' own "do NOT surround the code
    block in markdown backticks" instruction exists to work around for
    actual SQL generation. "questions" defaults to an empty list (rather
    than failing the whole parse) if it's missing or malformed - a
    usable "prose" with no suggested questions is still worth caching."""
    text = (raw_text or "").strip()
    fence_match = re.match(r'^```(?:json)?\s*(.*?)\s*```$', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    prose = parsed.get("prose")
    if not isinstance(prose, str) or not prose.strip():
        return None
    questions = parsed.get("questions")
    if not isinstance(questions, list):
        questions = []
    questions = [q.strip() for q in questions if isinstance(q, str) and q.strip()]
    return {"prose": prose.strip(), "questions": questions}


def _generate_and_cache_schema_overview(descriptor, user_id, schema_text):
    """Best-effort: generates and caches a short LLM-written {"prose",
    "questions"} pair describing `schema_text` - see this section's own
    header comment and schema_cache.py's module docstring for the full
    design. Called once per successful deep fetch from
    prime_schema_cache_with_reason() above - never on a plain cache hit
    (get_database_schema() alone never calls this), so this is paid once
    per connection per explicit refresh, not once per chat turn.

    Deliberately swallows every failure (no API key configured for the
    resolved provider, a transient LLM error, a response that doesn't
    parse into the expected shape, ...) rather than raising: a schema
    refresh that successfully updated the real schema text must never be
    reported as failed to the user just because this purely-additive
    enhancement's own LLM call had a bad moment. A failure here simply
    leaves whichever overview (if any) was already cached in place -
    webClient's Schema Viewer treats a missing overview as "nothing to
    show yet," never as an error, so there's no user-visible harm in
    quietly retrying on the next refresh instead."""
    cache_key = get_conn_identifier(descriptor)
    try:
        provider, model, api_key = _resolve_overview_llm_call(user_id)
        if not api_key:
            return
        client = provider.make_client(api_key)
        schema_block = f"Database Schema:\n{schema_text}\n\n"
        llm_input = provider.build_llm_input([], schema_block, "Produce the JSON now.")
        raw_text, _usage = provider.call(client, model, llm_input, _SCHEMA_OVERVIEW_SYSTEM_INSTRUCTION)
        overview = _parse_schema_overview_response(raw_text)
        if overview is None:
            return
        overview["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        schema_cache.set_overview(cache_key, overview)
    except Exception:
        logger.exception("Schema overview generation failed for %s", cache_key)


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
      _resolve_group_configured_descriptors above and config_routes.py's
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

    Since schema_cache.py's schema-cache entries are durable (state_store-
    backed, surviving restarts and shared across every Cloud Run instance -
    see that module's own docstring), this function no longer unconditionally
    force-refreshes every preset on every single restart/redeploy - see
    _prefetch_one's own comment below for exactly why. In short: if a
    preset already has a durably-persisted schema (from an earlier run of
    this same process, or a previous server lifetime before whatever just
    restarted it), that's loaded and this preset is done - no live DB
    query, no schema-overview LLM call, paid again just because the
    process happened to restart. There is deliberately NO invalidate_schema_
    cache() call anywhere tied to editing DATABASE_PRESETS_FILE itself -
    unlike a user's own custom connection (whose config-modal Save
    explicitly invalidates on change - see config_routes.py's connection-
    changed branch), a preset is a static file this app only ever reads
    once, at startup (app_config.py builds CONFIGURED_DBS at import time,
    with no live-reload) - there's no runtime "this preset's definition
    just changed" event for anything to hook an invalidation onto. What
    actually happens on the next restart after editing a preset depends on
    WHICH fields changed, because this function's own durable-cache check
    above keys on cache_key = get_conn_identifier(descriptor), which is
    derived from a preset's real connection-identity fields (per dialect -
    e.g. Postgres/MySQL: user@host:port/dbname; BigQuery: project.dataset;
    see each backend's own cache_key() docstring), NEVER from the preset's
    "id" or "name" in DATABASE_PRESETS_FILE (those are stripped out before
    a cache_key is ever computed - see _prefetch_one below):
      - Editing a field that IS part of cache_key() (a different host,
        port, database, project, account, etc.) makes this preset resolve
        to a brand-new cache_key on the next restart - nothing durable
        exists yet under THAT key, so it's fetched live and cached fresh,
        functionally equivalent to an explicit invalidation even though
        none actually happened. The OLD cache_key's durable row is not
        deleted, just permanently orphaned (nothing will ever look it up
        again unless the preset is reverted) - harmless, but not cleaned
        up either.
      - Editing a field that is NOT part of cache_key() (credentials - a
        password, a service-account key; a CA cert; the preset's own "id"/
        "name"; ...) leaves the cache_key unchanged, so the existing durably-
        cached schema keeps being served as-is, un-refreshed, on the
        assumption that the underlying database itself hasn't changed
        (usually true - rotating a password doesn't change a schema). If
        the actual schema genuinely did change independently of any
        preset-definition edit at all (a column added, a table dropped),
        that's picked up only via the existing "Refresh Schema" button -
        deliberately: a restart is no longer treated as its own implicit
        refresh trigger, matching this cache's existing no-TTL, refetch-
        only-when-asked design everywhere else (see schema_cache.py's own
        module docstring), preferring the cost savings (real DB load and
        Gemini token spend, on every restart, forever) over an automatic
        refresh tied to server restarts.

    Custom connections are deliberately NOT covered here - see this
    task's plan for why ("presets only" was the chosen scope): each
    user's own custom connections still warm up lazily on first use,
    same as before this existed, and get cached indefinitely from that
    first successful fetch onward, same as everything else. (They also
    have no equivalent of this FATAL-exclusion behavior - a custom
    connection that fails is just left as the user's own problem to fix
    via "Refresh Schema", same as always; there's no shared "list of
    presets" for an individual custom connection to be removed from.)

    Deliberately a full no-op - no prefetch, no fatal-exclusion - when
    DATABASE_PRESETS_FILE isn't set at all. In that case CONFIGURED_DBS is
    never actually empty: app_config.py falls back to a single synthetic
    "Default DB" preset wrapping DEFAULT_CONN (a plain env-derived
    connection string, e.g. DATABASE_URL) purely so the connections dialog
    always has something to show. That placeholder is not an admin's
    deliberate preset choice - nothing "wrong" with it should ever
    permanently drop it from the list the way a genuinely bad admin-
    configured preset should. Concretely, this also fixes a real bug: the
    e2e suite runs with no DATABASE_PRESETS_FILE, so its synthetic default
    connection previously got schema-prefetched at startup like a real
    preset, hit a fast DNS-resolution failure against its intentionally
    bogus host (classified FATAL, not TIMEOUT - see
    SCHEMA_FETCH_FAILURE_REASON_FATAL above), and was excluded from
    visible_configured_dbs() before a single test ever ran - collapsing
    "configured_databases" to [] for the rest of that server process and
    failing every test that expected a selectable preset to exist."""
    if not DATABASE_PRESETS_FILE or not CONFIGURED_DBS:
        return

    def _prefetch_one(db):
        descriptor = {k: v for k, v in db.items() if k not in ("id", "name")}
        preset_label = db.get("name") or db.get("id")
        cache_key = get_conn_identifier(descriptor)
        try:
            # schema_cache.get() reads the durable store directly (see that
            # module's own docstring) - a non-None result here means
            # "something was durably saved for this preset already," from
            # an earlier server lifetime or another still-running Cloud Run
            # instance's own prefetch. Loading that instead of
            # force-refetching live is
            # the whole point of this function no longer unconditionally
            # calling prime_schema_cache_with_reason() below - see this
            # function's own docstring for the full reasoning. (There used
            # to also be a second, independent "shallow" cache entry warmed
            # here for build_router_candidate_summaries() - removed now
            # that that function derives its summary directly from this
            # same deep entry instead, with no shallow entry of its own
            # left to warm.)
            if schema_cache.get(cache_key) is not None:
                return
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