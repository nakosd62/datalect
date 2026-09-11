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

from flask import Blueprint, request, jsonify, g

from app_config import GOOGLE_CLIENT_ID, AUTH_ENABLED, IS_CLOUD_RUN, logger
from auth_session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    issue_session_token,
    read_session_email,
)
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

# How long the crbot_session_id cookie (get_or_create_session_id/
# apply_session_cookie below) lives - was a fixed 24h, which meant an
# anonymous visitor's DB connection choice, auto-execute preference, and
# (as of the persisted chat-history feature) entire conversation history
# were all silently orphaned every single day, forcing a fresh identity on
# their very next visit regardless of how recently they'd been active.
# Set to 400 days - not a round number chosen for its own sake, but the
# actual ceiling: Chrome (since 2023) and every other major browser now
# hard-caps a cookie's Max-Age/Expires at 400 days regardless of what a
# server asks for, so this is genuinely "as long-lived as a cookie can be
# made", the closest thing to "forever" this mechanism allows. A visitor
# who returns within this window keeps the exact same anonymous identity
# (and therefore every bucket of history, every saved preference) they had
# before; one who doesn't returns as a brand-new anonymous identity, same
# as today, just after a much longer gap.
ANONYMOUS_SESSION_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60


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
    # 1. Bearer Token in Authorization Header (Google ID Token) - the
    # freshest, most authoritative signal when present and valid.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            if GOOGLE_CLIENT_ID:
                email = None
                try:
                    idinfo = id_token.verify_oauth2_token(
                        token, google_requests.Request(), GOOGLE_CLIENT_ID
                    )
                    email = idinfo.get("email")
                except Exception:
                    logger.warning("Google ID token verification failed", exc_info=True)
                if email:
                    # Signals refresh_auth_session_cookie() (this module's
                    # own app.after_request hook, registered in server.py)
                    # to (re)issue this app's own long-lived session cookie
                    # for this email - see auth_session.py's module
                    # docstring. Every ACTIVE request that resolves an
                    # identity this way pushes that cookie's expiry back
                    # out, which is what makes its window sliding rather
                    # than a fixed 30 days from first sign-in.
                    g.auth_session_refresh_email = email
                    return email
                # Falls through to 1b below rather than failing outright -
                # an expired/invalid Google token no longer means "logged
                # out" by itself; the app's own session cookie (if this
                # browser still carries a valid one) is exactly what covers
                # this gap. See auth_session.py's module docstring.
            else:
                return f"token:{token[:32]}"

    # 1b. This app's own long-lived, signed session cookie (see
    # auth_session.py's module docstring) - what actually keeps a user
    # logged in past the Bearer token's own ~1hr Google-imposed expiry.
    # Only reached when the Bearer branch above didn't already resolve an
    # identity: a fresh, still-valid Google token always wins when present.
    session_email = read_session_email(request.cookies.get(SESSION_COOKIE_NAME))
    if session_email:
        g.auth_session_refresh_email = session_email
        return session_email

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
    'auth.logout',
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
        max_age=ANONYMOUS_SESSION_COOKIE_MAX_AGE_SECONDS
    )
    return response


def refresh_auth_session_cookie(response):
    """Registered as `app.after_request` in server.py, right alongside
    enforce_authentication's before_request registration. (Re-)issues the
    app's own long-lived session cookie (see auth_session.py's module
    docstring) whenever THIS request resolved an identity via a fresh
    Bearer token or an already-valid session cookie -
    get_current_user_identity() stashes the email on flask.g precisely so
    this hook can find it after the fact, since a before_request/route
    handler doesn't have the outgoing `response` object to set a cookie on
    directly. This is what makes the session sliding: every active request
    pushes the cookie's expiry back out another SESSION_MAX_AGE_SECONDS,
    rather than counting down from the very first sign-in regardless of
    activity. A request that never resolved an identity this way at all
    (anonymous, IAP/legacy-cookie, local dev, or no signing key configured -
    see issue_session_token()) leaves `g` unset and this is a no-op."""
    email = getattr(g, 'auth_session_refresh_email', None)
    if email:
        token = issue_session_token(email)
        if token:
            response.set_cookie(
                SESSION_COOKIE_NAME,
                token,
                httponly=True,
                samesite='Lax',
                max_age=SESSION_MAX_AGE_SECONDS,
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


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    """Clears this app's own long-lived session cookie (see
    auth_session.py's module docstring) - without this, a browser that
    still carries that cookie would keep resolving to the same signed-in
    identity via get_current_user_identity()'s session-cookie fallback even
    after client.js has cleared its own locally-held Google ID token,
    silently undoing the logout on the very next request. Deliberately
    does NOT call get_current_user_identity() at all - "log me out" has to
    work even when the credential being logged out of is already stale/
    invalid, and there's nothing here that needs to know who was signed in
    to begin with. Reachable regardless of auth state - see
    enforce_authentication()'s own '/api/auth/' path-prefix exemption
    (EXEMPT_ENDPOINTS below lists it too, for the same belt-and-suspenders
    reason 'auth.get_current_user_status' is listed explicitly alongside
    that same prefix rule)."""
    resp = jsonify({'success': True})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp