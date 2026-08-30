"""
execute_routes.py

Runs client-submitted SQL against the resolved connection and returns
results. This is the one endpoint that intentionally returns raw database
error messages to the client (see the comment in the except block) - it's
a SQL runner, and the user needs the actual DB error to fix their query,
same as any SQL client would show them.

Statement execution itself is delegated to the resolved Backend (see
backends/base.py) - this route no longer knows or cares whether it's
talking to psycopg2, a BigQuery job client, or anything else. Its own job
is just: resolve the connection, time the call, shape the HTTP response,
and translate any backend exception into the existing error JSON shape.
That execute() call is wrapped in a wall-clock bound (see
SQL_EXECUTE_TIMEOUT_SECONDS/_execute_with_timeout below) so a runaway or
stuck query fails with a clear timeout error instead of hanging the
request forever - the execute-time counterpart to backends/base.py's
DB_CONNECT_TIMEOUT_SECONDS, which only covers connect().

A multi-statement script (semicolon-separated) that fails partway through
raises backends.SqlExecutionError instead of a bare exception (see that
class's docstring) - this route builds a richer failure response for that
case specifically, so the client can render every ATTEMPTED statement
(the ones that succeeded, plus the one that failed) as its own tab, the
same tabbed UI as an all-succeeded response, rather than one opaque error
that loses track of what did or didn't run:
  {"success": false, "error": "<failing statement's raw DB error>",
   "results": [...succeeded-statement dicts, in order...],
   "failedStatement": "<raw SQL text of the statement that failed>",
   "failedIndex": <0-based position among all statements>,
   "totalStatements": <how many statements the script was split into>,
   "executionTimeMs": ...}
Any other exception (connect() failure, a single-statement script's own
error, a backend with no multi-statement concept) keeps the original flat
shape: {"success": false, "error": ..., "executionTimeMs": ...} - no
"results"/"failedStatement" keys at all.

Also owns /api/ping, a separate lightweight liveness check for the status
dot (see that route's own docstring for why it isn't just /api/execute
with a hardcoded query string).

Multi-database question-answering (see translate_routes.py's module
docstring) adds a second dispatch path, entered only when the submitted
SQL carries at least one '-- database: preset:<id>'/'-- database:
custom:<key>' marker comment (see the "Multi-database dispatch" section
below) - a script with no such marker anywhere takes the single-connection
path above completely unchanged, byte-for-byte, including its response
shape. A marker-tagged script instead dispatches each distinct referenced
connection's statements to their own connection (opened once each, even
if interleaved in the original text) via this same single-connection
execute() path per connection, and merges every group's results into one
response shaped like:
  {"success": <false iff any group failed>, "results": [...every
   succeeded statement, across every group, each tagged with a
   "database": {"kind","id","name"} field...], "rowCount": ...,
   "executionTimeMs": ..., "failures": [...one entry per group that
   failed - only present when success is false...]}
"""

import concurrent.futures
import os
import re
import time

from flask import Blueprint, request, jsonify

from app_config import logger
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, resolve_descriptor_by_reference
from backends import get_backend, SqlExecutionError

execute_bp = Blueprint('execute', __name__)

# --- SQL execution timeout ---------------------------------------------------
# Bounds how long backend.execute() (below and in ping()) may run once a
# connection is already open - the execute-time counterpart to
# backends/base.py's DB_CONNECT_TIMEOUT_SECONDS, which only bounds connect().
# Without this, a runaway or accidentally-huge query (or a connection that
# goes silently dead mid-query) blocks the request indefinitely - same
# unbounded-hang problem DB_CONNECT_TIMEOUT_SECONDS already solves for
# connect(), just at the other end of the same call.
#
# Enforced generically (see _execute_with_timeout below) rather than via a
# per-dialect driver kwarg the way DB_CONNECT_TIMEOUT_SECONDS threads
# connect_timeout/tcp_connect_timeout/login_timeout per backend: there's no
# one statement-timeout knob shared across the ~10 supported dialects
# (BigQuery's is job-level, Sheets has no real query concept, MongoDB Atlas
# SQL/Databricks/Snowflake each differ again), so chasing down and
# maintaining a different driver-specific setting per backend isn't worth it
# for what's fundamentally the same fix everywhere. One shared env var
# covers all of them uniformly instead.
#
# 0 (or any non-positive value) disables this entirely - execute() then runs
# exactly as it did before this was added, with no wall-clock bound at all.
SQL_EXECUTE_TIMEOUT_SECONDS = float(os.environ.get("SQL_EXECUTE_TIMEOUT_SECONDS", 30))


def _execute_with_timeout(backend, conn, sql_text):
    """Runs backend.execute(conn, sql_text), bounded by
    SQL_EXECUTE_TIMEOUT_SECONDS (see that constant's docstring for why this
    is a generic thread-race rather than a per-backend driver setting).

    This can only bound how long the CALLER waits, not truly cancel work
    already in flight against the database - the executing thread is simply
    abandoned once the timeout fires (its eventual result or exception is
    discarded), left for the existing `finally: backend.close(conn)` in
    execute_query()/ping() to clean up. Closing `conn` from the caller's
    thread while the abandoned thread may still be blocked on it (a network
    read, typically) is what actually nudges most drivers to unblock and
    give up rather than hang forever - a best-effort nudge, not a
    guarantee, and driver-specific in exactly how it behaves. Either way
    `conn` must be treated as dead the moment this raises; nothing here
    tries to reuse it afterward.

    Raises the original exception unchanged on any non-timeout failure
    (a real SqlExecutionError or any other backend.execute() exception) -
    only a timeout gets a new, friendlier TimeoutError raised in its place.
    """
    if SQL_EXECUTE_TIMEOUT_SECONDS <= 0:
        return backend.execute(conn, sql_text)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(backend.execute, conn, sql_text)
        try:
            return future.result(timeout=SQL_EXECUTE_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"Query execution timed out after {SQL_EXECUTE_TIMEOUT_SECONDS:g} seconds"
            ) from None
    finally:
        # wait=False, always - see docstring: the whole point of this
        # timeout is that the caller must not block on the abandoned
        # thread, and a `with ThreadPoolExecutor(...)` block (or an
        # explicit wait=True shutdown) would do exactly that by waiting
        # for it to finish before letting this function return/raise.
        pool.shutdown(wait=False)


# --- Multi-database dispatch -------------------------------------------------
#
# Support for "all databases" mode (see translate_routes.py's module
# docstring): a script generated (or later hand-edited) for a "route"
# outcome carries one '-- database: preset:<id>'/'-- database:
# custom:<key>' comment immediately before EVERY statement, tagging which
# connection it targets (see translate_routes.py's
# _classify_generation_outcome - this marker is mechanically prepended
# server-side once Phase B's per-connection call returns, never something
# the model itself is asked to write). A script with NO such marker
# anywhere - every script generated before this feature existed, any
# hand-typed SQL, or one where the user stripped the comment - is
# completely unaffected: see execute_query() below, which only enters
# this dispatch path when at least one marker is found at all.
_DB_MARKER_RE = re.compile(r'^--\s*database:\s*(preset|custom):(\S+)', re.MULTILINE)

# Matches a WHOLE '-- database: ...' marker line (including its trailing
# '(<name>)' and the newline that ends it, not just the (kind, ref_id)
# prefix _DB_MARKER_RE itself captures) - see _strip_database_marker_lines
# below for why this exists as its own pattern.
_DB_MARKER_LINE_RE = re.compile(r'^--\s*database:\s*(?:preset|custom):\S+.*\n?', re.MULTILINE)


def _strip_database_marker_lines(sql_text):
    """Removes every '-- database: ...' marker line from sql_text before
    it ever reaches a backend's execute(). The marker's only job - telling
    _split_by_database_markers which connection a chunk targets - is
    already done by the time _execute_one_group has a concatenated group
    to run; nothing downstream needs the comment text itself.

    This matters for more than tidiness: real SQL dialects treat a line
    starting with '--' as a harmless comment, so leaving the marker in
    never broke anything there and easily went unnoticed - but Google
    Sheets' GViz query language (backends/sheets.py) is SQL-*like*, not
    real SQL, and has no comment syntax at all. A leading '-- database:
    ...' line isn't silently ignored there, it's a syntax error that
    breaks every Sheets connection's turn in ANY multi-database script,
    even though the actual query right below it would have run fine on
    its own. Stripping the marker once, centrally, here - rather than
    teaching sheets.py (or every future backend with the same limitation)
    to defensively strip a marker it was never supposed to know existed -
    fixes Sheets and keeps every other backend's behavior unchanged (a
    '--' comment they were already silently discarding at execution time
    just never reaches them at all now)."""
    return _DB_MARKER_LINE_RE.sub('', sql_text).strip()


def _split_by_database_markers(sql_text):
    """Splits `sql_text` into an ordered list of (kind, ref_id, chunk_text)
    tuples, one per '-- database: ...' marker found, each chunk_text
    running from that marker's own line up to (not including) the next
    marker (or the end of the script). Returns None if no marker is found
    at all - the caller's signal to fall back to today's single-connection
    execution path unchanged.

    Any content before the FIRST marker (rare - normally just blank lines/
    stray comments, since translate_routes.py always tags every statement
    starting with the very first one) is folded into that first chunk
    rather than silently dropped, so a user's own leading comment/
    formatting above the first statement still gets sent to whichever
    connection that first statement targets."""
    matches = list(_DB_MARKER_RE.finditer(sql_text))
    if not matches:
        return None
    chunks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(sql_text)
        chunks.append((m.group(1), m.group(2), sql_text[start:end]))
    if matches[0].start() > 0:
        kind, ref_id, chunk_text = chunks[0]
        chunks[0] = (kind, ref_id, sql_text[:matches[0].start()] + chunk_text)
    return chunks


def _execute_one_group(kind, ref_id, concatenated_sql, user_identity):
    """Runs one connection group's concatenated statements (see
    _execute_multi_database below) and NEVER raises - every outcome,
    including a since-deleted connection reference or a real execution
    failure, is captured and returned as
    {"results": [...succeeded statement dicts, "database"-tagged...],
     "failure": <failure dict, shaped like the old inline except-branches
     below, or None if the whole group succeeded>}.

    Returning rather than raising/appending-to-a-shared-list is what makes
    this safe to run from a ThreadPoolExecutor worker: each call only ever
    touches its own locals and its own connection, so N of these can run
    concurrently (one per distinct connection group) with no shared
    mutable state to race on - the caller (_execute_multi_database) is the
    only place results get merged, sequentially, back on the calling
    thread."""
    descriptor, name = resolve_descriptor_by_reference(kind, ref_id, user_identity)
    if descriptor is None:
        return {
            "results": [],
            "failure": {
                "database": {"kind": kind, "id": ref_id, "name": None},
                "error": f"This SQL references a database connection ({kind}:{ref_id}) that no longer exists.",
                "failedStatement": concatenated_sql.strip(),
            },
        }

    backend = None
    conn = None
    try:
        backend = get_backend(descriptor)
        conn = backend.connect(descriptor)
        group_results = _execute_with_timeout(backend, conn, _strip_database_marker_lines(concatenated_sql))
        for r in group_results:
            r["database"] = {"kind": kind, "id": ref_id, "name": name}
        return {"results": group_results, "failure": None}
    except SqlExecutionError as e:
        for r in e.results:
            r["database"] = {"kind": kind, "id": ref_id, "name": name}
        return {
            "results": e.results,
            "failure": {
                "database": {"kind": kind, "id": ref_id, "name": name},
                "error": str(e),
                "failedStatement": e.failed_statement,
                "failedIndex": e.statement_index,
                "totalStatements": e.total_statements,
            },
        }
    except Exception as e:
        logger.warning(
            "Multi-database SQL execution error for user=%s, connection=%s:%s: %s",
            user_identity, kind, ref_id, e,
        )
        return {
            "results": [],
            "failure": {
                "database": {"kind": kind, "id": ref_id, "name": name},
                "error": str(e),
            },
        }
    finally:
        if conn and backend:
            backend.close(conn)


def _execute_multi_database(chunks, user_identity):
    """Runs a marker-tagged multi-database script (see
    _split_by_database_markers above) and returns (results, failures) -
    `results` is every statement that succeeded, ACROSS every connection
    group, each tagged with a "database" field ({"kind","id","name"});
    `failures` is one entry per connection group that failed at all
    (empty when everything succeeded).

    Each distinct (kind, ref_id) referenced anywhere in the script gets
    its own connection, opened exactly ONCE regardless of how many
    separate marker chunks target it (its chunks are concatenated, in
    their original relative order, into one script and handed to that
    connection's OWN backend.execute() - fully reusing that existing,
    unmodified per-connection multi-statement/semicolon-splitting and
    SqlExecutionError partial-results handling, rather than reimplementing
    any of it here). A group whose connection reference no longer resolves
    (resolve_descriptor_by_reference returned None - a preset removed or a
    custom connection deleted since this SQL was generated/saved) is
    recorded as its own failure rather than raising, same "keep the other,
    independent groups running" posture as a real execution failure gets.

    Every distinct connection group runs in PARALLEL (one worker per
    group, via the same pre-allocate-results-array + future_to_index +
    as_completed pattern used elsewhere in this codebase - see db.py's
    build_router_candidate_summaries and translate_routes.py's
    _run_phase_b_fanout) - one group being slow (or hanging up to
    SQL_EXECUTE_TIMEOUT_SECONDS) no longer delays the others.

    Ordering note: the final `results` list is ordered by each DISTINCT
    connection group's first appearance in the script, with that group's
    own statements in their correct relative order within it - not a
    perfect global interleave down to individual-statement granularity
    across groups whose marker chunks are non-contiguous in the original
    text (a rare pattern - translate_routes.py's prompt has the model tag
    every statement, but naturally groups a given connection's statements
    together). This is a deliberate v1 scope cut: correct dispatch and
    correct per-group ordering either way, just not a byte-for-byte replay
    of an unusual interleaved-authoring order across groups. This ordering
    is driven by `group_order`'s original index, NOT by which group's
    thread happens to finish first - outcomes are written into a
    pre-allocated, index-addressed list precisely so parallelizing this
    doesn't change the response shape a client (or existing test) sees."""
    groups = {}
    group_order = []
    for kind, ref_id, chunk_text in chunks:
        key = (kind, ref_id)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(chunk_text)

    outcomes = [None] * len(group_order)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(group_order)) as pool:
        future_to_index = {
            pool.submit(_execute_one_group, kind, ref_id, "\n".join(groups[(kind, ref_id)]), user_identity): i
            for i, (kind, ref_id) in enumerate(group_order)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            outcomes[future_to_index[future]] = future.result()

    all_results = []
    failures = []
    for outcome in outcomes:
        all_results.extend(outcome["results"])
        if outcome["failure"] is not None:
            failures.append(outcome["failure"])

    return all_results, failures


@execute_bp.route('/api/execute', methods=['POST'])
def execute_query():
    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    # A marker-free script (see the dispatch below) still honors a
    # client-echoed pinned connection (see translate_routes.py's module
    # docstring on the pin mechanism) over the session's single "primary"
    # connection, whenever the caller hasn't passed an explicit
    # database_url override - this is what makes hand-typed/edited SQL run
    # against the conversation's actually-pinned connection rather than
    # always falling back to the first in-scope one. Harmless/no-op for a
    # single-connection session, which never has anything pinned (empty
    # pinned_connections is falsy below) - byte-identical to before this
    # feature existed. Only the FIRST pinned reference is used here (a
    # marker-free script is inherently single-connection - if it needed
    # more than one it would carry markers); if it fails to resolve
    # (deleted/removed since it was pinned), this silently falls back to
    # the session's primary connection, same graceful-degradation posture
    # as every other stale-reference case in this feature.
    pinned_connections = data.get('pinned_connections') or []
    if not data.get('database_url') and pinned_connections and isinstance(pinned_connections[0], dict):
        pinned_descriptor, _pinned_name = resolve_descriptor_by_reference(
            pinned_connections[0].get('kind'), pinned_connections[0].get('id'), user_identity
        )
        descriptor = pinned_descriptor if pinned_descriptor is not None else resolve_conn_str(None, user_identity)
    else:
        descriptor = resolve_conn_str(data.get('database_url'), user_identity)

    raw_query = (data.get('sql') or data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'Query cannot be empty'}), 400

    start_time = time.time()

    # Multi-database dispatch - see this module's "Multi-database dispatch"
    # section above. Only entered when at least one '-- database: ...'
    # marker is found anywhere in the script; a marker-free script (every
    # script this app generated before this feature existed, any hand-
    # typed SQL, or one where the user stripped the comment) falls straight
    # through to the single-connection path below, completely unchanged.
    database_marker_chunks = _split_by_database_markers(raw_query)
    if database_marker_chunks is not None:
        results, failures = _execute_multi_database(database_marker_chunks, user_identity)
        total_row_count = sum(r.get('rowCount', 0) for r in results)
        execution_time_ms = round((time.time() - start_time) * 1000)
        resp_body = {
            'success': not failures,
            'results': results,
            'rowCount': total_row_count,
            'executionTimeMs': execution_time_ms,
        }
        if failures:
            resp_body['failures'] = failures
        resp = jsonify(resp_body)
        if failures:
            return apply_session_cookie(resp, session_id), 400
        return apply_session_cookie(resp, session_id)

    backend = None
    conn = None

    try:
        backend = get_backend(descriptor)
        conn = backend.connect(descriptor)

        results = _execute_with_timeout(backend, conn, raw_query)
        total_row_count = sum(r.get('rowCount', 0) for r in results)

        execution_time_ms = round((time.time() - start_time) * 1000)

        resp = jsonify({
            'success': True,
            'results': results,
            'rowCount': total_row_count,
            'executionTimeMs': execution_time_ms
        })
        return apply_session_cookie(resp, session_id)

    except SqlExecutionError as e:
        # A multi-statement script failed partway through - e.results holds
        # every statement that succeeded BEFORE the failure (see that
        # class's docstring in backends/base.py and this module's own
        # docstring for the resulting response shape). Same "log server-
        # side, return the raw message" posture as the plain-Exception
        # branch below, just with the extra positional detail attached.
        execution_time_ms = round((time.time() - start_time) * 1000, 2)
        logger.warning(
            "SQL execution error for user=%s (statement %d/%d): %s",
            user_identity, e.statement_index + 1, e.total_statements, e
        )
        resp = jsonify({
            'success': False,
            'error': str(e),
            'results': e.results,
            'failedStatement': e.failed_statement,
            'failedIndex': e.statement_index,
            'totalStatements': e.total_statements,
            'executionTimeMs': execution_time_ms
        })
        return apply_session_cookie(resp, session_id), 400
    except Exception as e:
        execution_time_ms = round((time.time() - start_time) * 1000, 2)
        # Note: unlike the other handlers in this codebase, we intentionally
        # return the real database error message here. This endpoint runs
        # SQL the user themselves supplied/approved, so the backend's error
        # (bad column, syntax error, constraint violation, etc.) *is* the
        # feedback they need to fix their query - same as any SQL client.
        # We still log it server-side for observability/correlation.
        logger.warning("SQL execution error for user=%s: %s", user_identity, e)
        resp = jsonify({
            'success': False,
            'error': str(e),
            'executionTimeMs': execution_time_ms
        })
        return apply_session_cookie(resp, session_id), 400
    finally:
        if conn and backend:
            backend.close(conn)


@execute_bp.route('/api/ping', methods=['GET'])
def ping():
    """Cheap "is the active connection alive" check for the status dot
    client.js's checkDbStatus() polls - split out from /api/execute rather
    than reusing it with a hardcoded "SELECT 1" query text, because no
    single query string is valid across every dialect (Oracle has no
    SELECT-without-FROM form - see backends/base.py's liveness_sql). Each
    backend already knows its own dialect's quirks everywhere else in this
    file's sibling modules; this just asks it for the one trivial
    statement it knows is always safe to run, instead of the client
    guessing one that happens to work for most dialects.

    Deliberately its own route rather than a query param on /api/execute
    (e.g. "?ping=1"): a GET (idempotent, cheap to poll on an interval) with
    no request body, versus /api/execute's POST-with-arbitrary-SQL shape -
    conflating the two would mean either accepting a GET with a SQL body
    or bolting a "this one's not really user SQL" flag onto the one route
    that intentionally returns raw DB errors straight to the client (see
    the module docstring above)."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)

    descriptor = resolve_conn_str(None, user_identity)

    backend = None
    conn = None
    try:
        backend = get_backend(descriptor)
        conn = backend.connect(descriptor)
        _execute_with_timeout(backend, conn, backend.liveness_sql)
        resp = jsonify({'success': True})
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        # Same "log server-side, don't leak detail" posture as every other
        # non-/api/execute route in this app - the status dot only ever
        # shows connected/disconnected, never an error message, so there's
        # no reason for the client to see the raw exception text here.
        logger.warning("Ping (liveness check) failed for user=%s: %s", user_identity, e)
        resp = jsonify({'success': False})
        return apply_session_cookie(resp, session_id), 400
    finally:
        if conn and backend:
            backend.close(conn)