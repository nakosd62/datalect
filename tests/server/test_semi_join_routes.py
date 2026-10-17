"""
test_semi_join_routes.py

Unit + HTTP-level tests for server/semi_join_routes.py - cross-dataset
semi-join / filter pushdown's own new endpoint (/api/generate-semi-join-sql).
Mirrors test_combine_results.py's own conventions for this family of tests
(_GenaiHarness/_gemini_ok, duplicated locally rather than imported across
test modules - same established precedent those classes' own docstrings
already state).
"""
import json
import types as pytypes

from helpers import parse_translate_stream, write_database_presets_file, login_as


# --- _build_filter_context ---------------------------------------------------

def test_build_filter_context_renders_distinct_rows_with_the_filter_description(app_env):
    sj = app_env.semi_join_routes
    text, row_count, capped = sj._build_filter_context(
        "the hosting country and year",
        ["host_country", "year"],
        [{"host_country": "France", "year": 1900}, {"host_country": "USA", "year": 1904}],
        max_rows=200,
    )
    assert capped is False
    assert row_count == 2
    assert "the hosting country and year" in text
    assert "host_country='France'" in text
    assert "year=1900" in text
    assert "host_country='USA'" in text


def test_build_filter_context_dedupes_identical_rows(app_env):
    sj = app_env.semi_join_routes
    text, row_count, capped = sj._build_filter_context(
        "x", ["a"], [{"a": 1}, {"a": 1}, {"a": 2}], max_rows=200,
    )
    assert row_count == 2
    assert capped is False


def test_build_filter_context_handles_zero_driving_rows_without_falling_back_to_unfiltered(app_env):
    # See this function's own docstring: zero real rows is itself real
    # information (nothing matched), not a reason to answer the broader,
    # unfiltered question - the rendered text must say so explicitly,
    # distinctly from the "too many rows, give up filtering" case below.
    sj = app_env.semi_join_routes
    text, row_count, capped = sj._build_filter_context("the hosting country and year", [], [], max_rows=200)
    assert row_count == 0
    assert capped is False
    assert "NO matching rows" in text
    assert "the hosting country and year" in text


def test_build_filter_context_caps_out_and_signals_no_text_when_too_many_distinct_rows(app_env):
    sj = app_env.semi_join_routes
    rows = [{"a": i} for i in range(5)]
    text, row_count, capped = sj._build_filter_context("x", ["a"], rows, max_rows=3)
    assert text is None
    assert row_count == 5
    assert capped is True


# --- /api/generate-semi-join-sql - HTTP-level --------------------------------

def _presets_env(app_factory, tmp_path, extra_env=None):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-b", "name": "Marketing Postgres", "type": "postgres", "url": "postgresql://u:p@host-b:5432/b"},
    ])
    env = {"DATABASE_PRESETS_FILE": presets_path, "GEMINI_PRESET_KEYS": "fake-key-1"}
    env.update(extra_env or {})
    return app_factory(env=env)


class _GenaiHarness:
    """Local copy of test_combine_results.py's own _GenaiHarness (same
    shape, same single-threaded/single-queued-response trim) - see that
    class's own docstring."""

    def __init__(self):
        self.queue = []
        self.generate_calls = []

    def queue_response(self, resp):
        self.queue.append(resp)

    def queue_error(self, exc):
        self.queue.append(exc)

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


def _body(**overrides):
    body = {
        "prompt": "GDP of each country/year",
        "kind": "preset",
        "id": "pg-b",
        "filter_description": "the hosting country and year",
        "driving_result": {
            "columns": ["host_country", "year"],
            "rows": [{"host_country": "France", "year": 1900}, {"host_country": "USA", "year": 1904}],
        },
    }
    body.update(overrides)
    return body


def test_generate_semi_join_sql_requires_a_prompt(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/generate-semi-join-sql', json=_body(prompt=""))
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_generate_semi_join_sql_requires_a_valid_kind(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/generate-semi-join-sql', json=_body(kind="bogus"))
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_generate_semi_join_sql_requires_a_filter_description(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/generate-semi-join-sql', json=_body(filter_description=""))
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_generate_semi_join_sql_requires_a_driving_result_with_rows(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/generate-semi-join-sql', json=_body(driving_result={"columns": []}))
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_generate_semi_join_sql_rejects_an_unknown_connection(app_factory, tmp_path):
    env = _presets_env(app_factory, tmp_path)
    resp = env.client.post('/api/generate-semi-join-sql', json=_body(id="not-a-real-preset"))
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def _schema_fetch_by_url(mapping):
    def _fetch(descriptor):
        return mapping[descriptor.get("url")]
    return _fetch


def test_generate_semi_join_sql_succeeds_with_filter_applied(app_factory, tmp_path, monkeypatch):
    env = _presets_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-b:5432/b": "Table: gdp\ncountry TEXT\nyear INTEGER\nvalue NUMERIC\n",
    }))

    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(json.dumps({
        "sql": "SELECT country, year, value FROM gdp WHERE (country, year) IN (('France', 1900), ('USA', 1904));",
    })))

    resp = env.client.post('/api/generate-semi-join-sql', json=_body())
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['outcome'] == 'sql'
    assert "-- database: preset:pg-b (Marketing Postgres)" in data['sql']
    assert "WHERE (country, year) IN" in data['sql']
    assert data['filter_applied'] is True
    assert data['filter_row_count'] == 2
    assert data['filter_capped'] is False

    # Proves the real driving values actually reached the LLM call, not
    # just that the route returned something plausible.
    contents_text = str(harness.generate_calls[0]["contents"])
    assert "Filter context" in contents_text
    assert "France" in contents_text and "1900" in contents_text


def test_generate_semi_join_sql_falls_back_to_unfiltered_when_driving_result_exceeds_the_cap(
    app_factory, tmp_path, monkeypatch,
):
    # SEMI_JOIN_MAX_FILTER_ROWS is read at module-import time (it's the
    # default value of _build_filter_context's own `max_rows` parameter -
    # a plain post-import monkeypatch on the module attribute wouldn't
    # affect an already-bound default), so it has to be set via the env
    # BEFORE app_factory()/fresh_import() does that import, same as every
    # other env-derived constant this test suite exercises.
    env = _presets_env(app_factory, tmp_path, extra_env={"SEMI_JOIN_MAX_FILTER_ROWS": "1"})
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-b:5432/b": "Table: gdp\ncountry TEXT\nyear INTEGER\nvalue NUMERIC\n",
    }))

    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(json.dumps({"sql": "SELECT country, year, value FROM gdp;"})))

    resp = env.client.post('/api/generate-semi-join-sql', json=_body())
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['filter_applied'] is False
    assert data['filter_capped'] is True
    assert data['filter_row_count'] == 2

    # The capped-out driving rows never reached the LLM call at all - this
    # connection's own SQL was generated exactly like an ordinary,
    # independently-fetched connection, not a silently-truncated filter.
    contents_text = str(harness.generate_calls[0]["contents"])
    assert "Filter context" not in contents_text


def test_generate_semi_join_sql_tells_the_dependent_connection_about_zero_matching_driving_rows(
    app_factory, tmp_path, monkeypatch,
):
    env = _presets_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-b:5432/b": "Table: gdp\ncountry TEXT\nyear INTEGER\nvalue NUMERIC\n",
    }))

    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    harness.queue_response(_gemini_ok(json.dumps({"sql": "SELECT country, year, value FROM gdp WHERE 1=0;"})))

    resp = env.client.post('/api/generate-semi-join-sql', json=_body(
        driving_result={"columns": ["host_country", "year"], "rows": []},
    ))
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is True
    assert data['filter_applied'] is True
    assert data['filter_row_count'] == 0
    contents_text = str(harness.generate_calls[0]["contents"])
    assert "NO matching rows" in contents_text


def test_generate_semi_join_sql_surfaces_an_honest_failure_without_a_500(app_factory, tmp_path, monkeypatch):
    env = _presets_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@host-b:5432/b": "Table: gdp\ncountry TEXT\nyear INTEGER\nvalue NUMERIC\n",
    }))

    harness = _GenaiHarness()
    monkeypatch.setattr(env.translate_routes.genai, "Client", harness.make_client_class())
    # A plain, unrecognized exception (no `.code` the real GeminiProvider's
    # own classify_error/_gemini_error_code would match against any
    # retryable case) is treated as non-retryable - fails this one call
    # immediately, same as every other LLM-calling route in this app.
    harness.queue_error(RuntimeError("quota exceeded"))

    resp = env.client.post('/api/generate-semi-join-sql', json=_body())
    assert resp.status_code == 200
    _retry_events, data = parse_translate_stream(resp)
    assert data['success'] is False
    assert 'error' in data
