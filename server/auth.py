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


def get_or_create_session_id():
    """Retrieves or creates a session ID cookie or header."""
    session_id = request.cookies.get('crbot_session_id') or request.headers.get('X-Session-ID')
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id


def get_current_user_identity():
    """
    Extracts authenticated user identity from Bearer Tokens (verified via Google OAuth),
    GCP Identity-Aware Proxy (IAP) headers, or auth cookies.
    Falls back to 'global' state key when running locally.
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

    # 4. If auth is enabled (Cloud Run), unauthenticated requests return None
    if GOOGLE_CLIENT_ID or IS_CLOUD_RUN:
        return None

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
    user_identity = get_current_user_identity()
    session_id = get_or_create_session_id()
    is_authenticated = bool(user_identity and user_identity != session_id)

    resp = jsonify({
        'authenticated': is_authenticated,
        'user_id': user_identity,
        'session_id': session_id,
        'auth_required': AUTH_ENABLED
    })
    return apply_session_cookie(resp, session_id)