"""
translate_routes.py

Natural-language-to-SQL translation: API key selection, chat history ->
provider-native input conversion, the system prompt, and the
/api/translate route itself. Three LLM providers are supported today -
Google (the original/default, still "Gemini" under the hood - see
GeminiProvider), Anthropic ("Claude" under the hood - see ClaudeProvider),
and OpenAI - registered under the labels "google"/"anthropic"/"openai" in
_LLM_PROVIDERS below. There is deliberately no fleet-wide provider-select env var (there
used to be one, LLM_PROVIDER - removed since a session with nothing saved
just needs ONE hardcoded default provider+model pair, not an independently
configurable provider-name knob to keep in sync with it - see
get_llm_provider()'s docstring). A session picks its own provider/model via
the model-selection UI (state_store.py's llm_provider/llm_model), resolved
per-request in translate_query() below.

Provider dispatch goes through the LlmProvider interface (see that class's
docstring further down): translate_query()/stream_translation() call
methods on a single `provider` object rather than branching on the active
provider's name themselves at each step. This is what makes adding a new
provider a matter of writing one new LlmProvider subclass and adding one
line to _LLM_PROVIDERS, rather than finding and extending every
`if provider == ...` branch in this file -
there used to be about half a dozen of those (client construction,
model/key selection, history building, the call itself, error
classification, key-rotation logic) before this dispatch layer was
introduced.

Each provider's SDK-specific mechanics (key pool, error classification,
history shape, the actual API call) still live in their own free
functions/constants below (get_gemini_api_keys/_classify_claude_error/
build_openai_history_messages/_call_gemini/etc.) exactly as before this
dispatch layer was added - the LlmProvider subclasses are thin adapters
over those, not a rewrite of them. This matters for testing: existing
tests that patch translate_routes.genai.Client or call
translate_routes.pick_claude_api_key() directly keep working unchanged,
since those names and their behavior didn't move.

/api/translate streams its response as newline-delimited JSON (NDJSON)
rather than a single JSON body, so a client can show live "retrying..."
feedback while the retry loop below (the single place in this app that
retries a translation - see MAX_TRANSLATION_ATTEMPTS/LlmProvider.classify_error)
works through a transient LLM failure, instead of the request just
appearing to hang. See translate_query()'s stream_translation() for the
exact line shapes and the HTTP-status-code trade-off streaming requires.
"""

import concurrent.futures
import json
import random
import os
import re
import time
from abc import ABC, abstractmethod

from flask import Blueprint, request, jsonify, Response, stream_with_context
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

# from app_config import logger, log_and_generalize_error
from app_config import logger, state_store, MAX_TRANSLATION_ATTEMPTS, TRANSLATION_RETRY_DELAY_SECONDS

from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import (
    resolve_conn_str, get_database_schema, record_translation,
    resolve_in_scope_descriptors, build_router_candidate_summaries,
    resolve_descriptor_by_reference,
)
from backends import get_backend
from backends.base import SCHEMA_TABLES_ONLY, derive_tables_only_schema_text
from connection_router import (
    run_triage_call, is_label_only_response, strip_markdown_fence,
    _build_candidate_schema_block, _extract_json_object,
    _parse_single_dataset_triage_response,
)
import cancel_registry
from concurrency_guard import TRANSLATE_GUARD, busy_response
from rate_limiter import translate_rate_limit, summarize_rate_limit
from prompt_loader import load_prompt

translate_bp = Blueprint('translate', __name__)

# Which LLM provider a request actually uses is resolved per-session (see
# translate_query()'s session_data.get('llm_provider') lookup below), never
# a provider-NAME env var - "google"/"anthropic"/"openai" are the
# only valid values, matching _LLM_PROVIDERS' keys below. A session that never
# explicitly picked one (via the model-selection UI) falls back to this
# app's one fleet-wide default: whichever provider get_llm_provider()
# returns for an unrecognized/blank name - see that function's (and
# _default_fleet_provider()'s) docstring. This used to be independently
# configurable via an LLM_PROVIDER env var; that's gone now, replaced by
# DEFAULT_MODEL below, which names a MODEL rather than a provider - the
# provider that model belongs to is derived from it, so there's still only
# one knob to set, not two that could drift out of sync with each other.

# Each provider's own *_MODELS env var (GOOGLE_MODELS/ANTHROPIC_MODELS/
# OPENAI_MODELS - see LlmProvider.models_env_var below) is a single
# comma-separated list: the full list is what the model-selection modal
# offers for that provider (see LlmProvider.preset_models). Left entirely
# unset, each provider falls back to its own hardcoded single-model default
# (LlmProvider.fallback_models) so this app works out of the box with zero
# model configuration - claude-sonnet-5 for Anthropic (strong structured-
# output reasoning at a much lower cost than the top-tier model),
# gpt-5.6-luna for OpenAI (its cost-efficient tier - "gpt-5.6" alone, no
# suffix, is an alias for the top "-sol" tier instead), gemini-3.6-flash for
# Google.
#
# DEFAULT_MODEL (a single, app-wide env var naming exactly one model, e.g.
# "claude-sonnet-5") is what actually picks each provider's default model
# now, instead of that provider's own *_MODELS list's first entry always
# winning by simply being first - see LlmProvider.default_model's
# docstring. It only takes effect for whichever provider's own
# preset_models actually contains that exact model name; every other
# provider's default_model is unaffected and still falls back to its own
# preset_models[0]. When a session hasn't picked a provider at all yet,
# DEFAULT_MODEL also decides which provider becomes the app's ONE
# fleet-wide default (see _default_fleet_provider() below) - so setting it
# to an Anthropic or OpenAI model moves the whole fleet's default off
# Google without any separate provider-name knob. Google/gemini-3.6-flash
# remains the final fallback-of-last-resort when DEFAULT_MODEL is unset,
# blank, or doesn't match any currently-configured model at all.

# Per-dialect opening lines for the system instruction below - the rest of
# the instruction (output format, NO SQL sentinels, etc.) is identical
# across dialects, only the "what SQL flavor am I writing" framing differs.
# Keyed by Backend.dialect_name (see backends/base.py/postgres.py/
# bigquery.py) so a new backend just needs an entry here to get a properly
# targeted prompt instead of silently inheriting Postgres's.

# Past-turn query results embedded back into the prompt as chat history were
# previously uncapped (max_rows=len(rws) - i.e. "show all of them"). A wide
# result set from even one earlier turn, multiplied across up to 20 retained
# history turns, is exactly what can blow a prompt out to millions of
# tokens - this is what tripped Claude's 1M-token request limit. This caps
# how many rows of a PAST turn's results get serialized back into the LLM
# prompt; it has no effect on what the current turn's results show in the
# UI. Override via env var if 10 is too aggressive/lenient for your data.
HISTORY_RESULT_MAX_ROWS = int(os.environ.get("HISTORY_RESULT_MAX_ROWS", 10))

# How many conversational turns of history are sent back to the LLM. A
# "turn" here is a user message + the model's reply to it - 2 entries in
# the history list per turn - so the default of 10 turns keeps the last 20
# entries, same as the previous hardcoded -20 slice. Configurable since a
# large schema/result-heavy app may need this lower to stay under a
# provider's token limit (see HISTORY_RESULT_MAX_ROWS above for the other
# lever on that same problem).
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", 10))

# The CURRENT turn's own results, fed to the results-summarization LLM
# calls (_build_summary_prompt for "all databases" mode's Phase C,
# _build_single_summary_prompt for single-connection mode) - deliberately
# a SEPARATE, much more generous cap from HISTORY_RESULT_MAX_ROWS above:
# this is real, current data the summarization call exists to reason over
# in full, not old history being replayed turn after turn, so the default
# here is far higher. It still needs a real ceiling, though, now that both
# of those calls send every row rather than none - an adversarial (or just
# very wide) query (e.g. a bare `SELECT * FROM huge_table`) could otherwise
# blow the prompt out to an enormous token count on a single turn, with no
# history multiplier even needed to get there. Override via env var if
# 100 is too aggressive/lenient for your data.
SUMMARY_RESULTS_MAX_ROWS = int(os.environ.get("SUMMARY_RESULTS_MAX_ROWS", 100))

# There are two, INDEPENDENT retry mechanisms below, each with its own
# budget - they used to share one counter (MAX_GEMINI_ATTEMPTS), which
# quietly conflated two unrelated things now that both Gemini and Claude
# are supported. Keep them straight:
#
# 1. Transient-error retries (MAX_TRANSLATION_ATTEMPTS /
#    TRANSLATION_RETRY_DELAY_SECONDS below) - a provider's own backend is
#    momentarily struggling (a 5xx, a dropped connection), unrelated to
#    which API key was used. The SAME key is reused, after a
#    TRANSLATION_RETRY_DELAY_SECONDS pause to give the problem a moment to
#    clear. This bucket is shared by BOTH providers - see
#    _classify_gemini_error/_classify_claude_error - and bounded by
#    MAX_TRANSLATION_ATTEMPTS total calls (initial call + retries).
#
# 2. Gemini's own key-rotation retry (see _classify_gemini_error's 429
#    case) - a per-key rate limit/capacity exhaustion, where the fix is
#    simply "use a different configured key", not "wait". This is a
#    Gemini-specific hack: it only exists because this app supports
#    configuring a POOL of Gemini keys (GEMINI_PRESET_KEYS) to rotate
#    through, a pattern Claude isn't assumed to have (see
#    _classify_claude_error's docstring). Its retry fires immediately (no
#    delay - the next key was never subject to the limit that just hit),
#    and its OWN budget is simply "one attempt per configured key" - i.e.
#    it keeps going until every key in GEMINI_PRESET_KEYS has been tried
#    once (see the retry loop below), a count that has nothing to do with
#    MAX_TRANSLATION_ATTEMPTS and isn't a separate env var of its own.
#
# Anything that isn't one of these two retryable kinds (bad request,
# invalid model, auth failure, etc.) just fails the same way every time,
# so it's raised immediately instead of wasting a retry on it.
#
# MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS are configurable
# via env vars (e.g. to tune retry behavior for a noisier rollout without a
# code change) - same int()/float()-on-getenv pattern as
# backends/base.py's SCHEMA_SHARD_MIN_GROUP_SIZE. Formerly named
# MAX_GEMINI_ATTEMPTS/GEMINI_RETRY_DELAY_SECONDS - renamed now that they
# govern both providers' transient-error retries, not just Gemini's; there's
# no back-compat alias, so an existing deployment setting the old names
# needs updating. Now DEFINED in app_config.py, not here (imported above) -
# connection_router.py's triage_all_mode_question needs the same two
# constants for its own retry loop, and this module already imports FROM
# connection_router.py, so the reverse import would be circular (see
# app_config.py's own comment on this, right above where these now live).

# --- LLM call timeout --------------------------------------------------------
# Bounds how long ONE call to the configured LLM provider (Gemini/Claude/
# OpenAI) may take, threaded into each provider's make_client() below - the
# same "a hung network call must fail fast instead of blocking forever"
# problem backends/base.py's DB_CONNECT_TIMEOUT_SECONDS solves for a stalled
# DB connect() (see that constant's docstring for the fuller threaded=True/
# blast-radius reasoning, which applies identically here: server.py handles
# one request at a time per worker, so a single hung LLM call still stalls
# every other user's request for however long it hangs, unbounded, without
# this). Deliberately one shared knob across all three providers rather than
# a per-provider *_TIMEOUT_SECONDS - the failure mode ("this provider isn't
# responding") is identical regardless of which one a session happens to be
# using, same reasoning DB_CONNECT_TIMEOUT_SECONDS already applies across
# every SQL dialect.
#
# Each SDK is handed this in whatever unit/shape IT expects (see each
# make_client() below) rather than a shared wrapper, since the three differ:
# anthropic.Anthropic/openai.OpenAI both take a plain `timeout=<seconds>`
# kwarg directly, while google-genai's genai.Client takes it in milliseconds
# via a nested HttpOptions object.
#
# A timeout is just another transient failure to the existing retry loop
# (see MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS above and
# each provider's classify_error) - no separate handling needed there.
# anthropic.APITimeoutError/openai.APITimeoutError both already subclass
# their SDK's APIConnectionError, which _classify_claude_error/
# _classify_openai_error already retry. google-genai has no equivalent typed
# exception - a timeout there surfaces as a raw httpx.TimeoutException (or
# httpx2.TimeoutException - see the import above) instead, since this app
# doesn't opt into google-genai's own separate, SDK-internal retry_options
# (which would otherwise silently multiply this timeout by however many
# attempts that's configured for); _classify_gemini_error below has a
# dedicated case for it, treated the same as a transient 5xx.
TRANSLATION_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATION_TIMEOUT_SECONDS", 60))


# The LLM provider abstraction (API key pools, error classification, the
# chat-history format converters, the low-level per-provider calls, and the
# LlmProvider interface + GeminiProvider/ClaudeProvider/OpenAiProvider
# subclasses) now lives in llm_providers.py - see that module's own
# docstring for the full list and the reasoning. Everything it defines is
# re-imported here so it stays reachable as translate_routes.<name>, for:
#   - server/db.py's and server/config_routes.py's real
#     `from translate_routes import get_llm_provider, list_llm_providers_info`
#     (and similar) cross-module imports.
#   - every existing test's app_env.translate_routes.<name> /
#     env.translate_routes.<name> attribute access - none needed to change.
from llm_providers import (
    get_gemini_api_keys, pick_gemini_api_key,
    get_claude_api_keys, pick_claude_api_key,
    get_openai_api_keys, pick_openai_api_key,
    _classify_claude_error, _classify_openai_error,
    _gemini_error_code, _classify_gemini_error,
    _gemini_error_category, _claude_error_category, _openai_error_category,
    format_llm_error_for_user, LlmCallFailed,
    format_results_table_text, _render_history_result_block, _build_history_combined_text,
    build_gemini_history_contents, build_claude_history_messages, build_openai_history_messages,
    _call_gemini, _mark_claude_cache_boundary, _call_claude, _call_openai,
    LlmProvider, GeminiProvider, ClaudeProvider, OpenAiProvider,
    _LLM_PROVIDERS, _default_fleet_provider, get_llm_provider, list_llm_providers_info,
)

# The google-genai/anthropic/openai/httpx SDK imports above (and httpx2,
# guarded) are no longer used directly by this module's own remaining code -
# that usage moved to llm_providers.py along with everything above. They are
# deliberately KEPT here anyway, purely so tests that patch
# translate_routes.genai / .anthropic / .openai / .httpx directly (e.g.
# monkeypatch.setattr(translate_routes.genai, "Client", ...)) keep working
# unchanged: these SDK modules are singletons cached in sys.modules, so
# patching an attribute on the shared module object takes effect regardless
# of which file's own `import` statement brought it into that file's scope.
# Likewise `random` and `ABC`/`abstractmethod` (imported at the top of this
# file) have no remaining direct use here either, post-move, but are left in
# place rather than pruned - removing an import that's merely unused (as
# opposed to one something still reaches through, like the SDKs above) is a
# harmless cleanup with no test depending on it either way, but pruning it
# here isn't worth the risk of this list going stale relative to the actual
# top-of-file imports; left for a later, dedicated pass instead.


# SQL generation (dialect prompts/format rules, response cleanup, dataset-
# group mode's Phase B fan-out, single-dataset mode's Call 1/Call 2 helpers,
# and the shared language-verification check) now lives in
# sql_generation.py - see that module's own docstring for the full
# rationale. Everything is re-imported here so it stays reachable as
# translate_routes.<name>, both for translate_query() below (which still
# calls several of these directly - _run_phase_b_fanout, get_triage_
# schema_text/get_llm_schema_text, triage_single_dataset_question,
# _parse_sql_generation_response, _no_sql_language_mismatch,
# _DIALECT_PROMPT_INTROS/_DEFAULT_DIALECT_PROMPT_INTRO,
# _SQL_GENERATION_FORMAT_RULES, _TRIAGE_FAILURE_TEXT) and for every existing
# test's app_env.translate_routes.<name> attribute access.
from sql_generation import (
    _DIALECT_PROMPT_FILENAMES, _DIALECT_PROMPT_INTROS, _DEFAULT_DIALECT_PROMPT_INTRO,
    _COMMON_FORMAT_RULES,
    _NO_SQL_PREFIX_RE, _MAX_MARKER_PREAMBLE_CHARS, _NO_SQL_SEARCH_RE,
    _SQL_FENCE_LANG_ALTERNATION, _SQL_FENCE_RE, _LEADING_FENCE_RE, _TRAILING_FENCE_RE,
    _strip_no_sql_prefix, _clean_generated_sql, _TRIAGE_FAILURE_TEXT,
    get_llm_schema_text, get_triage_schema_text, generate_sql_for_connection,
    _drain_generation, _classify_generation_outcome, _run_phase_b_fanout,
    _no_sql_language_mismatch,
    triage_single_dataset_question, _SQL_GENERATION_FORMAT_RULES, _parse_sql_generation_response,
)
from language_detect import detect_language as _detect_language, describe_language as _describe_language


# The two post-execution results-summarization pipelines ("all databases"
# mode's Phase C and single-connection mode's own equivalent, plus the
# schema-fetch helper and retry machinery they share, plus their two
# /api/summarize-results and /api/summarize-result routes) now live in
# summarize_routes.py - see that module's own docstring for the full
# rationale. Everything except the two route functions themselves (never
# accessed as translate_routes.<name> in any test - only ever exercised
# over HTTP) is re-imported here so it stays reachable as
# translate_routes.<name>, matching every existing test's
# app_env.translate_routes.<name> attribute access.
from summarize_routes import (
    get_summary_schema_text,
    _SUMMARY_SYSTEM_INSTRUCTION, _build_summary_prompt,
    _default_content_parser, _default_language_text_extractor, _summarize_with_retry,
    _build_all_mode_schema_block, _clean_summary_response,
    _make_summary_content_parser, _summary_language_text, summarize_all_mode_results,
    _SINGLE_SUMMARY_SYSTEM_INSTRUCTION, _build_single_summary_prompt,
    _clean_single_summary_response, _make_single_summary_content_parser,
    _single_summary_language_text, summarize_single_connection_results,
)

# The chart/visualization eligibility + validation helpers
# (_CHART_MIN_ROWS/_column_looks_numeric/_pick_chartable_results/
# _describe_chartable_results/_clean_visualization/_clean_visualizations)
# live in chart_helpers.py (see that module's own docstring) - re-imported
# here purely so they stay reachable as translate_routes.<name> for every
# existing test's app_env.translate_routes.<name> attribute access.
# Nothing in this module's own remaining code calls them directly any more
# (their one real caller, the single-connection summarization pipeline,
# moved to summarize_routes.py too), same "keep purely for back-compat"
# posture as the SDK imports llm_providers.py's own docstring explains.
# _pick_chartable_results/_describe_chartable_results (plural) replaced
# the old _pick_chartable_result/_describe_chartable_columns (singular) -
# multiple simultaneous chartable result sets are now supported instead
# of at most one per turn - and _clean_visualizations (plural) is new
# alongside the still-unchanged, single-entry _clean_visualization.
from chart_helpers import (
    _CHART_MIN_ROWS, _column_looks_numeric, _pick_chartable_results,
    _describe_chartable_results, _clean_visualization, _clean_visualizations,
)


@translate_bp.route('/api/translate', methods=['POST'])
# rate_limiter.py's RATE_LIMIT_TRANSLATE (per-user request RATE over time,
# via Flask-Limiter) - runs before translate_query() is called at all, so
# a rate-limited request never reaches this function's own early-
# validation checks or TRANSLATE_GUARD.try_acquire() further down. See
# rate_limiter.py's own module docstring for why this is a different,
# complementary axis from that concurrency guard (rate over time vs.
# simultaneous in-flight requests).
@translate_rate_limit
def translate_query():
    data = request.get_json() or {}

    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)

    # A blank/never-set session field (see state_store.py's get_session
    # docstring) falls back to get_llm_provider()'s own hardcoded default
    # (Google) - same not-explicitly-chosen-yet convention connection_id
    # already uses. A request-body override (gemini_model/claude_model/
    # openai_model, or the generic "model") still wins over the session's
    # saved model when both are present - it existed before the
    # session-level choice did and stays the more specific, one-off
    # override.
    session_data = state_store.get_session(user_identity)
    provider = get_llm_provider(session_data.get('llm_provider'))
    llm_model = (
        data.get(provider.request_model_key) or data.get('model')
        or session_data.get('llm_model') or provider.default_model
    )
    # A user's own "Bring Your Own Key" (see state_store.py's
    # get_llm_byok_key/set_session docstrings), when saved for this
    # provider, is used INSTEAD of the app's own env-configured pool for
    # every LLM call this request makes - triage, Phase B's per-connection
    # fan-out, and the single-connection retry loop below all resolve
    # `byok_key` from here (either directly, or via `_run_phase_b_fanout`
    # re-resolving it itself for its own worker threads - see that
    # function's docstring). `byok_key` is read-only from this point
    # down, so stream_translation() below can safely close over it without
    # `nonlocal`.
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        return jsonify({'error': provider.missing_key_error}), 400
    tried_llm_keys = {api_key}

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt cannot be empty'}), 400

    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    # An explicit database_url override always means "use exactly this one
    # connection" (see resolve_conn_str above, which conn_str already
    # reflects) - it wins over "all" mode below, same as it always has.
    explicit_db_override = bool(data.get('database_url'))

    # Dataset group mode (session in_scope_mode == "group" - see db.py's
    # resolve_in_scope_descriptors/_resolve_group_configured_descriptors,
    # and app_config.py's own "DATASET GROUPS" comment) runs a real
    # two-phase flow - see stream_translation()'s router_only_group_mode
    # branch below, connection_router.triage_all_mode_question (its name
    # predates and is independent of this user-facing "group" concept -
    # see that module's own docstring), and _run_phase_b_fanout: a triage
    # call decides "answer" (table names alone are enough), "route"
    # (generate and execute real SQL against one or more specific
    # connections, in parallel), or "failed" (fixed apology text, no
    # fallback guess). Unconditional whenever in_scope_mode is "group",
    # regardless of how many datasets the active group actually lists
    # (even just one) - triage still needs to decide "answer directly" vs.
    # "actually go query this database" either way, so there's no
    # connection-count threshold below which it's skipped. A session whose
    # in_scope_mode isn't "group" (the default "single", or an explicit
    # database_url override) takes none of the branches below - see
    # stream_translation()'s single-connection path, which is byte-for-byte
    # the same code path this endpoint has always run.
    in_scope_entries = resolve_in_scope_descriptors(session_data, user_identity)
    router_only_group_mode = session_data.get('in_scope_mode') == 'group' and not explicit_db_override
    #
    # The triage call itself gets this turn's ordinary conversation history
    # (see triage_all_mode_question's docstring) - it's a single, non-per-
    # database step, so there's exactly one shared thread for it to consult
    # (e.g. resolving "how large is THIS database" against a prior turn's
    # answer). Phase B's per-connection calls below are different: each one
    # gets THAT SPECIFIC connection's own history instead (see
    # connection_histories just below) - but only from turns actually
    # asked against it directly in single-connection mode, never from a
    # dataset-group turn that merely routed to it (see client.js's
    # connectionBucketKey()/buildInScopeConnectionHistories() docstrings
    # for the client-side half of this).

    history = data.get('history', [])[-(HISTORY_MAX_TURNS * 2):]
    # See client.js's buildInScopeConnectionHistories() docstring: one
    # entry per in-scope connection the client currently has a bucket for,
    # keyed exactly like client.js's connectionBucketKey() builds its
    # bucket keys - "preset:<id>" / "custom:<key>" - each value that
    # connection's own history array from turns actually asked against it
    # DIRECTLY in single-connection mode. A dataset-group turn's own
    # per-connection outcome is deliberately never folded into any member
    # connection's own bucket (an earlier "fan-out" design did this and was
    # removed - it surprised users by making a group question appear,
    # unasked, in one specific connection's own history), so a connection
    # only ever reached through group mode contributes no history here at
    # all. Consulted below, per selected connection, ONLY for Phase B's
    # real SQL-generation calls - triage above keeps using the ordinary
    # shared `history`, since routing is not itself an NL-to-SQL
    # translation. Defaults to `{}` for an older client that never sends
    # this field at all, or a connection this dict simply has no entry for
    # (never visited directly) - both cases fall back to the same
    # empty-history behavior generate_sql_for_connection has always had,
    # not an error.
    connection_histories = data.get('connection_histories') or {}
    force_schema_refresh = bool(data.get('refresh_schema'))

    # Everything past this point - the schema fetch, the Gemini retry loop,
    # and building the final response - is streamed as newline-delimited
    # JSON (NDJSON) rather than returned as one JSON body, so the client can
    # show "retrying..." feedback live instead of just hanging for however
    # long the retry loop below takes (see client.js's readTranslateStream()).
    # Zero or more progress lines are emitted first:
    #   {"status": "retrying", "attempt": <next attempt #>, "maxAttempts": N,
    #    "delaySeconds": <float>, "rotatedKey": <bool>}
    # For the single-connection path specifically (router_only_group_mode
    # False - see stream_translation() below), exactly two more progress
    # lines are emitted ahead of the retry loop, so the client has
    # something better than a bare spinner for the two real waits that
    # happen before the first byte of SQL comes back - reading the schema,
    # then the LLM call itself:
    #   {"status": "phase_status", "phase": "schema"|"generating_sql",
    #    "message": "<short human-readable sentence>"}
    # This is deliberately just a label, not a progress bar - it doesn't
    # shrink either wait, it just tells the user which one they're in.
    # Router ("all databases") mode emits its OWN two "phase_status" lines
    # first, ahead of ITS two real pre-SQL waits - collecting every in-
    # scope connection's schema summary (build_router_candidate_summaries)
    # and the triage LLM call itself (triage_all_mode_question) - using
    # the exact same event shape, just different `phase` values:
    #   {"status": "phase_status", "phase": "collecting_schema_summaries"
    #    |"routing", "message": "<short human-readable sentence>"}
    # These used to not exist at all: before them, a router-mode request
    # streamed NOTHING for however long those two steps took (both can be
    # genuinely slow - schema collection scales with how many connections
    # are in scope, and triage now has its own real retry/key-rotation
    # budget, see triage_all_mode_question's docstring), leaving the
    # client with no visible progress until the FIRST event it could
    # otherwise render - phase_a_route - which only ever arrives once
    # triage has already fully finished, and only for the "route" outcome
    # (an "answer"/"failed" outcome gets no intermediate event at all
    # under the OLD design). router mode's own, more informative per-
    # connection progress (phase_a_route/phase_b_connection_done below)
    # still only starts once triage itself resolves to "route" - these two
    # new lines are what covers everything before that point, for every
    # outcome.
    # ...followed by exactly one terminal line:
    #   {"status": "done", "success": true, "sql": ..., "input_tokens": ...,
    #    "output_tokens": ..., "total_tokens": ..., "thinking_tokens": ...,
    #    "cached_content_tokens": ..., "duration": ...}
    #   or, on failure (non-retryable, or every retry exhausted):
    #   {"status": "done", "success": false, "error": "..."}
    #
    # IMPORTANT: because the HTTP status code has to be committed before any
    # of this streams - a chunked response can't retroactively become a 500
    # once a byte of it has already gone out - every request that makes it
    # this far now always returns HTTP 200, whether the translation itself
    # ultimately succeeds or fails. Failure lives in the terminal line's
    # "success"/"error" fields, not the HTTP status - callers (and tests)
    # must check that field. This is the one behavior change from before
    # streaming existed, where a failed translation was a real HTTP 500.
    # (The two early validation returns above - missing API key, empty
    # prompt - happen before any of this and keep their real 400 status,
    # since nothing has streamed yet at that point.)
    def stream_translation():
        nonlocal api_key
        cancel_token = None
        cancel_handle = None
        try:
            client = provider.make_client(api_key)
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)

            if router_only_group_mode:
                # "All databases" mode's real two-phase flow (see
                # connection_router.triage_all_mode_question and
                # _run_phase_b_fanout above): a triage call decides
                # whether the question can be answered directly from
                # table names alone, or genuinely needs real data from
                # one or more specific connections - and if so, generates
                # and executes real SQL against each of them,
                # independently and in parallel, exactly as if the user
                # had selected each one directly and asked the question
                # themselves. This is a complete, self-contained branch
                # that returns its own terminal NDJSON line directly, same
                # as the old Phase-A-only stub it replaces.
                #
                # Always runs triage regardless of how many connections
                # are configured, even just one - unlike the old stub,
                # which skipped the LLM call entirely when there was
                # "nothing to route between." Under this design that skip
                # would be wrong: even with one configured connection, the
                # triage call still decides "answer directly" vs.
                # "actually go query this database," so skipping it would
                # mean a single-connection "all" session could never get
                # real SQL - defeating the point of this feature for that
                # case.
                start_time = time.perf_counter()

                # First of router mode's two phase_status lines (see the
                # module docstring above) - collecting every in-scope
                # connection's schema summary can be a real, visible wait
                # (scales with how many connections are in scope; each
                # summary is itself schema_cache-backed, so this is fast
                # on a warm cache but not on a cold one or a forced
                # refresh), and previously had no progress indicator at
                # all. Both this and "routing" just below share the single
                # user-facing "Triaging…" label (see the app's own
                # canonical 4-message progress vocabulary - Triaging/
                # Generating SQL/Fetching Results/Summarizing - client.js's
                # showPhaseStatus()'s docstring has the full list) - the
                # distinct `phase` KEY still separates them for anything
                # that inspects the stream programmatically (see
                # test_translate_routes.py's phase-sequence assertions),
                # only the human-readable `message` is now shared.
                yield json.dumps({
                    "status": "phase_status",
                    "phase": "collecting_schema_summaries",
                    "message": "Triaging…",
                }) + "\n"
                candidate_summaries = build_router_candidate_summaries(in_scope_entries, user_identity)

                # Second of router mode's two phase_status lines - the
                # triage LLM call itself, which now carries its own real
                # retry/key-rotation budget (see run_triage_call's
                # docstring), so this wait can be the longest one and
                # previously had no progress indicator at all either.
                yield json.dumps({
                    "status": "phase_status",
                    "phase": "routing",
                    "message": "Triaging…",
                }) + "\n"
                # yield from (not a plain call) - run_triage_call is a
                # generator that yields live "retrying" NDJSON lines
                # whenever its own internal retry loop actually fires (key
                # rotation or a transient-error wait - see its docstring for
                # why this used to be invisible to the client). Forwarding
                # them here means a slow/rate-limited triage call gets the
                # exact same live feedback the single-connection generate-SQL
                # retry loop already gives - client.js needs no changes for
                # this, since 'retrying' is already handled generically
                # regardless of which server-side call produced it.
                #
                # This is the SAME unified triage call single-dataset mode's
                # own triage_single_dataset_question delegates to (see that
                # function's docstring) - here called directly with
                # num_candidates == len(candidate_summaries) and the
                # multi-candidate schema block, instead of through that
                # thin single-dataset-only wrapper.
                triage_result = yield from run_triage_call(
                    len(candidate_summaries), _build_candidate_schema_block(candidate_summaries),
                    prompt, provider, client, llm_model, history=history,
                    api_key=api_key, using_byok=bool(byok_key),
                )
                # Phase A's own elapsed time and LLM usage, isolated from
                # whatever Phase B work (if any) happens next below - NOT
                # logged as its own translations-table row at all (see the
                # "Phase A (triage) is deliberately NEVER recorded" comment
                # further down, where triage_result["outcome"] is switched
                # on): only a call that takes a prompt and generates real
                # SQL gets a row, and triage itself never does, regardless
                # of its outcome.
                triage_duration = round(1000 * (time.perf_counter() - start_time))
                triage_usage = dict(triage_result.get("usage") or {})
                usage_info = dict(triage_usage)
                extra_fields = {}

                if triage_result["outcome"] == "general":
                    # Can be answered from table names/dialects/general
                    # knowledge alone, no real database access needed -
                    # same '*** NO SQL ***' convention/rendering path
                    # client.js already handles with zero changes.
                    generated_sql = "*** NO SQL *** " + triage_result["answer"]
                    triage_log_text = generated_sql
                elif triage_result["outcome"] == "schema":
                    # NEW outcome for group mode (run_triage_call's merge -
                    # see its docstring): opens the group's own Schema
                    # Viewer, mirroring single-dataset mode's identical
                    # handling of this same outcome below. client.js's
                    # translate-response handler needs one small additive
                    # branch for this (IN_SCOPE_MODE === 'group' calls
                    # openGroupSchemaViewer() instead of the single-
                    # connection openSchemaViewer()) - see that file's own
                    # comment at the isOpenSchema branch.
                    generated_sql = "*** NO SQL *** OPEN SCHEMA VIEWER ***"
                    triage_log_text = generated_sql
                elif triage_result["outcome"] == "help":
                    # NEW outcome for group mode, same reasoning as
                    # "schema" just above - opens the (mode-agnostic) Help
                    # modal, which client.js's isOpenHelp branch already
                    # handles identically regardless of IN_SCOPE_MODE, so
                    # this needed no client.js changes at all.
                    generated_sql = "*** NO SQL *** OPEN HELP POPUP ***"
                    triage_log_text = generated_sql
                elif triage_result["outcome"] == "failed":
                    # Triage itself couldn't produce anything usable after
                    # its own bounded retry - deliberately NOT a fallback
                    # guess at some candidate connection: a wrong
                    # running real SQL against a database the user never
                    # asked about, so this shows a fixed apology instead.
                    # WHICH apology depends on WHY it failed (see
                    # triage_all_mode_question's docstring): "api_error"
                    # distinguishes a real technical/capacity failure (the
                    # LLM call itself raised and its own retry budget -
                    # key rotation and/or transient-error retries - ran
                    # out, e.g. every configured Gemini key was out of
                    # capacity) from a response that genuinely came back
                    # unparseable both times. These used to be
                    # indistinguishable, both showing _TRIAGE_FAILURE_TEXT
                    # - actively misleading for the api_error case, since
                    # it reads as "I couldn't understand your question"
                    # when the honest answer is a real, specific API/
                    # capacity problem - format_llm_error_for_user() below
                    # builds that message from triage_result["error"] (the
                    # raw exception - see triage_all_mode_question's
                    # docstring), including the actual provider error text,
                    # not just a generic "try again" apology.
                    if triage_result.get("api_error"):
                        generated_sql = "*** NO SQL *** " + format_llm_error_for_user(
                            provider, llm_model, triage_result["error"], using_byok=bool(byok_key)
                        )
                    else:
                        generated_sql = _TRIAGE_FAILURE_TEXT
                    triage_log_text = generated_sql
                else:  # "sql" - needs real data from specific connection(s)
                    # A group with exactly one in-scope connection calls
                    # run_triage_call with num_candidates == 1, which uses
                    # its single-dataset branch (byte-identical to single-
                    # dataset mode's own triage_single_dataset_question -
                    # see run_triage_call's docstring) - that branch's own
                    # "sql" outcome carries no "indices"/"message"/
                    # "database_prompts" at all, since single-dataset mode
                    # never has anything to pick between. There is still
                    # only one possible candidate here though (the group's
                    # sole member), so that's implicitly "selected" rather
                    # than requiring the model to say so - defaulting to it
                    # keeps a single-connection dataset group able to reach
                    # real SQL at all, the same guarantee this branch's own
                    # module comment above promises for "even just one"
                    # configured connection.
                    indices = triage_result.get("indices")
                    if indices is None:
                        indices = list(range(len(in_scope_entries)))
                    selected_entries = [in_scope_entries[i] for i in indices]
                    # Each connection gets ITS OWN instruction - triage's
                    # own rewrite of `prompt` for that connection alone
                    # when it supplied one (see triage_all_mode_question's
                    # "database_prompts" docstring for why the original
                    # question, verbatim, is frequently wrong once
                    # narrowed to a single connection - e.g. it was phrased
                    # across multiple databases at once), else falling
                    # back to the original `prompt` unchanged for that one
                    # connection - today's original behavior, preserved
                    # per-connection rather than failing the rewrite
                    # entirely.
                    database_prompts_by_index = triage_result.get("database_prompts") or {}
                    entry_prompts = [
                        database_prompts_by_index.get(i) or prompt for i in indices
                    ]
                    # Each connection's OWN merged history (Chunk 5 - see
                    # connection_histories' own declaration comment above)
                    # looked up by the exact same "kind:id" string
                    # client.js's connectionBucketKey() builds - `.get(...)
                    # or []` covers both an old client that never sent this
                    # field at all and a connection this dict simply has no
                    # entry for yet, falling back to empty history either
                    # way rather than erroring. Re-truncated here with the
                    # same HISTORY_MAX_TURNS bound `history` above already
                    # got - a per-connection bucket is capped client-side
                    # too (createChatHistoryStore's own maxTurns), but this
                    # is the same defensive belt-and-suspenders re-slice
                    # every other history value in this module gets.
                    entry_histories = [
                        (connection_histories.get(f"{e['kind']}:{e['id']}") or [])[-(HISTORY_MAX_TURNS * 2):]
                        for e in selected_entries
                    ]
                    # Both computed BEFORE Phase B even starts (unlike
                    # before this streaming redesign, when routing_message
                    # was only computed once Phase B had already fully
                    # returned) - triage resolving to "route" is all
                    # either of these needs, and the client needs them
                    # immediately: the Summary tab text and one placeholder
                    # tab per selected connection, well before any single
                    # connection's own generation call has finished.
                    # The server-built fallback (the model's own "message"
                    # was empty/missing) gets the same label-line header the
                    # model is instructed to lead with itself (see
                    # _TRIAGE_SYSTEM_INSTRUCTION), so the Summary tab always
                    # shows one regardless of which source this text came
                    # from. Deliberately hardcoded English here, unlike the
                    # model's own (translated) label: this is a last-resort
                    # fallback with no LLM call of its own to ask for a
                    # translation from, and only ever fires when the model
                    # itself failed to provide a usable "message" - routing
                    # still succeeds either way, this is strictly cosmetic.
                    routing_message = triage_result.get("message") or (
                        "Triage\n\nChecking " + ", ".join(e["name"] for e in selected_entries) + " for your question."
                    )
                    # `prompt` (new - Chunk 4 of "splitting SQL/summary per
                    # in-scope database") is this database's own entry from
                    # `entry_prompts` above - triage's per-connection
                    # rewrite of the user's original question when it
                    # supplied one, else that original question unchanged
                    # (see entry_prompts' own comment just above). Every
                    # OTHER consumer of connection_selection (PINNED_
                    # CONNECTIONS' kind/id-only mapping in client.js, every
                    # existing test) only ever reads .kind/.id/.name, so
                    # this is purely additive - added here (rather than a
                    # separate field) so a later per-database history
                    # fan-out has, in one place, everything it needs to
                    # record that database's own {prompt, SQL, results,
                    # summary} tuple: the same list already threaded
                    # through to client.js's allModeStreamState.
                    # connectionOrder (see startAllModeStreaming()) and
                    # pendingAllModeNotes.
                    #
                    # `type` (new - GA4 fan-out tracking): each entry's own
                    # dialect, pulled from its resolved descriptor
                    # (resolve_in_scope_descriptors always sets "type" -
                    # see db.py's _to_descriptor default) - client.js's
                    # trackAllModeFanoutTranslate()/-Execute() need this to
                    # report a real per-database `database_type` on the GA
                    # events fired for each connection in this fan-out,
                    # rather than the single active connection's type (or
                    # none at all), which is all client.js could see before
                    # this field existed. Purely additive, same reasoning
                    # as `prompt` above.
                    connection_selection = [
                        {
                            "kind": e["kind"], "id": e["id"], "name": e["name"],
                            "type": (e.get("descriptor") or {}).get("type", ""),
                            "prompt": p,
                        }
                        for e, p in zip(selected_entries, entry_prompts)
                    ]
                    yield json.dumps({
                        "status": "phase_a_route",
                        "routing_message": routing_message,
                        "connection_selection": connection_selection,
                    }) + "\n"

                    # Manually drains _run_phase_b_fanout (a generator -
                    # see its own docstring) rather than a plain `yield
                    # from`, since each per-completion event needs to be
                    # wrapped into its OWN NDJSON status line here, unlike
                    # generate_sql_for_connection's retry-progress `yield
                    # from` above, which forwards already-final dicts
                    # unchanged. The final four-value aggregate - the same
                    # shape a single blocking call to this function used
                    # to return before this redesign - is captured via
                    # StopIteration.value, the same idiom _drain_generation
                    # uses.
                    phase_b_gen = _run_phase_b_fanout(
                        selected_entries, entry_prompts, entry_histories,
                        provider, llm_model, user_identity, force_schema_refresh,
                    )
                    try:
                        while True:
                            done_entry, classified = next(phase_b_gen)
                            yield json.dumps({
                                "status": "phase_b_connection_done",
                                "kind": done_entry["kind"], "id": done_entry["id"], "name": done_entry["name"],
                                # Same "type" field/reasoning as
                                # connection_selection above - lets
                                # executeOneAllModeConnection() in client.js
                                # report a real per-database database_type
                                # on the sql_fanout_executed GA event it
                                # fires for THIS connection's own streamed
                                # auto-execute, without a second lookup.
                                "type": (done_entry.get("descriptor") or {}).get("type", ""),
                                **classified,
                            }) + "\n"
                    except StopIteration as stop:
                        sql_blocks, database_notes, generation_failures, phase_b_usage, phase_b_log_entries = stop.value

                    generated_sql = "\n\n".join(marked for _, marked in sql_blocks)
                    # Per-database structured equivalent of the joined
                    # `generated_sql` string above - the whole reason
                    # sql_blocks (from _run_phase_b_fanout) is a list of
                    # (entry, marked_sql) pairs in the first place, rather
                    # than already-joined text, is so a per-database view
                    # of "what SQL did THIS database get" doesn't need to
                    # be reconstructed later by re-parsing the combined
                    # string's own '-- database: ...' markers. `sql`
                    # above stays exactly as it's always been (joined,
                    # marker-tagged) for every existing consumer (the
                    # response's own 'sql' field, the SQL editor box,
                    # existing tests) - history logging below now uses
                    # `phase_b_log_entries` instead, one dedicated row per
                    # connection, rather than this joined string. This is
                    # purely additive, feeding a future per-database-history
                    # feature (and any other future per-database
                    # consumer) without touching anything that already
                    # depends on the flattened shape. Only ever present
                    # for databases that actually returned real SQL - a
                    # database that noted or failed instead has no entry
                    # here, same as it has no entry in `sql_blocks`
                    # itself; database_notes/generation_failures below
                    # remain the source of truth for those two cases.
                    sql_by_database = [
                        {"kind": entry["kind"], "id": entry["id"], "name": entry["name"], "sql": marked}
                        for entry, marked in sql_blocks
                    ]
                    for k in phase_b_usage:
                        # `or 0` on both sides - same None-vs-missing-key
                        # defensive reasoning as _run_phase_b_fanout's own
                        # usage_totals loop above.
                        usage_info[k] = (usage_info.get(k) or 0) + (phase_b_usage.get(k) or 0)
                    extra_fields = {
                        # A list, even for a length-1 pick - present for
                        # EVERY selected connection regardless of whether
                        # it ended up with real SQL, a note, or a
                        # failure, so a follow-up turn's pinned_connections
                        # can still reuse this turn's routing decision
                        # even if Phase B partially failed/noted. Also
                        # what drives client.js's existing disclosure
                        # banner/pin-handling code - unchanged, since it
                        # already fires generically for any response
                        # carrying this field. Same list already sent in
                        # the phase_a_route line above.
                        "connection_selection": connection_selection,
                        # Marks this as the new "route" shape for
                        # client.js (byte-identical to today's response
                        # for the "answer"/"failed" outcomes above - zero
                        # client changes needed for those two).
                        "router_route": True,
                        "routing_message": routing_message,
                        "database_notes": database_notes,
                        "generation_failures": generation_failures,
                        "sql_blocks": sql_by_database,
                    }
                    # Phase A's own text isn't real SQL - "route" just
                    # means it decided real data was needed and picked
                    # who to ask, same '*** NO SQL ***' convention as the
                    # "answer"/"failed" outcomes above use for their own
                    # non-SQL text.
                    triage_log_text = "*** NO SQL *** " + routing_message

                end_time = time.perf_counter()
                duration = round(1000 * (end_time - start_time))
                input_tokens = usage_info.get("input_tokens", 0)
                output_tokens = usage_info.get("output_tokens", 0)
                total_tokens = usage_info.get("total_tokens", 0)
                thinking_tokens = usage_info.get("thinking_tokens", 0)
                cached_content_tokens = usage_info.get("cached_content_tokens", 0)

                # Phase A (triage) is deliberately NEVER recorded in the
                # translations-table history/stats - only calls that take a
                # prompt and generate real SQL are (Phase B's own per-
                # connection generation below, single-connection mode's own
                # Call 2) - triage and summarization calls aren't useful
                # there and would just pollute the stats.

                if triage_result["outcome"] == "sql":
                    # One dedicated translations-table row PER SELECTED
                    # CONNECTION - not one combined row for the whole batch
                    # attributed only to the first connection, which is
                    # what this used to do (see phase_b_log_entries'
                    # docstring in _run_phase_b_fanout for the full
                    # reasoning). Each entry already carries its own real,
                    # independently-measured duration and its own usage
                    # (zeroed for a failure) - no derived "share of the
                    # total" math needed here at all, since Phase A's
                    # duration/usage was already logged separately above
                    # and every Phase B call's own elapsed time was
                    # measured directly, not inferred from a shared total.
                    for log_entry in phase_b_log_entries:
                        usage = log_entry["usage"]
                        record_translation(
                            user_identity, log_entry["entry"]["descriptor"], log_entry["prompt"],
                            log_entry["sql_command"], llm_model, log_entry["duration"],
                            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                            usage.get("total_tokens", 0), usage.get("thinking_tokens", 0),
                            usage.get("cached_content_tokens", 0),
                        )

                # `sql` may legitimately be "" here (every selected
                # connection returned a note or failed) - still
                # `success: True`, not an error, so the client renders
                # per-database detail from database_notes/
                # generation_failures instead of collapsing to a flat
                # error block.
                yield json.dumps({
                    'status': 'done',
                    'success': True,
                    'sql': generated_sql,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'total_tokens': total_tokens,
                    'thinking_tokens': thinking_tokens,
                    'cached_content_tokens': cached_content_tokens,
                    'duration': duration,
                    **extra_fields,
                }) + "\n"
                return

            # Byte-for-byte the same single-connection path this endpoint
            # has always run - see this module's docstring. Only reached
            # when router_only_group_mode is False (its own branch above
            # always returns before falling through to here), i.e. for the
            # overwhelming majority of sessions today: in_scope_mode isn't
            # "all", or an explicit database_url override is in play.
            #
            # Two-call redesign (see the module-level section comment above
            # _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION for the full
            # reasoning): Call 1 (triage_single_dataset_question) classifies
            # `prompt` into general knowledge/schema/help/SQL using only
            # this dataset's cheap SHALLOW schema; Call 2 (the JSON-
            # enveloped SQL-generation call below, inlined here exactly the
            # way this whole path already was before this redesign) only
            # ever runs once Call 1 resolves to "sql", and only then pays
            # the full DEEP schema fetch's cost. This mirrors dataset-group
            # mode's own Phase A/Phase B split one level down - see that
            # section comment for how closely.
            #
            # First of this path's phase_status lines (see the module
            # docstring above) - the shallow schema lookup is usually a
            # cache hit and near-instant, but can be a real, visible wait on
            # a cold cache or an explicit refresh_schema request, and the
            # client has no other way to distinguish "still building the
            # prompt" from "waiting on the model" without this.
            yield json.dumps({
                "status": "phase_status",
                "phase": "schema",
                "message": "Triaging…",
            }) + "\n"
            triage_schema = get_triage_schema_text(conn_str, user_identity, force_refresh=force_schema_refresh)
            triage_schema_block = f"Database Schema (overview):\n{triage_schema}\n\n"

            try:
                dialect_name = get_backend(conn_str).dialect_name
            except Exception:
                dialect_name = "PostgreSQL"
            dialect_intro = _DIALECT_PROMPT_INTROS.get(dialect_name, _DEFAULT_DIALECT_PROMPT_INTRO)

            # The key-ROTATION retry budget (see LlmProvider.
            # supports_key_rotation's docstring) - sized to how many keys
            # are actually configured for a provider that supports it
            # (Gemini today - see _classify_gemini_error's 429 case), or 1
            # (meaning "already exhausted, since tried_llm_keys already has
            # one key in it") for a provider that doesn't, making the
            # rotate_key branch of Call 2's own retry loop below
            # effectively unreachable for Claude/OpenAI, exactly as before
            # this dispatch existed. tried_llm_keys already starts as
            # {api_key} (set above, before this generator runs), so it's
            # the natural running total of distinct keys tried BY CALL 2 -
            # Call 1 tracks its own, independent key-rotation budget
            # internally (triage_single_dataset_question is given no
            # explicit tried_keys, so it starts fresh from {api_key}
            # itself), the same independent-per-call-budget precedent
            # dataset-group mode's own Phase A/Phase B split already
            # established (triage_all_mode_question's own internal
            # rotation is never threaded into _run_phase_b_fanout either).
            # A "Bring Your Own Key" forces this down to 1 (already met by
            # tried_llm_keys' own starting size), same reasoning as
            # generate_sql_for_connection's own using_byok parameter -
            # there's no second key of the user's own to rotate to, so this
            # loop's rotate_key branch below is made unreachable exactly
            # the same way it already is for a provider that doesn't
            # support rotation at all.
            key_pool_size = 1 if byok_key else provider.get_key_pool_size()

            # Second of this path's phase_status lines - Call 1 itself,
            # which carries its own real retry/key-rotation budget (see
            # triage_single_dataset_question's docstring), so this wait can
            # be more than instantaneous and previously had no progress
            # indicator of its own distinct from the old single call's.
            yield json.dumps({
                "status": "phase_status",
                "phase": "triage",
                "message": "Triaging…",
            }) + "\n"

            triage_start_time = time.perf_counter()
            # yield from (not a plain call) - triage_single_dataset_question
            # is a generator that yields live "retrying" NDJSON lines
            # whenever its own internal retry loop actually fires (key
            # rotation or a transient-error wait) - forwarded here exactly
            # like dataset-group mode's own `yield from
            # triage_all_mode_question(...)` above, so a slow/rate-limited
            # Call 1 gets the same live feedback Call 2's own retry loop
            # below already gives. client.js needs no changes for this -
            # 'retrying' is already handled generically regardless of which
            # server-side call produced it.
            triage_result = yield from triage_single_dataset_question(
                triage_schema_block, prompt, provider, client, llm_model, history=history,
                api_key=api_key, using_byok=bool(byok_key),
            )
            triage_duration = round(1000 * (time.perf_counter() - triage_start_time))
            triage_usage = dict(triage_result.get("usage") or {})

            # Adds Call 1's own token usage on top of whatever Call 2 (if
            # it runs at all) separately reports, for the ONE combined
            # {input,output,total,thinking,cached_content}_tokens total
            # this turn's own single record_translation row/'done' response
            # report - unlike dataset-group mode's own Phase A/Phase B
            # split (where Phase A/triage is never logged as a row at all -
            # see the "Phase A (triage) is deliberately NEVER recorded"
            # comment in that mode's own branch - and Phase B logs one row
            # PER SELECTED CONNECTION, not one per phase), single-dataset
            # mode has always logged exactly one row per turn, and this
            # redesign doesn't change that invariant - it only makes that
            # one row's token counts honest about BOTH LLM calls that made
            # up the turn, instead of silently dropping Call 1's own
            # contribution the moment Call 2 runs and its own usage dict
            # would otherwise just overwrite this variable outright.
            def _combined_usage(other_usage):
                other_usage = other_usage or {}
                return {
                    k: (triage_usage.get(k) or 0) + (other_usage.get(k) or 0)
                    for k in ("input_tokens", "output_tokens", "total_tokens",
                              "thinking_tokens", "cached_content_tokens")
                }

            # Outcomes 1-3 (general/schema/help) are each a COMPLETE,
            # self-contained turn on their own - Call 2 never runs for any
            # of them, satisfying the "skip the follow-up summarization
            # step unless Call 2 actually ran" requirement this redesign
            # was also asked to meet: client.js's executeSql() (the one
            # thing that ever triggers /api/summarize-result) is only
            # reached from its own final `else` branch, i.e. only when
            # `sql` is neither an OPEN HELP POPUP/OPEN SCHEMA VIEWER/
            # '*** NO SQL ***' reply nor empty - exactly the shape only
            # Call 2's own real-SQL outcome below ever produces. Each of
            # these three branches reuses the EXACT marker strings client.js
            # already string-matches on (see this module's own docstring on
            # why that makes this a zero-client-changes redesign), built
            # here server-side now instead of by the model itself - there
            # is no more risk of a model spuriously emitting (or forgetting)
            # one of these, since Call 1 only ever chooses among four fixed
            # JSON "action" values, never free-form marker text.
            if triage_result["outcome"] in ("general", "schema", "help", "failed"):
                if triage_result["outcome"] == "general":
                    generated_sql = "*** NO SQL *** " + triage_result["answer"]
                elif triage_result["outcome"] == "schema":
                    generated_sql = "*** NO SQL *** OPEN SCHEMA VIEWER ***"
                elif triage_result["outcome"] == "help":
                    generated_sql = "*** NO SQL *** OPEN HELP POPUP ***"
                else:
                    # Call 1 itself couldn't produce anything usable after
                    # its own bounded retry - deliberately NOT a fallback
                    # guess at "general"/"schema"/"help"/"sql": a wrong
                    # guess here could mean silently running real SQL the
                    # user never actually asked for. WHICH apology depends
                    # on WHY it failed - same "api_error" distinction (and
                    # the same format_llm_error_for_user()/_TRIAGE_FAILURE_
                    # TEXT choice between them) as dataset-group mode's own
                    # triage failure handling above; see triage_single_
                    # dataset_question's docstring for what "api_error"
                    # means here.
                    if triage_result.get("api_error"):
                        generated_sql = "*** NO SQL *** " + format_llm_error_for_user(
                            provider, llm_model, triage_result["error"], using_byok=bool(byok_key)
                        )
                    else:
                        generated_sql = _TRIAGE_FAILURE_TEXT

                input_tokens = triage_usage.get("input_tokens", 0)
                output_tokens = triage_usage.get("output_tokens", 0)
                total_tokens = triage_usage.get("total_tokens", 0)
                thinking_tokens = triage_usage.get("thinking_tokens", 0)
                cached_content_tokens = triage_usage.get("cached_content_tokens", 0)
                # Call 1 (triage) is deliberately NEVER recorded in the
                # translations-table history/stats - only calls that take a
                # prompt and generate real SQL are (Call 2 below) - triage
                # and summarization calls aren't useful there and would
                # just pollute the stats.
                yield json.dumps({
                    'status': 'done',
                    'success': True,
                    'sql': generated_sql,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'total_tokens': total_tokens,
                    'thinking_tokens': thinking_tokens,
                    'cached_content_tokens': cached_content_tokens,
                    'duration': triage_duration,
                }) + "\n"
                return

            # triage_result["outcome"] == "sql" from here on - Call 1
            # decided this prompt genuinely needs real SQL generated
            # against real data, so (and only so) this now pays for the
            # full DEEP schema fetch (unlike Call 1's own cheap shallow
            # fetch above) and runs Call 2.
            #
            # Call 1 (triage) is deliberately NEVER recorded in the
            # translations-table history/stats, even here where it decided
            # real SQL is needed - only Call 2 (the actual SQL-generation
            # call just below) gets its own row. Triage's own duration/
            # usage is still folded into the CLIENT-facing 'done' response
            # below (via _combined_usage) so the reported cost for the turn
            # stays honest about both calls - only the DB row is
            # triage-free now.
            schema = get_llm_schema_text(conn_str, user_identity, force_refresh=force_schema_refresh)
            system_instruction = dialect_intro + _SQL_GENERATION_FORMAT_RULES
            schema_block = f"Database Schema:\n{schema}\n\n"

            # Sequencing matters here for prompt-caching purposes: the schema
            # is large and identical across every call in a given session
            # (barring a schema refresh), so it belongs as far to the front
            # of the input as possible - ahead of history, and ahead of the
            # ever-different new prompt. It can't be its own leading
            # message, though: Claude's Messages API rejects two consecutive
            # same-role messages ("roles must alternate between user and
            # assistant"), and history already starts with a "user" turn, so
            # a standalone schema-only user message in front of it would
            # violate that. Instead it's prepended onto whichever message
            # actually comes first - history's oldest turn when there is
            # history, or the new prompt itself on a conversation's very
            # first call. Either way the final order is system prompt ->
            # schema -> history -> new prompt. build_llm_input() below is
            # where each provider decides exactly how (see LlmProvider.
            # build_llm_input's docstring and each subclass's own).
            new_prompt_content = f"User Request: {prompt}\n\nJSON response:"

            # Computed once, up front, off the user's own prompt - see
            # _no_sql_language_mismatch's docstring for the full picture.
            # None (detection unavailable or too low-confidence) disables
            # the check entirely below, exactly like every other call site
            # that threads this through.
            expected_language_code = _detect_language(prompt)

            # Third of this path's phase_status lines - emitted once, right
            # before Call 2's own retry loop below makes its first attempt.
            # This is the wait that's normally the longest one and the one
            # the "just a spinner" complaint was really about; a "retrying"
            # line (if any) will naturally overwrite this same banner
            # once/if the loop below actually needs one.
            yield json.dumps({
                "status": "phase_status",
                "phase": "generating_sql",
                "message": "Generating SQL…",
            }) + "\n"

            start_time = time.perf_counter()
            generated_sql = ""
            usage_info = {}
            # Bounded 2-attempt outer loop, same "1 real attempt + 1
            # corrective retry" budget _summarize_with_retry/triage_single_
            # dataset_question use for the exact same reason - covers BOTH
            # an unparseable JSON response (new - Call 2's own response is
            # now JSON-enveloped, see _parse_sql_generation_response) and a
            # language mismatch on a "cannot_answer_reason" reply (the
            # closest surviving equivalent of the old design's '*** NO SQL
            # ***' free-text replies), sharing the same 2-attempt budget
            # exactly like triage_all_mode_question's own outer loop treats
            # its own "unparseable" and "wrong language" cases as one
            # shared budget rather than two independent ones. Ordinary
            # generated SQL never enters either retry branch below at all -
            # see _no_sql_language_mismatch's docstring for why that check
            # is deliberately scoped to free text only.
            for language_attempt in range(2):
                llm_input = provider.build_llm_input(history, schema_block, new_prompt_content)
                # transient_attempt tracks the SHARED, both-providers budget
                # for same-key/after-a-delay retries (MAX_TRANSLATION_ATTEMPTS)
                # - it's advanced only by the "else" (non-rotate) branch
                # below, and reset fresh on each language_attempt: a
                # language-mismatch retry is a brand new call, not a
                # continuation of whatever transient-error budget the
                # previous attempt happened to consume. The Gemini-only
                # key-rotation budget above is tracked separately via
                # tried_llm_keys/gemini_key_pool_size, so a run of 429s
                # doesn't eat into this counter at all, and vice versa.
                transient_attempt = 1
                raw_response = ""
                try:
                    while True:
                        try:
                            raw_response, usage_info = provider.call(client, llm_model, llm_input, system_instruction)
                            break
                        except Exception as e:
                            retry_action = provider.classify_error(e)
                            if retry_action is None:
                                raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e

                            if retry_action["rotate_key"]:
                                # Key-rotation budget: one attempt per configured
                                # key. Checked BEFORE picking the next key (rather
                                # than relying on pick_api_key's own fallback-to-
                                # full-pool behavior) so exhaustion is decided here,
                                # not masked by that fallback.
                                if len(tried_llm_keys) >= key_pool_size:
                                    raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e
                                next_key = provider.pick_api_key(exclude=tried_llm_keys)
                                if next_key != api_key:
                                    api_key = next_key
                                    client = provider.make_client(api_key)
                                tried_llm_keys.add(api_key)
                                # No "in %ds" here - a key-rotation retry always
                                # fires immediately (see _classify_gemini_error's
                                # comment for why waiting doesn't make sense when
                                # the next attempt already uses a different key).
                                logger.warning(
                                    "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                                    provider.name, len(tried_llm_keys), key_pool_size, e
                                )
                                # Told to the client before continuing, so
                                # "retrying..." is visible even though there's no
                                # delay to speak of.
                                yield json.dumps({
                                    "status": "retrying",
                                    "attempt": len(tried_llm_keys),
                                    "maxAttempts": key_pool_size,
                                    "delaySeconds": 0,
                                    "rotatedKey": True,
                                }) + "\n"
                                continue

                            # Shared transient-error budget (both providers).
                            if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                                raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e
                            logger.warning(
                                "%s call failed (attempt %d/%d), retrying in %ds: %s",
                                provider.name, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e
                            )
                            # Told to the client before sleeping, not after, so
                            # "retrying..." is visible for the full delay instead of
                            # appearing right as the next attempt actually fires.
                            yield json.dumps({
                                "status": "retrying",
                                "attempt": transient_attempt + 1,
                                "maxAttempts": MAX_TRANSLATION_ATTEMPTS,
                                "delaySeconds": retry_action["delay"],
                                "rotatedKey": False,
                            }) + "\n"
                            transient_attempt += 1
                            if retry_action["delay"]:
                                time.sleep(retry_action["delay"])
                            continue
                except LlmCallFailed as e:
                    # Every attempt (and, for Gemini, every configured key) is
                    # exhausted - this is the ONE exception type raised only
                    # from inside this retry loop, so reaching here means the
                    # LLM genuinely was called and genuinely never returned
                    # usable SQL, as opposed to e.g. a schema-fetch failure
                    # before this loop even started (those fall through to the
                    # generic `except Exception` below, unlogged, since no LLM
                    # call was ever attempted for them). A real API failure
                    # like this is never retried for language reasons - it
                    # fails outright here exactly as it always has, on either
                    # language_attempt.
                    #
                    # duration is measured from the SAME start_time the success
                    # path uses (captured once, before this whole outer loop) -
                    # so, same as a successful later-attempt call, it already
                    # includes every attempt's own call time plus every
                    # inter-attempt wait/rotation (transient AND, now,
                    # language-driven) - plus triage_duration, Call 1's own
                    # already-measured elapsed time, so this turn's one
                    # reported duration honestly covers BOTH LLM calls, not
                    # just Call 2's own share of it.
                    call2_duration = round(1000 * (time.perf_counter() - start_time))
                    error_message = str(e)
                    # Call 2's own usage_info was never populated (it's only
                    # ever assigned on a successful provider.call() return
                    # above), so its own contribution here is a real, honest
                    # 0 - not a placeholder standing in for tokens that were
                    # actually spent. Logged as ITS OWN dedicated row here
                    # (Call 1's own row, with Call 1's own real, non-zero
                    # usage, was already logged separately right after
                    # triage resolved to "sql" - see that record_translation
                    # call above) - matching dataset-group mode's own "each
                    # LLM call gets its own row" convention rather than
                    # folding both calls' usage into one combined row the
                    # way this turn used to.
                    record_translation(
                        user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model,
                        call2_duration, 0, 0, 0, 0, 0,
                    )
                    yield json.dumps({
                        'status': 'done',
                        'success': False,
                        'error': error_message,
                    }) + "\n"
                    return

                parsed_generation = _parse_sql_generation_response(raw_response)
                if parsed_generation is None:
                    # Unparseable JSON - shares Call 2's own 2-attempt
                    # budget (this loop's `language_attempt` counter) with
                    # the language-mismatch case just below, rather than a
                    # separate budget of its own - see this loop's own
                    # section comment above.
                    if language_attempt + 1 < 2:
                        logger.warning(
                            "SQL-generation response could not be parsed as the required JSON object "
                            "(attempt %d/2) - discarding, retrying with an explicit correction",
                            language_attempt + 1,
                        )
                        new_prompt_content = (
                            f"{new_prompt_content}\n\nCORRECTION: your previous response could not be parsed - "
                            f"it must be ONLY a JSON object with exactly one of \"sql\"/\"cannot_answer_reason\" "
                            f"populated, no markdown fences, no other text. Respond again, from scratch, in "
                            f"that exact shape."
                        )
                        continue
                    logger.warning(
                        "SQL-generation response still unparseable after retrying - failing this turn"
                    )
                    # Call 2's OWN elapsed time - logged as its own
                    # dedicated row below (Call 1's own row was already
                    # logged separately above).
                    call2_duration = round(1000 * (time.perf_counter() - start_time))
                    error_message = (
                        "I wasn't able to produce a usable response to your prompt, even after retrying. "
                        "Try rephrasing your question."
                    )
                    # Real usage WAS spent by Call 2 itself (it succeeded,
                    # twice) - logged honestly as ITS OWN row here, rather
                    # than combined with Call 1's own already-logged usage
                    # (see the new record_translation call right after
                    # triage resolved to "sql", above) - matching dataset-
                    # group mode's own "each LLM call gets its own row"
                    # convention.
                    record_translation(
                        user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model,
                        call2_duration, usage_info.get("input_tokens", 0), usage_info.get("output_tokens", 0),
                        usage_info.get("total_tokens", 0), usage_info.get("thinking_tokens", 0),
                        usage_info.get("cached_content_tokens", 0),
                    )
                    yield json.dumps({
                        'status': 'done',
                        'success': False,
                        'error': error_message,
                    }) + "\n"
                    return

                if parsed_generation["outcome"] == "sql":
                    # Real SQL - never enters the language-mismatch check
                    # below at all, unchanged from before this redesign.
                    generated_sql = parsed_generation["sql"]
                    break

                # "cannot_answer" - the closest surviving equivalent of the
                # old design's '*** NO SQL ***' free-text replies (Call 2's
                # own structured escape hatch for "I can't confidently
                # generate SQL for this, and here's why" - see
                # _SQL_GENERATION_FORMAT_RULES). Language verification
                # mirrors this path's own pre-redesign check exactly - see
                # _no_sql_language_mismatch's docstring for what this does
                # and doesn't cover - just reading the free text straight
                # out of "reason" instead of stripping a marker off it.
                free_text = parsed_generation["reason"]
                actual_language_code = _no_sql_language_mismatch(free_text, expected_language_code)
                if actual_language_code is not None:
                    expected_name = _describe_language(expected_language_code)
                    actual_name = _describe_language(actual_language_code)
                    if language_attempt + 1 < 2:
                        logger.warning(
                            "Translation free-text reply came back in %s instead of the prompt's own %s "
                            "(attempt %d/2) - discarding, retrying with an explicit correction",
                            actual_name, expected_name, language_attempt + 1,
                        )
                        # Same "name the mistake and the fix directly"
                        # shape as _summarize_with_retry's own correction
                        # addendum - simply re-asking with the identical
                        # prompt would likely just reproduce the same
                        # wrong-language answer, since whatever pulled
                        # the model toward actual_name (usually
                        # foreign-language schema/data in view) is still
                        # there.
                        new_prompt_content = (
                            f"{new_prompt_content}\n\nCORRECTION: your previous free-text reply to this "
                            f"exact request was written in {actual_name}, which is WRONG - the request was "
                            f"in {expected_name}, so \"cannot_answer_reason\" must be written entirely in "
                            f"{expected_name} this time. Write your full response again, from scratch, "
                            f"entirely in {expected_name} this time."
                        )
                        continue
                    # The one corrective retry is exhausted and the
                    # reply STILL came back in the wrong language -
                    # mirrors _summarize_with_retry's own "never
                    # knowingly serve a response in the wrong language"
                    # guarantee: this turn fails outright (an honest,
                    # specific error) rather than silently showing text
                    # already confirmed to be in the wrong language.
                    logger.warning(
                        "Translation free-text reply still came back in %s instead of %s after "
                        "retrying - failing this turn rather than serving a known-wrong-language response",
                        actual_name, expected_name,
                    )
                    # Call 2's OWN elapsed time - same "its own dedicated
                    # row" reasoning as the unparseable-response branch
                    # above.
                    call2_duration = round(1000 * (time.perf_counter() - start_time))
                    error_message = (
                        f"The response kept coming back in {actual_name} instead of {expected_name}, "
                        f"even after retrying. Try rephrasing your question."
                    )
                    # Unlike the LlmCallFailed path above, real usage WAS
                    # spent by Call 2 itself (it succeeded, twice) - logged
                    # honestly as ITS OWN row here, same reasoning as the
                    # unparseable-response branch just above.
                    record_translation(
                        user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model,
                        call2_duration, usage_info.get("input_tokens", 0), usage_info.get("output_tokens", 0),
                        usage_info.get("total_tokens", 0), usage_info.get("thinking_tokens", 0),
                        usage_info.get("cached_content_tokens", 0),
                    )
                    yield json.dumps({
                        'status': 'done',
                        'success': False,
                        'error': error_message,
                    }) + "\n"
                    return
                generated_sql = "*** NO SQL *** " + free_text
                break
            end_time = time.perf_counter()

            # Call 2's OWN elapsed time/usage - logged as ITS OWN dedicated
            # translations-table row (Call 1's own row, with Call 1's own
            # duration/usage, was already logged separately above, right
            # after triage resolved to "sql") - matching dataset-group
            # mode's own "each LLM call gets its own row" convention
            # instead of collapsing both calls into one combined-usage row
            # the way this turn used to.
            #
            # Anonymous visitors share a single per-session identity
            # (anonymous:<session_id>) rather than a real signed-in one, but
            # the translation is recorded the same way regardless - a
            # write-only audit trail (aggregate usage/cost visibility, e.g.
            # via export_state.py) with no in-app read/purge surface anymore
            # (the /api/history endpoint that used to expose it was removed
            # as dead code once the History modal stopped showing it - see
            # chat_history_routes.py's module docstring for where that
            # modal's data actually comes from today).
            call2_duration = round(1000 * (end_time - start_time))
            record_translation(
                user_identity, conn_str, prompt, generated_sql, llm_model, call2_duration,
                usage_info.get("input_tokens", 0), usage_info.get("output_tokens", 0),
                usage_info.get("total_tokens", 0), usage_info.get("thinking_tokens", 0),
                usage_info.get("cached_content_tokens", 0),
            )

            # The CLIENT still sees ONE combined total for the whole turn
            # (both calls) in the 'done' response below - matching dataset-
            # group mode's own precedent (its 'done' response's usage_info
            # is Phase A + Phase B combined even though they're logged as
            # separate translations-table rows) - this is purely about how
            # this turn's usage is split across DB rows, not a change to
            # what the client is told it cost.
            duration = triage_duration + call2_duration
            combined_usage = _combined_usage(usage_info)
            input_tokens = combined_usage["input_tokens"]
            output_tokens = combined_usage["output_tokens"]
            total_tokens = combined_usage["total_tokens"]
            thinking_tokens = combined_usage["thinking_tokens"]
            cached_content_tokens = combined_usage["cached_content_tokens"]

            yield json.dumps({
                'status': 'done',
                'success': True,
                'sql': generated_sql,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens,
                'thinking_tokens': thinking_tokens,
                'cached_content_tokens': cached_content_tokens,
                'duration': duration,
            }) + "\n"

        except Exception as e:
            logger.exception("Translation failed")
            yield json.dumps({
                'status': 'done',
                'success': False,
                'error': str(e) or f"{type(e).__name__} occurred during translation.",
            }) + "\n"
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()

    # See concurrency_guard.py's own module docstring for why this exists
    # alongside Cloud Run's --concurrency/--max-instances (gcp_deploy.sh).
    # Acquired HERE, right before actually streaming - not any earlier -
    # so the two early-validation returns above (missing API key, empty
    # prompt) never touch this guard at all: they do essentially no work,
    # so they shouldn't consume one of a scarce number of "expensive work"
    # slots. A rejection here keeps the same real, non-streamed HTTP
    # status (503, not 200-with-success-false) those two early returns
    # already use, for the identical reason spelled out in the big comment
    # above stream_translation() - nothing has streamed yet at this point,
    # so an ordinary HTTP status is still possible. {"error": ...} (no
    # "success" key) matches that same early-validation shape exactly,
    # which client.js's readNdjsonStream already reads correctly as a
    # single, non-streamed line straight into `finalData` (see that
    # function's own docstring) - zero client-side changes needed.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'error': 'The server is handling too many translation requests right now. Please try again in a few seconds.',
        })

    def _translation_stream_with_guard_release():
        # Holds the guard slot open for stream_translation()'s ENTIRE
        # lifetime, not just until this route function returns - the
        # generator itself is what does the real (expensive) work, driven
        # lazily by Flask/gunicorn as it sends the response, well after
        # translate_query() has already returned the Response object below.
        # A plain try/finally around the outer call (the way
        # execute_routes.py's @guarded_route decorator works for its own,
        # non-streaming route) would release this slot immediately,
        # before any real work even started. Relies on stream_translation()'s
        # own `finally` (just above) reliably running even if the client
        # disconnects/cancels mid-stream - already something this codebase
        # depends on for cancel_registry's own cleanup there, not a new
        # assumption introduced here.
        try:
            yield from stream_translation()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_translation_stream_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)