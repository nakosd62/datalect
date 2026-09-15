"""
concurrency_guard.py: the ConcurrencyGuard class in isolation, its
MAX_CONCURRENT_TRANSLATE_REQUESTS/MAX_CONCURRENT_EXECUTE_REQUESTS env-var
wiring, and its real effect on /api/execute and /api/translate under
genuine concurrent access.

The class-level tests below construct their own ConcurrencyGuard instances
directly (via app_env.concurrency_guard.ConcurrencyGuard) rather than
poking at the module-level TRANSLATE_GUARD/EXECUTE_GUARD singletons, so
they don't care what env vars happened to be set when concurrency_guard.py
was last imported.

The HTTP-level tests need a REAL second request to be in flight while the
first is still running, not just two sequential calls - a guard that
happens to work when called twice in a row but never actually contends
would be untested. Both use the same shape: patch the route's backend/LLM
client with a fake that blocks on a threading.Event() until the test says
to proceed, fire the first request on a background thread, wait for
proof (a second Event) that the fake is actually mid-call, and only then
make the second, would-be-rejected request from the main thread. This
mirrors test_execute_routes.py's own _FakeBackend delay/threading.Event
convention (see that file's module docstring and
test_execute_times_out_and_returns_friendly_error), adapted from a fixed
sleep to a pair of Events since these tests need an exact, test-controlled
window rather than a "probably long enough" delay.
"""

import threading

from helpers import parse_translate_stream


# ---------------------------------------------------------------------------
# ConcurrencyGuard class, in isolation
# ---------------------------------------------------------------------------

def test_disabled_guard_max_concurrent_zero_always_acquires(app_env):
    guard = app_env.concurrency_guard.ConcurrencyGuard("test", 0)
    assert guard.max_concurrent == 0
    # No cap at all when disabled - acquiring far more times than any real
    # limit would allow must still always succeed, and release() (called or
    # not) must never raise.
    for _ in range(5):
        assert guard.try_acquire() is True
    guard.release()


def test_negative_max_concurrent_also_disables_the_guard(app_env):
    # Mirrors execute_routes.py's own "<=0 disables it" SQL_EXECUTE_TIMEOUT_
    # SECONDS convention (see concurrency_guard.py's module docstring) -
    # a negative value is just as "disabled" as exactly 0, not an error.
    guard = app_env.concurrency_guard.ConcurrencyGuard("test", -1)
    assert guard.try_acquire() is True
    assert guard.try_acquire() is True


def test_enabled_guard_blocks_at_capacity_and_frees_a_slot_on_release(app_env):
    guard = app_env.concurrency_guard.ConcurrencyGuard("test", 2)
    assert guard.try_acquire() is True
    assert guard.try_acquire() is True
    # Capacity is exactly 2 - a third, simultaneous acquire must be refused
    # outright (non-blocking - try_acquire() must return promptly, never
    # hang waiting for a slot).
    assert guard.try_acquire() is False

    guard.release()
    # Releasing exactly one of the two held slots must free exactly one -
    # a second concurrent try_acquire() at this point must still fail.
    assert guard.try_acquire() is True
    assert guard.try_acquire() is False


def test_read_limit_parses_env_var(app_env):
    assert app_env.concurrency_guard._read_limit("SOME_UNSET_VAR_XYZ") == 0


def test_read_limit_treats_non_integer_value_as_disabled(app_env, monkeypatch):
    monkeypatch.setenv("SOME_VAR_XYZ", "not-a-number")
    # A malformed value must degrade to "disabled" (0), matching this
    # module's documented "an admin has to explicitly opt in" posture -
    # never crash the app over a typo in an env var.
    assert app_env.concurrency_guard._read_limit("SOME_VAR_XYZ") == 0


def test_translate_and_execute_guards_default_to_disabled(app_env):
    # No MAX_CONCURRENT_*_REQUESTS set at all (the app_env fixture's default
    # environment) - both module-level singletons must come up disabled, so
    # a deployment that's never heard of this feature behaves exactly as it
    # always has.
    assert app_env.concurrency_guard.TRANSLATE_GUARD.max_concurrent == 0
    assert app_env.concurrency_guard.EXECUTE_GUARD.max_concurrent == 0
    assert app_env.concurrency_guard.TRANSLATE_GUARD.try_acquire() is True
    assert app_env.concurrency_guard.EXECUTE_GUARD.try_acquire() is True


def test_translate_and_execute_guard_limits_are_read_independently_from_env(app_factory):
    env = app_factory(env={
        "MAX_CONCURRENT_TRANSLATE_REQUESTS": "3",
        "MAX_CONCURRENT_EXECUTE_REQUESTS": "5",
    })
    assert env.concurrency_guard.TRANSLATE_GUARD.max_concurrent == 3
    assert env.concurrency_guard.EXECUTE_GUARD.max_concurrent == 5


# ---------------------------------------------------------------------------
# /api/execute wiring - a non-streaming route, guarded via @guarded_route
# ---------------------------------------------------------------------------

class _BlockingBackend:
    """Minimal stand-in for a real backends.Backend (see
    test_execute_routes.py's own _FakeBackend for the established
    convention this mirrors), built around a pair of threading.Event()s
    instead of a fixed delay - these tests need to hold a request open for
    an exact, test-controlled window (long enough to prove a concurrent
    second request gets rejected, no longer), not guess at a sleep
    duration that's "probably long enough"."""

    def __init__(self):
        self.execute_started = threading.Event()
        self.release_event = threading.Event()

    def connect(self, descriptor):
        return object()

    def close(self, connection):
        pass

    def execute(self, connection, sql_text):
        self.execute_started.set()
        self.release_event.wait(timeout=5)
        return [{"statement": sql_text, "columns": ["n"], "rows": [[1]], "rowCount": 1}]


def test_execute_guard_rejects_a_concurrent_request_then_releases_the_slot(app_factory, monkeypatch):
    """MAX_CONCURRENT_EXECUTE_REQUESTS=1: a second /api/execute request that
    arrives while the first is still genuinely mid-flight (proven via
    execute_started, not assumed from timing) must be rejected outright
    with 503 + Retry-After - per concurrency_guard.py's "reject, never
    queue" design, not delayed and not silently dropped. Once the first
    request's backend is allowed to finish, both the first request itself
    succeeds AND a brand new third request made afterward is accepted
    again - proving guard.release() actually ran, not just that
    acquisition/rejection alone works (which alone wouldn't catch a guard
    that leaks a permit and stays stuck at capacity forever)."""
    env = app_factory(env={"MAX_CONCURRENT_EXECUTE_REQUESTS": "1"})
    fake = _BlockingBackend()
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: fake)

    results = {}

    def _run_first():
        with env.app_config.app.test_client() as c:
            results['first'] = c.post('/api/execute', json={'sql': 'SELECT 1;'})

    t = threading.Thread(target=_run_first)
    t.start()
    assert fake.execute_started.wait(timeout=2), "first request never reached the fake backend's execute()"

    resp2 = env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp2.status_code == 503
    assert resp2.headers.get('Retry-After')
    data2 = resp2.get_json()
    assert data2['success'] is False
    assert "too many database queries" in data2['error']

    fake.release_event.set()
    t.join(timeout=5)
    assert results['first'].status_code == 200
    assert results['first'].get_json()['success'] is True

    # The slot must be free again immediately - not just "eventually".
    resp3 = env.client.post('/api/execute', json={'sql': 'SELECT 3;'})
    assert resp3.status_code == 200
    assert resp3.get_json()['success'] is True


def test_execute_guard_releases_the_slot_even_when_the_backend_raises(app_factory, monkeypatch):
    """A leaked permit (never released because the wrapped view raised) is
    exactly the failure mode concurrency_guard.py's own guarded_route()
    docstring calls out - a try/finally around the WHOLE view call, not a
    hand-threaded release() in each of execute_query()'s branches, is what
    guards against it. Confirms release() runs on the exception path too,
    not only the two happy-path tests above."""
    env = app_factory(env={"MAX_CONCURRENT_EXECUTE_REQUESTS": "1"})

    class _RaisingBackend:
        def connect(self, descriptor):
            return object()

        def close(self, connection):
            pass

        def execute(self, connection, sql_text):
            raise RuntimeError("boom")

    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: _RaisingBackend())

    resp1 = env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert resp1.status_code != 503  # the view's own error handling, not the guard, produced this
    assert resp1.get_json()['success'] is False

    # If the guard had leaked its one permit on the exception above, this
    # would come back 503 instead of running the (fresh, non-raising) view.
    fake2 = _BlockingBackend()
    fake2.release_event.set()  # never actually need to block for this check
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: fake2)
    resp2 = env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp2.status_code == 200
    assert resp2.get_json()['success'] is True


def test_execute_guard_is_a_no_op_when_disabled(app_env, monkeypatch):
    # Default env (no MAX_CONCURRENT_EXECUTE_REQUESTS set) - two requests
    # that would race under a limit of 1 must both succeed unconditionally.
    fake = _BlockingBackend()
    fake.release_event.set()  # never actually need to block - guard is off
    monkeypatch.setattr(app_env.execute_routes, "get_backend", lambda descriptor: fake)
    resp1 = app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    resp2 = app_env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp1.status_code == 200
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# /api/translate wiring - a streaming route, guarded by hand around the
# generator (not @guarded_route - see translate_routes.py's own comment)
# ---------------------------------------------------------------------------

class _FakeGenaiUsage:
    def __init__(self):
        self.prompt_token_count = 10
        self.candidates_token_count = 5
        self.total_token_count = 15
        self.thoughts_token_count = 0
        self.cached_content_token_count = 0


class _FakeGenaiResponse:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = _FakeGenaiUsage()


class _BlockingModels:
    """Stand-in for google.genai.Client(...).models - blocks generate_content
    on a pair of threading.Event()s the same way _BlockingBackend above
    blocks execute(), so a test can hold /api/translate's guard acquired
    for an exact, controlled window."""

    def __init__(self):
        self.started = threading.Event()
        self.release_event = threading.Event()

    def generate_content(self, model, contents, config):
        self.started.set()
        self.release_event.wait(timeout=5)
        return _FakeGenaiResponse("SELECT 1;")


def test_translate_guard_rejects_a_concurrent_request_then_releases_the_slot(app_factory, monkeypatch):
    """Same contract as the /api/execute test above, but for the streaming
    route: the guard here is acquired synchronously in translate_query()
    itself (before the streamed Response object is even built - see
    translate_routes.py's own comment on why), so it's already held the
    moment the first request's POST call returns, well before the response
    body is ever drained. Draining that body (get_data()) is what actually
    runs generate_content() and lets the release-on-completion half of this
    test proceed."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_CONCURRENT_TRANSLATE_REQUESTS": "1",
    })
    models = _BlockingModels()

    class _BlockingClient:
        def __init__(self, api_key=None, http_options=None):
            self.models = models

    monkeypatch.setattr(env.translate_routes.genai, "Client", _BlockingClient)

    results = {}

    def _run_first():
        with env.app_config.app.test_client() as c:
            resp = c.post('/api/translate', json={'prompt': 'show users'})
            resp.get_data()  # drains the stream - runs generate_content() synchronously here
            results['first'] = resp

    t = threading.Thread(target=_run_first)
    t.start()
    assert models.started.wait(timeout=2), "first request never reached the fake genai client's generate_content()"

    resp2 = env.client.post('/api/translate', json={'prompt': 'show users again'})
    assert resp2.status_code == 503
    assert resp2.headers.get('Retry-After')
    data2 = resp2.get_json()
    assert 'error' in data2
    assert 'status' not in data2  # a plain, non-streamed body - see busy_response()'s own docstring
    assert "too many translation requests" in data2['error']

    models.release_event.set()
    t.join(timeout=5)
    assert results['first'].status_code == 200

    _retry_events, final = parse_translate_stream(results['first'])
    assert final['success'] is True
    assert final['sql'] == "SELECT 1;"

    # The slot must be free again immediately once the first stream fully
    # finished (its generator's finally released it) - not "eventually".
    resp3 = env.client.post('/api/translate', json={'prompt': 'show users once more'})
    resp3.get_data()  # drain - see this file's module docstring on why an unread stream matters
    assert resp3.status_code == 200


def test_translate_early_validation_failures_never_consume_a_guard_slot(app_factory, monkeypatch):
    """A missing API key / empty prompt returns before translate_query() ever
    reaches the guard acquire (see that route's own comment on why - those
    requests do essentially no work, so shouldn't compete with real ones
    for a scarce slot). With capacity for exactly one real request, any
    number of early-validation failures beforehand must never eat into it."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_CONCURRENT_TRANSLATE_REQUESTS": "1",
    })

    for _ in range(3):
        resp = env.client.post('/api/translate', json={'prompt': '   '})
        assert resp.status_code == 400

    harness_models = _BlockingModels()
    harness_models.release_event.set()  # let it complete immediately

    class _Client:
        def __init__(self, api_key=None, http_options=None):
            self.models = harness_models

    monkeypatch.setattr(env.translate_routes.genai, "Client", _Client)
    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    resp.get_data()  # drain - see this file's module docstring on why an unread stream matters
    assert resp.status_code == 200


def test_translate_guard_is_a_no_op_when_disabled(app_factory, monkeypatch):
    # Default env (no MAX_CONCURRENT_TRANSLATE_REQUESTS set) - two requests
    # that would race under a limit of 1 must both succeed unconditionally.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    models = _BlockingModels()
    models.release_event.set()

    class _Client:
        def __init__(self, api_key=None, http_options=None):
            self.models = models

    monkeypatch.setattr(env.translate_routes.genai, "Client", _Client)
    resp1 = env.client.post('/api/translate', json={'prompt': 'show users'})
    resp1.get_data()
    resp2 = env.client.post('/api/translate', json={'prompt': 'show orders'})
    resp2.get_data()
    assert resp1.status_code == 200
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# TRANSLATE_GUARD is POOLED across /api/translate, /api/summarize-result,
# and /api/summarize-results - not one guard per route (per the user's own
# direction: a translate call and a summarize call are "the same kind of
# thing" as far as admission control is concerned). Proven in both
# directions below.
# ---------------------------------------------------------------------------

_SUMMARIZE_RESULT_BODY = {
    'prompt': 'how many rows', 'sql': 'SELECT 1;',
    'results': [{"columns": [], "rows": [], "rowCount": 0}],
}


def test_translate_guard_pool_is_shared_with_summarize_result(app_factory, monkeypatch):
    """A request in flight on /api/translate must make /api/summarize-result
    see TRANSLATE_GUARD as already exhausted (not its own, independent
    guard) - and releasing it (from wherever it was acquired) frees the
    exact same slot back up for /api/translate itself."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_CONCURRENT_TRANSLATE_REQUESTS": "1",
    })
    models = _BlockingModels()

    class _Client:
        def __init__(self, api_key=None, http_options=None):
            self.models = models

    monkeypatch.setattr(env.translate_routes.genai, "Client", _Client)

    results = {}

    def _run_translate():
        with env.app_config.app.test_client() as c:
            resp = c.post('/api/translate', json={'prompt': 'show users'})
            resp.get_data()
            results['translate'] = resp

    t = threading.Thread(target=_run_translate)
    t.start()
    assert models.started.wait(timeout=2), "translate request never reached generate_content()"

    # A DIFFERENT route, while /api/translate is still holding the pool's
    # one slot, must also be rejected.
    resp_summarize = env.client.post('/api/summarize-result', json=_SUMMARIZE_RESULT_BODY)
    assert resp_summarize.status_code == 503
    data = resp_summarize.get_json()
    assert data['success'] is False  # summarize-result's OWN shape, even though the guard is shared

    models.release_event.set()
    t.join(timeout=5)
    assert results['translate'].status_code == 200

    # The pool is free again - a fresh /api/summarize-result request must
    # now be admitted (not rejected by the guard; whatever it does with an
    # unparseable fake LLM response is irrelevant to this assertion).
    resp_after = env.client.post('/api/summarize-result', json=_SUMMARIZE_RESULT_BODY)
    resp_after.get_data()
    assert resp_after.status_code == 200


def test_translate_guard_pool_is_shared_with_summarize_result_in_the_other_direction(app_factory, monkeypatch):
    """Same guarantee as the test above, with the roles reversed: a request
    in flight on /api/summarize-result must make /api/translate itself see
    the pool as exhausted."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_CONCURRENT_TRANSLATE_REQUESTS": "1",
    })
    models = _BlockingModels()

    class _Client:
        def __init__(self, api_key=None, http_options=None):
            self.models = models

    monkeypatch.setattr(env.translate_routes.genai, "Client", _Client)

    results = {}

    def _run_summarize():
        with env.app_config.app.test_client() as c:
            resp = c.post('/api/summarize-result', json=_SUMMARIZE_RESULT_BODY)
            resp.get_data()
            results['summarize'] = resp

    t = threading.Thread(target=_run_summarize)
    t.start()
    assert models.started.wait(timeout=2), "summarize-result request never reached generate_content()"

    resp_translate = env.client.post('/api/translate', json={'prompt': 'show orders'})
    assert resp_translate.status_code == 503
    data = resp_translate.get_json()
    assert 'status' not in data  # /api/translate's OWN bare {"error": ...} shape, even though the guard is shared

    models.release_event.set()
    t.join(timeout=5)
    assert results['summarize'].status_code == 200
