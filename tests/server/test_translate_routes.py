"""
translate_routes.py: /api/translate. Patches translate_routes.genai.Client
with a fake that queues canned responses/exceptions - never talks to the
real Gemini API. types.Content/types.Part/types.GenerateContentConfig are
left as the real google-genai classes (plain data containers, no network
calls), so contents/config shape is exercised for real.

/api/translate streams newline-delimited JSON rather than a single JSON
body (see translate_routes.py's module docstring) - every test below that
reads the response body uses helpers.parse_translate_stream(resp) instead
of resp.get_json(), which would raise on any body with more than one JSON
value in it (i.e. any test where at least one retry actually happened).
The two early-validation tests (missing API key / empty prompt) are the
exception: those responses aren't streamed at all - they return before
translate_query() ever reaches the retry loop - so they keep using
resp.get_json() directly, same as before this changed.

Also note the status-code trade-off streaming required: a request that
makes it into the retry loop now always gets HTTP 200 back, whether the
translation ultimately succeeds or fails - the HTTP status has to be
fixed before anything streams, so it can't retroactively become a 500
once a retry line has already gone out. Failure is reported via the
terminal line's success/error fields instead - see
test_non_retryable_error_fails_immediately_and_reports_failure_in_body()
and test_exhausts_all_retry_attempts_and_reports_failure_in_body() below.
"""

import types as pytypes

import pytest

from helpers import install_fake_bigquery, parse_translate_stream, write_database_presets_file


class FakeGenaiResponse:
    def __init__(self, text, prompt_tokens=10, output_tokens=5, total_tokens=15,
                 thinking_tokens=0, cached_tokens=0):
        self.text = text
        self.usage_metadata = pytypes.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            total_token_count=total_tokens,
            thoughts_token_count=thinking_tokens,
            cached_content_token_count=cached_tokens,
        )


class GenaiHarness:
    def __init__(self):
        self.queue = []  # list of FakeGenaiResponse or Exception instances
        self.client_api_keys = []  # api_key each Client(...) was constructed with
        self.generate_calls = []  # kwargs of each generate_content call

    def queue_response(self, resp):
        self.queue.append(resp)

    def queue_error(self, exc):
        self.queue.append(exc)

    def make_client_class(self):
        harness = self

        class FakeModels:
            def generate_content(self, model, contents, config):
                harness.generate_calls.append(
                    {"model": model, "contents": contents, "config": config,
                     "api_key": harness.client_api_keys[-1]}
                )
                if not harness.queue:
                    raise AssertionError("GenaiHarness queue exhausted - test didn't queue enough responses")
                item = harness.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, api_key=None):
                self.api_key = api_key
                harness.client_api_keys.append(api_key)
                self.models = FakeModels()

        return FakeClient


class FakeApiError(Exception):
    """Minimal stand-in for google.genai.errors.APIError - just needs a
    `.code` int attribute, which _gemini_error_code() checks first."""
    def __init__(self, code):
        super().__init__(f"fake API error {code}")
        self.code = code


def test_missing_api_key_returns_400(app_env):
    resp = app_env.client.post('/api/translate', json={'prompt': 'show users'})
    assert resp.status_code == 400
    assert "Gemini API key is not configured." in resp.get_json()['error']


def test_empty_prompt_returns_400(app_factory):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    resp = env.client.post('/api/translate', json={'prompt': '   '})
    assert resp.status_code == 400
    assert "Prompt cannot be empty" in resp.get_json()['error']


def test_success_strips_markdown_fences_and_returns_token_counts(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("```sql\nSELECT * FROM users;\n```"))

    resp = env.client.post('/api/translate', json={'prompt': 'Show all users'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is True
    assert data['sql'] == "SELECT * FROM users;"
    assert data['total_tokens'] == 15
    assert data['input_tokens'] == 10
    assert data['output_tokens'] == 5


def test_success_records_translation_history(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    env.client.set_cookie("crbot_user_id", "alice@example.com")
    env.client.post('/api/translate', json={'prompt': 'give me one'})

    rows, stats, total_count = env.app_config.state_store.get_translation_history("alice@example.com")
    assert total_count == 1
    assert rows[0]['sql_command'] == "SELECT 1;"


def test_postgres_dialect_intro_used_by_default(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'hi'})
    system_instruction = harness.generate_calls[0]["config"].system_instruction
    assert "PostgreSQL-compatible RDBMSs" in system_instruction
    assert "BigQuery" not in system_instruction


def test_bigquery_dialect_intro_used_when_active_connection_is_bigquery(app_factory, tmp_path, monkeypatch):
    presets_path = write_database_presets_file(tmp_path, [
        {"type": "bigquery", "name": "BQ", "project_id": "p", "dataset": "d", "billing_project_id": "p"},
    ])
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "DATABASE_PRESETS_FILE": presets_path,
    })
    install_fake_bigquery(monkeypatch)  # so get_database_schema()'s connect() doesn't hit real GCP
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    # A per-request `database_url` override is just a raw string - not
    # enough to identify a rich BigQuery descriptor (type/project/dataset) -
    # so make the BigQuery preset the *active session connection* instead,
    # the same way selecting it via /api/config would (identity here is
    # "global": no auth configured in this env, matching
    # _effective_user(None)/get_current_user_identity()'s local fallback).
    env.app_config.state_store.set_session(
        "global", db_url="bigquery://p/d", db_type="bigquery",
        db_config={"project_id": "p", "dataset": "d", "billing_project_id": "p"},
    )

    env.client.post('/api/translate', json={'prompt': 'hi'})
    system_instruction = harness.generate_calls[0]["config"].system_instruction
    assert "BigQuery Standard SQL" in system_instruction
    assert "_TABLE_SUFFIX" in system_instruction


def test_429_rotates_key_and_succeeds_on_retry(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(429))
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.client_api_keys) == 2
    # The second Client() construction must use a *different* key than the first.
    assert harness.client_api_keys[0] != harness.client_api_keys[1]

    # Exactly one retry event, streamed before the retry itself happened -
    # see stream_translation()'s comment on why it's yielded before
    # time.sleep() rather than after.
    assert len(retry_events) == 1
    assert retry_events[0]["attempt"] == 2
    assert retry_events[0]["maxAttempts"] == 5
    assert retry_events[0]["rotatedKey"] is True
    assert retry_events[0]["delaySeconds"] == env.translate_routes.GEMINI_RETRY_DELAY_SECONDS


def test_server_error_retries_with_same_key(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(500))
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    # Consuming the streamed body is what actually drives the generator
    # through its retry (see stream_translation()/run_wsgi_app's
    # buffer-then-chain behavior - Werkzeug's test client only executes a
    # streamed response up to its first yielded line as part of .post()
    # itself; everything after that first "retrying" line - here, the
    # actual retry Gemini call - only runs once the body is actually read,
    # same as get_data()/parse_translate_stream() does below). Assert on
    # the harness's retry-driven state AFTER parsing, not before.
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    # Server-error retries don't rotate keys, and - unlike the 429/rotate
    # path - never reconstruct genai.Client() at all: the same client
    # object is just called again. So exactly one Client() construction,
    # but two generate_content() calls (the failed attempt + the retry).
    assert len(harness.client_api_keys) == 1
    assert len(harness.generate_calls) == 2


def test_non_retryable_error_fails_immediately_and_reports_failure_in_body(app_factory, monkeypatch):
    # Status is 200, not 500 - see this module's docstring on why a
    # streamed response can't carry a real error status. Nothing here has
    # actually streamed a retry line (there was none to stream - the
    # failure is non-retryable), but the HTTP status is decided once, for
    # every request that reaches the retry loop at all, not per-outcome.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(400))  # bad request - _classify_gemini_error returns None

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is False
    assert len(harness.generate_calls) == 1  # no retry attempted


def test_exhausts_all_retry_attempts_and_reports_failure_in_body(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    for _ in range(env.translate_routes.MAX_GEMINI_ATTEMPTS):
        harness.queue_error(FakeApiError(429))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    # Body consumption drives the rest of the retry loop - see the comment
    # in test_server_error_retries_with_same_key above - so parse first,
    # then assert on the fully-driven harness state.
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert "error" in data
    # A retry line is streamed before each of the first MAX-1 attempts'
    # retries - the MAX-th (final) attempt's failure ends the loop without
    # one more retry to announce.
    assert len(retry_events) == env.translate_routes.MAX_GEMINI_ATTEMPTS - 1
    assert len(harness.generate_calls) == env.translate_routes.MAX_GEMINI_ATTEMPTS


def test_max_gemini_attempts_defaults_to_5(app_env):
    assert app_env.translate_routes.MAX_GEMINI_ATTEMPTS == 5


def test_gemini_retry_delay_seconds_defaults_to_1(app_env):
    assert app_env.translate_routes.GEMINI_RETRY_DELAY_SECONDS == 1


def test_max_gemini_attempts_env_var_overrides_default(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2",
        "MAX_GEMINI_ATTEMPTS": "2",
    })
    assert env.translate_routes.MAX_GEMINI_ATTEMPTS == 2
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(429))
    harness.queue_error(FakeApiError(429))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False
    # Stopped after the configured 2 attempts, not the default 5 - proves
    # the env var actually drives the retry loop, not just the constant.
    assert len(harness.generate_calls) == 2


def test_gemini_retry_delay_seconds_env_var_is_used_as_sleep_duration(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "GEMINI_RETRY_DELAY_SECONDS": "3.5",
    })
    assert env.translate_routes.GEMINI_RETRY_DELAY_SECONDS == 3.5
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeApiError(500))
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    # Parse (fully drives the retry loop, including the actual sleep()
    # call - see the comment in test_server_error_retries_with_same_key
    # above) before checking sleep_calls.
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert retry_events[0]["delaySeconds"] == 3.5
    assert sleep_calls == [3.5]


def test_sets_session_cookie(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))
    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert "crbot_session_id" in resp.headers.get("Set-Cookie", "")


def test_pick_gemini_api_key_returns_none_when_no_keys_configured(app_env):
    assert app_env.translate_routes.pick_gemini_api_key() is None


def test_pick_gemini_api_key_avoids_excluded_when_alternative_exists(app_factory):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "key-a,key-b"})
    picked = env.translate_routes.pick_gemini_api_key(exclude={"key-a"})
    assert picked == "key-b"


def test_pick_gemini_api_key_falls_back_to_full_pool_when_all_excluded(app_factory):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "key-a"})
    picked = env.translate_routes.pick_gemini_api_key(exclude={"key-a"})
    assert picked == "key-a"
