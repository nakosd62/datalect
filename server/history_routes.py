"""
history_routes.py

Read and purge translation history for the current user.

Anonymous (Cloud Run, un-authenticated) visitors are allowed here too -
auth.py's ANONYMOUS_USER_ID_PREFIX gives each browser session its own
distinct "anonymous:<session_id>" identity, so one anonymous visitor's
history is already fully partitioned from every other's (and from any
authenticated user's) at the state_store layer, exactly like their DB
connection/auto-execute preference already is (see
test_config_cloud_run_anonymous_redaction.py's "dont_collide" tests). There
is deliberately no is_anonymous_user() gate here anymore - it would only be
rejecting someone from viewing their own already-isolated history.
"""

from flask import Blueprint, jsonify

from app_config import state_store, log_and_generalize_error
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie

history_bp = Blueprint('history', __name__)


@history_bp.route('/api/history', methods=['GET'])
def get_translation_history():
    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py, and
    # execute_routes.py/translate_routes.py/config_routes.py for the same
    # pattern.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    try:
        rows, stats, total_count = state_store.get_translation_history(user_identity)
        resp = jsonify({
            'success': True,
            'history': rows,
            'stats': stats,
            'total_count': total_count
        })
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to load translation history", e)
        return jsonify({'success': False, 'error': safe_message}), 500


@history_bp.route('/api/history/purge', methods=['DELETE', 'POST'])
def purge_translation_history():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    try:
        state_store.purge_translation_history(user_identity)
        resp = jsonify({
            'success': True,
            'message': 'Translation history purged successfully.'
        })
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to purge translation history", e)
        return jsonify({
            'success': False,
            'error': safe_message
        }), 500