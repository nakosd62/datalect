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
"""

import time

from flask import Blueprint, request, jsonify

from app_config import logger
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str
from backends import get_backend

execute_bp = Blueprint('execute', __name__)


@execute_bp.route('/api/execute', methods=['POST'])
def execute_query():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
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