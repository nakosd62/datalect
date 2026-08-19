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
        assert identity == env.auth.ANONYMOUS_USER_ID
        assert env.auth.is_anonymous_user(identity) is True


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
    assert "Gemini API key" in resp.get_json()["error"]


def test_auth_me_endpoint_is_always_exempt(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/api/auth/me')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "authenticated" in data
    assert data["auth_required"] is True


def test_protected_route_rejected_without_identity_when_auth_enabled(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    # /api/history isn't in EXEMPT_ENDPOINTS - with AUTH_ENABLED true, the
    # guard demands *some* identity signal before the route even runs.
    # Since get_current_user_identity() falls back to ANONYMOUS_USER_ID
    # (a truthy string) whenever GOOGLE_CLIENT_ID is set, the guard itself
    # never actually 401s in this app - it's is_anonymous_user() gating at
    # the route level that does the real rejection (see test_history_routes.py).
    resp = env.client.get('/api/history')
    assert resp.status_code == 403  # anonymous-user rejection, not 401


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
