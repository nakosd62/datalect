"""
schema_cache.py

A small in-memory, TTL-based cache for the (expensive) database schema
introspection string used in Gemini prompts. Schema rarely changes
mid-session, so re-running six information_schema queries on every single
/api/translate call adds latency and, since the schema text is part of
every prompt, unnecessary Gemini token cost too.

This is process-local (per Cloud Run instance / per local dev process) -
not shared across instances. That's a deliberate simplicity trade-off:
a shared cache (e.g. Firestore- or Redis-backed) would need a network
round-trip to check anyway, which eats into the latency win this is
meant to provide. Each instance just ends up with its own short-lived
copy, which is fine since staleness only matters within the TTL window.

Cache keys are derived from a non-sensitive identifier (see
get_conn_identifier in db.py), never the raw connection string - so a
cache dump or log line never exposes credentials.
"""

import os
import threading
import time

DEFAULT_TTL_SECONDS = int(os.environ.get("SCHEMA_CACHE_TTL_SECONDS", 300))

_lock = threading.Lock()
_cache = {}  # key -> (schema_text, expires_at_monotonic)


def get(key):
    """Returns the cached schema text for `key`, or None if missing/expired."""
    with _lock:
        entry = _cache.get(key)
        if not entry:
            return None
        schema_text, expires_at = entry
        if time.monotonic() >= expires_at:
            del _cache[key]
            return None
        return schema_text


def set(key, schema_text, ttl_seconds=DEFAULT_TTL_SECONDS):
    with _lock:
        _cache[key] = (schema_text, time.monotonic() + ttl_seconds)


def invalidate(key):
    """Drops any cached entry for `key`. Safe to call even if not cached."""
    with _lock:
        _cache.pop(key, None)


def clear():
    """Drops every cached entry. Mainly useful for tests."""
    with _lock:
        _cache.clear()