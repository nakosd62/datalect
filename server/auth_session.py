"""
auth_session.py

The app's own long-lived, signed session cookie - what actually keeps a
signed-in user logged in past the ~1 hour lifetime of a Google ID token.

Until this existed, auth.py's get_current_user_identity() re-verified the
raw Google ID token (a short-lived JWT, capped at about an hour - a fixed
Google policy, not something this app controls) on EVERY single request.
The instant that token lapsed, the very next request failed identity
resolution outright, and the user had to click "Sign in" again - even
though their underlying Google session was still perfectly valid. This is
exactly the mistake most other "Sign in with Google" integrations DON'T
make: Google Sign-In is meant to be a one-time identity assertion at login
time, not an ongoing per-request credential for a whole session.

The fix follows that same standard pattern: once a Bearer token verifies
successfully, this module signs the resulting email into an opaque cookie
value with its own, much longer lifetime (SESSION_MAX_AGE_SECONDS, sliding -
refreshed on every active request, see auth.py's
refresh_auth_session_cookie()). get_current_user_identity() then falls back
to this cookie whenever the Bearer token is missing or has expired, so a
user active at least once within that window never sees a sign-in prompt
again until they explicitly log out (see auth.py's /api/auth/logout route).

Deliberately a standalone module with no import from app_config.py (unlike
auth.py, which does): app_config.py's own startup guard (see its "Startup /
Module Scope Guard" section) needs to check is_session_signing_configured()
BEFORE/independent of auth.py even being imported, and auth.py already
imports FROM app_config - importing auth.py back from app_config.py would
be circular. This mirrors state_store.py's DB_CONFIG_ENCRYPTION_KEY / Fernet
precedent exactly, for the same reason.

The signing key itself is never stored alongside the cookie it protects -
it's read fresh from SESSION_SIGNING_KEY_ENV_VAR on every call (negligible
cost - HMAC-signing/verifying a small payload, no KDF involved), via a real
secret manager in production, a plain .env locally, same as
DB_CONFIG_ENCRYPTION_KEY/GOOGLE_CLIENT_ID/etc (app_config.py). An unset key
just means this cookie is never issued or honored - every request falls
straight through to per-request Bearer-token verification only, exactly
this app's original (pre-this-feature) behavior. That's fine for local dev
(zero configuration needed), but on Cloud Run - where users actually notice
being logged out hourly - app_config.py's startup guard turns a missing key
into a hard failure instead, same posture as the DB_CONFIG_ENCRYPTION_KEY
guard right above it.
"""

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Distinct from crbot_session_id (auth.py's per-browser anonymous-session-
# scoping cookie, which every visitor gets, signed-in or not) and
# crbot_user_id/user_id (a legacy plain-value cookie a reverse proxy/IAP
# setup could already set directly - see auth.py's get_current_user_identity
# docstring). This one is ONLY ever set by this app itself
# (refresh_auth_session_cookie() in auth.py), and only once a Google ID
# token has actually been verified - its value is a signed claim, never a
# raw, spoofable string.
SESSION_COOKIE_NAME = "crbot_auth_session"

# 30 days, sliding - see this module's docstring. itsdangerous embeds the
# signing timestamp in the token itself, so passing this same value to
# both dumps (implicitly, via the timestamp) and loads()'s max_age is what
# enforces it; there's no separate issued_at/exp field of this module's own
# to track or keep in sync.
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

SESSION_SIGNING_KEY_ENV_VAR = "SESSION_SIGNING_KEY"

# Scopes this module's signatures to this one purpose - cheap insurance
# against some unrelated future itsdangerous consumer reusing the same raw
# key value for something else and the two signed payloads being confused
# for one another.
_SALT = "datalect-auth-session"


def _load_serializer():
    """Returns a fresh URLSafeTimedSerializer built from the CURRENT
    SESSION_SIGNING_KEY_ENV_VAR value, or None if it's unset. Mirrors
    state_store.py's _load_cipher() precedent exactly: re-reads the env var
    on every call (negligible cost, no caching) so a changed/rotated key
    takes effect on the very next call with no special re-import/restart
    step needed, and stays permissive (None, not an exception) so purely-
    local dev keeps working with zero configuration - app_config.py's
    startup guard is what turns "no key configured" into a hard failure
    specifically on Cloud Run."""
    raw_key = os.environ.get(SESSION_SIGNING_KEY_ENV_VAR, "").strip()
    if not raw_key:
        return None
    return URLSafeTimedSerializer(raw_key, salt=_SALT)


def is_session_signing_configured():
    """Whether a signing key is configured right now - used by
    app_config.py's startup guard (see its own comment there) to decide
    whether to halt startup when Google auth is enabled on Cloud Run but
    this key is missing."""
    return _load_serializer() is not None


def issue_session_token(email):
    """Signs `email` into an opaque cookie value, or None if no signing key
    is configured or `email` is falsy - the caller (auth.py's
    refresh_auth_session_cookie()) simply skips setting the cookie in that
    case, leaving this app's session-persistence feature silently
    unavailable rather than erroring, exactly like every other
    optional-key-shaped feature in this app (see this module's docstring)."""
    serializer = _load_serializer()
    if not serializer or not email:
        return None
    return serializer.dumps({"email": email})


def read_session_email(raw_cookie_value):
    """Verifies and decodes a cookie value written by issue_session_token()
    above, returning the embedded email, or None if the value is missing,
    was signed under a different (rotated) key, has been tampered with, or
    is older than SESSION_MAX_AGE_SECONDS. Also returns None (rather than
    raising) whenever no signing key is configured at all. Every failure
    mode collapses to the same "just fall through to the caller's next
    identity signal" outcome - never a hard error - since a missing/broken/
    expired session cookie is exactly as unremarkable as never having had
    one."""
    if not raw_cookie_value:
        return None
    serializer = _load_serializer()
    if not serializer:
        return None
    try:
        data = serializer.loads(raw_cookie_value, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("email")
