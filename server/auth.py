"""
auth.py

Session/identity resolution and the authentication guard. Also owns the
one auth-related route, /api/auth/me, which lets the frontend check
"who am I" without triggering the auth guard itself (it's in the exempt
list below).

server.py registers `enforce_authentication` as an `app.before_request`
hook and registers `auth_bp` as a blueprint - this module doesn't reach
into `app` directly so it stays easy to unit test in isolation.
"""

import uuid

from flask import Blueprint, request, jsonify

from app_config import GOOGLE_CLIENT_ID, AUTH_ENABLED, IS_CLOUD_RUN, logger
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

auth_bp = Blueprint('auth', __name__)

# Identity prefix used for requests on Cloud Run / AUTH_ENABLED deployments
# that don't carry any verified identity (no Bearer token, no IAP header,
# no auth cookie). Rather than rejecting these requests outright, they're
# treated as an anonymous user: they get a fully working session
# (translate/execute/default DB all work), but routes that are inherently
# user-scoped (custom DB connections, translation history) explicitly
# check for this prefix and refuse it - see `is_anonymous_user` below.
#
# One identity PER BROWSER SESSION (ANONYMOUS_USER_ID_PREFIX + session id -
# see get_current_user_identity), not a single value every unauthenticated
# visitor shares. Previously this was one bare constant ("anonymous") used
# for literally every anonymous request, which meant concurrent anonymous
# visitors on Cloud Run all read/wrote the exact same state_store row: one
# visitor picking a different preset DB, or toggling auto-execute, silently
# changed it for every other anonymous visitor mid-session. Scoping it per
# session fixes that while keeping the authorization behavior identical -
# is_anonymous_user() still recognizes every one of these as "not really
# logged in".
ANONYMOUS_USER_ID_PREFIX = "anonymous:"


def is_anonymous_user(user_identity):
    """True if `user_identity` represents an anonymous (unauthenticated)
    identity - i.e. a Cloud Run / AUTH_ENABLED request with no verified
    login. One such identity exists per browser session now (see
    ANONYMOUS_USER_ID_PREFIX), so this checks the prefix rather than an
    exact match against a single shared value."""
    return bool(user_identity) and user_identity.startswith(ANONYMOUS_USER_ID_PREFIX)


def get_or_create_session_id():
    """Retrieves or creates a session ID cookie or header."""
    session_id = request.cookies.get('crbot_session_id') or request.headers.get('X-Session-ID')
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id


def get_current_user_identity(session_id=None):
    """
    Extracts authenticated user identity from Bearer Tokens (verified via Google OAuth),
    GCP Identity-Aware Proxy (IAP) headers, or auth cookies.
    Falls back to 'global' state key when running locally, or - when auth
    is enabled/Cloud Run but the request carries no verified identity - to
    an anonymous identity scoped to this browser's session (see
    ANONYMOUS_USER_ID_PREFIX) rather than one shared by every anonymous
    visitor.

    `session_id`, when the caller has one, should be the SAME value it's
    using for the crbot_session_id cookie (get_or_create_session_id()'s
    return value) - resolve it once per request and pass it through here,
    rather than letting this function derive its own independently.
    get_or_create_session_id() falls back to a freshly-generated UUID
    whenever the request carries no session cookie yet, so calling it
    twice in one request without a cookie present returns two DIFFERENT
    values - if this function generated its own while the caller sets a
    different one on the response cookie, a single browser's session state
    (active DB, auto-execute) would end up split across two different
    anonymous identities on its very first request. Callers that only need
    a truthy signal (e.g. the enforce_authentication guard) can omit it.
    """
    # 1. Bearer Token in Authorization Header (Google ID Token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            if GOOGLE_CLIENT_ID:
                try:
                    idinfo = id_token.verify_oauth2_token(
                        token, google_requests.Request(), GOOGLE_CLIENT_ID
                    )
                    return idinfo.get("email")
                except Exception:
                    logger.warning("Google ID token verification failed", exc_info=True)
                    return None
            else:
                return f"token:{token[:32]}"

    # 2. GCP / IAP / Custom Identity Headers
    iap_user = request.headers.get("X-Goog-Authenticated-User-Email") or request.headers.get("X-User-Email")
    if iap_user:
        return iap_user.replace("accounts.google.com:", "").strip()

    user_id_header = request.headers.get("X-User-ID")
    if user_id_header:
        return user_id_header.strip()

    # 3. Auth Cookie
    auth_cookie = request.cookies.get("crbot_user_id") or request.cookies.get("user_id")
    if auth_cookie:
        return auth_cookie.strip()

    # 4. If auth is enabled (Cloud Run), requests carrying no verified
    # identity are treated as anonymous rather than being rejected outright
    # - scoped to this browser's session so concurrent anonymous visitors
    # don't collide on the same state_store row (see
    # ANONYMOUS_USER_ID_PREFIX above). Falls back to resolving its own
    # session_id only if the caller didn't already have one on hand.
    if GOOGLE_CLIENT_ID or IS_CLOUD_RUN:
        return f"{ANONYMOUS_USER_ID_PREFIX}{session_id or get_or_create_session_id()}"

    # 5. Local fallback -> Single 'global' user identity
    return "global"


# List of Flask endpoint names that do not require authentication.
# Note: blueprint routes are registered as "<blueprint_name>.<function_name>",
# e.g. "auth.get_current_user_status" - keep this in sync with the route
# function names below and in config_routes.py.
EXEMPT_ENDPOINTS = {
    'index',
    'auth.get_current_user_status',
    'static',
    'config.handle_config',
    # Public, static-for-the-life-of-the-process build-id check (see
    # config_routes.py's get_client_version docstring) - no session/user
    # concept applies to it at all, same posture as 'static' above, and
    # client.js polls it periodically in the background regardless of
    # whether the visitor is signed in.
    'config.get_client_version',
    'login',
    'google_login',
    'oauth_callback',
    'auth_login'
}


def enforce_authentication():
    """Registered as `app.before_request` in server.py."""
    # 1. Allow static assets and options preflight requests (CORS)
    if request.method == 'OPTIONS' or request.endpoint == 'static':
        return

    # 2. Allow any request path starting with authentication endpoints (e.g., /api/auth/*)
    if request.path.startswith('/api/auth/') or request.path in ['/', '/login']:
        return

    # 3. Allow explicit exempt endpoints
    if request.endpoint in EXEMPT_ENDPOINTS or request.endpoint is None:
        return

    # 4. Enforce auth for all other routes if running on Cloud Run or AUTH_ENABLED is True
    if IS_CLOUD_RUN or AUTH_ENABLED:
        user_identity = get_current_user_identity()
        if not user_identity:
            return jsonify({'error': 'Unauthorized: Authentication required'}), 401


def apply_session_cookie(response, session_id):
    response.set_cookie(
        'crbot_session_id',
        session_id,
        httponly=True,
        samesite='Lax',
        max_age=86400
    )
    return response


# --- Auth Verification Endpoint ---
@auth_bp.route('/api/auth/me', methods=['GET'])
def get_current_user_status():
    # session_id resolved BEFORE get_current_user_identity() and passed
    # into it - see that function's docstring for why the order matters
    # (an anonymous identity must embed the exact same session_id this
    # response's cookie carries, or a fresh browser's very first request
    # would derive two different session ids and split its own state).
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    is_authenticated = bool(
        user_identity and user_identity != session_id and not is_anonymous_user(user_identity)
    )

    resp = jsonify({
        'authenticated': is_authenticated,
        'user_id': user_identity,
        'session_id': session_id,
        'auth_required': AUTH_ENABLED
    })
    return apply_session_cookie(resp, session_id)