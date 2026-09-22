"""
llm_providers.py

The LLM provider abstraction, extracted out of translate_routes.py (which
had grown past 5000 lines covering far too many concerns - see that
module's own docstring for what's left there). Everything in this file was
originally defined directly inside translate_routes.py; nothing here is new
behavior, just a change of address.

Contains: per-provider API key pools (get_gemini_api_keys/pick_gemini_api_key
and the Claude/OpenAI equivalents), per-provider error classification for
the retry loop (_classify_claude_error/_classify_openai_error/
_classify_gemini_error and the error_category() siblings used for the
user-facing message), the chat-history format converters
(build_gemini_history_contents/build_claude_history_messages/
build_openai_history_messages), the low-level per-provider API calls
(_call_gemini/_call_claude/_call_openai), and the LlmProvider interface
itself plus its three concrete subclasses (GeminiProvider/ClaudeProvider/
OpenAiProvider) and the _LLM_PROVIDERS registry/lookup helpers
(get_llm_provider/list_llm_providers_info/_default_fleet_provider).

translate_routes.py re-imports every one of these names right back into its
own namespace (see the `from llm_providers import (...)` block that
replaced this code there), so they remain reachable as
translate_routes.<name> for:
  - server/db.py's and server/config_routes.py's real
    `from translate_routes import get_llm_provider, list_llm_providers_info`
    (and similar) cross-module imports, which still work unchanged.
  - every existing test's `app_env.translate_routes.<name>` /
    `env.translate_routes.<name>` attribute access (key functions, the
    LlmProvider classes, TRANSLATION_TIMEOUT_SECONDS, etc.) - none of those
    needed to change for this move.

TRANSLATION_TIMEOUT_SECONDS is intentionally computed independently here
(same `os.environ.get("TRANSLATION_TIMEOUT_SECONDS", 60)` read
translate_routes.py itself still does at module level) rather than imported
from translate_routes.py - importing it from there would create a circular
import, since translate_routes.py imports FROM this module. Both are read
from the same environment variable at import time within the same process,
so they always agree.
"""
import os
import random
import re
from abc import ABC, abstractmethod

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
import anthropic
import openai
import httpx
try:
    # google-genai vendors a drop-in httpx fork under this separate import
    # namespace for some of its internal transport - see
    # _classify_gemini_error's TRANSLATION_TIMEOUT_SECONDS case below for
    # why both need checking. Not a direct dependency of this app; guarded
    # in case a future google-genai release drops it.
    import httpx2
except ImportError:  # pragma: no cover - present today via google-genai
    httpx2 = None

from app_config import logger, TRANSLATION_RETRY_DELAY_SECONDS

# See this module's docstring above for why this is computed here
# independently rather than imported from translate_routes.py.
TRANSLATION_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATION_TIMEOUT_SECONDS", 60))

# Same reasoning as TRANSLATION_TIMEOUT_SECONDS above: _render_history_
# result_block below needs this, and importing it from translate_routes.py
# (which is what defines the "real"/canonical copy of this constant, still
# used by other, non-moved code there) would be circular. Same env var, same
# default, read independently at import time.
HISTORY_RESULT_MAX_ROWS = int(os.environ.get("HISTORY_RESULT_MAX_ROWS", 10))


def get_gemini_api_keys():
    """Collect Gemini API keys from GEMINI_PRESET_KEYS (comma-separated;
    a single key is just a one-item list)."""
    preset_keys_env = os.environ.get("GEMINI_PRESET_KEYS", "")
    return [k.strip() for k in preset_keys_env.split(",") if k.strip()]


def pick_gemini_api_key(exclude=None):
    """Pick a Gemini API key at random from the configured pool.

    `exclude` is an optional set of keys already tried during this
    request (e.g. one that just came back rate-limited) - those are
    avoided when a fresh alternative exists. If every configured key is
    already in `exclude`, falls back to the full pool rather than
    returning None, so a request with more retry attempts than
    configured keys still retries something instead of giving up early.
    """
    keys = get_gemini_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def get_claude_api_keys():
    """Collect Claude API keys. Supports an optional comma-separated
    CLAUDE_PRESET_KEYS env var (same pool pattern as GEMINI_PRESET_KEYS,
    for load-balancing across several paid keys); falls back to the single
    standard ANTHROPIC_API_KEY var if that's not set, which is the normal
    case for one paid account."""
    preset_keys_env = os.environ.get("CLAUDE_PRESET_KEYS", "")
    keys = [k.strip() for k in preset_keys_env.split(",") if k.strip()]
    if keys:
        return keys
    single = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return [single] if single else []


def pick_claude_api_key(exclude=None):
    """Same selection logic as pick_gemini_api_key: random choice, avoiding
    already-tried keys in `exclude` where a fresh alternative exists. With
    only one configured key (the common case) this just returns it."""
    keys = get_claude_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def get_openai_api_keys():
    """Collect OpenAI API keys. Same pool pattern as get_claude_api_keys:
    an optional comma-separated OPENAI_PRESET_KEYS env var for load-
    balancing across several paid keys, falling back to the single
    standard OPENAI_API_KEY var (the normal case for one paid account).
    Like Claude - and unlike Gemini - this pool is never rotated through
    on a rate-limit error (see _classify_openai_error's docstring); it
    exists purely so a request can start with a different key each time
    if more than one happens to be configured."""
    preset_keys_env = os.environ.get("OPENAI_PRESET_KEYS", "")
    keys = [k.strip() for k in preset_keys_env.split(",") if k.strip()]
    if keys:
        return keys
    single = os.environ.get("OPENAI_API_KEY", "").strip()
    return [single] if single else []


def pick_openai_api_key(exclude=None):
    """Same selection logic as pick_gemini_api_key/pick_claude_api_key:
    random choice, avoiding already-tried keys in `exclude` where a fresh
    alternative exists. With only one configured key (the common case)
    this just returns it."""
    keys = get_openai_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def _classify_claude_error(exc):
    """Decide whether/how to retry a failed Claude call. Unlike Gemini (see
    _classify_gemini_error below), Claude never rotates keys here: the
    key-pool-rotation retry is a Gemini-specific hack for an app that's
    known to configure a POOL of Gemini keys (GEMINI_PRESET_KEYS) to spread
    load/rate-limits across - Claude isn't assumed to have that, so ALL of
    its retryable failures - including rate limits and "overloaded" - just
    wait and retry with the same key, same as a transient Gemini 5xx would:
      - RateLimitError (429), a 529 "overloaded" APIStatusError, any other
        5xx APIStatusError, or a connection-level APIConnectionError: retry
        with the same key after TRANSLATION_RETRY_DELAY_SECONDS.
      - Anything else (bad request, auth failure, invalid model): not
        retried, same as Gemini.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 529:  # Claude-specific "overloaded, try again" status
            return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}
        if isinstance(code, int) and 500 <= code < 600:
            return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}
        return None

    if isinstance(exc, anthropic.APIConnectionError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


def _classify_openai_error(exc):
    """Decide whether/how to retry a failed OpenAI call. Same policy (and
    same reasoning) as _classify_claude_error above: no key-rotation retry
    here either - that stays a Gemini-only mechanism (see this module's
    docstring) - so every retryable failure just waits and retries with
    the same key:
      - RateLimitError (429), InternalServerError (any 5xx), or an
        APIConnectionError (covers APITimeoutError too, which subclasses
        it): retry with the same key after TRANSLATION_RETRY_DELAY_SECONDS.
      - Anything else (bad request, auth failure, invalid model,
        permission denied, ...): not retried, same as the other two
        providers.
    The openai package's exception hierarchy is structurally very similar
    to anthropic's (both APIStatusError-rooted, both Stainless-generated
    SDKs) - RateLimitError and InternalServerError are both APIStatusError
    subclasses already scoped to their own status code, so (unlike
    Gemini's _gemini_error_code helper) there's no need to inspect a raw
    status_code integer here at all."""
    if isinstance(exc, openai.RateLimitError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, openai.InternalServerError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, openai.APIConnectionError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


def _gemini_error_code(exc):
    """Best-effort extraction of the HTTP-style status code the google-genai
    SDK attaches to APIError subclasses. Different SDK versions have used
    different attribute names, so this checks a couple."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


# Retry policy, keyed by failure type. This is the single place to add
# retry behavior for a new kind of Gemini failure as it comes up - each
# classifier below just needs to return a dict describing how to retry:
#   - delay (float): seconds to sleep before the next attempt, drawn from
#     the shared TRANSLATION_RETRY_DELAY_SECONDS budget (see the comment
#     above MAX_TRANSLATION_ATTEMPTS). Only ever non-zero for a failure
#     that's NOT key-related (see rotate_key below) - waiting only makes
#     sense when the next attempt is otherwise identical to the one that
#     just failed (same key, same everything), giving whatever went wrong
#     a moment to clear. When the next attempt already differs (a
#     different key), there's nothing to wait out.
#   - rotate_key (bool): pick a different configured Gemini API key for the
#     next attempt rather than reusing the one that just failed, drawing
#     from the SEPARATE, Gemini-only key-rotation budget (one attempt per
#     configured GEMINI_PRESET_KEYS entry - see the retry loop in
#     stream_translation()). Used for capacity/rate-limit errors: the
#     failed key is (at least momentarily) out of capacity, but a
#     different configured key almost certainly isn't, so that retry fires
#     immediately (delay=0) rather than sitting idle waiting out a limit a
#     different key was never subject to. This is Gemini's ONLY - see
#     _classify_claude_error, which never sets this. Transient server-side
#     errors aren't key-related at all, so they retry with the same key
#     instead - and since nothing changed about the request, they DO wait
#     out TRANSLATION_RETRY_DELAY_SECONDS first, on the theory the same
#     problem needs a moment to pass.
# Returning None means "don't retry this - raise immediately" (e.g. bad
# request, invalid model, auth failure - these fail the same way every
# time, so retrying wastes the attempt budget).

def _classify_gemini_error(exc):
    """Decide whether/how to retry a failed Gemini call. Returns a retry
    action dict (see policy comment above) or None to raise immediately."""
    code = _gemini_error_code(exc)

    # 429 - per-key rate limit / capacity exhausted. Rotate to a
    # different configured key so the next attempt isn't just hitting
    # the same limit again - and since that next attempt uses a key that
    # was never subject to the limit that just failed, there's nothing to
    # wait out: it retries immediately (delay=0), not after
    # TRANSLATION_RETRY_DELAY_SECONDS (that delay is reserved for the 5xx
    # case below, where the same key retries against the same problem).
    # This rotate_key retry draws from its own budget - one attempt per
    # configured Gemini key - entirely independent of
    # MAX_TRANSLATION_ATTEMPTS (see stream_translation()'s retry loop).
    if code == 429:
        return {"rotate_key": True, "delay": 0}

    # 5xx - transient, server-side hiccup (e.g. the plain "500 INTERNAL"
    # Gemini occasionally throws) unrelated to which key was used, so the
    # same key is fine to retry with. Unlike the 429 case above, the next
    # attempt is otherwise identical to the one that just failed, so this
    # one DOES wait out TRANSLATION_RETRY_DELAY_SECONDS first, giving the
    # transient condition a moment to actually pass before trying the
    # exact same thing again.
    is_server_error = (isinstance(code, int) and 500 <= code < 600) or isinstance(exc, genai_errors.ServerError)
    if is_server_error:
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    # TRANSLATION_TIMEOUT_SECONDS exceeded (see that constant's docstring) -
    # google-genai has no typed timeout exception the way anthropic/openai
    # do, so this surfaces as a raw httpx.TimeoutException/httpx2.
    # TimeoutException instead, with no .code/.status_code for
    # _gemini_error_code above to find. Treated the same as the 5xx case
    # just above: same key, retry after TRANSLATION_RETRY_DELAY_SECONDS -
    # a timeout is exactly the kind of transient condition that delay is
    # meant to give a moment to clear.
    timeout_exc_types = (httpx.TimeoutException,) if httpx2 is None else (httpx.TimeoutException, httpx2.TimeoutException)
    if isinstance(exc, timeout_exc_types):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


# --- User-facing LLM error messages -----------------------------------------
# A call's own retry/rotation budget (classify_error() above, per provider)
# is about giving a TRANSIENT failure a chance to clear before giving up -
# this is the separate, orthogonal question of what to tell the USER once
# that budget IS exhausted (or the failure was never retryable to begin
# with): today, every such failure just showed the raw SDK exception text
# verbatim in an "error" field - technically accurate, but meaningless to
# someone who isn't reading this app's source (a bare "503 UNAVAILABLE..."
# or "Error code: 429 - {'error': {'code': 'insufficient_quota', ...", with
# no indication of what to actually DO about it: retry, wait, or just pick
# a different model). The three functions below turn that into an honest,
# actionable sentence PLUS the original raw text (never hidden - just no
# longer the only thing shown), classified into exactly three buckets:
#   "unavailable" - the model/service itself is down or too busy right now
#     (a 5xx, Anthropic's 529 "overloaded", a dropped connection/timeout) -
#     the honest fix is "try again in a moment."
#   "exhausted" - a capacity/quota ceiling was hit (Gemini/Claude's 429,
#     OpenAI's "insufficient_quota" 429 specifically) - retrying the SAME
#     model right now won't help; a different model is the actual fix.
#   "invalid_key" - the API key itself was rejected (a 401/403, Gemini's
#     documented 400 "API key not valid" shape, or the SDKs' typed
#     AuthenticationError/PermissionDeniedError) - see the "Bring Your Own
#     Key" feature (webClient's Preferences dialog): the fix here depends on
#     WHOSE key failed, which format_llm_error_for_user's `using_byok` param
#     (not error_category/classify - that's still purely about the
#     exception's shape) decides between at formatting time: a user's own
#     saved key gets told to fix/remove it in Preferences, while this app's
#     own configured key failing is this app's problem, not something
#     picking a different model or editing Preferences fixes.
#   "other" - anything else (bad request, invalid model, an exception type
#     this app doesn't specifically recognize) - no confident guess at why,
#     so no specific advice beyond "try a different model."
# _llm_error_category(exc) below classifies according to whichever
# provider's exception shapes it's checking - it's never called directly;
# each provider's LlmProvider.error_category() (see GeminiProvider/
# ClaudeProvider/OpenAiProvider below) dispatches to its own provider-
# specific version of it, mirroring how classify_error()/_classify_*_error
# above are already split one-per-provider.

def _gemini_error_category(exc):
    """error_category() for Gemini. Prefers the semantic `.status` string
    google-genai's APIError attaches (e.g. "RESOURCE_EXHAUSTED",
    "UNAVAILABLE" - the same google.rpc.Code names gRPC/Google APIs use
    everywhere) when present, falling back to the numeric code check
    _gemini_error_code() already uses elsewhere - needed for a raw httpx
    timeout (no .status at all) and for lightweight test doubles that only
    set a numeric .code, same as _classify_gemini_error/_gemini_error_code
    already tolerate.

    "invalid_key" is Gemini's one genuinely ambiguous case: a rejected key
    is documented (see https://firebase.google.com/docs/ai-logic/error-codes)
    to come back as a plain HTTP 400 "API key not valid. Please pass a
    valid API key." - the SAME status/code a hundred other bad-request
    reasons also use - so a bare 400 is deliberately NOT enough on its own
    (that would misclassify unrelated bad-request failures); this only
    fires for 400 when the message text itself says so. A 401/403 (or the
    matching PERMISSION_DENIED/UNAUTHENTICATED status strings) is
    unambiguous and always treated as invalid_key."""
    status = getattr(exc, "status", None)
    if status == "RESOURCE_EXHAUSTED":
        return "exhausted"
    if status == "UNAVAILABLE":
        return "unavailable"
    if status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return "invalid_key"

    code = _gemini_error_code(exc)
    if code == 429:
        return "exhausted"
    if code == 503 or (isinstance(code, int) and 500 <= code < 600):
        return "unavailable"
    if code in (401, 403):
        return "invalid_key"
    if code == 400:
        message = str(getattr(exc, "message", None) or exc).lower()
        if "api key not valid" in message or "api_key_invalid" in message:
            return "invalid_key"

    timeout_exc_types = (httpx.TimeoutException,) if httpx2 is None else (httpx.TimeoutException, httpx2.TimeoutException)
    if isinstance(exc, timeout_exc_types):
        return "unavailable"

    return "other"


def _claude_error_category(exc):
    """error_category() for Claude. RateLimitError (429) is always
    "exhausted" - Anthropic's rate limits are a request/token-budget
    ceiling, not a "servers are momentarily busy" condition. AuthenticationError
    (401 - missing/malformed/revoked key) and PermissionDeniedError (403 -
    a key that's valid but not allowed to do this) are both unambiguous
    typed exceptions, checked ahead of the generic APIStatusError branch
    below (both subclass it) - "invalid_key". A 529 "overloaded" status or
    any other 5xx (including a connection-level failure/timeout, which
    subclasses APIConnectionError) reads as "unavailable", matching
    _classify_claude_error's own retry policy for those same statuses."""
    if isinstance(exc, anthropic.RateLimitError):
        return "exhausted"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "invalid_key"
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 529 or (isinstance(code, int) and 500 <= code < 600):
            return "unavailable"
        return "other"
    if isinstance(exc, anthropic.APIConnectionError):
        return "unavailable"
    return "other"


def _openai_error_category(exc):
    """error_category() for OpenAI. Unlike Gemini/Claude, OpenAI overloads
    its one 429 RateLimitError for two very different conditions (see
    https://platform.openai.com/docs/guides/error-codes), distinguished
    only by the "code" the API attaches to the error body: "insufficient_
    quota" is a hard billing/quota ceiling (genuinely "exhausted" - won't
    clear on its own), while every other 429 (typically
    "rate_limit_exceeded") is the ordinary "too many requests right now"
    kind - transient, reads as "unavailable/busy" same as a 5xx would.
    AuthenticationError (401) and PermissionDeniedError (403) are both
    unambiguous typed exceptions - "invalid_key". An InternalServerError
    (5xx) or a connection-level failure/timeout is "unavailable", matching
    _classify_openai_error's own retry policy."""
    if isinstance(exc, openai.RateLimitError):
        code = getattr(exc, "code", None)
        return "exhausted" if code == "insufficient_quota" else "unavailable"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "invalid_key"
    if isinstance(exc, openai.InternalServerError):
        return "unavailable"
    if isinstance(exc, openai.APIConnectionError):
        return "unavailable"
    return "other"


_LLM_ERROR_UNAVAILABLE_TEMPLATE = (
    "The selected model ({model}) is currently unavailable or too busy. "
    "Please retry later or select a different model.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_EXHAUSTED_TEMPLATE = (
    "Datalect's reserved capacity for this model ({model}) has been exhausted. "
    "Please select a different model.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_OTHER_TEMPLATE = (
    "The selected model ({model}) ran into an error. Please select a different model.\n\n"
    "Actual error message received:\n"
)
# "invalid_key" has two variants, not one - see the section comment above
# _gemini_error_category for why format_llm_error_for_user needs a
# `using_byok` flag to choose between them, rather than error_category()
# itself producing two different category strings for what is, from the
# exception's own shape, exactly the same failure.
_LLM_ERROR_INVALID_KEY_BYOK_TEMPLATE = (
    "Your custom API key for this model ({model}) was rejected. Please correct or remove it in "
    "Preferences (Bring Your Own Key) - until then, this model will keep failing.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_INVALID_KEY_ENV_TEMPLATE = (
    "The API key configured for this model ({model}) was rejected. This is a problem with the "
    "app's own configuration, not something selecting a different model fixes on its own - please "
    "let the app's administrator know, or try a different model in the meantime.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_TEMPLATES = {
    "unavailable": _LLM_ERROR_UNAVAILABLE_TEMPLATE,
    "exhausted": _LLM_ERROR_EXHAUSTED_TEMPLATE,
    "other": _LLM_ERROR_OTHER_TEMPLATE,
}


def format_llm_error_for_user(provider, model_name, exc, using_byok=False):
    """Turns a call's FINAL exception (after classify_error()'s own retry
    budget is exhausted, or immediately for a non-retryable one) into the
    honest, categorized message described in the section comment above -
    always ending with the original raw exception text, never hiding it.
    `provider` is any registered LlmProvider (its error_category() is what
    actually classifies `exc` - see GeminiProvider/ClaudeProvider/
    OpenAiProvider). An unrecognized category (there isn't one today, but
    error_category() implementations are free to extend) falls back to the
    generic "other" wording rather than raising.

    `using_byok` - whether THIS call used a user-saved Bring-Your-Own-Key
    value (see state_store.py's llm_byok_keys) rather than this app's own
    env-configured key - only changes anything when the category is
    "invalid_key": a user's own key gets told to fix/remove it in
    Preferences, while this app's own configured key failing is squarely
    this app's problem, not the user's, so it gets a different message
    entirely (see the two templates above). Every other category's wording
    is identical either way - a model being unavailable/exhausted has
    nothing to do with whose key hit that limit."""
    category = provider.error_category(exc)
    if category == "invalid_key":
        template = _LLM_ERROR_INVALID_KEY_BYOK_TEMPLATE if using_byok else _LLM_ERROR_INVALID_KEY_ENV_TEMPLATE
    else:
        template = _LLM_ERROR_TEMPLATES.get(category, _LLM_ERROR_OTHER_TEMPLATE)
    raw = str(exc) or f"{type(exc).__name__} occurred."
    return template.format(model=model_name) + raw


class LlmCallFailed(Exception):
    """Wraps an LLM provider call's final exception once format_llm_error_
    for_user() above has already turned it into the full, categorized,
    user-facing message - this wrapper's __str__ IS that message verbatim.

    Raised (replacing a bare `raise`) at every "give up" point inside a
    retry loop that sits INSIDE a wider try/except also covering unrelated
    failures (schema fetch, etc.) - see stream_translation()'s inline
    single-connection retry loop and generate_sql_for_connection() below,
    both of which report their final exception to a caller several frames
    away via a generic `except Exception as e: ... str(e)`. Without this,
    that generic catch has no way to tell "the LLM call itself failed" (do
    format the friendly message) apart from "something unrelated blew up
    nearby" (don't - str(e) should stay whatever that unrelated exception
    already says) - wrapping the message INTO the exception at the one
    point that's unambiguous means every existing `str(e)`-based caller
    gets the improved text for free, with no changes needed at the catch
    site itself.

    Deliberately NOT used by triage_all_mode_question (connection_router.py)
    or summarize_all_mode_results below - neither of those loops ever runs
    anything else ambiguous in their scope between capturing the LLM
    exception and returning it (summarize_all_mode_results' own schema
    fetch, via _build_all_mode_schema_block, happens BEFORE this retry
    loop even starts, and get_database_schema() never raises regardless -
    see its own docstring), so their callers (translate_routes.py's
    router_only_group_mode branch, and the /api/summarize-results route)
    call format_llm_error_for_user() directly on the raw exception instead -
    one fewer layer of indirection where it isn't needed."""
    pass


def format_results_table_text(columns, rows, max_rows=500):
    """Render a query result set as plain text suitable for an LLM prompt."""
    cols = columns or []
    rws = rows or []
    text = f"Columns: {', '.join(cols)}\nTotal Rows: {len(rws)}\nSample/Full Data:\n"
    text += "\n".join([str(r) for r in rws[:max_rows]])
    return text


def _render_history_result_block(index, res):
    """Render one turn's per-statement history-results entry as text. A
    failed statement/connection is shaped {error, ...} (see client.js's
    summarizeResultForHistory) rather than {columns, rows, rowCount} -
    previously that shape fell through this code silently as a blank
    "Columns: \nTotal Rows: 0" block, losing the error text entirely. Now
    an error entry renders its actual error message instead."""
    if 'error' in res:
        header = f"[Query Result {index + 1} - failed]"
        return header + "\n" + f"Error: {res.get('error')}"
    cols = res.get('columns') or []
    rws = res.get('rows') or []
    row_count = res.get('rowCount', len(rws))
    shown_rows = min(len(rws), HISTORY_RESULT_MAX_ROWS)
    header = f"[Query Result {index + 1} - {row_count} row(s) total, showing {shown_rows}]"
    return header + "\n" + format_results_table_text(cols, rws, max_rows=HISTORY_RESULT_MAX_ROWS)


def _build_history_combined_text(msg):
    """Build the full text for one client-supplied history turn: the base
    {role, text} text, any per-statement `results` (or errors - see
    _render_history_result_block above) from that turn's execution, and
    finally that turn's own stored summary, so a later turn's LLM call sees
    not just the raw data/errors but what was actually told to the user
    about them.

    The summary comes from one of two places depending on which mode the
    turn was: single-connection turns carry it directly as `summary`
    (mirroring pending.entry.summary / modelEntry.summary in client.js);
    all-mode turns carry it nested under `allMode.routingMessage` - that
    field starts out (in captureAllModeHistory's caller) as the triage
    routing message, but is overwritten with the real Phase C summary text
    before the turn is persisted to history (see the
    `notes.routingMessage = summaryEntry.text` assignment in client.js just
    before captureAllModeHistory() is called), so by the time it reaches
    here it IS the summary, not the routing message.

    Without this, build_gemini_history_contents/build_claude_history_
    messages/build_openai_history_messages below only ever read the raw
    columns/rows/rowCount/error data for a past turn - the explanation the
    user actually saw (which may highlight things not obvious from the raw
    data/error alone) was silently unavailable to later turns."""
    text = msg.get("text") or ""
    combined_text = text

    hist_results = msg.get("results")
    if hist_results:
        result_blocks = [_render_history_result_block(i, res) for i, res in enumerate(hist_results)]
        combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

    summary_text = msg.get("summary")
    if not summary_text:
        all_mode = msg.get("allMode")
        if all_mode:
            summary_text = all_mode.get("routingMessage")
    if summary_text:
        combined_text = combined_text + "\n\n[Summary given to the user]\n" + summary_text

    return combined_text


def build_gemini_history_contents(history):
    """
    Turn the client-supplied chat history into Gemini `types.Content` objects.
    Each history message is {role, text} and may optionally carry a `results`
    list - one entry per SQL statement that was executed for that turn, each
    shaped like {columns, rows, rowCount} or {error} - and/or a stored
    summary (`summary`, or `allMode.routingMessage` for all-mode turns). See
    _build_history_combined_text above for exactly how these are combined
    into that turn's text.
    """
    contents = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=combined_text)]
            )
        )
    return contents


def build_claude_history_messages(history):
    """Same purpose as build_gemini_history_contents above, targeting
    Claude's message shape instead: a plain list of {"role", "content"}
    dicts. Gemini's "model" role becomes Claude's "assistant"; "user" is
    unchanged. The results/error/summary-appending logic is identical to
    the Gemini version (see _build_history_combined_text) - only the
    returned container shape differs."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

        messages.append({
            "role": "assistant" if role == "model" else role,
            "content": combined_text,
        })
    return messages


def build_openai_history_messages(history):
    """Same purpose as build_gemini_history_contents/build_claude_history_
    messages above, targeting the OpenAI Responses API's "easy input
    message" shape instead: a plain list of {"role", "content"} dicts -
    structurally identical to Claude's, since the Responses API accepts a
    plain string for `content` (EasyInputMessageParam) rather than
    requiring Chat-Completions-style message objects. Gemini's "model" role
    becomes "assistant" (same mapping as Claude's); "user" is unchanged.
    The results/error/summary-appending logic is identical to the other two
    providers' versions (see _build_history_combined_text) - only the
    returned container shape (a plain dict, not a types.Content) differs
    from Gemini's."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

        messages.append({
            "role": "assistant" if role == "model" else role,
            "content": combined_text,
        })
    return messages


def _call_gemini(client, model, contents, system_instruction):
    """One Gemini generate_content call. Returns (text, usage_dict) - the
    usage_dict shape is shared with _call_claude below so the retry loop
    and the response-building code in stream_translation() don't need to
    know which provider actually ran.

    No explicit caching setup here, unlike _call_claude below: Gemini 2.5+
    models cache matching prefixes automatically ("implicit caching") with
    no opt-in call or config field required - Google's own docs are
    explicit that "there is nothing you need to do" beyond what
    stream_translation() already does structurally (putting the large,
    stable schema/history content ahead of the ever-changing new prompt).
    Cache hits are reported back via usage_metadata.cached_content_token_count,
    surfaced below the same way a real cache read is for Claude."""
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            # See the long comment on automatic_function_calling further
            # down in the original file history - unchanged from before.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    )
    text = response.text.strip() if response.text else ""
    usage = response.usage_metadata
    # `or 0` on every field below, not just a bare `getattr(..., 0)`/
    # `x if usage else 0` - a real Gemini response can carry usage_metadata
    # with a given field PRESENT but set to None rather than 0 (observed in
    # production: thoughts_token_count is None, not 0, on a call that
    # didn't use extended thinking) - `getattr(obj, name, 0)` only
    # substitutes 0 for a MISSING attribute, never a present-but-None one,
    # and every downstream consumer of this dict (usage totals summed
    # across Phase B's parallel calls, the translations-table columns,
    # the NDJSON response) does real arithmetic on these values, which
    # raises TypeError the moment one of them is None instead of an int.
    cached_content_tokens = (getattr(usage, 'cached_content_token_count', 0) or 0) if usage else 0
    return text, {
        "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
        "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
        "total_tokens": (usage.total_token_count or 0) if usage else 0,
        "thinking_tokens": (getattr(usage, 'thoughts_token_count', 0) or 0) if usage else 0,
        "cached_content_tokens": cached_content_tokens,
    }


def _mark_claude_cache_boundary(message):
    """Converts a plain {"role", "content": <str>} message (the shape
    build_claude_history_messages()/translate_query() build) into
    Anthropic's content-block form, with an ephemeral cache_control marker
    on that block. Claude has no automatic/implicit caching the way Gemini
    2.5+ does (see _call_gemini's docstring and this module's docstring) -
    a block only ever gets cached if explicitly marked like this. Marking
    it here means everything up to and including this message - system
    prompt, schema, and all history through this point - becomes a
    candidate cached prefix; see translate_query()'s comment on why the
    last already-accumulated history turn (not the ever-changing new
    prompt at the end) is the right message to mark."""
    message["content"] = [{
        "type": "text",
        "text": message["content"],
        "cache_control": {"type": "ephemeral"},
    }]


def _call_claude(client, model, messages, system_instruction):
    """One Claude messages.create call. Returns (text, usage_dict) in the
    same shape _call_gemini returns above.

    No `temperature` here on purpose: Claude Opus 4.7 and later (which
    includes the claude-sonnet-5 default) reject sampling parameters
    (temperature/top_p/top_k) outright rather than just ignoring them -
    Anthropic deprecated them for these newer models. This app wants
    low-variance SQL generation anyway, and these models are tuned for
    that by default without needing temperature pinned to near-0.

    The system prompt (dialect_intro + the fixed formatting rules) is sent
    as its own cache_control-marked block - it's identical on every call
    for a given dialect, so caching it benefits every session using that
    dialect, not just one conversation. Below Anthropic's per-model
    minimum cacheable size (1024 tokens for Sonnet, more for Haiku) this
    marker is simply a no-op - no error, the content just isn't written to
    the cache - so marking it unconditionally is always safe."""
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": system_instruction,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=messages,
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    usage = response.usage
    # `or 0` throughout below - same defensive reasoning as _call_gemini's
    # own usage dict above: a real usage object can report a field as
    # None rather than 0 (present attribute, null value), which every
    # downstream consumer's real arithmetic on this dict can't tolerate.
    cache_read_tokens = (getattr(usage, 'cache_read_input_tokens', 0) or 0) if usage else 0
    cache_creation_tokens = (getattr(usage, 'cache_creation_input_tokens', 0) or 0) if usage else 0
    return text, {
        "input_tokens": (usage.input_tokens or 0) if usage else 0,
        "output_tokens": (usage.output_tokens or 0) if usage else 0,
        "total_tokens": ((usage.input_tokens or 0) + (usage.output_tokens or 0)) if usage else 0,
        # This app doesn't use extended thinking on the Claude path, so
        # this is always 0 - reported anyway so record_translation() and
        # the NDJSON payload don't need a provider-specific case for it.
        "thinking_tokens": 0,
        # Tokens actually served from cache on THIS call (a cache miss/
        # write reports 0 here even though cache_creation_input_tokens is
        # nonzero, logged above) - same semantics as Gemini's
        # cached_content_token_count above, hence the shared field name.
        "cached_content_tokens": cache_read_tokens,
    }


def _call_openai(client, model, llm_input, system_instruction):
    """One OpenAI Responses API call (client.responses.create) - built on
    Responses rather than the older Chat Completions API, per this app's
    longer-term bet on it (see this module's docstring): OpenAI recommends
    Responses for new integrations, and its prompt caching applies more
    broadly than Chat Completions'. Returns (text, usage_dict) in the same
    shape _call_gemini/_call_claude return above.

    No `temperature` passed, for the same reason _call_claude doesn't pass
    one: current-generation reasoning-capable models (the gpt-5.6 family
    this app defaults to, and real OpenAI in general) reject sampling
    parameters outright rather than silently ignoring them.

    No explicit cache markers here either, unlike _call_claude's
    cache_control blocks: like Gemini 2.5+ (see _call_gemini's docstring),
    OpenAI's prompt caching is on by default for supported models with no
    opt-in call or parameter required - `instructions` (this app's fixed,
    per-dialect system prompt) plus the stable leading portion of `input`
    this app already structures schema/history to form (see
    translate_query()'s comment on why the schema goes as far to the front
    as possible) is exactly the kind of repeated, stable prefix that gets
    reused automatically."""
    response = client.responses.create(
        model=model,
        instructions=system_instruction,
        input=llm_input,
    )
    text = (response.output_text or "").strip()
    usage = response.usage
    input_tokens_details = getattr(usage, 'input_tokens_details', None) if usage else None
    output_tokens_details = getattr(usage, 'output_tokens_details', None) if usage else None
    # `or 0` throughout below - same defensive reasoning as _call_gemini's/
    # _call_claude's own usage dicts above: a real usage object can report
    # a field as None rather than 0 (present attribute, null value), which
    # every downstream consumer's real arithmetic on this dict can't
    # tolerate.
    cached_tokens = (getattr(input_tokens_details, 'cached_tokens', 0) or 0) if input_tokens_details else 0
    reasoning_tokens = (getattr(output_tokens_details, 'reasoning_tokens', 0) or 0) if output_tokens_details else 0
    return text, {
        "input_tokens": (usage.input_tokens or 0) if usage else 0,
        "output_tokens": (usage.output_tokens or 0) if usage else 0,
        "total_tokens": (usage.total_tokens or 0) if usage else 0,
        # Reasoning-model "thinking" tokens - same field this app already
        # reports for Gemini's thoughts_token_count; always 0 for a
        # non-reasoning model/response, same as Claude's always-0 above.
        "thinking_tokens": reasoning_tokens,
        # Tokens actually served from cache on THIS call - same semantics
        # as Gemini's cached_content_token_count / Claude's
        # cache_read_input_tokens above, hence the shared field name.
        "cached_content_tokens": cached_tokens,
    }


# --- LLM provider dispatch ------------------------------------------------
#
# Each provider above (Gemini/Claude/OpenAI) has its own free functions for
# key management, error classification, history-building, and the actual
# API call - those are the pieces that genuinely differ per SDK and are
# each independently unit-testable/patchable (see this module's docstring
# on why they're left as-is rather than folded into the classes below).
# What used to differ is HOW translate_query()/stream_translation() picked
# among them: a scattered `if LLM_PROVIDER == "claude": ... else: ...` at
# every call site. LlmProvider (and one subclass per provider) replaces
# that with a single object stream_translation() calls methods on -
# equivalent in spirit to backends/base.py's Backend interface for SQL
# dialects, just for LLM providers instead.
class LlmProvider(ABC):
    """Interface every registered LLM provider (see _LLM_PROVIDERS below)
    implements. Nothing here wraps a live network call directly - each
    method delegates to that provider's own free function(s) above, so
    those keep their existing names, signatures, and test coverage
    unchanged; this class only decides WHICH free functions get called."""

    #: This provider's registered label ("google"/"anthropic"/"openai") -
    #: the _LLM_PROVIDERS key it's stored under, and the value a session's
    #: saved llm_provider field holds once chosen via the model-selection
    #: UI. Deliberately NOT the same as this class's own name or the
    #: underlying SDK/product name (GeminiProvider/ClaudeProvider still wrap
    #: the actual Gemini/Claude APIs) - this is purely the user-facing
    #: company label.
    name = None

    #: The request-body key checked before the generic "model" override -
    #: e.g. "gemini_model" - so a caller can pin a model for one provider
    #: without affecting what another provider would use. Still named after
    #: the underlying SDK (not this provider's `name` label above) since
    #: it's a wire-format detail existing callers (e.g. the mobile client)
    #: already depend on verbatim - not part of this rename.
    request_model_key = None

    #: Env var holding this provider's comma-separated list of models (e.g.
    #: "GOOGLE_MODELS") - one var doing double duty: its first entry is
    #: this provider's default_model, the full list is preset_models (both
    #: below). Subclasses set this; None here only because the base class
    #: itself is never instantiated.
    models_env_var = None

    #: Single-model list used when models_env_var is entirely unset/blank -
    #: keeps this app usable with zero model configuration. Subclasses set
    #: this to their own hardcoded default (e.g. ["gemini-3.6-flash"]).
    fallback_models = None

    #: Exact 400 response text when this provider has no API key configured.
    missing_key_error = None

    #: True only for a provider this app is known to configure a POOL of
    #: keys for (Gemini, via GEMINI_PRESET_KEYS) - gates whether a
    #: classify_error() result with rotate_key=True is even meaningful.
    #: Claude/OpenAI both leave this False; their classify_error()
    #: implementations never return rotate_key=True in the first place (see
    #: _classify_claude_error's/_classify_openai_error's docstrings), so
    #: this is really a second, defensive line of documentation rather than
    #: something stream_translation()'s retry loop strictly needs to check -
    #: but see get_key_pool_size() below for the one place it's used
    #: directly.
    supports_key_rotation = False

    @abstractmethod
    def get_api_keys(self):
        """All configured API keys for this provider, as a list (possibly
        empty)."""
        raise NotImplementedError

    @abstractmethod
    def pick_api_key(self, exclude=None):
        """One configured API key at random, avoiding `exclude` where a
        fresh alternative exists. None if nothing is configured at all."""
        raise NotImplementedError

    @abstractmethod
    def make_client(self, api_key):
        """A fresh SDK client for this provider, authenticated with
        `api_key`."""
        raise NotImplementedError

    @abstractmethod
    def build_llm_input(self, history, schema_block, new_prompt_content):
        """Turns this request's chat history plus the (already-rendered)
        schema_block/new_prompt_content strings into whatever shape this
        provider's call() expects - a list of google-genai Content objects,
        a list of Claude/OpenAI-style {"role","content"} dicts, etc. Also
        decides WHERE the schema attaches (prepended to the first
        historical turn when there is history; folded into the new prompt,
        or split into its own leading block, when there isn't) - see
        translate_query()'s own comment for why that ordering matters for
        every provider's caching."""
        raise NotImplementedError

    @abstractmethod
    def call(self, client, model, llm_input, system_instruction):
        """One provider API call. Returns (text, usage_dict) - usage_dict
        always has the same five keys (input_tokens/output_tokens/
        total_tokens/thinking_tokens/cached_content_tokens) regardless of
        provider, so the caller (stream_translation()) never needs a
        provider-specific case for building its response."""
        raise NotImplementedError

    @abstractmethod
    def classify_error(self, exc):
        """Returns a retry-action dict ({"rotate_key": bool, "delay":
        float}) for a retryable failure, or None to raise `exc` immediately.
        See _classify_gemini_error's docstring (above the first
        implementation of this) for the full policy this documents once for
        every provider."""
        raise NotImplementedError

    @abstractmethod
    def error_category(self, exc):
        """Classifies a call's FINAL exception (retry budget exhausted, or
        immediately for a non-retryable one) into "unavailable"/
        "exhausted"/"other" for format_llm_error_for_user() - see the
        section comment above _gemini_error_category (above that function's
        first implementation) for what each bucket means and why this is a
        separate question from classify_error()'s retry policy."""
        raise NotImplementedError

    def get_key_pool_size(self):
        """How many attempts the key-ROTATION retry budget gets (see
        stream_translation()'s retry loop) - the number of distinct
        configured keys for a provider that supports rotating through a
        pool, or 1 for a provider that doesn't (so that budget is
        exhausted after the single already-tried key, i.e. effectively
        unused - matching every provider except Gemini today)."""
        return len(self.get_api_keys()) if self.supports_key_rotation else 1

    @property
    def preset_models(self):
        """Every model this provider offers, in order - parsed live (not
        cached at import time, so tests that reconfigure the env var
        per-case see the change, same as get_gemini_api_keys() already
        does for GEMINI_PRESET_KEYS) from this provider's models_env_var,
        comma-separated, blank entries dropped, each trimmed of
        surrounding whitespace. Falls back to fallback_models when the env
        var is entirely unset/blank, so this is never an empty list - the
        model-selection UI always has at least one option per provider,
        and default_model (below) always has something to return."""
        raw = os.environ.get(self.models_env_var, "") if self.models_env_var else ""
        models = [m.strip() for m in raw.split(",") if m.strip()]
        return models or list(self.fallback_models)

    @property
    def default_model(self):
        """The model used when neither a request override
        (request_model_key/"model") nor a saved session choice picks one.

        Checks the app-wide DEFAULT_MODEL env var first (parsed live, same
        as preset_models above, so it's never stale relative to a test or
        deployment that sets it): if that value is one of THIS provider's
        own preset_models, it wins - letting an operator pin the default to
        any specific model (not necessarily the first entry in this
        provider's own *_MODELS list) via one env var, without reordering
        that list. Otherwise falls back to preset_models' first entry, i.e.
        this provider's *_MODELS env var's first entry, or
        fallback_models[0] when that env var is unset - exactly the
        behavior before DEFAULT_MODEL existed. See get_llm_provider()'s
        docstring for how DEFAULT_MODEL also picks which provider is used
        at all, for a session that hasn't chosen one either."""
        override = os.environ.get("DEFAULT_MODEL", "").strip()
        if override and override in self.preset_models:
            return override
        return self.preset_models[0]


class GeminiProvider(LlmProvider):
    # Registered as "google" (see `name`'s docstring above) - this class
    # keeps its SDK-derived name since it still wraps the actual Gemini API
    # (genai.Client, GEMINI_PRESET_KEYS, etc.) regardless of that label.
    name = "google"
    request_model_key = "gemini_model"
    models_env_var = "GOOGLE_MODELS"
    # The app's ONE hardcoded fleet-wide default (see get_llm_provider()'s
    # docstring) - a session that never picked a provider at all ends up
    # here, with this list's first entry as the model actually used.
    fallback_models = ["gemini-3.6-flash"]
    missing_key_error = "Google API key is not configured."
    supports_key_rotation = True

    def get_api_keys(self):
        return get_gemini_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_gemini_api_key(exclude=exclude)

    def make_client(self, api_key):
        # http_options.timeout is milliseconds, unlike anthropic's/openai's
        # plain-seconds `timeout` kwarg (see ClaudeProvider's/OpenAiProvider's
        # make_client() below) - see TRANSLATION_TIMEOUT_SECONDS's docstring.
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(TRANSLATION_TIMEOUT_SECONDS * 1000)),
        )

    def build_llm_input(self, history, schema_block, new_prompt_content):
        contents = build_gemini_history_contents(history)
        if contents:
            first_part = contents[0].parts[0]
            first_part.text = schema_block + first_part.text
        else:
            new_prompt_content = schema_block + new_prompt_content
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=new_prompt_content)])
        )
        return contents

    def call(self, client, model, llm_input, system_instruction):
        return _call_gemini(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_gemini_error(exc)

    def error_category(self, exc):
        return _gemini_error_category(exc)


class ClaudeProvider(LlmProvider):
    # Registered as "anthropic" (see `name`'s docstring above) - this class
    # keeps its SDK-derived name since it still wraps the actual Claude API
    # (anthropic.Anthropic, ANTHROPIC_API_KEY, etc.) regardless of that label.
    name = "anthropic"
    request_model_key = "claude_model"
    models_env_var = "ANTHROPIC_MODELS"
    fallback_models = ["claude-sonnet-5"]
    missing_key_error = "Anthropic API key is not configured."
    supports_key_rotation = False

    def get_api_keys(self):
        return get_claude_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_claude_api_key(exclude=exclude)

    def make_client(self, api_key):
        # See TRANSLATION_TIMEOUT_SECONDS's docstring - this SDK takes a
        # plain seconds value directly, unlike GeminiProvider's milliseconds.
        return anthropic.Anthropic(api_key=api_key, timeout=TRANSLATION_TIMEOUT_SECONDS)

    def build_llm_input(self, history, schema_block, new_prompt_content):
        messages = build_claude_history_messages(history)
        if messages:
            messages[0]["content"] = schema_block + messages[0]["content"]
            # Marks the end of the accumulated (stable) prefix - see
            # _mark_claude_cache_boundary's docstring and this module's
            # (formerly translate_query()'s) comment on why the last
            # already-accumulated history turn, not the ever-changing new
            # prompt, is the right message to mark.
            _mark_claude_cache_boundary(messages[-1])
            messages.append({"role": "user", "content": new_prompt_content})
        elif schema_block:
            # A conversation's very first call - split into two content
            # blocks on one message so the schema half can still be
            # cache_control-marked independently of the ever-different new
            # prompt right after it (see _mark_claude_cache_boundary's
            # docstring for why concatenating the two into one marked
            # string would be wrong).
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": schema_block,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": new_prompt_content},
                ],
            })
        else:
            # No history AND no schema block at all - e.g.
            # triage_all_mode_question's first-ever call in a brand-new
            # conversation lands here, before it has any history to thread
            # through (see its own docstring - once a conversation has
            # turns, its calls take the branch above instead, same as any
            # other history-bearing call). Anthropic rejects cache_control
            # on an empty text block outright ("cache_control cannot be
            # set for empty text blocks"), so this must NOT fall into the
            # branch above with an empty `schema_block` - that would make
            # this call always fail with a 400 on a fresh conversation,
            # which the caller has no way to distinguish from a genuine
            # transient error: it just retries once, fails identically,
            # and silently falls back to its own "couldn't route"
            # behavior every single time, regardless of the question. There's
            # nothing worth cache-marking here anyway (a single plain-text
            # prompt with no stable prefix to reuse), so this is just the
            # one message, unmarked.
            messages.append({"role": "user", "content": new_prompt_content})
        return messages

    def call(self, client, model, llm_input, system_instruction):
        return _call_claude(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_claude_error(exc)

    def error_category(self, exc):
        return _claude_error_category(exc)


class OpenAiProvider(LlmProvider):
    name = "openai"
    request_model_key = "openai_model"
    models_env_var = "OPENAI_MODELS"
    fallback_models = ["gpt-5.6-luna"]
    missing_key_error = "OpenAI API key is not configured."
    supports_key_rotation = False

    def get_api_keys(self):
        return get_openai_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_openai_api_key(exclude=exclude)

    def make_client(self, api_key):
        # See TRANSLATION_TIMEOUT_SECONDS's docstring - same plain-seconds
        # kwarg as ClaudeProvider's make_client() above.
        return openai.OpenAI(api_key=api_key, timeout=TRANSLATION_TIMEOUT_SECONDS)

    def build_llm_input(self, history, schema_block, new_prompt_content):
        # Structurally identical to GeminiProvider's version above (prepend
        # the schema to the first historical turn when there's history,
        # otherwise fold it into the new prompt), not Claude's - OpenAI's
        # prompt caching is automatic like Gemini's, so there's no
        # cache_control-style marker to place (see _call_openai's
        # docstring); only the container shape (plain {"role","content"}
        # dicts, from build_openai_history_messages) differs from Gemini's
        # types.Content objects.
        messages = build_openai_history_messages(history)
        if messages:
            messages[0]["content"] = schema_block + messages[0]["content"]
        else:
            new_prompt_content = schema_block + new_prompt_content
        messages.append({"role": "user", "content": new_prompt_content})
        return messages

    def call(self, client, model, llm_input, system_instruction):
        return _call_openai(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_openai_error(exc)

    def error_category(self, exc):
        return _openai_error_category(exc)


_LLM_PROVIDERS = {
    "google": GeminiProvider(),
    "anthropic": ClaudeProvider(),
    "openai": OpenAiProvider(),
}


def _default_fleet_provider():
    """The provider used for a session that hasn't picked one at all - see
    get_llm_provider()'s own docstring below, the only caller. Resolved
    from DEFAULT_MODEL (see LlmProvider.default_model's docstring) when
    that env var names a model belonging to one of the three registered
    providers' own preset_models - whichever provider matches becomes the
    app's fleet-wide default, so setting DEFAULT_MODEL alone can move it
    onto Claude or OpenAI, not just Google. Falls back to Google - this
    app's original, still-hardcoded fallback-of-last-resort - when
    DEFAULT_MODEL is unset, blank, or doesn't match any currently-
    configured model at all. Checked in _LLM_PROVIDERS' own definition
    order (google, anthropic, openai), so a DEFAULT_MODEL value that
    happened to collide across two providers' preset_models (unlikely in
    practice - model names aren't shared across vendors) would
    deterministically resolve to the earlier one rather than varying by
    dict iteration order."""
    override = os.environ.get("DEFAULT_MODEL", "").strip()
    if override:
        for provider in _LLM_PROVIDERS.values():
            if override in provider.preset_models:
                return provider
    return _LLM_PROVIDERS["google"]


def get_llm_provider(name):
    """Returns the LlmProvider for `name` (a session's saved llm_provider
    value - "google"/"anthropic"/"openai"). Unlike backends/__init__.py's
    get_backend() - which raises on an unrecognized "type" - an
    unrecognized/blank provider name here falls back to this app's ONE
    fleet-wide default (see _default_fleet_provider() above - Google unless
    DEFAULT_MODEL names a model that belongs to a different provider)
    rather than erroring, and the same graceful fallback also covers a
    session whose saved value predates this app's provider labels being
    renamed from "gemini"/"claude" to "google"/"anthropic" - such a session
    just silently reverts to the default instead of erroring."""
    return _LLM_PROVIDERS.get(name) or _default_fleet_provider()


def list_llm_providers_info():
    """Every registered provider's {"name", "preset_models",
    "default_model"} - the shape config_routes.py's GET /api/config needs
    to build the model-selection modal's radio list, organized by
    provider. Order follows _LLM_PROVIDERS' own definition order (google,
    anthropic, openai) so the modal's provider sections render in a
    stable, predictable order across requests."""
    return [
        {"name": p.name, "preset_models": p.preset_models, "default_model": p.default_model}
        for p in _LLM_PROVIDERS.values()
    ]
