"""
history_routes.py

Read and purge translation history for the current user.
"""

from flask import Blueprint, jsonify

from app_config import state_store, log_and_generalize_error
from auth import get_current_user_identity

history_bp = Blueprint('history', __name__)


@history_bp.route('/api/history', methods=['GET'])
def get_translation_history():
    user_identity = get_current_user_identity()
    try:
        rows, stats, total_count = state_store.get_translation_history(user_identity)
        return jsonify({
            'success': True,
            'history': rows,
            'stats': stats,
            'total_count': total_count
        })
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to load translation history", e)
        return jsonify({'success': False, 'error': safe_message}), 500


@history_bp.route('/api/history/purge', methods=['DELETE', 'POST'])
def purge_translation_history():
    user_identity = get_current_user_identity()
    try:
        state_store.purge_translation_history(user_identity)
        return jsonify({
            'success': True,
            'message': 'Translation history purged successfully.'
        })
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to purge translation history", e)
        return jsonify({
            'success': False,
            'error': safe_message
        }), 500