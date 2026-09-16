"""
schema_cache.py

A small in-memory cache for the (expensive) database schema introspection
string used in Gemini prompts. Every successful schema fetch - deep or
shallow, for an admin-configured preset or a user's own custom connection,
whether it came from a plain lookup, the startup preset prefetch
(db.py's prefetch_all_preset_schemas()), the "Refresh Schema" button, or
/api/translate's own in-conversation refresh_schema checkbox - is cached
here indefinitely. There is deliberately no TTL/expiry concept at all: an
entry stays exactly as it was fetched until something explicitly
invalidates it (a connection's config changing - see db.py's
invalidate_schema_cache()) or the whole process restarts. Re-running six
information_schema queries on every single /api/translate call would add
latency and, since the schema text is part of every prompt, unnecessary
Gemini token cost - caching indefinitely means that cost is only ever
paid once per connection per process lifetime, unless something
explicitly asks for a fresh fetch.

This is process-local (per Cloud Run instance / per local dev process) -
not shared across instances. That's a deliberate simplicity trade-off:
a shared cache (e.g. Firestore- or Redis-backed) would need a network
round-trip to check anyway, which eats into the latency win this is
meant to provide. Each instance just ends up with its own copy, refreshed
independently whenever something explicitly asks it to be.

Cache keys are derived from a non-sensitive identifier (see
get_conn_identifier in db.py), never the raw connection string - so a
cache dump or log line never exposes credentials.
"""

import threading

_lock = threading.Lock()
_cache = {}  # key -> schema_text


def get(key):
    """Returns the cached schema text for `key`, or None if missing."""
    with _lock:
        return _cache.get(key)


def set(key, schema_text):
    with _lock:
        _cache[key] = schema_text


def invalidate(key):
    """Drops any cached entry for `key`. Safe to call even if not cached."""
    with _lock:
        _cache.pop(key, None)


def clear():
    """Drops every cached entry. Mainly useful for tests."""
    with _lock:
        _cache.clear()


def dump():
    """Returns a shallow copy of the entire cache ({key: schema_text}),
    for local-dev debugging only (see the /api/debug/schema-cache route
    in config_routes.py, which is the only caller of this today and is
    itself gated off on Cloud Run). A copy, not a live reference, so the
    caller can iterate it without holding _lock and without risking a
    concurrent set()/invalidate() mutating it mid-iteration. Keys are
    already non-sensitive (see this module's own docstring and
    get_conn_identifier() in db.py), and the values are schema/DDL text,
    not credentials - still, this is meant for a developer inspecting
    their own local process, not for any request-serving code path."""
    with _lock:
        return dict(_cache)
