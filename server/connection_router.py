"""
connection_router.py

Phase A of "all databases" mode (see translate_routes.py's module
docstring for Phase B, the real SQL generation, and Phase C,
summarize_all_mode_results): triage_all_mode_question is a cheap, single
LLM call that decides whether a natural-language question can be
answered directly from the session's in-scope connections' names/dialects/
table names alone (see db.py's resolve_in_scope_descriptors), or genuinely
needs real data from one or more specific connections - and if so, which
ones - before any full, column-level schema is ever fetched or sent to
the model.

Only runs at all when a session's in_scope_mode is "all" -
translate_routes.py's single-connection path never imports or calls this
module, so an existing single-connection session's behavior/cost/latency
is completely unaffected by this feature's existence (see this module's
tests in tests/server/test_connection_router.py for the regression guard).

Deliberately reuses the SAME LlmProvider/client/model translate_routes.py
already built for the main SQL-generation call, rather than a separate
"router model" - see this module's docstring in the plan this implements
for why (picking connections and generating dialect-correct SQL are
different-difficulty tasks best kept as two calls, but there's no
standalone cheap model configured for the first one, so it just borrows
whichever provider/model the session is already using). This means
triage_all_mode_question is a second call against the same client/API
key translate_routes.py already picked for the main generation call -
though it DOES run its own retry loop against that key pool (key
rotation on a 429, wait-and-retry on a transient 5xx/timeout, see
triage_all_mode_question's docstring) rather than deferring retry
entirely to the caller: a bare "catch everything, retry the same key
twice, give up" loop used to live here instead, which meant a capacity/
rate-limit failure was retried uselessly (same key, doomed to fail the
same way again) and then reported back indistinguishably from "the model
gave an unparseable response" - see translate_routes.py's
format_llm_error_for_user for the user-facing half of that fix.
"""

import json
import re
import time

from app_config import logger, MAX_IN_SCOPE_CONNECTIONS, MAX_TRANSLATION_ATTEMPTS, TRANSLATION_RETRY_DELAY_SECONDS

# How many of a session's in-scope connections a single question's Phase A
# routing may ever select at once - the same MAX_IN_SCOPE_CONNECTIONS cap
# config_routes.py applies to how many a user may mark in scope AT ALL
# (see its docstring in app_config.py). These used to be two independent
# constants (this module previously had its own, smaller
# MAX_DATABASES_PER_QUERY, defaulting to 5) - now there is exactly one
# "how many databases" knob, used everywhere the concept comes up.

def _build_candidate_prompt(candidate_summaries, user_question):
    lines = ["Candidate database connections:"]
    for i, c in enumerate(candidate_summaries):
        table_names = c.get("table_names") or []
        shown = ", ".join(table_names) if table_names else "(no tables discovered)"
        lines.append(
            f"[{i}] name={c.get('name')!r} dialect={c.get('dialect')!r} tables={shown}"
        )
    lines.append("")
    lines.append(f"User question: {user_question}")
    lines.append("")
    lines.append("JSON array of relevant candidate indices:")
    return "\n".join(lines)


def _strip_markdown_fence(text):
    """Strips a leading/trailing markdown code fence (```/```json/etc.)
    from `text` if present, tolerating models that wrap their JSON despite
    being told not to. Returns the (possibly unchanged) stripped string.
    Used by _parse_triage_response so this tolerance only needs to be
    right in one place."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _clean_indices(raw_list, num_candidates, max_connections):
    """Dedupe/range-check/cap a raw (untrusted, model-supplied) list of
    candidate indices, preserving the model's own ranking (first
    occurrence kept, in the order given). Never raises - non-coercible
    items are silently skipped, out-of-range/duplicate indices are
    silently dropped, and the result is capped at `max_connections` (stops
    appending once reached, rather than truncating a longer valid list
    from the end). Returns [] if nothing survives - used by
    _parse_triage_response so this validation only needs to be right in
    one place."""
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


# =============================================================================
# "All databases" mode triage: decides "answer" (table names/dialects alone
# are enough - no real data access needed), "route" (generate and execute
# real SQL against one or more specific connections), or "failed" (the LLM
# call never produced anything usable, even after a bounded retry). On
# total failure this returns "failed" rather than guessing a connection: a
# wrong guess here would mean silently running real SQL against a database
# the user never actually asked about, which is a materially worse failure
# mode than a routing mistake would be for a read-only pick.
# =============================================================================

_TRIAGE_SYSTEM_INSTRUCTION = (
    "You are a triage assistant for a natural-language-to-SQL app with more "
    "than one database connection configured. You are given a list of "
    "candidate database connections (each with a name, its SQL dialect, and "
    "a sample of its table/tab names - NOT full column-level schema, and NOT "
    "any actual data/rows) and a user's natural-language question. Decide "
    "exactly one of two things:\n"
    "1. \"answer\" - you can respond directly, using ONLY the connection "
    "list above (names, dialects, table names) and your own general "
    "knowledge, with NO need to look at any actual data/rows in any "
    "database. Use this for questions like \"how many databases do I "
    "have\", \"which database looks like it has sports data\", \"what "
    "tables does the Sales database have\", or a general-knowledge "
    "question unrelated to any database at all.\n"
    "2. \"route\" - fulfilling the request requires actually operating on "
    "real data in one or more of these connections - reading it (e.g. \"how "
    "many customers do we have\", \"what were last month's top products\") "
    "or adding/changing/removing it via INSERT/UPDATE/DELETE/DDL (e.g. "
    "\"add a new customer\", \"delete last month's test orders\", \"create "
    "a table for...\") - whichever the connection's database and the "
    "connected user's own permissions allow; you are not restricted to "
    "read-only questions. Pick which connection(s) are relevant, "
    "most-relevant first (most questions need exactly ONE; only "
    "include more than one when the question genuinely needs data from more "
    "than one - there is no cross-database join, each is queried "
    "independently). Never pick more than "
    f"{MAX_IN_SCOPE_CONNECTIONS} connections.\n"
    "For \"route\", ALSO rewrite the user's own question into a separate, "
    "self-contained instruction for EACH connection you picked, in "
    "\"database_prompts\" (a JSON object keyed by that connection's index as "
    "a string). Each connection is queried completely independently, by a "
    "SEPARATE call that only ever sees that ONE connection's own schema - it "
    "never sees the original question, the other connection(s), or this "
    "triage step at all - so the original wording is frequently wrong once "
    "narrowed to just one connection: a question phrased across multiple "
    "databases (\"give me data from 2 tables each from a different "
    "database\", \"compare X in database A against Y in database B\") must "
    "become a plain, single-database request for EACH one (e.g. \"give me "
    "data from one table in this database\" for each; \"how many X\" / \"how "
    "many Y\" split apart, one per relevant connection) - never an "
    "instruction that itself still mentions needing more than one database, "
    "since the connection being asked has no way to fulfill that. A question "
    "that was already naturally single-database in scope (even when routed "
    "to just one connection) can be rewritten as the same question, only "
    "reworded if needed to drop any reference to picking/identifying WHICH "
    "database (already decided here, not that connection's job to re-decide) "
    "- e.g. \"how large is this database\" -> \"how large is this "
    "database?\" is fine verbatim once only one connection is being asked. "
    "Every index you put in \"indices\" needs its own entry here - do not "
    "omit any.\n"
    "Both \"answer\" and \"message\" below (never \"database_prompts\" - those "
    "are internal, per-connection instructions the end user never sees) have "
    "two parts. FIRST, a label line: a short (one to two word) section-"
    "heading label meaning \"Triage\" - in English this label is literally "
    "the single word \"Triage\", but you must instead write it TRANSLATED "
    "into the SAME LANGUAGE as the user's own question below, with nothing "
    "else on that line, followed by a blank line. SECOND, immediately after "
    "that blank line, your actual response text, ALSO written in that same "
    "language - e.g., if the question was in English: \"Triage\\n\\nChecking "
    "Sales Postgres, since it has an orders table and the question asks "
    "about recent purchases.\" Never stop after the label - the label by "
    "itself, with nothing following it, is not a valid \"answer\"/\"message\" "
    "value; the label is a UI section heading prepended to your response, "
    "not a substitute for writing one. The label itself is plain text with "
    "no markdown emphasis of your own around it.\n"
    "Respond with ONLY a JSON object, no markdown fences, no other text:\n"
    "- For outcome 1: {\"action\": \"answer\", \"answer\": \"<your direct "
    "response, written for the end user in the same language as their "
    "question, plain text, starting with the translated label line "
    "described above>\"}\n"
    "- For outcome 2: {\"action\": \"route\", \"indices\": [...0-based "
    "candidate indices, most-relevant first...], \"message\": \"<starting "
    "with the translated label line described above, then one to two "
    "short sentences, for the end user, in the same language as their "
    "question: name the real database name(s) "
    "you're about to check, AND briefly explain WHY - what in the question "
    "and/or in those databases' table names made them the relevant pick, "
    "e.g. (for an English question) 'Triage\\n\\nChecking Sales Postgres, "
    "since it has an orders table "
    "and the question asks about recent purchases.' A message whose second "
    "part only names the database(s) with no reason is NOT acceptable - "
    "always include the brief why.>\", \"database_prompts\": {\"<index as a "
    "string>\": \"<that connection's own self-contained rewritten "
    "question - plain text, no \"Triage\" line, this one is never shown to "
    "the end user>\", ...one entry per index in \"indices\"...}}\n"
    "Refer to a connection ONLY by its real 'name' field, never by its "
    "candidate index/bracket number (the '[0]', '[1]', etc. above is an "
    "internal ordinal for this prompt only, meaningless to the user and "
    "never to be repeated back to them) or any other internal id/label - "
    "this applies inside \"database_prompts\" too: a rewritten question may "
    "reference the target database by its real name if useful context, but "
    "never by index/bracket number. If you genuinely cannot classify the "
    "question at all, prefer \"route\" with your single best guess over "
    "inventing a third response shape."
)


def _clean_database_prompts(raw, valid_indices):
    """Validates a "route" response's untrusted "database_prompts" value
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
    \\n\\n<body>" shape both _TRIAGE_SYSTEM_INSTRUCTION here and
    translate_routes.py's _SUMMARY_SYSTEM_INSTRUCTION ask the model for (a
    short section-heading label, written in the SAME LANGUAGE as the
    user's own question - see those two prompts - followed by a blank
    line, then the real response): the model wrote a leading line,
    a blank line, and then stopped, leaving nothing (or only whitespace)
    as the body. Also true for a genuinely empty/whitespace-only `text`.

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

    Used by triage_all_mode_question here and by translate_routes.py's
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


def _parse_triage_response(text, num_candidates, max_connections):
    """Parses the triage call's raw response text into exactly one of:
      {"outcome": "answer", "answer": <non-empty str>}
      {"outcome": "route", "indices": <non-empty list>, "message": <str|None>,
       "database_prompts": {int_index: non_empty_str, ...}}
      None  # unparseable, or doesn't fit either shape - caller retries
    Never raises. Does NOT tolerate a bare JSON array or an object without
    an "action" key - "action"/"route"/"answer" is this prompt's own
    contract, not something to guess around. An "action": "route"
    response whose indices are all invalid/out-of-range/empty after
    _clean_indices is treated as a parse failure for this attempt (None),
    not silently degraded to "answer" or a phantom empty routing - the
    caller's bounded retry gets another chance instead.

    "database_prompts" is validated leniently, never as a reason to retry
    the whole attempt (see _clean_database_prompts) - a missing/malformed
    rewrite for one or every connection just means Phase B falls back to
    the user's own original question for that connection (today's
    original behavior, before this field existed), not a failed triage
    attempt. The routing decision itself (which connections, and the
    user-facing "message") is still useful even when the model forgot or
    botched the per-connection rewrites."""
    if not text:
        return None
    cleaned = _strip_markdown_fence(text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    action = parsed.get("action")
    action = action.strip().lower() if isinstance(action, str) else None

    if action == "answer":
        answer = parsed.get("answer")
        if isinstance(answer, str) and answer.strip() and not is_label_only_response(answer):
            return {"outcome": "answer", "answer": answer.strip()}
        # Either missing/empty, JUST the translated label with no real
        # answer after it, or missing the label/blank-line shape entirely -
        # the "answer" outcome has no further step to fall back on (unlike
        # "message" below, which the caller already has a fallback sentence
        # for), so this is a parse failure like any other, giving the
        # bounded retry loop another attempt instead of showing the user a
        # bare label heading (or an un-labeled reply) with nothing under it.
        return None

    if action == "route":
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
        # decision) over a message-only omission the way the "answer"
        # outcome above must.
        if message is not None and message_is_label_only:
            message = None
        database_prompts = _clean_database_prompts(parsed.get("database_prompts"), indices)
        return {
            "outcome": "route", "indices": indices, "message": message,
            "database_prompts": database_prompts,
        }

    return None


def triage_all_mode_question(candidate_summaries, user_question, provider, client, model,
                              history=None, max_connections=MAX_IN_SCOPE_CONNECTIONS,
                              api_key=None, tried_keys=None, using_byok=False):
    """"All databases" mode's first call: decides whether `user_question`
    can be answered directly from `candidate_summaries` alone (names,
    dialects, table names - no real data access), or genuinely needs real
    data from one or more specific connections. Returns exactly one of:
      {"outcome": "answer", "answer": <str>, "usage": <dict|None>}
      {"outcome": "route", "indices": [...], "message": <str|None>,
       "database_prompts": {int_index: str, ...}, "usage": <dict|None>}
      {"outcome": "failed", "api_error": <bool>}

    "route"'s "database_prompts" is the model's own rewrite of
    `user_question` into a separate, self-contained instruction per
    selected connection (see _TRIAGE_SYSTEM_INSTRUCTION) - necessary
    because Phase B's per-connection calls (translate_routes.py's
    _run_phase_b_fanout) are each fully independent and only ever see ONE
    connection's own schema, never the original question's full framing
    or any other connection. Passing the verbatim original question
    through unchanged breaks down the moment it was phrased across
    multiple databases at once (e.g. "give me data from 2 tables each
    from a different database") - each individual connection has no way
    to fulfill an instruction that still talks about needing more than
    one database, and fails outright. Keyed by index (not positionally
    parallel to "indices") specifically so it can never desynchronize
    from whichever indices survive _clean_indices' own deduping/capping -
    see _clean_database_prompts. Missing an entry for some (or every)
    selected index is tolerated, not a parse failure - the caller
    (_run_phase_b_fanout) falls back to the original `user_question` for
    any connection with no rewrite, exactly today's pre-existing
    behavior.

    `history` (the session's ordinary, already-trimmed conversation turns -
    same shape/list stream_translation() already builds for the single-
    connection path, see translate_routes.py's `history` variable) lets a
    follow-up question resolve a reference from the PRIOR triage turn, e.g.
    "which databases have sports data?" -> "Baseball (BigQuery)" -> "how
    large is THIS database?" - without it, every triage call is answered in
    total isolation and "this database" is unresolvable. This is
    deliberately just the ordinary shared history, NOT the "different
    history per database" case that's still out of scope: this call is a
    single, non-per-database step (it only ever sees table names, never any
    one connection's real data), so there's exactly one conversation thread
    for it to consult, unlike Phase B's per-connection calls (see
    translate_routes.py's _run_phase_b_fanout, which deliberately still
    passes empty history to each - threading distinct per-database history
    through THOSE remains the deferred, genuinely complex follow-up work).
    None (the default) is treated as no history at all - today's original
    behavior, unchanged for any caller that doesn't pass it.

    Bounded 2-attempt retry at getting a PARSEABLE response - unchanged
    from before this docstring paragraph was updated. What DID change: an
    exception raised by the LLM call itself, within either of those 2
    attempts, is no longer treated identically to "the model replied with
    unparseable text." It's now retried using the exact same policy as
    every other LLM call in this app - provider.classify_error() (see
    translate_routes.py's generate_sql_for_connection, which the retry
    loop below mirrors byte-for-byte): a 429/capacity error rotates to a
    different configured key and retries immediately (budget: one attempt
    per configured key, provider.get_key_pool_size()); a transient
    5xx/timeout waits TRANSLATION_RETRY_DELAY_SECONDS and retries the same
    key (budget: MAX_TRANSLATION_ATTEMPTS); a non-retryable error ends
    this call's attempt immediately, with no further retry at all.
    Previously this loop caught EVERY exception the same way, never
    rotated keys, and reported the exact same generic {"outcome":
    "failed"} whether the LLM call itself failed (e.g. every configured
    Gemini key was out of capacity) or the model just replied with
    something unparseable - masking a resource-exhaustion/API condition
    as if the model had simply been unable to understand the question.
    The two are now distinguished via the "api_error" flag on a "failed"
    outcome:
      api_error=True: the LLM call's own retry budget (key rotation
        and/or transient-error retries) was used up, or it hit a non-
        retryable API error outright - a real technical/capacity
        problem, not a question-comprehension one. No fallback guess at
        some candidate connection is made here either way: a wrong guess
        would mean actually running real SQL against a database the user
        never asked about.
      api_error=False: every attempt got a real response back, but it
        was unparseable garbage both times - genuinely nothing more
        useful to try.
    A "failed" outcome also carries an "error" key: the raw exception the
    LLM call finally failed with when api_error=True (guaranteed to be an
    actual exception instance in that case - see the loop below, which
    only ever sets api_error=True in the same breath as capturing the
    exception that triggered it), or None when api_error=False (there is
    no exception to report - the model responded twice, just not usably).
    The caller (translate_routes.py's router_only_all_mode branch) shows
    a different, honest message for the api_error=True case - built by
    format_llm_error_for_user() there from this result's "error" key (the
    raw exception the LLM call finally failed with - see that key's own
    docstring just below) - instead of its fixed "I am not able to
    respond to your prompt" apology (_TRIAGE_FAILURE_TEXT), which is
    reserved for the genuinely-unparseable case.

    `api_key`/`tried_keys` mirror generate_sql_for_connection's own
    parameters of the same name: both optional, defaulting to a freshly
    picked key / a fresh single-key set when omitted (same "explicit, not
    closed-over" reasoning applies as that function's docstring, even
    though this call never runs in parallel across threads the way Phase
    B's fan-out does - keeping the same shape avoids a third, subtly
    different convention for the same idea). The caller
    (translate_routes.py) passes its own already-picked `api_key` as the
    starting point, so this doesn't burn a different key than the rest of
    the request for no reason unless a rotation is actually needed.

    `using_byok`, like generate_sql_for_connection's own parameter of the
    same name, forces the key-rotation budget down to exactly 1 (there's
    no second key of the user's own to rotate to). This function never
    calls format_llm_error_for_user() itself (it returns the raw
    exception via "error" instead - see above), so unlike that function
    it has nothing else to do with the flag."""
    llm_input = provider.build_llm_input(history or [], "", _build_candidate_prompt(candidate_summaries, user_question))

    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    last_error = None
    api_error = False
    for attempt in range(2):
        text = None
        transient_attempt = 1
        while True:
            try:
                text, usage = provider.call(client, model, llm_input, _TRIAGE_SYSTEM_INSTRUCTION)
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
                        "Connection triage call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        len(tried_keys), key_pool_size, e,
                    )
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    api_error = True
                    break
                logger.warning(
                    "Connection triage call failed (attempt %d/%d), retrying in %ds: %s",
                    transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e,
                )
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

        parsed = _parse_triage_response(text, len(candidate_summaries), max_connections)
        if parsed is not None:
            parsed["usage"] = usage
            return parsed
        last_error = f"unparseable triage response: {text!r}"
        api_error = False

    logger.warning("Connection triage (Phase A2, all-mode) failed after retry, no fallback: %s", last_error)
    return {
        "outcome": "failed",
        "api_error": api_error,
        "error": last_error if api_error else None,
    }
