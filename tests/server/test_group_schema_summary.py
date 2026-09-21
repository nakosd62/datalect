"""
The dataset-group Schema Viewer's own server-side pieces: db.py's
build_group_schema_summaries() and its thin route wrapper, GET
/api/schema/group (config_routes.py's handle_get_group_schema()) - powers
webClient's group-schema table (client.js's openGroupSchemaViewer()/
loadGroupSchemaViewer()), shown when the "i" icon on the dataset badge is
clicked while a dataset group (not a single preset/custom connection) is
the selected option.

Deliberately mirrors test_connection_router.py's own
_schema_fetch_by_url()/monkeypatch-db_module._fetch_database_schema
pattern for controlling each member's schema text without a real database
connection - build_group_schema_summaries() calls the same
get_database_schema() (deep=True) real /api/translate calls and GET
/api/schema already use, so this is the one real seam to fake underneath
it (see backends/base.py's parse_dataset_size_line()/
SCHEMA_SIZE_CHARS_PER_TOKEN for exactly how "data_size"/"schema_size_
tokens" are derived from whatever schema text comes back).
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
    """Same shape as test_connection_router.py's own helper of this name -
    duplicated rather than imported across test modules (no shared load-
    order dependency between the two files)."""
    def _fetch(descriptor, deep=True):
        return mapping[descriptor.get("url")]
    return _fetch


def test_build_group_schema_summaries_returns_one_row_per_member_in_dataset_list_order(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n\nEstimated dataset size: ~2.4 GB",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))

    summary = db_module.build_group_schema_summaries("grp-ab", "alice@example.com")
    assert summary["id"] == "grp-ab"
    assert summary["name"] == "AB Group"
    assert [d["id"] for d in summary["datasets"]] == ["pg-a", "pg-b"]

    pg_a, pg_b = summary["datasets"]
    assert pg_a["name"] == "Postgres A"
    assert pg_a["type"] == "PostgreSQL"
    assert pg_a["data_size"] == "~2.4 GB"
    assert pg_a["available"] is True
    # len("Table: deals\n  id integer NOT NULL\n\nEstimated dataset size: ~2.4 GB") / 4,
    # quantized up to the nearest 100 (see backends/base.py's
    # quantize_schema_size_tokens()/SCHEMA_SIZE_TOKEN_QUANTUM).
    import math
    assert pg_a["schema_size_tokens"] == 100 * math.ceil(len(
        "Table: deals\n  id integer NOT NULL\n\nEstimated dataset size: ~2.4 GB"
    ) / 4 / 100)

    # pg-b's schema text has no "Estimated dataset size" line at all (no
    # cheap size source, or the dialect simply never appended one here) -
    # data_size is None, never a misleading '' or 0.
    assert pg_b["data_size"] is None
    assert pg_b["available"] is True
    assert pg_b["schema_size_tokens"] == 100 * math.ceil(len("Table: campaigns\n  id integer NOT NULL\n") / 4 / 100)


def test_build_group_schema_summaries_marks_a_failed_member_unavailable_without_failing_the_whole_group(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module

    def _fetch(descriptor, deep=True):
        if descriptor.get("url") == "postgresql://u:p@h/a":
            return db_module._SCHEMA_FETCH_FAILED
        return "Table: campaigns\n  id integer NOT NULL\n"

    monkeypatch.setattr(db_module, "_fetch_database_schema", _fetch)

    summary = db_module.build_group_schema_summaries("grp-ab", "alice@example.com")
    pg_a, pg_b = summary["datasets"]
    assert pg_a["available"] is False
    assert pg_a["data_size"] is None
    assert pg_a["schema_size_tokens"] is None
    # The other member is unaffected by pg-a's own failure.
    assert pg_b["available"] is True
    assert pg_b["schema_size_tokens"] is not None


def test_build_group_schema_summaries_returns_none_for_an_unknown_group_id(app_factory, tmp_path):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    assert db_module.build_group_schema_summaries("not-a-real-group", "alice@example.com") is None


def test_get_group_schema_route_returns_the_summary_shape(app_factory, tmp_path, monkeypatch):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    import db as db_module
    monkeypatch.setattr(db_module, "_fetch_database_schema", _schema_fetch_by_url({
        "postgresql://u:p@h/a": "Table: deals\n  id integer NOT NULL\n\nEstimated dataset size: ~1.23M rows",
        "postgresql://u:p@h/b": "Table: campaigns\n  id integer NOT NULL\n",
    }))

    resp = env.client.get('/api/schema/group?id=grp-ab')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["kind"] == "group"
    assert data["id"] == "grp-ab"
    assert data["name"] == "AB Group"
    assert [d["id"] for d in data["datasets"]] == ["pg-a", "pg-b"]
    assert data["datasets"][0]["data_size"] == "~1.23M rows"
    # The full entries/tree a single-connection GET /api/schema returns is
    # deliberately NOT part of this response at all - just the four
    # lightweight facts the group table shows.
    assert "entries" not in data["datasets"][0]


def test_get_group_schema_route_404s_for_an_unknown_group_id(app_factory, tmp_path):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.get('/api/schema/group?id=not-a-real-group')
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_get_group_schema_route_400s_when_id_is_missing(app_factory, tmp_path):
    env = _two_preset_one_group_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.get('/api/schema/group')
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
