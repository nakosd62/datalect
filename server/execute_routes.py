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
"""

import time

from flask import Blueprint, request, jsonify

from app_config import logger
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str
from backends import get_backend, SqlExecutionError

execute_bp = Blueprint('execute', __name__)


@execute_bp.route('/api/execute', methods=['POST'])
def execute_query():
    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    descriptor = resolve_conn_str(data.get('database_url'), user_identity)

    raw_query = (data.get('sql') or data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'Query cannot be empty'}), 400

    backend = None
    conn = None
    start_time = time.time()

    try:
        backend = get_backend(descriptor)
        conn = backend.connect(descriptor)

        results = backend.execute(conn, raw_query)
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
        backend.execute(conn, backend.liveness_sql)
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