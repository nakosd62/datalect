"""
combine_routes.py

"All databases" mode's new combine step - JOIN/UNION-ing the real result
sets Phase B + /api/execute already fetched from two or more databases,
entirely in memory, via an embedded DuckDB session. Runs (at most) once per
turn, between /api/execute and Phase C (summarize_routes.py's
summarize_all_mode_results) - see server/prompts/combine_results.txt and
connection_router.py's "needs_combination" field for the trigger.

Why this belongs in the pipeline at all: triage (connection_router.py's
_MULTI_CANDIDATE_TRIAGE_SYSTEM_INSTRUCTION) still tells Phase B each
connection is queried completely independently - there is still no real
cross-database JOIN happening against any live, configured database
anywhere in this app, and there never will be (two different connections,
often two different vendors, share no execution engine to join inside).
What this module adds instead is a second, purely-in-memory "database" -
DuckDB, running embedded in this process - that the already-fetched,
already-capped result rows get loaded into as plain views, so ONE more SQL
statement can relate them to each other the same way a single real
database's own JOIN would. Nothing here ever touches a real configured
connection a second time; this only ever operates on rows /api/execute
already returned to the client earlier in the same turn.

Safety: the DuckDB connection used here is opened with external access
(filesystem, network, extension loading) disabled - see _execute_combine_
sql below - and the generated statement is independently verified to be a
single, plain, read-only SELECT/WITH before it's ever executed, via
sqlparse (the same library backends/*.py already use to split multi-
statement scripts), never just trusted from the prompt's own instruction
to the model.

Deliberately reuses summarize_routes.py's shared _summarize_with_retry for
this call's own retry/key-rotation policy, rather than a third copy of
that ~270-line loop - this is the same kind of call (an LLM call that must
return structured JSON, same bounded-retry/language-verification needs) as
Phase C's own. One accepted simplification from that reuse: _summarize_
with_retry always logs its LLM usage under state_store.record_llm_usage's
"summary" call_type (see that method's own docstring, which documents
exactly four fixed call_type values) - this call's own cost is therefore
folded into "summary" in llm_usage stats, not broken out separately.
Giving it a genuinely new fifth call_type would mean touching that
docstring's closed enum and every StateStore backend's own
record_llm_usage implementation for a single new call site - a bigger
change than this feature needs; left as a known, deliberate simplification
rather than done partially/inconsistently.
"""
import json
import os
import concurrent.futures

import duckdb
import pandas as pd
import sqlparse
from flask import Blueprint, request, jsonify, Response, stream_with_context

from app_config import logger, state_store
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_group_identity
import translate_routes
import cancel_registry
from concurrency_guard import TRANSLATE_GUARD, busy_response
from rate_limiter import summarize_rate_limit
from prompt_loader import load_prompt
from llm_providers import format_llm_error_for_user, get_llm_provider
from connection_router import _extract_json_object
from summarize_routes import _summarize_with_retry

combine_bp = Blueprint('combine', __name__)

_COMBINE_SYSTEM_INSTRUCTION = load_prompt("combine_results.txt")

# How many of each result's own real rows are shown to the LLM as a
# sample when it's deciding how to combine them - deliberately much
# smaller than summarize_routes.py's own SUMMARY_RESULTS_MAX_ROWS: the
# model only needs enough of a look to infer real column shapes/types and
# spot a plausible join key, not the whole result set - the SQL it writes
# runs against the FULL set of already-fetched rows regardless of how few
# of them it was shown here.
COMBINE_SAMPLE_ROWS = int(os.environ.get("COMBINE_SAMPLE_ROWS", 20))

# How many rows the combined query's own output is capped at - same
# ceiling backends/base.py's EXECUTE_RESULTS_MAX_ROWS already uses for a
# live database result, for the same reason (bounding one response's
# size/cost), even though this one never actually hits a live database.
COMBINE_RESULTS_MAX_ROWS = int(os.environ.get("COMBINE_RESULTS_MAX_ROWS", 500))

# Wall-clock bound on actually RUNNING the generated statement against
# DuckDB (not on the LLM call that wrote it - that already has its own
# budget via _summarize_with_retry/MAX_TRANSLATION_ATTEMPTS) - mirrors
# execute_routes.py's _execute_with_timeout/SQL_EXECUTE_TIMEOUT_SECONDS
# (same thread-race idiom: the worker thread is abandoned, not truly
# cancelled, on timeout), just with a much shorter default - this is an
# in-memory operation over at most a few thousand already-fetched rows,
# so anything approaching a real SQL_EXECUTE_TIMEOUT_SECONDS-scale wait
# almost certainly means a pathological statement (e.g. an accidental
# cross join on two sizeable sides), not merely a slow one.
COMBINE_EXECUTE_TIMEOUT_SECONDS = float(os.environ.get("COMBINE_EXECUTE_TIMEOUT_SECONDS", 10))


def _combinable_entries(database_results):
    """Returns [(original_index, entry), ...] for every entry in
    `database_results` (same per-statement-result/note/failure shape
    summarize_routes.py's _build_summary_prompt docstring describes) that
    actually has real columns/rows to combine - a note or a failure has
    neither, and is simply excluded from consideration here the same way
    it's already excluded from _build_summary_prompt's own SQL/chartable
    sections. `original_index` is `database_results`'s own 0-based
    position, preserved (never renumbered) so the view name assigned to
    each one (see _build_combine_schema_block) matches the SAME index the
    rest of this turn's pipeline (Phase C's "[i]" labels, client.js's
    result:N/chart:N link indices) already uses for that exact entry."""
    out = []
    for i, entry in enumerate(database_results or []):
        if entry.get("error") or entry.get("note"):
            continue
        if entry.get("columns") and entry.get("rows") is not None:
            out.append((i, entry))
    return out


def _infer_column_type(rows, column):
    """A short, human-readable type guess for one column, from its own
    real values (already normalized client-side the same way every other
    value in this app is - see backends/base.py's normalize_cell_value:
    dates/timestamps as ISO strings, decimals as plain floats) - shown to
    the LLM alongside each column's name so it can decide where an
    explicit CAST is needed (see combine_results.txt's own "Several of the
    columns..." paragraph) without having to guess from the column's name
    alone. Looks at the first non-null value only - good enough for a
    prompt hint, not a real schema; the actual DuckDB execution infers its
    own real types directly from the data regardless of what's guessed
    here."""
    for row in rows:
        value = (row or {}).get(column)
        if value is None:
            continue
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        return "string"
    return "unknown"


def _build_combine_schema_block(combinable):
    """Renders `combinable` (see _combinable_entries) into the per-view
    description block combine_results.txt's prompt expects: one section
    per entry, labeled with the SAME view name DuckDB will actually
    register it under (see _execute_combine_sql) and the SAME "[i]" index
    the rest of this turn's pipeline already uses for it, its real column
    names with an inferred type per column (_infer_column_type), its real
    total row count, and up to COMBINE_SAMPLE_ROWS of its actual rows -
    deliberately a much smaller sample than Phase C's own
    SUMMARY_RESULTS_MAX_ROWS (see that constant's own comment above)."""
    blocks = []
    for i, entry in combinable:
        name = entry.get("name") or "Unknown database"
        cols = entry.get("columns") or []
        rows = entry.get("rows") or []
        row_count = entry.get("rowCount", len(rows))
        shown = rows[:COMBINE_SAMPLE_ROWS]
        col_types = ", ".join(f"{c} ({_infer_column_type(rows, c)})" for c in cols)
        truncated_note = f" (showing a sample of {len(shown)})" if len(shown) < row_count else ""
        blocks.append(
            f"[{i}] view name: results_{i} - from \"{name}\"\n"
            f"Columns: {col_types}\n"
            f"Total rows: {row_count}{truncated_note}\n"
            f"Sample rows:\n" + "\n".join(str(r) for r in shown)
        )
    return "\n\n".join(blocks)


def _build_combine_prompt(user_question, combinable):
    schema_block = _build_combine_schema_block(combinable)
    return (
        f"Views available for this question:\n\n{schema_block}\n\n"
        f"User's original question: {user_question}"
    )


def _clean_combine_response(raw_text):
    """Parses combine_results.txt's raw response text into exactly one of:
      {"ok": True, "sql": <non-empty str>}
      {"ok": False, "error": <non-empty str>}
      None  # unparseable/malformed - caller retries, same convention as
            # every other content_parser this app passes to
            # _summarize_with_retry (connection_router.py's own
            # _parse_multi_candidate_triage_response, summarize_routes.py's
            # _clean_summary_response). _extract_json_object already
            # tolerates markdown-fence wrapping/surrounding chatter - see
            # its own docstring - so nothing extra is needed here for
            # that."""
    parsed = _extract_json_object(raw_text) if raw_text else None
    if not isinstance(parsed, dict):
        return None
    success = parsed.get("success")
    if success is True:
        sql = parsed.get("sql")
        if isinstance(sql, str) and sql.strip():
            return {"ok": True, "sql": sql.strip()}
        return None
    if success is False:
        error = parsed.get("error")
        if isinstance(error, str) and error.strip():
            return {"ok": False, "error": error.strip()}
        return None
    return None


def _combine_language_text(parsed):
    """_summarize_with_retry's own language_text_extractor for this
    caller - only a failure's "error" text is ever natural language the
    user reads (see combine_results.txt's own language-matching
    paragraph); "sql" is plain SQL regardless of the question's language,
    so a success has nothing here worth language-checking."""
    return parsed.get("error") if not parsed.get("ok") else None


def _is_single_readonly_select(sql_text):
    """True iff `sql_text` is exactly one statement (per sqlparse.split -
    the same splitter backends/postgres.py's own multi-statement handling
    already uses) whose own top-level type sqlparse recognizes as SELECT
    (a plain SELECT, or a WITH ... SELECT - sqlparse already classifies
    both as "SELECT", never "UNKNOWN", the same way it does for every
    other DML/DDL keyword it recognizes). This is the server-side safety
    net on top of combine_results.txt's own instruction to the model, not
    a replacement for also opening the DuckDB connection itself with
    external access disabled (see _execute_combine_sql) - a generated
    statement is never trusted on the prompt's word alone."""
    statements = [s for s in sqlparse.split(sql_text or "") if s.strip()]
    if len(statements) != 1:
        return False
    stripped = statements[0].strip().rstrip(";")
    if not stripped:
        return False
    return sqlparse.parse(stripped)[0].get_type() == "SELECT"


def _execute_combine_sql(sql_text, combinable):
    """Runs `sql_text` (already verified by _is_single_readonly_select)
    against a throwaway, in-memory DuckDB session with one view per
    `combinable` entry (see _combinable_entries), registered under the
    SAME "results_{i}" name the prompt was told about (_build_combine_
    schema_block) - each view backed by a pandas DataFrame built straight
    from that entry's own already-fetched `rows`/`columns` (pandas is
    already a dependency - see requirements.txt), nothing re-fetched from
    any real connection. Returns (columns, rows, row_count, truncated) on
    success - `rows` capped at COMBINE_RESULTS_MAX_ROWS, `truncated` True
    iff the real combined result had more. Raises on any execution failure
    (malformed SQL DuckDB itself rejects, a TimeoutError if
    COMBINE_EXECUTE_TIMEOUT_SECONDS is exceeded) - the caller turns that
    into this turn's own honest failure explanation, same posture as
    every other LLM-authored-SQL failure in this app.

    `enable_external_access=False` (DuckDB's own config flag) is what
    actually blocks ATTACHing a real file, COPY/read_csv/read_parquet
    against the filesystem, and LOADing an extension - independent of,
    and in addition to, _is_single_readonly_select's own statement-shape
    check above (that check alone wouldn't catch, say, a SELECT that still
    happened to reference a file-backed table function; this config flag
    closes that regardless of statement shape)."""
    con = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        for i, entry in combinable:
            df = pd.DataFrame(entry.get("rows") or [], columns=entry.get("columns") or None)
            con.register(f"results_{i}", df)

        def _run():
            cursor = con.execute(sql_text)
            columns = [d[0] for d in cursor.description]
            return cursor.fetchall(), columns

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_run)
            try:
                raw_rows, columns = future.result(timeout=COMBINE_EXECUTE_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(
                    f"Combining these results timed out after {COMBINE_EXECUTE_TIMEOUT_SECONDS:g} seconds"
                ) from None
        finally:
            # wait=False - same reasoning as execute_routes.py's own
            # _execute_with_timeout: on a timeout, this call must not
            # block waiting for the abandoned worker thread to finish.
            pool.shutdown(wait=False)
    finally:
        con.close()

    rows = [dict(zip(columns, r)) for r in raw_rows]
    row_count = len(rows)
    truncated = row_count > COMBINE_RESULTS_MAX_ROWS
    return columns, rows[:COMBINE_RESULTS_MAX_ROWS], row_count, truncated


def combine_all_mode_results(user_question, database_results, provider, client, model, user_identity=None,
                              api_key=None, tried_keys=None, using_byok=False,
                              dataset_type=None, dataset_name=None):
    """The combine step's own LLM call (one more SQL-writing call, same
    retry/key-rotation machinery every other LLM call in this app gets -
    see _summarize_with_retry) plus the actual DuckDB execution of
    whatever it returns. GENERATOR (see _summarize_with_retry's own
    docstring) - forwards its live 'retrying' progress lines unchanged.

    Returns one of:
      ("combined", {"columns", "rows", "rowCount", "truncated", "sql"}, usage, None)
      ("failed", None, usage, <error>)  # `error` is the RAW exception when
        the LLM call itself is what failed (retry/key-rotation budget
        exhausted, or a non-retryable error outright), or a plain
        descriptive string for every other failure (the model's own
        honest "success": false explanation, a rejected non-read-only
        statement, or a DuckDB execution error) - same "isinstance-check
        to tell the two apart" contract _summarize_with_retry's own
        docstring documents for every other caller of it, deliberately
        NOT pre-formatted here (format_llm_error_for_user needs a real
        LlmProvider's own error_category(), which only the ROUTE below -
        not this function, and not a test driving this function directly
        with a minimal fake provider - needs to depend on).
      ("skipped", None, None, None)  # fewer than 2 combinable entries -
        nothing to actually combine, regardless of what triage decided;
        never treated as a failure
    `usage` is None whenever no LLM call actually ran (the "skipped" case)."""
    combinable = _combinable_entries(database_results)
    if len(combinable) < 2:
        return "skipped", None, None, None

    expected_language_code = translate_routes._detect_language(user_question)
    prompt_content = _build_combine_prompt(user_question, combinable)
    parsed, usage, error = yield from _summarize_with_retry(
        prompt_content, "", _COMBINE_SYSTEM_INSTRUCTION, provider, client, model,
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
        log_label="Combine-results", expected_language_code=expected_language_code,
        content_parser=_clean_combine_response,
        language_text_extractor=_combine_language_text,
        invalid_content_error="response was not valid combine-step JSON",
        user_identity=user_identity, dataset_type=dataset_type, dataset_name=dataset_name,
    )
    if parsed is None:
        return "failed", None, usage, error

    if not parsed.get("ok"):
        return "failed", None, usage, parsed["error"]

    sql_text = parsed["sql"]
    if not _is_single_readonly_select(sql_text):
        return "failed", None, usage, (
            "The combine step tried to run something other than a single read-only query, "
            "so it was rejected."
        )

    try:
        columns, rows, row_count, truncated = _execute_combine_sql(sql_text, combinable)
    except Exception as e:
        logger.warning("Combine-step SQL execution failed: %s", e)
        return "failed", None, usage, f"Combining these results failed: {e}"

    return "combined", {
        "columns": columns, "rows": rows, "rowCount": row_count,
        "truncated": truncated, "sql": sql_text,
    }, usage, None


@combine_bp.route('/api/combine-results', methods=['POST'])
# Same pooled guard/rate limit /api/summarize-results itself draws from -
# see that route's own comment: an LLM call (plus, here, a quick in-memory
# DuckDB execution) is the same kind of work as far as this app's
# admission control is concerned, not a separate budget of its own.
@summarize_rate_limit
def combine_results():
    """Called by the client at most once per turn, only when triage set
    "needs_combination": true (connection_router.py's "sql" outcome - see
    that prompt's own paragraph) AND /api/execute came back with at least
    two real (non-note, non-failure) results to actually combine - see
    combine_all_mode_results' own "skipped" outcome for why the second
    half of that is re-checked here too rather than trusted from the
    client alone.

    Streams NDJSON, identical contract to /api/summarize-results (zero or
    more {"status": "retrying", ...} lines, then exactly one terminal
    line):
      {"status": "done", "success": true, "skipped": true}
        - fewer than 2 combinable entries; no combined tab to add.
      {"status": "done", "success": true, "skipped": false,
       "result": {"columns": [...], "rows": [...], "rowCount": N,
                  "truncated": bool, "sql": "..."}}
        - a combined result, shaped like any other database result entry
          (see execute_routes.py's module docstring) so the client can
          append it to `database_results` with zero new rendering
          concepts, with its own generated SQL attached for display.
      {"status": "done", "success": false, "error": "..."}
        - combine genuinely failed (no plausible key, a rejected
          statement, an execution error, or the LLM call itself
          exhausting its retry budget) - never blocks the rest of the
          turn; the caller just shows this as an honest note and carries
          on to Phase C over the per-database results exactly as it would
          have anyway."""
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

    def stream_combine_results():
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            group_dataset_type, group_dataset_name = resolve_group_identity(
                session_data.get('in_scope_group_id') or ''
            )
            outcome, result, usage, error = yield from combine_all_mode_results(
                prompt, database_results, provider, client, llm_model, user_identity=user_identity,
                api_key=api_key, using_byok=bool(byok_key),
                dataset_type=group_dataset_type, dataset_name=group_dataset_name,
            )
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()

        if outcome == "skipped":
            yield json.dumps({'status': 'done', 'success': True, 'skipped': True}) + "\n"
            return
        if outcome == "failed":
            # Same "isinstance-check to tell an LLM-call-level failure
            # apart from the model's/our own honest plain-string reason"
            # logic /api/summarize-results' own route already applies to
            # summarize_all_mode_results' identically-shaped "error" -
            # see combine_all_mode_results' own docstring for why this
            # formatting belongs here, not inside that generator.
            error_message = (
                format_llm_error_for_user(provider, llm_model, error, using_byok=bool(byok_key))
                if isinstance(error, BaseException) else
                (error or "Unable to combine these results right now.")
            )
            yield json.dumps({'status': 'done', 'success': False, 'error': error_message}) + "\n"
            return
        yield json.dumps({
            'status': 'done', 'success': True, 'skipped': False, 'result': result,
        }) + "\n"

    # See concurrency_guard.py's own module docstring - TRANSLATE_GUARD,
    # the same pooled guard /api/summarize-results itself uses, acquired
    # here (right before actually streaming) for the identical reason:
    # nothing above this point does any real work, so nothing before it
    # should compete for a scarce slot.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many results-combination requests right now. Please try again in a few seconds.',
        })

    def _stream_combine_results_with_guard_release():
        try:
            yield from stream_combine_results()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_combine_results_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)
