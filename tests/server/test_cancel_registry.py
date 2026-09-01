"""
tests/server/test_cancel_registry.py

Covers server/cancel_registry.py (the process-local registry backing the
client's "Stop" button - see client.js's cancelInFlightQuery()) and the
POST /api/cancel route in execute_routes.py that drives it.

Two layers, matching this suite's usual split for a new piece of
non-trivial logic:

1. Pure-Python unit tests against cancel_registry's module-level API
   directly (register/unregister/cancel, and CancellableHandle's
   double-close safety) - no Flask app needed at all.
2. A thin Flask-level test hitting POST /api/cancel through a real test
   client, confirming the route's session-scoping (X-Session-ID header,
   per auth.py's get_or_create_session_id()) and response shape, using a
   directly-registered fake closer rather than a real in-flight query -
   exercising the actual execute_routes.py/translate_routes.py
   registration/cleanup code itself (e.g. a real /api/execute call racing
   a real /api/cancel call) would need a live-but-abandonable backend
   connection or LLM client fake this suite doesn't have; that gap is
   accepted the same way test_translate_routes.py already accepts it for
   the LLM-client side (its FakeClient.close() is just a call counter,
   not a real abort).

This is a dedicated file rather than additions to test_execute_routes.py
(which already covers /api/execute and /api/ping) so the two stay
independently readable, and so this file's existence doesn't collide with
that one's.
"""

import threading

from helpers import fresh_import


# ---------------------------------------------------------------------------
# 1. Pure-Python unit tests - no Flask app, just cancel_registry itself.
# ---------------------------------------------------------------------------


def _fresh_cancel_registry(monkeypatch, tmp_path):
    """Returns a freshly-imported cancel_registry module with an empty
    registry, via the same fresh_import() every other test file uses -
    keeps this file consistent with the suite's isolation discipline
    instead of importing cancel_registry directly (which could pick up
    another test's module-level _registry state if import order ever put
    it in sys.modules first)."""
    env = fresh_import(monkeypatch, tmp_path, register_blueprints=True)
    return env.cancel_registry


def test_register_then_cancel_closes_handle(monkeypatch, tmp_path):
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls = []
    token, handle = cancel_registry.register("session-a", lambda: calls.append("closed"))

    cancelled = cancel_registry.cancel("session-a")

    assert cancelled == 1
    assert calls == ["closed"]


def test_cancel_unknown_session_is_a_noop(monkeypatch, tmp_path):
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)

    assert cancel_registry.cancel("no-such-session") == 0


def test_cancel_only_affects_its_own_session(monkeypatch, tmp_path):
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls_a, calls_b = [], []
    cancel_registry.register("session-a", lambda: calls_a.append(1))
    cancel_registry.register("session-b", lambda: calls_b.append(1))

    cancelled = cancel_registry.cancel("session-a")

    assert cancelled == 1
    assert calls_a == [1]
    assert calls_b == []
    # session-b's handle is untouched and still cancellable later.
    assert cancel_registry.cancel("session-b") == 1
    assert calls_b == [1]


def test_cancel_closes_every_handle_registered_for_a_session(monkeypatch, tmp_path):
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls = []
    cancel_registry.register("session-a", lambda: calls.append("first"))
    cancel_registry.register("session-a", lambda: calls.append("second"))

    cancelled = cancel_registry.cancel("session-a")

    assert cancelled == 2
    assert sorted(calls) == ["first", "second"]


def test_unregister_prevents_a_later_cancel_from_closing_it(monkeypatch, tmp_path):
    """Mirrors the normal-completion path: a request's own `finally`
    unregisters its handle before a concurrent /api/cancel could reach
    it."""
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls = []
    token, handle = cancel_registry.register("session-a", lambda: calls.append(1))

    cancel_registry.unregister("session-a", token)

    assert cancel_registry.cancel("session-a") == 0
    assert calls == []


def test_unregister_unknown_token_or_session_does_not_raise(monkeypatch, tmp_path):
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    token, _handle = cancel_registry.register("session-a", lambda: None)

    # Wrong token, wrong session, already-removed token - none of these
    # should raise (a request's finally block shouldn't ever blow up on
    # cleanup just because a cancel already ran first).
    cancel_registry.unregister("session-a", "not-a-real-token")
    cancel_registry.unregister("no-such-session", token)
    cancel_registry.unregister("session-a", token)
    cancel_registry.unregister("session-a", token)


def test_handle_close_is_idempotent(monkeypatch, tmp_path):
    """CancellableHandle.close() must be safe to call twice - this is what
    makes it safe for a request's own `finally` and a concurrent
    /api/cancel to race: whichever runs first wins, the other is a
    no-op."""
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls = []
    token, handle = cancel_registry.register("session-a", lambda: calls.append(1))

    handle.close()
    handle.close()
    cancel_registry.cancel("session-a")  # the handle is gone from the registry by now anyway

    assert calls == [1]


def test_handle_close_swallows_closer_exceptions(monkeypatch, tmp_path):
    """A closer that raises (e.g. a driver's close() call itself errors on
    an already-broken connection) must not propagate out of
    CancellableHandle.close() - cancel_registry.cancel() has to keep
    closing every OTHER handle for that session even if one closer blows
    up."""
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)
    calls = []

    def bad_closer():
        raise RuntimeError("driver blew up")

    cancel_registry.register("session-a", bad_closer)
    cancel_registry.register("session-a", lambda: calls.append("ok"))

    cancelled = cancel_registry.cancel("session-a")

    assert cancelled == 2
    assert calls == ["ok"]


def test_concurrent_close_and_cancel_only_run_the_closer_once(monkeypatch, tmp_path):
    """Two threads racing to close the SAME handle (one via handle.close()
    directly - simulating a request's own finally - the other via
    cancel_registry.cancel() - simulating a concurrent /api/cancel) must
    only actually invoke the underlying closer once."""
    cancel_registry = _fresh_cancel_registry(monkeypatch, tmp_path)

    call_count = [0]
    lock = threading.Lock()

    def closer():
        with lock:
            call_count[0] += 1

    _token, handle = cancel_registry.register("race-session", closer)

    barrier = threading.Barrier(2)

    def close_directly():
        barrier.wait()
        handle.close()

    def close_via_cancel():
        barrier.wait()
        cancel_registry.cancel("race-session")

    t1 = threading.Thread(target=close_directly)
    t2 = threading.Thread(target=close_via_cancel)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# 2. Flask-level tests - the actual POST /api/cancel route.
# ---------------------------------------------------------------------------


def test_api_cancel_with_nothing_registered_returns_zero(client):
    resp = client.post("/api/cancel", headers={"X-Session-ID": "session-x"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"success": True, "cancelled": 0}


def test_api_cancel_closes_a_handle_registered_for_that_session(app_env):
    calls = []
    app_env.cancel_registry.register("session-x", lambda: calls.append(1))

    resp = app_env.client.post("/api/cancel", headers={"X-Session-ID": "session-x"})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "cancelled": 1}
    assert calls == [1]


def test_api_cancel_does_not_touch_a_different_sessions_handle(app_env):
    calls = []
    app_env.cancel_registry.register("session-other", lambda: calls.append(1))

    resp = app_env.client.post("/api/cancel", headers={"X-Session-ID": "session-x"})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "cancelled": 0}
    assert calls == []
    # Still there, and still cancellable, since the request above was
    # scoped to a different session entirely.
    assert app_env.cancel_registry.cancel("session-other") == 1
    assert calls == [1]


def test_api_cancel_sets_the_session_cookie_like_other_routes(app_env):
    """cancel_query() calls apply_session_cookie() same as every other
    route that resolves a session id - a client relying on the cookie
    (rather than resending X-Session-ID on every call) should get it set
    here too."""
    resp = app_env.client.post("/api/cancel", headers={"X-Session-ID": "session-x"})

    assert resp.status_code == 200
    set_cookie_headers = resp.headers.get_all("Set-Cookie")
    assert any(header.startswith("crbot_session_id=") for header in set_cookie_headers)


# ---------------------------------------------------------------------------
# 3. Integration: /api/execute registers and unregisters a real handle.
# ---------------------------------------------------------------------------


def test_execute_registers_and_unregisters_cancel_handle_around_the_call(app_env, monkeypatch):
    """Confirms execute_routes.py's single-connection path actually wires
    into cancel_registry (not just that cancel_registry works in
    isolation): a handle should be registered for the session while
    backend.execute() is running and gone again once the request
    completes normally."""
    seen_during_call = {}

    class _FakeBackend:
        def connect(self, descriptor):
            return object()

        def close(self, connection):
            pass

        def execute(self, connection, sql_text):
            # Snapshot registry state from inside the call, before this
            # request's own `finally` has a chance to unregister it.
            seen_during_call["cancelled_count"] = app_env.cancel_registry.cancel("session-y")
            return [{"statement": sql_text, "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}]

    monkeypatch.setattr(app_env.execute_routes, "get_backend", lambda descriptor: _FakeBackend())

    resp = app_env.client.post(
        "/api/execute",
        json={"sql": "SELECT 1;"},
        headers={"X-Session-ID": "session-y"},
    )

    assert resp.status_code == 200
    # A handle was registered and live during execute() - cancel() found
    # and closed exactly one (which, incidentally, also proves calling
    # cancel() mid-flight doesn't blow up the in-progress request).
    assert seen_during_call["cancelled_count"] == 1
    # And nothing is left registered for this session afterward - the
    # request's own `finally` already unregistered (or the cancel() call
    # above already removed) it.
    assert app_env.cancel_registry.cancel("session-y") == 0
