"""
test_combine_results.py

Unit + HTTP-level tests for server/combine_routes.py - the new "all
databases" mode combine step (JOIN/UNION over already-fetched result sets,
via an embedded, in-memory DuckDB session - see that module's own
docstring for the full design). Mirrors test_connection_router.py's own
conventions for this family of tests (_FakeProvider/_drain/GenaiHarness,
duplicated locally rather than imported across test modules - same
established precedent those classes' own docstrings already state).
"""
import json
import types as pytypes

from helpers import parse_translate_stream, write_database_presets_file


def _drain(gen):
    """See test_connection_router.py's identical helper's own docstring -
    combine_all_mode_results is a generator for the same reason
    summarize_all_mode_results/triage_all_mode_question are (it forwards
    _summarize_with_retry's own live 'retrying' progress lines); a direct
    unit test with no NDJSON stream to forward into drains it here and
    keeps only the final `return` value."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class _FakeProvider:
    """Local copy of test_connection_router.py's _FakeProvider (same
    shape) - see its own docstring there for the full reasoning. Minimal
    stand-in for llm_providers.py's LlmProvider, just enough of
    build_llm_input()/call() for combine_all_mode_results to drive, with
    no real client/network involved at all."""

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


def _combine_json(sql):
    return json.dumps({"success": True, "sql": sql})


def _combine_failure_json(error):
    return json.dumps({"success": False, "error": error})


_SALES = {
    "name": "Sales Postgres", "kind": "preset", "id": "pg-a",
    "columns": ["customer_id", "name"],
    "rows": [{"customer_id": 1, "name": "Alice"}, {"customer_id": 2, "name": "Bob"}],
    "rowCount": 2,
}
_MARKETING = {
    "name": "Marketing Postgres", "kind": "preset", "id": "pg-b",
    "columns": ["cust_id", "campaign"],
    "rows": [{"cust_id": 2, "campaign": "Spring Sale"}, {"cust_id": 3, "campaign": "Winter Sale"}],
    "rowCount": 2,
}


# --- _combinable_entries ----------------------------------------------------

def test_combinable_entries_excludes_notes_and_failures_but_preserves_original_index(app_env):
    cr = app_env.combine_routes
    database_results = [
        dict(_SALES), {"name": "Support Postgres", "note": "Nothing relevant here."},
        dict(_MARKETING), {"name": "HR Postgres", "error": "Connection refused."},
    ]
    combinable = cr._combinable_entries(database_results)
    assert [i for i, _ in combinable] == [0, 2]
    assert combinable[0][1]["name"] == "Sales Postgres"
    assert combinable[1][1]["name"] == "Marketing Postgres"


def test_combinable_entries_excludes_an_entry_with_no_rows_key_at_all(app_env):
    cr = app_env.combine_routes
    assert cr._combinable_entries([{"name": "X", "columns": ["a"]}]) == []
    assert cr._combinable_entries([]) == []
    assert cr._combinable_entries(None) == []


# --- _build_combine_schema_block --------------------------------------------

def test_build_combine_schema_block_labels_views_by_original_index_with_inferred_types(app_env):
    cr = app_env.combine_routes
    combinable = cr._combinable_entries([dict(_SALES), dict(_MARKETING)])
    block = cr._build_combine_schema_block(combinable)
    assert "[0] view name: results_0" in block
    assert "[1] view name: results_1" in block
    assert "customer_id (integer)" in block
    assert "name (string)" in block
    assert "Total rows: 2" in block


# --- _is_single_readonly_select ----------------------------------------------

def test_is_single_readonly_select_accepts_plain_select_and_with_select(app_env):
    cr = app_env.combine_routes
    assert cr._is_single_readonly_select("SELECT * FROM results_0") is True
    assert cr._is_single_readonly_select("WITH a AS (SELECT 1) SELECT * FROM a") is True


def test_is_single_readonly_select_rejects_multi_statement_and_non_select(app_env):
    cr = app_env.combine_routes
    assert cr._is_single_readonly_select("SELECT 1; DROP TABLE results_0") is False
    assert cr._is_single_readonly_select("DELETE FROM results_0") is False
    assert cr._is_single_readonly_select("") is False
    assert cr._is_single_readonly_select(None) is False


# --- _execute_combine_sql ----------------------------------------------------

def test_execute_combine_sql_runs_a_real_join_against_registered_views(app_env):
    cr = app_env.combine_routes
    combinable = cr._combinable_entries([dict(_SALES), dict(_MARKETING)])
    sql = (
        "SELECT r0.name, r1.campaign FROM results_0 r0 "
        "JOIN results_1 r1 ON r0.customer_id = r1.cust_id"
    )
    columns, rows, row_count, truncated = cr._execute_combine_sql(sql, combinable)
    assert columns == ["name", "campaign"]
    assert rows == [{"name": "Bob", "campaign": "Spring Sale"}]
    assert row_count == 1
    assert truncated is False


def test_execute_combine_sql_runs_a_union(app_env):
    cr = app_env.combine_routes
    combinable = cr._combinable_entries([
        {"name": "A", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
        {"name": "B", "columns": ["x"], "rows": [{"x": 2}], "rowCount": 1},
    ])
    sql = "SELECT x FROM results_0 UNION ALL SELECT x FROM results_1 ORDER BY x"
    columns, rows, row_count, truncated = cr._execute_combine_sql(sql, combinable)
    assert columns == ["x"]
    assert rows == [{"x": 1}, {"x": 2}]
    assert row_count == 2


def test_execute_combine_sql_raises_on_a_reference_to_a_view_that_was_never_registered(app_env):
    cr = app_env.combine_routes
    combinable = cr._combinable_entries([dict(_SALES)])
    try:
        cr._execute_combine_sql("SELECT * FROM results_5", combinable)
        raise AssertionError("expected an exception")
    except Exception as e:
        assert "results_5" in str(e) or "Catalog" in str(e)


def test_execute_combine_sql_caps_output_rows_and_flags_truncated(app_env, monkeypatch):
    cr = app_env.combine_routes
    monkeypatch.setattr(cr, "COMBINE_RESULTS_MAX_ROWS", 2)
    combinable = cr._combinable_entries([
        {"name": "A", "columns": ["x"], "rows": [{"x": i} for i in range(5)], "rowCount": 5},
    ])
    columns, rows, row_count, truncated = cr._execute_combine_sql(
        "SELECT x FROM results_0 ORDER BY x", combinable,
    )
    assert row_count == 5
    assert len(rows) == 2
    assert truncated is True


def test_execute_combine_sql_cannot_touch_the_filesystem(app_env):
    # Safety net independent of _is_single_readonly_select's own statement-
    # shape check - see _execute_combine_sql's own docstring:
    # enable_external_access=False blocks this regardless of what the
    # statement itself looks like.
    cr = app_env.combine_routes
    combinable = cr._combinable_entries([dict(_SALES)])
    try:
        cr._execute_combine_sql("SELECT * FROM read_csv_auto('/etc/passwd')", combinable)
        raise AssertionError("expected a Permission Error")
    except Exception as e:
        assert "disabled" in str(e).lower() or "permission" in str(e).lower()


# --- _clean_combine_response -------------------------------------------------

def test_clean_combine_response_parses_success_and_failure_shapes(app_env):
    cr = app_env.combine_routes
    assert cr._clean_combine_response(_combine_json("SELECT 1")) == {"ok": True, "sql": "SELECT 1"}
    assert cr._clean_combine_response(_combine_failure_json("no shared key")) == {
        "ok": False, "error": "no shared key",
    }


def test_clean_combine_response_tolerates_markdown_fences_same_as_triage_does(app_env):
    cr = app_env.combine_routes
    fenced = "```json\n" + _combine_json("SELECT 1") + "\n```"
    assert cr._clean_combine_response(fenced) == {"ok": True, "sql": "SELECT 1"}


def test_clean_combine_response_returns_none_for_malformed_or_empty_text(app_env):
    cr = app_env.combine_routes
    assert cr._clean_combine_response("not json at all") is None
    assert cr._clean_combine_response("") is None
    assert cr._clean_combine_response(None) is None
    assert cr._clean_combine_response(json.dumps({"success": True, "sql": "  "})) is None
    assert cr._clean_combine_response(json.dumps({"success": False, "error": ""})) is None
    assert cr._clean_combine_response(json.dumps({"something": "else"})) is None


# --- combine_all_mode_results - direct generator unit tests -----------------

def test_combine_all_mode_results_is_skipped_with_fewer_than_two_combinable_entries(app_env):
    cr = app_env.combine_routes
    provider = _FakeProvider([])  # queue empty - an LLM call here would fail the test
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "q", [dict(_SALES)], provider, client=None, model="m",
    ))
    assert (outcome, result, usage, error) == ("skipped", None, None, None)
    assert provider.calls == []


def test_combine_all_mode_results_succeeds_and_actually_executes_the_generated_sql(app_env):
    cr = app_env.combine_routes
    provider = _FakeProvider([_combine_json(
        "SELECT r0.name, r1.campaign FROM results_0 r0 JOIN results_1 r1 ON r0.customer_id = r1.cust_id"
    )])
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "which customers are in both", [dict(_SALES), dict(_MARKETING)], provider, client=None, model="m",
    ))
    assert outcome == "combined"
    assert error is None
    assert result["columns"] == ["name", "campaign"]
    assert result["rows"] == [{"name": "Bob", "campaign": "Spring Sale"}]
    assert result["rowCount"] == 1
    assert result["truncated"] is False
    assert "JOIN" in result["sql"]
    assert len(provider.calls) == 1


def test_combine_all_mode_results_reports_the_models_own_honest_failure(app_env):
    cr = app_env.combine_routes
    provider = _FakeProvider([_combine_failure_json(
        "These results share no column that could identify the same real-world entity."
    )])
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "combine these", [dict(_SALES), dict(_MARKETING)], provider, client=None, model="m",
    ))
    assert outcome == "failed"
    assert result is None
    assert "no column" in error


def test_combine_all_mode_results_rejects_a_non_select_statement_before_ever_running_it(app_env):
    cr = app_env.combine_routes
    provider = _FakeProvider([_combine_json("DELETE FROM results_0")])
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "q", [dict(_SALES), dict(_MARKETING)], provider, client=None, model="m",
    ))
    assert outcome == "failed"
    assert "read-only" in error


def test_combine_all_mode_results_turns_a_duckdb_execution_error_into_an_honest_failure(app_env):
    cr = app_env.combine_routes
    # Syntactically a single read-only SELECT (passes _is_single_readonly_
    # select), but references a column neither view actually has - DuckDB
    # itself rejects this at execution time.
    provider = _FakeProvider([_combine_json("SELECT nonexistent_column FROM results_0")])
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "q", [dict(_SALES), dict(_MARKETING)], provider, client=None, model="m",
    ))
    assert outcome == "failed"
    assert result is None
    assert isinstance(error, str) and error


def test_combine_all_mode_results_gives_up_immediately_for_a_non_retryable_exception(app_env):
    # combine_all_mode_results deliberately returns the RAW exception here
    # (not yet formatted into a user-facing message) - see its own
    # docstring: formatting (format_llm_error_for_user, which needs a real
    # LlmProvider's own error_category()) is the ROUTE's job, same
    # isinstance-check contract summarize_all_mode_results' own identical
    # test already covers for its sibling pipeline.
    cr = app_env.combine_routes
    provider = _FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
    outcome, result, usage, error = _drain(cr.combine_all_mode_results(
        "q", [dict(_SALES), dict(_MARKETING)], provider, client=None, model="m",
    ))
    assert outcome == "failed"
    assert isinstance(error, RuntimeError)
    assert str(error) == "boom"
    assert len(provider.calls) == 1


# --- /api/combine-results - HTTP-level ---------------------------------------

def _presets_env(app_factory, tmp_path):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Sales Postgres", "type": "postgres", "url": "postgresql://u:p@host-a:5432/a"},
        {"id": "pg-b", "name": "Marketing Postgres", "type": "postgres", "url": "postgresql://u:p@host-b:5432/b"},
    ])
    return app_factory(env={"DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1"})


def test_combine_results_route_requires_a_prompt(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/combine-results', json={'database_results': [dict(_SALES)]})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_combine_results_route_requires_non_empty_database_results(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/combine-results', json={'prompt': 'q', 'database_results': []})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_combine_results_route_reports_skipped_with_fewer_than_two_combinable_results_with_no_llm_call(
    app_factory, tmp_path, monkeypatch,
):
    env = _presets_env(app_factory, tmp_path)

    class _ExplodingClient:
        def generate_content(self, *a, **k):
            raise AssertionError("should never be called for a skipped combine")

    class _ExplodingGenaiClient:
        def __init__(self, *a, **k):
            self.models = _ExplodingClient()

    monkeypatch.setattr(env.translate_routes.genai, "Client", _ExplodingGenaiClient)

    resp = env.client.post('/api/combine-results', json={
        'prompt': 'q', 'database_results': [dict(_SALES)],
    })
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data == {'status': 'done', 'success': True, 'skipped': True}


class _GenaiHarness:
    """Local copy of test_connection_router.py's GenaiHarness, trimmed to
    the single-threaded, single-queued-response shape this file's own
    HTTP-level test needs - see that class's own docstring for the fuller
    concurrent-fan-out-oriented version this is derived from."""

    def __init__(self):
        self.queue = []
        self.generate_calls = []

    def queue_response(self, resp):
        self.queue.append(resp)

    def make_client_class(self):
        harness = self

        class FakeModels:
            def generate_content(self, model, contents, config):
                harness.generate_calls.append({"model": model, "contents": contents, "config": config})
                if not harness.queue:
                    raise AssertionError("_GenaiHarness queue exhausted")
                item = harness.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, api_key=None, http_options=None):
                self.models = FakeModels()

        return FakeClient


def _gemini_ok(text):
    class _Resp:
        def __init__(self, text):
            self.text = text
            self.usage_metadata = pytypes.SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5, total_token_count=15,
                thoughts_token_count=0, cached_content_token_count=0,
            )
    return _Resp(text)


def test_combine_results_route_succeeds_end_to_end_with_a_real_join(app_factory, tmp_path, monkeypatch):
    env = _presets_env(app_factory, tmp_path)
    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(_combine_json(
        "SELECT r0.name, r1.campaign FROM results_0 r0 JOIN results_1 r1 ON r0.customer_id = r1.cust_id"
    )))

    resp = env.client.post('/api/combine-results', json={
        'prompt': 'which customers are in both',
        'database_results': [dict(_SALES), dict(_MARKETING)],
    })
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['status'] == 'done'
    assert data['success'] is True
    assert data['skipped'] is False
    assert data['result']['columns'] == ['name', 'campaign']
    assert data['result']['rows'] == [{'name': 'Bob', 'campaign': 'Spring Sale'}]
    assert data['result']['rowCount'] == 1


def test_combine_results_route_surfaces_an_honest_failure_without_a_500(app_factory, tmp_path, monkeypatch):
    env = _presets_env(app_factory, tmp_path)
    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(_combine_failure_json("no shared key across these two results")))

    resp = env.client.post('/api/combine-results', json={
        'prompt': 'q', 'database_results': [dict(_SALES), dict(_MARKETING)],
    })
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data == {
        'status': 'done', 'success': False, 'error': 'no shared key across these two results',
    }
