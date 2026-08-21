"""
/api/config's active_connection_missing / active_connection_missing_message
fields - surfaced when a session's stored connection_id (a preset's id, or
a saved custom connection's connection_key) no longer resolves to anything
real, because an admin removed/renamed the preset or the user deleted the
custom connection (see db.py's resolve_active_descriptor and
config_routes.py's module docstring / plan). Per the confirmed behavior:
querying/translating still silently falls back to the default connection
(no hard error anywhere - see test_translate_routes.py/test_execute_routes.py
for that), but the GET response here must clearly flag it so the config
modal can warn the user instead of quietly showing the default as if it
were what they'd actually picked. The session's connection_id is left
untouched when this happens (not auto-cleared) - if the preset/connection
reappears, it resolves correctly again on the next request, which is why
none of these tests ever re-POST to "fix" the session; they only mutate
CONFIGURED_DBS/db_connections out from under an already-selected session.
"""

from helpers import login_as, write_database_presets_file


def test_brand_new_session_is_never_flagged_missing(app_env):
    data = app_env.client.get('/api/config').get_json()
    assert data['active_connection_missing'] is False
    assert data['active_connection_missing_message'] == ""


def test_removed_preset_shows_missing_flag_and_falls_back_to_default(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Default DB", "url": "postgresql://demo:pw@host/default"},
        {"type": "postgres", "name": "Removable", "url": "postgresql://demo:pw@host/removable"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "postgres+Removable"})
    assert resp.status_code == 200
    before = env.client.get('/api/config').get_json()
    assert before['active_connection_missing'] is False
    assert before['active_preset_id'] == "postgres+Removable"

    # The admin removes "Removable" from DATABASE_PRESETS_FILE (e.g. the
    # next deploy) - simulated here by mutating the live CONFIGURED_DBS
    # list in place (config_routes.py/db.py both imported a reference to
    # this exact list object at import time, so mutating it - as opposed to
    # reassigning app_config.CONFIGURED_DBS to a new list - is visible to
    # both without re-importing anything).
    env.config_routes.CONFIGURED_DBS[:] = [
        db for db in env.config_routes.CONFIGURED_DBS if db["name"] != "Removable"
    ]

    after = env.client.get('/api/config').get_json()
    assert after['active_connection_missing'] is True
    assert after['active_connection_missing_message']
    assert "no longer available" in after['active_connection_missing_message'].lower()
    # The actual connection served falls back to the (still-configured)
    # default preset, not an error - but active_preset_id keeps reporting
    # the session's own stored selection as-is (untouched, not
    # auto-cleared - see module docstring), so the UI can still show which
    # (now-missing) preset the user had picked alongside the warning.
    # active_database_url stays blank either way, though - the fallback is
    # still a preset connection, and a preset's real connection string is
    # always redacted (see config_routes.py's handle_config), missing or not.
    assert after['active_database_url'] == ""
    assert after['active_preset_id'] == "postgres+Removable"

    # The session's connection_id itself is left untouched (not
    # auto-cleared) - if "Removable" comes back, it resolves correctly
    # again without the user having to re-select it.
    env.config_routes.CONFIGURED_DBS.append(
        {"id": "postgres+Removable", "name": "Removable", "type": "postgres",
         "url": "postgresql://demo:pw@host/removable"},
    )
    restored = env.client.get('/api/config').get_json()
    assert restored['active_connection_missing'] is False
    assert restored['active_preset_id'] == "postgres+Removable"
    assert restored['active_database_url'] == ""


def test_deleted_custom_connection_shows_missing_flag_and_falls_back_to_default(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    before = app_env.client.get('/api/config').get_json()
    assert before['active_connection_missing'] is False
    assert before['active_is_custom'] is True

    # The user deletes their only saved custom connection (e.g. via the
    # config modal's remove control) - simulated directly against
    # state_store, same as config_routes.py's own custom_databases-replace
    # path would do.
    app_env.app_config.state_store.set_db_connections(
        "alice@example.com", None, None, None, custom_databases=[],
    )

    after = app_env.client.get('/api/config').get_json()
    assert after['active_connection_missing'] is True
    assert after['active_connection_missing_message']
    # is_custom itself is left untouched (part of the untouched connection_id
    # reference - see module docstring) even though it no longer resolves;
    # the actual connection served is still the default.
    assert after['active_is_custom'] is True
    assert after['active_database_url'] == app_env.app_config.DEFAULT_CONN
