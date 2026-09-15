"""
concurrency_guard.py

Simple, process-local admission control for this app's most expensive
per-request routes: /api/translate (an LLM call, retried up to
MAX_TRANSLATION_ATTEMPTS times), /api/execute (a live database
connection, or up to MAX_IN_SCOPE_CONNECTIONS of them in "all databases"
mode), and /api/summarize-result/-results (single-connection mode's and
"all databases" mode's own post-execution summarization step - another
LLM call, made by the client once execution finishes). TRANSLATE_GUARD
below covers all THREE of these routes as one shared pool, not just
/api/translate - by design: an LLM call made to generate SQL and an LLM
call made to summarize results are the same kind of expensive, gunicorn-
thread-and-LLM-key-pool-consuming work as far as this guard is concerned,
so they draw from the same budget rather than each getting an
independently-sized allowance. All are already individually timeout-
bounded (TRANSLATION_TIMEOUT_SECONDS; DB_CONNECT_TIMEOUT_SECONDS/
SQL_EXECUTE_TIMEOUT_SECONDS - see backends/base.py and execute_routes.py),
but nothing previously bounded HOW MANY of them this one process would try
to run at once. Under a real traffic surge that meant every request got
accepted and just piled up - consuming this process's fixed gunicorn
thread pool (server.py's --threads flag; see the Dockerfile's own comment
on why this app runs one process/many threads, not many processes), the
LLM key pool, and outbound DB connections - until something else gave way
first (GUNICORN_TIMEOUT killing a stuck worker mid-request, the DB's own
max_connections, or a provider's rate limit): a slow, ugly failure far
downstream of where the real problem started.

This bounds it right at the door instead: each named guard tracks how many
requests for its guarded concern(s) are currently in flight *in this
process*, and a request that would exceed the configured maximum is
rejected immediately (HTTP 503 + Retry-After) rather than admitted and
left to queue.

Why this is NOT just a duplicate of Cloud Run's own --concurrency/
--max-instances flags (gcp_deploy.sh): those cap how many requests reach
an instance AT ALL, uniformly across every route it serves - they have no
notion that /api/translate and /api/execute are expensive while
/api/config, /api/cancel, and /api/ping are cheap. If gunicorn's whole
thread pool (GUNICORN_THREADS) is tied up running slow translate/execute
calls, a health check or a "Stop" click has nowhere to go on that instance
either, even though Cloud Run has every right to keep routing traffic to
it (it hasn't hit --concurrency itself). Configuring each guard's limit
BELOW GUNICORN_THREADS reserves that headroom deliberately - e.g. 3
concurrent translate/execute requests out of 4 total threads leaves one
thread always free for the cheap routes. Setting a guard's limit equal to
(or above) GUNICORN_THREADS makes it a no-op in practice: gunicorn
physically can't have more requests in flight than it has threads, so
Cloud Run would never even hand this process a request this guard would
reject.

Configuration: two independent env vars - MAX_CONCURRENT_TRANSLATE_
REQUESTS and MAX_CONCURRENT_EXECUTE_REQUESTS. There is no separate
MAX_CONCURRENT_SUMMARIZE_REQUESTS: /api/summarize-result and /api/
summarize-results both draw from TRANSLATE_GUARD, the exact same pool
/api/translate itself uses, rather than getting their own dedicated
guard(s) - deliberately, per the reasoning in this docstring's opening
paragraph. Each var defaults to 0, which means DISABLED (no limit at
all) - same "<=0 disables it" convention execute_routes.py's own
SQL_EXECUTE_TIMEOUT_SECONDS already uses, and the same "an admin has to
explicitly opt in" posture this app takes generally (see app_config.py's
DATABASE_PRESETS_FILE comment) - a deployment that's never heard of this
feature behaves exactly as it always has.

Process-local by design, same as schema_cache.py's cache and
cancel_registry.py's registry (see that module's own docstring for the
identical reasoning) - each guard counts requests THIS process is
handling, not the whole fleet, so it naturally composes with Cloud Run's
own per-instance/fleet-wide scaling: N instances each capped at M
concurrent translate calls is a fleet-wide cap of N*M, which is exactly
the same multiplication this app's --max-instances/--concurrency tuning
already has to reason about. This is also why it's safe under server.py's
own --workers 1 --threads N gunicorn config (one process, many threads,
sharing this same module-level state) but would silently under-count
if a future change ever moved to --workers > 1 (multiple processes) -
exactly the same caveat cancel_registry.py's own docstring already calls
out for its own registry, for the identical reason.
"""

import functools
import os
import threading

from flask import jsonify

from app_config import logger


class ConcurrencyGuard:
    """Tracks how many requests for ONE named route are currently in
    flight in this process, via a plain threading.Semaphore - safe across
    gunicorn's gthread worker threads for the same reason schema_cache.py's
    lock-guarded cache and cancel_registry.py's lock-guarded registry
    already are (see this module's own docstring). `max_concurrent <= 0`
    disables this guard entirely - try_acquire() always succeeds and
    release() is a no-op, so an unconfigured guard costs nothing and
    changes nothing about today's behavior."""

    def __init__(self, name, max_concurrent):
        self.name = name
        self.max_concurrent = max_concurrent
        self._enabled = max_concurrent > 0
        self._semaphore = threading.Semaphore(max_concurrent) if self._enabled else None

    def try_acquire(self):
        """Non-blocking: returns True (a slot was taken - the caller MUST
        call release() exactly once when its work is done, success or
        failure, e.g. via `finally`) or False (already at capacity - the
        caller should reject the request outright, never wait) IMMEDIATELY,
        never blocking until a slot frees up. Blocking here would just move
        the unbounded-pileup problem this guard exists to prevent one layer
        down: a request thread parked waiting on this semaphore is still a
        thread that can't serve anything else, indistinguishable from the
        problem itself."""
        if not self._enabled:
            return True
        return self._semaphore.acquire(blocking=False)

    def release(self):
        """Safe to call only after a try_acquire() that returned True -
        mirrors threading.Semaphore's own contract (an extra release()
        would over-release and let more requests through than
        max_concurrent actually allows). A disabled guard's release() is a
        no-op, matching try_acquire() always returning True for it."""
        if self._enabled:
            self._semaphore.release()


def _read_limit(env_var_name):
    raw = (os.environ.get(env_var_name) or "0").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer - disabling this concurrency guard (treating it as 0).",
            env_var_name, raw,
        )
        return 0
    return value


# Read once at import time, same as every other env-derived constant in
# this app (MAX_TRANSLATION_ATTEMPTS, SQL_EXECUTE_TIMEOUT_SECONDS, etc.) -
# a value that could only ever change via a redeploy anyway.
TRANSLATE_GUARD = ConcurrencyGuard("translate", _read_limit("MAX_CONCURRENT_TRANSLATE_REQUESTS"))
EXECUTE_GUARD = ConcurrencyGuard("execute", _read_limit("MAX_CONCURRENT_EXECUTE_REQUESTS"))


def busy_response(body, retry_after_seconds=5):
    """Builds the standard 503 + Retry-After rejection. `body` is caller-
    supplied rather than fixed here, since /api/translate and /api/execute
    each need to keep their OWN established error-shape convention rather
    than a third shape neither client-side handler has ever seen:
    /api/translate's early-validation failures (missing prompt/API key)
    return a bare {"error": ...} - see translate_routes.py's
    translate_query() - which client.js's readNdjsonStream already reads
    correctly as a single, non-streamed line straight into `finalData`
    (see that function's own docstring); /api/execute's own exception
    handlers return {"success": False, "error": ...} instead (see
    execute_routes.py) - client.js's `response.ok && data.success` checks
    already treat anything else, this included, as a normal failure to
    surface. Both call sites need zero client-side changes because of
    this - the rejection just reuses a shape the client already handles."""
    resp = jsonify(body)
    resp.status_code = 503
    resp.headers['Retry-After'] = str(retry_after_seconds)
    return resp


def guarded_route(guard, busy_body):
    """Decorator for a NON-STREAMING Flask view (e.g. /api/execute): wraps
    the whole view call in guard.try_acquire()/release(), returning
    busy_response(busy_body) instead of ever calling the view when the
    guard is at capacity. `busy_body` is a plain dict (built once, at
    decoration time - the common case, no per-request data needed in it)
    or a zero-arg callable returning one (for a body that needs anything
    computed per-request, though neither current use of this decorator
    needs that).

    Wrapping the whole call in try/finally here - rather than threading
    guard.release() through each of a view's own internal return points
    by hand - is deliberate: /api/execute in particular has more than one
    return path inside itself (a multi-database dispatch branch that
    returns early, and a single-connection try/except/finally below it),
    and a hand-threaded release() only needs to be missed from one of
    them to leak a permit forever (a stuck-low guard that slowly ratchets
    itself down to always-503, indistinguishable from a real capacity
    problem until someone notices the drift). A decorator's own finally
    covers every one of the wrapped view's return/raise points
    unconditionally, by construction, with nothing new to remember if the
    view gains another branch later.

    Do NOT use this on a route that returns a STREAMED Response (e.g.
    /api/translate) - the expensive work there (the generator Flask
    iterates while sending the response) happens AFTER this decorator's
    wrapped call already returned, so its `finally` would release the
    guard immediately, before the real work even starts. See
    translate_routes.py's translate_query() for that route's own,
    hand-rolled guard usage instead, which holds the slot open for the
    generator's full lifetime via its own wrapping generator + finally."""
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapped(*args, **kwargs):
            if not guard.try_acquire():
                body = busy_body() if callable(busy_body) else busy_body
                return busy_response(body)
            try:
                return view_func(*args, **kwargs)
            finally:
                guard.release()
        return wrapped
    return decorator
