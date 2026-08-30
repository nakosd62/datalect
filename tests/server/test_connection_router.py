"""
connection_router.py (Phase A of multi-database question-answering - see
translate_routes.py's module docstring) and its wiring into
/api/translate's stream_translation(): a session with 0/1 connections in
scope never imports/calls this module at all (the core backward-compat
regression guard - see translate_routes.py's `if not multi_db` branch),
a session with 2+ runs Phase A then generates tagged multi-statement SQL,
a client-echoed still-valid pinned set skips Phase A entirely, and a
Phase A failure (bad JSON, or the LLM call itself raising) falls back to
the first in-scope candidate rather than failing the whole request.

Also covers backends/base.py's extract_entry_names_from_schema_text
against each of this codebase's known schema-heading conventions
("Table: <name>", "Table family: <name> (...)", "Tab: <name>") directly,
as a lighter-weight stand-in for spinning up a real per-backend
connection harness for every dialect.
"""

import sqlite3
import threading
import time
import types as pytypes

from helpers import login_as, parse_translate_stream, write_database_presets_file


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


def _put_both_in_scope(client):
    resp = client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })
    assert resp.status_code == 200


def _set_all_mode(client):
    resp = client.post('/api/config', json={"in_scope_mode": "all"})
    assert resp.status_code == 200


# --- select_relevant_connections / _parse_router_response, direct unit tests ---


class _FakeProvider:
    """Minimal stand-in for translate_routes.py's LlmProvider - just enough
    of build_llm_input()/call() for select_relevant_connections to drive,
    with no real client/network involved at all."""

    def __init__(self, responses):
        self._responses = list(responses)  # list of str (response text) or Exception
        self.calls = []

    def build_llm_input(self, history, schema_block, new_prompt_content):
        return new_prompt_content

    def call(self, client, model, llm_input, system_instruction):
        self.calls.append({"llm_input": llm_input, "system_instruction": system_instruction})
        if not self._responses:
            raise AssertionError("_FakeProvider queue exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {}


def test_select_relevant_connections_parses_plain_json_array():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(["[1, 0]"])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": ["x"]},
                  {"name": "B", "dialect": "MySQL", "table_names": ["y"]}]
    result = select_relevant_connections(candidates, "some question", provider, client=None, model="m")
    assert result["indices"] == [1, 0]
    assert result["reasoning"] is None  # a bare array carries no reasoning
    assert result["usage"] == {}
    assert len(provider.calls) == 1


def test_select_relevant_connections_tolerates_markdown_fence_and_wrapped_object():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(['```json\n{"indices": [0], "reasoning": "Only one candidate given."}\n```'])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0]
    assert result["reasoning"] == "Only one candidate given."


def test_select_relevant_connections_clamps_to_max_connections():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(["[0, 1, 2, 3]"])
    candidates = [{"name": f"C{i}", "dialect": "PostgreSQL", "table_names": []} for i in range(4)]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m", max_connections=2)
    assert result["indices"] == [0, 1]


def test_select_relevant_connections_falls_back_to_zero_after_bad_json_twice():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(["not json at all", "still not json"])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []},
                  {"name": "B", "dialect": "PostgreSQL", "table_names": []}]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0]
    assert result["reasoning"] is None
    assert result["usage"] is None  # no attempt actually succeeded
    assert len(provider.calls) == 2  # one bounded retry, then fallback


def test_select_relevant_connections_falls_back_to_zero_when_call_raises():
    from connection_router import select_relevant_connections

    provider = _FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0]
    assert result["usage"] is None


def test_select_relevant_connections_drops_out_of_range_and_duplicate_indices():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(["[5, 0, 0, -1, 1]"])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []},
                  {"name": "B", "dialect": "PostgreSQL", "table_names": []}]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0, 1]


def test_select_relevant_connections_reasoning_absent_when_response_lacks_it():
    from connection_router import select_relevant_connections

    provider = _FakeProvider(['{"indices": [0]}'])
    candidates = [{"name": "A", "dialect": "PostgreSQL", "table_names": []}]
    result = select_relevant_connections(candidates, "q", provider, client=None, model="m")
    assert result["indices"] == [0]
    assert result["reasoning"] is None


# --- triage_all_mode_question's "database_prompts" - direct unit tests ---
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


# --- /api/translate wiring: single- vs multi-in-scope, pin, Phase A failure ---


def test_single_in_scope_never_calls_phase_a_and_response_has_no_connection_selection(app_factory, monkeypatch):
    env = app_factory(env={"GEMINI_PRESET_KEYS": "fake-key-1"})

    def _boom(*a, **kw):
        raise AssertionError("Phase A should never be reached for a single-in-scope session")
    monkeypatch.setattr(env.translate_routes, "select_relevant_connections", _boom)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("SELECT 1;"))

    resp = env.client.post('/api/translate', json={'prompt': 'show me stuff'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert 'connection_selection' not in data
    assert len(harness.generate_calls) == 1


def test_multi_in_scope_runs_phase_a_then_tags_generated_sql(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _put_both_in_scope(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    # Phase A: picks both, most-relevant first (candidate 1 = Marketing, then 0 = Sales).
    harness.queue_response(_gemini_ok("[1, 0]"))
    # Phase B: one statement per selected connection, tagged with the
    # ephemeral DB<N> ordinal matching resolved_selected_entries' order
    # (DB1 = Marketing Postgres, DB2 = Sales Postgres, since Phase A's [1, 0]
    # became that order).
    harness.queue_response(_gemini_ok(
        "-- database: DB1\nSELECT * FROM campaigns;\n\n-- database: DB2\nSELECT * FROM deals;"
    ))

    resp = env.client.post('/api/translate', json={'prompt': 'campaigns and deals'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 2

    assert data['connection_selection'] == [
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres"},
        {"kind": "preset", "id": "pg-a", "name": "Sales Postgres"},
    ]
    assert "-- database: preset:pg-b (Marketing Postgres)" in data['sql']
    assert "-- database: preset:pg-a (Sales Postgres)" in data['sql']
    assert "DB1" not in data['sql']
    assert "DB2" not in data['sql']


def test_pinned_connections_skip_phase_a(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _put_both_in_scope(env.client)

    def _boom(*a, **kw):
        raise AssertionError("Phase A should be skipped when a still-valid pin is echoed back")
    monkeypatch.setattr(env.translate_routes, "select_relevant_connections", _boom)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("-- database: DB1\nSELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={
        'prompt': 'more deals please',
        'pinned_connections': [{"kind": "preset", "id": "pg-a"}],
    })
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 1  # Phase B only
    assert data['connection_selection'] == [{"kind": "preset", "id": "pg-a", "name": "Sales Postgres"}]
    assert "-- database: preset:pg-a (Sales Postgres)" in data['sql']


def test_stale_pinned_connection_falls_back_to_running_phase_a_fresh(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _put_both_in_scope(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok("[0]"))  # Phase A actually runs
    harness.queue_response(_gemini_ok("-- database: DB1\nSELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={
        'prompt': 'more deals please',
        # References a connection no longer in scope - not a valid pin.
        'pinned_connections': [{"kind": "preset", "id": "no-longer-in-scope"}],
    })
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 2


def test_phase_a_failure_falls_back_to_first_in_scope_candidate(app_factory, tmp_path, monkeypatch):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    _put_both_in_scope(env.client)

    harness = GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    # Phase A gets one bounded retry inside select_relevant_connections
    # itself, so two unparseable responses exhausts it and falls back to
    # candidate 0 (Sales Postgres, first in stable in-scope order).
    harness.queue_response(_gemini_ok("not json"))
    harness.queue_response(_gemini_ok("still not json"))
    harness.queue_response(_gemini_ok("-- database: DB1\nSELECT * FROM deals;"))

    resp = env.client.post('/api/translate', json={'prompt': 'ambiguous question'})
    _, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert len(harness.generate_calls) == 3  # 2 Phase A attempts + 1 Phase B
    assert data['connection_selection'] == [{"kind": "preset", "id": "pg-a", "name": "Sales Postgres"}]


# --- "all configured databases" mode: 2-phase triage -> parallel Phase B ---
#
# connection_router.triage_all_mode_question's system prompt asks for
# {"action": "answer", "answer": "..."} or {"action": "route", "indices":
# [...], "message": "..."} (see that function's docstring) - a brand-new
# contract, NOT select_relevant_connections' {"indices": [...],
# "reasoning": "..."} shape the legacy multi_db tests above queue.


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
    # more than one connection per call, so it had nothing to mislabel
    # (unlike the legacy multi_db path's ephemeral DB<N> convention).
    assert "DB1" not in data['sql'] and "DB2" not in data['sql']
    assert data['connection_selection'] == [
        {"kind": "preset", "id": "pg-a", "name": "Sales Postgres"},
        {"kind": "preset", "id": "pg-b", "name": "Marketing Postgres"},
    ]
    assert data['database_notes'] == []
    assert data['generation_failures'] == []


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
    # NOT a candidate-0 fallback guess (unlike select_relevant_connections'
    # legacy behavior) - a wrong guess here would mean actually running
    # real SQL against a database the user never asked about.
    assert 'Sales Postgres' not in data['sql']
    assert 'router_route' not in data
    assert 'connection_selection' not in data


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


def test_summarize_all_mode_results_gives_up_after_retry_returning_none(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    provider = _FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
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
