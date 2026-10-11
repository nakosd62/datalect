"""
share_routes.py

POST /api/share - the header "Share" button's own endpoint. Takes whatever
turn is CURRENTLY on screen (the client already has this in exactly the
shape it needs - see client.js's buildSharePayload()) and freezes it into a
durable, anonymously-readable snapshot keyed by a short, random share_id,
returning a full URL (PUBLIC_BASE_URL or this request's own host, plus that
share_id) the user can copy and hand to anyone.

GET /api/share/<share_id> - reads one back. Public on purpose: a share link
is meant to work for anyone holding it, including a visitor with no
session/identity of their own (see auth.py's own anonymous-identity
fallback) - this route is listed in auth.py's EXEMPT_ENDPOINTS for exactly
that reason, even though in practice the auth guard would already let an
anonymous request through to it regardless (nothing here requires anything
MORE than "has some identity", which every request always has once
GOOGLE_CLIENT_ID/IS_CLOUD_RUN is set - see get_current_user_identity's own
fallback chain).

The STORED payload is a frozen, point-in-time snapshot - the prompt, the
generated SQL, the model/dataset display labels, and the ACTUAL fetched
results/summary/Thoughts & Notes content exactly as they looked at share
time - and this route never executes a query, reads a live schema, or
exposes any database credential to a viewer who opens the link. The payload
is otherwise stored OPAQUELY, the same "don't interpret the caller's blob"
posture chat_history_routes.py's own POST /api/chat-history/save already
takes for a turn list - this route doesn't need to understand client.js's
own turn shape, just bound its size and persist it.

What a VIEWER's browser does with that snapshot is a separate question,
answered entirely client-side (see client.js's own maybeRenderSharedView()):
for a share whose `dataset_ref` names an admin preset or dataset group - a
shared, org-wide connection nobody needs special credentials for - the
viewer's own session is switched to that same dataset (an ordinary POST
/api/config, exactly as if they'd picked it from the connection list
themselves) and the turn is dropped into their OWN chat history, fully
editable and re-runnable from there. For a share whose dataset was a
private/custom connection, no such switch ever happens - connections are
looked up by (user_id, connection_key) (see state_store.py's db_connections
table), so a viewer who never saved that connection simply has no way to
reach it, by construction, not by any check this route performs. Either way
this route's own job stays the same: persist and return the snapshot
exactly as given, never a credential, never a live query.

server.py's own 404 handler is what actually gets a share_id in front of
this route's GET sibling in the first place: a bare-path visit to
agent.datalect.xyz/<share_id> doesn't match any real static file, so it
falls through to that handler, which serves the SAME index.html as "/"
whenever the unmatched path looks like a share_id (see SHARE_ID_PATH_RE
below, imported from there) - client.js's own boot sequence is what
actually calls this GET route once the page loads, by reading
window.location.pathname itself.
"""

import re

from flask import Blueprint, jsonify, request

from app_config import state_store, log_and_generalize_error, PUBLIC_BASE_URL
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie

share_bp = Blueprint('share', __name__)

# Matches the shape generate_share_id() (state_store.py) actually produces -
# but deliberately a little more permissive than SHARE_ID_ALPHABET/
# SHARE_ID_LENGTH's own exact charset/length, same "accept anything
# plausible, let the store lookup be the real authority" posture every
# other id-shaped route param in this app already takes. Used BOTH to
# reject an obviously-malformed share_id early in GET /api/share/<share_id>
# below (no point round-tripping to the state store for something that can
# never have been generated) AND by server.py's 404 handler, which imports
# this same pattern to decide whether an unmatched bare path is plausibly a
# share link rather than a missing static asset or a typo'd URL - one
# definition, so the two can never drift apart.
SHARE_ID_PATH_RE = re.compile(r'^[a-z0-9]{6,24}$')

# Hard cap on the whole POST body - unlike report_routes.py's per-field
# _MAX_FIELD_CHARS, this can't truncate individual fields and still be
# useful (a truncated result set/SQL string would make the shared view
# itself wrong, not just long), so instead the whole request is rejected
# outright once it's clearly larger than any real turn should ever be. 4 MB
# comfortably covers a large result table plus visualizations/Thoughts &
# Notes text; an ordinary turn is a tiny fraction of that.
_MAX_SHARE_BODY_BYTES = 4 * 1024 * 1024

# Hard cap on each plain display-label field (database_name/database_type/
# provider/model) - these are short badge text in every normal case; this
# just guards against something unbounded ending up here by mistake.
_MAX_LABEL_CHARS = 200


def _truncated_label(value):
    text = (value or '').strip() if isinstance(value, str) else ''
    return text[:_MAX_LABEL_CHARS]


# Which dataset a shared turn used, besides its plain display labels - this
# is what maybeRenderSharedView() (client.js) reads to decide whether a
# viewer's own session can be switched to the SAME dataset (an admin preset
# or dataset group - shared, org-wide, nobody needs a personal credential
# for it) or must stay on whatever the viewer already had active (a private/
# custom connection - see this module's own docstring on why that's safe by
# construction rather than by anything checked here). Treated as a pure UI
# hint, never as an authorization decision in its own right: whatever a
# client claims here, the actual dataset switch still goes through POST
# /api/config's own independent validation (a bogus/stale preset or group id
# there just resolves to nothing, same as it would for any other request),
# and a viewer can never reach a private connection this way regardless of
# what dataset_ref says, since that lookup is keyed by THEIR OWN user_id,
# never the sharer's. "custom" is both the explicit value a share-creating
# client sends for its own private connection AND the default this route
# falls back to for anything it doesn't recognize - "assume nothing can be
# switched to" is the safe direction to fail in if this ever gets a
# malformed value.
_VALID_DATASET_REF_KINDS = {'preset', 'group'}


def _sanitized_dataset_ref(value):
    if isinstance(value, dict):
        kind = value.get('kind')
        ref_id = value.get('id')
        if kind in _VALID_DATASET_REF_KINDS and isinstance(ref_id, str) and ref_id.strip():
            return {'kind': kind, 'id': _truncated_label(ref_id)}
    return {'kind': 'custom', 'id': None}


def build_share_url(share_id):
    """The full, copyable URL for `share_id` - PUBLIC_BASE_URL if a deployer
    configured one (see that constant's own docstring for why that's
    sometimes necessary), else this request's own host. Only ever called
    from inside a request context (POST /api/share below)."""
    base = PUBLIC_BASE_URL or request.host_url.rstrip('/')
    return f"{base}/{share_id}"


@share_bp.route('/api/share', methods=['POST'])
def create_share():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)

    if request.content_length and request.content_length > _MAX_SHARE_BODY_BYTES:
        resp = jsonify({
            'success': False,
            'error': 'This result is too large to share as a link. Try sharing a smaller result set.',
        })
        return apply_session_cookie(resp, session_id), 413

    data = request.get_json(silent=True) or {}
    model_entry = data.get('model_entry')
    if not isinstance(model_entry, dict):
        resp = jsonify({'success': False, 'error': 'model_entry (an object) is required.'})
        return apply_session_cookie(resp, session_id), 400
    user_entry = data.get('user_entry')

    # Stored opaquely (same posture save_chat_bucket() already takes for
    # `turns`) - this route doesn't parse/validate model_entry's own
    # internal shape, just the handful of plain display labels alongside
    # it, and the overall body-size cap above.
    payload = {
        'user_entry': user_entry if isinstance(user_entry, dict) else None,
        'model_entry': model_entry,
        'database_name': _truncated_label(data.get('database_name')),
        'database_type': _truncated_label(data.get('database_type')),
        'provider': _truncated_label(data.get('provider')),
        'model': _truncated_label(data.get('model')),
        'in_scope_mode': 'group' if data.get('in_scope_mode') == 'group' else 'single',
        'dataset_ref': _sanitized_dataset_ref(data.get('dataset_ref')),
    }

    try:
        share_id = state_store.create_share(payload, user_identity)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to create share link", e)
        resp = jsonify({'success': False, 'error': safe_message})
        return apply_session_cookie(resp, session_id), 500

    if not share_id:
        safe_message = log_and_generalize_error(
            "Failed to create share link", RuntimeError("state_store.create_share returned no id")
        )
        resp = jsonify({'success': False, 'error': safe_message})
        return apply_session_cookie(resp, session_id), 500

    resp = jsonify({'success': True, 'share_id': share_id, 'url': build_share_url(share_id)})
    return apply_session_cookie(resp, session_id)


@share_bp.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    # No session/identity resolution needed at all - unlike every other
    # route in this app, a share's visibility never depends on WHO is
    # asking (see this module's own docstring on why that's the point).
    # Still not exempt from a stray old session cookie accompanying the
    # request, so nothing here touches cookies either way.
    if not SHARE_ID_PATH_RE.match(share_id or ''):
        return jsonify({'success': False, 'error': 'No such shared link.'}), 404
    try:
        payload = state_store.get_share(share_id)
    except Exception as e:
        safe_message = log_and_generalize_error("Failed to load shared link", e)
        return jsonify({'success': False, 'error': safe_message}), 500
    if payload is None:
        return jsonify({'success': False, 'error': 'No such shared link.'}), 404
    return jsonify({
        'success': True,
        'user_entry': payload.get('user_entry'),
        'model_entry': payload.get('model_entry'),
        'database_name': payload.get('database_name') or '',
        'database_type': payload.get('database_type') or '',
        'provider': payload.get('provider') or '',
        'model': payload.get('model') or '',
        'in_scope_mode': payload.get('in_scope_mode') or 'single',
        # .get(...) rather than a bare key lookup: a share created before
        # dataset_ref existed (schema_version 1, same as every row today)
        # simply has no such key at all - _sanitized_dataset_ref's own
        # "custom"/None fallback is exactly the right default for one of
        # those too, not just for a malformed value, so there's no need for
        # a schema_version bump over adding this field.
        'dataset_ref': payload.get('dataset_ref') or {'kind': 'custom', 'id': None},
    })
