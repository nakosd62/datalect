"""
connection_router.py

Home of the app's unified triage call (run_triage_call): Call 1 for BOTH
single-dataset mode (translate_routes.py's triage_single_dataset_question,
now a thin num_candidates=1 wrapper around this module's run_triage_call)
and Phase A of "all databases"/group mode (called directly from
translate_routes.py's router_only_group_mode branch, with num_candidates
== the number of in-scope connections). See run_triage_call's own
docstring for the full picture and for why this used to be two separate,
substantially duplicated implementations (this module's own
triage_all_mode_question plus translate_routes.py's
triage_single_dataset_question) before this merge.

Group mode's own half of this - deciding whether a natural-language
question can be answered directly from the session's in-scope
connections' names/dialects/table names alone (see db.py's
resolve_in_scope_descriptors), or genuinely needs real data from one or
more specific connections, and if so which ones - before any full,
column-level schema is ever fetched or sent to the model - only runs at
all when a session's in_scope_mode is "all". Single-dataset mode's own
half runs for every other session, using this exact same retry-loop/
parsing/key-rotation machinery with num_candidates fixed at 1 and no
indices/database_prompts/message to resolve.

Deliberately reuses the SAME LlmProvider/client/model translate_routes.py
already built for the main SQL-generation call, rather than a separate
"router model" - see this module's docstring in the plan this implements
for why (picking connections and generating dialect-correct SQL are
different-difficulty tasks best kept as two calls, but there's no
standalone cheap model configured for the first one, so it just borrows
whichever provider/model the session is already using). This means
run_triage_call is a second call against the same client/API key
translate_routes.py already picked for the main generation call - though
it DOES run its own retry loop against that key pool (key rotation on a
429, wait-and-retry on a transient 5xx/timeout, see run_triage_call's
docstring) rather than deferring retry entirely to the caller.
"""

import json
import re
import time

from app_config import logger, MAX_IN_SCOPE_CONNECTIONS, MAX_TRANSLATION_ATTEMPTS, TRANSLATION_RETRY_DELAY_SECONDS
# Shared with translate_routes.py's own language-verification machinery
# (_no_sql_language_mismatch/_summarize_with_retry there) - see
# language_detect.py's own module docstring for why this lives in its own
# standalone module rather than either file importing from the other
# (translate_routes.py already imports FROM this module, so the reverse
# would be circular).
from language_detect import detect_language, describe_language
from prompt_loader import load_prompt

# How many of a session's in-scope connections a single question's Phase A
# routing may ever select at once - the same MAX_IN_SCOPE_CONNECTIONS cap
# config_routes.py applies to how many a user may mark in scope AT ALL
# (see its docstring in app_config.py). There is exactly one "how many
# databases" knob, used everywhere the concept comes up.


def _build_candidate_schema_block(candidate_summaries):
    """Renders `candidate_summaries` (name/dialect/table-list per in-scope
    connection - see run_triage_call's own docstring for exactly what this
    is: names/dialects/table names only, no column-level detail) into its
    own stable block - analogous to single-connection mode's schema_block
    (translate_routes.py's generate_sql_for_connection builds the
    identically-shaped f"Database Schema:\n{schema}\n\n") - meant to be
    passed to provider.build_llm_input() as ITS schema_block parameter
    rather than folded into the ever-changing new-prompt text.

    This is what lets build_llm_input() place this block ahead of the
    history vector (see that function's own docstring on exactly where
    schema_block attaches - prepended to the first historical turn when
    there is history, folded into the new prompt only when there isn't),
    matching the design's own LLM-1 input ordering: "<triage system
    instructions> : <summary database schema of all databases> : <history
    vector> : <new user prompt>". Only ever built for the multi-candidate
    (group-mode) call site - single-dataset mode builds its own schema
    block from a plain shallow-schema TEXT dump instead (see
    translate_routes.py's get_triage_schema_text) - run_triage_call itself
    is agnostic to which convention produced the schema_block it's given;
    see that function's own docstring for why unifying the two schema-
    block-rendering conventions themselves was deliberately left out of
    this merge."""
    lines = ["Candidate database connections:"]
    for i, c in enumerate(candidate_summaries):
        table_names = c.get("table_names") or []
        shown = ", ".join(table_names) if table_names else "(no tables discovered)"
        lines.append(
            f"[{i}] name={c.get('name')!r} dialect={c.get('dialect')!r} tables={shown}"
        )
    lines.append("")
    return "\n".join(lines) + "\n\n"


def strip_markdown_fence(text):
    """Strips a leading/trailing markdown code fence (```/```json/etc.)
    from `text` if present, tolerating models that wrap their JSON despite
    being told not to. Returns the (possibly unchanged) stripped string.
    Used by _extract_json_object here (as its first, cheapest candidate),
    and by translate_routes.py's own _clean_summary_response (Phase C's
    structured per-database summary response) - public (no leading
    underscore) specifically so this tolerance stays written in exactly
    one place across both callers rather than being copied."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


# A ```-fenced block found ANYWHERE in a response (not just anchored to
# the very start/end the way strip_markdown_fence's own check is) - used
# by _extract_json_object's own "chatter around a fence" fallback below.
_JSON_FENCE_RE = re.compile(r'```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```', re.DOTALL | re.IGNORECASE)


def _extract_json_object(text):
    """Best-effort JSON-object extraction from a triage/SQL-generation
    call's raw text response (every one of this app's JSON-enveloped
    prompts asks for ONLY a bare JSON object, no fences, no other text) -
    tolerating leading/trailing chatter and markdown-fence wrapping that a
    naive strip_markdown_fence()-then-json.loads() sequence would fail on
    (that only strips a fence anchored at the very start/end of the
    string). Without this, a weak/local model that wraps its JSON in even
    a single sentence of chatter (e.g. "Sure, here's the SQL you asked "
    "for:\\n\\n```json\\n{...}\\n```\\n\\nLet me know if you need anything "
    "else!") would fail to parse at all.

    Returns the parsed dict, or None if nothing usable could be found.
    Tried in order, each only attempted after the previous one fails to
    yield a dict, so a well-behaved response never pays for the more
    expensive fallback searches:
      1. The whole (fence-stripped) text, as-is - the well-behaved case
         every provider is expected to produce in practice.
      2. The contents of a ```-fenced block found ANYWHERE in the text
         (see _JSON_FENCE_RE) - a model that wraps its JSON in a sentence
         or two of chatter before and/or after a fenced block.
      3. The substring from the first '{' to the last '}' in the text - a
         last-resort attempt at a model that emits chatter with no fence
         at all, wrapped around (or before) a real JSON object.
    Never raises. Used by _parse_single_dataset_triage_response and
    _parse_multi_candidate_triage_response here, and by
    translate_routes.py's own _parse_sql_generation_response (Call 2's own
    parser, imported from here for the same reason strip_markdown_fence
    already is)."""
    if not text:
        return None
    candidates = [strip_markdown_fence(text)]
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _clean_indices(raw_list, num_candidates, max_connections):
    """Dedupe/range-check/cap a raw (untrusted, model-supplied) list of
    candidate indices, preserving the model's own ranking (first
    occurrence kept, in the order given). Never raises - non-coercible
    items are silently skipped, out-of-range/duplicate indices are
    silently dropped, and the result is capped at `max_connections` (stops
    appending once reached, rather than truncating a longer valid list
    from the end). Returns [] if nothing survives - used by
    _parse_multi_candidate_triage_response so this validation only needs
    to be right in one place."""
    seen = set()
    indices = []
    for item in raw_list or []:
        try:
            index = int(item)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= num_candidates or index in seen:
            continue
        seen.add(index)
        indices.append(index)
        if len(indices) >= max_connections:
            break
    return indices


def _clean_database_prompts(raw, valid_indices):
    """Validates a "sql" response's untrusted "database_prompts" value
    against the (already-cleaned) `valid_indices` list, returning a plain
    {int_index: non_empty_prompt_str} dict covering only entries that
    actually check out - never raises, and never lets one bad entry throw
    away the rest. An index missing from the result (whether `raw` wasn't
    a dict at all, that key was absent, its value wasn't a non-empty
    string, or the key didn't parse to one of `valid_indices` in the first
    place - e.g. it referred to an index _clean_indices already dropped as
    out-of-range/duplicate) simply has no per-connection rewrite - the
    caller falls back to the original user question for that one
    connection, same as if this whole field were absent. Deliberately
    keyed by index rather than positionally parallel to `indices`: doing
    it this way means _clean_indices' own deduping/capping/reordering of
    the raw indices list can never desynchronize this dict from whichever
    indices actually survived - each entry is independently matched by its
    own index, not by position."""
    if not isinstance(raw, dict):
        return {}
    valid = set(valid_indices)
    cleaned = {}
    for key, value in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index not in valid:
            continue
        if isinstance(value, str) and value.strip():
            cleaned[index] = value.strip()
    return cleaned


def is_label_only_response(text):
    """True when `text` is a known failure mode of the "<label line>
    \\n\\n<body>" shape both _MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION
    here and translate_routes.py's _SUMMARY_SYSTEM_INSTRUCTION ask the
    model for (a short section-heading label, written in the SAME
    LANGUAGE as the user's own question - see those two prompts -
    followed by a blank line, then the real response): the model wrote a
    leading line, a blank line, and then stopped, leaving nothing (or only
    whitespace) as the body. Also true for a genuinely empty/whitespace-
    only `text`.

    Deliberately does NOT flag a response with no blank line at all as
    invalid - that's a plain, un-labeled answer (the model skipped the
    label-line convention entirely), which this app has always accepted
    as-is rather than penalizing; only a response that visibly started
    the two-part shape and then produced no real content counts as this
    specific failure. This is also why the check is POSITION-based (the
    first line, and only the first line, up to the first blank line)
    rather than content-based: it doesn't need to know what the label
    text actually says (impossible now that it's translated into the
    user's own question's language - see those two prompts), only
    whether whatever is there was followed by real content or not.

    Used by run_triage_call here and by translate_routes.py's
    summarize_all_mode_results for the same reason. Both MUST call this
    on the response text before applying their own `.strip()` to it (see
    each call site) - a response that's just "<label>\\n\\n" with nothing
    real after it has its tell-tale trailing blank line removed by a
    naive `.strip()`, at which point it's indistinguishable from a plain
    single-line, never-labeled response that must NOT be flagged; this
    function only strips LEADING whitespace itself for exactly that
    reason. client.js's own renderMarkdownLiteSummaryTab() separately
    relies on this same first-line/blank-line convention to decide what
    to bold - but purely for DISPLAY, not validity, so it has no need for
    this check itself: by the time either "answer"/"message"/summary text
    reaches the client, this function has already guaranteed it isn't
    label-only."""
    if not isinstance(text, str):
        return False
    if not text.strip():
        return True
    parts = re.split(r'\n[ \t]*\n', text.lstrip(), maxsplit=1)
    if len(parts) != 2:
        return False
    return not parts[1].strip()


# =============================================================================
# Single-dataset triage (num_candidates == 1): decides "general" (answerable
# from general knowledge/conversation alone), "schema" (opens the Schema
# Viewer), "help" (opens the Help modal), or "sql" (generate and execute
# real SQL against this one dataset). Byte-identical prompt wording and
# parsing to this module's pre-merge translate_routes.py counterpart - see
# run_triage_call's own docstring for why keeping this branch unchanged
# mattered.
# =============================================================================

_SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION = load_prompt("triage_single_dataset.txt")


def _parse_single_dataset_triage_response(text):
    """Parses a single-dataset triage call's raw response text
    (_SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION) into exactly one of:
      {"outcome": "general", "answer": <non-empty str>}
      {"outcome": "schema"}
      {"outcome": "help"}
      {"outcome": "sql"}
      None  # unparseable, or doesn't fit any of the four shapes - caller retries
    Never raises. Mirrors _parse_multi_candidate_triage_response's own
    "general"/"schema"/"help" handling exactly - same "action"-keyed JSON
    contract and the same refusal to guess around a missing/unrecognized
    "action" - just without any indices/message/database_prompts to
    validate, since a single-dataset triage call has nothing to pick
    between."""
    parsed = _extract_json_object(text)
    if not isinstance(parsed, dict):
        return None

    action = parsed.get("action")
    action = action.strip().lower() if isinstance(action, str) else None

    if action == "general":
        answer = parsed.get("answer")
        if isinstance(answer, str) and answer.strip():
            return {"outcome": "general", "answer": answer.strip()}
        # Missing/empty "answer" - unlike "schema"/"help"/"sql" (which need
        # no free text from the model at all), "general" has nothing to
        # fall back on here, so this is a parse failure like any other,
        # giving the bounded retry loop below another attempt rather than
        # returning an empty answer to the user.
        return None

    if action in ("schema", "help", "sql"):
        return {"outcome": action}

    return None


# =============================================================================
# Multi-candidate triage (num_candidates > 1, i.e. dataset-group/"all
# databases" mode): decides "general"/"schema"/"help" exactly like the
# single-dataset case above, or "sql" - generate and execute real SQL
# against one or more of the candidate connections. On total failure this
# returns "failed" rather than guessing a connection: a wrong guess here
# would mean silently running real SQL against a database the user never
# actually asked about, which is a materially worse failure mode than a
# routing mistake would be for a read-only pick.
# =============================================================================

_MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION = load_prompt("triage_multi_candidate.txt").replace(
    "__MAX_IN_SCOPE_CONNECTIONS__", str(MAX_IN_SCOPE_CONNECTIONS)
)


def _parse_multi_candidate_triage_response(text, num_candidates, max_connections):
    """Parses a multi-candidate triage call's raw response text
    (_MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION) into exactly one of:
      {"outcome": "general", "answer": <non-empty str>}
      {"outcome": "schema"}
      {"outcome": "help"}
      {"outcome": "sql", "indices": <non-empty list>, "message": <str|None>,
       "database_prompts": {int_index: non_empty_str, ...}}
      None  # unparseable, or doesn't fit any of the four shapes - caller retries
    Never raises. Mirrors _parse_single_dataset_triage_response's own
    "general"/"schema"/"help" handling exactly - this is genuinely the
    same parser with one more outcome ("sql") needing the indices/message/
    database_prompts validation this module's pre-merge triage_all_mode_
    question/_parse_triage_response used to do for its own "route"
    outcome. An "action": "sql" response whose indices are all invalid/
    out-of-range/empty after _clean_indices is treated as a parse failure
    for this attempt (None), not silently degraded to "general" or a
    phantom empty routing - the caller's bounded retry gets another chance
    instead.

    "database_prompts" is validated leniently, never as a reason to retry
    the whole attempt (see _clean_database_prompts) - a missing/malformed
    rewrite for one or every connection just means Phase B falls back to
    the user's own original question for that connection, not a failed
    triage attempt. The routing decision itself (which connections, and
    the user-facing "message") is still useful even when the model forgot
    or botched the per-connection rewrites."""
    parsed = _extract_json_object(text)
    if not isinstance(parsed, dict):
        return None

    action = parsed.get("action")
    action = action.strip().lower() if isinstance(action, str) else None

    if action == "general":
        answer = parsed.get("answer")
        if isinstance(answer, str) and answer.strip() and not is_label_only_response(answer):
            return {"outcome": "general", "answer": answer.strip()}
        # Either missing/empty, JUST the translated label with no real
        # answer after it, or missing the label/blank-line shape entirely -
        # the "general" outcome has no further step to fall back on
        # (unlike "message" below, which the caller already has a fallback
        # sentence for), so this is a parse failure like any other, giving
        # the bounded retry loop another attempt instead of showing the
        # user a bare label heading (or an un-labeled reply) with nothing
        # under it.
        return None

    if action in ("schema", "help"):
        return {"outcome": action}

    if action == "sql":
        indices = _clean_indices(parsed.get("indices"), num_candidates, max_connections)
        if not indices:
            return None
        message = parsed.get("message")
        # Checked on the RAW value, before the .strip() below collapses
        # a "label line, then a blank line, then nothing" response down
        # to just the label - is_label_only_response needs that blank
        # line intact to tell "just the label" apart from "a plain
        # single-line message with no label convention at all" (see its
        # docstring); stripping first would erase exactly the evidence it
        # depends on.
        message_is_label_only = isinstance(message, str) and is_label_only_response(message)
        message = message.strip() if isinstance(message, str) and message.strip() else None
        # A "message" that doesn't have a real body after its label line -
        # JUST the label, or missing the label/blank-line shape entirely -
        # is treated the same as a missing message - the caller
        # (translate_routes.py's stream_translation()) already builds a
        # translated-label fallback sentence for that case, so there's no
        # need to fail this whole attempt (and lose a valid routing
        # decision) over a message-only omission the way the "general"
        # outcome above must.
        if message is not None and message_is_label_only:
            message = None
        database_prompts = _clean_database_prompts(parsed.get("database_prompts"), indices)
        return {
            "outcome": "sql", "indices": indices, "message": message,
            "database_prompts": database_prompts,
        }

    return None


def _build_triage_question_prompt(prompt):
    """The ever-changing half of triage's prompt for BOTH single-dataset
    and multi-candidate triage - just the new prompt itself, now that the
    stable schema block (single-dataset mode's own overview text, or
    multi-candidate mode's _build_candidate_schema_block) is built (and
    placed) separately by the caller. One shared wording now, used
    regardless of candidate count."""
    return f"User Request: {prompt}\n\nJSON classification:"


def run_triage_call(num_candidates, schema_block, prompt, provider, client, model,
                     history=None, max_connections=MAX_IN_SCOPE_CONNECTIONS,
                     api_key=None, tried_keys=None, using_byok=False):
    """Unified triage call - Call 1 for BOTH single-dataset mode
    (translate_routes.py's triage_single_dataset_question, a thin wrapper
    around this function with num_candidates fixed at 1) and Phase A of
    "all databases"/group mode (called directly from translate_routes.py's
    router_only_group_mode branch, with num_candidates == the number of
    in-scope connections). Previously these were two separate
    implementations - _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION/_parse_
    single_dataset_triage_response/triage_single_dataset_question used to
    live in translate_routes.py, and _TRIAGE_SYSTEM_INSTRUCTION/_parse_
    triage_response/triage_all_mode_question here - genuinely duplicated
    retry-loop, key-rotation, and language-verification machinery,
    differing only in the prompt wording and in whether a "route"/"sql"
    outcome also needed to pick which connection(s) to query. This
    function merges them into ONE retry loop with exactly one behavioral
    branch, decided purely by `num_candidates`:
      num_candidates == 1: uses _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION
        and _parse_single_dataset_triage_response - byte-identical prompt
        wording and parsing to single-dataset mode's own pre-merge
        behavior, so this is a pure internal refactor for that caller, not
        a behavior change (see triage_single_dataset_question's own
        docstring: this keeps that function's entire existing test suite
        passing unmodified).
      num_candidates > 1: uses _MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION
        and _parse_multi_candidate_triage_response - the "sql" outcome
        additionally carries "indices"/"message"/"database_prompts", and
        "general"/"sql" carry the translated two-part label-line
        convention (see is_label_only_response's docstring) that only
        ever made sense once there was genuinely more than one candidate
        to explain a pick between. "schema"/"help" are NEW outcomes for
        this caller - group mode previously had no way to resolve to
        either; see translate_routes.py's router_only_group_mode branch
        for what it now does with them (opens the group's own Schema
        Viewer / Help modal, mirroring single-dataset mode's own handling
        of the same two outcomes).

    Returns exactly one of:
      {"outcome": "general", "answer": <str>, "usage": <dict|None>}
      {"outcome": "schema", "usage": <dict|None>}
      {"outcome": "help", "usage": <dict|None>}
      {"outcome": "sql", "usage": <dict|None>}  # num_candidates == 1 only
      {"outcome": "sql", "indices": [...], "message": <str|None>,
       "database_prompts": {int_index: str, ...},
       "usage": <dict|None>}  # num_candidates > 1 only
      {"outcome": "failed", "api_error": <bool>, "error": <exception|None>}

    `schema_block` is built by the CALLER using whichever convention its
    own mode already uses (single-dataset mode's own plain overview-text
    block via translate_routes.py's get_triage_schema_text, or multi-
    candidate mode's _build_candidate_schema_block) - this function only
    needs `num_candidates` itself (to bound/validate a "sql" outcome's
    "indices", and to pick which of the two prompts/parsers above
    applies), not the candidates' own contents, so unifying the two
    different schema-block-rendering conventions themselves was
    deliberately left out of this merge: a real difference in what each
    mode already fetches/caches for its own schema summary (single-
    dataset mode fetches a session-scoped shallow schema fresh, cheaply,
    even on a cold cache; multi-candidate mode reads only whatever's
    already cached from each connection's own deep-schema entry via
    db.build_router_candidate_summaries, deliberately never triggering a
    fetch of its own) - collapsing these into one shared representation
    would have meant picking one of those two caching behaviors for both
    modes, a real behavior change neither mode asked for.

    `history` (the session's ordinary, already-trimmed conversation turns)
    lets a follow-up question resolve a reference from the PRIOR triage
    turn, e.g. "which databases have sports data?" -> "Baseball (BigQuery)"
    -> "how large is THIS database?" - without it, every triage call is
    answered in total isolation and "this database"/"this dataset" is
    unresolvable. None (the default) is treated as no history at all.

    Bounded 2-attempt retry at getting a PARSEABLE, correctly-languaged
    response. An exception raised by the LLM call itself, within either of
    those 2 attempts, is retried using the same policy as every other LLM
    call in this app - provider.classify_error() (mirrors translate_
    routes.py's generate_sql_for_connection byte-for-byte): a 429/capacity
    error rotates to a different configured key and retries immediately
    (budget: one attempt per configured key, provider.get_key_pool_size());
    a transient 5xx/timeout waits TRANSLATION_RETRY_DELAY_SECONDS and
    retries the same key (budget: MAX_TRANSLATION_ATTEMPTS); a non-
    retryable error ends this call's attempt immediately, with no further
    retry at all.

    GENERATOR: every time either retry branch below actually fires, this
    yields a fully wire-encoded NDJSON progress line (`json.dumps({"status":
    "retrying", ...}) + "\\n"`), in the identical shape stream_
    translation()'s own inline single-connection retry loop and generate_
    sql_for_connection already emit. The caller forwards these live via
    `triage_result = yield from run_triage_call(...)` - client.js needs no
    changes to already show this: 'retrying' is handled generically by its
    existing dispatcher, regardless of which server-side call actually
    produced the line. A caller with no NDJSON stream to forward into
    (e.g. a unit test calling this function directly) drains it via
    _drain_generation below.

    The two failure reasons are distinguished via the "api_error" flag on
    a "failed" outcome:
      api_error=True: the LLM call's own retry budget (key rotation
        and/or transient-error retries) was used up, or it hit a non-
        retryable API error outright - a real technical/capacity
        problem, not a question-comprehension one. No fallback guess is
        made here either way: a wrong guess would mean actually running
        real SQL against a database the user never asked about (multi-
        candidate mode) or treating an ambiguous prompt as answerable
        when it might not be (single-dataset mode).
      api_error=False: every attempt got a real response back, but it
        was unparseable garbage both times - genuinely nothing more
        useful to try.
    A "failed" outcome also carries an "error" key: the raw exception the
    LLM call finally failed with when api_error=True, or None when
    api_error=False.

    `api_key`/`tried_keys` mirror generate_sql_for_connection's own
    parameters of the same name: both optional, defaulting to a freshly
    picked key / a fresh single-key set when omitted. `using_byok`, like
    generate_sql_for_connection's own parameter of the same name, forces
    the key-rotation budget down to exactly 1 (there's no second key of
    the user's own to rotate to)."""
    single = num_candidates == 1
    system_instruction = (
        _SINGLE_DATASET_TRIAGE_SYSTEM_INSTRUCTION if single
        else _MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION
    )

    # Mutable - a language-mismatch retry (see below) appends a correction
    # onto this exact string for the next attempt.
    question_prompt_content = _build_triage_question_prompt(prompt)

    # Computed once, up front, off the user's own prompt - see
    # language_detect.detect_language's own docstring. None (detection
    # unavailable or too low-confidence) disables the check below
    # entirely, same as every other call site that threads this through.
    expected_language_code = detect_language(prompt)

    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    last_error = None
    api_error = False
    for attempt in range(2):
        llm_input = provider.build_llm_input(history or [], schema_block, question_prompt_content)
        text = None
        transient_attempt = 1
        while True:
            try:
                text, usage = provider.call(client, model, llm_input, system_instruction)
                api_error = False
                break
            except Exception as e:
                last_error = e
                retry_action = provider.classify_error(e)
                if retry_action is None:
                    api_error = True
                    break

                if retry_action["rotate_key"]:
                    if len(tried_keys) >= key_pool_size:
                        api_error = True
                        break
                    next_key = provider.pick_api_key(exclude=tried_keys)
                    if next_key != api_key:
                        api_key = next_key
                        client = provider.make_client(api_key)
                    tried_keys.add(api_key)
                    logger.warning(
                        "Triage call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        len(tried_keys), key_pool_size, e,
                    )
                    # Told to the client before continuing, same as
                    # generate_sql_for_connection's identical line.
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": len(tried_keys),
                        "maxAttempts": key_pool_size,
                        "delaySeconds": 0,
                        "rotatedKey": True,
                    }) + "\n"
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    api_error = True
                    break
                logger.warning(
                    "Triage call failed (attempt %d/%d), retrying in %ds: %s",
                    transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e,
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
            # The LLM call's own retry budget is exhausted, or it hit a
            # non-retryable error outright - api_error is already True at
            # this point. No point spending the second unparseable-
            # response attempt on a call that's already just proven it
            # can't succeed right now.
            break

        parsed = (
            _parse_single_dataset_triage_response(text) if single
            else _parse_multi_candidate_triage_response(text, num_candidates, max_connections)
        )
        if parsed is not None:
            # Language verification - mirrors translate_routes.py's
            # _no_sql_language_mismatch for triage's own free text:
            # "answer" for "general", "message" for a multi-candidate
            # "sql" outcome ("database_prompts" is internal, per-
            # connection instructions the end user never sees - never
            # checked here). A "sql" outcome's "message" being None (the
            # model omitted it - the parser already tolerates that) has no
            # free text to check at all, so it's never flagged - the
            # caller already has its own server-built fallback sentence
            # for exactly that case. Single-dataset mode's own "sql"
            # outcome never carries a "message" at all.
            if parsed["outcome"] == "general":
                free_text = parsed["answer"]
            elif not single and parsed["outcome"] == "sql":
                free_text = parsed.get("message")
            else:
                free_text = None
            actual_language_code = None
            if free_text and expected_language_code is not None:
                detected = detect_language(free_text)
                if detected is not None and detected != expected_language_code:
                    actual_language_code = detected

            if actual_language_code is None:
                parsed["usage"] = usage
                return parsed

            expected_name = describe_language(expected_language_code)
            actual_name = describe_language(actual_language_code)
            if attempt + 1 < 2:
                logger.warning(
                    "Triage response came back in %s instead of the prompt's own %s "
                    "(attempt %d/2) - discarding, retrying with an explicit correction",
                    actual_name, expected_name, attempt + 1,
                )
                last_error = f"response was written in {actual_name} instead of {expected_name}"
                api_error = False
                # Same "name the mistake and the fix directly" shape as
                # _summarize_with_retry's/stream_translation()'s own
                # correction addendum - simply re-asking with the identical
                # prompt would likely just reproduce the same wrong-
                # language answer.
                question_prompt_content = (
                    f"{question_prompt_content}\n\nCORRECTION: your previous response to this exact "
                    f"prompt was written in {actual_name}, which is WRONG - the prompt was in "
                    f"{expected_name}, so your \"answer\"/\"message\" free text must be written entirely "
                    f"in {expected_name} this time (this applies only to that free text - \"indices\"/"
                    f"\"database_prompts\" are unaffected). Write your full response again, from "
                    f"scratch, entirely in {expected_name} this time."
                )
                continue
            # The one corrective retry is exhausted and the response STILL
            # came back in the wrong language - counts as an overall
            # triage failure (the caller's existing fixed apology text,
            # api_error=False) rather than silently returning text already
            # confirmed to be in the wrong language.
            logger.warning(
                "Triage response still came back in %s instead of %s after retrying - "
                "failing triage rather than serving a known-wrong-language response",
                actual_name, expected_name,
            )
            last_error = f"response was still written in {actual_name} instead of {expected_name} after retrying"
            api_error = False
            break
        last_error = f"unparseable triage response: {text!r}"
        api_error = False

    logger.warning("Triage failed after retry, no fallback: %s", last_error)
    return {
        "outcome": "failed",
        "api_error": api_error,
        "error": last_error if api_error else None,
    }


def _drain_generation(gen):
    """Runs a run_triage_call() generator to completion from a plain
    (non-streaming) context, discarding every yielded 'retrying' progress
    line and returning the final `return`ed result dict - identical in
    shape and purpose to translate_routes.py's own _drain_generation
    (which does the same thing for generate_sql_for_connection/
    summarize_all_mode_results/summarize_single_connection_results) - kept
    as a separate copy here rather than a shared import since translate_
    routes.py already imports FROM this module and the reverse import
    would be circular. Used by tests that call run_triage_call directly
    and want its plain result dict, not a generator object to iterate
    themselves."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value
