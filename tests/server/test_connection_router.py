"""
connection_router.py (Phase A of "all databases" mode - see
translate_routes.py's module docstring) and its wiring into
/api/translate's stream_translation(): a session whose in_scope_mode
isn't "all" never calls triage_all_mode_question at all (the core
regression guard - single-connection sessions and an explicit
database_url override both take that path), a session in "all" mode
runs triage first, which decides "answer" (no real data access needed),
"route" (generate and execute real SQL against one or more connections,
mechanically tagged with a stable marker server-side), or "failed" (the
triage LLM call never produced anything usable even after a bounded
retry - deliberately not a fallback guess at some candidate connection).

Also covers backends/base.py's extract_entry_names_from_schema_text
against each of this codebase's known schema-heading conventions
("Table: <name>", "Table family: <name> (...)", "Tab: <name>") directly,
as a lighter-weight stand-in for spinning up a real per-backend
connection harness for every dialect.
"""

import json
import sqlite3
import threading
import time
import types as pytypes

from helpers import login_as, parse_translate_stream, parse_translate_stream_events, write_database_presets_file


class GenaiHarness:
    """Local copy of test_translate_routes.py's GenaiHarness (same shape) -
    duplicated here rather than imported across test modules so this file
    has no load-order dependency on that one.

    Extended with a `lock` and `register_marker()` for "all databases"
    mode's Phase B fan-out (see translate_routes.py's _run_phase_b_fanout),
    which genuinely runs multiple generate_content calls concurrently
    across real threads - unlike every other test in this file, so
    `generate_calls`/the FIFO `queue` need real thread-safety here, not
    just single-threaded-test convenience. `register_marker(marker, resp)`
    registers a response returned only once the FIFO `queue` is empty AND
    the call's `contents` contains `marker` verbatim - this is what lets a
    parallel-fan-out test give each connection's own call a reliably
    correct canned response despite there being no guaranteed order across
    threads for which call lands first (a plain FIFO queue can't do this:
    which of N racing calls pops which queued item is nondeterministic)."""

    def __init__(self):
        self.queue = []
        self.markers = {}  # marker substring -> response, checked once `queue` is empty
        self.client_api_keys = []
        self.client_http_options = []
        self.generate_calls = []
        self.lock = threading.Lock()

    def queue_response(self, resp):
        self.queue.append(resp)

    def queue_error(self, exc):
        self.queue.append(exc)

    def register_marker(self, marker, resp):
        self.markers[marker] = resp

    def make_client_class(self):
        harness = self

        class FakeModels:
            def generate_content(self, model, contents, config):
                with harness.lock:
                    harness.generate_calls.append(
                        {"model": model, "contents": contents, "config": config,
                         "api_key": harness.client_api_keys[-1]}
                    )
                    if harness.queue:
                        item = harness.queue.pop(0)
                    else:
                        text_repr = str(contents)
                        item = next(
                            (resp for marker, resp in harness.markers.items() if marker in text_repr),
                            None,
                        )
                        if item is None:
                            raise AssertionError(
                                f"GenaiHarness: no queued response and no marker matched contents: {text_repr[:300]!r}"
                            )
                # A registered marker value may be a zero-arg callable
                # instead of a canned response/exception - lets a
                # concurrency test simulate a slow call (e.g. sleep then
                # return) without blocking the shared `lock` above for the
                # duration of that sleep (the callable runs after the
                # `with` block releases it).
                if callable(item) and not isinstance(item, Exception):
                    item = item()
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, api_key=None, http_options=None):
                self.api_key = api_key
                with harness.lock:
                    harness.client_api_keys.append(api_key)
                    harness.client_http_options.append(http_options)
                self.models = FakeModels()

        return FakeClient


def _two_preset_env(app_factory, tmp_path, extra_env=None):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Sales Postgres", "type": "postgres", "url": "postgresql://u:p@host-a:5432/a"},
        {"id": "pg-b", "name": "Marketing Postgres", "type": "postgres", "url": "postgresql://u:p@host-b:5432/b"},
    ])
    env = {"DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1"}
    env.update(extra_env or {})
    return app_factory(env=env)


def _set_all_mode(client):
    resp = client.post('/api/config', json={"in_scope_mode": "all"})
    assert resp.status_code == 200


class FakeApiError(Exception):
    """Minimal stand-in for google.genai.errors.APIError - just needs a
    `.code` int attribute, which translate_routes.py's
    _gemini_error_code() checks first. Local copy of test_translate_
    routes.py's own class of the same name (same shape) - duplicated
    here rather than imported across test modules, same as this file's
    GenaiHarness above."""
    def __init__(self, code):
        super().__init__(f"fake API error {code}")
        self.code = code


# --- Triage prompt: "answer"/"message" must lead with a "Triage" line ----
# The end user's UI (client.js's renderMarkdownLite()) bolds+underlines a
# standalone "Triage" line wherever it appears. Originally the parser
# (_parse_triage_response) just extracted these fields as-is regardless of
# content - but a real model turned out to sometimes over-comply with
# "alone on its own line" and respond with JUST the label and nothing
# else, which silently produced a bare heading with no actual triage text
# under it (see is_label_only_response and its two call sites below for
# the fix) - so both the prompt wording AND the parser's handling of that
# failure mode are covered here now. The label itself was later made
# language-agnostic (translated into the user's own question's language
# instead of a fixed English word), which is why is_label_only_response
# detects this failure mode by POSITION (a leading line, blank line, then
# nothing) rather than by matching specific label text - see its own
# docstring.

def test_triage_prompt_requires_answer_and_message_to_lead_with_a_translated_label_line():
    from connection_router import _TRIAGE_SYSTEM_INSTRUCTION

    assert "TRANSLATED into the SAME LANGUAGE as the user's own question" in _TRIAGE_SYSTEM_INSTRUCTION
    assert "\"answer\"" in _TRIAGE_SYSTEM_INSTRUCTION and "\"message\"" in _TRIAGE_SYSTEM_INSTRUCTION
    # Explicitly never required of "database_prompts" - those are internal,
    # per-connection instructions the end user never sees.
    assert "never \"database_prompts\"" in _TRIAGE_SYSTEM_INSTRUCTION
    # The label is explicitly called out as insufficient on its own - see
    # is_label_only_response's own docstring comment for the real failure
    # mode this guards against.
    assert "is not a valid" in _TRIAGE_SYSTEM_INSTRUCTION


def test_parse_triage_response_treats_an_answer_of_just_a_label_line_as_unparseable():
    from connection_router import _parse_triage_response

    # A label line (any language - "Triage"/"Clasificación" are just
    # examples) followed by a blank line and nothing else is unparseable,
    # regardless of what the label word actually is.
    for answer in (
        'Triage\n\n', '  Triage  \n\n   ', '**Triage**\n\n', '__Triage__\n\n',
        'triage\n\n', 'Clasificación\n\n',
    ):
        assert _parse_triage_response(
            json.dumps({"action": "answer", "answer": answer}), num_candidates=2, max_connections=20,
        ) is None


def test_parse_triage_response_accepts_a_bare_answer_with_no_label_line_at_all():
    from connection_router import _parse_triage_response

    # A response with no blank-line-separated leading line at all is NOT
    # label-only - it's an ordinary (if non-compliant with the label
    # convention) answer, and this app has always accepted that as-is
    # rather than retrying over it - see is_label_only_response's
    # docstring for why only a VISIBLE label-then-nothing shape is
    # flagged, never a plain unlabeled response.
    assert _parse_triage_response(
        json.dumps({"action": "answer", "answer": "Sales Postgres has the most customers."}),
        num_candidates=2, max_connections=20,
    ) == {"outcome": "answer", "answer": "Sales Postgres has the most customers."}


def test_parse_triage_response_downgrades_a_message_of_just_a_label_line_to_none():
    from connection_router import _parse_triage_response

    # Unlike "answer" above, a label-only "message" doesn't fail the whole
    # attempt - the routing decision (indices) is still good, and the
    # caller already has a translated-label fallback sentence for a
    # missing message (see translate_routes.py's stream_translation()).
    parsed = _parse_triage_response(
        json.dumps({"action": "route", "indices": [0], "message": "Triage\n\n"}),
        num_candidates=2, max_connections=20,
    )
    assert parsed == {
        "outcome": "route", "indices": [0], "message": None, "database_prompts": {},
    }


# --- triage_all_mode_question's "database_prompts" - direct unit tests ---


class _FakeProvider:
    """Minimal stand-in for translate_routes.py's LlmProvider - just enough
    of build_llm_input()/call() for triage_all_mode_question to drive, with
    no real client/network involved at all.

    `key_pool` (default: one fake key) and `classify_error` (default:
    every exception is non-retryable, i.e. always returns None) let a
    test drive triage_all_mode_question's own retry loop directly -
    key rotation on a 429-like classification, budget exhaustion, etc. -
    without needing the full GenaiHarness/real-Gemini-exception machinery
    the end-to-end tests further down use. Tests that don't care about
    retry behavior at all (the large majority - see the existing
    "database_prompts" tests below) get today's original behavior
    unchanged: a single configured key, and any raised exception treated
    as immediately non-retryable."""

    def __init__(self, responses, key_pool=None, classify_error=None):
        self._responses = list(responses)  # list of str (response text) or Exception
        self.calls = []
        self._key_pool = list(key_pool) if key_pool else ["fake-key-1"]
        self._classify_error = classify_error or (lambda exc: None)
        self.made_clients = []

    def build_llm_input(self, history, schema_block, new_prompt_content):
        return new_prompt_content

    def call(self, client, model, llm_input, system_instruction):
        self.calls.append({"client": client, "llm_input": llm_input, "system_instruction": system_instruction})
        if not self._responses:
            raise AssertionError("_FakeProvider queue exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {}

    def classify_error(self, exc):
        return self._classify_error(exc)

    def pick_api_key(self, exclude=None):
        exclude = exclude or set()
        remaining = [k for k in self._key_pool if k not in exclude]
        return remaining[0] if remaining else self._key_pool[0]

    def make_client(self, api_key):
        self.made_clients.append(api_key)
        return f"client-for-{api_key}"

    def get_key_pool_size(self):
        return len(self._key_pool)


#
# Phase B's per-connection calls are fully independent - each only ever
# sees ONE connection's own schema, never the original question's full
# framing or any other connection - so a question phrased across multiple
# databases at once ("give me data from 2 tables each from a different
# database") breaks down once handed unchanged to a single connection.
# triage_all_mode_question asks the model to rewrite the question per
# selected connection instead; these tests drive that field directly via
# _FakeProvider, independent of the full /api/translate round-trip.

def test_triage_all_mode_question_route_outcome_includes_per_connection_database_prompts():
    from connection_router import triage_all_mode_question

    provider = _FakeProvider([
        '{"action": "route", "indices": [0, 1], "message": "Checking A and B.", '
        '"database_prompts": {"0": "Give me data from one table in this database.", '
        '"1": "Give me data from a different table in this database."}}'
    ])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": ["x"]},
                  {"name": "B", "dialect": "MySQL", "table_names": ["y"]}]
    result = triage_all_mode_question(
        candidates, "give me data from 2 tables each from a different database", provider, client=None, model="m",
    )
    assert result["outcome"] == "route"
    assert result["indices"] == [0, 1]
    assert result["database_prompts"] == {
        0: "Give me data from one table in this database.",
        1: "Give me data from a different table in this database.",
    }


def test_triage_all_mode_question_route_outcome_defaults_to_empty_dict_when_model_omits_the_field():
    from connection_router import triage_all_mode_question

    # No "database_prompts" key at all - the model ignored/forgot it. This
    # must not be treated as an unparseable response (no retry wasted) -
    # the routing decision is still perfectly usable, the caller just falls
    # back to the original question for every selected connection.
    provider = _FakeProvider(['{"action": "route", "indices": [0], "message": "Checking A."}'])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": ["x"]}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")
    assert result["outcome"] == "route"
    assert result["database_prompts"] == {}
    assert len(provider.calls) == 1  # no wasted retry


def test_triage_all_mode_question_route_outcome_drops_database_prompts_entries_for_indices_the_model_didnt_pick():
    from connection_router import triage_all_mode_question

    # The model included a rewrite for index 2, but only selected indices
    # [0, 1] - index 2 was presumably dropped by _clean_indices (out of
    # range, a duplicate, or simply never chosen). That entry must be
    # silently discarded, not carried through to a connection Phase B was
    # never asked to contact at all.
    provider = _FakeProvider([
        '{"action": "route", "indices": [0, 1], "message": "Checking A and B.", '
        '"database_prompts": {"0": "Rewritten for A.", "1": "Rewritten for B.", '
        '"2": "Rewritten for a database not actually selected."}}'
    ])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []},
                  {"name": "B", "dialect": "MySQL", "table_names": []},
                  {"name": "C", "dialect": "MySQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0, 1]
    assert result["database_prompts"] == {0: "Rewritten for A.", 1: "Rewritten for B."}


def test_triage_all_mode_question_route_outcome_tolerates_malformed_database_prompts_entries():
    from connection_router import triage_all_mode_question

    # A non-dict "database_prompts", a non-string value, an empty/
    # whitespace-only string, and a non-numeric key must each be dropped
    # individually - none of this is a reason to retry the whole attempt,
    # since the routing decision (indices/message) is unaffected and still
    # perfectly usable; only the malformed per-connection rewrite is lost
    # (falling back to the original question for that one connection).
    provider = _FakeProvider([
        '{"action": "route", "indices": [0, 1, 2], "message": "Checking three.", '
        '"database_prompts": {"0": "Valid rewrite for A.", "1": 42, "2": "   ", "not-a-number": "x"}}'
    ])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []},
                  {"name": "B", "dialect": "MySQL", "table_names": []},
                  {"name": "C", "dialect": "MySQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0, 1, 2]
    assert result["database_prompts"] == {0: "Valid rewrite for A."}


def test_clean_database_prompts_returns_empty_dict_for_non_dict_input():
    from connection_router import _clean_database_prompts

    assert _clean_database_prompts(["not", "a", "dict"], [0, 1]) == {}
    assert _clean_database_prompts(None, [0, 1]) == {}
    assert _clean_database_prompts("also not a dict", [0, 1]) == {}


# --- triage_all_mode_question's own retry policy - direct unit tests ---
#
# Regression guard for the bug this fixes: the LLM call raising was
# previously indistinguishable from the model replying with unparseable
# text - both collapsed into the same generic {"outcome": "failed"}, with
# no key rotation attempted at all even for a classified-retryable error.
# These drive triage_all_mode_question directly via _FakeProvider's
# configurable key_pool/classify_error, independent of the full
# /api/translate round-trip (see the end-to-end GenaiHarness-based tests
# further down for that).


def test_triage_retries_a_retryable_error_and_succeeds_on_a_rotated_key():
    from connection_router import triage_all_mode_question

    # First call raises, second (after rotating to the pool's other key)
    # succeeds - proves the retry loop actually recovers, which the old
    # "catch everything, retry same key, give up after 2" loop never
    # could for a genuinely per-key capacity error.
    provider = _FakeProvider(
        [RuntimeError("rate limited"), '{"action": "answer", "answer": "42"}'],
        key_pool=["key-a", "key-b"],
        classify_error=lambda exc: {"rotate_key": True, "delay": 0},
    )
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client="initial-client", model="m")

    assert result["outcome"] == "answer"
    assert result["answer"] == "42"
    assert len(provider.calls) == 2
    # The retry rotated to a genuinely different key/client for the
    # second attempt.
    assert provider.made_clients == ["key-b"]
    assert provider.calls[1]["client"] == "client-for-key-b"


def test_triage_reports_api_error_when_key_rotation_budget_is_exhausted():
    from connection_router import triage_all_mode_question

    # Only ONE configured key (the common case) - a retryable/rotate-key
    # classification has nowhere to rotate to, so this must give up
    # immediately (1 call, not 2) rather than uselessly retrying the same
    # doomed key a second time the way the old loop did.
    provider = _FakeProvider(
        [RuntimeError("resource exhausted")],
        key_pool=["only-key"],
        classify_error=lambda exc: {"rotate_key": True, "delay": 0},
    )
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")

    assert result == {"outcome": "failed", "api_error": True}
    assert len(provider.calls) == 1


def test_triage_reports_api_error_for_a_non_retryable_exception():
    from connection_router import triage_all_mode_question

    # classify_error returning None (the _FakeProvider default) means
    # "not retryable" - same bad-request/auth-failure/invalid-model bucket
    # every other LLM call in this app raises immediately for. Still an
    # "api_error", not the generic unparseable-response apology: a
    # genuine API/config problem is never "I couldn't understand your
    # question."
    provider = _FakeProvider([RuntimeError("bad request")])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")

    assert result == {"outcome": "failed", "api_error": True}
    assert len(provider.calls) == 1


def test_triage_unparseable_response_is_not_reported_as_api_error():
    from connection_router import triage_all_mode_question

    # Regression guard the other way: a call that succeeds twice but
    # returns garbage both times is genuinely "couldn't understand the
    # question," not an API/capacity problem - api_error must stay False
    # so the caller shows _TRIAGE_FAILURE_TEXT, not _TRIAGE_API_ERROR_TEXT.
    provider = _FakeProvider(["not json", "still not json"])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = triage_all_mode_question(candidates, "q", provider, client=None, model="m")

    assert result == {"outcome": "failed", "api_error": False}
    assert len(provider.calls) == 2


# --- extract_entry_names_from_schema_text, against this codebase's known headings ---


def test_extract_entry_names_handles_table_heading_style():
    from backends.base import extract_entry_names_from_schema_text

    schema = "Table: users\nid INTEGER\nname TEXT\n\nTable: orders\nid INTEGER\n"
    assert extract_entry_names_from_schema_text(schema) == ["users", "orders"]


def test_extract_entry_names_handles_table_family_heading_style():
    from backends.base import extract_entry_names_from_schema_text

    schema = "Table family: events (partitioned by day)\ncol1 STRING\n"
    assert extract_entry_names_from_schema_text(schema) == ["events"]


def test_extract_entry_names_handles_tab_heading_style():
    from backends.base import extract_entry_names_from_schema_text

    schema = "Tab: Sheet1\ncolA\ncolB\n\nTab: Sheet2\ncolC\n"
    assert extract_entry_names_from_schema_text(schema) == ["Sheet1", "Sheet2"]


def test_extract_entry_names_never_raises_on_garbage_input():
    from backends.base import extract_entry_names_from_schema_text

    assert extract_entry_names_from_schema_text("") == []
    assert extract_entry_names_from_schema_text(None) == []
    assert extract_entry_names_from_schema_text("schema fetch failed: connection refused") == []


def test_extract_entry_names_respects_max_names_cap():
    from backends.base import extract_entry_names_from_schema_text

    schema = "\n\n".join(f"Table: t{i}\ncol INTEGER" for i in range(10))
    assert extract_entry_names_from_schema_text(schema, max_names=3) == ["t0", "t1", "t2"]


def test_single_in_scope_never_calls_triage_and_response_has_no_connection_selection(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})

    def _boom(*a, **kw):
        raise AssertionError("Triage should never be reached for a non-'all'-mode session")
    monkeypatch.setattr(env.translate_routes, "triage_all_mode_question", _boom)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show me stuff'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert 'connection_selection' not in data
    assert len(harness.generate_calls) == 1


# --- "all configured databases" mode: 2-phase triage -> parallel Phase B ---
#
# connection_router.triage_all_mode_question's system prompt asks for
# {"action": "answer", "answer": "..."} or {"action": "route", "indices":
# [...], "message": "..."} (see that function's docstring).


def _schema_fetch_by_url(mapping):
    """Returns a _fetch_database_schema replacement keyed by connection
    url, for monkeypatching db_module._fetch_database_schema - shared
    shape across the tests below that need per-connection full-schema
    control without a real database connection."""
    def _fetch(descriptor):
        return mapping[descriptor.get("url")]
    return _fetch


def test_all_mode_answer_outcome_returns_no_sql_text_and_never_calls_phase_b(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "answer", "answer": "You have 2 databases configured: Sales Postgres and Marketing Postgres."}'
    ))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 1  # triage only - Phase B never runs
    assert data['sql'] == (
        '*** NO SQL *** You have 2 databases configured: Sales Postgres and Marketing Postgres.'
    )
    # Byte-identical response shape to a single-connection NO-SQL reply -
    # no new fields, so client.js needs zero changes for this outcome.
    assert 'router_route' not in data
    assert 'connection_selection' not in data
    assert 'database_notes' not in data
    assert 'generation_failures' not in data


def test_all_mode_triage_call_receives_conversation_history_so_a_followup_can_resolve_which_database(app_factory, tmp_path, monkeypatch):
    # Regression guard for a real user-reported gap: the FIRST triage call
    # in a conversation had always passed history=[] (see the original,
    # explicit design: "there is no past turns" for that first call) - but
    # a SUBSEQUENT triage call in the SAME conversation needs the ordinary
    # history a follow-up like "how large is THIS database" depends on to
    # resolve which connection "this" even refers to, since triage's own
    # "answer" outcome from a prior turn is the only place that ever got
    # said out loud. Deliberately distinct from Phase B's per-connection
    # history, which remains empty always (unaffected by this fix).
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())

    # Turn 1: a pure meta-question, answered directly - no history at all
    # yet (a brand-new conversation), matching the original design.
    harness.queue_response(_gemini_ok(
        '{"action": "answer", "answer": "Marketing Postgres has campaign-related data."}'
    ))
    resp1 = env.client.post('/api/translate', json={'prompt': 'which database has campaign data'})
    _, data1 = parse_translate_stream(resp1)
    assert data1['sql'] == '*** NO SQL *** Marketing Postgres has campaign-related data.'

    # Turn 2: "how large is this database" only makes sense with turn 1's
    # answer in view - the client always echoes chatStore's accumulated
    # turns back as `history` (see client.js's translatePrompt()), so this
    # mirrors exactly what a real follow-up request sends.
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [1], "message": "Checking Marketing Postgres."}'
    ))
    harness.register_marker("campaigns", _gemini_ok("SELECT COUNT(*) FROM campaigns;"))
    resp2 = env.client.post('/api/translate', json={
        'prompt': 'how large is this database',
        'history': [
            {'role': 'user', 'text': 'which database has campaign data'},
            {'role': 'model', 'text': data1['sql']},
        ],
    })
    _, data2 = parse_translate_stream(resp2)

    # The second triage call's own prompt (generate_calls[1], right after
    # the first turn's single triage call at index 0) must actually carry
    # turn 1's answer text - proof history was threaded through, not just
    # that the mocked routing happened to work out.
    second_triage_contents = str(harness.generate_calls[1]["contents"])
    assert "Marketing Postgres has campaign-related data" in second_triage_contents

    assert data2['router_route'] is True
    assert "-- database: preset:pg-b (Marketing Postgres)\nSELECT COUNT(*) FROM campaigns;" in data2['sql']


def test_all_mode_route_outcome_runs_phase_b_in_parallel_for_both_selected_connections(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\namount NUMERIC\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\nspend NUMERIC\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking Sales Postgres and Marketing Postgres."}'
    ))
    # Marker-based (not FIFO) dispatch for the two Phase B calls, since
    # they genuinely race across threads - see GenaiHarness' docstring.
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    # Deliberately avoids the words "deals"/"campaigns" in the prompt
    # itself - those words are also the harness's dispatch markers (each
    # only appears in ONE connection's own schema text), and the prompt
    # text flows into every Phase B call's `contents` too, so a prompt
    # containing both would make the marker match ambiguous for both
    # calls regardless of which connection they're actually for.
    resp = env.client.post('/api/translate', json={'prompt': 'how is everything performing across the board'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 3  # triage + 2 Phase B calls

    assert data['router_route'] is True
    assert data['routing_message'] == "Checking Sales Postgres and Marketing Postgres."
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']
    assert "-- database: preset:pg-b (Marketing Postgres)\nSELECT * FROM campaigns;" in data['sql']
    # The server injected these markers mechanically - the model never saw
    # more than one connection per call, so it had nothing to mislabel.
    assert "DB1" not in data['sql'] and "DB2" not in data['sql']
    assert data['connection_selection'] == [
        {"kind": "preset", "id": "pg-a", "name": "Sales Postgres"},
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres"},
    ]
    assert data['database_notes'] == []
    assert data['generation_failures'] == []


def test_all_mode_route_outcome_falls_back_to_a_triage_labeled_message_when_the_model_omits_one(
    app_factory, tmp_path, monkeypatch
):
    # The model's own "message" field is what normally carries the
    # "Triage" leading line (see _TRIAGE_SYSTEM_INSTRUCTION); when a
    # "route" response has no usable message at all, translate_routes.py
    # builds a server-side fallback sentence instead - that fallback needs
    # the same "Triage" leading line for the Summary tab to look
    # consistent regardless of which source the text actually came from.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0, 1]}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    resp = env.client.post('/api/translate', json={'prompt': 'how is everything performing across the board'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['routing_message'].startswith("Triage\n\n")
    assert "Sales Postgres" in data['routing_message'] and "Marketing Postgres" in data['routing_message']


def test_all_mode_route_outcome_with_one_database_returning_no_sql_note(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0, 1], "message": "Checking both."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("*** NO SQL *** Campaigns data doesn't cover this question."))

    # Avoids "deals"/"campaigns" in the prompt itself - see the comment on
    # the parallel-fanout test above for why that would break the
    # harness's per-connection marker dispatch.
    resp = env.client.post('/api/translate', json={'prompt': 'first database question, plus something unrelated'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['router_route'] is True
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']
    assert "preset:pg-b" not in data['sql']  # a NO-SQL note, not fed into the executable SQL
    assert data['database_notes'] == [
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres",
         "text": "Campaigns data doesn't cover this question."},
    ]
    assert data['generation_failures'] == []


def test_all_mode_route_outcome_with_one_database_generation_failure_still_returns_the_other(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0, 1], "message": "Checking both."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    # A plain RuntimeError has no .code/isn't a genai ServerError/timeout,
    # so _classify_gemini_error treats it as non-retryable - raised
    # immediately, no wasted extra queued/marked responses needed here.
    harness.register_marker("campaigns", RuntimeError("simulated Gemini failure"))

    resp = env.client.post('/api/translate', json={'prompt': 'give me a full breakdown from both'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True  # the OVERALL request still succeeds
    assert data['router_route'] is True
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']
    assert "preset:pg-b" not in data['sql']
    assert data['generation_failures'] == [
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres", "error": "simulated Gemini failure"},
    ]
    assert data['database_notes'] == []


def test_all_mode_route_outcome_all_databases_fail_or_note_returns_empty_sql_but_success_true(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0, 1], "message": "Checking both."}'))
    harness.register_marker("deals", _gemini_ok("*** NO SQL *** Deals table has nothing relevant."))
    harness.register_marker("campaigns", RuntimeError("simulated failure"))

    resp = env.client.post('/api/translate', json={'prompt': 'irrelevant question'})
    _, data = parse_translate_stream(resp)
    # Every selected connection either noted or failed - `sql` is
    # legitimately empty, but this is still a successful response (the
    # client renders per-database detail, not a flat error).
    assert data['success'] is True
    assert data['router_route'] is True
    assert data['sql'] == ''
    assert data['database_notes'] == [
        {"kind": "preset", "id": "pg-a", "name": "Sales Postgres", "text": "Deals table has nothing relevant."},
    ]
    assert data['generation_failures'] == [
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres", "error": "simulated failure"},
    ]


def test_all_mode_emits_phase_status_lines_for_schema_collection_and_routing_before_any_outcome(
    app_factory, tmp_path, monkeypatch,
):
    """Regression guard for the bug this fixes: before these two lines
    existed, an "all databases" mode request streamed NOTHING at all while
    collecting every in-scope connection's schema summary
    (build_router_candidate_summaries) and while the triage LLM call
    itself was in flight (triage_all_mode_question) - and for the
    "answer"/"failed" outcomes specifically, NOTHING EVER arrived before
    the terminal "done" line (phase_a_route only fires for the "route"
    outcome - see the test below). Checked against the "answer" outcome
    here since it's the starkest case: no other event of any kind exists
    to mask the gap."""
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "answer", "answer": "You have 2 databases configured."}'))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have'})
    events = parse_translate_stream_events(resp)

    phase_status_events = [e for e in events if e['status'] == 'phase_status']
    assert [e['phase'] for e in phase_status_events] == ['collecting_schema_summaries', 'routing']
    assert all(isinstance(e['message'], str) and e['message'] for e in phase_status_events)
    # Both precede the terminal line, in order.
    assert events.index(phase_status_events[0]) < events.index(phase_status_events[1]) < len(events) - 1
    assert events[-1]['status'] == 'done'
    assert events[-1]['sql'] == '*** NO SQL *** You have 2 databases configured.'


def test_all_mode_route_outcome_streams_phase_a_route_then_phase_b_connection_done_before_terminal_done(
    app_factory, tmp_path, monkeypatch,
):
    # Progressive-rendering support (see translate_routes.py's
    # stream_translation() docstring on the router_only_all_mode "route"
    # branch): a "phase_a_route" line reports the routing message and
    # full connection_selection BEFORE either Phase B call has finished,
    # then one "phase_b_connection_done" line per selected connection
    # (this test doesn't care which order those two arrive in - see the
    # completion-order test below for that), then the existing terminal
    # "done" line last, unchanged in shape.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking both."}'
    ))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("*** NO SQL *** Campaigns data doesn't cover this question."))

    resp = env.client.post('/api/translate', json={'prompt': 'first database question, plus something else'})
    events = parse_translate_stream_events(resp)

    # Two phase_status lines now precede phase_a_route - schema-summary
    # collection, then routing (see translate_routes.py's
    # stream_translation() docstring and the dedicated test just below) -
    # so phase_a_route is no longer necessarily events[0]; find it
    # directly rather than assuming a fixed index.
    phase_status_events = [e for e in events if e['status'] == 'phase_status']
    assert [e['phase'] for e in phase_status_events] == ['collecting_schema_summaries', 'routing']

    route_event = next(e for e in events if e['status'] == 'phase_a_route')
    assert route_event['routing_message'] == 'Checking both.'
    assert route_event['connection_selection'] == [
        {"kind": "preset", "id": "pg-a", "name": "Sales Postgres"},
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres"},
    ]

    connection_done_events = [e for e in events if e['status'] == 'phase_b_connection_done']
    assert len(connection_done_events) == 2
    by_id = {e['id']: e for e in connection_done_events}
    assert by_id['pg-a']['outcome'] == 'sql'
    assert by_id['pg-a']['sql'] == "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;"
    assert by_id['pg-b']['outcome'] == 'note'
    assert by_id['pg-b']['text'] == "Campaigns data doesn't cover this question."

    # Every phase_b_connection_done line comes strictly after the
    # phase_a_route line and strictly before the terminal 'done' line.
    assert events[-1]['status'] == 'done'
    assert events.index(events[-1]) == len(events) - 1
    route_index = events.index(route_event)
    for e in connection_done_events:
        assert events.index(e) > route_index
        assert events.index(e) < len(events) - 1


def test_all_mode_route_outcome_streams_phase_b_connection_done_in_completion_order_not_original_order(
    app_factory, tmp_path, monkeypatch,
):
    # The whole point of turning _run_phase_b_fanout into a generator:
    # pg-a is selected FIRST (indices=[0, 1]) but its own generation call
    # is deliberately slow, so the client should still see pg-b's
    # phase_b_connection_done event first - proving these events reflect
    # real completion order, not selected_entries' original order (which
    # the terminal 'done' line's sql_blocks/database_notes/
    # generation_failures still preserve, unchanged - see the other
    # route-outcome tests above).
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking both."}'
    ))

    def _slow_deals_response():
        time.sleep(0.3)
        return _gemini_ok("SELECT * FROM deals;")

    harness.register_marker("deals", _slow_deals_response)
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    resp = env.client.post('/api/translate', json={'prompt': 'first database question, plus something else'})
    events = parse_translate_stream_events(resp)

    connection_done_events = [e for e in events if e['status'] == 'phase_b_connection_done']
    assert [e['id'] for e in connection_done_events] == ['pg-b', 'pg-a']

    # The terminal line's own ordering guarantee is untouched by any of
    # this - still selected_entries' ORIGINAL order (pg-a, then pg-b).
    _, data = parse_translate_stream(resp)
    assert data['sql'].index('preset:pg-a') < data['sql'].index('preset:pg-b')


def test_classify_generation_outcome_covers_sql_note_empty_and_failed_shapes(app_factory, tmp_path):
    # Direct unit test of the small helper _run_phase_b_fanout's
    # per-completion streaming event and its final original-order summary
    # loop both call, so the marker-prepend/note-strip logic is verified
    # once, in isolation, rather than only indirectly through the fuller
    # end-to-end route-outcome tests above.
    env = _two_preset_env(app_factory, tmp_path)
    classify = env.translate_routes._classify_generation_outcome
    entry = {"kind": "preset", "id": "pg-a", "name": "Sales Postgres"}

    assert classify(entry, ("ok", "SELECT * FROM deals;", {})) == {
        "outcome": "sql",
        "sql": "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;",
    }
    assert classify(entry, ("ok", "*** NO SQL *** nothing relevant here", {})) == {
        "outcome": "note",
        "text": "nothing relevant here",
    }
    assert classify(entry, ("ok", "   ", {})) == {"outcome": "note", "text": ""}
    assert classify(entry, ("failed", "boom")) == {"outcome": "failed", "error": "boom"}


def test_all_mode_failed_outcome_returns_fixed_apology_text_not_candidate_zero_fallback(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("not json"))
    harness.queue_response(_gemini_ok("still not json"))

    resp = env.client.post('/api/translate', json={'prompt': 'ambiguous question'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 2  # triage's own bounded retry, then "failed"
    assert data['sql'] == '*** NO SQL *** I am not able to respond to your prompt.'
    # NOT a candidate-0 fallback guess - a wrong guess here would mean
    # actually running real SQL against a database the user never asked
    # about.
    assert 'Sales Postgres' not in data['sql']
    assert 'router_route' not in data
    assert 'connection_selection' not in data


def test_all_mode_resource_exhausted_triage_shows_honest_message_not_generic_apology(
    app_factory, tmp_path, monkeypatch,
):
    """End-to-end regression guard for the actual bug report this fixes:
    with a single configured Gemini key, a 429 (RESOURCE_EXHAUSTED) on the
    triage call has nowhere to rotate to and must give up immediately -
    but the user-facing text must say so honestly (_TRIAGE_API_ERROR_TEXT),
    NOT the generic "I am not able to respond to your prompt" apology
    reserved for a genuinely unparseable response (see the test just
    above, which must keep getting that exact text)."""
    env = _two_preset_env(app_factory, tmp_path)  # GEMINI_PRESET_KEYS: one key
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_error(FakeApiError(429))

    resp = env.client.post('/api/translate', json={'prompt': 'ambiguous question'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    # No point retrying a second time against the same, already-exhausted
    # key - one call only, not triage's usual 2-attempt budget.
    assert len(harness.generate_calls) == 1
    assert data['sql'].startswith('*** NO SQL *** I couldn\'t reach the AI model right now')
    assert data['sql'] != '*** NO SQL *** I am not able to respond to your prompt.'
    assert 'router_route' not in data
    assert 'connection_selection' not in data


def test_all_mode_triage_recovers_by_rotating_to_a_second_configured_gemini_key(
    app_factory, tmp_path, monkeypatch,
):
    """The other half of the fix: with a SECOND configured key actually
    available, a 429 on the first must not be given up on at all - it
    rotates and the turn succeeds normally, exactly as every other LLM
    call in this app already does for a per-key capacity error."""
    env = _two_preset_env(app_factory, tmp_path, extra_env={
        "GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2",
    })
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_error(FakeApiError(429))
    harness.queue_response(_gemini_ok('{"action": "answer", "answer": "There are 2 databases configured."}'))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 2
    assert harness.client_api_keys[0] != harness.client_api_keys[1]
    assert data['sql'] == '*** NO SQL *** There are 2 databases configured.'


def test_all_mode_phase_b_generation_recovers_by_rotating_to_a_second_configured_gemini_key(
    app_factory, tmp_path, monkeypatch,
):
    """Regression guard for a real bug: _run_phase_b_fanout's per-connection
    worker (_run_one) used to pick a fresh, independent api_key but then
    hand it to generate_sql_for_connection alongside the OUTER client built
    for triage's own (separately, RANDOMLY picked - see
    pick_gemini_api_key) key. With 2+ configured keys those two frequently
    differed, so the request that actually hit the wire used one key while
    the retry loop's bookkeeping thought it was using another - on a 429 it
    could exclude a key that was never really tried and rotate straight
    back onto the one that just failed, or give up as "budget exhausted"
    while a perfectly good second key sat unused. Single connection here
    (no ThreadPoolExecutor concurrency to race) so this is fully
    deterministic: the queue is consumed in strict order (triage, then
    Phase B's two attempts)."""
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Sales Postgres", "type": "postgres", "url": "postgresql://u:p@host-a:5432/a"},
    ])
    env = app_factory(env={
        "DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1,fake-key-2",
    })
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema",
                         _schema_fetch_by_url({"postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n"}))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0], "message": "Checking Sales Postgres."}'))
    harness.queue_error(FakeApiError(429))  # Phase B's first attempt
    harness.queue_response(_gemini_ok("SELECT * FROM deals;"))  # Phase B's rotated retry

    resp = env.client.post('/api/translate', json={'prompt': 'how many deals do we have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    # Triage + 2 Phase B attempts (the first 429, the second recovered) -
    # NOT a generation_failures entry, which is what the old bug would
    # have produced once the (bogus) rotation budget looked exhausted.
    assert len(harness.generate_calls) == 3
    assert harness.generate_calls[1]['api_key'] != harness.generate_calls[2]['api_key']
    assert data['router_route'] is True
    assert data['generation_failures'] == []
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']


def test_all_mode_with_only_one_configured_connection_still_runs_triage_and_can_route(app_factory, tmp_path, monkeypatch):
    # Regression guard for the REMOVED "skip the LLM call entirely when
    # only 1 connection is configured" special case: correct under the old
    # Phase-A-only stub (nothing to route between), but wrong now - even
    # with one configured connection, triage still decides "answer
    # directly" vs. "actually go query this database," so skipping it
    # would mean a single-connection "all" session could never get real
    # SQL at all.
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Sales Postgres", "type": "postgres", "url": "postgresql://u:p@host-a:5432/a"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema",
                         _schema_fetch_by_url({"postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n"}))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0], "message": "Checking Sales Postgres."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={'prompt': 'how many deals do we have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 2  # triage + 1 Phase B call - no longer skipped
    assert data['router_route'] is True
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']
    assert data['connection_selection'] == [{"kind": "preset", "id": "pg-a", "name": "Sales Postgres"}]


def test_all_mode_dynamically_includes_a_newly_saved_custom_connection(app_factory, tmp_path, monkeypatch):
    # The whole point of "all" over a frozen, save-time-computed list: a
    # connection the user saves AFTER the session already has in_scope_mode
    # "all" must be included on the very next request, with no re-save of
    # scope at all - db.py's _resolve_all_configured_descriptors resolves
    # state_store.get_db_connections() fresh every call.
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Sales Postgres", "type": "postgres", "url": "postgresql://u:p@host-a:5432/a"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1"})
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "answer", "answer": "You have 1 database configured."}'))

    resp = env.client.post('/api/translate', json={'prompt': 'show me stuff'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert 'connection_selection' not in data
    assert data['sql'] == '*** NO SQL *** You have 1 database configured.'

    # Alice saves a second connection of her own (a custom one) - a
    # completely separate save from the in-scope arrays/in_scope_mode,
    # neither of which this request even mentions.
    resp2 = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@host-b:5432/b",
        "database_name": "Marketing Postgres", "is_custom": True,
    })
    assert resp2.status_code == 200

    harness.queue_response(_gemini_ok('{"action": "route", "indices": [1], "message": "Checking Marketing Postgres."}'))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))
    resp3 = env.client.post('/api/translate', json={'prompt': 'marketing figures please'})
    _, data3 = parse_translate_stream(resp3)
    assert data3['success'] is True
    assert data3['router_route'] is True
    assert "-- database: custom:" in data3['sql']
    assert "Marketing Postgres" in data3['sql']
    assert "SELECT * FROM campaigns;" in data3['sql']


def test_all_mode_fetches_schema_for_every_candidate_regardless_of_cache_state(app_factory, tmp_path, monkeypatch):
    # Triage's candidate summaries must reflect a live schema fetch for
    # EVERY in-scope connection, not just whichever happens to already be
    # sitting in schema_cache - e.g. right after a server restart, when the
    # cache is empty for every connection. build_router_candidate_summaries
    # calls the ordinary, cache-aware get_database_schema() per connection
    # (db.py), which itself always fetches fresh on a cache miss - this test
    # proves that live fetch actually happens for BOTH candidates (a fresh
    # app_factory instance starts with a genuinely empty schema_cache, same
    # as a real restart), and that both candidates' real table names reach
    # the triage prompt - not just the one it ends up selecting.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    # Imported only AFTER app_factory/fresh_import has run - it drops "db"
    # from sys.modules and re-imports it fresh per test (see helpers.py's
    # fresh_import docstring), so importing it any earlier would monkeypatch
    # a stale module object translate_routes.py never actually calls into.
    import db as db_module

    fetched_urls = []

    def _fake_fetch(descriptor):
        fetched_urls.append(descriptor.get("url"))
        if descriptor.get("url") == "postgresql://u:p@host-a:5432/a":
            return "Table: deals\nid INTEGER\n"
        return "Table: campaigns\nid INTEGER\n"

    monkeypatch.setattr(db_module, "_fetch_database_schema", _fake_fetch)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0], "message": "Deals question."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={'prompt': 'how many deals do we have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True

    # Both connections' schemas were actually fetched live during triage's
    # candidate-summary build - neither was pre-cached, neither skipped.
    assert set(fetched_urls) == {"postgresql://u:p@host-a:5432/a", "postgresql://u:p@host-b:5432/b"}

    # And both candidates' real table names reached the triage prompt -
    # not just the one it happened to select.
    triage_prompt_text = str(harness.generate_calls[0]["contents"])
    assert "deals" in triage_prompt_text
    assert "campaigns" in triage_prompt_text


def test_all_mode_route_phase_b_uses_empty_history_and_full_schema_per_connection(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\namount NUMERIC\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\nspend NUMERIC\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0], "message": "Checking Sales Postgres."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={
        'prompt': 'how many deals',
        'history': [{"role": "user", "text": "some earlier unrelated turn"}, {"role": "model", "text": "an answer"}],
    })
    _, data = parse_translate_stream(resp)
    assert data['success'] is True

    # generate_calls[0] is triage (table names only); generate_calls[1] is
    # the Phase B call, which must carry pg-a's FULL schema (column-level
    # detail triage never saw) and EMPTY history - never this request's
    # actual past turns, regardless of what was sent (per-database history
    # is explicitly deferred to later work).
    assert len(harness.generate_calls) == 2
    phase_b_contents = str(harness.generate_calls[1]["contents"])
    assert "amount" in phase_b_contents
    assert "some earlier unrelated turn" not in phase_b_contents


def test_all_mode_route_phase_b_uses_triages_per_connection_rewrite_not_the_original_cross_database_prompt(
        app_factory, tmp_path, monkeypatch):
    # Regression guard for a real user-reported failure: a question phrased
    # across multiple databases at once ("give me data from 2 tables each
    # from a different database") used to get passed VERBATIM to both
    # Phase B calls - each of which only ever sees ONE connection's schema,
    # so an instruction that itself still talks about needing a different
    # database made no sense to either and failed. Each Phase B call must
    # instead receive its OWN, connection-scoped rewrite from triage's
    # "database_prompts" - never the original question's cross-database
    # framing.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking Sales Postgres and Marketing Postgres.", '
        '"database_prompts": {'
        '"0": "Give me data from one table in this database.", '
        '"1": "Give me data from a different table in this database."}}'
    ))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    resp = env.client.post('/api/translate', json={
        'prompt': 'give me data from 2 tables each from a different database',
    })
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['router_route'] is True

    # generate_calls[0] is triage; [1]/[2] are the two Phase B calls (order
    # not guaranteed under real concurrency, so check both, not by index).
    phase_b_contents = [str(c["contents"]) for c in harness.generate_calls[1:]]
    assert any("Give me data from one table in this database." in c for c in phase_b_contents)
    assert any("Give me data from a different table in this database." in c for c in phase_b_contents)
    # The original, cross-database-phrased question never reached either
    # Phase B call - only its per-connection rewrite did.
    assert not any("2 tables each from a different database" in c for c in phase_b_contents)


def test_all_mode_route_phase_b_falls_back_to_the_original_prompt_when_triage_omits_database_prompts(
        app_factory, tmp_path, monkeypatch):
    # Graceful degradation: a "route" response with no "database_prompts"
    # field at all (the model ignored/forgot it, or is an older/simpler
    # response shape) must still work exactly as it did before this field
    # existed - every selected connection gets the original question
    # unchanged, not an empty/missing prompt.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking both."}'
    ))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    resp = env.client.post('/api/translate', json={'prompt': 'the original question, verbatim'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True

    phase_b_contents = [str(c["contents"]) for c in harness.generate_calls[1:]]
    assert all("the original question, verbatim" in c for c in phase_b_contents)


def test_all_mode_route_phase_b_calls_run_concurrently_not_serially(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0, 1], "message": "Checking both."}'))

    delay_seconds = 0.4

    def _slow(text):
        def _make():
            time.sleep(delay_seconds)
            return _gemini_ok(text)
        return _make

    harness.register_marker("deals", _slow("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _slow("SELECT * FROM campaigns;"))

    start = time.perf_counter()
    resp = env.client.post('/api/translate', json={'prompt': 'give me a full breakdown from both'})
    _, data = parse_translate_stream(resp)
    elapsed = time.perf_counter() - start

    assert data['success'] is True
    assert "preset:pg-a" in data['sql']
    assert "preset:pg-b" in data['sql']
    # Run serially, this would take at least 2 * delay_seconds (plus the
    # triage call). Run in parallel (the whole point of _run_phase_b_fanout
    # above), total wall time stays well under that.
    assert elapsed < delay_seconds * 1.8


# --- Phase A (triage) logged to the translations table as "All Databases"/"All Databases" ---


def _translation_rows(env):
    """Every row currently in the translations table, oldest first, as
    plain dicts - a raw query against the same SQLite file app_config's
    state_store is using, same pattern as
    test_translation_history_naming.py's _last_recorded_database_name
    (get_translation_history() doesn't surface database_type/database_name
    at all, so there's no route through the app's own API to check
    these)."""
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


def test_all_mode_answer_outcome_logs_triage_as_a_dedicated_all_all_row(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "answer", "answer": "You have 2 databases configured."}'
    ))

    resp = env.client.post('/api/translate', json={'prompt': 'how many databases do I have'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True

    # The "answer" outcome IS Phase A in its entirety - exactly one
    # translations-table row, tagged "All Databases"/"All Databases" rather than any real
    # database (there's no real database involved at all here), carrying
    # triage's own token usage (see _gemini_ok's fixed usage_metadata).
    rows = _translation_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row['database_type'] == 'All Databases'
    assert row['database_name'] == 'All Databases'
    assert row['nl_prompt'] == 'how many databases do I have'
    assert row['sql_command'] == data['sql']
    assert (row['input_tokens'], row['output_tokens'], row['total_tokens'],
            row['thinking_tokens'], row['cached_content_tokens']) == (10, 5, 15, 0, 0)


def test_all_mode_failed_outcome_logs_triage_as_a_dedicated_all_all_row(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    # Every attempt comes back unparseable - triage exhausts its bounded
    # retry and falls back to the fixed apology text (see
    # _TRIAGE_FAILURE_TEXT), never a candidate-0 guess.
    harness.queue_response(_gemini_ok("not json"))
    harness.queue_response(_gemini_ok("still not json"))

    resp = env.client.post('/api/translate', json={'prompt': 'gibberish question'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True

    rows = _translation_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row['database_type'] == 'All Databases'
    assert row['database_name'] == 'All Databases'
    assert row['sql_command'] == data['sql']


def test_all_mode_route_outcome_logs_a_separate_all_all_triage_row_with_no_double_counting(
    app_factory, tmp_path, monkeypatch,
):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(
        '{"action": "route", "indices": [0, 1], "message": "Checking Sales Postgres and Marketing Postgres."}'
    ))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;"))
    harness.register_marker("campaigns", _gemini_ok("SELECT * FROM campaigns;"))

    resp = env.client.post('/api/translate', json={'prompt': 'how is everything performing across the board'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['router_route'] is True

    # Two rows now: Phase A's own "All Databases"/"All Databases" row, and Phase B's row
    # attributed to the first selected connection (same convention
    # `connection_selection`'s ordering already uses) - never combined
    # into just one the way this used to work.
    rows = _translation_rows(env)
    assert len(rows) == 2
    triage_row, phase_b_row = rows

    assert triage_row['database_type'] == 'All Databases'
    assert triage_row['database_name'] == 'All Databases'
    assert triage_row['nl_prompt'] == 'how is everything performing across the board'
    # Not real SQL - Phase A's own routing decision, same '*** NO SQL ***'
    # convention the "answer"/"failed" outcomes use for their own text.
    assert triage_row['sql_command'] == '*** NO SQL *** Checking Sales Postgres and Marketing Postgres.'
    assert (triage_row['input_tokens'], triage_row['output_tokens'], triage_row['total_tokens'],
            triage_row['thinking_tokens'], triage_row['cached_content_tokens']) == (10, 5, 15, 0, 0)

    assert phase_b_row['database_type'] == 'postgres'
    assert phase_b_row['database_name'] == 'Sales Postgres'
    assert phase_b_row['sql_command'] == data['sql']
    # Phase B's own portion only - two successful Phase B calls, each with
    # _gemini_ok's fixed usage, summed - Phase A's own usage (already in
    # the row above) is never folded in here too.
    assert (phase_b_row['input_tokens'], phase_b_row['output_tokens'], phase_b_row['total_tokens'],
            phase_b_row['thinking_tokens'], phase_b_row['cached_content_tokens']) == (20, 10, 30, 0, 0)

    # The response's own combined totals (what the client actually shows
    # the user for this turn) still reflect Phase A + Phase B together...
    assert data['input_tokens'] == 30
    assert data['output_tokens'] == 15
    assert data['total_tokens'] == 45
    # ...while the two logged rows' durations are an exact split of that
    # same combined total - proof nothing is double-counted (or dropped)
    # across the two rows, not just that both happen to be non-negative.
    assert triage_row['duration'] + phase_b_row['duration'] == data['duration']
    assert triage_row['duration'] >= 0 and phase_b_row['duration'] >= 0


# --- "All databases" mode, Phase C: post-execution results summarization ---


def test_build_summary_prompt_formats_real_results_notes_and_errors_into_labeled_blocks(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    prompt_text = env.translate_routes._build_summary_prompt(
        "how is everything performing",
        [
            {"name": "Sales Postgres", "columns": ["total"], "rows": [{"total": 500}], "rowCount": 1},
            {"name": "Marketing Postgres", "note": "No revenue data tracked here."},
            {"name": "Ops MySQL", "error": "connection refused"},
        ],
    )
    assert "Original question: how is everything performing" in prompt_text
    assert "Sales Postgres - 1 row(s) total, showing 1:" in prompt_text
    assert "Columns: total" in prompt_text
    assert "Marketing Postgres: No revenue data tracked here." in prompt_text
    assert "Ops MySQL: query failed - connection refused" in prompt_text


def test_build_summary_prompt_caps_rows_the_same_way_past_turn_history_does(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    many_rows = [{"n": i} for i in range(env.translate_routes.HISTORY_RESULT_MAX_ROWS + 25)]
    prompt_text = env.translate_routes._build_summary_prompt(
        "q", [{"name": "Sales Postgres", "columns": ["n"], "rows": many_rows, "rowCount": len(many_rows)}],
    )
    max_rows = env.translate_routes.HISTORY_RESULT_MAX_ROWS
    assert f"showing {max_rows}" in prompt_text
    assert f"Total Rows: {len(many_rows)}" in prompt_text
    # Exactly max_rows serialized row lines, not every row in `many_rows`.
    assert prompt_text.count("{'n':") == max_rows


def test_summarize_all_mode_results_returns_stripped_text_and_usage_on_success(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    provider = _FakeProvider(["  Sales is up 10%, Marketing had no data.  "])
    text, usage = env.translate_routes.summarize_all_mode_results(
        "how is everything performing", [{"name": "Sales Postgres", "columns": [], "rows": []}],
        provider, client=None, model="m",
    )
    assert text == "Sales is up 10%, Marketing had no data."
    assert usage == {}
    assert len(provider.calls) == 1


def test_summarize_all_mode_results_gives_up_immediately_for_a_non_retryable_exception(app_factory, tmp_path):
    # classify_error returning None (the _FakeProvider default) means "not
    # retryable" - same bucket every other LLM call in this app raises
    # immediately for (see triage_all_mode_question's identical test). No
    # point spending a second content-validity attempt on a call that's
    # already just proven it can't succeed right now.
    env = _two_preset_env(app_factory, tmp_path)
    provider = _FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
    text, usage = env.translate_routes.summarize_all_mode_results(
        "q", [{"name": "Sales Postgres", "columns": [], "rows": []}], provider, client=None, model="m",
    )
    assert (text, usage) == (None, None)
    assert len(provider.calls) == 1


# --- summarize_all_mode_results' own retry policy - direct unit tests ----
#
# Regression guard for the bug this fixes: unlike every other LLM call in
# "all databases" mode (triage_all_mode_question, generate_sql_for_
# connection), Phase C's summarization call used a bare 2-attempt retry
# with no key rotation and no transient-error wait at all - a real
# capacity/rate-limit error on the configured Gemini key exhausted that
# budget in well under a second, silently leaving the Summary tab exactly
# as it was before Phase C ran (no error surfaced anywhere) - easy to
# mistake for a client-side rendering bug, which is exactly what it looked
# like. These mirror triage_all_mode_question's own key-rotation tests
# above almost exactly.

def test_summarize_all_mode_results_retries_a_retryable_error_and_succeeds_on_a_rotated_key(
    app_factory, tmp_path,
):
    env = _two_preset_env(app_factory, tmp_path)
    provider = _FakeProvider(
        [RuntimeError("rate limited"), "Result Summary\n\nSales is up 10%."],
        key_pool=["key-a", "key-b"],
        classify_error=lambda exc: {"rotate_key": True, "delay": 0},
    )
    text, usage = env.translate_routes.summarize_all_mode_results(
        "how is everything performing", [{"name": "Sales Postgres", "columns": [], "rows": []}],
        provider, client="initial-client", model="m",
    )
    assert text == "Result Summary\n\nSales is up 10%."
    assert len(provider.calls) == 2
    # The retry rotated to a genuinely different key/client for the
    # second attempt, same as triage_all_mode_question's own retry does.
    assert provider.made_clients == ["key-b"]
    assert provider.calls[1]["client"] == "client-for-key-b"


def test_summarize_all_mode_results_gives_up_immediately_when_key_rotation_budget_is_exhausted(
    app_factory, tmp_path,
):
    # Only ONE configured key (the common case) - a retryable/rotate-key
    # classification has nowhere to rotate to, so this gives up after 1
    # call rather than uselessly retrying the same doomed key again.
    env = _two_preset_env(app_factory, tmp_path)
    provider = _FakeProvider(
        [RuntimeError("resource exhausted")],
        key_pool=["only-key"],
        classify_error=lambda exc: {"rotate_key": True, "delay": 0},
    )
    text, usage = env.translate_routes.summarize_all_mode_results(
        "q", [{"name": "Sales Postgres", "columns": [], "rows": []}], provider, client=None, model="m",
    )
    assert (text, usage) == (None, None)
    assert len(provider.calls) == 1


# A real model turned out to sometimes over-comply with
# _SUMMARY_SYSTEM_INSTRUCTION's "alone on its own first line" wording and
# respond with JUST the "Result Summary" label, nothing else - which used
# to sail straight through the `if stripped:` check (a non-empty string)
# and get shown to the user as a bare heading with no summary under it.
# Treated the same as a genuinely empty response: retried once, and if the
# second attempt is no better, (None, None) - same as any other
# unrecoverable Phase C failure (the Summary tab is just left as-is).
def test_summarize_all_mode_results_retries_a_response_that_is_just_the_results_summary_label(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    # A label line (any language - "Result(s) Summary"/"Résumé des
    # résultats" are just examples) followed by a blank line and nothing
    # else is unparseable, regardless of what the label word actually is -
    # see is_label_only_response's docstring for why this is POSITION-
    # based, not a match against a fixed English string.
    for label in (
        "Results Summary\n\n", "  Results Summary  \n\n  ", "**Results Summary**\n\n",
        "result summary\n\n", "Résumé des résultats\n\n",
    ):
        provider = _FakeProvider([label, "Results Summary\n\nSales is up 10%."])
        text, usage = env.translate_routes.summarize_all_mode_results(
            "how is everything performing", [{"name": "Sales Postgres", "columns": [], "rows": []}],
            provider, client=None, model="m",
        )
        assert text == "Results Summary\n\nSales is up 10%."
        assert len(provider.calls) == 2

    provider = _FakeProvider(["Results Summary\n\n", "Results Summary\n\n"])
    text, usage = env.translate_routes.summarize_all_mode_results(
        "q", [{"name": "Sales Postgres", "columns": [], "rows": []}], provider, client=None, model="m",
    )
    assert (text, usage) == (None, None)
    assert len(provider.calls) == 2


def test_summarize_results_endpoint_returns_no_sql_prefixed_summary_and_logs_an_all_databases_row(
    app_factory, tmp_path, monkeypatch,
):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("Sales revenue is $500; Marketing had nothing relevant."))

    resp = env.client.post('/api/summarize-results', json={
        'prompt': 'how is everything performing across the board',
        'database_results': [
            {"kind": "preset", "id": "pg-a", "name": "Sales Postgres",
             "columns": ["total"], "rows": [{"total": 500}], "rowCount": 1},
            {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres", "note": "Nothing relevant."},
        ],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['summary'] == '*** NO SQL *** Sales revenue is $500; Marketing had nothing relevant.'

    rows = _translation_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row['database_type'] == 'All Databases'
    assert row['database_name'] == 'All Databases'
    assert row['nl_prompt'] == 'how is everything performing across the board'
    assert row['sql_command'] == data['summary']
    assert (row['input_tokens'], row['output_tokens'], row['total_tokens']) == (10, 5, 15)


def test_summarize_results_endpoint_requires_prompt_and_database_results(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/summarize-results', json={'prompt': '', 'database_results': []})
    assert resp.status_code == 400

    resp = env.client.post('/api/summarize-results', json={'prompt': 'q'})
    assert resp.status_code == 400

    assert _translation_rows(env) == []


def test_summarize_results_endpoint_returns_success_false_when_the_llm_call_fails(
    app_factory, tmp_path, monkeypatch,
):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(RuntimeError("simulated failure"))
    harness.queue_response(RuntimeError("simulated failure again"))

    resp = env.client.post('/api/summarize-results', json={
        'prompt': 'q',
        'database_results': [{"kind": "preset", "id": "pg-a", "name": "Sales Postgres", "columns": [], "rows": []}],
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is False
    # Best-effort - no translations-table row for a call that never
    # produced anything to log.
    assert _translation_rows(env) == []


# --- Regression: a real provider reporting None (not 0) for a usage field ---


def test_all_mode_route_outcome_tolerates_a_real_usage_field_reported_as_none_not_zero(
    app_factory, tmp_path, monkeypatch,
):
    # Real-world crash this guards against: a genuine Gemini response's
    # usage_metadata.thoughts_token_count comes back as None (not 0)
    # whenever a call didn't use extended thinking - _gemini_ok's default
    # of 0 for every test above never exercised this, since a mock always
    # supplies a real int. _call_gemini's usage dict construction used to
    # do `getattr(usage, 'thoughts_token_count', 0)`, which only
    # substitutes 0 for a MISSING attribute, never a present-but-None one -
    # so this field stayed None all the way into _run_phase_b_fanout's
    # `usage_totals[k] += ...` summation, which raised
    # "TypeError: unsupported operand type(s) for +=: 'int' and 'NoneType'"
    # the moment a "route" outcome's Phase B call actually hit this in
    # production.
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _set_all_mode(env.client)

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-a:5432/a": "Table: deals\nid INTEGER\n",
        "postgresql://u:p@host-b:5432/b": "Table: campaigns\nid INTEGER\n",
    }))

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok('{"action": "route", "indices": [0], "message": "Checking Sales Postgres."}'))
    harness.register_marker("deals", _gemini_ok("SELECT * FROM deals;", thinking_tokens=None, cached_tokens=None))

    resp = env.client.post('/api/translate', json={'prompt': 'show me some data from that database'})
    _, data = parse_translate_stream(resp)

    assert data['success'] is True
    assert "-- database: preset:pg-a (Sales Postgres)\nSELECT * FROM deals;" in data['sql']
    # None was coerced to 0, not left as None (which would either crash
    # the summation above, or silently poison the JSON payload/
    # translations-table row with a null instead of an integer).
    assert data['thinking_tokens'] == 0
    assert data['cached_content_tokens'] == 0

    rows = _translation_rows(env)
    assert all(row['thinking_tokens'] == 0 and row['cached_content_tokens'] == 0 for row in rows)


def _gemini_ok(text, thinking_tokens=0, cached_tokens=0):
    class _Resp:
        def __init__(self, text):
            self.text = text
            self.usage_metadata = pytypes.SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5, total_token_count=15,
                thoughts_token_count=thinking_tokens, cached_content_token_count=cached_tokens,
            )
    return _Resp(text)
