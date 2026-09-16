"""
translate_routes.py: /api/translate. Patches translate_routes.genai.Client
with a fake that queues canned responses/exceptions - never talks to the
real Gemini API. types.Content/types.Part/types.GenerateContentConfig are
left as the real google-genai classes (plain data containers, no network
calls), so contents/config shape is exercised for real.

The Anthropic (Claude) provider path (selected by saving llm_provider=
"anthropic" on the test session - see helpers.select_llm_provider() and
that section further down) is covered the same way: ClaudeHarness patches
translate_routes.anthropic.Anthropic with a fake that queues canned
responses/exceptions, never talking to the real Claude API either. Its
Fake*Error classes subclass the real anthropic exception types directly
(anthropic.RateLimitError/APIStatusError/APIConnectionError), because
_classify_claude_error does real isinstance() checks against those types -
unlike Gemini's plain .code-duck-typing via FakeApiError above, a stand-in
that merely looked similar wouldn't satisfy those checks.

The OpenAI provider path (selected the same way - see that section further
down, after Anthropic's) follows the exact same pattern once more:
OpenAiHarness patches translate_routes.openai.OpenAI with a fake whose
.responses.create(...) queues canned responses/exceptions - this app's
OpenAI integration is built on the Responses API, not the older Chat
Completions API (see translate_routes.py's _call_openai docstring). Its
Fake*Error classes likewise subclass the real openai exception types
(openai.RateLimitError/InternalServerError/APIConnectionError/
BadRequestError) for the same isinstance()-check reason.

None of the three providers' dispatch goes through hand-rolled
if/elif branches anymore - translate_query()/stream_translation() call
methods on an LlmProvider object (get_llm_provider(session_data.get(
'llm_provider')) - see that function's and the LlmProvider class's
docstrings in translate_routes.py; there's no separate LLM_PROVIDER env
var anymore - a fresh session with nothing saved falls back to the one
fleet-wide default, Google/gemini-3.6-flash unless DEFAULT_MODEL names a
model belonging to a different provider - see get_llm_provider()'s and
_default_fleet_provider()'s docstrings). The tests below don't test
that class directly except in a small dedicated section near the end;
they exercise it the same way they always exercised the old if/elif
branches - through /api/translate with the session's saved llm_provider
set via helpers.select_llm_provider() (a thin wrapper around
state_store.set_session() - see that helper's docstring) - since that's
what actually matters and it's what would break if the dispatch ever
picked the wrong provider.

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

IMPORTANT, and easy to trip over: env.client.post('/api/translate', ...)
must always be captured and its body actually read (parse_translate_stream(
resp), or resp.get_json()/get_data() for the two early-validation cases
above) - even in a test that only cares about a side effect (translation
history being recorded, the fake LLM client having been called with a
particular shape, ...) and never inspects the response body itself. This
is NOT optional bookkeeping: Werkzeug's test client only pulls the FIRST
item out of a stream_with_context()-wrapped generator response to build
its status/headers - it does NOT drain the rest unless something actually
reads the body afterward. Since stream_translation() now yields two
phase_status lines (see translate_routes.py's module docstring) before
the single-connection path's real work (schema lookup, the LLM call,
record_translation()), a bare `env.client.post(...)` with the return value
discarded only advances the generator to that first phase_status line and
leaves it suspended there - record_translation() and everything after it
never runs, and the fake LLM client never gets called. Every test below
was written before those two lines existed, when the very first (and
only, in the no-retry case) yield was the terminal "done" line itself -
meaning the entire function body, side effects included, had already run
by the time a bare post() pulled that one item. That was always an
implicit dependency on stream_translation()'s exact yield order, not a
guarantee - it just happened to hold until this comment was added.
"""

import json
import sqlite3
import types as pytypes

import anthropic
import httpx
import openai
import pytest

from helpers import (
    install_fake_bigquery, install_fake_mssql_connect, parse_translate_stream,
    parse_translate_stream_events, write_database_presets_file, login_as,
    select_llm_provider, set_llm_byok_key,
)


def _translation_rows(env):
    """Every row currently in the translations table, oldest first, as
    plain dicts - a raw query against the same SQLite file app_config's
    state_store is using. The translations audit log is write-only these
    days (see chat_history_routes.py's module docstring for why) - no
    route through the app's own API surfaces nl_prompt/sql_command/
    created_at or the token/duration/database_* columns some tests here
    need to assert on directly, so this is the only way to check them
    (same reasoning as test_connection_router.py's own _translation_rows
    helper)."""
    with sqlite3.connect(env.app_config.state_store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT database_type, database_name, nl_prompt, sql_command, model,
                   duration, input_tokens, output_tokens, total_tokens,
                   thinking_tokens, cached_content_tokens
            FROM translations ORDER BY id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


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
        self.client_http_options = []  # http_options each Client(...) was constructed with
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
            def __init__(self, api_key=None, http_options=None):
                self.api_key = api_key
                harness.client_api_keys.append(api_key)
                harness.client_http_options.append(http_options)
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
    assert "Google API key is not configured." in resp.get_json()['error']


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


def test_success_streams_schema_and_generating_sql_phase_status_before_done(app_factory, monkeypatch):
    """The single-connection path (see stream_translation()'s module
    docstring) emits two {"status": "phase_status", ...} lines ahead of the
    terminal "done" line - one before the schema lookup, one before the LLM
    call - so the client has something better than a bare spinner for the
    two real waits that happen before any SQL comes back. Neither is a
    "retrying" line, so parse_translate_stream's retry_events/final_data
    split (used by every other test in this file) is unaffected by their
    presence - this test uses parse_translate_stream_events instead, since
    it needs to see every line, not just the terminal one."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'give me one'})
    events = parse_translate_stream_events(resp)

    phase_statuses = [e for e in events if e.get("status") == "phase_status"]
    assert [e["phase"] for e in phase_statuses] == ["schema", "generating_sql"]
    assert all(isinstance(e["message"], str) and e["message"] for e in phase_statuses)

    # Both phase_status lines come before the terminal "done" line, and
    # neither is mistaken for it.
    assert events[-1]["status"] == "done"
    assert events[-1]["success"] is True


def test_success_records_translation_history(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    env.client.set_cookie("crbot_user_id", "alice@example.com")
    resp = env.client.post('/api/translate', json={'prompt': 'give me one'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['sql_command'] == "SELECT 1;"


def test_postgres_dialect_intro_used_by_default(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
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

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
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

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
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
    resp = env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    contents = harness.generate_calls[0]["contents"]
    assert contents[0].parts[0].text == "Database Schema:\nNo schema description available.\n\nshow users"
    assert "Database Schema:" not in contents[-1].parts[0].text
    assert "now show orders" in contents[-1].parts[0].text


# --- Language verification for the single-connection '*** NO SQL ***' -----
# free-text reply (stream_translation()'s own retry loop) - regression
# guard for the gap _COMMON_FORMAT_RULES' bare "write this in the user's
# language" instruction line used to leave open with nothing verifying the
# model actually complied (the exact gap the summarization calls'
# _detect_language/_summarize_with_retry machinery was already fixed for -
# see that section's own comment in translate_routes.py). fresh_import()
# always substitutes a fake, always-returns-nothing language identifier
# (see helpers.py's FakeLanguageIdentifier) so the real py3langid model
# isn't reloaded on every single test - these tests monkeypatch
# translate_routes._detect_language directly with a small, deterministic
# stand-in so the retry/failure behavior can be exercised without depending
# on real language classification.


def test_no_sql_reply_in_wrong_language_is_retried_and_corrected(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())

    # Question is in English; the model's first '*** NO SQL ***' reply
    # comes back in German - flagged as a mismatch by the stand-in below -
    # and its second, corrected reply is accepted.
    monkeypatch.setattr(
        env.translate_routes, "_detect_language",
        lambda text: "de" if "Datenbanken" in text else ("en" if text else None),
    )
    harness.queue_response(FakeGenaiResponse("*** NO SQL *** Sie haben 3 Datenbanken konfiguriert."))
    harness.queue_response(FakeGenaiResponse("*** NO SQL *** You have 3 databases configured."))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have?'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['sql'] == "*** NO SQL *** You have 3 databases configured."
    assert len(harness.generate_calls) == 2
    # The retry prompt actually sent to the model must carry the explicit
    # correction naming the mistake - confirms this isn't a coincidental
    # second attempt, but the language-mismatch retry actually firing.
    second_prompt_text = harness.generate_calls[1]["contents"][-1].parts[0].text
    assert "CORRECTION" in second_prompt_text and "German" in second_prompt_text


def test_no_sql_reply_still_wrong_language_after_retry_fails_the_turn(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())

    # Both attempts come back in German - mirrors _summarize_with_retry's
    # own "never knowingly serve a response in the wrong language"
    # guarantee: this must fail the turn outright (an honest, specific
    # error) rather than silently showing text already confirmed wrong.
    monkeypatch.setattr(
        env.translate_routes, "_detect_language",
        lambda text: "de" if "Datenbanken" in text else ("en" if text else None),
    )
    harness.queue_response(FakeGenaiResponse("*** NO SQL *** Sie haben 3 Datenbanken konfiguriert."))
    harness.queue_response(FakeGenaiResponse("*** NO SQL *** Immer noch 3 Datenbanken."))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have?'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert "German" in data['error'] and "English" in data['error']
    assert len(harness.generate_calls) == 2

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert "TRANSLATION_ERROR" in rows[0]['sql_command']


def test_plain_sql_response_never_triggers_a_language_check(app_factory, monkeypatch):
    # Regression guard for _no_sql_language_mismatch's own scoping: a plain
    # generated-SQL response (no '*** NO SQL ***' prefix) must never be run
    # through language detection at all - detect_language on a SELECT
    # statement is meaningless, and this must never cost a second LLM call.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())

    detect_calls = []

    def _spy_detect_language(text):
        detect_calls.append(text)
        return "en"

    monkeypatch.setattr(env.translate_routes, "_detect_language", _spy_detect_language)
    harness.queue_response(FakeGenaiResponse("SELECT * FROM users;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show all users'})
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['sql'] == "SELECT * FROM users;"
    assert len(harness.generate_calls) == 1
    # _detect_language is still called once, on the user's own PROMPT
    # (expected_language_code) - just never on the generated SQL itself.
    assert detect_calls == ['show all users']


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


def test_translation_timeout_seconds_defaults_to_60(app_env):
    assert app_env.translate_routes.TRANSLATION_TIMEOUT_SECONDS == 60


def test_translation_timeout_seconds_env_var_overrides_default(app_factory):
    env = app_factory(env={"TRANSLATION_TIMEOUT_SECONDS": "15"})
    assert env.translate_routes.TRANSLATION_TIMEOUT_SECONDS == 15


def test_gemini_client_is_constructed_with_translation_timeout_in_milliseconds(app_factory, monkeypatch):
    # google-genai's http_options.timeout is milliseconds, unlike anthropic's/
    # openai's plain-seconds `timeout` kwarg - see GeminiProvider.make_client's
    # comment and TRANSLATION_TIMEOUT_SECONDS's docstring.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1", "TRANSLATION_TIMEOUT_SECONDS": "45"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    parse_translate_stream(resp)
    assert len(harness.client_http_options) == 1
    assert harness.client_http_options[0].timeout == 45000


def test_classify_gemini_error_retries_httpx_timeout_with_same_key(app_factory, monkeypatch):
    """A TRANSLATION_TIMEOUT_SECONDS timeout surfaces as a raw
    httpx.TimeoutException (google-genai has no typed timeout exception the
    way anthropic/openai do - see _classify_gemini_error's added case and
    TRANSLATION_TIMEOUT_SECONDS's docstring), and should retry with the same
    key after TRANSLATION_RETRY_DELAY_SECONDS, same as a transient 5xx."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(httpx.TimeoutException("timed out"))
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert len(harness.client_api_keys) == 1  # same client/key reused, no rotation
    assert len(harness.generate_calls) == 2


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
    """Default HISTORY_RESULT_MAX_ROWS is 10 - a 5-row result passes
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
    resp = env.client.post('/api/translate', json={'prompt': 'now what', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    contents = harness.generate_calls[0]["contents"]
    history_text = contents[0].parts[0].text  # schema is prepended here too, but the header text is still present
    assert "[Query Result 1 - 9999 row(s) total, showing 2]" in history_text
    assert "[2]" not in history_text
    assert "[3]" not in history_text


def test_history_result_truncation_reaches_the_real_claude_call(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1", "HISTORY_RESULT_MAX_ROWS": "2"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = _make_history_with_results([[0], [1], [2], [3]], row_count=9999)
    resp = env.client.post('/api/translate', json={'prompt': 'now what', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    messages = harness.create_calls[0]["messages"]
    # Sole history turn is also the cache_control boundary (see the
    # caching section below), so its content is block form.
    history_text = messages[0]["content"][0]["text"]
    assert "[Query Result 1 - 9999 row(s) total, showing 2]" in history_text
    assert "[2]" not in history_text
    assert "[3]" not in history_text


# --- History entries carrying an error, or a stored summary --------------------
# Gap 1/2/3 of "Turn History Handling in Datalect": a turn that concluded with
# an error is still added to history (client.js's executeSql() failure
# branches now call chatStore.pushTurn()/mutate the pending entry, where
# previously they never persisted anything at all), with the error text
# preserved (client.js's summarizeResultForHistory() now returns {error, ...}
# instead of collapsing it to a fake 0-row success). This section proves the
# SERVER side of that: a `results` entry shaped {error: "..."} renders as real
# error text instead of a blank "Columns: \nTotal Rows: 0" block, and a
# turn's own stored summary (`summary` for single-connection turns,
# `allMode.routingMessage` for all-mode turns - which client.js overwrites
# with the real Phase C summary text before persisting, not the earlier
# triage routing message) is appended too, so a later turn's LLM call sees
# what the user was actually told, not just the raw data/errors.


def _make_history_with_error_result(error_text, database=None):
    result = {"error": error_text}
    if database:
        result["database"] = database
    return [{
        "role": "model",
        "text": "SELECT * FROM does_not_exist;",
        "results": [result],
    }]


def test_gemini_history_renders_an_error_result_as_real_text(app_env):
    history = _make_history_with_error_result('relation "does_not_exist" does not exist')

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "[Query Result 1 - failed]" in text
    assert 'relation "does_not_exist" does not exist' in text
    # Not the old blank-block shape this used to silently fall back to.
    assert "Total Rows: 0" not in text


def test_claude_history_renders_an_error_result_as_real_text(app_env):
    history = _make_history_with_error_result("permission denied for table users")

    messages = app_env.translate_routes.build_claude_history_messages(history)
    content = messages[0]["content"]
    assert "[Query Result 1 - failed]" in content
    assert "permission denied for table users" in content
    assert "Total Rows: 0" not in content


def test_openai_history_renders_an_error_result_as_real_text(app_env):
    history = _make_history_with_error_result("statement timeout")

    messages = app_env.translate_routes.build_openai_history_messages(history)
    content = messages[0]["content"]
    assert "[Query Result 1 - failed]" in content
    assert "statement timeout" in content
    assert "Total Rows: 0" not in content


def test_gemini_history_with_mixed_success_and_error_results_renders_both(app_env):
    """A multi-statement turn that partly succeeded before failing (see
    executeSql()'s multi-statement partial-failure branch) carries both
    shapes in the same `results` list - both must render, in order."""
    history = [{
        "role": "model",
        "text": "SELECT 1; SELECT * FROM nope;",
        "results": [
            {"columns": ["n"], "rows": [[1]], "rowCount": 1},
            {"error": "relation \"nope\" does not exist"},
        ],
    }]

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "[Query Result 1 - 1 row(s) total, showing 1]" in text
    assert "[Query Result 2 - failed]" in text
    assert 'relation "nope" does not exist' in text


def test_gemini_history_appends_a_turn_own_stored_summary(app_env):
    """Single-connection turns carry their summary directly as `summary`."""
    history = [{
        "role": "model",
        "text": "SELECT * FROM orders;",
        "results": [{"columns": ["id"], "rows": [[1]], "rowCount": 1}],
        "summary": "There is one order in the system, with id 1.",
    }]

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "There is one order in the system, with id 1." in text


def test_claude_history_appends_an_all_mode_turn_stored_summary(app_env):
    """All-mode turns carry their (final, Phase-C) summary nested under
    `allMode.routingMessage` - see captureAllModeHistory in client.js."""
    history = [{
        "role": "model",
        "text": "-- database: db1\nSELECT 1;",
        "results": [{"columns": ["n"], "rows": [[1]], "rowCount": 1, "database": {"kind": "connection", "id": "db1", "name": "db1"}}],
        "allMode": {
            "routingMessage": "Only db1 had relevant data, which shows a single row.",
            "databaseNotes": [],
            "generationFailures": [],
            "executeFailures": [],
        },
    }]

    messages = app_env.translate_routes.build_claude_history_messages(history)
    assert "Only db1 had relevant data, which shows a single row." in messages[0]["content"]


def test_history_summary_not_duplicated_when_both_summary_and_all_mode_present(app_env):
    """`summary` takes priority over `allMode.routingMessage` when (for
    whatever reason) both are set, rather than appending both - there is
    only ever one real summary for a given turn."""
    history = [{
        "role": "model",
        "text": "SELECT 1;",
        "summary": "The real summary.",
        "allMode": {"routingMessage": "A stale routing message that should not appear."},
    }]

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    text = contents[0].parts[0].text
    assert "The real summary." in text
    assert "A stale routing message that should not appear." not in text


def test_history_entry_with_no_summary_or_results_is_unaffected(app_env):
    """A plain {role, text} turn (no `results`, no `summary`, no `allMode`)
    still renders as just its own text - the new summary/error handling
    must not add anything when there's nothing to add."""
    history = [{"role": "user", "text": "how many orders are there?"}]

    contents = app_env.translate_routes.build_gemini_history_contents(history)
    assert contents[0].parts[0].text == "how many orders are there?"


# --- Google Sheets (GViz) dialect intro: comments stay forbidden ---------------
# GViz has no comment syntax at all (see backends/sheets.py's module
# docstring), so this dialect's intro tells the model to never add one,
# unconditionally - unlike every other dialect, nothing ever asks the model
# to write a '-- database: ...' marker line itself; in "all databases" mode
# that marker is always prepended mechanically, server-side, after
# generation returns (see translate_routes.py's _classify_generation_outcome
# and execute_routes.py's _strip_database_marker_lines), so there's no
# contradiction for this test to guard against beyond the plain rule itself.

def test_sheets_dialect_intro_still_forbids_comments_generally(app_env):
    intro = app_env.translate_routes._DIALECT_PROMPT_INTROS["Google Visualization API Query Language"]
    assert "NEVER add any comments" in intro


# --- PostgreSQL dialect intro: FILTER clause only attaches to one aggregate ----
# Regression guard for a real user-reported failure: the model generated
# "(MAX(total_score) - MIN(total_score)) FILTER (WHERE total_score IS NOT
# NULL)" - Postgres's FILTER clause is only valid immediately after a single
# aggregate function call (including an ordered-set aggregate's WITHIN GROUP
# form), never after a parenthesized expression combining two or more
# aggregates, so this failed with "syntax error at or near FILTER". Guards
# that the dialect intro keeps warning about exactly this mistake.

def test_postgres_dialect_intro_warns_against_filter_on_combined_aggregate_expressions(app_env):
    intro = app_env.translate_routes._DIALECT_PROMPT_INTROS["PostgreSQL"]
    assert "FILTER" in intro
    assert "(MAX(x) - MIN(x)) FILTER (WHERE ...)" in intro


# --- "All databases" mode, Phase C summary prompt: one paragraph per database ---
# The real per-database paragraph breaks (and the brevity of each one) are
# entirely down to what this prompt asks the LLM for. Phase C's response is
# structured JSON now (see _clean_summary_response) rather than free-text
# prose with blank-line-separated paragraphs, so this guards the JSON
# contract itself - the "per_database" object keyed by index, plus the
# separate "cross_database" field for a paragraph spanning multiple
# databases - the only place this behavior is actually specified.
def test_summary_prompt_asks_for_one_paragraph_per_database(app_env):
    instruction = app_env.translate_routes._SUMMARY_SYSTEM_INSTRUCTION
    assert "PER DATABASE" in instruction
    assert "brief" in instruction.lower()
    assert '"per_database"' in instruction
    # The separate cross_database field, not a paragraph folded into
    # per_database - covers a question that genuinely needs something
    # spanning multiple databases (e.g. a grand total) without making that
    # the default shape for every response.
    assert '"cross_database"' in instruction


# --- Phase C summary prompt: must lead with a "Result Summary" line ------
# Mirrors connection_router.py's "Triage" leading-line requirement (see
# test_connection_router.py's test_triage_prompt_requires_answer_and_
# message_to_lead_with_a_triage_line, plus its two label-only-response
# tests) - client.js's renderMarkdownLite() bolds+underlines a standalone
# "Result Summary" line wherever it appears, so this guards the prompt
# text. A real model turned out to sometimes over-comply with "alone on
# its own line" and respond with JUST the label - see
# test_connection_router.py's test_summarize_all_mode_results_retries_a_
# response_that_is_just_the_result_summary_label for the server-side
# retry that now catches that instead of silently showing a bare heading.
def test_summary_prompt_requires_a_leading_translated_results_summary_line(app_env):
    instruction = app_env.translate_routes._SUMMARY_SYSTEM_INSTRUCTION
    # Pluralized ("Results Summary", not "Result Summary") and, per the
    # instruction text, translated into the user's own question's
    # language rather than a fixed literal English phrase - see
    # is_label_only_response's docstring for why the retry-detection logic
    # had to become language-agnostic to match.
    assert "meaning \"Results Summary\"" in instruction
    assert "TRANSLATED into the SAME LANGUAGE as the user's original question" in instruction
    assert "is not a valid response" in instruction


# --- Phase C summary prompt: explain an error, don't just acknowledge it ---
# Regression coverage for a real product request: a database that failed
# used to only get a bare "say so" acknowledgment from this prompt - the
# same treatment as a database that had nothing relevant, even though an
# error carries a real, actionable diagnosis (a permissions problem, a
# timeout, a malformed query) a "no relevant data" note never does. The
# client-side gap this closes (requestAllModeResultsSummary used to skip
# Phase C entirely whenever no database succeeded, even with real errors
# to explain) is covered by multi-database.spec.js's own e2e regression
# test - this one guards the prompt TEXT the model actually gets, the only
# place this instruction is ever expressed.
def test_summary_prompt_instructs_explaining_an_error_not_just_acknowledging_it(app_env):
    instruction = app_env.translate_routes._SUMMARY_SYSTEM_INSTRUCTION
    assert "briefly explain" in instruction.lower()
    assert "what the error suggests" in instruction.lower()
    assert "what could fix it" in instruction.lower()
    # Still keeps the plain-note case distinct from the error case - a
    # database with nothing relevant isn't asked to be "explained" the
    # same way an actual failure is.
    assert "database noted it had nothing relevant" in instruction


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
    resp = env.client.post('/api/translate', json={'prompt': 'newest prompt', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    contents = harness.generate_calls[0]["contents"]
    # 2 surviving turns (4 entries) + the new prompt appended = 5.
    assert len(contents) == 5
    all_text = "\n".join(c.parts[0].text for c in contents)
    assert "prompt 0" not in all_text
    assert "SELECT 0;" not in all_text
    assert "prompt 1" in all_text
    assert "prompt 2" in all_text


def test_history_sent_to_claude_is_capped_to_history_max_turns(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1", "HISTORY_MAX_TURNS": "2"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 4;"))

    history = []
    for i in range(3):
        history.append({"role": "user", "text": f"prompt {i}"})
        history.append({"role": "model", "text": f"SELECT {i};"})
    resp = env.client.post('/api/translate', json={'prompt': 'newest prompt', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

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


# --- Anthropic (Claude) provider path (llm_provider="anthropic") ---
# translate_query() branches to _call_claude/_classify_claude_error/
# build_claude_history_messages/pick_claude_api_key instead of their Gemini
# counterparts whenever the session's saved llm_provider is "anthropic" -
# see helpers.select_llm_provider() and translate_routes.py's
# ClaudeProvider/_LLM_PROVIDERS (registered under the "anthropic" label -
# the class itself keeps its SDK-derived name). Everything upstream of that branch
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
        self.client_timeouts = []  # timeout each Anthropic(...) was constructed with
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
            def __init__(self, api_key=None, timeout=None):
                self.api_key = api_key
                harness.client_api_keys.append(api_key)
                harness.client_timeouts.append(timeout)
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
    env = app_factory(env={})
    select_llm_provider(env, "anthropic")
    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    assert resp.status_code == 400
    assert "Anthropic API key is not configured." in resp.get_json()['error']


def test_claude_success_strips_markdown_fences_and_returns_token_counts(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
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


def test_claude_client_is_constructed_with_translation_timeout_in_seconds(app_factory, monkeypatch):
    # Unlike GeminiProvider's milliseconds (see that test in the Gemini
    # section above), anthropic.Anthropic takes a plain seconds value - see
    # ClaudeProvider.make_client's comment and TRANSLATION_TIMEOUT_SECONDS's
    # docstring.
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1", "TRANSLATION_TIMEOUT_SECONDS": "45"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    parse_translate_stream(resp)
    assert harness.client_timeouts == [45]


def test_claude_success_records_translation_history(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    # Not select_llm_provider(env, ...) here - that seeds the "global"
    # identity, but this test's request resolves to "alice@example.com" via
    # the cookie below, so the provider choice has to be seeded on THAT
    # identity instead for it to actually take effect.
    env.app_config.state_store.set_session("alice@example.com", llm_provider="anthropic")
    env.client.set_cookie("crbot_user_id", "alice@example.com")
    resp = env.client.post('/api/translate', json={'prompt': 'give me one'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['sql_command'] == "SELECT 1;"


def test_claude_no_temperature_param_is_ever_passed(app_factory, monkeypatch):
    """Regression guard for _call_claude's documented reason for omitting
    temperature: claude-sonnet-5 and later reject sampling params outright.
    FakeMessages.create()'s signature above has no temperature parameter at
    all, so passing one would raise TypeError rather than silently
    accepting it - proving the real call site never does."""
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
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
        "ANTHROPIC_API_KEY": "fake-key-1",
        "DATABASE_PRESETS_FILE": presets_path,
    })
    select_llm_provider(env, "anthropic")
    install_fake_mssql_connect(monkeypatch)
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    env.app_config.state_store.set_session(
        "global", connection_id="mssql+MS", is_custom=False,
    )

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = [
        {"role": "user", "text": "show users"},
        {"role": "model", "text": "SELECT * FROM users;",
         "results": [{"columns": ["id"], "rows": [[1]], "rowCount": 1}]},
    ]
    resp = env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 2;"))

    history = [{"role": "user", "text": "show users"}]
    resp = env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    system = harness.create_calls[0]["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_claude_cache_control_marks_last_history_turn_not_the_new_prompt(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 3;"))

    history = [
        {"role": "user", "text": "show users"},
        {"role": "model", "text": "SELECT * FROM users;"},
        {"role": "user", "text": "now filter to active ones"},
        {"role": "model", "text": "SELECT * FROM users WHERE active;"},
    ]
    resp = env.client.post('/api/translate', json={'prompt': 'now just the count', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    messages = harness.create_calls[0]["messages"]
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in content[1]


def test_claude_build_llm_input_with_no_history_and_no_schema_sends_one_unmarked_block(app_factory):
    """Regression test: connection_router.py's triage_all_mode_question
    always calls build_llm_input(history=[], schema_block="", ...) - there's
    no schema in play at that stage at all (see that module's docstring).
    Before this was special-cased, an empty history fell into the same
    "conversation's very first call" branch
    test_claude_schema_attaches_to_new_prompt_when_there_is_no_history above
    covers - which unconditionally cache_control-marks the schema block,
    even when it's "". Anthropic rejects cache_control on an empty text
    block outright ("cache_control cannot be set for empty text blocks"),
    so every triage call to Claude used to fail with a 400 outright. This
    test pins down the fix directly at the build_llm_input level: an empty
    schema_block with no history produces exactly one content block (the
    new prompt, as a plain string - not a content-block list at all), never
    an empty cache_control-marked block."""
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    provider = env.translate_routes.ClaudeProvider()

    messages = provider.build_llm_input([], "", "some new prompt")

    assert len(messages) == 1
    assert messages[0] == {"role": "user", "content": "some new prompt"}


def test_claude_reports_cache_read_tokens_via_cached_content_tokens(app_factory, monkeypatch):
    """cached_content_tokens in the NDJSON response is how a caller sees
    whether caching is actually paying off - it's fed from Anthropic's
    usage.cache_read_input_tokens (see _call_claude)."""
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;", cache_read_tokens=1234))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['cached_content_tokens'] == 1234


def test_claude_default_model_is_claude_sonnet_5(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "claude-sonnet-5"


def test_claude_model_env_var_overrides_default(app_factory, monkeypatch):
    env = app_factory(env={
        "ANTHROPIC_API_KEY": "fake-key-1",
        "ANTHROPIC_MODELS": "claude-opus-x",
    })
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "claude-opus-x"


def test_claude_model_override_via_request_body(app_factory, monkeypatch):
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi', 'claude_model': 'claude-x-custom'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "claude-x-custom"


def test_claude_429_never_rotates_key_and_retries_with_delay(app_factory, monkeypatch):
    """Claude's key-rotation retry was removed - key-rotation is now a
    Gemini-only hack (this app is only known to configure a POOL of keys
    for Gemini, via GEMINI_PRESET_KEYS). Even with multiple CLAUDE_PRESET_
    KEYS configured, a RateLimitError just retries with the SAME key after
    TRANSLATION_RETRY_DELAY_SECONDS, exactly like a 5xx/connection error -
    see _classify_claude_error's docstring."""
    env = app_factory(env={
        "CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    select_llm_provider(env, "anthropic")
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
        "CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2",
    })
    select_llm_provider(env, "anthropic")
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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
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
        "ANTHROPIC_API_KEY": "fake-key-1",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    select_llm_provider(env, "anthropic")
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
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    select_llm_provider(env, "anthropic")
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
    env = app_factory(env={"CLAUDE_PRESET_KEYS": "fake-key-1,fake-key-2"})
    select_llm_provider(env, "anthropic")
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


# --- OpenAI provider (llm_provider="openai") ---------------------------------
#
# translate_query() dispatches to _call_openai/_classify_openai_error/
# build_openai_history_messages/pick_openai_api_key (via OpenAiProvider - see
# translate_routes.py's module docstring) whenever the session's saved
# llm_provider is "openai" - see helpers.select_llm_provider().
# Everything upstream of provider dispatch (dialect intro selection, schema
# fetch, NDJSON streaming shape, markdown fence stripping) is shared code
# already covered by the Gemini tests above, so the tests below focus on
# what's actually different: this provider is built on the Responses API
# (client.responses.create), not Chat Completions - see _call_openai's
# docstring for why - so its call shape (model/instructions/input, not
# model/system/messages or model/contents/config) and its usage-field names
# (usage.input_tokens_details.cached_tokens, usage.output_tokens_details.
# reasoning_tokens) differ from both other providers'. Its retry-
# classification rules mirror Claude's exactly (no key rotation - see
# _classify_openai_error's docstring) except that OpenAI's SDK already
# scopes RateLimitError/InternalServerError to their own status codes, so
# there's no generic-status-code fake needed the way Claude's
# FakeClaudeStatusError is.


class FakeOpenAiResponse:
    def __init__(self, text, input_tokens=10, output_tokens=5, total_tokens=15,
                 cached_tokens=0, reasoning_tokens=0):
        self.output_text = text
        self.usage = pytypes.SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            input_tokens_details=pytypes.SimpleNamespace(cached_tokens=cached_tokens),
            output_tokens_details=pytypes.SimpleNamespace(reasoning_tokens=reasoning_tokens),
        )


class OpenAiHarness:
    def __init__(self):
        self.queue = []  # list of FakeOpenAiResponse or Exception instances
        self.client_api_keys = []  # api_key each OpenAI(...) was constructed with
        self.client_timeouts = []  # timeout each OpenAI(...) was constructed with
        self.create_calls = []  # kwargs of each responses.create call

    def queue_response(self, resp):
        self.queue.append(resp)

    def queue_error(self, exc):
        self.queue.append(exc)

    def make_client_class(self):
        harness = self

        class FakeResponses:
            def create(self, model, instructions, input):
                harness.create_calls.append(
                    {"model": model, "instructions": instructions, "input": input,
                     "api_key": harness.client_api_keys[-1]}
                )
                if not harness.queue:
                    raise AssertionError("OpenAiHarness queue exhausted - test didn't queue enough responses")
                item = harness.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, api_key=None, timeout=None):
                self.api_key = api_key
                harness.client_api_keys.append(api_key)
                harness.client_timeouts.append(timeout)
                self.responses = FakeResponses()

        return FakeClient


class FakeOpenAiRateLimitError(openai.RateLimitError):
    """A real 429 raises openai.RateLimitError specifically -
    _classify_openai_error checks for this exact type. Unlike Claude's
    FakeClaudeStatusError(429) (Claude's SDK only has one generic
    APIStatusError for every status code), openai.RateLimitError is
    already scoped to 429 by the SDK itself, so no separate status_code
    needs to be set here at all. Bypasses RateLimitError's real __init__
    (which requires a live httpx2 Response) since only the type itself is
    ever checked."""
    def __init__(self):
        Exception.__init__(self, "fake rate limit")


class FakeOpenAiInternalServerError(openai.InternalServerError):
    """Same idea for a 5xx - openai.InternalServerError is already scoped
    to the 5xx range by the SDK, unlike Claude's shared APIStatusError, so
    there's no generic-status-code fake needed here the way
    FakeClaudeStatusError is."""
    def __init__(self):
        Exception.__init__(self, "fake internal server error")


class FakeOpenAiConnectionError(openai.APIConnectionError):
    """Same connection-level case as Claude's FakeClaudeConnectionError -
    openai.APIConnectionError also covers APITimeoutError (a subclass of
    it), so this one fake covers both."""
    def __init__(self):
        Exception.__init__(self, "fake connection error")


class FakeOpenAiBadRequestError(openai.BadRequestError):
    """Non-retryable (a real 400) - _classify_openai_error returns None
    for anything that isn't RateLimitError/InternalServerError/
    APIConnectionError, same policy as Claude's equivalent case."""
    def __init__(self):
        Exception.__init__(self, "fake bad request")


def test_openai_missing_api_key_returns_400(app_factory):
    env = app_factory(env={})
    select_llm_provider(env, "openai")
    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    assert resp.status_code == 400
    assert "OpenAI API key is not configured." in resp.get_json()['error']


def test_openai_success_strips_markdown_fences_and_returns_token_counts(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("```sql\nSELECT * FROM users;\n```", input_tokens=20, output_tokens=8, total_tokens=28))

    resp = env.client.post('/api/translate', json={'prompt': 'Show all users'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is True
    assert data['sql'] == "SELECT * FROM users;"
    assert data['total_tokens'] == 28
    assert data['input_tokens'] == 20
    assert data['output_tokens'] == 8


def test_openai_client_is_constructed_with_translation_timeout_in_seconds(app_factory, monkeypatch):
    # Same plain-seconds kwarg as ClaudeProvider's - see that test in the
    # Anthropic section above.
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1", "TRANSLATION_TIMEOUT_SECONDS": "45"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    parse_translate_stream(resp)
    assert harness.client_timeouts == [45]


def test_openai_success_records_translation_history(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    # Not select_llm_provider(env, ...) here - see the matching comment in
    # test_claude_success_records_translation_history above.
    env.app_config.state_store.set_session("alice@example.com", llm_provider="openai")
    env.client.set_cookie("crbot_user_id", "alice@example.com")
    resp = env.client.post('/api/translate', json={'prompt': 'give me one'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['sql_command'] == "SELECT 1;"


def test_openai_call_uses_responses_api_shape_not_chat_completions(app_factory, monkeypatch):
    """Pins down the one thing genuinely unique to this provider: the call
    is client.responses.create(model=, instructions=, input=) - Responses'
    own shape - not client.chat.completions.create(model=, messages=). The
    system prompt goes through `instructions`, never folded into `input`."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    call = harness.create_calls[0]
    assert "PostgreSQL-compatible RDBMSs" in call["instructions"]
    assert isinstance(call["input"], list)
    assert all("PostgreSQL-compatible RDBMSs" not in (m.get("content") or "") for m in call["input"])


def test_openai_history_uses_assistant_role_and_appends_results(app_factory, monkeypatch):
    """build_openai_history_messages() maps Gemini's "model" role to
    "assistant" (same mapping as Claude's), leaving "user" untouched, and
    appends query-results text exactly like the other two providers'
    history builders do."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 2;"))

    history = [
        {"role": "user", "text": "show users"},
        {"role": "model", "text": "SELECT * FROM users;",
         "results": [{"columns": ["id"], "rows": [[1]], "rowCount": 1}]},
    ]
    resp = env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    messages = harness.create_calls[0]["input"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].endswith("show users")
    assert messages[1]["role"] == "assistant"
    assert "SELECT * FROM users;" in messages[1]["content"]
    assert "[Query Result 1" in messages[1]["content"]
    assert messages[2]["role"] == "user"


def test_openai_schema_precedes_history_and_is_not_glued_to_the_new_prompt(app_factory, monkeypatch):
    """Same ordering regression guard as the Gemini/Claude versions above:
    the schema is prepended to the first historical message, not glued
    onto the ever-changing new prompt."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 2;"))

    history = [{"role": "user", "text": "show users"}]
    resp = env.client.post('/api/translate', json={'prompt': 'now show orders', 'history': history})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    messages = harness.create_calls[0]["input"]
    assert messages[0]["content"] == "Database Schema:\nNo schema description available.\n\nshow users"
    assert "Database Schema:" not in messages[-1]["content"]
    assert "now show orders" in messages[-1]["content"]


def test_openai_schema_attaches_to_new_prompt_when_there_is_no_history(app_factory, monkeypatch):
    """With no prior history, unlike Claude's two-content-block split
    (there's no cache_control marker to place - see _call_openai's
    docstring on why OpenAI's caching is automatic), the schema is simply
    concatenated onto the new prompt in one plain string, same as Gemini's
    no-history case."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show users'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring

    messages = harness.create_calls[0]["input"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Database Schema:\nNo schema description available.\n\nUser Request: show users\n\nSQL Query:"


def test_openai_reports_cached_tokens(app_factory, monkeypatch):
    """cached_content_tokens in the NDJSON response is fed from OpenAI's
    usage.input_tokens_details.cached_tokens (see _call_openai)."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;", cached_tokens=1234))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['cached_content_tokens'] == 1234


def test_openai_reports_reasoning_tokens_as_thinking_tokens(app_factory, monkeypatch):
    """thinking_tokens in the NDJSON response is fed from OpenAI's
    usage.output_tokens_details.reasoning_tokens - no Claude equivalent
    (always 0 there, see _call_claude), and unlike Gemini's
    thoughts_token_count this is a genuinely distinct field name per
    provider, all folded into the same shared usage_dict key."""
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;", reasoning_tokens=42))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['thinking_tokens'] == 42


def test_openai_default_model_is_gpt_5_6_luna(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "gpt-5.6-luna"


def test_openai_model_env_var_overrides_default(app_factory, monkeypatch):
    env = app_factory(env={
        "OPENAI_API_KEY": "fake-key-1",
        "OPENAI_MODELS": "gpt-5.6-terra",
    })
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "gpt-5.6-terra"


def test_openai_model_override_via_request_body(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi', 'openai_model': 'gpt-5.6-custom'})
    parse_translate_stream(resp)  # drains the stream - see this file's module docstring
    assert harness.create_calls[0]["model"] == "gpt-5.6-custom"


def test_openai_rate_limit_never_rotates_key_and_retries_with_delay(app_factory, monkeypatch):
    """Same policy as Claude's 429 case - no key rotation for OpenAI either
    (see _classify_openai_error's docstring), even with multiple
    OPENAI_PRESET_KEYS configured: a RateLimitError just retries with the
    SAME key after TRANSLATION_RETRY_DELAY_SECONDS."""
    env = app_factory(env={
        "OPENAI_PRESET_KEYS": "fake-key-1,fake-key-2",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeOpenAiRateLimitError())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    # Same key reused - no second OpenAI(...) construction at all, even
    # though a second OPENAI_PRESET_KEYS entry is configured and available.
    assert len(harness.client_api_keys) == 1
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert retry_events[0]["delaySeconds"] == 2.5
    assert sleep_calls == [2.5]


def test_openai_internal_server_error_retries_with_same_key(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeOpenAiInternalServerError())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(retry_events) == 1
    assert retry_events[0]["rotatedKey"] is False
    assert len(harness.client_api_keys) == 1
    assert len(harness.create_calls) == 2


def test_openai_connection_error_retries_with_same_key(app_factory, monkeypatch):
    env = app_factory(env={
        "OPENAI_API_KEY": "fake-key-1",
        "TRANSLATION_RETRY_DELAY_SECONDS": "2.5",
    })
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    sleep_calls = []
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda secs: sleep_calls.append(secs))
    harness.queue_error(FakeOpenAiConnectionError())
    harness.queue_response(FakeOpenAiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert retry_events[0]["rotatedKey"] is False
    assert retry_events[0]["delaySeconds"] == 2.5
    assert sleep_calls == [2.5]
    assert len(harness.client_api_keys) == 1


def test_openai_non_retryable_error_fails_immediately(app_factory, monkeypatch):
    env = app_factory(env={"OPENAI_API_KEY": "fake-key-1"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeOpenAiBadRequestError())  # _classify_openai_error returns None

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert retry_events == []
    assert data['success'] is False
    assert len(harness.create_calls) == 1  # no retry attempted


def test_openai_exhausts_all_retry_attempts_and_reports_failure_in_body(app_factory, monkeypatch):
    """Same as Claude's equivalent test: since OpenAI never rotates keys,
    a run of RateLimitErrors exhausts the shared transient-error budget
    (MAX_TRANSLATION_ATTEMPTS), not a key-rotation budget - configuring 2
    OPENAI_PRESET_KEYS here is deliberate: it proves the extra key is
    never touched (only one OpenAI(...) client is ever constructed) even
    though it's available."""
    env = app_factory(env={"OPENAI_PRESET_KEYS": "fake-key-1,fake-key-2"})
    select_llm_provider(env, "openai")
    harness = OpenAiHarness()
    monkeypatch.setattr(env.translate_routes.openai, "OpenAI", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    for _ in range(env.translate_routes.MAX_TRANSLATION_ATTEMPTS):
        harness.queue_error(FakeOpenAiRateLimitError())

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert "error" in data
    assert len(retry_events) == env.translate_routes.MAX_TRANSLATION_ATTEMPTS - 1
    assert len(harness.create_calls) == env.translate_routes.MAX_TRANSLATION_ATTEMPTS
    assert len(harness.client_api_keys) == 1
    assert all(action["rotatedKey"] is False for action in retry_events)


def test_pick_openai_api_key_returns_none_when_no_keys_configured(app_env):
    assert app_env.translate_routes.pick_openai_api_key() is None


def test_pick_openai_api_key_avoids_excluded_when_alternative_exists(app_factory):
    env = app_factory(env={"OPENAI_PRESET_KEYS": "key-a,key-b"})
    picked = env.translate_routes.pick_openai_api_key(exclude={"key-a"})
    assert picked == "key-b"


def test_pick_openai_api_key_falls_back_to_full_pool_when_all_excluded(app_factory):
    env = app_factory(env={"OPENAI_PRESET_KEYS": "key-a"})
    picked = env.translate_routes.pick_openai_api_key(exclude={"key-a"})
    assert picked == "key-a"


def test_get_openai_api_keys_returns_empty_list_when_nothing_configured(app_env):
    assert app_env.translate_routes.get_openai_api_keys() == []


def test_get_openai_api_keys_falls_back_to_openai_api_key_when_no_preset_keys(app_factory):
    env = app_factory(env={"OPENAI_API_KEY": "sk-single"})
    assert env.translate_routes.get_openai_api_keys() == ["sk-single"]


def test_get_openai_api_keys_prefers_preset_keys_over_single_var(app_factory):
    env = app_factory(env={
        "OPENAI_PRESET_KEYS": "key-a,key-b",
        "OPENAI_API_KEY": "sk-single",
    })
    assert env.translate_routes.get_openai_api_keys() == ["key-a", "key-b"]


# --- LlmProvider dispatch (provider-agnostic) --------------------------------
#
# The tests above (across all three providers) already exercise
# get_llm_provider()/LlmProvider indirectly via /api/translate - this small
# section covers the dispatch/registry mechanics directly: an unrecognized
# saved llm_provider value must still behave exactly as it did before this
# app removed the separate LLM_PROVIDER env var (silently default to
# Google, not error) - including a value that was only ever valid under the
# OLD "gemini"/"claude" labels, e.g. a session saved before that rename -
# and each registered name must resolve to the right adapter.

def test_get_llm_provider_falls_back_to_google_for_unknown_name(app_env):
    provider = app_env.translate_routes.get_llm_provider("not-a-real-provider")
    assert provider.name == "google"


def test_get_llm_provider_falls_back_to_google_for_empty_string(app_env):
    provider = app_env.translate_routes.get_llm_provider("")
    assert provider.name == "google"


def test_get_llm_provider_returns_registered_providers_by_name(app_env):
    tr = app_env.translate_routes
    assert isinstance(tr.get_llm_provider("google"), tr.GeminiProvider)
    assert isinstance(tr.get_llm_provider("anthropic"), tr.ClaudeProvider)
    assert isinstance(tr.get_llm_provider("openai"), tr.OpenAiProvider)


def test_unrecognized_persisted_llm_provider_falls_back_to_google_end_to_end(app_factory, monkeypatch):
    """End-to-end version of the unit test above: a session whose saved
    llm_provider is unrecognized - either a plain typo, or a stale value
    from before this app's provider labels were renamed from gemini/claude
    to google/anthropic - still translates via Google, exactly as
    get_llm_provider()'s own fallback guarantees. There's no LLM_PROVIDER
    env var anymore to misconfigure fleet-wide in the first place - this is
    purely a per-session concern now."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    select_llm_provider(env, "gemini")  # the pre-rename label - now unrecognized
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 1


# --- LlmProvider.preset_models / default_model / list_llm_providers_info() ---
#
# Each provider has exactly one *_MODELS env var (GOOGLE_MODELS/
# ANTHROPIC_MODELS/OPENAI_MODELS), comma-separated - its first entry doubles
# as that provider's default_model, the full list is preset_models (the
# model-selection modal's data source). Left unset, a provider falls back
# to its own hardcoded single-model fallback_models.

def test_preset_models_and_default_model_fall_back_when_env_var_unset(app_env):
    tr = app_env.translate_routes
    assert tr.get_llm_provider("google").preset_models == ["gemini-3.6-flash"]
    assert tr.get_llm_provider("google").default_model == "gemini-3.6-flash"
    assert tr.get_llm_provider("anthropic").preset_models == ["claude-sonnet-5"]
    assert tr.get_llm_provider("anthropic").default_model == "claude-sonnet-5"
    assert tr.get_llm_provider("openai").preset_models == ["gpt-5.6-luna"]
    assert tr.get_llm_provider("openai").default_model == "gpt-5.6-luna"


def test_models_env_var_parses_comma_separated_list_first_entry_is_default(app_factory):
    env = app_factory(env={"GOOGLE_MODELS": "gemini-3.6-flash,gemini-2.5-pro"})
    provider = env.translate_routes.get_llm_provider("google")
    assert provider.preset_models == ["gemini-3.6-flash", "gemini-2.5-pro"]
    assert provider.default_model == "gemini-3.6-flash"


def test_models_env_var_first_entry_becomes_the_new_default_when_reordered(app_factory):
    # Nothing hardcodes which entry is "the default" beyond position - a
    # deploy that wants gemini-2.5-pro to be the fleet-wide default just
    # lists it first, no separate GEMINI_MODEL var to keep in sync.
    env = app_factory(env={"ANTHROPIC_MODELS": "claude-opus-5,claude-sonnet-5"})
    provider = env.translate_routes.get_llm_provider("anthropic")
    assert provider.default_model == "claude-opus-5"
    assert provider.preset_models == ["claude-opus-5", "claude-sonnet-5"]


def test_models_env_var_trims_whitespace_and_drops_blank_entries(app_factory):
    env = app_factory(env={"OPENAI_MODELS": " gpt-5.6-sol , , gpt-5.6-terra "})
    provider = env.translate_routes.get_llm_provider("openai")
    assert provider.preset_models == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert provider.default_model == "gpt-5.6-sol"


def test_models_env_var_change_is_reflected_live_not_cached(app_factory):
    # Read fresh on every access (like get_gemini_api_keys() already does
    # for GEMINI_PRESET_KEYS), not memoized at import time - lets
    # fresh_import()-based tests reconfigure it per test case, and would
    # let a future admin settings change take effect immediately.
    env = app_factory(env={"GOOGLE_MODELS": "gemini-2.5-flash"})
    import os as _os
    provider = env.translate_routes.get_llm_provider("google")
    assert "gemini-2.5-pro" not in provider.preset_models
    _os.environ["GOOGLE_MODELS"] = "gemini-2.5-flash,gemini-2.5-pro"
    try:
        assert "gemini-2.5-pro" in provider.preset_models
    finally:
        del _os.environ["GOOGLE_MODELS"]


def test_list_llm_providers_info_returns_all_three_providers_in_order(app_env):
    info = app_env.translate_routes.list_llm_providers_info()
    assert [p["name"] for p in info] == ["google", "anthropic", "openai"]
    gemini_info = info[0]
    assert gemini_info["default_model"] == "gemini-3.6-flash"
    assert gemini_info["preset_models"] == ["gemini-3.6-flash"]


# --- DEFAULT_MODEL env var (overrides which model is "the default") --------
#
# A single, app-wide DEFAULT_MODEL env var picks which model each
# provider's own default_model actually resolves to, instead of that
# provider's own *_MODELS list's first entry always winning purely by being
# first - see LlmProvider.default_model's docstring. It only takes effect
# for whichever provider's own preset_models genuinely contains that exact
# model name; every other provider is unaffected. When a session hasn't
# picked a provider at all yet, it also decides which provider becomes the
# app's ONE fleet-wide default (see get_llm_provider()/
# _default_fleet_provider()) - Google/gemini-3.6-flash remains the final
# fallback when DEFAULT_MODEL is unset, blank, or matches nothing
# configured at all.

def test_default_model_overrides_which_model_this_provider_uses_by_default(app_factory):
    env = app_factory(env={
        "ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5",
        "DEFAULT_MODEL": "claude-opus-5",
    })
    provider = env.translate_routes.get_llm_provider("anthropic")
    # preset_models' own order (and therefore the model-selection modal's
    # own listing) is untouched - only which one counts as "the default".
    assert provider.preset_models == ["claude-sonnet-5", "claude-opus-5"]
    assert provider.default_model == "claude-opus-5"


def test_default_model_is_ignored_for_a_provider_it_does_not_belong_to(app_factory):
    # DEFAULT_MODEL names an Anthropic model - Google's own default_model
    # (a completely different provider's preset_models) must be unaffected
    # and keep falling back to its own first entry.
    env = app_factory(env={
        "GOOGLE_MODELS": "gemini-3.6-flash,gemini-2.5-pro",
        "DEFAULT_MODEL": "claude-opus-5",
    })
    provider = env.translate_routes.get_llm_provider("google")
    assert provider.default_model == "gemini-3.6-flash"


def test_default_model_blank_or_unset_falls_back_to_first_entry(app_factory):
    for value in ("", "   "):
        env = app_factory(env={"ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5", "DEFAULT_MODEL": value})
        assert env.translate_routes.get_llm_provider("anthropic").default_model == "claude-sonnet-5"


def test_default_model_not_matching_any_configured_model_falls_back_to_first_entry(app_factory):
    env = app_factory(env={
        "ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5",
        "DEFAULT_MODEL": "not-a-real-model",
    })
    assert env.translate_routes.get_llm_provider("anthropic").default_model == "claude-sonnet-5"


def test_default_model_picks_the_fleet_wide_default_provider_when_none_saved(app_factory):
    # No session has picked a provider at all (get_llm_provider("") is
    # exactly what a fresh/unset session resolves through) - DEFAULT_MODEL
    # naming an Anthropic model moves the WHOLE fleet's default off Google.
    env = app_factory(env={
        "ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5",
        "DEFAULT_MODEL": "claude-opus-5",
    })
    provider = env.translate_routes.get_llm_provider("")
    assert provider.name == "anthropic"
    assert provider.default_model == "claude-opus-5"


def test_default_model_not_matching_anything_still_falls_back_to_google_fleet_wide(app_factory):
    env = app_factory(env={"DEFAULT_MODEL": "not-a-real-model-anywhere"})
    provider = env.translate_routes.get_llm_provider("")
    assert provider.name == "google"
    assert provider.default_model == "gemini-3.6-flash"


def test_list_llm_providers_info_reflects_default_model_override_for_matching_provider_only(app_factory):
    env = app_factory(env={
        "ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5",
        "DEFAULT_MODEL": "claude-opus-5",
    })
    info = env.translate_routes.list_llm_providers_info()
    by_name = {p["name"]: p for p in info}
    assert by_name["anthropic"]["default_model"] == "claude-opus-5"
    # Google/OpenAI never had a matching model - unaffected.
    assert by_name["google"]["default_model"] == "gemini-3.6-flash"
    assert by_name["openai"]["default_model"] == "gpt-5.6-luna"


def test_translate_uses_default_model_env_var_end_to_end_for_a_fresh_session(app_factory, monkeypatch):
    """A session that never picked a provider or model at all (no saved
    llm_provider/llm_model - the true "fresh install" case) actually
    translates via Claude, on the model DEFAULT_MODEL names - not Google,
    the old hardcoded fallback-of-last-resort."""
    env = app_factory(env={
        "ANTHROPIC_API_KEY": "fake-key-1",
        "ANTHROPIC_MODELS": "claude-sonnet-5,claude-opus-5",
        "DEFAULT_MODEL": "claude-opus-5",
    })
    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.create_calls[0]["model"] == "claude-opus-5"


# --- Session-persisted model selection (model-selection UI) ------------------
#
# translate_query() resolves the effective provider/model from the current
# session's saved llm_provider/llm_model (see state_store.py's get_session/
# set_session and config_routes.py's POST /api/config), falling back to the
# one hardcoded default (Google) and that provider's own default_model
# whenever nothing's been saved - see get_llm_provider()'s docstring.

def test_translate_uses_persisted_session_provider_over_env_default(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1", "ANTHROPIC_API_KEY": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    env.app_config.state_store.set_session(
        "alice@example.com", llm_provider="anthropic", llm_model="claude-sonnet-5",
    )

    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.create_calls[0]["model"] == "claude-sonnet-5"


def test_translate_falls_back_to_env_provider_when_session_never_saved_a_choice(app_factory, monkeypatch):
    # Regression guard: a session that never touched the model-selection UI
    # (session_data["llm_provider"] == "") must behave identically to before
    # persisted selection existed - this is what keeps every other test in
    # this file (none of which touch state_store) passing unchanged.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True


def test_translate_request_body_model_override_still_wins_over_persisted_session_model(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    env.app_config.state_store.set_session(
        "alice@example.com", llm_provider="google", llm_model="gemini-saved-model",
    )

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi', 'model': 'request-override-model'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.generate_calls[0]["model"] == "request-override-model"


def test_translate_falls_back_to_provider_default_model_when_only_provider_persisted(app_factory, monkeypatch):
    # A session that saved a provider but somehow has no model saved (e.g.
    # an old/partial write) falls back to that provider's own default_model,
    # not an empty string.
    env = app_factory(env={"ANTHROPIC_API_KEY": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    env.app_config.state_store.set_session("alice@example.com", llm_provider="anthropic")

    harness = ClaudeHarness()
    monkeypatch.setattr(env.translate_routes.anthropic, "Anthropic", harness.make_client_class())
    harness.queue_response(FakeClaudeResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.create_calls[0]["model"] == "claude-sonnet-5"


# --- User-facing LLM error messages (error_category()/format_llm_error_for_
# user()/LlmCallFailed) -------------------------------------------------------
#
# These are the FINAL-failure classifier/formatter described in the section
# comment above _gemini_error_category() in translate_routes.py - a
# deliberately separate concern from _classify_*_error's retry policy above
# (a call can be retried several times via that policy and only reach
# error_category()/format_llm_error_for_user() once every retry budget is
# exhausted, or immediately for a non-retryable exception). Reuses the same
# Fake*Error test doubles defined above for the retry-policy tests, since the
# real SDK exception shapes are identical either way.

def test_gemini_error_category_prefers_semantic_status_string_over_numeric_code(app_env):
    exc = FakeApiError(500)  # numeric code alone would read as "unavailable"
    exc.status = "RESOURCE_EXHAUSTED"
    assert app_env.translate_routes._gemini_error_category(exc) == "exhausted"


def test_gemini_error_category_unavailable_status_string(app_env):
    exc = FakeApiError(400)  # numeric code alone would read as "other"
    exc.status = "UNAVAILABLE"
    assert app_env.translate_routes._gemini_error_category(exc) == "unavailable"


def test_gemini_error_category_falls_back_to_numeric_code_429(app_env):
    assert app_env.translate_routes._gemini_error_category(FakeApiError(429)) == "exhausted"


def test_gemini_error_category_falls_back_to_numeric_code_503_and_other_5xx(app_env):
    assert app_env.translate_routes._gemini_error_category(FakeApiError(503)) == "unavailable"
    assert app_env.translate_routes._gemini_error_category(FakeApiError(500)) == "unavailable"


def test_gemini_error_category_httpx_timeout_is_unavailable(app_env):
    exc = httpx.TimeoutException("timed out")
    assert app_env.translate_routes._gemini_error_category(exc) == "unavailable"


def test_gemini_error_category_unrecognized_exception_is_other(app_env):
    assert app_env.translate_routes._gemini_error_category(FakeApiError(400)) == "other"
    assert app_env.translate_routes._gemini_error_category(ValueError("boom")) == "other"


def test_claude_error_category_rate_limit_is_always_exhausted(app_env):
    assert app_env.translate_routes._claude_error_category(FakeClaudeRateLimitError()) == "exhausted"


def test_claude_error_category_overloaded_and_other_5xx_are_unavailable(app_env):
    assert app_env.translate_routes._claude_error_category(FakeClaudeStatusError(529)) == "unavailable"
    assert app_env.translate_routes._claude_error_category(FakeClaudeStatusError(500)) == "unavailable"


def test_claude_error_category_connection_error_is_unavailable(app_env):
    assert app_env.translate_routes._claude_error_category(FakeClaudeConnectionError()) == "unavailable"


def test_claude_error_category_non_retryable_status_is_other(app_env):
    assert app_env.translate_routes._claude_error_category(FakeClaudeStatusError(400)) == "other"


def test_openai_error_category_insufficient_quota_is_exhausted(app_env):
    exc = FakeOpenAiRateLimitError()
    exc.code = "insufficient_quota"
    assert app_env.translate_routes._openai_error_category(exc) == "exhausted"


def test_openai_error_category_ordinary_rate_limit_is_unavailable(app_env):
    # No .code (or a different one) set - the ordinary "rate_limit_exceeded"
    # kind, not a hard quota ceiling.
    assert app_env.translate_routes._openai_error_category(FakeOpenAiRateLimitError()) == "unavailable"


def test_openai_error_category_internal_server_and_connection_errors_are_unavailable(app_env):
    assert app_env.translate_routes._openai_error_category(FakeOpenAiInternalServerError()) == "unavailable"
    assert app_env.translate_routes._openai_error_category(FakeOpenAiConnectionError()) == "unavailable"


def test_openai_error_category_bad_request_is_other(app_env):
    assert app_env.translate_routes._openai_error_category(FakeOpenAiBadRequestError()) == "other"


def test_format_llm_error_for_user_unavailable_template_includes_model_and_raw_error(app_env):
    provider = app_env.translate_routes.get_llm_provider("google")
    exc = FakeApiError(503)
    message = app_env.translate_routes.format_llm_error_for_user(provider, "gemini-3.6-flash", exc)
    assert message.startswith(
        "The selected model (gemini-3.6-flash) is currently unavailable or too busy. "
        "Please retry later or select a different model.\n\n"
        "Actual error message received:\n"
    )
    assert message.endswith("fake API error 503")


def test_format_llm_error_for_user_exhausted_template(app_env):
    provider = app_env.translate_routes.get_llm_provider("anthropic")
    exc = FakeClaudeRateLimitError()
    message = app_env.translate_routes.format_llm_error_for_user(provider, "claude-sonnet-5", exc)
    assert message.startswith(
        "Datalect's reserved capacity for this model (claude-sonnet-5) has been exhausted. "
        "Please select a different model.\n\n"
        "Actual error message received:\n"
    )
    assert message.endswith("fake rate limit")


def test_format_llm_error_for_user_other_template(app_env):
    provider = app_env.translate_routes.get_llm_provider("openai")
    exc = FakeOpenAiBadRequestError()
    message = app_env.translate_routes.format_llm_error_for_user(provider, "gpt-5", exc)
    assert message.startswith(
        "The selected model (gpt-5) ran into an error. Please select a different model.\n\n"
        "Actual error message received:\n"
    )
    assert message.endswith("fake bad request")


def test_format_llm_error_for_user_falls_back_to_exception_type_name_when_str_is_empty(app_env):
    # An exception with no message at all (str(exc) == "") still produces a
    # non-empty "actual error" line rather than silently trailing off.
    provider = app_env.translate_routes.get_llm_provider("google")
    message = app_env.translate_routes.format_llm_error_for_user(provider, "gemini-3.6-flash", ValueError())
    assert message.endswith("ValueError occurred.")


def test_llm_call_failed_str_is_the_formatted_message_verbatim(app_env):
    formatted = "The selected model (x) ran into an error. Please select a different model.\n\nActual error message received:\nboom"
    wrapped = app_env.translate_routes.LlmCallFailed(formatted)
    assert str(wrapped) == formatted


def test_single_connection_translate_final_failure_shows_categorized_message(app_factory, monkeypatch):
    """End-to-end check that LlmCallFailed's wrapper mechanism (raised at
    stream_translation()'s inline single-connection retry loop) reaches the
    client's data['error'] with the categorized message intact, with zero
    special-casing needed at that loop's outer catch-all (see
    format_llm_error_for_user()'s and LlmCallFailed's docstrings)."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    # A 503 on every attempt (MAX_TRANSLATION_ATTEMPTS) - not a 429, so no
    # key rotation kicks in first; this exhausts the same-key retry budget
    # and reaches the final "give up" raise.
    for _ in range(10):
        harness.queue_error(FakeApiError(503))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert data['error'].startswith(
        "The selected model (gemini-3.6-flash) is currently unavailable or too busy. "
        "Please retry later or select a different model.\n\n"
        "Actual error message received:\n"
    )
    assert data['error'].endswith("fake API error 503")


def test_single_connection_translate_final_failure_logs_a_translation_row(app_factory, monkeypatch):
    """A total single-connection LLM-call failure - every attempt in the
    retry loop exhausted, LlmCallFailed raised - now logs a real
    translations-table row (against the actual connection, same as a
    success does) instead of vanishing silently: 0 for every token count
    (no response was ever successfully returned to have real usage numbers
    from - see the retry loop's usage_info, only ever assigned on a
    successful provider.call()), a non-negative duration measured across
    every attempt and inter-attempt wait, and a TRANSLATION_ERROR(...)
    sentinel in sql_command in place of real SQL, following the same
    overloaded-column convention "*** NO SQL ***" already uses for non-SQL
    text in that column."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    for _ in range(10):
        harness.queue_error(FakeApiError(503))

    env.client.set_cookie("crbot_user_id", "alice@example.com")
    resp = env.client.post('/api/translate', json={'prompt': 'give me one'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False

    rows = _translation_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row['nl_prompt'] == 'give me one'
    assert row['sql_command'] == f"TRANSLATION_ERROR ({data['error']})"
    assert row['input_tokens'] == 0
    assert row['output_tokens'] == 0
    assert row['total_tokens'] == 0
    assert row['thinking_tokens'] == 0
    assert row['cached_content_tokens'] == 0
    assert row['duration'] >= 0


def test_single_connection_translate_non_retryable_failure_also_logs_a_translation_row(app_factory, monkeypatch):
    """Same as above, but for the immediate (no-retry) non-retryable
    failure path - LlmCallFailed is raised on the very first attempt here,
    so this is also a regression guard that the new logging doesn't
    accidentally depend on having gone through at least one retry."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    monkeypatch.setattr(env.translate_routes.time, "sleep", lambda *a, **k: None)
    harness.queue_error(FakeApiError(400))  # bad request - _classify_gemini_error returns None

    env.client.set_cookie("crbot_user_id", "alice@example.com")
    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['sql_command'] == f"TRANSLATION_ERROR ({data['error']})"
    assert rows[0]['total_tokens'] == 0


# --- "invalid_key" category (a rejected/invalid API key) -----------------
#
# See the section comment above _gemini_error_category() in
# translate_routes.py: a 401/403 (or Gemini's own PERMISSION_DENIED/
# UNAUTHENTICATED status strings, or its documented 400 "API key not
# valid" shape) or the SDKs' typed AuthenticationError/PermissionDeniedError
# is classified "invalid_key" - distinct from "other" specifically so
# format_llm_error_for_user() can word it around WHOSE key failed (see the
# "Bring Your Own Key" feature) rather than a generic "ran into an error."

def test_gemini_error_category_permission_denied_and_unauthenticated_status_are_invalid_key(app_env):
    exc = FakeApiError(403)
    exc.status = "PERMISSION_DENIED"
    assert app_env.translate_routes._gemini_error_category(exc) == "invalid_key"

    exc = FakeApiError(401)
    exc.status = "UNAUTHENTICATED"
    assert app_env.translate_routes._gemini_error_category(exc) == "invalid_key"


def test_gemini_error_category_401_and_403_numeric_codes_are_invalid_key(app_env):
    assert app_env.translate_routes._gemini_error_category(FakeApiError(401)) == "invalid_key"
    assert app_env.translate_routes._gemini_error_category(FakeApiError(403)) == "invalid_key"


def test_gemini_error_category_400_with_api_key_not_valid_message_is_invalid_key(app_env):
    exc = FakeApiError(400)
    exc.message = "API key not valid. Please pass a valid API key."
    assert app_env.translate_routes._gemini_error_category(exc) == "invalid_key"

    exc = FakeApiError(400)
    exc.message = "API_KEY_INVALID: bad key"
    assert app_env.translate_routes._gemini_error_category(exc) == "invalid_key"


def test_gemini_error_category_bare_400_without_key_related_message_is_other(app_env):
    # Regression guard for the deliberately conservative design described
    # in _gemini_error_category's own docstring: Gemini overloads a plain
    # 400 for a hundred unrelated bad-request reasons, so the message text
    # must actually say the key was rejected - a 400 alone is NOT enough.
    exc = FakeApiError(400)
    exc.message = "Request contains an invalid argument."
    assert app_env.translate_routes._gemini_error_category(exc) == "other"
    assert app_env.translate_routes._gemini_error_category(FakeApiError(400)) == "other"


def test_claude_error_category_authentication_and_permission_denied_are_invalid_key(app_env):
    class FakeClaudeAuthenticationError(anthropic.AuthenticationError):
        def __init__(self):
            Exception.__init__(self, "fake auth error")
            self.status_code = 401

    class FakeClaudePermissionDeniedError(anthropic.PermissionDeniedError):
        def __init__(self):
            Exception.__init__(self, "fake permission denied")
            self.status_code = 403

    assert app_env.translate_routes._claude_error_category(FakeClaudeAuthenticationError()) == "invalid_key"
    assert app_env.translate_routes._claude_error_category(FakeClaudePermissionDeniedError()) == "invalid_key"


def test_openai_error_category_authentication_and_permission_denied_are_invalid_key(app_env):
    class FakeOpenAiAuthenticationError(openai.AuthenticationError):
        def __init__(self):
            Exception.__init__(self, "fake auth error")

    class FakeOpenAiPermissionDeniedError(openai.PermissionDeniedError):
        def __init__(self):
            Exception.__init__(self, "fake permission denied")

    assert app_env.translate_routes._openai_error_category(FakeOpenAiAuthenticationError()) == "invalid_key"
    assert app_env.translate_routes._openai_error_category(FakeOpenAiPermissionDeniedError()) == "invalid_key"


# --- format_llm_error_for_user's using_byok wording (invalid_key only) ----

def test_format_llm_error_for_user_invalid_key_env_template_tells_admin_not_user(app_env):
    # using_byok omitted (defaults to False) - this app's OWN env-
    # configured key was rejected, so the message points at the app's
    # administrator, not at the user's Preferences dialog.
    provider = app_env.translate_routes.get_llm_provider("google")
    exc = FakeApiError(401)
    message = app_env.translate_routes.format_llm_error_for_user(provider, "gemini-3.6-flash", exc)
    assert message.startswith(
        "The API key configured for this model (gemini-3.6-flash) was rejected. This is a problem "
        "with the app's own configuration, not something selecting a different model fixes on its "
        "own - please let the app's administrator know, or try a different model in the meantime.\n\n"
        "Actual error message received:\n"
    )
    assert "Preferences" not in message
    assert message.endswith("fake API error 401")


def test_format_llm_error_for_user_invalid_key_byok_template_tells_user_to_fix_preferences(app_env):
    provider = app_env.translate_routes.get_llm_provider("google")
    exc = FakeApiError(401)
    message = app_env.translate_routes.format_llm_error_for_user(
        provider, "gemini-3.6-flash", exc, using_byok=True,
    )
    assert message.startswith(
        "Your custom API key for this model (gemini-3.6-flash) was rejected. Please correct or "
        "remove it in Preferences (Bring Your Own Key) - until then, this model will keep "
        "failing.\n\nActual error message received:\n"
    )
    assert "administrator" not in message
    assert message.endswith("fake API error 401")


def test_format_llm_error_for_user_using_byok_only_changes_invalid_key_wording(app_env):
    # Regression guard: using_byok=True must NOT change any of the other
    # three categories' wording - it's specific to "invalid_key" (see
    # format_llm_error_for_user's docstring).
    provider = app_env.translate_routes.get_llm_provider("google")
    exc = FakeApiError(503)
    message = app_env.translate_routes.format_llm_error_for_user(
        provider, "gemini-3.6-flash", exc, using_byok=True,
    )
    assert message.startswith(
        "The selected model (gemini-3.6-flash) is currently unavailable or too busy. "
        "Please retry later or select a different model.\n\n"
        "Actual error message received:\n"
    )


# --- "Bring Your Own Key" actually used instead of the app's env key -----
#
# See state_store.py's get_llm_byok_key/set_session docstrings and
# translate_query()'s own comment on where `byok_key` is resolved. These
# are end-to-end checks (through /api/translate) that a saved BYOK value
# is what actually gets used, and that a rejected BYOK key never silently
# falls back to rotating through the app's own configured pool - see
# generate_sql_for_connection's using_byok docstring for why that
# fallback would be actively wrong (a billing/security surprise).

def test_translate_uses_byok_key_instead_of_env_configured_key(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "env-key-1"})
    login_as(env.client, "alice@example.com")
    set_llm_byok_key(env, "google", "alices-own-key", user_identity="alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.client_api_keys == ["alices-own-key"]


def test_translate_falls_back_to_env_key_when_no_byok_key_is_saved(app_factory, monkeypatch):
    # Regression guard the other way: a session with nothing saved for
    # BYOK must behave exactly as before this feature existed - the app's
    # own env-configured key is used, unchanged.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "env-key-1"})
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.client_api_keys == ["env-key-1"]


def test_translate_byok_key_failure_never_rotates_to_env_configured_keys_and_shows_byok_worded_message(
    app_factory, monkeypatch,
):
    # Two env-configured keys are available - if the retry loop's
    # key-rotation budget weren't forced down to 1 for a BYOK call (see
    # generate_sql_for_connection's/stream_translation's using_byok
    # handling), a 401 here would incorrectly rotate onto one of THOSE,
    # silently abandoning the user's own key mid-request.
    env = app_factory(env={"GEMINI_PRESET_KEYS": "env-key-1,env-key-2"})
    login_as(env.client, "alice@example.com")
    set_llm_byok_key(env, "google", "alices-bad-key", user_identity="alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_error(FakeApiError(401))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    assert resp.status_code == 200
    _, data = parse_translate_stream(resp)
    assert data['success'] is False
    # Exactly one call, with the user's own key - no rotation attempt onto
    # either of the two env-configured keys.
    assert harness.client_api_keys == ["alices-bad-key"]
    assert data['error'].startswith(
        "Your custom API key for this model (gemini-3.6-flash) was rejected. Please correct or "
        "remove it in Preferences (Bring Your Own Key)"
    )


def test_translate_byok_key_removed_falls_back_to_env_key_again(app_factory, monkeypatch):
    # The "x" clear button in Preferences (client.js's byokProvidersMarkedFor
    # Clear) saves an explicit empty string - state_store.py's set_session
    # docstring on llm_byok_keys - which must behave exactly like never
    # having saved one at all, not like an empty/blank key being "used".
    env = app_factory(env={"GEMINI_PRESET_KEYS": "env-key-1"})
    login_as(env.client, "alice@example.com")
    set_llm_byok_key(env, "google", "alices-own-key", user_identity="alice@example.com")
    set_llm_byok_key(env, "google", "", user_identity="alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'hi'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert harness.client_api_keys == ["env-key-1"]


# --- Single-connection mode's own post-execution results summarization --
#
# The single-connection equivalent of "all databases" mode's Phase C (see
# test_connection_router.py's own "Phase C" section for that one) - mirrors
# its test conventions closely, adjusted for: a real single connection/
# schema (Phase C now resolves a real schema per in-scope database too -
# see _build_all_mode_schema_block/Gap 4) and an explicit SQL statement
# rather than one per database. Both this call and Phase C's now share the
# same SUMMARY_RESULTS_MAX_ROWS cap (a dedicated, generous abuse/cost-
# protection cap, distinct from HISTORY_RESULT_MAX_ROWS's past-turn-replay
# purpose - see SUMMARY_RESULTS_MAX_ROWS's own definition comment - and
# distinct from Gap 5 of "Turn History Handling in Datalect", which is
# what made Phase C stop using HISTORY_RESULT_MAX_ROWS here in the first
# place - see test_build_summary_prompt_does_not_cap_rows_the_same_way_
# past_turn_history_does in test_connection_router.py, this test's Phase C
# equivalent).


def test_build_single_summary_prompt_includes_the_question_sql_and_results(app_env):
    results = [{"columns": ["n"], "rows": [{"n": 42}], "rowCount": 1}]
    prompt_text = app_env.translate_routes._build_single_summary_prompt(
        "how many users signed up", "SELECT COUNT(*) AS n FROM users;", results,
        app_env.translate_routes._pick_chartable_result(results),
    )
    assert "Original question: how many users signed up" in prompt_text
    assert "SELECT COUNT(*) AS n FROM users;" in prompt_text
    assert "Query Result 1 - 1 row(s):" in prompt_text
    assert "Columns: n" in prompt_text
    assert "{'n': 42}" in prompt_text


def test_build_single_summary_prompt_does_not_cap_rows_the_same_way_past_turn_history_does(app_env):
    # A row count that exceeds HISTORY_RESULT_MAX_ROWS but stays under
    # SUMMARY_RESULTS_MAX_ROWS should still come through in full,
    # unaffected by that unrelated, much stingier history-replay cap.
    # Phase C's own equivalent test
    # (test_build_summary_prompt_does_not_cap_rows_the_same_way_past_turn_
    # history_does in test_connection_router.py) asserts the same thing.
    assert app_env.translate_routes.HISTORY_RESULT_MAX_ROWS < app_env.translate_routes.SUMMARY_RESULTS_MAX_ROWS
    row_count = app_env.translate_routes.HISTORY_RESULT_MAX_ROWS + 25
    many_rows = [{"n": i} for i in range(row_count)]
    results = [{"columns": ["n"], "rows": many_rows, "rowCount": row_count}]
    prompt_text = app_env.translate_routes._build_single_summary_prompt(
        "q", "SELECT n FROM t;", results, app_env.translate_routes._pick_chartable_result(results),
    )
    assert f"Query Result 1 - {row_count} row(s):" in prompt_text
    assert f"Total Rows: {row_count}" in prompt_text
    # Every single row serialized, not just HISTORY_RESULT_MAX_ROWS of them.
    assert prompt_text.count("{'n':") == row_count


def test_build_single_summary_prompt_caps_rows_at_summary_results_max_rows(app_factory):
    # New abuse/cost-protection guard: an adversarial (or just very wide)
    # result set must still be capped somewhere, now at the dedicated
    # SUMMARY_RESULTS_MAX_ROWS constant rather than being sent to the LLM
    # in full no matter how large.
    env = app_factory(env={"SUMMARY_RESULTS_MAX_ROWS": "3"})
    row_count = 10
    many_rows = [{"n": i} for i in range(row_count)]
    results = [{"columns": ["n"], "rows": many_rows, "rowCount": row_count}]
    prompt_text = env.translate_routes._build_single_summary_prompt(
        "q", "SELECT n FROM t;", results, env.translate_routes._pick_chartable_result(results),
    )
    # The real total row count is still reported honestly...
    assert f"Query Result 1 - {row_count} row(s) total, showing the first 3:" in prompt_text
    assert f"Total Rows: {row_count}" in prompt_text
    # ...but only the capped number of rows is actually serialized.
    assert prompt_text.count("{'n':") == 3


def test_build_single_summary_prompt_formats_notes_and_errors_too(app_env):
    results = [{"columns": [], "rows": [], "rowCount": 0}, {"error": "syntax error near SELECT"}]
    prompt_text = app_env.translate_routes._build_single_summary_prompt(
        "q", "SELECT 1; SELECT 2;", results, app_env.translate_routes._pick_chartable_result(results),
    )
    assert "Query Result 2: query failed - syntax error near SELECT" in prompt_text


# --- Single-connection summary prompt: explain an error, don't just report it ---
# Regression coverage for the same product request as the all-databases
# equivalent above (test_summary_prompt_instructs_explaining_an_error_not_
# just_acknowledging_it): this instruction used to describe its input as
# only ever "the actual result rows", with no mention that a statement
# result might instead be a note or an error at all - even though _build_
# single_summary_prompt() has always been able to send exactly that shape
# (see test_build_single_summary_prompt_formats_notes_and_errors_too just
# above). The model was never actually told what to do when it saw one.
def test_single_summary_prompt_instructs_explaining_an_error_not_just_reporting_it(app_env):
    instruction = app_env.translate_routes._SINGLE_SUMMARY_SYSTEM_INSTRUCTION
    assert "an error explaining that it failed to execute" in instruction.lower()
    assert "briefly explain" in instruction.lower()
    assert "what the error suggests" in instruction.lower()
    assert "what could fix it" in instruction.lower()
    # A multi-statement script where some succeeded and others failed must
    # get BOTH covered, not just whichever the model happens to notice
    # first.
    assert "address both" in instruction.lower()


def test_summarize_single_connection_results_returns_stripped_text_and_usage_on_success(app_env):
    from test_connection_router import _FakeProvider

    provider = _FakeProvider([json.dumps({
        "summary": "Results Summary\n\nSignups are up 20% this week - worth a closer look at channel X.",
        "visualization": None,
    })])
    # summarize_single_connection_results is now a generator (yields live
    # 'retrying' progress lines - see its docstring); drain it to get the
    # final (parsed, usage, error) result, same idiom used for
    # generate_sql_for_connection elsewhere. `parsed` is now the
    # {"summary", "visualization"} dict _clean_single_summary_response
    # produces, not a bare string - see that function's own docstring.
    parsed, usage, error = app_env.translate_routes._drain_generation(
        app_env.translate_routes.summarize_single_connection_results(
            "how many signups this week", "Sales Schema", "SELECT COUNT(*) FROM signups;",
            [{"columns": ["n"], "rows": [{"n": 42}], "rowCount": 1}],
            provider, client=None, model="m",
        )
    )
    assert parsed["summary"] == "Results Summary\n\nSignups are up 20% this week - worth a closer look at channel X."
    # Only 1 row - below _CHART_MIN_ROWS - so charting was never even
    # offered to the model regardless of what it says (see
    # _pick_chartable_result); "visualization" is forced None either way.
    assert parsed["visualization"] is None
    assert usage == {}
    assert error is None
    assert len(provider.calls) == 1


def test_summarize_single_connection_results_gives_up_immediately_for_a_non_retryable_exception(app_env):
    from test_connection_router import _FakeProvider

    provider = _FakeProvider([RuntimeError("boom")])
    parsed, usage, error = app_env.translate_routes._drain_generation(
        app_env.translate_routes.summarize_single_connection_results(
            "q", "schema", "SELECT 1;", [{"columns": [], "rows": []}], provider, client=None, model="m",
        )
    )
    assert (parsed, usage) == (None, None)
    assert isinstance(error, RuntimeError)
    assert len(provider.calls) == 1


def test_summarize_result_endpoint_returns_no_sql_prefixed_summary_and_logs_a_real_connection_row(
    app_factory, monkeypatch,
):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse(json.dumps({
        "summary": "Signups are up 20% this week - worth digging into channel X.",
        "visualization": None,
    })))

    resp = env.client.post('/api/summarize-result', json={
        'prompt': 'how many signups this week',
        'sql': 'SELECT COUNT(*) AS n FROM signups;',
        'results': [{"columns": ["n"], "rows": [{"n": 42}], "rowCount": 1}],
    })
    assert resp.status_code == 200
    # /api/summarize-result now streams NDJSON (a live 'retrying' line per
    # retry, then one terminal line) - resp.get_json() no longer applies
    # here even in this no-retry case, since the mimetype is no longer
    # application/json. See parse_translate_stream's own docstring.
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['summary'] == '*** NO SQL *** Signups are up 20% this week - worth digging into channel X.'
    # Only 1 row in the result - below _CHART_MIN_ROWS - so charting was
    # never on offer this turn regardless of what the model said (see
    # _pick_chartable_result); the route must still report a validated
    # None, not whatever the (here, honest) model wrote.
    assert data['visualization'] is None

    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['nl_prompt'] == 'how many signups this week'
    assert rows[0]['sql_command'] == data['summary']


def test_summarize_result_endpoint_streams_a_retrying_line_before_the_terminal_line(
    app_factory, monkeypatch,
):
    """End-to-end regression guard for single-connection mode's own
    summarization call - the client-visible half of the gap this feature
    closes: a transient/capacity error mid-summarization used to be
    entirely invisible over the wire, since /api/summarize-result
    returned one plain JSON body only once the whole retry loop had
    already finished. Mirrors test_429_rotates_key_and_retries_
    immediately_with_no_delay's identical structure for /api/translate,
    and test_summarize_results_endpoint_streams_a_retrying_line_before_
    the_terminal_line (test_connection_router.py) for Phase C."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2"})
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_error(FakeApiError(429))
    harness.queue_response(FakeGenaiResponse(json.dumps({
        "summary": "Signups are up 20% this week.", "visualization": None,
    })))

    resp = env.client.post('/api/summarize-result', json={
        'prompt': 'how many signups this week',
        'sql': 'SELECT COUNT(*) AS n FROM signups;',
        'results': [{"columns": ["n"], "rows": [{"n": 42}], "rowCount": 1}],
    })
    assert resp.status_code == 200
    retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['summary'] == '*** NO SQL *** Signups are up 20% this week.'
    assert len(harness.client_api_keys) == 2
    assert harness.client_api_keys[0] != harness.client_api_keys[1]

    assert len(retry_events) == 1
    assert retry_events[0]["attempt"] == 2
    assert retry_events[0]["maxAttempts"] == 2
    assert retry_events[0]["rotatedKey"] is True
    assert retry_events[0]["delaySeconds"] == 0


def test_summarize_result_endpoint_uses_byok_key_instead_of_env_configured_key(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    set_llm_byok_key(env, "google", "alices-own-key", user_identity="alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse(json.dumps({"summary": "All good here.", "visualization": None})))

    resp = env.client.post('/api/summarize-result', json={
        'prompt': 'q', 'sql': 'SELECT 1;', 'results': [{"columns": [], "rows": [], "rowCount": 0}],
    })
    assert resp.status_code == 200
    assert parse_translate_stream(resp)[1]['success'] is True
    assert harness.client_api_keys == ["alices-own-key"]


def test_summarize_result_endpoint_requires_prompt_sql_and_results(app_env):
    resp = app_env.client.post('/api/summarize-result', json={'prompt': '', 'sql': '', 'results': []})
    assert resp.status_code == 400

    resp = app_env.client.post('/api/summarize-result', json={'prompt': 'q', 'sql': 'SELECT 1;'})
    assert resp.status_code == 400

    resp = app_env.client.post('/api/summarize-result', json={'prompt': 'q', 'results': [{"columns": [], "rows": []}]})
    assert resp.status_code == 400


def test_summarize_result_endpoint_returns_success_false_when_the_llm_call_fails(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_error(FakeApiError(401))

    resp = env.client.post('/api/summarize-result', json={
        'prompt': 'q', 'sql': 'SELECT 1;', 'results': [{"columns": [], "rows": [], "rowCount": 0}],
    })
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False

    # A total LLM-call failure IS now logged, against the real connection
    # this was run for (unlike Phase C's "All Databases" attribution), with
    # a TRANSLATION_ERROR(...) sentinel standing in for the summary text
    # and 0 for every token count (no response was ever successfully
    # returned to have real usage numbers from).
    rows = _translation_rows(env)
    assert len(rows) == 1
    assert rows[0]['nl_prompt'] == 'q'
    assert rows[0]['sql_command'].startswith('TRANSLATION_ERROR (')
    assert data['error'] in rows[0]['sql_command']
    assert rows[0]['input_tokens'] == 0
    assert rows[0]['output_tokens'] == 0
    assert rows[0]['total_tokens'] == 0


# --- Charting feature: chartability gating, numeric detection, and
# visualization validation ---
#
# The single-connection results summarizer now also decides, in the same
# LLM call, whether the result set should be charted instead of tabled -
# see the module comment above _SINGLE_SUMMARY_SYSTEM_INSTRUCTION for the
# full design rationale. These tests cover the pieces that make that
# decision trustworthy even though the model's own say-so is never taken
# on faith: _pick_chartable_result decides server-side, independent of the
# model, whether charting is even possible for this turn; _column_looks_
# numeric backs that decision and _clean_visualization's own column
# choices; and _clean_visualization/_clean_single_summary_response
# re-validate whatever the model claims against the real result set,
# silently falling back to a null/no-chart decision on any mismatch
# rather than erroring or rendering garbage.

def test_pick_chartable_result_is_none_when_more_than_one_real_tabular_result(app_env):
    # Two real (non-note, non-error) results in the same turn is treated
    # as ambiguous - there's no single result set to chart against - so
    # charting is never offered regardless of shape.
    results = [
        {"columns": ["n"], "rows": [{"n": 1}, {"n": 2}], "rowCount": 2},
        {"columns": ["m"], "rows": [{"m": 1}, {"m": 2}], "rowCount": 2},
    ]
    assert app_env.translate_routes._pick_chartable_result(results) is None


def test_pick_chartable_result_is_none_below_the_minimum_row_count(app_env):
    assert app_env.translate_routes._CHART_MIN_ROWS == 2
    results = [{"columns": ["n"], "rows": [{"n": 1}], "rowCount": 1}]
    assert app_env.translate_routes._pick_chartable_result(results) is None


def test_pick_chartable_result_is_none_with_no_numeric_column(app_env):
    results = [{"columns": ["name"], "rows": [{"name": "a"}, {"name": "b"}], "rowCount": 2}]
    assert app_env.translate_routes._pick_chartable_result(results) is None


def test_pick_chartable_result_is_none_for_a_note_or_error_only_result(app_env):
    # An empty statement result (no columns at all - e.g. an INSERT/UPDATE
    # note) and a pure error result both fail the "has columns" test in
    # _pick_chartable_result, same as today's note/error prompt formatting
    # already distinguishes them from real tabular results.
    results = [{"columns": [], "rows": [], "rowCount": 0}]
    assert app_env.translate_routes._pick_chartable_result(results) is None

    results = [{"error": "syntax error"}]
    assert app_env.translate_routes._pick_chartable_result(results) is None


def test_pick_chartable_result_returns_the_entry_when_genuinely_chartable(app_env):
    entry = {"columns": ["day", "n"], "rows": [{"day": "Mon", "n": 1}, {"day": "Tue", "n": 2}], "rowCount": 2}
    assert app_env.translate_routes._pick_chartable_result([entry]) is entry


def test_column_looks_numeric_requires_ninety_percent_of_non_null_values(app_env):
    looks_numeric = app_env.translate_routes._column_looks_numeric
    # 9 of 10 non-null values numeric - right at the 90% threshold - counts.
    rows = [{"n": i} for i in range(9)] + [{"n": "not a number"}]
    assert looks_numeric(rows, "n") is True

    # 8 of 10 - below threshold - does not count.
    rows = [{"n": i} for i in range(8)] + [{"n": "a"}, {"n": "b"}]
    assert looks_numeric(rows, "n") is False

    # Nulls are excluded from the denominator entirely, so a column that's
    # all-numeric among its non-null values still counts even with lots of
    # nulls mixed in.
    rows = [{"n": None}] * 20 + [{"n": 1}, {"n": 2}]
    assert looks_numeric(rows, "n") is True

    # No non-null values seen at all - can't be numeric.
    assert looks_numeric([{"n": None}], "n") is False

    # Booleans are technically an int subclass in Python but are
    # categorical in meaning, not numeric - must not count.
    rows = [{"n": True}, {"n": False}, {"n": True}]
    assert looks_numeric(rows, "n") is False


def test_clean_visualization_rejects_an_invalid_chart_type(app_env):
    chartable = {"columns": ["day", "n"], "rows": [{"day": "Mon", "n": 1}, {"day": "Tue", "n": 2}]}
    raw = {"chart_type": "pie", "x_column": "day", "y_columns": ["n"], "series_column": None}
    assert app_env.translate_routes._clean_visualization(raw, chartable) is None


def test_clean_visualization_rejects_a_hallucinated_column_name(app_env):
    chartable = {"columns": ["day", "n"], "rows": [{"day": "Mon", "n": 1}, {"day": "Tue", "n": 2}]}
    # x_column not among the real result set's columns at all.
    raw = {"chart_type": "bar", "x_column": "made_up_column", "y_columns": ["n"], "series_column": None}
    assert app_env.translate_routes._clean_visualization(raw, chartable) is None

    # A hallucinated y_column is simply dropped from the list rather than
    # invalidating the whole decision - but if that leaves y_columns empty,
    # the whole visualization is rejected (nothing left to actually chart).
    raw = {"chart_type": "bar", "x_column": "day", "y_columns": ["made_up_column"], "series_column": None}
    assert app_env.translate_routes._clean_visualization(raw, chartable) is None


def test_clean_visualization_rejects_a_non_numeric_y_column(app_env):
    chartable = {"columns": ["day", "label"], "rows": [{"day": "Mon", "label": "a"}, {"day": "Tue", "label": "b"}]}
    raw = {"chart_type": "line", "x_column": "day", "y_columns": ["label"], "series_column": None}
    assert app_env.translate_routes._clean_visualization(raw, chartable) is None


def test_clean_visualization_drops_an_invalid_series_column_but_keeps_the_rest(app_env):
    chartable = {"columns": ["day", "n"], "rows": [{"day": "Mon", "n": 1}, {"day": "Tue", "n": 2}]}
    raw = {"chart_type": "bar", "x_column": "day", "y_columns": ["n"], "series_column": "not_a_real_column"}
    cleaned = app_env.translate_routes._clean_visualization(raw, chartable)
    assert cleaned == {"chart_type": "bar", "x_column": "day", "y_columns": ["n"], "series_column": None}


def test_clean_visualization_accepts_a_genuinely_valid_decision(app_env):
    chartable = {
        "columns": ["day", "n", "region"],
        "rows": [{"day": "Mon", "n": 1, "region": "east"}, {"day": "Tue", "n": 2, "region": "west"}],
    }
    raw = {"chart_type": "line", "x_column": "day", "y_columns": ["n"], "series_column": "region"}
    cleaned = app_env.translate_routes._clean_visualization(raw, chartable)
    assert cleaned == {"chart_type": "line", "x_column": "day", "y_columns": ["n"], "series_column": "region"}


def test_clean_visualization_is_none_when_chartable_entry_is_none(app_env):
    # Even a perfectly well-formed visualization object must be rejected
    # outright if the server-side gate never made charting possible for
    # this turn in the first place.
    raw = {"chart_type": "bar", "x_column": "day", "y_columns": ["n"], "series_column": None}
    assert app_env.translate_routes._clean_visualization(raw, None) is None


def test_clean_single_summary_response_returns_none_for_unparseable_json(app_env):
    assert app_env.translate_routes._clean_single_summary_response("not json at all", None) is None
    assert app_env.translate_routes._clean_single_summary_response("", None) is None
    assert app_env.translate_routes._clean_single_summary_response(None, None) is None


def test_clean_single_summary_response_returns_none_for_label_only_summary(app_env):
    # A "summary" that follows the "<label>\n\n<body>" convention but never
    # produced a real body - is_label_only_response's specific failure mode
    # (see its own docstring) - is still rejected the same way it always
    # was, now from inside the JSON envelope rather than as the raw
    # response text.
    raw_text = json.dumps({"summary": "Results Summary\n\n", "visualization": None})
    assert app_env.translate_routes._clean_single_summary_response(raw_text, None) is None


def test_clean_single_summary_response_falls_back_to_null_visualization_without_failing_the_summary(app_env):
    # An otherwise-valid "summary" paired with an invalid "visualization"
    # (here, a hallucinated column) must not invalidate the whole response
    # and trigger a retry - only "summary" being broken should do that.
    # The bad visualization is simply cleaned down to None.
    chartable = {"columns": ["day", "n"], "rows": [{"day": "Mon", "n": 1}, {"day": "Tue", "n": 2}]}
    raw_text = json.dumps({
        "summary": "Results Summary\n\nSignups trended upward this week.",
        "visualization": {"chart_type": "bar", "x_column": "made_up", "y_columns": ["n"], "series_column": None},
    })
    parsed = app_env.translate_routes._clean_single_summary_response(raw_text, chartable)
    assert parsed["summary"] == "Results Summary\n\nSignups trended upward this week."
    assert parsed["visualization"] is None


def test_summarize_result_endpoint_returns_a_validated_visualization_for_a_genuinely_chartable_result(
    app_factory, monkeypatch,
):
    """Positive-path counterpart to test_summarize_result_endpoint_returns_
    no_sql_prefixed_summary_and_logs_a_real_connection_row above (which only
    exercises the "not chartable, visualization forced None" case): with a
    genuinely chartable (>= _CHART_MIN_ROWS, one numeric column) result set
    and a model response that picks real, valid columns, the route's final
    JSON must actually carry the validated visualization object through."""
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(FakeGenaiResponse(json.dumps({
        "summary": "Signups trended upward across the week.",
        "visualization": {
            "chart_type": "line", "x_column": "day", "y_columns": ["signups"], "series_column": None,
        },
    })))

    resp = env.client.post('/api/summarize-result', json={
        'prompt': 'how did signups trend this week',
        'sql': 'SELECT day, signups FROM daily_signups;',
        'results': [{
            "columns": ["day", "signups"],
            "rows": [
                {"day": "Mon", "signups": 10},
                {"day": "Tue", "signups": 14},
                {"day": "Wed", "signups": 9},
            ],
            "rowCount": 3,
        }],
    })
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['summary'] == '*** NO SQL *** Signups trended upward across the week.'
    assert data['visualization'] == {
        "chart_type": "line", "x_column": "day", "y_columns": ["signups"], "series_column": None,
    }
