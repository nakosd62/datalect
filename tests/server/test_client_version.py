"""
GET /api/client-version + app_config.py's CLIENT_BUILD_ID: lets client.js
detect "a reload would pick up new code" without nagging the user on every
server restart - see config_routes.py's get_client_version() docstring and
app_config.py's _compute_client_build_id()/CLIENT_BUILD_ID for the full
design. The key property under test: the id is derived from the CONTENT of
webClient/index.html, client.js, and style.css - not from process start
time or file mtimes - so it only changes when one of those files' bytes
actually change.
"""

import os

import pytest


def test_client_version_endpoint_returns_a_build_id(client):
    resp = client.get('/api/client-version')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data.get('client_build_id'), str)
    assert len(data['client_build_id']) > 0


def test_client_version_endpoint_is_exempt_from_auth_guard(app_factory):
    # config.get_client_version is explicitly in EXEMPT_ENDPOINTS (see
    # auth.py) so it's reachable the same way config.handle_config already
    # is for an anonymous Cloud Run visitor - mirrors
    # test_auth.py's own test_config_route_is_always_exempt_from_auth_guard.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.get('/api/client-version')
    assert resp.status_code == 200


def test_client_build_id_is_stable_across_requests_and_fresh_app_instances(app_factory):
    # Same real, unchanged webClient/ files every time -> the exact same id,
    # whether asked twice on one app instance or once each on two
    # independently-built ones (a fresh_import() re-executes app_config.py
    # from scratch, so this also proves the id isn't randomized per import -
    # e.g. accidentally seeded from something like a random salt or the
    # current timestamp).
    env_a = app_factory()
    env_b = app_factory()
    id_from_a = env_a.client.get('/api/client-version').get_json()['client_build_id']
    id_from_a_again = env_a.client.get('/api/client-version').get_json()['client_build_id']
    id_from_b = env_b.client.get('/api/client-version').get_json()['client_build_id']
    assert id_from_a == id_from_a_again
    assert id_from_a == id_from_b


def test_compute_client_build_id_changes_when_a_watched_file_s_content_changes(app_env, tmp_path, monkeypatch):
    # Points app.static_folder at a scratch directory this test fully
    # controls (rather than the real webClient/) so it can prove the actual
    # property this feature depends on: the id tracks file CONTENT, not
    # just "some file exists at this path" or a timestamp.
    static_dir = tmp_path / "fake_static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>v1</html>")
    (static_dir / "client.js").write_text("console.log('v1');")
    (static_dir / "style.css").write_text("body { color: red; }")

    monkeypatch.setattr(app_env.app_config.app, "static_folder", str(static_dir))
    build_id_v1 = app_env.app_config._compute_client_build_id()

    # Touching just ONE of the three watched files is enough to change the
    # overall id - a real frontend change is exactly this shape (usually
    # only client.js edited, not all three at once).
    (static_dir / "client.js").write_text("console.log('v2');")
    build_id_v2 = app_env.app_config._compute_client_build_id()

    assert build_id_v1 != build_id_v2

    # Reverting the content reproduces the ORIGINAL id exactly - this is a
    # pure content hash, not something that also folds in mtime/order/a
    # monotonically-increasing counter.
    (static_dir / "client.js").write_text("console.log('v1');")
    build_id_v1_again = app_env.app_config._compute_client_build_id()
    assert build_id_v1_again == build_id_v1


def test_compute_client_build_id_is_unaffected_by_a_restart_that_touches_no_watched_file(app_env, tmp_path, monkeypatch):
    # The exact scenario the whole feature exists to distinguish: a
    # server restart after a BACKEND-only change (nothing under
    # webClient/ touched) must NOT look like a new client version to an
    # already-open tab.
    static_dir = tmp_path / "fake_static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>v1</html>")
    (static_dir / "client.js").write_text("console.log('v1');")
    (static_dir / "style.css").write_text("body { color: red; }")
    monkeypatch.setattr(app_env.app_config.app, "static_folder", str(static_dir))

    build_id_before = app_env.app_config._compute_client_build_id()
    # Simulate "the process restarted" - recompute from scratch with
    # nothing on disk having changed at all.
    build_id_after_restart = app_env.app_config._compute_client_build_id()
    assert build_id_before == build_id_after_restart


def test_compute_client_build_id_does_not_crash_when_a_watched_file_is_missing(app_env, tmp_path, monkeypatch):
    # A stripped-down dev checkout or a build step that hasn't produced
    # style.css yet shouldn't take startup down over what's ultimately
    # just a "please reload" nicety.
    static_dir = tmp_path / "fake_static_missing"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>v1</html>")
    (static_dir / "client.js").write_text("console.log('v1');")
    # style.css deliberately not created.
    monkeypatch.setattr(app_env.app_config.app, "static_folder", str(static_dir))

    build_id = app_env.app_config._compute_client_build_id()
    assert isinstance(build_id, str)
    assert len(build_id) > 0
