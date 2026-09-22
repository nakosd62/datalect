"""
summarize_routes.py

The two post-execution results-summarization pipelines, extracted out of
translate_routes.py (see that module's own docstring for what's left there,
and llm_providers.py's/chart_helpers.py's own docstrings for the two
earlier pieces extracted the same way). Nothing here is new behavior, just
a change of address - including the routes themselves, which now live on
their own Blueprint (summarize_bp) rather than translate_bp, mirroring this
codebase's existing one-blueprint-per-concern-module precedent (auth_bp,
config_bp, execute_bp, chat_history_bp, report_bp) - see server.py's own
docstring/registration list. Moving blueprints doesn't change either
route's URL (no url_prefix is set anywhere in this app) or require any
change to auth.py's EXEMPT_ENDPOINTS (neither route is exempt) or to
rate_limiter.py/concurrency_guard.py (both apply to a route via a plain
decorator/explicit guard object, with no blueprint-name dependency).

Contains TWO parallel, but distinct, pipelines:

  - "All databases" mode's Phase C (summarize_all_mode_results, its own
    prompt/schema/response-parsing helpers, and the /api/summarize-results
    route) - a brief, structured, per-database answer over results Phase B
    gathered from potentially SEVERAL databases at once.

  - Single-connection mode's own post-execution summarization
    (summarize_single_connection_results, its own prompt/schema/response-
    parsing helpers including the "ride-along" chart/visualization
    decision, and the /api/summarize-result route) - a single answer over
    one connection's own just-executed SQL and results.

Both share ONE underlying retry/key-rotation/language-verification
mechanism (_summarize_with_retry, with _default_content_parser/
_default_language_text_extractor as its prose-contract defaults) - see that
function's own docstring for the full reasoning on why this one ~270-line
generator serves two callers whose "valid content" shapes differ (a plain
prose string for single-connection mode's defensive backstop path vs. each
pipeline's own structured JSON content_parser). get_summary_schema_text
(both pipelines' own schema-fetch helper, always reduced to the
"tables_only" derivative) is shared the same way.

translate_routes.py re-imports every name below back into its own
namespace (except the two route functions themselves, which are never
accessed as translate_routes.<name> in any test - only exercised over HTTP
via the Flask test client), so they remain reachable as
translate_routes.<name> for every existing test's
`app_env.translate_routes.<name>` / `env.translate_routes.<name>`
attribute access.

SUMMARY_RESULTS_MAX_ROWS is intentionally computed independently here (same
`os.environ.get("SUMMARY_RESULTS_MAX_ROWS", 100)` read translate_routes.py
itself still does at module level, since app_env.translate_routes.
SUMMARY_RESULTS_MAX_ROWS is asserted directly by existing tests) rather
than imported from translate_routes.py - importing it from there would be
circular, since translate_routes.py imports FROM this module. Both are read
from the same environment variable at import time within the same process,
so they always agree. See llm_providers.py's TRANSLATION_TIMEOUT_SECONDS
docstring note for the identical precedent, including the corresponding
addition of this module to tests/server/helpers.py's _APP_MODULE_NAMES
reload list (needed so a test's own env override actually takes effect
here too, not just in translate_routes.py's copy).
"""
import json
import os
import time

from flask import Blueprint, request, jsonify, Response, stream_with_context

from app_config import logger, state_store, MAX_TRANSLATION_ATTEMPTS
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_database_schema, resolve_descriptor_by_reference
from backends.base import derive_tables_only_schema_text
from connection_router import is_label_only_response, strip_markdown_fence
# `import translate_routes` (the module, not `from translate_routes import
# _detect_language/_describe_language`) - deliberately deferred/indirect:
# several existing tests patch translate_routes._detect_language directly
# (monkeypatch.setattr(app_env.translate_routes, "_detect_language", ...))
# to control the language-verification behavior _summarize_with_retry/
# _build_summary_prompt/_build_single_summary_prompt use below. A plain
# `from language_detect import detect_language as _detect_language` here
# would bind its OWN independent name, which that monkeypatch would never
# reach - unlike the SDK-module-singleton case (llm_providers.py's own
# docstring), a bare function reference isn't shared automatically. Calling
# through translate_routes._detect_language/._describe_language instead
# means whatever's CURRENTLY set there (real or patched) is what actually
# runs. Safe despite translate_routes.py importing FROM this module
# (`from summarize_routes import (...)`, further down that file): by the
# time it reaches that line, translate_routes.py has already executed its
# own `from language_detect import detect_language as _detect_language,
# describe_language as _describe_language`, so those attributes already
# exist on the (still-loading) translate_routes module object by the time
# this import statement runs - and nothing here actually READS
# translate_routes._detect_language until a request/test calls one of the
# functions below, long after both modules have finished loading either way.
import translate_routes
import cancel_registry
from concurrency_guard import TRANSLATE_GUARD, busy_response
from rate_limiter import summarize_rate_limit
from prompt_loader import load_prompt
from llm_providers import format_results_table_text, format_llm_error_for_user, get_llm_provider
from chart_helpers import _pick_chartable_result, _describe_chartable_columns, _clean_visualization

summarize_bp = Blueprint('summarize', __name__)

# See this module's docstring above for why this is computed here
# independently rather than imported from translate_routes.py.
SUMMARY_RESULTS_MAX_ROWS = int(os.environ.get("SUMMARY_RESULTS_MAX_ROWS", 100))


def get_summary_schema_text(descriptor, user_identity):
    """Both summarization call sites' own schema fetch -
    stream_summarize_result() (single-dataset mode's /api/summarize-
    results, via summarize_single_connection_results) and
    _build_all_mode_schema_block() (Phase C's cross-database
    summarization, via summarize_all_mode_results) below.

    Always reduces to the "tables_only" schema derivative (see
    backends/base.py's derive_tables_only_schema_text) - unconditionally,
    regardless of SCHEMA_TABLES_ONLY. Unlike get_llm_schema_text above
    (single-dataset mode's own SQL-GENERATION path, which only reduces
    when that flag is explicitly turned on), summarizing an already-
    executed query's results never needs anything beyond what each
    table/column means - never constraints, indexes, views, grants, or
    any of the other schema-object sections a translate prompt can still
    carry when SCHEMA_TABLES_ONLY is off - so this reduces every time,
    independent of that flag's setting.

    get_database_schema() itself is completely untouched by this (same
    cache key, same cached full deep text either way, for any other
    caller) - this only trims what gets handed onward to the
    summarization LLM from here."""
    schema = get_database_schema(descriptor, user_identity)
    return derive_tables_only_schema_text(schema)


_SUMMARY_SYSTEM_INSTRUCTION = load_prompt("summary_all_databases.txt")


def _build_summary_prompt(user_question, database_results, expected_language_code=None):
    """Renders `database_results` - client-submitted
    [{"name", "sql", "columns", "rows", "rowCount"} | {"name", "note"} |
    {"name", "error"} | {"name", "sql", "error"}, ...], one entry per
    statement result/note/failure Phase B + the client's own /api/execute
    call produced for a "route" outcome turn - into the prompt for Phase
    C's summarization call above: a "SQL executed for each database"
    section (Gap 4 of "Turn History Handling in Datalect" - previously
    Phase C had no SQL in its prompt at all, only the raw results/errors,
    despite the design's own LLM-3 input spec calling for "generated SQL
    for all in-scope databases"), followed by one labeled results block
    per entry, same as before. A note/generation-failure entry has no
    `sql` (nothing was ever generated to run for it) and is simply
    skipped in the SQL section, same as it's already skipped from having
    a results block below.

    Real result rows are capped at SUMMARY_RESULTS_MAX_ROWS (Gap 5's fix
    made this call reason over "the complete results/errors from all tabs
    and databases" per the design doc, matching _build_single_summary_
    prompt's own equivalent cap below - but "complete" with no ceiling at
    all is an abuse/cost vector once every row is actually being sent to
    an LLM call: a single wide `SELECT *` against a huge table would blow
    the prompt out to an enormous token count on this ONE turn alone, no
    history multiplier even needed. SUMMARY_RESULTS_MAX_ROWS is
    deliberately a SEPARATE, far more generous constant than
    HISTORY_RESULT_MAX_ROWS above (see its own definition/comment) - that
    one bounds how much of old, already-summarized history gets replayed
    turn after turn; this one bounds the CURRENT turn's own data, being
    reasoned over exactly once, for exactly the purpose of producing the
    summary that (once persisted - see captureAllModeHistory in client.js)
    is what future turns actually see instead. When a result set is
    actually truncated, the header names both the real total row count and
    how many are shown, so the model - and, since the header text ends up
    in the summary that's replayed as history, later turns too - isn't
    misled into thinking it saw everything.

    `expected_language_code` is _detect_language(user_question)'s result,
    computed once by the caller (summarize_all_mode_results) and threaded
    through here so the trailing reminder can name the target language
    concretely (e.g. "Respond in German.") instead of only the indirect
    "same language as the question" framing - see the language-
    verification section comment above _SUMMARY_SYSTEM_INSTRUCTION for
    why. None (detection unavailable or too low-confidence to trust)
    leaves the reminder exactly as it always was.

    Each per-database results/note/error block is prefixed with its own
    "[i]" (0-based index into `database_results`, matching this project's
    established convention for numbering candidates in a model prompt -
    see connection_router.py's _build_candidate_schema_block's own
    "[{i}] name=..." - so _SUMMARY_SYSTEM_INSTRUCTION's JSON contract can
    key its "per_database" object by that same index and the caller
    (summarize_all_mode_results/_clean_summary_response) can zip the
    parsed response straight back against `database_results` by position,
    with no name-matching or extra bookkeeping needed."""
    sql_blocks = []
    for entry in (database_results or []):
        sql = entry.get("sql")
        if sql:
            name = entry.get("name") or "Unknown database"
            sql_blocks.append(f"{name}:\n{sql}")
    sql_joiner = "\n\n"
    sql_section = f"SQL executed for each database:\n\n{sql_joiner.join(sql_blocks)}\n\n" if sql_blocks else ""

    blocks = []
    for i, entry in enumerate(database_results or []):
        name = entry.get("name") or "Unknown database"
        error = entry.get("error")
        note = entry.get("note")
        if error:
            blocks.append(f"[{i}] {name}: query failed - {error}")
        elif note:
            blocks.append(f"[{i}] {name}: {note}")
        else:
            cols = entry.get("columns") or []
            rows = entry.get("rows") or []
            row_count = entry.get("rowCount", len(rows))
            shown_rows = min(len(rows), SUMMARY_RESULTS_MAX_ROWS)
            header = (
                f"[{i}] {name} - {row_count} row(s):"
                if shown_rows >= row_count
                else f"[{i}] {name} - {row_count} row(s) total, showing the first {shown_rows}:"
            )
            blocks.append(header + "\n" + format_results_table_text(cols, rows, max_rows=SUMMARY_RESULTS_MAX_ROWS))
    results_text = "\n\n".join(blocks) if blocks else "(no databases returned anything)"
    # The trailing reminder repeats _SUMMARY_SYSTEM_INSTRUCTION's own
    # language-matching rule right here, at the very end of the actual
    # user-turn content rather than only up in the system instruction -
    # some models (observed concretely with gpt-5.3-codex on this exact
    # call) weight an instruction placed immediately before generation more
    # heavily than one stated earlier in a long system prompt, so this is
    # deliberate reinforcement/redundancy, not a duplicate to clean up.
    # When a language was confidently detected, name it directly instead
    # of (or rather, in addition to - the indirect framing stays as a
    # fallback for whatever the name-based sentence doesn't cover) asking
    # the model to infer it - a concrete target is harder for a model to
    # drift away from under pressure from foreign-language data than an
    # instruction that requires it to first correctly infer the question's
    # language and then remember to match it several paragraphs later.
    language_name = translate_routes._describe_language(expected_language_code)
    named_language_sentence = (
        f" Concretely: the question above is in {language_name}, so your ENTIRE response - the "
        f"label line and every paragraph - must be written in {language_name}, in full, not "
        f"partially or with any other language mixed in."
        if language_name else ""
    )
    return (
        f"Original question: {user_question}\n\n"
        f"{sql_section}"
        f"Results gathered from each database queried to help answer it:\n\n{results_text}\n\n"
        "Reminder: write your response - the label line AND every paragraph - in the SAME "
        "LANGUAGE as the \"Original question\" above, no matter what language the database/table "
        "names or the results data shown above happen to be in." + named_language_sentence
    )


# is_label_only_response (imported from connection_router.py, shared with
# triage_all_mode_question there) detects a response that's just a leading
# label with no real paragraphs after it, so it can be retried exactly
# like a genuinely empty response, instead of silently showing the user a
# bare heading with nothing usable underneath it - see its own docstring
# for why this is POSITION-based rather than matching a specific word: the
# label is translated into the user's own question's language, so it can
# no longer be matched against a fixed English string like "Result
# Summary"/"Results Summary". "All databases" mode's own Phase C
# summarization (_SUMMARY_SYSTEM_INSTRUCTION/summarize_all_mode_results
# below) no longer uses this function at all - it moved from the free-text
# label+blank-line convention to a structured JSON response, and
# _clean_summary_response (below) validates that shape directly instead.
# Single-connection mode's own equivalent (_SINGLE_SUMMARY_SYSTEM_
# INSTRUCTION/summarize_single_connection_results, later in this file) no
# longer asks the model for a leading label line at all - the UI already
# shows this content under its own "Summary" tab, so the prompt now tells
# the model to start straight in on the substantive answer. This function
# is still called from that path, purely as a defensive backstop: if a
# response ever comes back shaped like a bare heading with nothing real
# after it (the model ignoring the no-label instruction), this still
# catches it and forces a retry rather than showing the user an
# empty-looking tab - via _summarize_with_retry's own default
# content_parser, see below.


def _default_content_parser(text):
    """Default `content_parser` for _summarize_with_retry (below) - single-
    connection mode's prose contract: a non-empty, non-label-only stripped
    string, or None. The label-only check is now purely a defensive
    backstop - the prompt itself no longer asks for a leading label line
    at all (see _SINGLE_SUMMARY_SYSTEM_INSTRUCTION) - guarding only
    against a model that ignores that and returns a bare heading with
    nothing real after it. is_label_only_response runs on the RAW `text`
    (before stripping), same reasoning as everywhere else it's used - see
    its own docstring."""
    stripped = (text or "").strip()
    if stripped and not is_label_only_response(text or ""):
        return stripped
    return None


def _default_language_text_extractor(parsed):
    """Default `language_text_extractor` for _summarize_with_retry (below):
    `parsed` (from _default_content_parser above) already IS the text to
    run _detect_language over - single-connection mode's own prose
    contract has only ever had one string to check."""
    return parsed


def _summarize_with_retry(prompt_content, schema_block, system_instruction, provider, client, model,
                           api_key=None, tried_keys=None, using_byok=False, log_label="Summarization",
                           expected_language_code=None, content_parser=None, language_text_extractor=None,
                           invalid_content_error=None):
    """Shared bounded-retry machinery behind BOTH summarize_all_mode_results
    ("all databases" mode's Phase C, below) and summarize_single_
    connection_results (single-connection mode's own equivalent, added
    later in this file) - extracted so this retry/key-rotation policy is
    written and tested in exactly ONE place rather than duplicated
    verbatim across two callers that build different prompts/system
    instructions but need identical failure handling.

    `content_parser` and `language_text_extractor` are what let this one
    retry loop serve two callers whose notion of "valid content" is no
    longer the same shape: single-connection mode's own caller
    (summarize_single_connection_results) still needs a free-text contract
    (a non-empty, non-label-only-shaped stripped string - see
    is_label_only_response, kept as a defensive backstop even though the
    prompt no longer asks for a label line itself), while "all databases"
    mode's own Phase C (summarize_all_mode_results, below) now needs a
    structured per-database JSON object instead (see
    _SUMMARY_SYSTEM_INSTRUCTION/_clean_summary_response). Rather than
    duplicate this whole ~140-line retry/rotation/language-check loop a
    third time for the JSON shape, both `content_parser` (raw model text ->
    parsed content, or None if invalid - replaces the old inline
    stripped-and-not-label-only check) and `language_text_extractor`
    (parsed content -> the text _detect_language should actually check,
    since the JSON shape has several separate strings, not one) default to
    closures reproducing EXACTLY the original prose behavior
    (_default_content_parser/_default_language_text_extractor, just below)
    when omitted, so summarize_single_connection_results' existing call
    needs no changes at all. `invalid_content_error` is the log_label-
    adjacent last_error text to use when `content_parser` rejects a
    response; left None, the original empty-vs-label-only-specific message
    is used (again, exactly reproducing prior behavior for the default
    caller) - a JSON-mode caller instead supplies one description covering
    every way its own parser can reject a response.

    Bounded 2-attempt retry at getting usable CONTENT back (a response
    `content_parser` rejects - see above - counts as a failed attempt,
    same as connection_router.py's triage_all_mode_question treats an
    unparseable response). Nested
    inside each of those 2 attempts is the SAME transient-error/key-
    rotation retry loop generate_sql_for_connection/triage_all_mode_
    question already run (provider.classify_error()/
    MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS/
    provider.get_key_pool_size()) - this call used to be the one LLM call
    in the whole "all databases" pipeline that did NOT get that treatment:
    a real capacity/rate-limit error on the configured key would exhaust
    a bare 2-attempt loop with no rotation and no wait, in well under a
    second, silently leaving the Summary tab as if Phase C had simply
    never run - easy to mistake for a rendering bug (which is exactly what
    this looked like from the client side) rather than the resource-
    exhaustion condition it actually was. `api_key`/`tried_keys` mirror
    those two functions' own parameters of the same name for the same
    reason: the caller's own already-picked key is the natural starting
    point, and an explicit (not closed-over) `tried_keys` set is safe to
    thread through a fresh call each time this fires. `log_label` only
    changes what appears in the logger.warning() calls below, so log
    lines stay distinguishable between callers.

    GENERATOR, same idiom as generate_sql_for_connection/triage_all_mode_
    question: every time the retry loop below actually rotates a key or
    waits out a transient error, this yields a fully wire-encoded NDJSON
    progress line (`json.dumps({"status": "retrying", ...}) + "\\n"`).
    This is newer than the retry/rotation POLICY itself (see the previous
    paragraph) - the policy was added first, purely server-side, with
    nothing surfaced to the client; a slow/rate-limited summarization call
    could silently sit in a multi-second TRANSLATION_RETRY_DELAY_SECONDS
    wait with the "Summarizing…" banner frozen, indistinguishable
    from a hang. Both direct callers (summarize_all_mode_results,
    summarize_single_connection_results) forward these via `return (yield
    from _summarize_with_retry(...))`, and both routes that call THEM
    (/api/summarize-results, /api/summarize-result) forward them again the
    same way, all the way out to the client's existing generic 'retrying'
    handling - no new client-side event kind, just a new source for the
    same one. A caller with nowhere live to forward into (e.g. a unit test
    calling this directly) drains it with the same _drain_generation
    helper generate_sql_for_connection's own direct callers already use.

    On total failure (LLM call retry/rotation budget exhausted, or 2
    consecutive content-invalid responses) returns (None, None, error) -
    `error` is the raw exception the LLM call finally failed with when
    that's what happened (guaranteed to be an actual exception instance in
    that case - the same reasoning as triage_all_mode_question's own
    "error" key: the only way to reach `text is None` below is via the
    except block that just set `last_error = e`, and nothing after that
    ever reassigns it to something else before this returns), or a plain
    descriptive string when it was instead 2 consecutive content-invalid
    responses (nothing genuinely went wrong at the API level, so there's
    no exception to report - just isinstance-check `error` to tell the two
    apart). The caller uses format_llm_error_for_user() to build an honest
    message from `error` when it's a real exception, surfacing WHY
    summarization didn't produce anything rather than just leaving the
    Summary tab silently as it already was - it's the caller, not this
    function, that needs `using_byok` for that (see
    generate_sql_for_connection's docstring for the general reasoning);
    `using_byok` is accepted here only to force the key-rotation budget
    down to 1 attempt, same as there.

    Takes `prompt_content` (the plain new-user-turn text
    _build_summary_prompt/_build_single_summary_prompt built) and
    `schema_block` rather than an already-built `llm_input`, unlike
    before language verification was added: on a language-mismatch retry
    (see below) this function needs to rebuild `llm_input` itself from a
    CORRECTED prompt_content via provider.build_llm_input(), which it
    can't do starting from an opaque, already-provider-specific-shaped
    llm_input value.

    `expected_language_code` - _detect_language(user_question)'s result,
    threaded through from the caller - adds a second content-validity
    check alongside `content_parser`'s own: once content is accepted,
    `language_text_extractor(parsed)` is run through _detect_language, and
    if that doesn't match, the response is discarded exactly like invalid
    content is (consuming one of the 2 attempts), and - unlike the
    invalid-content case, which just retries with the exact same prompt -
    the next attempt's prompt gets an extra, blunt correction line naming
    the required language, since simply asking again with no change would
    likely just reproduce the same wrong-language answer. None (detection
    unavailable or too low-confidence) skips this check entirely - see
    _detect_language's own docstring.

    Returns (parsed, usage, None) on success - `parsed` is exactly what
    `content_parser` returned (a plain stripped string for the default
    prose caller, or _clean_summary_response's dict for the JSON caller),
    NOT YET wrapped in whatever presentation the caller adds on top (e.g.
    the app's "*** NO SQL ***" convention, or the "**Name:**" per-database
    tagging reconstructed by /api/summarize-results below) - that stays
    the caller's job, exactly as it always has, so it lives in exactly one
    place per caller."""
    content_parser = content_parser or _default_content_parser
    language_text_extractor = language_text_extractor or _default_language_text_extractor
    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    last_error = None
    for attempt in range(2):
        text = None
        transient_attempt = 1
        llm_input = provider.build_llm_input([], schema_block, prompt_content)
        while True:
            try:
                text, usage = provider.call(client, model, llm_input, system_instruction)
                break
            except Exception as e:
                last_error = e
                retry_action = provider.classify_error(e)
                if retry_action is None:
                    text = None
                    break

                if retry_action["rotate_key"]:
                    if len(tried_keys) >= key_pool_size:
                        text = None
                        break
                    next_key = provider.pick_api_key(exclude=tried_keys)
                    if next_key != api_key:
                        api_key = next_key
                        client = provider.make_client(api_key)
                    tried_keys.add(api_key)
                    logger.warning(
                        "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        log_label, len(tried_keys), key_pool_size, e,
                    )
                    # Told to the client before continuing - see this
                    # function's own module-level neighbor
                    # generate_sql_for_connection's identical line, and this
                    # function's docstring below on why this loop now yields
                    # at all (it didn't used to: this call had no live
                    # progress reporting even after retry/key-rotation was
                    # added to its policy).
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": len(tried_keys),
                        "maxAttempts": key_pool_size,
                        "delaySeconds": 0,
                        "rotatedKey": True,
                    }) + "\n"
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    text = None
                    break
                logger.warning(
                    "%s call failed (attempt %d/%d), retrying in %ds: %s",
                    log_label, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e,
                )
                # Told to the client before sleeping, not after - same
                # reasoning as generate_sql_for_connection's identical line.
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

        if text is None:
            # The LLM call's own retry/key-rotation budget is exhausted, or
            # it hit a non-retryable error outright - no point spending the
            # second content-validity attempt on a call that's already
            # just proven it can't succeed right now with any configured
            # key (same reasoning as triage_all_mode_question's identical
            # early break).
            break

        # content_parser runs on the RAW `text` - the default parser needs
        # it un-stripped for the same reason is_label_only_response always
        # has (see its own docstring: a "label line, then a blank line,
        # then nothing" response needs that blank line intact to be told
        # apart from a plain single-line response with no label convention
        # at all); a JSON-mode parser like _clean_summary_response just
        # ignores incidental leading/trailing whitespace itself via its own
        # json.loads.
        parsed = content_parser(text)
        if parsed is not None:
            if expected_language_code is not None:
                language_text = language_text_extractor(parsed)
                actual_language_code = translate_routes._detect_language(language_text) if language_text else None
                if actual_language_code is not None and actual_language_code != expected_language_code:
                    expected_name = translate_routes._describe_language(expected_language_code)
                    actual_name = translate_routes._describe_language(actual_language_code)
                    logger.warning(
                        "%s came back in %s instead of the question's own %s (attempt %d/2) - discarding%s",
                        log_label, actual_name, expected_name, attempt + 1,
                        ", retrying with an explicit correction" if attempt + 1 < 2 else " (no attempts left)",
                    )
                    last_error = f"response was written in {actual_name} instead of {expected_name}"
                    # Simply retrying with the SAME prompt would likely just
                    # reproduce the same wrong-language answer, since
                    # whatever pulled the model toward actual_name (usually
                    # foreign-language data in the results) is still there -
                    # so the next attempt's prompt gets an explicit,
                    # unambiguous correction addendum on top of the
                    # reminder _build_summary_prompt/_build_single_summary_
                    # prompt already appended, naming both the mistake and
                    # the fix directly rather than repeating the same
                    # instruction that didn't work the first time.
                    prompt_content = (
                        f"{prompt_content}\n\nCORRECTION: your previous answer to this exact request was "
                        f"written in {actual_name}, which is WRONG - the question was in {expected_name}, so "
                        f"your response must be entirely in {expected_name}. Write your full response again, "
                        f"from scratch, entirely in {expected_name} this time."
                    )
                    continue
            return parsed, usage, None
        if invalid_content_error is not None:
            last_error = invalid_content_error
        else:
            stripped = (text or "").strip()
            last_error = (
                "response was only the label, or missing the label/blank-line shape, with no real content after it"
                if stripped else "empty summarization response"
            )

    logger.warning("%s failed after retry: %s", log_label, last_error)
    return None, None, last_error


def _build_all_mode_schema_block(database_results, user_identity):
    """Resolves and renders each unique in-scope database referenced in
    `database_results` (one entry per statement result/note/failure - see
    _build_summary_prompt's own docstring for the exact shape) into a
    single schema_block for Phase C's LLM call. Gap 4 of "Turn History
    Handling in Datalect": previously Phase C always ran with
    schema_block="" - literally no schema at all - even though the
    design's own LLM-3 input spec calls for "detailed database schema for
    all in-scope databases".

    Each unique (kind, id) pair is resolved via resolve_descriptor_by_
    reference - the same {kind, id}-only trust boundary translate_routes.py/
    execute_routes.py already use everywhere else (never raw descriptors/
    credentials sent by the client) - then its schema is fetched via
    get_summary_schema_text() above, the SAME cached get_database_schema()
    Phase B/single-connection mode already use, but always reduced to the
    "tables_only" derivative regardless of SCHEMA_TABLES_ONLY - Phase C
    only ever needs to know what each table/column means, never
    constraints/indexes/views/etc, and every database referenced here
    already got its full schema fetched (and cached) moments earlier in
    this same turn by Phase B, so this costs nothing beyond a cache
    lookup either way. A reference that no longer resolves (a preset
    removed, or a custom connection deleted, in the moments since
    triage/Phase B ran) is silently skipped, same leniency
    resolve_in_scope_descriptors already applies elsewhere - one missing
    schema shouldn't block summarizing the other databases that did
    resolve.

    `user_identity` falsy (a caller with no real session to resolve
    against - e.g. a unit test exercising summarize_all_mode_results()
    directly) returns "" - the exact schema-less prompt this call always
    sent before Gap 4, rather than raising."""
    if not user_identity:
        return ""
    names_by_key = {}
    order = []
    for entry in (database_results or []):
        kind = entry.get("kind")
        ref_id = entry.get("id")
        if not kind or ref_id is None:
            continue
        key = (kind, ref_id)
        if key not in names_by_key:
            names_by_key[key] = entry.get("name") or "Unknown database"
            order.append(key)

    blocks = []
    for key in order:
        kind, ref_id = key
        descriptor, _resolved_name = resolve_descriptor_by_reference(kind, ref_id, user_identity)
        if descriptor is None:
            continue
        schema = get_summary_schema_text(descriptor, user_identity)
        blocks.append(f"{names_by_key[key]}:\n{schema}")
    if not blocks:
        return ""
    return "Database schema for each database queried:\n\n" + "\n\n".join(blocks) + "\n\n"


def _clean_summary_response(raw_text, num_databases):
    """Parses Phase C's structured JSON response (see
    _SUMMARY_SYSTEM_INSTRUCTION) into
      {"label": <non-empty str>, "per_database": {int_index: <non-empty str>, ...},
       "cross_database": <non-empty str> | None}
    or None (unparseable, or missing/incomplete required content) - the
    caller's bounded retry (_summarize_with_retry, via the content_parser
    it's given) treats None exactly like an empty/invalid response used to
    be treated before Phase C moved off free-text prose.

    Mirrors connection_router.py's _parse_triage_response/_clean_database_
    prompts shape closely (JSON via strip_markdown_fence + json.loads, a
    dict keyed by string-int index) but is intentionally STRICTER than
    that sibling: _clean_database_prompts tolerates a missing per-
    connection rewrite for triage (Phase B just falls back to the user's
    own original question for that one connection), but there is no
    equivalent fallback text for a missing per-database summary paragraph
    here - nothing sensible to show the user in its place - so unlike
    triage, a gap for ANY index in range(num_databases) invalidates the
    WHOLE response, giving the bounded retry loop another attempt instead
    of silently showing a summary with one database's paragraph missing.

    `num_databases` is the caller's own len(database_results) - see
    _build_summary_prompt's docstring for why its "[i]" indices already
    match this same 0-based numbering.

    "cross_database" is optional (see _SUMMARY_SYSTEM_INSTRUCTION - only
    meant to be written when the question genuinely asks for something
    spanning multiple databases): a missing, non-string, or blank value
    simply becomes None, never a reason to invalidate the rest of a
    response that otherwise checks out."""
    if not raw_text:
        return None
    cleaned = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    label = parsed.get("label")
    if not (isinstance(label, str) and label.strip()):
        return None
    label = label.strip()

    raw_per_database = parsed.get("per_database")
    if not isinstance(raw_per_database, dict):
        return None
    per_database = {}
    for key, value in raw_per_database.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str) and value.strip():
            per_database[index] = value.strip()
    for index in range(num_databases):
        if index not in per_database:
            return None

    cross_database = parsed.get("cross_database")
    cross_database = cross_database.strip() if isinstance(cross_database, str) and cross_database.strip() else None

    return {"label": label, "per_database": per_database, "cross_database": cross_database}


def _make_summary_content_parser(num_databases):
    """Binds `num_databases` into a content_parser closure for
    _summarize_with_retry - see _clean_summary_response above for the
    actual validation. A small wrapper rather than a lambda so it's
    consistent with, and greppable alongside, _default_content_parser."""
    def _parser(text):
        return _clean_summary_response(text, num_databases)
    return _parser


def _summary_language_text(parsed):
    """language_text_extractor for Phase C's JSON-mode content_parser (see
    _make_summary_content_parser/_clean_summary_response above): since
    `parsed` is now a dict of several separate strings rather than one, this
    concatenates every actual paragraph the model wrote - the label, each
    per-database paragraph, and the cross-database paragraph if present -
    into one blob for _detect_language to run over. Mirrors single-
    connection mode's own _default_language_text_extractor, which simply
    returns its one parsed prose string directly - adapted here to a
    parsed shape with more than one."""
    parts = [parsed.get("label") or ""]
    parts.extend((parsed.get("per_database") or {}).values())
    cross_database = parsed.get("cross_database")
    if cross_database:
        parts.append(cross_database)
    return "\n\n".join(part for part in parts if part)


def summarize_all_mode_results(user_question, database_results, provider, client, model, user_identity=None,
                                api_key=None, tried_keys=None, using_byok=False):
    """"All databases" mode's Phase C - see the section comment above for
    the fuller picture of when/why this runs. A brief, structured answer
    to `user_question` - one short paragraph per database, over the
    ACTUAL data gathered from every database Phase B was routed to,
    rather than the routing message triage produced before any of it was
    known, plus an optional separate cross-database paragraph (see
    _SUMMARY_SYSTEM_INSTRUCTION for the exact JSON shape asked for).

    All the retry/key-rotation policy (bounded 2-attempt content-validity
    retry, nested transient-error/key-rotation retry) now lives in the
    shared _summarize_with_retry() above - see its docstring for the full
    reasoning. This function's own job is just building the Phase-C-
    specific prompt/schema_block/system instruction and JSON parser/
    language-extractor, and delegating to it.

    `user_identity` (new - Gap 4) is what lets this build a real
    schema_block via _build_all_mode_schema_block above instead of the
    permanently-empty "" every call used to pass; defaults to None (and
    therefore an empty schema_block, unchanged from before Gap 4) for a
    caller with no real session to resolve connections against.

    GENERATOR (see _summarize_with_retry's own docstring): `yield from`s
    that function directly, so its live 'retrying' progress lines pass
    straight through unchanged - this function adds none of its own, it
    just builds the Phase-C-specific prompt/schema_block ahead of
    delegating.

    Returns (parsed, usage, error) on success/failure - `parsed`, when not
    None, is exactly _clean_summary_response's own returned shape
    ({"label", "per_database", "cross_database"} - see its docstring),
    never the model's raw JSON text. See _summarize_with_retry's docstring
    for the exact meaning of `error` on failure."""
    expected_language_code = translate_routes._detect_language(user_question)
    prompt_content = _build_summary_prompt(user_question, database_results, expected_language_code)
    schema_block = _build_all_mode_schema_block(database_results, user_identity)
    num_databases = len(database_results or [])
    return (yield from _summarize_with_retry(
        prompt_content, schema_block, _SUMMARY_SYSTEM_INSTRUCTION, provider, client, model,
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
        log_label="Phase C summarization", expected_language_code=expected_language_code,
        content_parser=_make_summary_content_parser(num_databases),
        language_text_extractor=_summary_language_text,
        invalid_content_error="response was not valid, complete per-database summary JSON",
    ))


@summarize_bp.route('/api/summarize-results', methods=['POST'])
# See concurrency_guard.py's own module docstring (TRANSLATE_GUARD) and
# rate_limiter.py's own docstring (RATE_LIMIT_TRANSLATE/
# _translate_family_rate_limit) for why this route draws from the SAME
# pooled guard/rate limit as /api/translate and summarize_result() below,
# rather than a separate one of its own: an LLM call to generate SQL and
# an LLM call to summarize results are the same kind of work as far as
# this app's admission control is concerned.
@summarize_rate_limit
def summarize_results():
    """See the "All databases" mode, Phase C section comment above for the
    full picture. Called by the client exactly once per "route" outcome
    turn, only after /api/execute has actually run every database Phase B
    selected (never for a single-connection session - client.js only ever
    calls this from executeSql()'s router_route handling).

    Streams newline-delimited JSON (NDJSON), same contract as /api/
    translate (see that route's module docstring) and for the same
    reason: summarize_all_mode_results()'s own retry loop
    (_summarize_with_retry, see its docstring) can now genuinely take
    several real seconds - a transient-error wait, possibly a key
    rotation first - and previously nothing reached the client during
    that wait at all; the "Summarizing…" banner just sat there
    frozen, indistinguishable from a hang. Zero or more
    {"status": "retrying", "attempt": <next attempt #>, "maxAttempts": N,
     "delaySeconds": <float>, "rotatedKey": <bool>} lines are emitted live
    as that retry loop runs - client.js needs no changes to show these,
    since 'retrying' is already handled generically by its existing
    dispatcher, regardless of which server-side call produced the line -
    followed by exactly one terminal line:
      {"status": "done", "success": true, "summary": "...",
       "database_summaries": [{"kind", "id", "name", "text"}, ...],
       "cross_database_summary": "..." | null}
      or, on failure (retry/rotation budget exhausted, or 2 consecutive
      content-invalid responses):
      {"status": "done", "success": false, "error": "..."}
    "summary" stays the single joined, "**Name:** paragraph" string this
    route has always returned - untouched consumers (history persistence,
    the Summary tab's existing rendering) keep working unmodified - but it
    is now RECONSTRUCTED here from summarize_all_mode_results' own
    structured `parsed` return value rather than trusted verbatim from the
    model: Phase C's own response is JSON now (see _SUMMARY_SYSTEM_
    INSTRUCTION/_clean_summary_response), so the bold "**Name:**" lead-in
    for each paragraph is built from `database_results`' own real name,
    zipped back against `parsed["per_database"]` by the same 0-based index
    _build_summary_prompt's "[i]" labels used - a reliability improvement
    over the old free-text convention, which trusted the model to copy a
    database's name into its own prose verbatim. Grouped by (kind, id)
    before that heading is built, since `database_results` (and so
    `per_database`) has one entry per STATEMENT RESULT, not per database -
    a database whose SQL had multiple statements gets multiple indices,
    all sharing the same identity - so "database_summaries" really is one
    entry PER DATABASE (as its own shape below already promised), each
    "text" combining every one of that database's own resultset
    paragraphs (newline-joined, so they render as sub-paragraphs nested
    under one heading rather than that heading repeating once per
    resultset), not one entry per resultset. "database_summaries" and
    "cross_database_summary" are purely ADDITIVE new fields alongside that
    unchanged "summary" string (Chunk 1's own sql_blocks precedent) - the
    per-database split callers need to record separate per-database turns
    later, and the cross-database paragraph split out on its own, distinct
    from any one database's paragraph.
    The two early-validation returns below (missing API key, missing
    prompt/database_results) happen before any of this and keep their
    real plain-JSON 400 responses, exactly as /api/translate's own two
    early-validation cases do - nothing has streamed yet at that point,
    so there's no reason to pay the NDJSON/chunked-response cost for a
    request that never even reaches the retry loop."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    session_data = state_store.get_session(user_identity)
    provider = get_llm_provider(session_data.get('llm_provider'))
    llm_model = (
        data.get(provider.request_model_key) or data.get('model')
        or session_data.get('llm_model') or provider.default_model
    )
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        resp = jsonify({'success': False, 'error': provider.missing_key_error})
        return apply_session_cookie(resp, session_id), 400

    prompt = (data.get('prompt') or '').strip()
    database_results = data.get('database_results')
    if not prompt or not isinstance(database_results, list) or not database_results:
        resp = jsonify({'success': False, 'error': 'prompt and database_results are required'})
        return apply_session_cookie(resp, session_id), 400

    def stream_summarize_results():
        start_time = time.perf_counter()
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            parsed, usage, error = yield from summarize_all_mode_results(
                prompt, database_results, provider, client, llm_model, user_identity=user_identity,
                api_key=api_key, using_byok=bool(byok_key),
            )
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()
        duration = round(1000 * (time.perf_counter() - start_time))

        if parsed is None:
            # `error` is the raw exception when the LLM call itself is what
            # failed (see summarize_all_mode_results' docstring) - format that
            # honestly, same as every other LLM-call failure in this app now
            # does. The other case (2 consecutive content-invalid responses,
            # nothing wrong at the API level) has no exception to report, so
            # it keeps the original generic message instead.
            error_message = (
                format_llm_error_for_user(provider, llm_model, error, using_byok=bool(byok_key))
                if isinstance(error, BaseException) else
                'Unable to summarize results right now.'
            )
            # Phase C (summarization) is deliberately NEVER recorded in the
            # translations-table history/stats - only calls that take a
            # prompt and generate real SQL are (single-connection Call 2,
            # dataset-group mode's own Phase B per-connection generation) -
            # triage and summarization calls aren't useful there and would
            # just pollute the stats. See the matching comment on Phase A's
            # own (removed) logging call further down for the fuller
            # reasoning; this used to log a "Dataset Group"/"Dataset Group"
            # row here on a total Phase C failure.
            yield json.dumps({'status': 'done', 'success': False, 'error': error_message}) + "\n"
            return

        # Zip parsed["per_database"] (keyed by the same 0-based index
        # _build_summary_prompt's own "[i]" results-block labels used)
        # back against `database_results` by position, so each paragraph
        # is reunited with its database's real kind/id/name - see this
        # route's own docstring above for why this reconstruction (rather
        # than trusting the model's own name copy) is now a reliability
        # improvement, not just a format change.
        #
        # `database_results` has one entry per STATEMENT RESULT/note/
        # failure (see _build_summary_prompt's own docstring), not one per
        # database - a database whose own SQL had multiple statements
        # contributes multiple entries here, all sharing the same (kind,
        # id, name), and the model likewise wrote one paragraph per index
        # (still asked to reason about just "that index's" own results,
        # not to merge across indices itself - the wording of each
        # paragraph is unchanged by this). Grouped here, by (kind, id), so
        # the rendered summary shows ONE heading per actual database, with
        # each of its own resultsets' paragraphs nested underneath as
        # sub-paragraphs (joined by a single '\n' - a soft line break the
        # Summary tab's `white-space: pre-wrap` renders without a blank
        # line, distinct from the blank-line-separated '\n\n' between
        # different databases below) - instead of the same database's name
        # repeating as a separate, flat top-level paragraph once per
        # resultset.
        per_database = parsed["per_database"]
        grouped_by_database = {}
        database_order = []
        for i, entry in enumerate(database_results):
            key = (entry.get("kind"), entry.get("id"))
            if key not in grouped_by_database:
                grouped_by_database[key] = {
                    "kind": entry.get("kind"), "id": entry.get("id"),
                    "name": entry.get("name") or "Unknown database",
                    "paragraphs": [],
                }
                database_order.append(key)
            grouped_by_database[key]["paragraphs"].append(per_database.get(i, ""))

        database_summaries = []
        summary_paragraphs = []
        for key in database_order:
            group = grouped_by_database[key]
            # One combined block of text per database - a single resultset
            # (the common case) looks byte-identical to before this
            # change; 2+ resultsets get their own paragraphs stacked on
            # separate lines under the one heading instead of repeating it.
            combined_text = "\n".join(p for p in group["paragraphs"] if p)
            database_summaries.append({
                "kind": group["kind"], "id": group["id"], "name": group["name"], "text": combined_text,
            })
            summary_paragraphs.append(f"**{group['name']}:** {combined_text}")

        cross_database_summary = parsed.get("cross_database")
        if cross_database_summary:
            summary_paragraphs.append(cross_database_summary)

        summary_text = "*** NO SQL *** " + parsed["label"] + "\n\n" + "\n\n".join(summary_paragraphs)
        # Phase C (summarization) is deliberately never recorded in the
        # translations-table history/stats - see the comment on this
        # function's own failure branch above for why. `usage`/`duration`
        # (computed above) are no longer used for anything now that this
        # call logs nothing and the client response below never carried
        # usage/duration fields either.

        yield json.dumps({
            'status': 'done', 'success': True, 'summary': summary_text,
            'database_summaries': database_summaries,
            'cross_database_summary': cross_database_summary,
        }) + "\n"

    # See concurrency_guard.py's own module docstring - TRANSLATE_GUARD,
    # the SAME guard /api/translate itself uses (pooled, not a dedicated
    # one for this route) - acquired HERE, right before actually
    # streaming, same placement/reasoning as translate_query()'s own
    # TRANSLATE_GUARD.try_acquire() below: nothing above this point does
    # any real work, so nothing before it should compete for a scarce
    # slot. {"success": False, "error": ...} matches this route's own
    # early-validation failures above.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many results-summarization requests right now. Please try again in a few seconds.',
        })

    def _stream_summarize_results_with_guard_release():
        # Holds the guard slot open for stream_summarize_results()'s ENTIRE
        # lifetime - see translate_query()'s own
        # _translation_stream_with_guard_release for the identical
        # reasoning (the generator, not this view function, is what does
        # the real work, driven lazily by Flask/gunicorn as the response
        # streams out).
        try:
            yield from stream_summarize_results()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_summarize_results_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)


# --- Single-connection mode's own post-execution results summarization --
#
# The single-connection equivalent of "all databases" mode's Phase C above
# - see that section's own comment for the general shape/reasoning this
# mirrors. Once a single-connection turn's generated SQL has actually been
# executed (client.js's executeSql()), a SEPARATE LLM call is made with the
# original question, the schema, the SQL that ran, and up to
# SUMMARY_RESULTS_MAX_ROWS rows of what it returned (explicit product
# decision: this call exists to reason over the current turn's own result
# set, exactly like Phase C now does too - see _build_summary_prompt's own
# docstring for why that's a separate, far more generous cap than
# HISTORY_RESULT_MAX_ROWS, and SUMMARY_RESULTS_MAX_ROWS's own definition
# comment above for why a real ceiling is needed at all now that every row
# is actually sent to this call) - and asked to both answer the question and
# call out actionable insight, not just
# restate the data. Skipped entirely by the client when the LLM instead
# answered directly via the "*** NO SQL ***" convention (nothing was
# executed, so nothing to summarize). Same best-effort posture as Phase C:
# any failure here is reported back as {"success": false}, never a hard
# error, so the client just leaves the Summary tab out rather than
# treating a nice-to-have's failure as a turn failure.
#
# This call also decides, "ride-along" with the summary (one LLM call,
# not a second round trip), whether the results are worth showing as a
# chart instead of only a table - see _SINGLE_SUMMARY_SYSTEM_INSTRUCTION's
# own "visualization" paragraph below and _clean_single_summary_response.
# The model's own judgment about WHETHER charting is even possible is
# never trusted on its own: _pick_chartable_result decides that server-
# side, from the real executed statement_results, before the model is
# ever asked anything - a multi-statement result, a single-row result, or
# a result with no numeric column at all is never offered a chart no
# matter what the model might otherwise claim, and its own x_column/
# y_columns/series_column choices are re-validated against the real
# columns (and, for y_columns, the real row VALUES - see
# _column_looks_numeric) rather than trusted on faith, the same "never
# blindly trust LLM output" posture this app already applies to generated
# SQL (see translate_query()'s own docstring).


_SINGLE_SUMMARY_SYSTEM_INSTRUCTION = load_prompt("summary_single_connection.txt")


def _build_single_summary_prompt(user_question, sql, statement_results, chartable_entry, expected_language_code=None):
    """Renders `statement_results` - client-submitted [{"columns", "rows",
    "rowCount"} | {"note"} | {"error"}, ...], one entry per SQL statement
    /api/execute actually ran for this turn - into one labeled text block
    per statement for the single-connection summarization call above.

    Real result rows are capped at SUMMARY_RESULTS_MAX_ROWS, the same
    abuse/cost-protection cap _build_summary_prompt's own docstring
    explains above (a SEPARATE, far more generous constant than
    HISTORY_RESULT_MAX_ROWS - see SUMMARY_RESULTS_MAX_ROWS's own
    definition comment). When a statement's results are actually
    truncated, the header names both the real total row count and how
    many are shown, so the model isn't misled into thinking it saw
    everything.

    `chartable_entry` - _pick_chartable_result(statement_results)'s own
    return value, computed once by the caller and threaded through here
    (rather than recomputed) so the "Chartable columns" section below
    always describes the exact same entry _clean_single_summary_response
    will later validate the model's "visualization" choice against - see
    _describe_chartable_columns for the rendering itself.

    `expected_language_code` - see _build_summary_prompt's own docstring
    for what this is and why it's threaded through from the caller rather
    than detected here."""
    blocks = []
    for i, entry in enumerate(statement_results or []):
        error = entry.get("error")
        note = entry.get("note")
        if error:
            blocks.append(f"Query Result {i + 1}: query failed - {error}")
        elif note:
            blocks.append(f"Query Result {i + 1}: {note}")
        else:
            cols = entry.get("columns") or []
            rows = entry.get("rows") or []
            row_count = entry.get("rowCount", len(rows))
            shown_rows = min(len(rows), SUMMARY_RESULTS_MAX_ROWS)
            header = (
                f"Query Result {i + 1} - {row_count} row(s):"
                if shown_rows >= row_count
                else f"Query Result {i + 1} - {row_count} row(s) total, showing the first {shown_rows}:"
            )
            blocks.append(header + "\n" + format_results_table_text(cols, rows, max_rows=SUMMARY_RESULTS_MAX_ROWS))
    results_text = "\n\n".join(blocks) if blocks else "(no rows returned)"
    # Same reinforcement-at-the-end rationale as _build_summary_prompt's own
    # trailing reminder above - see that function's comment, and see its
    # named_language_sentence for why a confidently-detected language gets
    # named concretely here too.
    language_name = translate_routes._describe_language(expected_language_code)
    named_language_sentence = (
        f" Concretely: the question above is in {language_name}, so your ENTIRE response - the "
        f"label line and every paragraph - must be written in {language_name}, in full, not "
        f"partially or with any other language mixed in."
        if language_name else ""
    )
    return (
        f"Original question: {user_question}\n\n"
        f"SQL executed:\n{sql}\n\n"
        f"Results:\n\n{results_text}\n\n"
        f"{_describe_chartable_columns(chartable_entry)}\n"
        "Reminder: write your response - the label line AND every paragraph of \"summary\" - in "
        "the SAME LANGUAGE as the \"Original question\" above, no matter what language the schema, "
        "SQL, or results data shown above happen to be in." + named_language_sentence
    )
def _clean_single_summary_response(raw_text, chartable_entry):
    """Parses the single-connection summarization call's structured JSON
    response (see _SINGLE_SUMMARY_SYSTEM_INSTRUCTION) into
      {"summary": <non-empty str>, "visualization": <_clean_visualization's
       shape> | None}
    or None (unparseable, or "summary" itself is missing/invalid - the
    caller's bounded retry, via _summarize_with_retry's content_parser,
    treats None exactly like an empty/invalid response always was before
    this call moved off free-text prose). Mirrors _clean_summary_response's
    JSON-via-strip_markdown_fence-then-json.loads shape closely - see that
    function's own docstring - adapted to this call's own "summary" +
    "visualization" envelope instead of Phase C's per-database one.

    "summary" is validated exactly like the old free-text contract
    (_default_content_parser) always was: a non-empty, non-label-only
    stripped string. A malformed/missing "summary" invalidates the WHOLE
    response (returns None, giving the bounded retry another attempt) -
    same as a missing per-database paragraph does for Phase C - since
    there's nothing sensible to show in its place. "visualization", by
    contrast, is validated leniently: _clean_visualization returning None
    (a table) is always a fine, valid outcome, never a reason to retry -
    only "summary" itself failing validation is."""
    if not raw_text:
        return None
    cleaned = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    summary = parsed.get("summary")
    if not (isinstance(summary, str) and summary.strip() and not is_label_only_response(summary)):
        return None
    summary = summary.strip()

    visualization = _clean_visualization(parsed.get("visualization"), chartable_entry)
    return {"summary": summary, "visualization": visualization}


def _make_single_summary_content_parser(chartable_entry):
    """Binds `chartable_entry` into a content_parser closure for
    _summarize_with_retry - see _clean_single_summary_response above for
    the actual validation. A small wrapper rather than a lambda so it's
    consistent with, and greppable alongside, _make_summary_content_parser
    (Phase C's own equivalent binder)."""
    def _parser(text):
        return _clean_single_summary_response(text, chartable_entry)
    return _parser


def _single_summary_language_text(parsed):
    """language_text_extractor for this call's JSON-mode content_parser -
    mirrors Phase C's own _summary_language_text, adapted to this call's
    "summary" + "visualization" shape: only "summary" is ever prose worth
    running _detect_language over ("visualization" is column names/enum
    values, not natural language)."""
    return parsed.get("summary") or ""


def summarize_single_connection_results(user_question, schema, sql, statement_results, provider, client, model,
                                         api_key=None, tried_keys=None, using_byok=False):
    """Single-connection mode's equivalent of summarize_all_mode_results
    above - see this file's "Single-connection mode's own post-execution
    results summarization" section comment for the fuller picture, and
    _summarize_with_retry's docstring for the shared retry/key-rotation
    policy both this and summarize_all_mode_results now go through.

    Unlike Phase C (which has no single SQL statement to point to - it
    summarizes across possibly several databases, each with its own
    schema and its own generated SQL - see _build_all_mode_schema_block/
    _build_summary_prompt's SQL section), this call has a real schema and
    a real, already-executed SQL statement for exactly one connection,
    both of which are given to the model: the schema via build_llm_input's
    own schema_block parameter (same cache-friendly placement
    translate_query()'s single-connection path already uses - schema
    ahead of the (empty, here) history and the new prompt), and the SQL/
    results via _build_single_summary_prompt.

    Also decides, ride-along with the summary (see this file's "Single-
    connection mode's own post-execution results summarization" section
    comment), whether the results are chartable - _pick_chartable_result
    computed once here and threaded into both the prompt
    (_describe_chartable_columns, via _build_single_summary_prompt) and
    the response validation (_clean_visualization, via
    _make_single_summary_content_parser), so the two can never disagree
    about which columns were actually on offer.

    GENERATOR (see _summarize_with_retry's own docstring): `yield from`s
    that function directly, so its live 'retrying' progress lines pass
    straight through unchanged - this function adds none of its own.

    Returns (parsed, usage, error) - `parsed` is exactly
    _clean_single_summary_response's own {"summary", "visualization"}
    dict, or None on failure (see _summarize_with_retry's docstring for
    the exact meaning of `usage`/`error` in that case) - NOT yet unwrapped
    into a plain summary string; that stays the caller's job (see
    stream_summarize_result below), exactly the same "parsed, not
    presentation-wrapped" contract summarize_all_mode_results' own
    {"label", "per_database", "cross_database"} return already has."""
    expected_language_code = translate_routes._detect_language(user_question)
    chartable_entry = _pick_chartable_result(statement_results)
    prompt_content = _build_single_summary_prompt(
        user_question, sql, statement_results, chartable_entry, expected_language_code,
    )
    return (yield from _summarize_with_retry(
        prompt_content, f"Database Schema:\n{schema}\n\n", _SINGLE_SUMMARY_SYSTEM_INSTRUCTION, provider, client, model,
        content_parser=_make_single_summary_content_parser(chartable_entry),
        language_text_extractor=_single_summary_language_text,
        invalid_content_error="response was not the expected {\"summary\": ..., \"visualization\": ...} JSON shape, "
                               "or \"summary\" itself was empty/label-only",
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
        log_label="Single-connection results summarization", expected_language_code=expected_language_code,
    ))


@summarize_bp.route('/api/summarize-result', methods=['POST'])
# See rate_limiter.py's own docstring (RATE_LIMIT_TRANSLATE/
# _translate_family_rate_limit) - pooled with /api/translate and
# summarize_results() above under the SAME budget, not a separate knob of
# its own: an LLM call to generate SQL and an LLM call to summarize
# results are the same kind of work as far as this app's admission
# control is concerned.
@summarize_rate_limit
def summarize_result():
    """See this module's "Single-connection mode's own post-execution
    results summarization" section comment above for the full picture.
    Called by the client once per single-connection turn, after
    /api/execute has actually run the generated SQL - never for a "route"
    outcome turn (that already gets its own Phase C summary via
    /api/summarize-results above) and never when the LLM answered
    directly via the "*** NO SQL ***" convention (nothing was executed,
    so nothing to summarize).

    Streams NDJSON, same contract/reasoning as /api/summarize-results
    above (see that route's docstring) - summarize_single_connection_
    results shares the exact same underlying retry machinery
    (_summarize_with_retry), so it needed the exact same fix: zero or
    more live {"status": "retrying", ...} lines while that retry loop
    runs, then exactly one terminal {"status": "done", "success": ...,
    "summary"/"error": ...} line. The two early-validation returns below
    (missing API key, missing prompt/sql/results) keep their real plain-
    JSON 400 responses, same as above."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    session_data = state_store.get_session(user_identity)
    provider = get_llm_provider(session_data.get('llm_provider'))
    llm_model = (
        data.get(provider.request_model_key) or data.get('model')
        or session_data.get('llm_model') or provider.default_model
    )
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        resp = jsonify({'success': False, 'error': provider.missing_key_error})
        return apply_session_cookie(resp, session_id), 400

    prompt = (data.get('prompt') or '').strip()
    sql = (data.get('sql') or '').strip()
    statement_results = data.get('results')
    if not prompt or not sql or not isinstance(statement_results, list) or not statement_results:
        resp = jsonify({'success': False, 'error': 'prompt, sql and results are required'})
        return apply_session_cookie(resp, session_id), 400

    # See concurrency_guard.py's own module docstring - TRANSLATE_GUARD,
    # the SAME pooled guard /api/translate and /api/summarize-results also
    # use, not a dedicated one for this route - acquired HERE, before the
    # schema lookup just below, not just before streaming starts: unlike
    # /api/summarize-results above (which has no equivalent inline DB work
    # before its own guard acquire), this route's schema fetch is itself
    # real (schema-cache-backed, but occasionally cold) DB work, and needs
    # to happen while this guard is actually held, not in a gap between
    # acquiring it and the point where a wrapping generator's own finally
    # would take over. {"success": False, "error": ...} matches this
    # route's own early-validation failures above.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many results-summarization requests right now. Please try again in a few seconds.',
        })

    def stream_summarize_result():
        # Same connection this turn's own /api/translate call itself
        # resolved (no client-side override needed/sent - the session's
        # current single connection, exactly like /api/translate's
        # single-connection path). Resolved HERE, inside the generator
        # (rather than synchronously above, before TRANSLATE_GUARD was even
        # acquired) so this DB work happens entirely within the window the
        # guard is held - see the comment above this generator's
        # definition.
        conn_str = resolve_conn_str(data.get('database_url'), user_identity)
        schema = get_summary_schema_text(conn_str, user_identity)

        start_time = time.perf_counter()
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            parsed, usage, error = yield from summarize_single_connection_results(
                prompt, schema, sql, statement_results, provider, client, llm_model, api_key=api_key,
                using_byok=bool(byok_key),
            )
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()
        duration = round(1000 * (time.perf_counter() - start_time))

        if parsed is None:
            error_message = (
                format_llm_error_for_user(provider, llm_model, error, using_byok=bool(byok_key))
                if isinstance(error, BaseException) else
                'Unable to summarize results right now.'
            )
            # Call 3 (summarization) is deliberately NEVER recorded in the
            # translations-table history/stats - only calls that take a
            # prompt and generate real SQL are (Call 2 here, dataset-group
            # mode's own Phase B per-connection generation) - triage and
            # summarization calls aren't useful there and would just
            # pollute the stats.
            yield json.dumps({'status': 'done', 'success': False, 'error': error_message}) + "\n"
            return

        # `parsed` is summarize_single_connection_results' own
        # {"summary", "visualization"} dict (see its own docstring) - only
        # "summary" gets the "*** NO SQL ***" prefix/translations-table
        # logging treatment; "visualization" (already fully validated
        # against the real executed columns/rows - see _clean_
        # visualization) rides along in the response as-is, for client.js
        # to render as a chart instead of/alongside the results table when
        # it's not None.
        summary_text = "*** NO SQL *** " + parsed["summary"]
        visualization = parsed["visualization"]
        # Call 3 (summarization) is deliberately never recorded in the
        # translations-table history/stats - see the comment on this
        # function's own failure branch above for why.

        yield json.dumps({
            'status': 'done', 'success': True, 'summary': summary_text, 'visualization': visualization,
        }) + "\n"

    def _stream_summarize_result_with_guard_release():
        # Same reasoning as _stream_summarize_results_with_guard_release
        # above (and translate_query()'s own equivalent wrapper) - holds
        # the guard slot open for stream_summarize_result()'s ENTIRE
        # lifetime, schema lookup included, not just until this view
        # function returns.
        try:
            yield from stream_summarize_result()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_summarize_result_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)
