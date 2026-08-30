"""
The "in scope" connection set (multi-database question-answering - see
translate_routes.py's module docstring): /api/config's
in_scope_preset_ids/in_scope_custom_connection_keys GET fields, their
lazy derivation from the pre-existing single connection_id/is_custom
fields for a session that's never explicitly saved them, and the POST
validation config_routes.py applies (empty-set rejection,
MAX_IN_SCOPE_CONNECTIONS cap, silent-drop of unknown/stale references) -
modeled directly on test_config_model_selection.py and its
helpers.select_llm_provider pattern.
"""

from helpers import login_as, write_database_presets_file


def _two_preset_env(app_factory, tmp_path, extra_env=None):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
    ])
    env = {"DATABASE_PRESETS_FILE": presets_path}
    env.update(extra_env or {})
    return app_factory(env=env)


def test_get_config_exposes_in_scope_mode_default(app_env):
    data = app_env.client.get('/api/config').get_json()
    assert data["in_scope_mode"] == "single"


def test_post_config_persists_in_scope_mode_all_and_round_trips(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"in_scope_mode": "all"})
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_mode"] == "all"


def test_post_config_invalid_in_scope_mode_is_silently_ignored(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={"in_scope_mode": "all"})

    resp = env.client.post('/api/config', json={"in_scope_mode": "some-bogus-value"})
    assert resp.status_code == 200

    # An unrecognized in_scope_mode is treated as "nothing to save" (same
    # leniency an unrecognized llm_provider name gets) - it must not clear
    # the previously-saved "all" back down to the default.
    data = env.client.get('/api/config').get_json()
    assert data["in_scope_mode"] == "all"


def test_in_scope_mode_save_is_independent_of_in_scope_arrays(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })

    # Switching in_scope_mode alone (as the client's "single" radio pick
    # does - see triggerConfigSave()) must not be rejected as an empty
    # in-scope save, and must leave the previously-saved arrays alone.
    resp = env.client.post('/api/config', json={"in_scope_mode": "single"})
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_mode"] == "single"
    assert data["in_scope_preset_ids"] == ["pg-a", "pg-b"]


def test_get_config_exposes_max_in_scope_connections_default(app_env):
    data = app_env.client.get('/api/config').get_json()
    assert data["max_in_scope_connections"] == 20


def test_get_config_max_in_scope_connections_reads_env_override(app_factory):
    env = app_factory(env={"MAX_IN_SCOPE_CONNECTIONS": "3"})
    data = env.client.get('/api/config').get_json()
    assert data["max_in_scope_connections"] == 3


def test_never_touched_session_displays_the_resolved_default_as_in_scope(app_env):
    # Nothing has ever been explicitly selected (connection_id == "") -
    # state_store.py's _lazy_derive_in_scope itself derives two empty
    # lists (the same "fall back to the app default" convention
    # resolve_active_descriptor already uses for a blank connection_id),
    # but config_routes.py's GET handler layers a display-only fallback
    # on top - mirroring active_preset_id's own "or DEFAULT_PRESET_ID"
    # convention - so the connection picker's checkboxes still show the
    # session's actual effective default connection as checked, rather
    # than nothing at all.
    data = app_env.client.get('/api/config').get_json()
    assert data["in_scope_preset_ids"] == ["postgres+Default DB"]
    assert data["in_scope_custom_connection_keys"] == []


def test_session_with_explicit_preset_selection_derives_single_entry_in_scope(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={"preset_id": "pg-a"})

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_preset_ids"] == ["pg-a"]
    assert data["in_scope_custom_connection_keys"] == []


def test_session_with_active_custom_connection_derives_single_entry_in_scope(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "My DB", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    key = data["active_custom_connection_key"]
    assert key
    assert data["in_scope_preset_ids"] == []
    assert data["in_scope_custom_connection_keys"] == [key]


def test_post_config_persists_valid_in_scope_preset_set_and_round_trips(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_preset_ids"] == ["pg-a", "pg-b"]
    assert data["in_scope_custom_connection_keys"] == []


def test_post_config_rejects_empty_in_scope_set(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={"preset_id": "pg-a"})

    resp = env.client.post('/api/config', json={
        "in_scope_preset_ids": [],
        "in_scope_custom_connection_keys": [],
    })
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "At least one database connection must be in scope."

    # Rejected outright - the session's actual saved scope (from the
    # preset_id save just above) stays in place, not silently cleared.
    data = env.client.get('/api/config').get_json()
    assert data["in_scope_preset_ids"] == ["pg-a"]


def test_post_config_rejects_set_over_configured_max(app_factory, tmp_path):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": f"pg-{i}", "name": f"Postgres {i}", "type": "postgres", "url": f"postgresql://u:p@h/db{i}"}
        for i in range(3)
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": presets_path, "MAX_IN_SCOPE_CONNECTIONS": "2"})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-0", "pg-1", "pg-2"],
        "in_scope_custom_connection_keys": [],
    })
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "At most 2 database connections may be in scope at once."


def test_post_config_silently_drops_unknown_preset_id(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "not-a-real-preset-id"],
        "in_scope_custom_connection_keys": [],
    })
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_preset_ids"] == ["pg-a"]


def test_post_config_silently_drops_stale_custom_connection_key(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a"],
        "in_scope_custom_connection_keys": ["never-saved-key"],
    })
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data["in_scope_custom_connection_keys"] == []


def test_post_config_accepts_custom_key_added_in_the_same_request(app_env):
    # A connection added AND marked in-scope in the same Save must not be
    # spuriously dropped for "not existing yet" - see config_routes.py's
    # comment on reference_custom_databases/merged_custom_databases.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db",
        "database_name": "My DB", "is_custom": True,
        "custom_databases": [
            {"name": "My DB", "type": "postgres", "url": "postgresql://u:p@h/db", "config": {}},
        ],
    })
    assert resp.status_code == 200
    key = app_env.client.get('/api/config').get_json()["active_custom_connection_key"]
    assert key

    resp2 = app_env.client.post('/api/config', json={
        "in_scope_preset_ids": [],
        "in_scope_custom_connection_keys": [key],
        "custom_databases": [
            {"name": "My DB", "type": "postgres", "url": "postgresql://u:p@h/db", "config": {}},
        ],
    })
    assert resp2.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data["in_scope_custom_connection_keys"] == [key]


def test_in_scope_save_is_independent_of_llm_provider_and_auto_sql_execute(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")

    env.client.post('/api/config', json={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"})
    env.client.post('/api/config', json={"auto_sql_execute": False})
    env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })

    data = env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "anthropic"
    assert data["active_llm_model"] == "claude-sonnet-5"
    assert data["auto_sql_execute"] is False
    assert data["in_scope_preset_ids"] == ["pg-a", "pg-b"]


def test_in_scope_only_save_does_not_touch_llm_provider_or_auto_sql_execute(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={"llm_provider": "openai", "llm_model": "gpt-5.6-luna"})

    env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })

    data = env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "openai"
    assert data["active_llm_model"] == "gpt-5.6-luna"
    assert data["in_scope_preset_ids"] == ["pg-a", "pg-b"]


def test_in_scope_set_is_isolated_per_user(app_factory, tmp_path):
    env = _two_preset_env(app_factory, tmp_path)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={
        "in_scope_preset_ids": ["pg-a", "pg-b"],
        "in_scope_custom_connection_keys": [],
    })

    login_as(env.client, "bob@example.com")
    data = env.client.get('/api/config').get_json()
    # bob never saved a scope of his own - sees his own lazily-derived
    # default (the first preset, display-resolved the same way
    # active_preset_id is - see config_routes.py's GET handler), not
    # alice's saved multi-preset scope.
    assert data["in_scope_preset_ids"] == ["pg-a"]
    assert data["in_scope_custom_connection_keys"] == []
