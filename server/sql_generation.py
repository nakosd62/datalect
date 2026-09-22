"""
sql_generation.py

SQL generation, extracted out of translate_routes.py (see that module's own
docstring for what's left there, and llm_providers.py's/chart_helpers.py's/
summarize_routes.py's own docstrings for the earlier pieces extracted the
same way). Nothing here is new behavior, just a change of address.

Covers everything involved in turning a natural-language prompt into SQL
for one or more connections, EXCEPT the /api/translate route itself and
stream_translation() (translate_query() in translate_routes.py) - those
stay put for now as the last, most tangled piece of this whole refactor
(see translate_routes.py's own docstring). Specifically:

  - Dialect prompts (_DIALECT_PROMPT_INTROS et al) and the two format-rules
    constants (_COMMON_FORMAT_RULES for dataset-group mode's older '*** NO
    SQL ***'-marker convention, _SQL_GENERATION_FORMAT_RULES for single-
    connection mode's newer JSON-enveloped Call 2) - both still used
    directly by translate_query() too, hence the re-export back.

  - Response cleanup: _clean_generated_sql (fence-stripping/chatter-
    tolerant SQL extraction) and _strip_no_sql_prefix, plus their
    supporting regexes.

  - Dataset-group mode's Phase B: generate_sql_for_connection (one
    connection's own SQL generation, with the full transient-error/key-
    rotation/language-verification retry policy) and _run_phase_b_fanout
    (runs it in parallel across every selected connection via a
    ThreadPoolExecutor), plus their small helpers _drain_generation/
    _classify_generation_outcome.

  - Single-dataset mode's own two-call redesign: triage_single_dataset_
    question (Call 1 - a thin wrapper around connection_router.
    run_triage_call) and _parse_sql_generation_response (Call 2's JSON
    response parser).

  - _no_sql_language_mismatch, the language-verification check for a
    '*** NO SQL ***'-prefixed free-text reply - used both by Phase B above
    and by translate_query()'s own single-connection path, hence the
    re-export back.

translate_routes.py re-imports every name below back into its own
namespace, so they remain reachable as translate_routes.<name> - both for
translate_query() (which calls several of these directly as bare names:
_run_phase_b_fanout, get_triage_schema_text/get_llm_schema_text,
triage_single_dataset_question, _parse_sql_generation_response,
_no_sql_language_mismatch, _DIALECT_PROMPT_INTROS/_DEFAULT_DIALECT_PROMPT_
INTRO, _SQL_GENERATION_FORMAT_RULES, _TRIAGE_FAILURE_TEXT) and for every
existing test's app_env.translate_routes.<name> attribute access.

`import translate_routes` (the module, not `from translate_routes import
_detect_language/_describe_language`) is deliberately deferred/indirect,
for the exact same reason summarize_routes.py's own docstring/import
comment explains: several existing tests patch translate_routes.
_detect_language/._describe_language directly (e.g. to exercise
triage_single_dataset_question's or generate_sql_for_connection's own
language-mismatch retry deterministically), and a plain independent import
here would bind its own separate name that monkeypatch would never reach.
Calling through translate_routes._detect_language/._describe_language
instead means whatever's CURRENTLY set there (real or patched) is what
actually runs - safe despite translate_routes.py importing FROM this module
further down, for the same "nothing here reads the attribute until a
request/test actually calls one of these functions, long after both
modules have finished loading" reasoning summarize_routes.py's own comment
gives in full.
"""
import concurrent.futures
import json
import re
import time

from app_config import logger, state_store, MAX_TRANSLATION_ATTEMPTS
from backends import get_backend
from backends.base import SCHEMA_TABLES_ONLY, derive_tables_only_schema_text
from connection_router import run_triage_call, _extract_json_object
from llm_providers import LlmCallFailed, format_llm_error_for_user
from prompt_loader import load_prompt
# See this module's docstring above for why this is `import translate_routes`
# (deferred/indirect) rather than a direct `from language_detect import ...`/
# `from db import get_database_schema`: besides _detect_language/
# _describe_language, a test also patches translate_routes.
# get_database_schema directly (test_get_triage_schema_text_forwards_to_
# get_database_schema_with_deep_false) expecting get_triage_schema_text
# here to see it - so every real call site below goes through
# translate_routes.get_database_schema(...) rather than a bare name too.
import translate_routes


_DIALECT_PROMPT_FILENAMES = {
    "PostgreSQL": "postgresql",
    "BigQuery Standard SQL": "bigquery_standard_sql",
    "Snowflake SQL": "snowflake_sql",
    "MySQL": "mysql",
    "Databricks SQL": "databricks_sql",
    "Oracle Database": "oracle_database",
    "Amazon Redshift SQL": "amazon_redshift_sql",
    "Microsoft SQL Server": "microsoft_sql_server",
    "Google Visualization API Query Language": "google_visualization_api_query_language",
    "MongoDB Atlas SQL": "mongodb_atlas_sql",
}
# Each dialect's own prompt intro - the dialect-specific gotchas/syntax
# rules a model needs before generating SQL for it - now lives as its own
# plain-text file under server/prompts/dialects/ (see prompt_loader.py's
# own docstring for why) rather than as a hardcoded string literal here.
# The dict's shape/keys are unchanged from before this moved to files -
# only where each value's text actually lives changed.
_DIALECT_PROMPT_INTROS = {
    dialect: load_prompt("dialects", f"{filename}.txt")
    for dialect, filename in _DIALECT_PROMPT_FILENAMES.items()
}
_DEFAULT_DIALECT_PROMPT_INTRO = _DIALECT_PROMPT_INTROS["PostgreSQL"]

# The output-format/behavior rules that follow the dialect intro in the
# system prompt - identical for every dialect, so it's pulled out once here
# rather than duplicated per dialect entry above. See
# server/prompts/sql_common_format_rules.txt for the actual wording.
_COMMON_FORMAT_RULES = load_prompt("sql_common_format_rules.txt")


_NO_SQL_PREFIX_RE = re.compile(r'^\*\*\*\s*NO\s*SQL\s*\*\*\*\s*', re.IGNORECASE)


def _strip_no_sql_prefix(text):
    """Strips the '*** NO SQL ***' sentinel (see _COMMON_FORMAT_RULES)
    from the front of `text`, tolerating the same loose whitespace/casing
    client.js's own copy of this regex already tolerates. Returns the
    stripped, trimmed remainder (possibly empty)."""
    return _NO_SQL_PREFIX_RE.sub("", text or "").strip()


# A model that ignores "Return ONLY the raw SQL code block" or "*** NO SQL
# ***" (see _COMMON_FORMAT_RULES) and instead wraps its actual answer in a
# sentence or two of chatter defeats every check below on its own, since
# they're anchored at position 0 (does this string START with ```, does it
# START with *** NO SQL ***) - exactly the failure mode a weaker local
# model (Ollama) hits far more often than a frontier one: real-world
# reports were "the SQL box shows a prose explanation with the query
# somewhere inside it" and "asking about the app itself never opens the
# Help popup" - both are this same anchored-parsing brittleness, not (only)
# a prompt-wording problem. _MAX_MARKER_PREAMBLE_CHARS bounds how much
# leading text is treated as "chatter to discard before a marker/fence"
# rather than scanning the entire response indefinitely, which risks
# matching a marker-like or fence-like sequence that coincidentally shows
# up deep inside a long, otherwise-legitimate multi-statement SQL script -
# a couple of sentences' worth is plenty for the kind of preamble an
# instruction-following slip actually produces ("Sure, here's the query
# you asked for: ...").
_MAX_MARKER_PREAMBLE_CHARS = 400
_NO_SQL_SEARCH_RE = re.compile(r'\*\*\*\s*NO\s*SQL\s*\*\*\*', re.IGNORECASE)

# The set of markdown fence "language" tags a model is actually likely to
# write on a ```-fenced SQL block (bare ``` with no tag at all is handled
# separately below, since this alternation is always optional). Kept as an
# explicit safelist - rather than the previous "any run of word characters"
# - specifically so a response with NO real tag, where the fence is glued
# directly onto the SQL with no separating whitespace/newline (e.g.
# Ollama's qwen2.5-coder:3b occasionally emitting "```SELECT * FROM
# foo;\n```" instead of "```sql\nSELECT ...\n```"), can never have its
# first SQL keyword ("SELECT", "WITH", ...) mistaken for a language tag
# and silently eaten along with the fence delimiter - see the regression
# tests for _clean_generated_sql for the exact case this fixes.
_SQL_FENCE_LANG_ALTERNATION = (
    r'sql|mysql|postgres(?:ql)?|tsql|mssql|plsql|t-sql|oracle|sqlite|'
    r'snowflake|redshift|bigquery|databricks|hive|spark(?:sql)?'
)
# A complete, paired fence: opening ``` (+ optional safelisted tag), a REAL
# newline (never just "some following whitespace"), the content, then a
# closing ```. Requiring an actual newline right after the opening
# delimiter is what makes the tag safelist above airtight: a bare
# "```SELECT ...\n```" has no recognized tag and no newline immediately
# after the delimiter either, so this simply doesn't match it at all (it
# falls through to the leading/trailing stripping below instead) rather
# than guessing where a tag might end.
_SQL_FENCE_RE = re.compile(
    r'```[ \t]*(?:' + _SQL_FENCE_LANG_ALTERNATION + r')?[ \t]*\r?\n(.*?)```',
    re.DOTALL | re.IGNORECASE,
)
# Used only when _SQL_FENCE_RE found no complete pair - independently strip
# a leading and/or a trailing fence delimiter, so an UNPAIRED fence (an
# opening ``` with no closing one, a stray closing ``` with no opening one,
# or an opening ``` glued directly onto the SQL with no tag/newline at all)
# still gets its backticks removed instead of being shipped to the client
# verbatim. The two are independent (a response can have either, both, or
# - after this whole function runs - neither) precisely so a lone stray
# ``` at either end doesn't require its non-existent counterpart to also
# be present before anything gets cleaned.
_LEADING_FENCE_RE = re.compile(
    r'^```[ \t]*(?:' + _SQL_FENCE_LANG_ALTERNATION + r')?[ \t]*\r?\n?',
    re.IGNORECASE,
)
_TRAILING_FENCE_RE = re.compile(r'\r?\n?[ \t]*```[ \t]*$')


def _clean_generated_sql(raw_text):
    """Turns whatever text a provider's call() returned into what the rest
    of this file already expects: either exactly the raw SQL (no fences),
    or a string starting exactly with the '*** NO SQL ***' marker (see
    _NO_SQL_PREFIX_RE above) - so a well-behaved response (any of Google/
    Anthropic/OpenAI, or Ollama on a good day) round-trips through this
    completely unchanged, while a response that buries either one behind a
    sentence or two of chatter, or wraps it in markdown fences the prompt
    explicitly asked it not to use (Ollama's small local models, in
    practice - qwen2.5:3b and, less often but still seen, qwen2.5-
    coder:3b), still gets classified correctly instead of having that
    chatter or fence syntax shipped to the client as if it were part of
    the SQL/marker text itself.

    Checked in this order:
      1. The '*** NO SQL ***' marker, searched for (not just matched at
         position 0) within the first _MAX_MARKER_PREAMBLE_CHARS - if
         found, everything before it is discarded and the marker onward is
         returned as-is. This only fixes the marker's POSITION; every
         caller still runs _NO_SQL_PREFIX_RE.match()/_strip_no_sql_prefix()
         on the result to actually recognize it and extract the free text,
         exactly as before.
      2. A complete, PAIRED ```-fenced block anywhere in the text (see
         _SQL_FENCE_RE) - if found, its contents alone are returned,
         discarding everything outside the fence (chatter before AND
         after it, e.g. "Here's the SQL:\\n```sql\\nSELECT ...\\n```\\nLet
         me know if you need anything else!").
      3. No complete pair found - the response may still have an UNPAIRED
         fence delimiter at one end (an opening ``` with no closing ```,
         a stray closing ``` with no opening one at all, or an opening ```
         glued directly onto the SQL with no recognized tag and no
         newline to anchor on - see _SQL_FENCE_RE's own comment for why
         that shape doesn't count as a "complete pair"). _LEADING_FENCE_RE
         and _TRAILING_FENCE_RE each strip their end independently, so
         either shape - or neither, for the common case of a response
         that's already clean - is handled without requiring both."""
    text = (raw_text or "").strip()
    if not text:
        return text

    marker_match = _NO_SQL_SEARCH_RE.search(text[:_MAX_MARKER_PREAMBLE_CHARS])
    if marker_match:
        return text[marker_match.start():].strip()

    fence_match = _SQL_FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()

    text = _LEADING_FENCE_RE.sub('', text, count=1)
    text = _TRAILING_FENCE_RE.sub('', text, count=1)
    return text.strip()


# Fixed apology text for when "all databases" mode's triage call fails
# outright (see triage_all_mode_question's "failed" outcome). Reserved
# specifically for the "api_error": False case - the model actually
# responded (twice), but with something unparseable both times, so there's
# no real per-call detail to surface (unlike _COMMON_FORMAT_RULES' own "I
# cannot respond with reasonable confidence" case, which asks the MODEL
# itself to explain why in its own words - there's no such text to draw on
# here). Still names the one honest reason that IS known (two attempts,
# neither produced a usable response) rather than a bare, unexplained "I
# am not able to respond" with nothing behind it. The OTHER "failed" case
# (api_error=True: the LLM call itself raised, and its own retry budget -
# key rotation and/or transient-error retries - was fully used up without
# ever getting a response at all) used to show a second fixed apology
# text here (identical regardless of which model or what actually went
# wrong); it's now built per-call by format_llm_error_for_user() instead
# (see that function's own section comment, and its call site in
# stream_translation()'s router branch below) - honest about WHY it
# failed, and including the real error, rather than one more generic
# "try again in a moment."
_TRIAGE_FAILURE_TEXT = (
    "*** NO SQL *** I wasn't able to produce a usable response to your "
    "prompt, even after retrying. Try rephrasing your question."
)


def get_llm_schema_text(descriptor, user_identity, force_refresh=False):
    """Single-dataset mode's own schema fetch - stream_translation()'s
    inline "byte-for-byte the same single-connection path this endpoint
    has always run" branch (reached whenever router_only_group_mode is
    False: in_scope_mode isn't "group", or an explicit database_url
    override is in play), the one call site that hands a schema straight
    to the LLM being asked to translate a prompt into SQL for a single,
    explicitly-identified dataset.

    Wraps get_database_schema() with the SCHEMA_TABLES_ONLY reduction
    (see backends/base.py's derive_tables_only_schema_text) so that, when
    that flag is enabled, this one call site hands the LLM the cheaper
    tables-only derivative instead of the full deep schema text -
    exactly, and only, for this "translating NL to SQL in single-dataset
    mode" case. get_database_schema() itself is completely untouched by
    this (same cache key, same cached full deep text either way) - this
    only trims what gets handed onward to the LLM from here.

    Deliberately NOT used anywhere else schema_text reaches an LLM:
    dataset-group mode's own Phase A triage (connection_router.py's
    build_router_candidate_summaries) already uses its own much smaller
    "shallow" candidate-summary representation, unrelated to this;
    dataset-group mode's Phase B fanout (_run_phase_b_fanout via
    generate_sql_for_connection below) always sees the full deep schema;
    Phase C's cross-database summarization (_build_all_mode_schema_block)
    and /api/summarize-results's own schema fetch (stream_summarize_
    result) both always see the full deep schema too. SCHEMA_TABLES_ONLY
    only ever affects this one path."""
    schema = translate_routes.get_database_schema(descriptor, user_identity, force_refresh=force_refresh)
    if SCHEMA_TABLES_ONLY:
        schema = derive_tables_only_schema_text(schema)
    return schema


def get_triage_schema_text(descriptor, user_identity, force_refresh=False):
    """Single-dataset mode's own Call 1 (triage_single_dataset_question,
    see its own docstring below) - the ONLY call site that hands this
    dataset's SHALLOW schema (deep=False - Phase 1/catalog-only, no live
    per-table queries - see db.get_database_schema's own docstring) to an
    LLM, mirroring dataset-group mode's own Phase A triage (connection_
    router.build_router_candidate_summaries), which already fetches every
    in-scope connection's schema this same cheap way for the identical
    reason: classifying a prompt into general knowledge/schema/help/SQL
    needs to know this dataset's shape (its dialect and table/tab names),
    never real data or column-level/constraint/index detail, so there is
    no reason to pay Phase 2's live-query cost before even knowing whether
    real SQL generation (get_llm_schema_text below - the ONLY call site
    that still fetches the full deep schema for this mode) will run at
    all. Cached completely independently from get_llm_schema_text's own
    deep fetch (see get_database_schema's cache_key/deep=False split) -
    a cold triage-schema cache never forces a deep fetch, and vice versa.

    Deliberately NOT reduced further by SCHEMA_TABLES_ONLY (unlike
    get_llm_schema_text above) - that flag's own derive_tables_only_
    schema_text() reduction is meant to trim what an already-DEEP schema
    hands an LLM; the shallow fetch here is already far smaller than even
    that reduced form, so there is nothing left for it to usefully do."""
    return translate_routes.get_database_schema(descriptor, user_identity, force_refresh=force_refresh, deep=False)


def generate_sql_for_connection(descriptor, prompt, history, provider, client, model,
                                 user_identity, force_schema_refresh=False,
                                 api_key=None, tried_keys=None, using_byok=False):
    """Generates SQL for exactly ONE connection - behaviorally identical to
    what happens today when a user has that one connection selected and
    submits `prompt`: fetches its full (TTL-cached) schema via
    get_database_schema(), resolves its dialect intro, appends
    _COMMON_FORMAT_RULES, builds llm_input via provider.build_llm_input(),
    and runs the same transient-error/key-rotation retry loop
    stream_translation()'s single-connection path has always run
    (MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS/
    provider.classify_error()/provider.get_key_pool_size()) before calling
    provider.call(). This is a standalone module-level function (not a
    refactor of stream_translation()'s inline code, which keeps its own
    copy of this same logic for the single-connection path, separately
    tested - see this module's docstring on the backward-compatibility
    guarantee) so it can be safely reused by _run_phase_b_fanout below
    without touching that existing, already-tested code path at all.

    Generator: yields fully wire-encoded NDJSON progress lines
    (`json.dumps({"status": "retrying", ...}) + "\\n"`), identical in
    shape to what stream_translation() has always emitted inline, so a
    caller that wants to forward live progress can do
    `... = yield from generate_sql_for_connection(...)`. A caller that
    doesn't care about live progress (Phase B's parallel fan-out, which
    runs in a worker thread with no NDJSON stream of its own to forward
    into) drains this generator via _drain_generation() below instead,
    discarding every yielded line.

    `history` and `api_key`/`tried_keys` are explicit parameters (not
    closed-over/`nonlocal`, unlike stream_translation()'s inline retry
    loop) specifically so a ThreadPoolExecutor worker can drive its own,
    independent key-rotation budget - N threads racing on one shared
    mutable `tried_keys` set would corrupt it, so Phase B always calls
    this with a fresh, independently-picked key and a fresh {api_key} set
    (see _run_phase_b_fanout).

    `using_byok=True` means `api_key` is a user's own "Bring Your Own Key"
    (see state_store.py's get_llm_byok_key) rather than one of this app's
    own env-configured keys: the retry loop's key-rotation budget is
    forced down to exactly 1 (there is no second key to rotate to, and
    silently falling back to an env key would defeat the whole point of
    the user supplying their own), and format_llm_error_for_user() is
    told so it can word an "invalid key" failure as "fix your key in
    Preferences" rather than "this app's admin needs to fix this".

    Returns (via `return`, capturable by `yield from` or
    _drain_generation): (generated_sql, usage_info, duration_ms,
    final_api_key, final_client) - generated_sql already has markdown
    code-fences stripped. On total failure, raises the final classified-
    as-fatal (or retry-budget-exhausted) exception wrapped as
    LlmCallFailed (see its own docstring) - str() on it is already the
    full, categorized, user-facing message format_llm_error_for_user()
    builds, so a caller that just does str(exc) (e.g.
    _run_phase_b_fanout's per-connection failure handling) gets that text
    with no further changes. Never swallows anything; the caller decides
    how to handle it.

    Also verifies the language of a '*** NO SQL ***'-prefixed free-text
    reply (this connection couldn't confidently generate SQL, and said why)
    against `prompt`'s own detected language - see _no_sql_language_
    mismatch's docstring and this function's own inline comments above its
    retry loop. A persistent mismatch after one corrective retry raises
    LlmCallFailed too, same as any other exhausted-retry-budget failure
    above - never returns known-wrong-language free text."""
    schema = translate_routes.get_database_schema(descriptor, user_identity, force_refresh=force_schema_refresh)

    try:
        dialect_name = get_backend(descriptor).dialect_name
    except Exception:
        dialect_name = "PostgreSQL"
    dialect_intro = _DIALECT_PROMPT_INTROS.get(dialect_name, _DEFAULT_DIALECT_PROMPT_INTRO)

    system_instruction = dialect_intro + _COMMON_FORMAT_RULES
    schema_block = f"Database Schema:\n{schema}\n\n"
    new_prompt_content = f"User Request: {prompt}\n\nSQL Query:"

    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    # Computed once, up front, off this connection's own prompt (the exact
    # text sent to the LLM as "User Request: ..." above - whichever of
    # triage's per-connection rewrite or the original question
    # _run_phase_b_fanout's caller resolved this to, see that function's
    # own docstring). None (detection unavailable/too low-confidence)
    # disables the check below entirely, same as every other call site
    # that threads this through.
    expected_language_code = translate_routes._detect_language(prompt)

    start_time = time.perf_counter()
    generated_sql = ""
    usage_info = {}
    # Bounded 2-attempt outer loop - this function still uses the older
    # '*** NO SQL ***'-marker convention (_COMMON_FORMAT_RULES), unlike
    # stream_translation()'s single-connection Call 2 (which moved to a
    # JSON "cannot_answer_reason" envelope) - but a free-text '*** NO SQL
    # ***' reply here is exactly the same kind of prose that can drift
    # into the wrong language under the same "foreign-language schema/data
    # pulls the model along" failure mode _no_sql_language_mismatch was
    # built to catch for that other call site (and for connection_router.
    # py's triage_all_mode_question). This function - Phase B's per-
    # connection SQL generation, reached once triage_all_mode_question has
    # already routed a dataset-group-mode question here for real SQL - was
    # never given that same check when it was added elsewhere, which is
    # exactly the gap a user reported seeing in practice: a connection
    # that couldn't confidently generate SQL would occasionally explain
    # why in the wrong language, with nothing here to catch or correct it.
    # Same "1 real attempt + 1 corrective retry" budget as those other two
    # call sites.
    for language_attempt in range(2):
        llm_input = provider.build_llm_input(history, schema_block, new_prompt_content)
        # transient_attempt tracks the shared same-key/after-a-delay retry
        # budget (MAX_TRANSLATION_ATTEMPTS) - reset fresh on each
        # language_attempt, same reasoning as stream_translation()'s own
        # Call 2 loop: a language-mismatch retry is a brand new call, not
        # a continuation of whatever transient-error budget the previous
        # attempt happened to consume. tried_keys/key_pool_size (the
        # separate key-rotation budget) is deliberately NOT reset here -
        # it's shared across language attempts, same as Call 2's own loop.
        transient_attempt = 1
        while True:
            try:
                generated_sql, usage_info = provider.call(client, model, llm_input, system_instruction)
                break
            except Exception as e:
                retry_action = provider.classify_error(e)
                if retry_action is None:
                    raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e

                if retry_action["rotate_key"]:
                    if len(tried_keys) >= key_pool_size:
                        raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e
                    next_key = provider.pick_api_key(exclude=tried_keys)
                    if next_key != api_key:
                        api_key = next_key
                        client = provider.make_client(api_key)
                    tried_keys.add(api_key)
                    logger.warning(
                        "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        provider.name, len(tried_keys), key_pool_size, e
                    )
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": len(tried_keys),
                        "maxAttempts": key_pool_size,
                        "delaySeconds": 0,
                        "rotatedKey": True,
                    }) + "\n"
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e
                logger.warning(
                    "%s call failed (attempt %d/%d), retrying in %ds: %s",
                    provider.name, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e
                )
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

        generated_sql = _clean_generated_sql(generated_sql)

        # Real SQL never enters the language-mismatch check below at all -
        # only a '*** NO SQL ***'-prefixed free-text reply (a "couldn't
        # confidently generate SQL for this, here's why" explanation) is
        # checked, same scoping _no_sql_language_mismatch's own docstring
        # describes for its other two call sites.
        stripped = generated_sql.strip()
        if _NO_SQL_PREFIX_RE.match(stripped):
            free_text = _strip_no_sql_prefix(stripped)
            actual_language_code = _no_sql_language_mismatch(free_text, expected_language_code)
            if actual_language_code is not None:
                expected_name = translate_routes._describe_language(expected_language_code)
                actual_name = translate_routes._describe_language(actual_language_code)
                if language_attempt + 1 < 2:
                    logger.warning(
                        "Phase B connection generation's free-text reply came back in %s instead of "
                        "the prompt's own %s (attempt %d/2) - discarding, retrying with an explicit "
                        "correction",
                        actual_name, expected_name, language_attempt + 1,
                    )
                    new_prompt_content = (
                        f"{new_prompt_content}\n\nCORRECTION: your previous free-text reply to this "
                        f"exact request was written in {actual_name}, which is WRONG - the request was "
                        f"in {expected_name}, so your '*** NO SQL ***' reply must be written entirely "
                        f"in {expected_name} this time. Write your full response again, from scratch, "
                        f"entirely in {expected_name} this time."
                    )
                    continue
                # The one corrective retry is exhausted and the reply
                # STILL came back in the wrong language - mirrors Call 2's
                # own "never knowingly serve a response in the wrong
                # language" guarantee: this ONE connection fails outright
                # (an honest, specific LlmCallFailed - caught by
                # _run_phase_b_fanout's _run_one_timed, above, and surfaced
                # as a per-connection generation_failures entry) rather
                # than silently showing free text already confirmed to be
                # in the wrong language. Every other selected connection in
                # this same turn is unaffected - _run_phase_b_fanout runs
                # each one independently.
                logger.warning(
                    "Phase B connection generation's free-text reply still came back in %s instead of "
                    "%s after retrying - failing this one connection rather than serving a "
                    "known-wrong-language response",
                    actual_name, expected_name,
                )
                raise LlmCallFailed(
                    f"The response kept coming back in {actual_name} instead of {expected_name}, "
                    f"even after retrying."
                )
        break
    end_time = time.perf_counter()

    return generated_sql, usage_info, round(1000 * (end_time - start_time)), api_key, client


def _drain_generation(gen):
    """Runs a generate_sql_for_connection() generator to completion from a
    plain (non-generator) context - a ThreadPoolExecutor worker has no
    `yield from` of its own to capture the return value with. Discards
    every yielded progress line (no live per-attempt retry UI for Phase
    B's parallel fan-out - see this module's docstring on why batching the
    whole fan-out into one final response is the deliberate, simpler
    choice here). Re-raises whatever the generator itself raised,
    unchanged."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _classify_generation_outcome(entry, outcome):
    """Classifies one connection's raw Phase B outcome - either
    ("ok", generated_sql, usage_info, duration_ms) or
    ("failed", error_str, duration_ms), the exact tuple shapes
    _run_phase_b_fanout's ThreadPoolExecutor loop already produces - into
    the one shape both that function's per-completion streaming event AND
    its final original-order summary loop need, so the marker-prepend/
    note-strip logic is written exactly once instead of twice. Returns one
    of:
      {"outcome": "sql", "sql": <marker-prepended text>}
      {"outcome": "note", "text": <str, '*** NO SQL ***' prefix stripped -
        "" for the rare case where the model returned a blank response;
        _run_phase_b_fanout's own final loop intentionally still drops
        that case from `database_notes`, exactly like this function's
        pre-extraction inline code always has - but the streaming event
        still needs SOME event for it, so it's surfaced here as an empty
        note rather than silently vanishing from the stream too>
      {"outcome": "failed", "error": <str>}
    Never raises - a raised generation call is already represented as
    outcome[0] == "failed" by the caller before this is invoked. The
    trailing duration_ms in both tuple shapes is irrelevant to
    classification itself (it's what _run_phase_b_fanout's own final loop
    uses to log each connection's own translations-table row) - unpacked
    here only so this still works against the real tuple shape.
    """
    if outcome[0] == "failed":
        return {"outcome": "failed", "error": outcome[1]}
    _, generated_sql, _usage_info, _duration = outcome
    stripped = (generated_sql or "").strip()
    if not stripped:
        return {"outcome": "note", "text": ""}
    if _NO_SQL_PREFIX_RE.match(stripped):
        return {"outcome": "note", "text": _strip_no_sql_prefix(stripped)}
    marked = f"-- database: {entry['kind']}:{entry['id']} ({entry['name']})\n{stripped}"
    return {"outcome": "sql", "sql": marked}


def _run_phase_b_fanout(selected_entries, prompts, histories, provider, model, user_identity, force_schema_refresh):
    """"All databases" mode's Phase B: runs generate_sql_for_connection()
    once per entry in `selected_entries`, in PARALLEL via a
    ThreadPoolExecutor (same pre-allocate-results-array +
    future_to_index + as_completed pattern as db.py's
    build_router_candidate_summaries, one worker per connection - no
    artificial cap, since this is already bounded by
    MAX_IN_SCOPE_CONNECTIONS upstream in triage_all_mode_question).

    This is a GENERATOR: as each connection's call completes (in
    COMPLETION order, not original order - this is what lets
    stream_translation()'s router_only_group_mode branch report each
    database's own result to the client as soon as it's ready, rather
    than waiting for the slowest one), it yields `(entry, classified)`,
    where `classified` is _classify_generation_outcome's return shape for
    that connection. A caller uninterested in the streaming events (e.g.
    a test only checking the final aggregate) should drain it with the
    same `_drain_generation`-style idiom already used elsewhere in this
    module. Once every connection has completed, this function `return`s
    (captured via `StopIteration.value`, same idiom) the exact same four
    values it has always returned - see below - rebuilt in
    `selected_entries`' ORIGINAL order (not completion order).

    `prompts` is a list the same length/order as `selected_entries` - each
    connection's OWN instruction, not necessarily the user's original
    question verbatim. The caller (stream_translation()'s router_only_all_
    mode branch) is responsible for resolving each entry to either the
    triage call's per-connection rewrite (triage_all_mode_question's
    "database_prompts" - see that function's docstring for why the
    original, possibly cross-database-phrased question can't just be
    reused unchanged here) or the original question itself as a fallback
    when no rewrite was supplied for that connection - this function
    itself stays oblivious to where each prompt came from, it just sends
    prompts[i] to selected_entries[i].

    `histories` is likewise a list the same length/order as
    `selected_entries` - each connection's own FULLY MERGED chat history
    (see stream_translation()'s `connection_histories` declaration comment
    for where this comes from and why it's already merged across single-
    connection mode and every prior all-mode turn by the time it reaches
    here). Same obliviousness as `prompts`: this function just sends
    histories[i] to selected_entries[i], already truncated/shaped by the
    caller.

    Each call is fully independent: its OWN freshly-picked api_key AND a
    client built from that exact key (never a shared tried-keys set - N
    threads racing on one shared mutable set would corrupt it - see
    generate_sql_for_connection's docstring), that connection's OWN
    history (histories[i] above), and that connection's own full schema/
    dialect intro - i.e. exactly as if the user had selected just that one
    connection and submitted its own `prompts[i]` directly. There is
    deliberately no shared `client` parameter here (unlike
    triage_all_mode_question/
    summarize_all_mode_results, which reuse the caller's already-picked
    key/client as their starting point) - every worker's key is picked
    independently at fan-out time, so a single client handed in from
    outside would almost always belong to a DIFFERENT key than at least
    some workers end up using (see _run_one's own comment for the bug this
    fixes). One connection's call failing (its own retry budget exhausted,
    or a non-retryable error) does NOT prevent the others from completing -
    matches the same tolerant, per-item failure isolation already used
    both by db.py's schema-summary fan-out and by execute_routes.py's
    per-connection execution.

    Returns (sql_blocks, database_notes, generation_failures, usage_totals,
    phase_b_log_entries):
      sql_blocks: [(entry, marked_sql_text), ...] - one per entry that
        returned REAL SQL, marker-prepended here (mechanically, by this
        function - never by the model, which only ever sees ONE
        connection so it has nothing to mislabel) with the exact stable
        format execute_routes.py already parses: '-- database:
        preset:<id> (<name>)' / '-- database: custom:<key> (<name>)'.
        In `selected_entries`' ORIGINAL (most-relevant-first) order, not
        completion order.
      database_notes: [{"kind","id","name","text"}, ...] - one per entry
        whose call returned a '*** NO SQL ***' reply instead of real SQL
        (prefix stripped), same original order.
      generation_failures: [{"kind","id","name","error"}, ...] - one per
        entry whose call raised, same original order.
      usage_totals: the five usage_info keys, summed across every call
        that actually produced a billable response (a failed call
        contributes nothing).
      phase_b_log_entries: [{"entry", "prompt", "duration", "sql_command",
        "usage"}, ...] - ONE PER SELECTED CONNECTION, regardless of
        outcome, same original order - what stream_translation()'s "route"
        outcome branch needs to log each connection's OWN dedicated
        translations-table row (see record_translation) rather than one
        combined row for the whole batch attributed to only the first
        connection, which is what this function used to force on every
        caller. `duration` is this connection's own real measured elapsed
        time (never a derived share of a shared total - these calls run in
        parallel, so "correct" here means each call's own actual wall
        time), `usage` is `{}` for a failed call (nothing billable was
        ever returned) or that call's real usage_info dict otherwise, and
        `sql_command` is the exact text to log - real marker-prepended
        SQL, a "*** NO SQL ***"-prefixed note (or the bare prefix alone
        for the rare blank-response case), or a "TRANSLATION_ERROR
        (<error>)" sentinel - matching whichever of the three outcomes
        this specific connection had, the same conventions used elsewhere
        in this module for a non-SQL or failed translation.

    Resolves `user_identity`'s "Bring Your Own Key" value for `provider`
    (state_store.get_llm_byok_key) exactly ONCE here, up front - not
    per-worker - since it's the same user/provider for every entry in
    this fan-out. When set, every worker uses that key instead of picking
    its own from the env-configured pool, and generate_sql_for_connection
    is told using_byok=True so its own retry loop won't try to rotate to
    a different (env-configured) key on an auth failure.
    """
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)

    def _run_one(entry, entry_prompt, entry_history):
        # BUG FIXED HERE: this used to pick a fresh `worker_api_key` but
        # then pass it alongside the OUTER, closed-over `client` - which
        # was built (once, at the top of stream_translation()) for
        # whatever key triage happened to be using, not this worker's own.
        # provider.pick_api_key() (Gemini's own impl - see
        # pick_gemini_api_key) picks RANDOMLY from the configured pool, so
        # with 2+ keys configured, `worker_api_key` frequently differed
        # from the key `client` was actually authenticated with. The
        # request that hit the wire used `client`'s real key the whole
        # time, but generate_sql_for_connection's retry loop believed it
        # was using `worker_api_key` - so on a 429, it excluded the WRONG
        # key from rotation (one that was never actually tried) and could
        # rotate straight back onto the real, already-exhausted key, or
        # give up as "budget exhausted" while a perfectly good configured
        # key had never been attempted at all. Building the client here,
        # from the SAME worker_api_key passed below, keeps the two
        # permanently in sync - exactly what every other rotating call in
        # this app (generate_sql_for_connection's own internal rotation,
        # triage_all_mode_question, summarize_all_mode_results) already
        # does whenever it picks a new key.
        # BYOK short-circuits the "pick a fresh key per worker" scheme
        # above entirely - there's only one key, so every worker uses it
        # (and, per generate_sql_for_connection's using_byok docstring,
        # its retry loop won't try to rotate away from it on failure).
        worker_api_key = byok_key or provider.pick_api_key()
        worker_client = provider.make_client(worker_api_key)
        gen = generate_sql_for_connection(
            entry["descriptor"], entry_prompt, entry_history, provider, worker_client, model, user_identity,
            force_schema_refresh=force_schema_refresh,
            api_key=worker_api_key, tried_keys={worker_api_key}, using_byok=bool(byok_key),
        )
        return _drain_generation(gen)  # (generated_sql, usage_info, duration_ms, _key, _client)

    def _run_one_timed(entry, entry_prompt, entry_history):
        # Wraps _run_one with its OWN start_time, captured here rather than
        # trusting generate_sql_for_connection's own returned duration_ms -
        # that value only exists on the success path (it's computed right
        # before that function's `return`, which a raised LlmCallFailed
        # never reaches). Measuring here instead means every connection
        # gets a real, honest duration whether it ultimately succeeds or
        # fails - each per-connection translations-table row logged below
        # (see stream_translation()'s "route" outcome branch) needs exactly
        # this, the same way the single-connection /api/translate failure
        # path and generate_sql_for_connection's own success path do.
        start_time = time.perf_counter()
        try:
            generated_sql, usage_info, _duration, _key, _client = _run_one(entry, entry_prompt, entry_history)
            duration = round(1000 * (time.perf_counter() - start_time))
            return ("ok", generated_sql, usage_info, duration)
        except Exception as e:
            duration = round(1000 * (time.perf_counter() - start_time))
            return ("failed", str(e), duration)

    outcomes = [None] * len(selected_entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected_entries)) as pool:
        future_to_index = {
            pool.submit(_run_one_timed, entry, prompts[i], histories[i]): i
            for i, entry in enumerate(selected_entries)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            entry = selected_entries[index]
            # _run_one_timed never raises (it catches its own exceptions
            # to measure duration on both the success and failure path -
            # see its own comment) - future.result() here can only ever
            # raise for something truly unexpected (e.g. the worker thread
            # itself being killed), which is deliberately NOT caught, same
            # as any other unexpected crash in this module.
            outcomes[index] = future.result()
            if outcomes[index][0] == "failed":
                logger.warning(
                    "Phase B generation failed for %s:%s: %s",
                    entry["kind"], entry["id"], outcomes[index][1],
                )
            # Yielded in COMPLETION order (whatever order this loop
            # actually reaches each future in) - NOT `index` order. The
            # final, order-stable return value below is rebuilt from
            # `outcomes` in ORIGINAL order regardless of what order this
            # loop yielded in, so callers that only care about the
            # aggregate (draining this generator to completion) see
            # exactly the same result they always have.
            yield entry, _classify_generation_outcome(entry, outcomes[index])

    sql_blocks, database_notes, generation_failures = [], [], []
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                     "thinking_tokens": 0, "cached_content_tokens": 0}
    # One entry per connection in `selected_entries` (ORIGINAL order, same
    # as everything else this function returns) - everything
    # stream_translation()'s "route" outcome branch needs to log this
    # connection's OWN dedicated translations-table row, rather than the
    # old single combined row attributed only to the first selected
    # connection (see this function's own module-level history/audit
    # notes above - this replaces that bundling entirely, one real row per
    # connection instead of one for the whole batch): the descriptor to
    # attribute it to, the actual per-connection instruction that was sent
    # (prompts[i] - triage's own rewrite when it supplied one, else the
    # user's original question, same resolution stream_translation()
    # already does before calling this function), the real per-connection
    # duration measured above (never a derived share of the total wall
    # time - these calls run in parallel, so each one's own elapsed time
    # is the honest number), that connection's own usage (zeroed for a
    # failure, same convention the single-connection /api/translate
    # failure path now uses), and the sql_command text to log - real SQL,
    # a "*** NO SQL ***"-prefixed note, or a TRANSLATION_ERROR(...)
    # sentinel, matching whichever of the three outcomes this connection
    # actually had.
    phase_b_log_entries = []
    for i, (entry, outcome) in enumerate(zip(selected_entries, outcomes)):
        entry_prompt = prompts[i]
        if outcome[0] == "failed":
            _, error_str, duration = outcome
            generation_failures.append({
                "kind": entry["kind"], "id": entry["id"], "name": entry["name"], "error": error_str,
            })
            phase_b_log_entries.append({
                "entry": entry, "prompt": entry_prompt, "duration": duration,
                "sql_command": f"TRANSLATION_ERROR ({error_str})",
                "usage": {},
            })
            continue
        _, generated_sql, usage_info, duration = outcome
        for k in usage_totals:
            # `or 0` guards against a provider returning this key present
            # but explicitly None (e.g. real Gemini responses report
            # thoughts_token_count as None, not 0, whenever a call didn't
            # use extended thinking) - `.get(k, 0)` alone only substitutes
            # 0 for a MISSING key, not a present-but-None one, and `+=`
            # against None raises TypeError. See _call_gemini/_call_claude/
            # _call_openai above, which are the real fix (never return
            # None in the first place) - this is a defensive backstop.
            usage_totals[k] += (usage_info or {}).get(k) or 0
        classified = _classify_generation_outcome(entry, outcome)
        if classified["outcome"] == "sql":
            sql_blocks.append((entry, classified["sql"]))
            log_sql_command = classified["sql"]
        else:
            # "note" outcome - classified["text"] may legitimately be ""
            # (the model's response was blank after stripping); the
            # aggregate `database_notes` list still drops that empty case
            # exactly as it always has (nothing useful to show in the
            # Summary tab for it), but this connection still gets its own
            # logged row below, using the same "*** NO SQL ***" convention
            # as every other non-SQL reply in this table - an empty note
            # logs as the bare prefix rather than silently having no
            # sql_command text at all.
            if classified["text"]:
                database_notes.append({
                    "kind": entry["kind"], "id": entry["id"], "name": entry["name"],
                    "text": classified["text"],
                })
            log_sql_command = ("*** NO SQL *** " + classified["text"]) if classified["text"] else "*** NO SQL ***"
        phase_b_log_entries.append({
            "entry": entry, "prompt": entry_prompt, "duration": duration,
            "sql_command": log_sql_command, "usage": usage_info or {},
        })
    return sql_blocks, database_notes, generation_failures, usage_totals, phase_b_log_entries


def _no_sql_language_mismatch(generated_sql, expected_language_code):
    """Returns the language code the free-text portion of `generated_sql`
    is actually written in, when that confidently differs from
    `expected_language_code` - or None when there's nothing to flag.

    Added alongside the language-verification machinery above
    (_detect_language/_summarize_with_retry, originally built for the two
    post-execution summarization calls - see the section comment above)
    to close the same gap for the ORIGINAL NL-to-SQL translation call
    itself: a '*** NO SQL ***'-prefixed free-text reply (a general-
    knowledge answer, a help-popup request, an error explanation - see
    _COMMON_FORMAT_RULES) is exactly the kind of prose that can drift into
    the wrong language under the same "foreign-language data pulls the
    model along" failure mode the summarization fix was built for - but
    until this was added, this call site had only a bare system-prompt
    instruction telling the model to match the user's language, with
    nothing verifying it actually did. (connection_router.py's
    triage_all_mode_question has the identical need for its own "answer"/
    "message" free text, but calls language_detect.detect_language
    directly rather than through this function - see that module's own
    docstring for why it can't import from this file at all.)

    Deliberately narrow: only ever checks free text that's already been
    identified as such by the caller (the '*** NO SQL ***'-stripped
    portion of a translation response) - it does NOT try to detect whether
    `generated_sql` itself is a '*** NO SQL ***' reply; the caller
    (stream_translation()'s single-connection path, which also generates
    plain SQL with no free text to check at all) checks _NO_SQL_PREFIX_RE
    itself first. Ordinary generated SQL is never a valid `generated_sql`
    argument here for that reason - running _detect_language on a SELECT
    statement is meaningless. SQL comments the model was asked to add
    (_COMMON_FORMAT_RULES' other named "write this in the user's language"
    case) are also NOT covered - reliably isolating just the comment text
    out of a whole SQL script for language detection is separate work;
    this closes the far more common and more visibly reported gap (a whole
    free-text reply coming back in the wrong language), not every corner
    _COMMON_FORMAT_RULES' instruction touches.

    Returns None whenever there's nothing actionable to say:
    `expected_language_code` is None (detection unavailable/low-confidence
    on the user's own prompt - see _detect_language's own docstring),
    `generated_sql` is empty, detection on it is itself unavailable/
    low-confidence, or it already matches `expected_language_code`."""
    if expected_language_code is None:
        return None
    text = (generated_sql or "").strip()
    if not text:
        return None
    actual_language_code = translate_routes._detect_language(text)
    if actual_language_code is not None and actual_language_code != expected_language_code:
        return actual_language_code
    return None


# =============================================================================
# Single-dataset mode's own two-call redesign for stream_translation()'s
# single-connection path below: Call 1 (triage_single_dataset_question)
# classifies a prompt into general knowledge/schema/help/SQL BEFORE any
# dialect-specific SQL-generation system instruction is ever built or sent,
# and Call 2 (the '*** NO SQL ***'-free JSON-enveloped SQL-generation call
# stream_translation() itself runs inline, only when Call 1 resolves to
# "sql") never has to decide whether to classify at all - it only ever
# generates SQL, with a single structured escape hatch for "I can't
# confidently answer this."
#
# This exists because a real qwen2.5-coder:7b smoke test surfaced a
# genuine architectural problem, not a prompt-wording one: the old single-
# call design (_COMMON_FORMAT_RULES, still used unchanged by dataset-group
# mode's own Phase B fan-out below - see generate_sql_for_connection - and
# deliberately left that way, per this session's repeated agreement to
# scope this redesign to the single-connection path only) asked ONE call
# to both classify (via a '*** NO SQL ***' free-text marker convention)
# AND generate SQL in the same response - the model generated genuinely
# correct SQL while ALSO spuriously prepending '*** NO SQL ***' to it,
# treating the marker as a reflexive prefix rather than a true either/or
# branch. Splitting classification and generation into two calls removes
# the marker (and the ambiguity it created) from the generation call
# entirely: Call 2's JSON envelope's OWN SHAPE (exactly one of "sql"/
# "cannot_answer_reason" populated) is what tells the two cases apart now,
# not a literal string a model has to remember to emit (or not) inside
# free text it's also trying to get right in every other way.
#
# Mirrors dataset-group mode's own two-phase precedent one level down
# (connection_router.py's triage_all_mode_question decides "answer vs.
# route" before ever picking a connection) - the identical classify-then-
# act shape, just with four outcomes instead of two, scoped to exactly one
# already-selected dataset rather than choosing among several, and (unlike
# that function) never itself generating SQL - triage_single_dataset_
# question's own "sql" outcome is purely a signal for stream_translation()
# to run Call 2 next, not something it has any further step to take here.
# =============================================================================

# _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION, _extract_json_object, and
# _parse_single_dataset_triage_response now live in connection_router.py
# (imported above) - they're shared with that module's own multi-candidate
# (dataset-group) triage prompt/parser via one unified retry-loop function,
# run_triage_call, also imported above. See that function's own docstring
# for the full reasoning behind the merge.


def triage_single_dataset_question(schema_block, prompt, provider, client, model,
                                    history=None, api_key=None, tried_keys=None, using_byok=False):
    """Single-dataset mode's own Call 1: decides which of general
    knowledge/schema questions/help questions/real SQL generation `prompt`
    needs, given `schema_block` (this dataset's own SHALLOW/overview
    schema text - see get_triage_schema_text - which is deliberately NOT
    the full deep schema stream_translation()'s eventual Call 2 still
    fetches separately, and only once this call actually resolves to
    "sql") and `history` (this session's already-trimmed conversation
    turns for this exact dataset, letting a follow-up resolve a reference
    from the prior turn, e.g. "who is the current US president?" ->
    answer -> "and the vice president?").

    A THIN WRAPPER around connection_router.run_triage_call with
    num_candidates fixed at 1 - see that function's own docstring for the
    full retry-loop/key-rotation/language-verification/GENERATOR
    reasoning (unchanged by the merge that introduced this wrapper; this
    function's own signature, generator-ness, and return shape are all
    still exactly what they were before that merge, so every existing
    caller/test of this function needed zero changes).

    Returns exactly one of:
      {"outcome": "general", "answer": <str>, "usage": <dict|None>}
      {"outcome": "schema", "usage": <dict|None>}
      {"outcome": "help", "usage": <dict|None>}
      {"outcome": "sql", "usage": <dict|None>}
      {"outcome": "failed", "api_error": <bool>, "error": <exception|None>}"""
    return (yield from run_triage_call(
        1, schema_block, prompt, provider, client, model,
        history=history, max_connections=1,
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
    ))


# The output-format/behavior rules that follow the dialect intro in
# stream_translation()'s single-connection Call 2 (SQL generation, see the
# module-level section comment above _SINGLE_DATASET_TRIAGE_SYSTEM_
# INSTRUCTION) - identical for every dialect, so pulled out once here
# rather than duplicated per dialect entry, same reasoning as
# _COMMON_FORMAT_RULES. Deliberately a SEPARATE constant, not a
# replacement for _COMMON_FORMAT_RULES - that one is still used, unchanged,
# by generate_sql_for_connection (dataset-group mode's Phase B fan-out,
# explicitly out of scope for this redesign - see this module's own
# docstring). Call 2 is only ever reached once Call 1 has already decided
# "sql" - it is never asked to classify anything itself, which is exactly
# what removes the failure mode this redesign exists to fix: there is no
# more '*** NO SQL ***' marker for a model to reflexively (and, in
# practice, sometimes spuriously) emit, since the JSON envelope's OWN
# SHAPE (exactly one of "sql"/"cannot_answer_reason" populated) is what
# the app now uses to tell "real SQL" and "can't confidently answer" apart,
# not a literal string sharing space with the free text.
_SQL_GENERATION_FORMAT_RULES = load_prompt("sql_generation_format_rules.txt")


def _parse_sql_generation_response(text):
    """Parses Call 2's raw response text (_SQL_GENERATION_FORMAT_RULES)
    into exactly one of:
      {"outcome": "sql", "sql": <non-empty str>}
      {"outcome": "cannot_answer", "reason": <non-empty str>}
      None  # unparseable, both populated, or neither populated - caller retries
    Never raises. "sql" is additionally passed through _clean_generated_
    sql's own fence-stripping as defense-in-depth only (that function's
    '*** NO SQL ***'-marker search is a harmless no-op here - real SQL has
    no legitimate reason to contain that exact substring) - the prompt
    above already asks for a bare, fence-free string, but a model that
    nests a fenced block INSIDE the JSON string value is otherwise
    indistinguishable from one that didn't. The outer JSON envelope itself
    goes through _extract_json_object (see its own docstring) rather than
    a bare strip_markdown_fence()+json.loads(), so a weak/local model that
    wraps the WHOLE envelope in a sentence or two of chatter (not just an
    inner SQL string) still parses instead of forcing a retry."""
    parsed = _extract_json_object(text)
    if not isinstance(parsed, dict):
        return None

    sql = parsed.get("sql")
    sql = sql.strip() if isinstance(sql, str) else ""
    reason = parsed.get("cannot_answer_reason")
    reason = reason.strip() if isinstance(reason, str) else ""

    if bool(sql) == bool(reason):
        # Neither populated, or both populated - the contract requires
        # exactly one; either way this attempt gets no more benefit of the
        # doubt than a plain unparseable response would.
        return None
    if sql:
        return {"outcome": "sql", "sql": _clean_generated_sql(sql)}
    return {"outcome": "cannot_answer", "reason": reason}
