"""
schema_cache.py

A small, dependency-free cache for the (expensive) database schema
introspection string used in Gemini prompts. Every successful schema
fetch - deep or shallow, for an admin-configured preset or a user's own
custom connection, whether it came from a plain lookup, the startup
preset prefetch (db.py's prefetch_all_preset_schemas()), the "Refresh
Schema" button, or /api/translate's own in-conversation refresh_schema
checkbox - is cached here indefinitely. There is deliberately no TTL/
expiry concept at all: an entry stays exactly as it was fetched until
something explicitly invalidates it (a connection's config changing - see
db.py's invalidate_schema_cache()) or someone clicks "Refresh Schema".
Re-running six information_schema queries on every single /api/translate
call would add latency and, since the schema text is part of every
prompt, unnecessary Gemini token cost - caching indefinitely means that
cost is only ever paid once per connection, unless something explicitly
asks for a fresh fetch.

This module has NO process-local state of its own any more - every read
and write here goes straight through to state_store.py's durable schema-
cache table (SQLite locally, Firestore on Cloud Run - see that module's
own "Durable schema cache" section). That's a deliberate change from this
module's earlier design, which kept an in-memory dict as a fast "L1" in
front of the durable "L2" store: on Cloud Run, with multiple instances
behind a load balancer with no session affinity, an in-memory L1 meant
instance A invalidating/refreshing a key never reached instance B's own
already-warm copy - so the SAME user, immediately after saving a changed
connection or clicking "Refresh Schema", could have their very next
request land on instance B and see the stale schema again right away.
Bounding that staleness with a short TTL doesn't actually fix it either:
a TTL long enough to preserve any real caching benefit (tens of seconds)
is still far longer than the time it takes a user to take their next
action, and a TTL short enough to close that gap stops being a
meaningful cache at all.

Going pure-durable fixes this completely rather than bounding it, and
the cost of doing so is small: a state_store point read/write (a single
SQLite row or a single Firestore document, keyed by cache_key - see
get_conn_identifier() in db.py) is milliseconds, dwarfed by both the live
database introspection query this cache exists to avoid (which can run
into tens of seconds - BigQuery's TABLE_STORAGE queries in particular)
and by the LLM call that dominates a /api/translate request's latency
regardless of how the schema was sourced. The one place this module is
consulted for several connections in a single request - build_router_
candidate_summaries() in db.py, "all mode" triage across every in-scope
connection - already fans those lookups out concurrently via a
ThreadPoolExecutor, so N point reads there cost about the same wall-clock
time as one, not N serialized round trips.

Cache keys are derived from a non-sensitive identifier (see
get_conn_identifier in db.py), never the raw connection string - so a
cache dump or log line never exposes credentials.

Alongside the schema text itself, each entry's wall-clock set() time is
tracked (see get_cached_at() below) so callers can show a "last
refreshed" timestamp - purely informational, never part of the
cache-hit/miss decision itself.

Also alongside the schema text, an optional "overview" - a short LLM-
generated {"prose", "questions": [...]} pair describing the dataset in
plain English plus a handful of example questions worth asking about it
(see db.py's _generate_and_cache_schema_overview(), called right after
every successful deep schema (re)fetch in prime_schema_cache_with_reason())
- is tracked the same way _cached_at used to be. Unlike the schema text
itself, this is best-effort - a schema whose overview generation failed
(no LLM key configured, a transient LLM error, ...) or hasn't run yet
simply has no entry here; webClient's Schema Viewer treats a missing
overview as "nothing to show yet" rather than an error. The ER diagram
itself is NOT cached here at all - it's built client-side, on the fly,
from the same Constraints/naming-convention-relationship data the Schema
Viewer already parses out of the plain schema text (see
buildSchemaErDiagram() in client.js) - deterministic from real schema
data, so there's nothing to cache or keep in sync.

Finally, a fetch's in-flight/last-error status - "is a (re)fetch for this
key currently running, and if not, did the last one fail and why" - is
ALSO tracked durably now (mark_fetch_pending()/mark_fetch_done()/
is_fetch_pending()/get_last_fetch_error() below), for exactly the same
cross-instance reason the schema text itself moved off process-local
memory: the background thread that runs a (re)fetch after a config-modal
Save (see config_routes.py's handle_config()) runs on whichever instance
handled that POST, but the client's subsequent polling GET (/api/config/
schema-fetch-status) can land on a different instance under Cloud Run's
load balancer - which, if this status were still process-local, would
have never heard of that fetch and would wrongly report "not pending"
while it's genuinely still running elsewhere.
"""

import datetime


# Deferred at call time, not import time - this can't be a top-level
# "from app_config import state_store" the way db.py's own equivalent
# import is: db.py already imports
# app_config BEFORE it imports this module, so by the time IT asks for
# state_store, app_config.py is guaranteed to have already finished
# building it - but nothing guarantees this module itself isn't imported
# earlier than that in some other order, e.g. directly. Importing lazily,
# inside each function that needs it, sidesteps having to reason about or
# depend on import order at all.
def _state_store():
    from app_config import state_store
    return state_store


def get(key):
    """Returns the cached schema text for `key`, or None if `key` has
    never been fetched at all."""
    durable = _state_store().get_cached_schema(key)
    return durable.get("schema_text") if durable else None


def get_cached_at(key):
    """Returns the ISO 8601 UTC timestamp string of when `key`'s current
    entry was last set() (see webClient's Schema Viewer feature - its
    "Last refreshed: ..." display, formatted client-side via
    formatSchemaCachedAt() in client.js), or None if `key` isn't cached at
    all. Never consulted by any cache-hit/miss decision in db.py - this is
    a separate, read-only lookup."""
    durable = _state_store().get_cached_schema(key)
    return durable.get("cached_at") if durable else None


def get_overview(key):
    """Returns `key`'s cached {"prose", "questions", "generated_at"} dict
    (see this module's own docstring), or None if no overview has ever
    been successfully generated for it (never generated at all, or every
    attempt so far has failed - both look identical here, "nothing to
    show yet")."""
    durable = _state_store().get_cached_schema(key)
    return durable.get("overview") if durable else None


def set_overview(key, overview):
    """Records `key`'s freshly generated overview dict - called only from
    db.py's _generate_and_cache_schema_overview(), only after a real LLM
    call succeeded and its response was parsed into the expected shape.
    Does NOT touch schema_text/cached_at - the schema text itself is set
    separately (schema_cache.set(), below), and always fetched/set first;
    this can safely be called moments later without disturbing it."""
    _state_store().set_cached_schema_overview(key, overview)


def set(key, schema_text):
    cached_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _state_store().set_cached_schema(key, schema_text, cached_at)


def mark_fetch_pending(key):
    """Records that a schema (re)fetch for `key` has just started - called
    once, right at the top of db.py's prime_schema_cache_with_reason(),
    before it does any real work. Always paired with a later
    mark_fetch_done() call (in a `finally`), success or failure, so `key`
    never gets stuck showing "pending" forever."""
    _state_store().mark_schema_fetch_pending(key)


def mark_fetch_done(key, error=None):
    """Records that the fetch mark_fetch_pending() announced for `key` has
    finished - always clears the pending flag; `error` (one of db.py's
    SCHEMA_FETCH_FAILURE_REASON_* strings) records the failure reason if
    given, or clears any previously-recorded one for `key` if not (a
    successful fetch supersedes whatever failed before it - this key's
    connection plainly works now)."""
    _state_store().mark_schema_fetch_done(key, error=error)


def is_fetch_pending(key):
    """True if a schema fetch for `key` is currently in flight (see
    mark_fetch_pending()/mark_fetch_done() above) - False for a key that
    was never fetched at all, same as one whose fetch already finished.
    Answered durably (see this module's own docstring) so a poll landing
    on a different Cloud Run instance than the one running the fetch
    still sees the correct in-flight status."""
    return _state_store().get_schema_fetch_status(key)["pending"]


def get_last_fetch_error(key):
    """Returns `key`'s most recent failed-fetch reason (see
    mark_fetch_done()), or None if its last attempt succeeded, or if it's
    never been fetched at all - both look identical here, same "nothing to
    report" convention get_overview() already uses for a key with no
    overview."""
    return _state_store().get_schema_fetch_status(key)["error"]


def invalidate(key):
    """Drops any cached entry for `key` - schema text, overview, and
    fetch-status alike, all as one durable delete (see state_store.py's
    delete_cached_schema() docstring). Safe to call even if not cached at
    all."""
    _state_store().delete_cached_schema(key)


def dump():
    """Returns every currently-cached {key: schema_text} pair (entries
    with no schema text yet - e.g. a fetch is pending but hasn't
    succeeded - are omitted, same as before), for local-dev debugging only
    (see the /api/debug/schema-cache route in config_routes.py, which is
    the only caller of this today and is itself gated off on Cloud Run).
    Keys are already non-sensitive (see this module's own docstring and
    get_conn_identifier() in db.py), and the values are schema/DDL text,
    not credentials - still, this is meant for a developer inspecting
    their own local state, not for any request-serving code path."""
    return _state_store().list_cached_schema_texts()
