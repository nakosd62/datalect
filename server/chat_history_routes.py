"""
chat_history_routes.py

Persists the actual chat/turn-navigation conversation state - client.js's
chatStoresByBucket, the in-memory registry behind the back/forward arrows,
the multi-turn context sent to /api/translate, and the rendered result
tabs/summaries - so it survives a page reload or a server restart instead
of starting over empty every time.

Deliberately a separate module/blueprint from history_routes.py: that one
owns /api/history, the "translations" AUDIT LOG (one row per NL->SQL call).
That log is still written to on every translation and still fully queryable
via /api/history and /api/history/purge - it's just no longer surfaced by
the History modal's UI, which now shows THIS module's data instead (one row per
database with saved turns, via GET /api/chat-history/summary below, with a
per-database or delete-all control that's really just save_chat_bucket()
with an empty turns list - see /api/chat-history/save's own docstring).
Unrelated data, a different shape, no purge button in common - this
module's data is the conversation itself.

Every route here resolves user_identity exactly like history_routes.py's
own routes: session_id first (so an anonymous identity is scoped to THIS
browser session, not a freshly-derived one - see auth.py's
get_current_user_identity docstring), then get_current_user_identity(session_id),
then re-applies the session cookie on the way out so a first-time
anonymous visitor's session_id is preserved consistently across every
endpoint that touches it.
"""

from flask import Blueprint, jsonify, request

from app_config import state_store, log_and_generalize_error, CONFIGURED_DBS
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie

chat_history_bp = Blueprint('chat_history', __name__)


def _resolve_bucket_display(bucket_key, preset_by_id, custom_by_key):
    """Maps one bucket_key (see client.js's computeBucketConnectionSuffix())
    to {"kind", "name", "type", "available"} for the History modal's
    database list - never the bucket_key's own raw suffix when that suffix
    could be sensitive (see the "custom-adhoc" branch below).

    "available" is False whenever the bucket's own connection can no
    longer be resolved against this user's CURRENT presets/custom
    connections - a preset removed from DATABASE_PRESETS_FILE since this
    bucket was last written, or a custom connection this user has since
    deleted. Still returned (not dropped) - the whole point of this modal
    is letting old, otherwise-invisible turns actually get cleared out."""
    if bucket_key == 'all':
        return {'kind': 'all', 'name': 'All Pre-Configured Datasets (combined)', 'type': None, 'available': True}
    if bucket_key.startswith('preset:'):
        preset = preset_by_id.get(bucket_key[len('preset:'):])
        return {
            'kind': 'preset',
            'name': preset.get('name') if preset else None,
            'type': preset.get('type') if preset else None,
            'available': preset is not None,
        }
    if bucket_key.startswith('custom-adhoc:'):
        # This bucket's own suffix is the user's raw, never-saved
        # connection URL (see computeBucketConnectionSuffix()'s own
        # docstring in client.js) - for a Postgres/MySQL/MongoDB-style
        # dialect that URL can embed a plaintext password, so it must
        # never be echoed back here, logged, or rendered anywhere in the
        # UI. There's nothing else to resolve a name/type from either -
        # an ad hoc connection was, by definition, never given either.
        return {'kind': 'custom-adhoc', 'name': None, 'type': None, 'available': False}
    if bucket_key.startswith('custom:'):
        custom = custom_by_key.get(bucket_key[len('custom:'):])
        return {
            'kind': 'custom',
            'name': custom.get('name') if custom else None,
            'type': custom.get('type') if custom else None,
            'available': custom is not None,
        }
    # Defensive only - every bucket_key this app itself ever writes matches
    # one of the branches above; a row shaped otherwise could only get here
    # via a manual DB edit or a future, not-yet-handled key shape.
    return {'kind': 'unknown', 'name': None, 'type': None, 'available': False}


@chat_history_bp.route('/api/chat-history/summary', methods=['GET'])
def get_chat_history_summary():
    """Powers the History modal's database list - one row per bucket that
    actually has turns (a bucket cleared via POST /api/chat-history/save
    with turns: [] still has a row, just with turn_count 0, so those are
    filtered out here rather than shown as an empty entry). Deliberately a
    separate, lighter-weight endpoint from GET /api/chat-history: this one
    never sends the turns themselves (results/SQL text, sometimes large)
    back down just to render a list of names and counts."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    try:
        buckets = state_store.get_chat_history(user_identity).get('buckets', {}) or {}
        preset_by_id = {str(db.get('id')): db for db in CONFIGURED_DBS}
        custom_by_key = {
            db.get('connection_key'): db
            for db in state_store.get_db_connections(user_identity)
            if db.get('connection_key')
        }
        summary = []
        for bucket_key, turns in buckets.items():
            turn_count = len(turns or []) // 2
            if turn_count <= 0:
                continue
            entry = {'bucket_key': bucket_key, 'turn_count': turn_count}
            entry.update(_resolve_bucket_display(bucket_key, preset_by_id, custom_by_key))
            summary.append(entry)
        resp = jsonify({'success': True, 'buckets': summary})
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to load chat history summary", e)
        return jsonify({'success': False, 'error': safe_message}), 500


@chat_history_bp.route('/api/chat-history', methods=['GET'])
def get_chat_history():
    """Called once per identity per page-load (see client.js's
    hydrateChatHistoryFromServer()) to restore every bucket this user has
    ever saved, before the UI switches to whichever one is active."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    try:
        result = state_store.get_chat_history(user_identity)
        resp = jsonify({
            'success': True,
            'buckets': result.get('buckets', {}),
            'active_bucket_key': result.get('active_bucket_key', ''),
        })
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to load chat history", e)
        return jsonify({'success': False, 'error': safe_message}), 500


@chat_history_bp.route('/api/chat-history/save', methods=['POST'])
def save_chat_history_bucket():
    """Upserts one bucket's full turn list - called from
    createChatHistoryStore()'s pushTurn() (via its onPersist callback)
    every time ANY bucket receives a new turn, whether or not it's the one
    currently shown on screen (see all-mode's pushTurnIntoBucket()). Never
    changes which bucket is "active" - see /api/chat-history/activate for
    that."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json(silent=True) or {}
    bucket_key = (data.get('bucket_key') or '').strip()
    turns = data.get('turns')
    if not bucket_key or not isinstance(turns, list):
        return jsonify({'success': False, 'error': 'bucket_key and turns (a list) are required.'}), 400
    try:
        state_store.save_chat_bucket(user_identity, bucket_key, turns)
        resp = jsonify({'success': True})
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to save chat history", e)
        return jsonify({'success': False, 'error': safe_message}), 500


@chat_history_bp.route('/api/chat-history/activate', methods=['POST'])
def activate_chat_history_bucket():
    """Records which bucket is "active" - called from
    reconcileActiveHistoryBucket() whenever the client switches to a
    (possibly still-empty) bucket, independent of whether a turn is ever
    pushed into it. Saves nothing about the bucket's own turns - see
    /api/chat-history/save for that."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json(silent=True) or {}
    bucket_key = (data.get('bucket_key') or '').strip()
    if not bucket_key:
        return jsonify({'success': False, 'error': 'bucket_key is required.'}), 400
    try:
        state_store.set_active_chat_bucket(user_identity, bucket_key)
        resp = jsonify({'success': True})
        return apply_session_cookie(resp, session_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to set active chat bucket", e)
        return jsonify({'success': False, 'error': safe_message}), 500
