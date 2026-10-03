"""
The dataset-group Schema Viewer's "Overview" tree entry: db.py's
_generate_and_cache_group_schema_overview()/get_cached_group_overview(),
and the two routes that read/trigger it - GET /api/schema/group's new
top-level 'overview' field, and POST /api/config/refresh-schema's new
kind="group" branch (config_routes.py's handle_get_group_schema()/
handle_refresh_schema()).

Mirrors test_group_schema_summary.py's own `_two_preset_one_group_env()`
preset/group fixture and test_connection_router.py's own `_FakeProvider`
LLM-call double (duplicated here rather than imported across test
modules - no shared load-order dependency between test files, same
convention test_group_schema_summary.py's own module docstring already
documents for its `_schema_fetch_by_url` helper).
"""

from helpers import login_as, write_database_presets_file


def _two_preset_one_group_env(app_factory, tmp_path, extra_env=None):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
        {"id": "grp-ab", "name": "AB Group", "type": "dataset_group", "dataset_list": ["pg-a", "pg-b"]},
    ])
    env = {"DATABASE_PRESETS_FILE": presets_path}
    env.update(extra_env or {})
    return app_factory(env=env)


def _schema_fetch_by_url(mapping):
    def _fetch(descriptor, deep=True):
        return mapping[descriptor.get("url")]
    return _fetch


class _FakeProvider:
    """Same interface test_connection_router.py's own _FakeProvider
    exercises (build_llm_input/call/make_client/pick_api_key) - the one
    real seam _generate_and_cache_group_schema_overview() calls through,
    so a real LLM round-trip never has to run in this test file either."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def build_llm_input(self, history, schema_block, new_prompt_content):
        return schema_block

    def call(self, client, model, llm_input, system_instruction):
        self.calls.append({"client": client, "llm_input": llm_input, "system_instruction": system_instruction})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    def make_client(self, api_key):
        return f"client-for-{api_key}"

    def pick_api_key(self, exclude=None):
        return "fake-key"


def _patch_llm(monkeypatch, db_module, provider, api_key="fake-key"):
    monkeypatch.setattr(
        db_module, "_resolve_overview_llm_call",
        lambda user_id: (provider, "fake-model", api_key),
    )


def test_generates_and_caches_a_group_overview_from_member_schemas(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))
    # pg-a already has its own single-connection overview cached - this
    # should get folded into the group prompt's input as extra context
    # (see _build_group_overview_schema_block()'s own docstring), never
    # used to skip calling the LLM.
    import schema_cache
    schema_cache.set_overview(
        db_module.get_conn_identifier({"type": "postgres", "url": "postgresql://u:p@h/a"}),
        {"prose": "Sales deals.", "questions": ["q"], "generated_at": "2024-01-01T00:00:00+00:00"},
    )

    provider = _FakeProvider(['{"prose": "A sales + marketing group.", "questions": ["q1", "q2", "q3"]}'])
    _patch_llm(monkeypatch, db_module, provider)

    assert db_module.get_cached_group_overview("grp-ab") is None

    ok = db_module._generate_and_cache_group_schema_overview("grp-ab", "alice@example.com")
    assert ok is True
    assert len(provider.calls) == 1

    # Both members' table names show up in the prompt sent to the LLM,
    # and pg-a's already-cached overview prose is folded in too.
    sent = provider.calls[0]["llm_input"]
    assert "deals" in sent
    assert "campaigns" in sent
    assert "Sales deals." in sent

    overview = db_module.get_cached_group_overview("grp-ab")
    assert overview["prose"] == "A sales + marketing group."
    assert overview["questions"] == ["q1", "q2", "q3"]
    assert "generated_at" in overview


def test_a_member_with_no_fetchable_schema_is_still_listed_by_name_not_dropped(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module

    def _fetch(descriptor, deep=True):
        if descriptor.get("url") == "postgresql://u:p@h/a":
            return db_module._SCHEMA_FETCH_FAILED
        return "Table: campaigns\n  id integer NOT NULL\n"

    monkeypatch.setattr(db_module, "_fetch_database_schema", _fetch)
    provider = _FakeProvider(['{"prose": "p", "questions": []}'])
    _patch_llm(monkeypatch, db_module, provider)

    ok = db_module._generate_and_cache_group_schema_overview("grp-ab", "alice@example.com")
    assert ok is True
    sent = provider.calls[0]["llm_input"]
    assert "Postgres A" in sent
    assert "schema unavailable" in sent


def test_returns_false_without_calling_the_llm_when_no_api_key_is_configured(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))
    provider = _FakeProvider([])
    _patch_llm(monkeypatch, db_module, provider, api_key=None)

    ok = db_module._generate_and_cache_group_schema_overview("grp-ab", "alice@example.com")
    assert ok is False
    assert len(provider.calls) == 0
    assert db_module.get_cached_group_overview("grp-ab") is None


def test_returns_false_and_caches_nothing_when_the_llm_response_does_not_parse(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))
    provider = _FakeProvider(["not json at all"])
    _patch_llm(monkeypatch, db_module, provider)

    ok = db_module._generate_and_cache_group_schema_overview("grp-ab", "alice@example.com")
    assert ok is False
    assert db_module.get_cached_group_overview("grp-ab") is None


def test_returns_false_for_an_unknown_group_id_without_touching_the_llm(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    provider = _FakeProvider([])
    _patch_llm(monkeypatch, db_module, provider)

    ok = db_module._generate_and_cache_group_schema_overview("not-a-real-group", "alice@example.com")
    assert ok is False
    assert len(provider.calls) == 0


def test_get_group_schema_route_includes_null_overview_before_any_generation(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))

    resp = env.client.get('/api/schema/group?id=grp-ab')
    assert resp.status_code == 200
    assert resp.get_json()["overview"] is None
    # Each member's own row also carries its own (here, not-yet-generated)
    # overview field - see build_group_schema_summaries()'s own docstring.
    assert resp.get_json()["datasets"][0]["overview"] is None


def test_get_group_schema_route_surfaces_a_cached_group_overview(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))
    provider = _FakeProvider(['{"prose": "A group.", "questions": ["q1"]}'])
    _patch_llm(monkeypatch, db_module, provider)
    assert db_module._generate_and_cache_group_schema_overview("grp-ab", "alice@example.com") is True

    resp = env.client.get('/api/schema/group?id=grp-ab')
    assert resp.status_code == 200
    overview = resp.get_json()["overview"]
    assert overview["prose"] == "A group."
    assert overview["questions"] == ["q1"]


def test_refresh_schema_route_group_kind_regenerates_the_group_overview(app_env, monkeypatch):
    calls = []

    def _fake_generate(group_id, user_id):
        calls.append((group_id, user_id))
        return True

    monkeypatch.setattr(app_env.config_routes, "_generate_and_cache_group_schema_overview", _fake_generate)
    monkeypatch.setattr(
        app_env.config_routes, "CONFIGURED_DB_GROUPS",
        [{"id": "grp-ab", "name": "AB Group", "dataset_list": ["pg-a", "pg-b"]}],
    )
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "grp-ab"})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert calls == [("grp-ab", "alice@example.com")]


def test_refresh_schema_route_group_kind_404s_for_an_unknown_group(app_env, monkeypatch):
    monkeypatch.setattr(app_env.config_routes, "CONFIGURED_DB_GROUPS", [])
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "not-a-real-group"})

    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_refresh_schema_route_group_kind_502s_when_generation_fails(app_env, monkeypatch):
    monkeypatch.setattr(app_env.config_routes, "CONFIGURED_DB_GROUPS", [{"id": "grp-ab", "name": "AB Group", "dataset_list": []}])
    monkeypatch.setattr(app_env.config_routes, "_generate_and_cache_group_schema_overview", lambda group_id, user_id: False)
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": "grp-ab"})

    assert resp.status_code == 502
    assert resp.get_json()["success"] is False


def test_refresh_schema_route_group_kind_missing_id_returns_400(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config/refresh-schema', json={"kind": "group", "id": ""})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
