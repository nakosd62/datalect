"""
rate_limiter.py: the _rate_limit_key()/_read_limit_string() helpers in
isolation, and RATE_LIMIT_TRANSLATE/RATE_LIMIT_EXECUTE's real effect on
/api/translate and /api/execute.

Unlike concurrency_guard.py's own tests, nothing here needs genuine
concurrent requests or threading.Event-based blocking - rate limiting is a
count-over-TIME mechanism, not a simultaneous-in-flight one (see
rate_limiter.py's own module docstring), so a handful of ordinary
sequential requests is enough to prove or disprove it. That distinction is
itself worth a dedicated regression test below
(test_rate_limit_counts_sequential_non_overlapping_requests_unlike_the_
concurrency_guard) - proving this module catches a pattern
concurrency_guard.py's own semaphore-based guard provably does not.
"""

import threading

from helpers import login_as, parse_translate_stream


# ---------------------------------------------------------------------------
# _read_limit_string() / _rate_limit_key(), in isolation
# ---------------------------------------------------------------------------

def test_read_limit_string_unset_is_disabled(app_env):
    assert app_env.rate_limiter._read_limit_string("SOME_UNSET_RATE_LIMIT_XYZ") is None


def test_read_limit_string_parses_a_valid_limit_string(app_env, monkeypatch):
    monkeypatch.setenv("SOME_RATE_LIMIT_XYZ", "20 per minute")
    assert app_env.rate_limiter._read_limit_string("SOME_RATE_LIMIT_XYZ") == "20 per minute"


def test_read_limit_string_treats_malformed_value_as_disabled(app_env, monkeypatch):
    # Flask-Limiter itself silently treats a malformed limit string as
    # "never limit" (verified directly against the installed library) -
    # indistinguishable from a deliberately-disabled guard unless something
    # validates eagerly and logs. See this function's own docstring.
    monkeypatch.setenv("SOME_RATE_LIMIT_XYZ", "not-a-valid-limit-string")
    assert app_env.rate_limiter._read_limit_string("SOME_RATE_LIMIT_XYZ") is None


def test_translate_and_execute_rate_limits_default_to_disabled(app_env):
    assert app_env.rate_limiter.RATE_LIMIT_TRANSLATE is None
    assert app_env.rate_limiter.RATE_LIMIT_EXECUTE is None


def test_rate_limit_key_scopes_anonymous_visitors_by_session_not_globally(app_factory):
    """Two different anonymous browsers (two different requests, each
    generating its own fresh crbot_session_id since neither carries a
    cookie) must resolve to two DIFFERENT keys - this app's own per-session
    anonymous-identity design (auth.py's ANONYMOUS_USER_ID_PREFIX), reused
    here rather than a bare, globally-shared "anonymous" bucket. Needs
    GOOGLE_CLIENT_ID set - see test_auth.py's own
    test_anonymous_identity_used_when_auth_enabled_but_no_identity_given
    for why get_current_user_identity() only takes the anonymous-per-
    session branch (rather than local dev's single "global" fallback) once
    auth is at least potentially in play."""
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    with env.app_config.app.test_request_context('/'):
        key_a = env.rate_limiter._rate_limit_key()
    with env.app_config.app.test_request_context('/'):
        key_b = env.rate_limiter._rate_limit_key()
    assert key_a != key_b
    assert key_a.startswith("anonymous:")
    assert key_b.startswith("anonymous:")


# ---------------------------------------------------------------------------
# /api/execute wiring
# ---------------------------------------------------------------------------

class _SimpleBackend:
    """No blocking needed here (see this file's module docstring) - just a
    working backend so /api/execute's own single-connection path has
    something to call."""

    def connect(self, descriptor):
        return object()

    def close(self, connection):
        pass

    def execute(self, connection, sql_text):
        return [{"statement": sql_text, "columns": ["n"], "rows": [[1]], "rowCount": 1}]


def test_execute_rate_limit_rejects_the_request_past_the_configured_count(app_factory, monkeypatch):
    env = app_factory(env={"RATE_LIMIT_EXECUTE": "2 per minute"})
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: _SimpleBackend())

    resp1 = env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    resp2 = env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    resp3 = env.client.post('/api/execute', json={'sql': 'SELECT 3;'})
    assert resp3.status_code == 429
    assert resp3.headers.get('Retry-After')
    data3 = resp3.get_json()
    assert data3['success'] is False
    assert "too quickly" in data3['error']


def test_execute_rate_limit_is_per_user_not_global(app_factory, monkeypatch):
    """The whole point of keying on get_current_user_identity() instead of
    a bare IP: one user hitting their own cap must never affect a
    DIFFERENT, simultaneously-active user's own allowance."""
    env = app_factory(env={"RATE_LIMIT_EXECUTE": "1 per minute"})
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: _SimpleBackend())

    with env.app_config.app.test_client() as user_a:
        login_as(user_a, "alice@example.com")
        resp_a1 = user_a.post('/api/execute', json={'sql': 'SELECT 1;'})
        resp_a2 = user_a.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp_a1.status_code == 200
    assert resp_a2.status_code == 429  # alice is over her own budget

    with env.app_config.app.test_client() as user_b:
        login_as(user_b, "bob@example.com")
        resp_b1 = user_b.post('/api/execute', json={'sql': 'SELECT 1;'})
    # bob has never made a request before - his own budget is untouched by
    # alice's, even though hers is already exhausted.
    assert resp_b1.status_code == 200


def test_execute_rate_limit_does_not_consume_a_concurrency_guard_slot_when_it_rejects(app_factory, monkeypatch):
    """Ordering regression: rate_limiter.py's execute_rate_limit() is
    placed ABOVE @guarded_route in execute_routes.py specifically so a
    rate-limited request never reaches EXECUTE_GUARD.try_acquire() at all.
    With plenty of concurrency headroom (capacity 2) but a tight rate limit
    (1/minute), the second request must come back 429 (the rate limiter),
    never 503 (the concurrency guard) - a 503 here would mean the guard
    was reached despite the rate limit already being exhausted, i.e. the
    decorators are stacked in the wrong order."""
    env = app_factory(env={
        "RATE_LIMIT_EXECUTE": "1 per minute",
        "MAX_CONCURRENT_EXECUTE_REQUESTS": "2",
    })
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: _SimpleBackend())

    resp1 = env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    resp2 = env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp1.status_code == 200
    assert resp2.status_code == 429
    assert resp2.get_json()['success'] is False


def test_execute_rate_limit_is_a_no_op_when_disabled(app_env, monkeypatch):
    monkeypatch.setattr(app_env.execute_routes, "get_backend", lambda descriptor: _SimpleBackend())
    for _ in range(5):
        resp = app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/translate wiring
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


class _FakeModels:
    def generate_content(self, model, contents, config):
        return _FakeGenaiResponse("SELECT 1;")


class _FakeGenaiClient:
    def __init__(self, api_key=None, http_options=None):
        self.models = _FakeModels()


def test_translate_rate_limit_rejects_the_request_past_the_configured_count(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "RATE_LIMIT_TRANSLATE": "2 per minute",
    })
    monkeypatch.setattr(env.translate_routes.genai, "Client", _FakeGenaiClient)

    resp1 = env.client.post('/api/translate', json={'prompt': 'show users'})
    resp1.get_data()
    resp2 = env.client.post('/api/translate', json={'prompt': 'show orders'})
    resp2.get_data()
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    resp3 = env.client.post('/api/translate', json={'prompt': 'show products'})
    assert resp3.status_code == 429
    assert resp3.headers.get('Retry-After')
    data3 = resp3.get_json()
    assert 'status' not in data3  # plain, non-streamed body - not read via parse_translate_stream
    assert "too quickly" in data3['error']


def test_translate_rate_limit_counts_early_validation_failures_too(app_factory):
    """Deliberate difference from concurrency_guard.py (see
    rate_limiter.py's own module docstring): an empty prompt still hits
    the route and must still count against the budget - unlike the
    concurrency guard, which is only acquired after early validation
    passes, since rate limiting exists to catch repeated hits of the
    endpoint itself, validation failures included."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "RATE_LIMIT_TRANSLATE": "2 per minute",
    })

    resp1 = env.client.post('/api/translate', json={'prompt': '   '})
    resp2 = env.client.post('/api/translate', json={'prompt': '   '})
    assert resp1.status_code == 400
    assert resp2.status_code == 400

    # The budget (2) is already spent on the two failed validations above -
    # a third hit, even one that would otherwise succeed, must be rejected.
    resp3 = env.client.post('/api/translate', json={'prompt': '   '})
    assert resp3.status_code == 429


def test_rate_limit_counts_sequential_non_overlapping_requests_unlike_the_concurrency_guard(app_factory, monkeypatch):
    """The key distinction this module exists for (see its own module
    docstring): a script firing one request, waiting for it to finish,
    then firing another - never more than one in flight at a time - would
    sail straight through concurrency_guard.py's semaphore-based guard (it
    only ever sees 1 concurrent request), but must still be caught here,
    since this module counts hits over a time window regardless of
    overlap. Every request below is made and fully resolved one at a time,
    strictly sequentially, with EXECUTE_GUARD generous - proving the
    rejection came from the rate limiter's own request-count tracking, not
    from anything concurrency-related."""
    env = app_factory(env={
        "RATE_LIMIT_EXECUTE": "1 per minute",
        "MAX_CONCURRENT_EXECUTE_REQUESTS": "10",  # deliberately generous - never the bottleneck here
    })
    monkeypatch.setattr(env.execute_routes, "get_backend", lambda descriptor: _SimpleBackend())

    resp1 = env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert resp1.status_code == 200  # first request fully completes...

    resp2 = env.client.post('/api/execute', json={'sql': 'SELECT 2;'})
    assert resp2.status_code == 429  # ...yet the very next one, made only after it did, is still rejected


# ---------------------------------------------------------------------------
# RATE_LIMIT_TRANSLATE is POOLED across /api/translate, /api/summarize-
# result, and /api/summarize-results - not one independent counter per
# route (per the user's own direction: a translate call and a summarize
# call are "the same kind of thing"). Proven in both directions below.
# ---------------------------------------------------------------------------

_SUMMARIZE_RESULT_BODY = {
    'prompt': 'how many rows', 'sql': 'SELECT 1;',
    'results': [{"columns": [], "rows": [], "rowCount": 0}],
}


def test_translate_rate_limit_pool_is_shared_with_summarize_result(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "RATE_LIMIT_TRANSLATE": "1 per minute",
    })
    monkeypatch.setattr(env.translate_routes.genai, "Client", _FakeGenaiClient)

    resp1 = env.client.post('/api/translate', json={'prompt': 'show users'})
    resp1.get_data()
    assert resp1.status_code == 200

    # The ONE shared allowance is already spent by /api/translate above - a
    # completely different route must still be rejected, not given its own
    # independent budget.
    resp2 = env.client.post('/api/summarize-result', json=_SUMMARIZE_RESULT_BODY)
    assert resp2.status_code == 429
    assert resp2.get_json()['success'] is False


def test_translate_rate_limit_pool_is_shared_with_summarize_result_in_the_other_direction(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "RATE_LIMIT_TRANSLATE": "1 per minute",
    })
    monkeypatch.setattr(env.translate_routes.genai, "Client", _FakeGenaiClient)

    resp1 = env.client.post('/api/summarize-result', json=_SUMMARIZE_RESULT_BODY)
    resp1.get_data()
    assert resp1.status_code == 200

    resp2 = env.client.post('/api/translate', json={'prompt': 'show orders'})
    assert resp2.status_code == 429
    assert 'status' not in resp2.get_json()  # /api/translate's OWN bare {"error": ...} shape
