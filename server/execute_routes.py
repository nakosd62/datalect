"""
execute_routes.py

Runs client-submitted SQL against the resolved connection and returns
results. This is the one endpoint that intentionally returns raw database
error messages to the client (see the comment in the except block) - it's
a SQL runner, and the user needs the actual Postgres error to fix their
query, same as any SQL client would show them.
"""

import time

from flask import Blueprint, request, jsonify
import sqlparse

from app_config import logger
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_db_connection

execute_bp = Blueprint('execute', __name__)


@execute_bp.route('/api/execute', methods=['POST'])
def execute_query():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    data = request.get_json() or {}

    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    raw_query = (data.get('sql') or data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'Query cannot be empty'}), 400

    conn = None
    start_time = time.time()

    try:
        conn = get_db_connection(conn_str, user_identity)
        conn.autocommit = True

        statements = [s.strip() for s in sqlparse.split(raw_query) if s.strip()]
        results = []
        total_row_count = 0

        with conn.cursor() as cursor:
            for stmt in statements:
                stmt_clean = stmt.rstrip(';').strip()
                if not stmt_clean:
                    continue

                cursor.execute(stmt_clean)
                row_count = cursor.rowcount

                columns = None
                rows = None

                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = []
                    for r in cursor.fetchall():
                        row_dict = {}
                        for idx, col in enumerate(columns):
                            val = r[idx]
                            if hasattr(val, 'isoformat'):
                                val = val.isoformat()
                            elif hasattr(val, 'to_eng_string'):
                                val = float(val)
                            elif isinstance(val, bytes):
                                val = val.decode('utf-8', errors='replace')
                            elif type(val).__name__ == 'Decimal':
                                val = float(val)
                            row_dict[col] = val
                        rows.append(row_dict)
                    count = len(rows)
                else:
                    count = row_count if row_count >= 0 else 0

                total_row_count += count

                results.append({
                    'statement': stmt_clean,
                    'columns': columns,
                    'rows': rows,
                    'rowCount': count
                })

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
        # SQL the user themselves supplied/approved, so the Postgres error
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
        if conn:
            conn.close()