"""
history_routes.py: /api/history and /api/history/purge. Both work for any
identity, including a genuinely anonymous (Cloud Run, signed-out) one -
auth.py's per-session ANONYMOUS_USER_ID_PREFIX identity already isolates
one anonymous visitor's history from every other's (and from any
authenticated user's), the same way it isolates DB selection/auto-execute
(see test_config_cloud_run_anonymous_redaction.py's "dont_collide" tests),
so there's no separate sign-in gate here anymore.

Authenticated-user coverage below is exercised via the auth cookie (the
simplest way to get a stable, non-anonymous, non-"global" user_identity
without mocking Google ID-token verification).

NOTE: Flask/Werkzeug's test client's own cookie jar takes precedence over
a manually-supplied `headers={"Cookie": ...}` dict (it gets silently
dropped) - use `client.set_cookie(name, value)` instead, which is what
`login_as` below wraps.
"""

import pytest

from helpers import login_as


def test_get_history_success_for_identified_user(client):
    login_as(client, "alice@example.com")
    resp = client.get('/api/history')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'history' in data and 'stats' in data and 'total_count' in data


def test_get_history_reflects_recorded_translations(app_env):
    app_env.app_config.state_store.record_translation(
        "alice@example.com", "postgres", "My DB", "show users", "SELECT * FROM users;",
        "gemini-2.5-flash", 100, 5, 5, 10, 0, 0,
    )
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.get('/api/history')
    data = resp.get_json()
    assert data['total_count'] == 1
    assert data['history'][0]['nl_prompt'] == "show users"


def test_get_history_rejected_for_local_global_identity_is_not_anonymous(client):
    # NOTE: with auth disabled entirely (no GOOGLE_CLIENT_ID, not Cloud
    # Run), get_current_user_identity() falls back to "global", which does
    # NOT start with ANONYMOUS_USER_ID_PREFIX - is_anonymous_user("global")
    # is False, so local/dev usage of /api/history works without any cookie.
    resp = client.get('/api/history')
    assert resp.status_code == 200


def test_anonymous_visitor_can_view_their_own_history(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/api/history')  # no identity signal -> anonymous:<session_id>
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['total_count'] == 0


def test_anonymous_visitor_can_purge_their_own_history(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.post('/api/history/purge')
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_two_anonymous_visitors_have_isolated_history(app_factory):
    # Mirrors test_config_cloud_run_anonymous_redaction.py's
    # "dont_collide" tests - translation history is keyed by the same
    # per-session anonymous:<session_id> identity as DB selection/
    # auto-execute, so it's isolated the same way. Two separate test
    # clients against the same app instance simulate two different
    # browsers (each gets its own cookie jar, so its own
    # crbot_session_id -> its own anonymous:<session_id> identity).
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    identity_one = browser_one.get('/api/config').get_json()['user_id']
    identity_two = browser_two.get('/api/config').get_json()['user_id']
    assert identity_one != identity_two

    env.app_config.state_store.record_translation(
        identity_one, "postgres", "My DB", "show users", "SELECT * FROM users;",
        "gemini-2.5-flash", 100, 5, 5, 10, 0, 0,
    )

    assert browser_one.get('/api/history').get_json()['total_count'] == 1
    assert browser_two.get('/api/history').get_json()['total_count'] == 0


def test_anonymous_purge_only_affects_the_requesting_sessions_history(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    identity_one = browser_one.get('/api/config').get_json()['user_id']
    identity_two = browser_two.get('/api/config').get_json()['user_id']

    env.app_config.state_store.record_translation(
        identity_one, "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0,
    )
    env.app_config.state_store.record_translation(
        identity_two, "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0,
    )

    resp = browser_one.post('/api/history/purge')
    assert resp.status_code == 200

    assert browser_one.get('/api/history').get_json()['total_count'] == 0
    assert browser_two.get('/api/history').get_json()['total_count'] == 1


def test_purge_deletes_history_for_identified_user(app_env):
    app_env.app_config.state_store.record_translation(
        "alice@example.com", "postgres", "My DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0,
    )
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/history/purge')
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    after = app_env.client.get('/api/history').get_json()
    assert after['total_count'] == 0


def test_purge_only_affects_the_requesting_user(app_env):
    app_env.app_config.state_store.record_translation(
        "alice@example.com", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0,
    )
    app_env.app_config.state_store.record_translation(
        "bob@example.com", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0,
    )
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/history/purge')  # purges alice only

    login_as(app_env.client, "bob@example.com")
    bob_history = app_env.client.get('/api/history').get_json()
    assert bob_history['total_count'] == 1


def test_purge_supports_delete_method_too(client):
    login_as(client, "alice@example.com")
    resp = client.delete('/api/history/purge')
    assert resp.status_code == 200


def test_get_history_handles_state_store_exception_gracefully(app_env, monkeypatch):
    def boom(user_id):
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "get_translation_history", boom)
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.get('/api/history')
    assert resp.status_code == 500
    data = resp.get_json()
    assert data['success'] is False
    # Generalized message - the raw exception text must never leak here
    # (unlike /api/execute, which intentionally does leak SQL errors).
    assert "db is on fire" not in data['error']
