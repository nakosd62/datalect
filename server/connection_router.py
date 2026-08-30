"""
connection_router.py

Phase A of the multi-database question-answering feature (see
translate_routes.py's module docstring for Phase B, the real SQL
generation): a cheap, single LLM call that picks WHICH of a session's
in-scope database connections (see db.py's resolve_in_scope_descriptors)
a natural-language question is actually about, before any full,
column-level schema is ever fetched or sent to the model.

Only runs at all when a session has 2+ connections in scope -
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
select_relevant_connections is a second call against the same client - it
does NOT construct its own client or manage its own API key/retry pool;
that's translate_routes.py's job, same as the main generation call.
"""

import json

from app_config import logger, MAX_IN_SCOPE_CONNECTIONS

# How many of a session's in-scope connections a single question's Phase A
# routing may ever select at once - the same MAX_IN_SCOPE_CONNECTIONS cap
# config_routes.py applies to how many a user may mark in scope AT ALL
# (see its docstring in app_config.py). These used to be two independent
# constants (this module previously had its own, smaller
# MAX_DATABASES_PER_QUERY, defaulting to 5) - now there is exactly one
# "how many databases" knob, used everywhere the concept comes up.

_ROUTER_SYSTEM_INSTRUCTION = (
    "You are a routing assistant for a natural-language-to-SQL app that has "
    "more than one database connection configured. You are given a list of "
    "candidate database connections (each with a name, its SQL dialect, and "
    "a sample of its table/tab names) and a user's natural-language "
    "question. Your ONLY job is to decide which of these connections the "
    "question is actually about - you do NOT write any SQL.\n"
    "Respond with ONLY a JSON object of the form "
    '{"indices": [...], "reasoning": "..."}. "indices" is a JSON array of '
    "the candidate indices (0-based, matching the order they were given to "
    "you) that are relevant to the question, ordered from most to least "
    "relevant - e.g. [0] for a question about only the first candidate, or "
    "[2, 0] if it plausibly needs both the third and first candidates, "
    "most-relevant first. Most questions are about exactly ONE connection - "
    "only include more than one when the question genuinely appears to "
    "need data from more than one of them (there is no cross-database "
    "join; each connection can only ever be queried independently). Never "
    f"include more than {MAX_IN_SCOPE_CONNECTIONS} indices. If you are "
    "unsure, include your single best guess rather than an empty array. "
    '"reasoning" is a short (one or two sentence) plain-English '
    "explanation, written for the end user asking the question, of why you "
    "picked the connection(s) you picked - e.g. \"This looks like a "
    "question about customer orders, which lives in the Sales Postgres "
    "database.\" Refer to a connection ONLY by its real name (the 'name' "
    "field above) - never by its candidate index/bracket number (the "
    "'[0]', '[1]', etc. above is an internal ordinal for this prompt only, "
    "meaningless to the user and never to be repeated back to them) or any "
    "other internal id/label. Return ONLY the JSON object - no other text, "
    "no markdown code fences."
)


def _build_router_prompt(candidate_summaries, user_question):
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
    Shared by both _parse_router_response and _parse_triage_response so
    this tolerance only needs to be right in one place."""
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
    from the end). Returns [] if nothing survives - shared by both
    _parse_router_response and _parse_triage_response so this validation
    only needs to be right in one place."""
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


def _parse_router_response(text, num_candidates, max_connections):
    """Parses Phase A's raw response text into a (indices, reasoning) pair:
    `indices` a deduped, in-range, length-capped list of candidate indices,
    in the order the model returned them (its own ranking, most-relevant
    first); `reasoning` the model's own short explanation string, or None
    if it didn't provide one (tolerated, not required - see below).
    Tolerant of a markdown code fence around the JSON (some models wrap it
    despite being told not to), of the whole response being a bare JSON
    array rather than the requested {"indices": [...], "reasoning": "..."}
    object (some models/older prompts still just emit the array - kept
    working rather than treated as a parse failure), and of the indices
    array being nested one level under some other single key. Never
    raises; returns (None, None) on anything that isn't recoverably a list
    of indices, so the caller can fall back."""
    if not text:
        return None, None
    cleaned = _strip_markdown_fence(text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None, None

    reasoning = None
    if isinstance(parsed, dict):
        raw_reasoning = parsed.get("reasoning")
        if isinstance(raw_reasoning, str) and raw_reasoning.strip():
            reasoning = raw_reasoning.strip()
        # Tolerate a model wrapping the array in an object under some other
        # key too, e.g. {"relevant": [...]} with no "indices" key at all -
        # take the first list value found rather than requiring one exact
        # key name.
        list_value = parsed.get("indices")
        if not isinstance(list_value, list):
            list_value = next((v for v in parsed.values() if isinstance(v, list)), None)
        parsed = list_value
    if not isinstance(parsed, list):
        return None, None

    indices = _clean_indices(parsed, num_candidates, max_connections)
    if not indices:
        return None, None
    return indices, reasoning


def select_relevant_connections(candidate_summaries, user_question, provider, client, model,
                                 max_connections=MAX_IN_SCOPE_CONNECTIONS):
    """Phase A: picks which of `candidate_summaries` (see
    db.py's build_router_candidate_summaries - one {"name", "dialect",
    "table_names"} dict per in-scope connection, in a fixed order) are
    relevant to `user_question`. Returns a dict:
    {"indices": [...], "reasoning": <str|None>, "usage": <dict|None>}.
    "indices" is a list into `candidate_summaries`, most-relevant first,
    length 1..max_connections - NEVER empty. "reasoning" is the model's own
    short natural-language explanation for its pick, or None if the model
    didn't provide one (a fallback pick, or a response that only contained
    a bare indices array). "usage" is whatever token-usage dict
    `provider.call()` returned for the attempt that actually produced the
    indices (None on total failure, since no billable call succeeded).

    Never raises: any failure (a bad/unparseable response, or the LLM call
    itself raising) falls back to indices=[0], reasoning=None after one
    bounded retry, so a router hiccup degrades to "just use the first
    in-scope connection" rather than failing the whole request or
    introducing multi-statement complexity nobody asked for.

    Reuses the caller's already-built `provider`/`client`/`model` (see this
    module's docstring for why) via the exact same build_llm_input()/call()
    dispatch translate_routes.py's main generation call already goes
    through - this is a second, independent call against that client, not
    a special-cased one-off request shape.

    Deliberately has NO knowledge of translate_routes.py's transient-error
    retry loop (MAX_TRANSLATION_ATTEMPTS/rotate-key) - a Phase A failure
    just gets ONE bounded retry of its own (see attempts below) rather than
    plugging into that shared budget, since a router mistake is cheap to
    fall back from (worse relevance, not a failed request) while the real
    generation call's failures are not."""
    llm_input = provider.build_llm_input([], "", _build_router_prompt(candidate_summaries, user_question))

    last_error = None
    for attempt in range(2):
        try:
            text, usage = provider.call(client, model, llm_input, _ROUTER_SYSTEM_INSTRUCTION)
        except Exception as e:
            last_error = e
            continue
        indices, reasoning = _parse_router_response(text, len(candidate_summaries), max_connections)
        if indices:
            return {"indices": indices, "reasoning": reasoning, "usage": usage}
        last_error = f"unparseable router response: {text!r}"

    logger.warning("Connection router Phase A failed after retry, falling back to candidate 0: %s", last_error)
    return {"indices": [0], "reasoning": None, "usage": None}


# =============================================================================
# "All databases" mode triage - a real two-phase design, not the Phase-A-only
# advisory stub this replaces (see translate_routes.py's router_only_all_mode
# branch). Deliberately a SEPARATE function/prompt/parser from
# select_relevant_connections above, not a variant of it: the two have
# different failure contracts (a Phase A routing mistake for the LEGACY
# arbitrary-multi-select path just picks a worse-than-ideal connection to
# generate real SQL against anyway - cheap to shrug off - while a triage
# mistake here would mean silently running real SQL against a database the
# user never actually asked about, so on total failure this returns "failed"
# rather than select_relevant_connections' guess-candidate-0 fallback) and the
# legacy path's existing tests assert its guess-candidate-0 behavior stays
# exactly as-is - this new function must never change that.
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
    "2. \"route\" - answering requires looking at actual data (rows) in one "
    "or more of these connections - e.g. \"how many customers do we have\", "
    "\"what were last month's top products\". Pick which connection(s) are "
    "relevant, most-relevant first (most questions need exactly ONE; only "
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
    "Respond with ONLY a JSON object, no markdown fences, no other text:\n"
    "- For outcome 1: {\"action\": \"answer\", \"answer\": \"<your direct "
    "response, written for the end user, plain text>\"}\n"
    "- For outcome 2: {\"action\": \"route\", \"indices\": [...0-based "
    "candidate indices, most-relevant first...], \"message\": \"<one short "
    "sentence, for the end user, naming which real database name(s) you're "
    "about to check and why>\", \"database_prompts\": {\"<index as a "
    "string>\": \"<that connection's own self-contained rewritten "
    "question>\", ...one entry per index in \"indices\"...}}\n"
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


def _parse_triage_response(text, num_candidates, max_connections):
    """Parses the triage call's raw response text into exactly one of:
      {"outcome": "answer", "answer": <non-empty str>}
      {"outcome": "route", "indices": <non-empty list>, "message": <str|None>,
       "database_prompts": {int_index: non_empty_str, ...}}
      None  # unparseable, or doesn't fit either shape - caller retries
    Never raises. Unlike _parse_router_response, does NOT tolerate a bare
    JSON array or an object without an "action" key - this is a brand-new
    prompt contract with no back-compat burden. An "action": "route"
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
        if isinstance(answer, str) and answer.strip():
            return {"outcome": "answer", "answer": answer.strip()}
        return None

    if action == "route":
        indices = _clean_indices(parsed.get("indices"), num_candidates, max_connections)
        if not indices:
            return None
        message = parsed.get("message")
        message = message.strip() if isinstance(message, str) and message.strip() else None
        database_prompts = _clean_database_prompts(parsed.get("database_prompts"), indices)
        return {
            "outcome": "route", "indices": indices, "message": message,
            "database_prompts": database_prompts,
        }

    return None


def triage_all_mode_question(candidate_summaries, user_question, provider, client, model,
                              history=None, max_connections=MAX_IN_SCOPE_CONNECTIONS):
    """"All databases" mode's first call: decides whether `user_question`
    can be answered directly from `candidate_summaries` alone (names,
    dialects, table names - no real data access), or genuinely needs real
    data from one or more specific connections. Returns exactly one of:
      {"outcome": "answer", "answer": <str>, "usage": <dict|None>}
      {"outcome": "route", "indices": [...], "message": <str|None>,
       "database_prompts": {int_index: str, ...}, "usage": <dict|None>}
      {"outcome": "failed"}

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

    Bounded 2-attempt retry, same shape as select_relevant_connections
    above. On total failure (every attempt either raised or produced an
    unparseable response) returns {"outcome": "failed"} - deliberately NOT
    a candidate-0 fallback like select_relevant_connections: a wrong guess
    here means actually generating and running real SQL against a database
    the user never asked about, so the caller must show a fixed apology
    instead of silently picking one (see translate_routes.py's
    router_only_all_mode branch, which maps "failed" to the app's existing
    '*** NO SQL *** I am not able to respond to your prompt.' text)."""
    llm_input = provider.build_llm_input(history or [], "", _build_router_prompt(candidate_summaries, user_question))

    last_error = None
    for attempt in range(2):
        try:
            text, usage = provider.call(client, model, llm_input, _TRIAGE_SYSTEM_INSTRUCTION)
        except Exception as e:
            last_error = e
            continue
        parsed = _parse_triage_response(text, len(candidate_summaries), max_connections)
        if parsed is not None:
            parsed["usage"] = usage
            return parsed
        last_error = f"unparseable triage response: {text!r}"

    logger.warning("Connection triage (Phase A2, all-mode) failed after retry, no fallback: %s", last_error)
    return {"outcome": "failed"}
