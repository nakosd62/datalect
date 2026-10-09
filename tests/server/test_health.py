"""
GET /health - the lightweight liveness/readiness probe for infrastructure
checks (Cloud Monitoring uptime checks, Cloud Run health checks), added so
those probes stop hitting '/' and loading webClient/index.html's GA4 tag
(see server.py's health() docstring for the full "Council Bluffs, Iowa
shows up as a new user every day" story this fixes). Mirrors
test_client_version.py's own structure for another always-exempt,
no-session-concept route.

Deliberately a bare no-op now, by design (see server.py's health()
docstring): it used to also prove the state store was reachable via
state_store.list_cached_schema_texts(), but on Firestore that's a full
collection scan of schema_cache - one billed/counted read PER DOCUMENT
currently cached, not a flat cost - and an infrastructure probe hitting
this route on a fixed schedule turned that into a constant, ever-growing
Firestore read load (~3,000 reads/hour observed, with zero real user
activity) as schema_cache accumulated entries over time. There is
intentionally no "unreachable state store -> 503" test here any more -
this route no longer touches the state store at all, so there is no
failure path left to exercise.
"""

import pytest


def test_health_endpoint_returns_ok_unconditionally(client):
    resp = client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_health_endpoint_is_exempt_from_auth_guard(app_factory):
    # 'health' is explicitly in EXEMPT_ENDPOINTS (see auth.py) so an
    # infrastructure probe never gets redirected into a login flow or
    # bounced with a 401, the same way config.handle_config/
    # config.get_client_version already are for an anonymous Cloud Run
    # visitor - mirrors test_client_version.py's own
    # test_client_version_endpoint_is_exempt_from_auth_guard.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/health')
    assert resp.status_code == 200


def test_health_endpoint_never_renders_the_spa_shell(client):
    # The entire point of this route: unlike '/', the response here must
    # never be webClient/index.html (and therefore can never execute its
    # GA4 tag, no matter how often or from where a probe polls it).
    resp = client.get('/health')
    assert resp.content_type.startswith('application/json')
    assert b'<html' not in resp.data.lower()


def test_health_endpoint_never_touches_the_state_store(app_env, monkeypatch):
    # The whole point of making this route a no-op: even a state store
    # that's completely broken must never affect /health. Previously this
    # file had a companion test asserting a 503 here when the state store
    # was unreachable - removed because that failure path no longer
    # exists (see this module's own docstring for why).
    def boom():
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "list_cached_schema_texts", boom)
    resp = app_env.client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}
