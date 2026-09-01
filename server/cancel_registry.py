"""
cancel_registry.py

Backs the user-facing "Stop" button (client.js) with a small, process-local
registry of work currently in flight per session, so a POST /api/cancel can
try to abandon it immediately instead of the user just waiting out
whichever timeout would eventually give up on its own (backends/base.py's
DB_CONNECT_TIMEOUT_SECONDS, execute_routes.py's SQL_EXECUTE_TIMEOUT_SECONDS,
or - for an LLM call - nothing today).

Like schema_cache.py, this is process-local (per Cloud Run instance / per
local dev process), guarded by one threading.Lock(). That's safe here for
the same reason it's safe there: server.py runs Werkzeug with
threaded=True but as a SINGLE process, never multiple worker processes, so
every request - whichever thread handles it - shares this same dict. A
future move to a multi-process server (gunicorn with >1 worker, say) would
break this silently (a /api/cancel request could land on a different
worker than the one running the query) - worth remembering if that ever
changes.

IMPORTANT: this provides no real cancellation, only a best-effort nudge -
see execute_routes.py's _execute_with_timeout docstring, which this
generalizes into something a user can trigger immediately rather than
only ever firing after a fixed timeout elapses. The one lever available
for a blocking call already in progress (a DB driver call blocked on a
socket read, or an LLM SDK's blocking HTTP call) is to close whatever
connection/client object it's blocked on - most drivers and HTTP clients
treat that as license to give up and raise rather than hang forever, but
it's driver/SDK-specific behavior, never a guarantee, and the abandoned
thread itself is never forcibly killed (Python has no safe way to do
that). Worst case, closing does nothing and the call simply runs to
completion on its own; whichever caller already gave up on it discards
the result either way.
"""

import threading
import uuid

from app_config import logger

_lock = threading.Lock()
# session_id -> {token: CancellableHandle}
_registry = {}


class CancellableHandle:
    """Wraps one "abandon this" callable so it can be invoked safely from
    either the request that registered it (once its own work finishes
    normally - success, failure, or a caller-side timeout) or a concurrent
    /api/cancel call in another thread - whichever gets there first wins,
    the other is a no-op. Without this, a normal completion racing a
    /api/cancel call could double-close the same connection/client, which
    at best is a harmless no-op and at worst raises an "already closed"
    exception from whichever side loses the race.
    """
    __slots__ = ("_closer", "_lock", "_closed")

    def __init__(self, closer):
        self._closer = closer
        self._lock = threading.Lock()
        self._closed = False

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._closer()
        except Exception:
            # Closing an already-half-dead connection/client (which is
            # exactly the state a just-cancelled or just-timed-out one is
            # often in) can itself raise - this is cleanup on a
            # best-effort path, never something worth failing the
            # request over.
            logger.exception("cancel_registry: closer raised while closing")


def register(session_id, closer):
    """Registers `closer` (a zero-arg callable - e.g. `lambda:
    backend.close(conn)` or `client.close` - that abandons/closes
    whatever this piece of work is blocked on) under `session_id`.
    Returns (token, handle): the caller MUST, in a finally, call
    `unregister(session_id, token)` AND `handle.close()` itself once its
    own work finishes - never call the underlying close directly, or a
    concurrent /api/cancel racing it loses the double-close protection
    CancellableHandle exists to provide.

    Supports MULTIPLE simultaneous entries per session_id - "all
    databases" mode can have several connections' worth of work in
    flight at once for one session (one HTTP request per selected
    connection - see translate_routes.py/execute_routes.py's own
    docstrings), and a single Stop click needs to abandon all of them,
    not just whichever registered first.
    """
    handle = CancellableHandle(closer)
    token = uuid.uuid4().hex
    with _lock:
        _registry.setdefault(session_id, {})[token] = handle
    return token, handle


def unregister(session_id, token):
    """Removes one registry entry - always safe to call even if `token`
    was already removed (by cancel() below, or a duplicate call), so
    callers never need to guard this themselves."""
    with _lock:
        entries = _registry.get(session_id)
        if entries is not None:
            entries.pop(token, None)
            if not entries:
                _registry.pop(session_id, None)


def cancel(session_id):
    """Best-effort: closes every currently-registered handle for
    `session_id` (there can be more than one - see register()'s
    docstring). Returns how many handles were closed, purely for
    /api/cancel's own response - 0 isn't an error, it just means there
    was nothing left to cancel (the turn already finished on its own, or
    hadn't reached a cancellable step yet - e.g. still resolving triage's
    candidate summaries).

    Deliberately does NOT unregister these entries itself - each one's
    own owning request is still responsible for that in its own finally
    (see register()'s docstring), since that's also where its actual
    work either raises (the closed connection/client nudged it to give
    up) or, if the driver/SDK doesn't cooperate, eventually just finishes
    on its own regardless.
    """
    with _lock:
        handles = list(_registry.get(session_id, {}).values())
    for handle in handles:
        handle.close()
    return len(handles)
