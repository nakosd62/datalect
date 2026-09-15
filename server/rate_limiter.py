"""
rate_limiter.py

Per-user request-RATE limiting for this app's most expensive routes -
/api/translate, /api/execute, and /api/summarize-result/-results - via
Flask-Limiter. This is a different axis of protection than
concurrency_guard.py's in-process admission control, and the two are
meant to stack, not duplicate each other:
concurrency_guard.py caps how many requests for one route can be
SIMULTANEOUSLY in flight in this process; this module caps how many
requests one identified user can make over a TIME WINDOW, regardless of
whether they overlap. A script that fires one request, waits for it to
finish, then fires another, repeated 200 times in a minute, never trips
concurrency_guard's semaphore at all (there's never more than one request
in flight) but would still exhaust the LLM key pool / outbound DB
connections over the course of that minute - this module is what catches
that pattern instead.

"Per user" reuses this app's OWN existing notion of a user - the exact
identity auth.get_current_user_identity() already resolves and that
chat_history_routes.py/config_routes.py already use to scope translation
history and saved connections: the real signed-in email when
AUTH_ENABLED/IS_CLOUD_RUN and a verified identity is present, or
"anonymous:<session id>" otherwise (see auth.py's own docstring on
ANONYMOUS_USER_ID_PREFIX). Deliberately NOT a bare IP address: this app
runs behind gunicorn/Cloud Run's own front-end proxy, and nothing in this
codebase runs werkzeug.middleware.proxy_fix.ProxyFix, so
request.remote_addr would be that proxy's own address, not the real
client's - every visitor would collapse into one bucket. Reusing the
identity this app already trusts for state avoids that trap for free, and
means a signed-in user's rate limit follows them across sessions/devices
exactly the same way their translation history already does, while an
anonymous visitor still gets their own separate, per-browser-session
allowance.

Configuration: two independent env vars - RATE_LIMIT_TRANSLATE and
RATE_LIMIT_EXECUTE - each a Flask-Limiter rate limit string (e.g. "20 per
minute" - see
https://flask-limiter.readthedocs.io/en/stable/#rate-limit-string-notation).
There is no separate RATE_LIMIT_SUMMARIZE: /api/summarize-result and
/api/summarize-results share RATE_LIMIT_TRANSLATE's own budget, pooled
together with /api/translate itself via Flask-Limiter's shared_limit()/
scope= mechanism (verified directly - two different views decorated with
the same scope draw down ONE combined counter, not one each) - see
_translate_family_rate_limit() below. Deliberate, not an oversight: an LLM
call made to generate SQL and an LLM call made to summarize results are
the same kind of expensive work as far as this app's admission control is
concerned, so they draw from the same allowance rather than each getting
its own independently-sized one.

Unset/empty (the default) means DISABLED for that budget - same "an admin
has to explicitly opt in" posture as concurrency_guard.py's
MAX_CONCURRENT_*_REQUESTS and execute_routes.py's own
SQL_EXECUTE_TIMEOUT_SECONDS - a deployment that's never heard of this
feature behaves exactly as it always has. Unlike concurrency_guard.py's
own numeric "<=0 disables it" convention, Flask-Limiter's limit strings
have no single sentinel value to overload for "disabled" - so disabling
here means translate_rate_limit()/summarize_rate_limit()/execute_rate_
limit() hand back the ORIGINAL, undecorated view function unchanged,
rather than applying a decorator configured with some "unlimited" value.

Storage: in-memory (storage_uri="memory://"), explicitly - process-local,
same reasoning as schema_cache.py's cache, cancel_registry.py's registry,
and concurrency_guard.py's own guards (see that module's docstring). Each
Limiter instance gets its own private in-memory backend (verified
directly - two Limiter(..., storage_uri="memory://") instances never
share state), so this composes safely with this app's per-test fresh-
import isolation without any extra cleanup. Safe under server.py's own
--workers 1 --threads N gunicorn config (one process, many threads,
sharing this module-level state) but, exactly like concurrency_guard.py,
each Cloud Run INSTANCE counts independently - with --max-instances=N, a
"20 per minute" limit is a per-instance allowance, not a precise
fleet-wide one, unless a shared backend (e.g. Redis) is configured later
via storage_uri. Enabling gcloud run deploy's own --session-affinity flag
makes a given user's requests more likely to keep landing on the same
instance, which makes this per-instance count closer to accurate in
practice, but Cloud Run documents that affinity as best-effort, not a
guarantee - a known, accepted trade-off for now, not an oversight.

Response shape on rejection: a 429 ("Too Many Requests" - RFC 6585; a
rate-limited request COULD have proceeded right now, unlike
concurrency_guard.py's 503 "temporarily unavailable" for a request that
genuinely can't be served this instant) with a Retry-After header giving
the caller a concrete number of seconds until they have room again, and a
body shaped to match whatever failure shape THIS route already returns -
see translate_rate_limit()/execute_rate_limit() below - so, exactly as
with concurrency_guard.py, zero client-side changes are needed: client.js
already renders either shape as a normal failure.

One deliberate difference from concurrency_guard.py worth calling out:
that module's guards are acquired only AFTER each route's own early-
validation checks (missing API key, empty prompt/SQL) - so a request that
fails validation never consumes a scarce concurrency slot, since that
slot only matters while real work is in flight. Rate limiting has no such
carve-out: RATE_LIMIT_TRANSLATE/RATE_LIMIT_EXECUTE apply to every hit of
the route, including ones that go on to fail validation. That's
intentional, not an oversight - the whole point here is to catch a script
hammering the ENDPOINT ITSELF repeatedly, and a flood of empty-prompt
requests is exactly the kind of pattern this should catch, not exempt.
"""

import os
import time

import limits
from flask import jsonify
from flask_limiter import Limiter

from app_config import app, logger
from auth import get_current_user_identity, get_or_create_session_id


def _rate_limit_key():
    """This app's own existing notion of "one user" (see this module's
    docstring) - NOT a bare session id on its own, and NOT an IP address.
    get_current_user_identity() already folds a session id in for anonymous
    visitors (ANONYMOUS_USER_ID_PREFIX + session id - see auth.py), so
    calling it directly here gives every distinct visitor/account their own
    key, matching exactly what the rest of this app already treats as
    "a user" for state scoping (translation history, saved connections)."""
    session_id = get_or_create_session_id()
    return get_current_user_identity(session_id)


limiter = Limiter(
    key_func=_rate_limit_key,
    app=app,
    storage_uri="memory://",
    # Each route below shapes its own rejection response via on_breach
    # (see translate_rate_limit()/execute_rate_limit()) to match that
    # route's existing conventions - Flask-Limiter's own X-RateLimit-*/
    # Retry-After headers would be redundant with (and inconsistent in
    # style with) that hand-built response, so they're turned off here.
    headers_enabled=False,
)


def _read_limit_string(env_var_name):
    """Empty/unset -> None (disabled - see this module's docstring).
    Validated eagerly via limits.parse() rather than left for Flask-
    Limiter to discover lazily: an unparseable limit string is silently
    treated as "never limit" by Flask-Limiter itself (verified directly -
    it neither raises nor logs), which would make a typo indistinguishable
    from a deliberately-disabled guard. Logging a warning and disabling
    here instead matches concurrency_guard.py's own _read_limit()
    convention for a malformed MAX_CONCURRENT_*_REQUESTS value."""
    raw = (os.environ.get(env_var_name) or "").strip()
    if not raw:
        return None
    try:
        limits.parse(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a valid rate limit string (e.g. \"20 per minute\") - "
            "disabling this rate limit (treating it as unset).",
            env_var_name, raw,
        )
        return None
    return raw


# Read once at import time, same as every other env-derived constant in
# this app (MAX_TRANSLATION_ATTEMPTS, concurrency_guard.py's own limits,
# etc.) - a value that could only ever change via a redeploy anyway.
RATE_LIMIT_TRANSLATE = _read_limit_string("RATE_LIMIT_TRANSLATE")
RATE_LIMIT_EXECUTE = _read_limit_string("RATE_LIMIT_EXECUTE")

# Flask-Limiter's shared_limit() scope name pooling /api/translate,
# /api/summarize-result, and /api/summarize-results into ONE combined
# counter per user under RATE_LIMIT_TRANSLATE - see this module's own
# docstring for why. An arbitrary but stable string; never surfaced
# anywhere outside this process (not logged, not returned to the client).
_TRANSLATE_FAMILY_SCOPE = "translate_family"


def _rate_limited_response(body, retry_after_seconds):
    resp = jsonify(body)
    resp.status_code = 429
    resp.headers['Retry-After'] = str(retry_after_seconds)
    return resp


def _make_on_breach(busy_body):
    """Builds an on_breach callback for Limiter.limit(): Flask-Limiter
    calls this with a RequestLimit describing the breach, and - per its own
    docstring - embeds whatever flask.Response this returns into the
    RateLimitExceeded it raises internally, instead of its own default
    response. `request_limit.reset_at` is the epoch second this exact
    limit window resets, so retry_after is always the real remaining wait,
    not a guessed constant."""
    def on_breach(request_limit):
        retry_after = max(0, int(request_limit.reset_at - time.time()))
        body_value = busy_body() if callable(busy_body) else busy_body
        return _rate_limited_response(body_value, retry_after)
    return on_breach


def _translate_family_rate_limit(view_func, busy_body):
    """Shared implementation for translate_rate_limit()/
    summarize_rate_limit() below - both apply RATE_LIMIT_TRANSLATE,
    pooled across /api/translate, /api/summarize-result, and /api/
    summarize-results via limiter.shared_limit(..., scope=
    _TRANSLATE_FAMILY_SCOPE) rather than limiter.limit(...) - the two are
    otherwise identical, but shared_limit() is what actually makes
    multiple decorated views draw down ONE combined counter per user
    instead of each getting its own independent one at the same configured
    number (verified directly against the installed library). `busy_body`
    still varies per call site, so each route's rejection keeps ITS OWN
    established failure shape (see this module's docstring's discussion of
    response shape) even though the underlying budget is shared."""
    if not RATE_LIMIT_TRANSLATE:
        return view_func
    decorator = limiter.shared_limit(
        RATE_LIMIT_TRANSLATE,
        scope=_TRANSLATE_FAMILY_SCOPE,
        on_breach=_make_on_breach(busy_body),
    )
    return decorator(view_func)


def translate_rate_limit(view_func):
    """Applies RATE_LIMIT_TRANSLATE to translate_query() when configured;
    otherwise returns view_func completely unchanged (see this module's
    docstring on the "disabled means skip the decorator" convention). This
    budget is POOLED with summarize_rate_limit() below, not independent of
    it - see _translate_family_rate_limit()'s own docstring.

    Placed directly under @translate_bp.route(...) (see translate_routes.py)
    so the check runs before translate_query() itself is ever called -
    including before its own early-validation checks and before
    TRANSLATE_GUARD.try_acquire() - a rate-limited request never touches
    the concurrency guard at all.

    Rejection body is a bare {"error": ...} - the SAME shape
    translate_query()'s own early-validation failures and
    concurrency_guard's own busy_response() already use for this route
    (see that function's docstring) - which client.js's readNdjsonStream
    already reads correctly as a single, non-streamed line straight into
    `finalData`. Zero client-side changes needed."""
    return _translate_family_rate_limit(view_func, {
        'error': 'You are sending translation requests too quickly. Please slow down and try again shortly.',
    })


def summarize_rate_limit(view_func):
    """Applies the SAME pooled RATE_LIMIT_TRANSLATE budget
    translate_rate_limit() uses (see _translate_family_rate_limit()'s
    docstring) to a summarization view (summarize_result()/
    summarize_results() in translate_routes.py) - not a separate,
    independently-sized limit. Intended for BOTH /api/summarize-result and
    /api/summarize-results - decorate each of them with this same
    function; per-user hits against either one (or /api/translate itself)
    all draw down the one shared counter.

    Placed directly under each route's own @translate_bp.route(...) so the
    check runs before the view is ever called - including before its own
    early-validation checks and before TRANSLATE_GUARD.try_acquire() (both
    summarize routes share that same pooled guard too - see
    concurrency_guard.py) - a rate-limited request never touches the
    concurrency guard at all, same ordering guarantee translate_rate_
    limit()/execute_rate_limit() already give their own routes.

    Rejection body is {"success": False, "error": ...} - matching both
    summarization routes' own existing failure shape (see
    translate_routes.py's summarize_result()/summarize_results(), and
    concurrency_guard.py's own busy_response() usage for these same two
    routes) - client.js's `data.success` checks already treat this the
    same way. Zero client-side changes needed. (The body text differs from
    translate_rate_limit()'s own message only because the two routes keep
    different response SHAPES, not because the budget being reported on is
    in any way different.)"""
    return _translate_family_rate_limit(view_func, {
        'success': False,
        'error': 'The server is handling too many requests right now. Please try again in a few seconds.',
    })


def execute_rate_limit(view_func):
    """Same contract as translate_rate_limit() above, for /api/execute -
    governed by RATE_LIMIT_EXECUTE. Placed between @execute_bp.route(...)
    and @guarded_route(...) (see execute_routes.py) so the check runs
    before guarded_route's own EXECUTE_GUARD.try_acquire() - a rate-limited
    request never consumes a concurrency slot either.

    Rejection body is {"success": False, "error": ...} - matching every
    other failure shape this route already returns (see execute_routes.py's
    own except blocks and concurrency_guard.py's busy_response() usage
    here) - client.js's `response.ok && data.success` check already treats
    this the same way. Zero client-side changes needed."""
    if not RATE_LIMIT_EXECUTE:
        return view_func
    decorator = limiter.limit(
        RATE_LIMIT_EXECUTE,
        on_breach=_make_on_breach({
            'success': False,
            'error': 'You are sending database query requests too quickly. Please slow down and try again shortly.',
        }),
    )
    return decorator(view_func)
