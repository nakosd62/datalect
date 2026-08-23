"""
translate_routes.py: /api/translate. Patches translate_routes.genai.Client
with a fake that queues canned responses/exceptions - never talks to the
real Gemini API. types.Content/types.Part/types.GenerateContentConfig are
left as the real google-genai classes (plain data containers, no network
calls), so contents/config shape is exercised for real.

The Claude/Anthropic provider path (LLM_PROVIDER=claude - see that section
further down) is covered the same way: ClaudeHarness patches
translate_routes.anthropic.Anthropic with a fake that queues canned
responses/exceptions, never talking to the real Claude API either. Its
Fake*Error classes subclass the real anthropic exception types directly
(anthropic.RateLimitError/APIStatusError/APIConnectionError), because
_classify_claude_error does real isinstance() checks against those types -
unlike Gemini's plain .code-duck-typing via FakeApiError above, a stand-in
that merely looked similar wouldn't satisfy those checks.

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

import anthropic
import pytest

from helpers import install_fake_bigquery, install_fake_mssql_connect, parse_translate_stream, write_database_presets_file


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
    # The preset has no explicit "id" in the fixture above, so it falls
    # back to "{type}+{name}" (see app_config.py's DATABASE_PRESETS_FILE
    # comment) - "bigquery+BQ" here.
    env.app_config.state_store.set_session(
        "global", connection_id="bigquery+BQ", is_custom=False,
    )

    env.client.post('/api/translate', json={'prompt': 'hi'})
    system_instruction = harness.generate_calls[0]["config"].system_instruction
    assert "BigQuery Standard SQL" in system_instruction
    assert "_TABLE_SUFFIX" in system_instruction


def test_mssql_dialect_intro_used_when_active_connection_is_mssql(app_factory, tmp_path, monkeypatch):
    presets_path = write_database_presets_file(tmp_path, [
        {"type": "mssql", "name": "MS", "host": "h", "database": "d", "user": "u", "password": "p"},
    ])
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "DATABASE_PRESETS_FILE": presets_path,
    })
    install_fake_mssql_connect(monkeypatch)  # so get_database_schema()'s connect() doesn't hit a real server
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    # Same "make the preset the active session connection" approach as the
    # BigQuery test above - "mssql+MS" is the {type}+{name} fallback id
    # (see app_config.py's DATABASE_PRESETS_FILE comment).
    env.app_config.state_store.set_session(
        "global", connection_id="mssql+MS", is_custom=False,
    )

    env.client.post('/api/translate', json={'prompt': 'hi'})
    system_instruction = harness.generate_calls[0]["config"].system_instruction
    assert "Microsoft SQL Server" in system_instruction
    assert "SELECT TOP" in system_instruction
    assert "GO statement" in system_instruction
    # Regression guard: this dialect has no session-level default-schema
    # override (see backends/mssql.py's module docstring), so the prompt
    # must tell Gemini to always reuse the schema-qualified names shown in
    # the schema section - the previous wording ("do not schema-qualify...")
    # was actively wrong whenever a connection's configured schema differs
    # from the connecting login's own default schema, and produced
    # unqualified SQL that failed with "Invalid object name".
    assert "schema-qualified" in system_instruction
    assert "do not schema-qualify" not in system_instruction.lower()


def test_schema_precedes_history_and_is_not_glued_to_the_new_prompt(app_factory, monkeypatch):
    """Regression guard for the system -> schema -> history -> new-prompt
    ordering translate_query() builds (see its long comment on why - it's
    what makes the schema a stable, repeatable prefix a future caching
    pass could rely on): the schema text is prepended to the FIRST content
    item (history's oldest turn, when there is history), not glued onto
    the ever-different new prompt at the end. This is the Gemini-side
    counterpart to the same-named Claude tests further down."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 2;"))

    history = [{"role": "user", "text": "show users"}]
    env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})

    contents = harness.generate_calls[0]["contents"]
    assert contents[0].parts[0].text == "Database Schema:\nNo schema description available.\n\nshow users"
    assert "Database Schema:" not in contents[-1].parts[0].text
    assert "now show orders" in contents[-1].parts[0].text


def test_429_rotates_key_and_retries_immediately_with_no_delay(app_factory, monkeypatch):
    # A 429 (per-key rate limit/capacity exhausted) rotates to a different
    # key and retries right away - no TRANSLATION_RETRY_DELAY_SECONDS wait,
    # since the next attempt already isn't subject to whatever limit the
    # failed key just hit. See _classify_gemini_error's comment for why this
    # differs from the 5xx/same-key case (test_server_error_retries_with_
    # same_key/test_translation_retry_delay_seconds_env_var_is_used_as_
    # sleep_duration below), which DOES wait. This retry budget is governed
    # by the number of configured Gemini keys (2 here), NOT by
    # MAX_TRANSLATION_ATTEMPTS - see test_gemini_key_rotation_exhaustion_is_
    # independent_of_max_translation_attempts below for the dedicated
    # regression guard on that independence.
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2",
        # A conspicuously large, non-default delay - if this leaked into
        # the 429 path at all (even a stray non-zero value), the request
        # would visibly hang for 3.5s if the sleep() patch below weren't
        # in place, or sleep_calls would show it. Set high enough that any
        # regression back to using it would be unmistakable.
        "TRANSLATION_RETRY_DELAY_SECONDS": "3.5",
    })
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
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
    # attempt/maxAttempts here are in terms of the Gemini key-rotation
    # budget (2 configured keys), not MAX_TRANSLATION_ATTEMPTS (which
    # defaults to 5 and is irrelevant to this retry kind).
    assert retry_events[0]["attempt"] == 2
    assert retry_events[0]["maxAttempts"] == 2
    assert retry_events[0]["rotatedKey"] is True
    assert retry_events[0]["delaySeconds"] == 0
    # time.sleep() isn't called at all for a delay=0 retry (see
    # stream_translation()'s "if retry_action["delay"]:" guard) - not just
    # called with 0.
    assert sleep_calls == []


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
    """429s exhaust Gemini's key-rotation budget, which is sized to the
    number of CONFIGURED keys (2 here) - not to MAX_TRANSLATION_ATTEMPTS.
    A conspicuously large MAX_TRANSLATION_ATTEMPTS is set explicitly to
    prove the two budgets are independent: if key-rotation exhaustion were
    still (wrongly) gated on MAX_TRANSLATION_ATTEMPTS, this test would keep
    retrying well past 2 attempts instead of giving up right at 2."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2",
        "MAX_TRANSLATION_ATTEMPTS": "100",
    })
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(429))
    harness.queue_error(FakeApiError(429))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    # Body consumption drives the rest of the retry loop - see the comment
    # in test_server_error_retries_with_same_key above - so parse first,
    # then assert on the fully-driven harness state.
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert "error" in data
    # One retry line streamed before the second (last configured key's)
    # attempt - the second attempt's failure ends the loop, since every
    # configured key has now been tried, without one more retry to announce.
    assert len(retry_events) == 1
    assert len(harness.generate_calls) == 2


def test_gemini_key_rotation_exhaustion_is_independent_of_max_translation_attempts(app_factory, monkeypatch):
    """Dedicated regression guard for the user-facing requirement that the
    Gemini key-rotation retry count is 'independent of what controls the
    LLM transient errors': with only 1 configured key and a generously
    large MAX_TRANSLATION_ATTEMPTS, a 429 must give up after exactly 1
    attempt (no second key to rotate to) rather than retrying up to the
    transient-error budget."""
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_TRANSLATION_ATTEMPTS": "100",
    })
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(429))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert retry_events == []  # no second key to rotate to - gave up immediately
    assert len(harness.generate_calls) == 1


def test_max_translation_attempts_defaults_to_5(app_env):
    assert app_env.translate_routes.MAX_TRANSLATION_ATTEMPTS == 5


def test_translation_retry_delay_seconds_defaults_to_1(app_env):
    assert app_env.translate_routes.TRANSLATION_RETRY_DELAY_SECONDS == 1


def test_max_translation_attempts_env_var_overrides_default(app_factory, monkeypatch):
    # Uses a 500 (transient, same-key/delay path) rather than a 429, since
    # MAX_TRANSLATION_ATTEMPTS governs only the shared transient-error
    # budget - a 429's key-rotation budget is sized by configured key count
    # instead (see test_exhausts_all_retry_attempts_and_reports_failure_in_
    # body / test_gemini_key_rotation_exhaustion_is_independent_of_max_
    # translation_attempts above).
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "MAX_TRANSLATION_ATTEMPTS": "2",
    })
    assert env.translate_routes.MAX_TRANSLATION_ATTEMPTS == 2
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(500))
    harness.queue_error(FakeApiError(500))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False
    # Stopped after the configured 2 attempts, not the default 5 - proves
    # the env var actually drives the retry loop, not just the constant.
    assert len(harness.generate_calls) == 2


def test_translation_retry_delay_seconds_env_var_is_used_as_sleep_duration(app_factory, monkeypatch):
    env = app_factory(env={
        "GEMINI_PRESET_KEYS": "fake-key-1",
        "TRANSLATION_RETRY_DELAY_SECONDS": "3.5",
    })
    assert env.translate_routes.TRANSLATION_RETRY_DELAY_SECONDS == 3.5
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


# --- History result-row truncation for the LLM (HISTORY_RESULT_MAX_ROWS) ---
# format_results_table_text()/build_gemini_history_contents()/
# build_claude_history_messages() cap how many rows of a PAST turn's query
# results get replayed into the model's context, while still reporting the
# real row count fetched from the database in a header line ("N row(s)
# total, showing M"). This only affects the text built for the LLM call -
# it reads `history` (the client-supplied list of {role, text, results}
# dicts) without ever mutating it, so none of this touches what the
# client itself stores/renders when the UI steps back into history; that
# stays the full result set the client already has. Shared logic between
# the two providers (format_results_table_text/the row_count-vs-shown_rows
# math is identical in both builders), so each behavior below gets one
# test per provider rather than being Gemini/Claude-exclusive like the
# sections above/below this one.


def _make_history_with_results(rows, row_count):
    return [{
        "role": "model",
        "text": "SELECT * FROM t;",
        "results": [{"columns": ["id"], "rows": rows, "rowCount": row_count}],
    }]


def test_gemini_history_result_not_truncated_when_it_fits_under_the_default_cap(app_env):
    """Default HISTORY_RESULT_MAX_ROWS is 50 - a 5-row result passes
    through untouched, with the header reporting the same count twice
    (the real rowCount, and how many are shown - equal since nothing was
    cut)."""
    rows = [[i] for i in range(5)]
    history = _make_history_with_results(rows, row_count=5)

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "[Query Result 1 - 5 row(s) total, showing 5]" in text
    for i in range(5):
        assert f"[{i}]" in text


def test_gemini_history_result_truncated_to_env_var_but_reports_real_row_count(app_factory):
    """The row data actually included in the LLM's context is cut down to
    HISTORY_RESULT_MAX_ROWS (here overridden small, to keep the test
    data short), but the header still reports the REAL row count fetched
    from the database (4213) - not just how many rows made it into the
    text (3). The two figures are deliberately different in this test
    data to prove the header isn't just echoing len(rows)."""
    env = app_factory(env={"HISTORY_RESULT_MAX_ROWS": "3"})
    rows = [[i] for i in range(5)]  # 5 rows offered, only 3 should show
    history = _make_history_with_results(rows, row_count=4213)

    contents = env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "[Query Result 1 - 4213 row(s) total, showing 3]" in text
    for i in range(3):
        assert f"[{i}]" in text
    for i in range(3, 5):
        assert f"[{i}]" not in text
    # The client-supplied history object itself is never mutated - the
    # UI's own "step back into history" view (built from this same object
    # elsewhere) still has all 5 rows.
    assert len(history[0]["results"][0]["rows"]) == 5


def test_claude_history_result_not_truncated_when_it_fits_under_the_default_cap(app_env):
    rows = [[i] for i in range(5)]
    history = _make_history_with_results(rows, row_count=5)

    messages = app_env.translate_routes.build_claude_history_messages(history)
    assert "[Query Result 1 - 5 row(s) total, showing 5]" in messages[0]["content"]
    for i in range(5):
        assert f"[{i}]" in messages[0]["content"]


def test_claude_history_result_truncated_to_env_var_but_reports_real_row_count(app_factory):
    env = app_factory(env={"HISTORY_RESULT_MAX_ROWS": "3"})
    rows = [[i] for i in range(5)]
    history = _make_history_with_results(rows, row_count=4213)

    messages = env.translate_routes.build_claude_history_messages(history)
    content = messages[0]["content"]
    assert "[Query Result 1 - 4213 row(s) total, showing 3]" in content
    for i in range(3):
        assert f"[{i}]" in content
    for i in range(3, 5):
        assert f"[{i}]" not in content
    assert len(history[0]["results"][0]["rows"]) == 5


def test_history_result_truncation_reaches_the_real_gemini_call(app_factory, monkeypatch):
    """End-to-end version of the two unit tests above: proves the
    truncated-but-accurately-labeled text actually reaches the contents
    Gemini is called with, through the full /api/translate route (schema
    fetch, dialect intro, history building) rather than just the builder
    function in isolation."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1", "HISTORY_RESULT_MAX_ROWS": "2"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 2;"))

    history = _make_history_with_results([[0], [1], [2], [3]], row_count=9999)
    env.client.post('/api/translate', json={'prompt': 'now what', 'history': history})

    contents = harness.generate_calls[0]["contents"]
    history_text = contents[0].parts[0].text  # schema is prepended here too, but the header text is still present
    assert "[Query Result 1 - 9999 row(s) total, showing 2]" in history_text
    assert "[2]" not in history_text
    assert "[3]" not in history_text


def test_history_result_truncation_reaches_the_real_claude_call(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1", "HISTORY_RESULT_MAX_ROWS": "2"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = _make_history_with_results([[0], [1], [2], [3]], row_count=9999)
    env.client.post('/api/translate', json={'prompt': 'now what', 'history': history})

    messages = harness.create_calls[0]["messages"]
    # Sole history turn is also the cache_control boundary (see the
    # caching section below), so its content is block form.
    history_text = messages[0]["content"][0]["text"]
    assert "[Query Result 1 - 9999 row(s) total, showing 2]" in history_text
    assert "[2]" not in history_text
    assert "[3]" not in history_text


# --- History turn-count cap (HISTORY_MAX_TURNS) ---
# Separate lever from HISTORY_RESULT_MAX_ROWS above: that one trims the row
# data WITHIN a turn's results; this one drops whole OLDER turns outright.
# translate_query() applies it once, up front (history[-(HISTORY_MAX_TURNS *
# 2):]), before either provider's history-builder function ever sees the
# list - so both providers get the same cap for free without their own
# builder needing to know about it. Also exposed read-only via /api/config
# (history_max_turns) so the client's own turn-navigation cap (chatStore in
# client.js) can match this exactly - see config_routes.py's import of this
# constant.


def test_history_max_turns_defaults_to_10(app_env):
    assert app_env.translate_routes.HISTORY_MAX_TURNS == 10


def test_history_sent_to_gemini_is_capped_to_history_max_turns(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1", "HISTORY_MAX_TURNS": "2"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 4;"))

    history = []
    for i in range(3):  # 3 turns offered, cap is 2 - the oldest must be dropped entirely
        history.append({"role": "user", "text": f"prompt {i}"})
        history.append({"role": "model", "text": f"SELECT {i};"})
    env.client.post('/api/translate', json={'prompt': 'newest prompt', 'history': history})

    contents = harness.generate_calls[0]["contents"]
    # 2 surviving turns (4 entries) + the new prompt appended = 5.
    assert len(contents) == 5
    all_text = "\n".join(c.parts[0].text for c in contents)
    assert "prompt 0" not in all_text
    assert "SELECT 0;" not in all_text
    assert "prompt 1" in all_text
    assert "prompt 2" in all_text


def test_history_sent_to_claude_is_capped_to_history_max_turns(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1", "HISTORY_MAX_TURNS": "2"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 4;"))

    history = []
    for i in range(3):
        history.append({"role": "user", "text": f"prompt {i}"})
        history.append({"role": "model", "text": f"SELECT {i};"})
    env.client.post('/api/translate', json={'prompt': 'newest prompt', 'history': history})

    messages = harness.create_calls[0]["messages"]
    assert len(messages) == 5
    # messages[0]'s content is block form (cache_control boundary - see the
    # caching section above); the rest are plain strings.
    all_text = "\n".join(
        (m["content"][0]["text"] if isinstance(m["content"], list) else m["content"])
        for m in messages
    )
    assert "prompt 0" not in all_text
    assert "SELECT 0;" not in all_text
    assert "prompt 1" in all_text
    assert "prompt 2" in all_text


def test_config_exposes_history_max_turns_default(app_env):
    """The client (webClient/client.js's chatStore) reads this via
    /api/config to keep its own turn-navigation cap in sync with what
    /api/translate actually replays to the LLM - see config_routes.py's
    import of HISTORY_MAX_TURNS."""
    data = app_env.client.get('/api/config').get_json()
    assert data['history_max_turns'] == 10


def test_config_exposes_history_max_turns_env_override(app_factory):
    env = app_factory(env={"HISTORY_MAX_TURNS": "3"})
    data = env.client.get('/api/config').get_json()
    assert data['history_max_turns'] == 3


# --- Claude provider path (LLM_PROVIDER=claude) ---
# translate_query() branches to _call_claude/_classify_claude_error/
# build_claude_history_messages/pick_claude_api_key instead of their Gemini
# counterparts whenever LLM_PROVIDER=claude - see translate_routes.py's
# module-level LLM_PROVIDER comment. Everything upstream of that branch
# (dialect intro selection, schema fetch, NDJSON streaming shape, markdown
# fence stripping) is shared code already covered by the Gemini tests above,
# so the tests below focus on what's actually different: API key selection/
# fallback, the Claude SDK call shape, its own retry-classification rules
# (529 "overloaded" and connection errors have no Gemini equivalent), and
# history-role mapping ("model" -> "assistant").


class FakeClaudeResponse:
    def __init__(self, text, input_tokens=10, output_tokens=5, cache_read_tokens=0):
        self.content = [pytypes.SimpleNamespace(type="text", text=text)]
        self.usage = pytypes.SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_tokens,
        )


class ClaudeHarness:
    def __init__(self):
        self.queue = []  # list of FakeClaudeResponse or Exception instances
        self.client_api_keys = []  # api_key each Anthropic(...) was constructed with
        self.create_calls = []  # kwargs of each messages.create call

    def queue_response(self, resp):
        self.queue.append(resp)

    def queue_error(self, exc):
        self.queue.append(exc)

    def make_client_class(self):
        harness = self

        class FakeMessages:
            def create(self, model, max_tokens, system, messages):
                harness.create_calls.append(
                    {"model": model, "max_tokens": max_tokens, "system": system,
                     "messages": messages, "api_key": harness.client_api_keys[-1]}
                )
                if not harness.queue:
                    raise AssertionError("ClaudeHarness queue exhausted - test didn't queue enough responses")
                item = harness.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, api_key=None):
                self.api_key = api_key
                harness.client_api_keys.append(api_key)
                self.messages = FakeMessages()

        return FakeClient


class FakeClaudeRateLimitError(anthropic.RateLimitError):
    """A real 429 response raises anthropic.RateLimitError specifically,
    not a generic APIStatusError(status_code=429) - _classify_claude_error
    checks for this exact type first, ahead of the generic
    APIStatusError/status_code branch below, so the fake needs to actually
    be one. Bypasses APIStatusError's real __init__ (which requires a live
    httpx2 Response/Request) since only .status_code is ever read."""
    def __init__(self):
        Exception.__init__(self, "fake rate limit")
        self.status_code = 429


class FakeClaudeStatusError(anthropic.APIStatusError):
    """Generic fake for any other status code (529 overloaded, other 5xx,
    or a non-retryable 4xx like 400) - same __init__-bypass reasoning as
    FakeClaudeRateLimitError above."""
    def __init__(self, status_code):
        Exception.__init__(self, f"fake status {status_code}")
        self.status_code = status_code


class FakeClaudeConnectionError(anthropic.APIConnectionError):
    """No Gemini equivalent - google-genai's retry policy classifies purely
    on HTTP-style status code (_gemini_error_code), with nothing like this
    connection-level exception type. Same __init__-bypass as above."""
    def __init__(self):
        Exception.__init__(self, "fake connection error")


def test_claude_missing_api_key_returns_400(app_factory):
    env = app_factory(env={"LLM_PROVIDER": "claude"})
    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    assert resp.status_code == 400
    assert "Claude API key is not configured." in resp.get_json()['error']


def test_claude_success_strips_markdown_fences_and_returns_token_counts(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("```sql\nSELECT * FROM users;\n```", input_tokens=20, output_tokens=8))

    resp = env.client.post('/api/translate', json={'prompt': 'Show all users'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is True
    assert data['sql'] == "SELECT * FROM users;"
    assert data['input_tokens'] == 20
    assert data['output_tokens'] == 8
    assert data['total_tokens'] == 28
    # This app doesn't use extended thinking or prompt caching on the
    # Claude path (see _call_claude's docstring), so these are always 0
    # rather than provider-specific missing fields.
    assert data['thinking_tokens'] == 0
    assert data['cached_content_tokens'] == 0


def test_claude_success_records_translation_history(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.set_cookie("crbot_user_id", "alice@example.com")
    env.client.post('/api/translate', json={'prompt': 'give me one'})

    rows, stats, total_count = env.app_config.state_store.get_translation_history("alice@example.com")
    assert total_count == 1
    assert rows[0]['sql_command'] == "SELECT 1;"


def test_claude_no_temperature_param_is_ever_passed(app_factory, monkeypatch):
    """Regression guard for _call_claude's documented reason for omitting
    temperature: claude-sonnet-5 and later reject sampling params outright.
    FakeMessages.create()'s signature above has no temperature parameter at
    all, so passing one would raise TypeError rather than silently
    accepting it - proving the real call site never does."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is True


def test_claude_dialect_intro_reaches_the_system_param(app_factory, tmp_path, monkeypatch):
    """Dialect-intro selection is keyed off Backend.dialect_name, not the
    LLM provider (see the Gemini mssql-dialect test above) - this just
    proves it's still wired through correctly on the Claude call path,
    where the system prompt is messages.create()'s `system` kwarg instead
    of GenerateContentConfig.system_instruction."""
    presets_path = write_database_presets_file(tmp_path, [
        {"type": "mssql", "name": "MS", "host": "h", "database": "d", "user": "u", "password": "p"},
    ])
    env = app_factory(env={
        "LLM_PROVIDER": "claude",
        "ANTHROPIC_API_KEY": "fake-key-1",
        "DATABASE_PRESETS_FILE": presets_path,
    })
    install_fake_mssql_connect(monkeypatch)
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.app_config.state_store.set_session(
        "global", connection_id="mssql+MS", is_custom=False,
    )

    env.client.post('/api/translate', json={'prompt': 'hi'})
    # system is a one-block list now (see test_claude_system_prompt_is_
    # cache_control_marked below for why) rather than a plain string.
    system_instruction = harness.create_calls[0]["system"][0]["text"]
    assert "Microsoft SQL Server" in system_instruction
    assert "schema-qualified" in system_instruction


def test_claude_history_uses_assistant_role_and_appends_results(app_factory, monkeypatch):
    """build_claude_history_messages() maps Gemini's "model" role to
    Claude's "assistant" (Claude has no "model" role) and leaves "user"
    untouched, appending query-results text exactly like
    build_gemini_history_contents() does for the Gemini path. (Schema
    placement and cache_control marking on messages[0]/messages[-2] are
    covered by the dedicated tests below.)"""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = [
        {"role": "user", "text": "show users"},
        {"role": "model", "text": "SELECT * FROM users;",
         "results": [{"columns": ["id"], "rows": [[1]], "rowCount": 1}]},
    ]
    env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})

    messages = harness.create_calls[0]["messages"]
    # Two history messages, then the new user turn translate_query() appends.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].endswith("show users")
    assert messages[1]["role"] == "assistant"
    # messages[1] is the last history entry, so it's the cache_control
    # boundary (see the dedicated cache-control tests below) - its content
    # is block form now, not a plain string.
    content_text = messages[1]["content"][0]["text"]
    assert "SELECT * FROM users;" in content_text
    assert "[Query Result 1" in content_text
    assert messages[2]["role"] == "user"


def test_claude_schema_precedes_history_and_is_not_glued_to_the_new_prompt(app_factory, monkeypatch):
    """Regression guard for the system -> schema -> history -> new-prompt
    ordering translate_query() builds (see its long comment on why): when
    there IS history, the schema is prepended to the FIRST historical
    message rather than glued onto the ever-changing new prompt - that's
    what makes it a stable, repeatable prefix Claude's cache_control
    marker (see the dedicated tests below) relies on."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = [{"role": "user", "text": "show users"}]
    env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})

    messages = harness.create_calls[0]["messages"]
    # A single-entry history means this message is both the first (schema
    # prepended) AND the last historical turn (cache_control boundary) -
    # content is block form, not a plain string, as a result.
    assert messages[0]["content"][0]["text"] == "Database Schema:\nNo schema description available.\n\nshow users"
    # The new prompt (last message) carries the prompt text but not the
    # schema - that was only ever attached once, up front.
    assert "Database Schema:" not in messages[-1]["content"]
    assert "now show orders" in messages[-1]["content"]


def test_claude_schema_attaches_to_new_prompt_when_there_is_no_history(app_factory, monkeypatch):
    """With no prior history the new prompt IS the first (and only)
    message, so it carries the schema directly - but as two separate
    content blocks (schema, then the new prompt), not one concatenated
    string, so the schema half can be independently cache_control-marked
    (see the dedicated cache-control tests below) even on a conversation's
    very first call."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'show users'})

    messages = harness.create_calls[0]["messages"]
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["text"] == "Database Schema:\nNo schema description available.\n\n"
    assert content[1]["text"] == "User Request: show users\n\nSQL Query:"


# --- Claude prompt caching (cache_control) ---
# Claude has no automatic/implicit caching the way Gemini 2.5+ does (see
# _call_gemini's docstring) - a block is only ever cached if explicitly
# marked with cache_control. These tests pin down where those markers
# land: the system prompt always; the schema block always too, whether
# that's prepended to the last already-accumulated history turn (when
# there is history) or split into its own content block on a
# conversation's very first call (when there isn't) - see
# translate_query()'s comment on why concatenating the schema onto the
# ever-changing new prompt and marking THAT would defeat the point. The
# new prompt itself is never marked, in either case - it's guaranteed to
# differ every call and would gain nothing from caching.


def test_claude_system_prompt_is_cache_control_marked(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'hi'})

    system = harness.create_calls[0]["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_claude_cache_control_marks_last_history_turn_not_the_new_prompt(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 3;"))

    history = [
        {"role": "user", "text": "show users"},
        {"role": "model", "text": "SELECT * FROM users;"},
        {"role": "user", "text": "now filter to active ones"},
        {"role": "model", "text": "SELECT * FROM users WHERE active;"},
    ]
    env.client.post('/api/translate', json={'prompt': 'now just the count', 'history': history})

    messages = harness.create_calls[0]["messages"]
    assert len(messages) == 5  # 4 history turns + the new prompt
    # Only the last history turn (index 3) carries a cache_control marker -
    # not any earlier turn, and not the new prompt appended after it.
    for i, message in enumerate(messages):
        is_marked = isinstance(message["content"], list)
        assert is_marked == (i == 3), f"message {i} marked={is_marked}"
    assert messages[3]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_claude_schema_block_is_cache_control_marked_even_with_no_history(app_factory, monkeypatch):
    """With no history, the sole message still splits into two content
    blocks (see test_claude_schema_attaches_to_new_prompt_when_there_is_no_history
    above): the schema block IS cache_control-marked here - it's the
    single largest, most-repeated-across-conversations block this app
    sends, so it shouldn't have to wait for a second call to start being
    cacheable. The new-prompt block right after it is left unmarked, since
    it ends in the ever-changing prompt text and would gain nothing from
    caching."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'show users'})

    messages = harness.create_calls[0]["messages"]
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in content[1]


def test_claude_reports_cache_read_tokens_via_cached_content_tokens(app_factory, monkeypatch):
    """cached_content_tokens in the NDJSON response is how a caller sees
    whether caching is actually paying off - it's fed from Anthropic's
    usage.cache_read_input_tokens (see _call_claude)."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;", cache_read_tokens=1234))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['cached_content_tokens'] == 1234


def test_claude_default_model_is_claude_sonnet_5(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'hi'})
    assert harness.create_calls[0]["model"] == "claude-sonnet-5"


def test_claude_model_env_var_overrides_default(app_factory, monkeypatch):
    env = app_factory(env={
        "LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1",
        "CLAUDE_MODEL": "claude-opus-x",
    })
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'hi'})
    assert harness.create_calls[0]["model"] == "claude-opus-x"


def test_claude_model_override_via_request_body(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.client.post('/api/translate', json={'prompt': 'hi', 'claude_model': 'claude-x-custom'})
    assert harness.create_calls[0]["model"] == "claude-x-custom"


def test_claude_429_never_rotates_key_and_retries_with_delay(app_factory, monkeypatch):
    """Claude's key-rotation retry was removed - key-rotation is now a
    Gemini-only hack (this app is only known to configure a POOL of keys
    for Gemini, via GEMINI_PRESET_KEYS). Even with multiple CLAUDE_PRESET_
    KEYS configured, a RateLimitError just retries with the SAME key after
    TRANSLATION_RETRY_DELAY_SECONDS, exactly like a 5xx/connection error -
    see _classify_claude_error's docstring."""
    env = app_factory(env={
        "LLM_PROVIDER": "claude",
        "CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeClaudeRateLimitError())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    # Same key reused - no second Anthropic(...) construction at all, even
    # though a second CLAUDE_PRESET_KEYS entry is configured and available.
    assert len(harness.client_api_keys) == 1
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert retry_events[0]["delaySeconds"] == 2.5
    assert sleep_calls == [2.5]


def test_claude_529_overloaded_never_rotates_key_and_retries_with_delay(app_factory, monkeypatch):
    """529 ("overloaded, try again") is Claude-specific - _classify_gemini_
    error has no equivalent status code. Like the 429 case above, this no
    longer rotates keys - it just waits and retries with the same key."""
    env = app_factory(env={
        "LLM_PROVIDER": "claude",
        "CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2",
    })
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeClaudeStatusError(529))
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.client_api_keys) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert retry_events[0]["delaySeconds"] == env.translate_routes.TRANSLATION_RETRY_DELAY_SECONDS
    assert sleep_calls == [env.translate_routes.TRANSLATION_RETRY_DELAY_SECONDS]


def test_claude_server_error_retries_with_same_key(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeClaudeStatusError(500))
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert len(harness.client_api_keys) == 1
    assert len(harness.create_calls) == 2


def test_claude_connection_error_retries_with_same_key(app_factory, monkeypatch):
    """APIConnectionError has no Gemini-side test above - it's a Claude-only
    branch in _classify_claude_error (transient, not key-related, so it
    retries with the same key after TRANSLATION_RETRY_DELAY_SECONDS, exactly
    like a 5xx APIStatusError)."""
    env = app_factory(env={
        "LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeClaudeConnectionError())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert retry_events[0]["rotatedKey"] is False
    assert retry_events[0]["delaySeconds"] == 2.5
    assert sleep_calls == [2.5]
    assert len(harness.client_api_keys) == 1


def test_claude_non_retryable_error_fails_immediately(app_factory, monkeypatch):
    env = app_factory(env={"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeClaudeStatusError(400))  # bad request - _classify_claude_error returns None

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is False
    assert len(harness.create_calls) == 1  # no retry attempted


def test_claude_exhausts_all_retry_attempts_and_reports_failure_in_body(app_factory, monkeypatch):
    """Since Claude no longer rotates keys (see test_claude_429_never_
    rotates_key_and_retries_with_delay above), a run of RateLimitErrors now
    exhausts the shared transient-error budget (MAX_TRANSLATION_ATTEMPTS),
    not a key-rotation budget - configuring 2 CLAUDE_PRESET_KEYS here is
    deliberate: it proves the extra key is never touched (only one
    Anthropic(...) client is ever constructed) even though it's available."""
    env = app_factory(env={"LLM_PROVIDER": "claude", "CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    for _ in range(env.translate_routes.MAX_TRANSLATION_ATTEMPTS):
        harness.queue_error(FakeClaudeRateLimitError())

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert "error" in data
    assert len(retry_events) == env.translate_routes.MAX_TRANSLATION_ATTEMPTS - 1
    assert len(harness.create_calls) == env.translate_routes.MAX_TRANSLATION_ATTEMPTS
    assert len(harness.client_api_keys) == 1
    assert all(action["rotatedKey"] is False for action in retry_events)


def test_pick_claude_api_key_returns_none_when_no_keys_configured(app_env):
    assert app_env.translate_routes.pick_claude_api_key() is None


def test_pick_claude_api_key_avoids_excluded_when_alternative_exists(app_factory):
    env = app_factory(env={"CLAUDE_PRESET_KEYS": "key-a,key-b"})
    picked = env.translate_routes.pick_claude_api_key(exclude={"key-a"})
    assert picked == "key-b"


def test_pick_claude_api_key_falls_back_to_full_pool_when_all_excluded(app_factory):
    env = app_factory(env={"CLAUDE_PRESET_KEYS": "key-a"})
    picked = env.translate_routes.pick_claude_api_key(exclude={"key-a"})
    assert picked == "key-a"


def test_get_claude_api_keys_returns_empty_list_when_nothing_configured(app_env):
    assert app_env.translate_routes.get_claude_api_keys() == []


def test_get_claude_api_keys_falls_back_to_anthropic_api_key_when_no_preset_keys(app_factory):
    env = app_factory(env={"ANTHROPIC_API_KEY": "sk-ant-single"})
    assert env.translate_routes.get_claude_api_keys() == ["sk-ant-single"]


def test_get_claude_api_keys_prefers_preset_keys_over_single_var(app_factory):
    # CLAUDE_PRESET_KEYS is the pool for load-balancing across several paid
    # keys; ANTHROPIC_API_KEY is only the single-account fallback - when
    # both are set, the pool wins (see get_claude_api_keys's docstring).
    env = app_factory(env={
        "CLAUDE_PRESET_KEYS": "key-a,key-b",
        "ANTHROPIC_API_KEY": "sk-ant-single",
    })
    assert env.translate_routes.get_claude_api_keys() == ["key-a", "key-b"]
