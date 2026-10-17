"""
semi_join_routes.py

Cross-dataset semi-join / filter pushdown's own new endpoint - see the
project design doc (cross-dataset-semi-join-filter-pushdown-design.md) and
connection_router.py's "semi_join" triage field for the full picture.

Where this sits in the turn: triage (connection_router.py's
_MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION) can flag that one picked
connection's result ("driving") should be used to FILTER another picked
connection's own query ("dependent") before that query ever runs - e.g.
"GDP of each country that hosted the Olympics" needs the real (country,
year) host pairs from one connection before the GDP connection's own query
is even written, not just related to it afterward (that's the EXISTING
combine step - combine_routes.py - a different, narrower tool for a
different shape: both sides independently bounded, related only for
display, not one side used to constrain the other's own live query).

translate_routes.py's router_only_group_mode branch, upon seeing
"semi_join" in triage's result, excludes the dependent connection from
that turn's ordinary Phase B fan-out (_run_phase_b_fanout) entirely - no
SQL is generated for it yet. Once the client has executed the driving
connection's own SQL (via the existing /api/execute) and has its real
rows in hand, it calls this endpoint to generate the dependent
connection's SQL - the real driving rows become a "filter context" block
threaded into that ONE connection's own Phase B prompt (see
generate_sql_for_connection's own `filter_context` parameter and
sql_generation_format_rules.txt's own "Filter context" paragraph). The
client then executes the returned SQL exactly like any other connection's
SQL - this endpoint never touches a live database connection itself, it
only ever generates text.

Deliberately NOT folded into /api/translate's own router_only_group_mode
branch, for the same reason /api/combine-results is its own endpoint
rather than folded into /api/execute: this step can only run once the
driving connection's own execute has already completed elsewhere in the
turn, which is a separate HTTP round-trip the client orchestrates, not
something a single streaming /api/translate call could do internally
(Phase B's SQL generation and /api/execute's actual execution have always
been two separate phases/endpoints in this app - see execute_routes.py's
own module docstring).
"""
import json

from flask import Blueprint, request, jsonify, Response, stream_with_context

from app_config import logger, state_store, SEMI_JOIN_MAX_FILTER_ROWS
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie, is_superuser
from db import resolve_descriptor_by_reference, record_translation
import cancel_registry
from concurrency_guard import TRANSLATE_GUARD, busy_response
from rate_limiter import translate_rate_limit
from llm_providers import get_llm_provider, _default_fleet_provider, LlmCallFailed
from sql_generation import generate_sql_for_connection, _classify_generation_outcome

semi_join_bp = Blueprint('semi_join', __name__)

# How many of a session's HISTORY_MAX_TURNS-bounded turns this endpoint
# will accept for the dependent connection's own history - mirrors
# translate_routes.py's own HISTORY_MAX_TURNS re-slice (see that module's
# `entry_histories` comment: a per-connection bucket is capped client-side
# too, but every server call that accepts a history list re-slices it
# anyway as a defensive belt-and-suspenders bound). Imported lazily inside
# the route (rather than at module load) to avoid a circular import -
# translate_routes.py already imports FROM sql_generation.py and
# connection_router.py, and this module sits beside both, not above them.


def _build_filter_context(filter_description, columns, rows, max_rows=SEMI_JOIN_MAX_FILTER_ROWS):
    """Renders the driving connection's own real, already-executed rows
    into the plain-text "filter context" block generate_sql_for_connection's
    `filter_context` parameter expects - real data standing in for the
    place a general-knowledge literal would otherwise go (see that
    function's own docstring, and sql_generation_format_rules.txt's
    "Filter context" paragraph).

    Returns (text, row_count, capped):
      text: the rendered block to pass as `filter_context`, or None when
        `capped` is True (see below) - the caller is expected to fall back
        to generating this connection's SQL with NO filter_context at all
        in that case (same as if semi_join had never been detected for
        this turn), surfacing an honest caveat to the end user rather than
        silently building a filter from a truncated sample.
      row_count: the number of DISTINCT rows actually found (after
        deduping - the driving connection's own real rows may repeat the
        same filter-relevant values across columns this function doesn't
        know aren't part of the filter, e.g. a `population` column riding
        along with `country`/`year` - deduping on the FULL row, not a
        guessed subset of "the" key columns, is the only dedup that's
        always safe without knowing which columns matter).
      capped: True iff the distinct row count exceeds `max_rows` - this is
        the ONE failure mode this function has; every other input
        (including zero rows) renders successfully, since "the driving
        side returned nothing" and "the driving side returned too much"
        both deserve very different handling (see each branch's own
        comment below), neither of which is a reason to fail outright.

    `columns` fixes a stable per-row key order for rendering (the exact
    order the driving connection's own real execute result reported) -
    falls back to each row dict's own insertion order when empty/missing,
    which is still stable (ordinary Python dict iteration) even though
    it's not guaranteed to match the driving query's own SELECT list in
    that fallback case."""
    deduped = []
    seen = set()
    for row in rows or []:
        keys = columns if columns else list((row or {}).keys())
        pairs = tuple((k, (row or {}).get(k)) for k in keys)
        if pairs in seen:
            continue
        seen.add(pairs)
        deduped.append(pairs)

    if not deduped:
        # The driving connection's own query genuinely matched nothing -
        # this is itself real information, not an absence of it: the
        # dependent connection's own answer should correctly be empty too,
        # not silently fall back to answering the broader, unfiltered
        # question (which would be actively wrong, not just less precise -
        # e.g. "no Olympics were held in the range this question implies"
        # should not quietly become "GDP for every country and year").
        return (
            f"Filter context: another database's own query for {filter_description} "
            f"returned NO matching rows at all. Your own query should reflect that there "
            f"is nothing to report here - write it so it correctly returns no rows (or "
            f"state plainly that there's nothing matching), rather than answering the "
            f"broader, unfiltered question.",
            0, False,
        )

    if len(deduped) > max_rows:
        # Triage guessed the sizes backward (the "driving" side wasn't
        # actually small) - see this function's own docstring: never apply
        # a filter built from a silently-truncated sample, since a WRONG
        # filter is worse than no filter at all (it produces a confidently
        # wrong answer instead of an honestly broader one). The caller
        # falls back to generating this connection's SQL unfiltered.
        return None, len(deduped), True

    lines = [
        ", ".join(f"{k}={v!r}" for k, v in pairs)
        for pairs in deduped
    ]
    text = (
        f"Filter context: restrict your answer to these real values for {filter_description}, "
        f"drawn from another database's own already-executed query this same turn - "
        f"{len(deduped)} row(s):\n" + "\n".join(lines)
    )
    return text, len(deduped), False


def _validate_request(data):
    """Returns an error string (for a 400 response) or None when `data`
    has everything this route needs. Never raises - every field is
    independently checked, same defensive posture as every other route in
    this app that trusts nothing from the client body."""
    if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
        return "prompt is required"
    if data.get("kind") not in ("preset", "custom"):
        return "kind must be \"preset\" or \"custom\""
    if not data.get("id"):
        return "id is required"
    if not isinstance(data.get("filter_description"), str) or not data["filter_description"].strip():
        return "filter_description is required"
    driving_result = data.get("driving_result")
    if not isinstance(driving_result, dict) or not isinstance(driving_result.get("rows"), list):
        return "driving_result (with a \"rows\" list) is required"
    return None


@semi_join_bp.route('/api/generate-semi-join-sql', methods=['POST'])
@translate_rate_limit
def generate_semi_join_sql():
    """Called by the client once, per semi-join pair, per turn: only after
    triage flagged "semi_join" (connection_router.py) AND the driving
    connection's own /api/execute has already completed with real rows (or
    a real empty result - see _build_filter_context's own zero-row
    handling). Generates - but does NOT execute - the dependent
    connection's own SQL, with the driving connection's real rows threaded
    in as filter context.

    Streams NDJSON, same contract shape as /api/translate's own
    single-connection path and /api/combine-results: zero or more
    {"status": "retrying", ...} lines, then exactly one terminal line:
      {"status": "done", "success": true, "outcome": "sql",
       "sql": "<marker-prepended, ready to pass straight to /api/execute>",
       "knowledge_note": <str>|None, "filter_applied": bool,
       "filter_row_count": int, "filter_capped": bool}
        - filter_applied is False (with filter_capped True) when the
          driving result exceeded SEMI_JOIN_MAX_FILTER_ROWS - the SQL
          above was generated WITHOUT any filter context at all in that
          case, same as an ordinary independently-fetched connection,
          so the caller should surface an honest caveat rather than
          silently presenting it as filtered.
      {"status": "done", "success": true, "outcome": "note", "text": "..."}
        - the model answered '*** NO SQL ***' instead (e.g. decided the
          filter context meant there was genuinely nothing to query).
      {"status": "done", "success": false, "error": "..."}
        - the LLM call itself failed (retry/key-rotation budget
          exhausted, or a non-retryable error) - never blocks the rest of
          the turn; the caller just shows this as an honest note, same
          posture as any other failed connection in this app."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    error = _validate_request(data)
    if error:
        resp = jsonify({'success': False, 'error': error})
        return apply_session_cookie(resp, session_id), 400

    session_data = state_store.get_session(user_identity)
    # Same is_superuser()-gated provider/model resolution as every other
    # LLM-calling route in this app (combine_routes.py's own
    # combine_results(), translate_routes.py's translate_query()) - a
    # non-superuser's request body and session-saved model are never
    # trusted for the actual model used, only the app's one fleet-wide
    # default is.
    if is_superuser(user_identity):
        provider = get_llm_provider(session_data.get('llm_provider'))
        llm_model = (
            data.get(provider.request_model_key) or data.get('model')
            or session_data.get('llm_model') or provider.default_model
        )
    else:
        provider = _default_fleet_provider()
        llm_model = provider.default_model
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        resp = jsonify({'success': False, 'error': provider.missing_key_error})
        return apply_session_cookie(resp, session_id), 400

    descriptor, name = resolve_descriptor_by_reference(data.get("kind"), data.get("id"), user_identity)
    if descriptor is None:
        resp = jsonify({'success': False, 'error': 'That connection is no longer available.'})
        return apply_session_cookie(resp, session_id), 400

    prompt = data["prompt"].strip()
    filter_description = data["filter_description"].strip()
    driving_result = data["driving_result"]
    filter_text, filter_row_count, filter_capped = _build_filter_context(
        filter_description, driving_result.get("columns") or [], driving_result.get("rows") or [],
    )
    history = (data.get("history") or [])

    entry = {"kind": data["kind"], "id": data["id"], "name": name}

    def stream_generate():
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            gen = generate_sql_for_connection(
                descriptor, prompt, history, provider, client, llm_model, user_identity,
                api_key=api_key, tried_keys={api_key}, using_byok=bool(byok_key),
                filter_context=filter_text,
            )
            try:
                generated_sql, usage_info, duration_ms, _key, _client, knowledge_note = yield from gen
            except LlmCallFailed as e:
                yield json.dumps({'status': 'done', 'success': False, 'error': str(e)}) + "\n"
                return
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()

        classified = _classify_generation_outcome(entry, ("ok", generated_sql, usage_info, duration_ms, knowledge_note))
        # Same "one dedicated translations-table row per real generation
        # call" convention every other Phase B call gets (see
        # translate_routes.py's own phase_b_log_entries loop) - this
        # connection's own call just happens to run later, in its own
        # request, rather than alongside the rest of that turn's fan-out.
        record_translation(
            user_identity, descriptor, prompt, classified.get("sql") or classified.get("text") or "",
            llm_model, duration_ms,
            usage_info.get("input_tokens", 0), usage_info.get("output_tokens", 0),
            usage_info.get("total_tokens", 0), usage_info.get("thinking_tokens", 0),
            usage_info.get("cached_content_tokens", 0),
        )
        if classified["outcome"] == "failed":
            # _classify_generation_outcome never actually produces this
            # for an ("ok", ...) tuple (see its own docstring - "failed"
            # only ever comes from an outcome[0] == "failed" input, which
            # this call site never passes) - kept purely as a defensive
            # fallthrough, not a path expected to execute.
            yield json.dumps({'status': 'done', 'success': False, 'error': classified["error"]}) + "\n"
            return
        yield json.dumps({
            'status': 'done', 'success': True,
            'filter_applied': bool(filter_text) and not filter_capped,
            'filter_row_count': filter_row_count,
            'filter_capped': filter_capped,
            **classified,
        }) + "\n"

    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many requests right now. Please try again in a few seconds.',
        })

    def _stream_generate_with_guard_release():
        try:
            yield from stream_generate()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_generate_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)
