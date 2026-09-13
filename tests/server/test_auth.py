"""
auth.py: identity resolution precedence, the anonymous-user concept, and
the enforce_authentication guard. Uses app_env/app_factory (see conftest)
since get_current_user_identity() reads from Flask's `request` context.
"""

import pytest


def test_local_fallback_identity_is_global_when_auth_disabled(app_env):
    with app_env.app_config.app.test_request_context('/api/config'):
        assert app_env.auth.get_current_user_identity() == "global"


def test_auth_cookie_wins_when_present(app_env):
    with app_env.app_config.app.test_request_context('/api/config', headers={"Cookie": "crbot_user_id=alice@example.com"}):
        assert app_env.auth.get_current_user_identity() == "alice@example.com"


def test_user_id_header_is_honored(app_env):
    with app_env.app_config.app.test_request_context('/api/config', headers={"X-User-ID": " bob@example.com "}):
        assert app_env.auth.get_current_user_identity() == "bob@example.com"


def test_iap_header_strips_accounts_google_com_prefix(app_env):
    with app_env.app_config.app.test_request_context(
        '/api/config', headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:carol@example.com"}
    ):
        assert app_env.auth.get_current_user_identity() == "carol@example.com"


def test_bearer_token_without_google_client_id_configured_returns_opaque_identity(app_env):
    # GOOGLE_CLIENT_ID isn't set in this env - no ID-token verification is
    # attempted, so a bearer token still yields *some* stable identity
    # rather than silently falling through to a weaker signal.
    with app_env.app_config.app.test_request_context(
        '/api/config', headers={"Authorization": "Bearer some-raw-token-value"}
    ):
        identity = app_env.auth.get_current_user_identity()
        assert identity.startswith("token:")


def test_no_identity_signals_and_auth_disabled_falls_back_to_global(app_env):
    with app_env.app_config.app.test_request_context('/api/config'):
        assert app_env.auth.get_current_user_identity() == "global"


def test_anonymous_identity_used_when_auth_enabled_but_no_identity_given(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    with env.app_config.app.test_request_context('/api/config'):
        identity = env.auth.get_current_user_identity()
        assert identity.startswith(env.auth.ANONYMOUS_USER_ID_PREFIX)
        assert env.auth.is_anonymous_user(identity) is True


def test_anonymous_identity_is_scoped_to_the_session_id_passed_in(app_factory):
    # The whole point of ANONYMOUS_USER_ID_PREFIX being a prefix rather
    # than a single constant: two different browser sessions get two
    # different (but both still "anonymous") identities, so their DB
    # selection/auto-execute state doesn't collide - see auth.py's comment
    # above ANONYMOUS_USER_ID_PREFIX for the bug this fixes.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    with env.app_config.app.test_request_context('/api/config'):
        identity_a = env.auth.get_current_user_identity(session_id="session-aaa")
        identity_b = env.auth.get_current_user_identity(session_id="session-bbb")
        assert identity_a != identity_b
        assert identity_a == "anonymous:session-aaa"
        assert identity_b == "anonymous:session-bbb"
        assert env.auth.is_anonymous_user(identity_a) is True
        assert env.auth.is_anonymous_user(identity_b) is True


def test_anonymous_identity_without_an_explicit_session_id_falls_back_to_get_or_create(app_factory):
    # Callers that don't have a session_id on hand (or don't need a stable
    # one - e.g. the enforce_authentication guard) still get a valid
    # anonymous identity, derived internally.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    with env.app_config.app.test_request_context(
        '/api/config', headers={"Cookie": "crbot_session_id=cookie-session-id"}
    ):
        identity = env.auth.get_current_user_identity()
        assert identity == "anonymous:cookie-session-id"


def test_cookie_identity_still_wins_even_when_auth_enabled(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    with env.app_config.app.test_request_context('/api/config', headers={"Cookie": "crbot_user_id=dave@example.com"}):
        assert env.auth.get_current_user_identity() == "dave@example.com"


def test_is_anonymous_user_false_for_real_identities(app_env):
    assert app_env.auth.is_anonymous_user("alice@example.com") is False
    assert app_env.auth.is_anonymous_user("global") is False
    assert app_env.auth.is_anonymous_user(None) is False


# --- enforce_authentication guard, via real HTTP requests ---------------------

def test_config_get_works_without_auth_when_auth_disabled(client):
    resp = client.get('/api/config')
    assert resp.status_code == 200


def test_translate_route_is_reachable_without_auth_when_disabled(client):
    # No API key configured in this env -> a 400 from the route itself,
    # not a 401 from the auth guard - proves the guard let it through.
    resp = client.post('/api/translate', json={"prompt": "show users"})
    assert resp.status_code == 400
    assert "Google API key" in resp.get_json()["error"]


def test_auth_me_endpoint_is_always_exempt(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/api/auth/me')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "authenticated" in data
    assert data["auth_required"] is True


def test_protected_route_not_rejected_without_identity_when_auth_enabled(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    # get_current_user_identity() falls back to an anonymous:<session>
    # identity (a truthy string) whenever GOOGLE_CLIENT_ID is set, so the
    # enforce_authentication guard itself never actually 401s anonymous
    # requests in this app. There is currently no remaining
    # is_anonymous_user() rejection anywhere at the route level either -
    # saving a custom DB connection used to gate on it, and is now fully
    # un-gated once its per-session "anonymous:<session_id>" identity gives
    # it the same isolation an authenticated user's identity would (see
    # test_config_custom_connections.py). This exercises the
    # custom-connection save specifically, to prove the old 403 is gone.
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200


def test_config_route_is_always_exempt_from_auth_guard(app_factory):
    # config.handle_config is explicitly in EXEMPT_ENDPOINTS so anonymous
    # Cloud Run visitors can still load enough config to pick a preset.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/api/config')
    assert resp.status_code == 200


def test_static_and_index_routes_never_require_auth(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/')
    assert resp.status_code == 200


def test_apply_session_cookie_sets_httponly_cookie(app_env):
    with app_env.app_config.app.test_request_context('/api/config'):
        from flask import jsonify
        resp = app_env.auth.apply_session_cookie(jsonify({"ok": True}), "sess-123")
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "crbot_session_id=sess-123" in cookie_header
        assert "HttpOnly" in cookie_header


def test_apply_session_cookie_max_age_is_400_days_not_the_old_24h(app_env):
    # Was a fixed 86400 (24h) - extended so an anonymous visitor's DB
    # selection/auto-execute preference and (as of the persisted
    # chat-history feature) their whole conversation history aren't
    # orphaned every single day. 400 days specifically because that's the
    # actual ceiling every major browser enforces on Set-Cookie's Max-Age
    # regardless of what a server asks for (see
    # ANONYMOUS_SESSION_COOKIE_MAX_AGE_SECONDS's own comment in auth.py) -
    # not an arbitrary round number.
    with app_env.app_config.app.test_request_context('/api/config'):
        from flask import jsonify
        resp = app_env.auth.apply_session_cookie(jsonify({"ok": True}), "sess-123")
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert f"Max-Age={app_env.auth.ANONYMOUS_SESSION_COOKIE_MAX_AGE_SECONDS}" in cookie_header
        assert app_env.auth.ANONYMOUS_SESSION_COOKIE_MAX_AGE_SECONDS == 400 * 24 * 60 * 60


# --- App-owned long-lived session cookie (auth_session.py) --------------------
#
# These exercise the actual fix for "Google auto-logs users off roughly
# hourly": get_current_user_identity() re-verifying the Bearer Google ID
# token on every request used to be the ONLY identity signal - the instant
# that ~1hr-lifetime token lapsed, every request failed identity resolution.
# Now a successful Bearer verification also mints this app's own long-lived,
# signed session cookie (crbot_auth_session), and a missing/expired/invalid
# Bearer token falls through to that cookie instead of failing outright. See
# auth_session.py's module docstring and auth.py's refresh_auth_session_cookie().
#
# None of this exercises real Google ID-token verification (no test here has
# real Google credentials, nor should it) - every test patches
# `id_token.verify_oauth2_token` directly, the same seam auth.py itself calls
# through.

SESSION_TEST_ENV = {
    "GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com",
}


def _env_with_signing_key():
    from helpers import FAKE_SESSION_SIGNING_KEY
    return {**SESSION_TEST_ENV, "SESSION_SIGNING_KEY": FAKE_SESSION_SIGNING_KEY}


def _mock_bearer_success(monkeypatch, env, email):
    """Patches auth.py's `id_token.verify_oauth2_token` (the exact seam
    get_current_user_identity() calls through) to succeed with `email`."""
    monkeypatch.setattr(
        env.auth.id_token, "verify_oauth2_token",
        lambda token, request, client_id: {"email": email},
    )


def _mock_bearer_failure(monkeypatch, env):
    """Same seam as _mock_bearer_success, but simulating an expired/invalid
    Google ID token - exactly what happens roughly once an hour today."""
    def _raise(token, request, client_id):
        raise ValueError("Token used too late")
    monkeypatch.setattr(env.auth.id_token, "verify_oauth2_token", _raise)


def _set_cookie_headers(resp):
    return resp.headers.getlist("Set-Cookie")


def test_successful_bearer_verification_issues_session_cookie(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    _mock_bearer_success(monkeypatch, env, "alice@example.com")

    resp = env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["user_id"] == "alice@example.com"

    cookies = _set_cookie_headers(resp)
    session_cookie = next((c for c in cookies if c.startswith(env.auth_session.SESSION_COOKIE_NAME + "=")), None)
    assert session_cookie is not None
    assert "HttpOnly" in session_cookie


def test_no_session_cookie_issued_when_signing_key_unset(app_factory, monkeypatch):
    # SESSION_TEST_ENV alone - no SESSION_SIGNING_KEY - matches this app's
    # original (pre-this-feature) behavior: Bearer verification still works
    # per-request, but no durable session is ever established.
    env = app_factory(env=SESSION_TEST_ENV)
    _mock_bearer_success(monkeypatch, env, "alice@example.com")

    resp = env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})
    assert resp.status_code == 200
    assert resp.get_json()["authenticated"] is True

    cookies = _set_cookie_headers(resp)
    assert not any(c.startswith(env.auth_session.SESSION_COOKIE_NAME + "=") for c in cookies)


def test_bearer_failure_falls_back_to_valid_session_cookie(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    _mock_bearer_success(monkeypatch, env, "alice@example.com")
    first = env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})
    assert first.get_json()["authenticated"] is True

    # Now simulate the Google ID token having expired - env.client's cookie
    # jar still carries the crbot_auth_session cookie from the first
    # request, so identity should resolve from THAT instead of 401ing or
    # silently degrading to anonymous.
    _mock_bearer_failure(monkeypatch, env)
    second = env.client.get('/api/auth/me', headers={"Authorization": "Bearer now-expired-token"})
    assert second.status_code == 200
    data = second.get_json()
    assert data["authenticated"] is True
    assert data["user_id"] == "alice@example.com"


def test_no_bearer_token_but_valid_session_cookie_resolves_and_refreshes(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    _mock_bearer_success(monkeypatch, env, "alice@example.com")
    env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})

    # No Authorization header at all this time - a browser tab reloaded
    # after the local Google token/JS state was long gone, but the durable
    # cookie is still there.
    resp = env.client.get('/api/auth/me')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["user_id"] == "alice@example.com"

    # Sliding renewal: an active request via the cookie alone still
    # refreshes the cookie's expiry, per refresh_auth_session_cookie().
    cookies = _set_cookie_headers(resp)
    assert any(c.startswith(env.auth_session.SESSION_COOKIE_NAME + "=") for c in cookies)


def test_tampered_session_cookie_is_rejected(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    env.client.set_cookie(env.auth_session.SESSION_COOKIE_NAME, "not-a-real-signed-value")

    resp = env.client.get('/api/auth/me')
    assert resp.status_code == 200
    data = resp.get_json()
    # Falls all the way through to the anonymous fallback (GOOGLE_CLIENT_ID
    # is set) rather than resolving to any real identity, and rather than
    # erroring.
    assert data["authenticated"] is False
    assert env.auth.is_anonymous_user(data["user_id"]) is True


def test_session_cookie_signed_under_a_different_key_is_rejected(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    other_token = env.auth_session.URLSafeTimedSerializer(
        "a-completely-different-key", salt=env.auth_session._SALT
    ).dumps({"email": "eve@example.com"})
    env.client.set_cookie(env.auth_session.SESSION_COOKIE_NAME, other_token)

    resp = env.client.get('/api/auth/me')
    data = resp.get_json()
    assert data["authenticated"] is False
    assert data["user_id"] != "eve@example.com"


def test_logout_clears_session_cookie_and_ends_the_session(app_factory, monkeypatch):
    env = app_factory(env=_env_with_signing_key())
    _mock_bearer_success(monkeypatch, env, "alice@example.com")
    env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})

    logout_resp = env.client.post('/api/auth/logout')
    assert logout_resp.status_code == 200
    assert logout_resp.get_json()["success"] is True
    cookies = _set_cookie_headers(logout_resp)
    cleared = next((c for c in cookies if c.startswith(env.auth_session.SESSION_COOKIE_NAME + "=")), None)
    assert cleared is not None
    # delete_cookie() expires it immediately rather than merely emptying
    # the value - both this and an explicit Max-Age=0 are valid browser
    # signals to drop it, but assert the actual mechanism Flask uses here.
    assert ("Expires=Thu, 01 Jan 1970" in cleared) or ("Max-Age=0" in cleared)

    # No Bearer token on this next request either - without a live session
    # cookie, identity must NOT still resolve to alice.
    after = env.client.get('/api/auth/me')
    data = after.get_json()
    assert data["user_id"] != "alice@example.com"


def test_logout_is_reachable_without_any_prior_identity(app_factory):
    # "Log me out" has to work even when there was nothing valid to log out
    # of (stale/absent cookie) - see auth.py's logout() docstring.
    env = app_factory(env=_env_with_signing_key())
    resp = env.client.post('/api/auth/logout')
    assert resp.status_code == 200
    assert resp.get_json()["success"] == True


def test_protected_route_not_401ed_by_a_stale_bearer_token_when_cookie_present(app_factory, monkeypatch):
    # Regression guard on the Bearer-fallthrough behavior change itself:
    # a route gated by enforce_authentication (not just the always-exempt
    # /api/auth/me) must not 401 just because THIS request's Bearer token
    # failed verification, as long as the session cookie still resolves.
    env = app_factory(env=_env_with_signing_key())
    _mock_bearer_success(monkeypatch, env, "alice@example.com")
    env.client.get('/api/auth/me', headers={"Authorization": "Bearer real-looking-token"})

    _mock_bearer_failure(monkeypatch, env)
    resp = env.client.post(
        '/api/config',
        json={
            "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
            "database_name": "My DB", "is_custom": True,
        },
        headers={"Authorization": "Bearer now-expired-token"},
    )
    assert resp.status_code == 200
