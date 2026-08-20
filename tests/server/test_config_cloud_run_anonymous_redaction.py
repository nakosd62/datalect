"""
config_routes.py's anonymous-visitor redaction on Cloud Run specifically:
IS_CLOUD_RUN (K_SERVICE set) is a stricter gate than just "auth is enabled"
(GOOGLE_CLIENT_ID set) - it's the actual Cloud Run detection, and it's what
config_routes.py's GET response redaction branches key off of (never
sending an admin preset's real connection string/credentials, or another
identity's active connection string, to an anonymous visitor). Anonymous
visitors CAN now save and see their own self-supplied custom connections
(see the "can_save_and_see_their_own_custom_connection" test below) - that
redaction was always about admin/other-user secrets, not a blanket "no
custom connections for anonymous users" rule; see config_routes.py's
handle_config for the full reasoning. Also covers the flip side of that
same anonymity: two DIFFERENT anonymous visitors (two browser sessions)
must not see or affect each other's active DB / auto-execute / custom
connections either - see auth.py's ANONYMOUS_USER_ID_PREFIX and the
"...dont_collide..." tests below. Requires mock_firestore, since
IS_CLOUD_RUN=True with no working Firestore client is a hard startup
RuntimeError by design (see app_config.py's "Halting startup to prevent
ephemeral SQLite fallback").
"""

import pytest

from helpers import login_as, write_database_presets_file


@pytest.fixture
def cloud_run_env(tmp_path):
    """The common Cloud Run env every test below starts from - a single
    "Demo" Postgres preset written to its own file under tmp_path (each
    test gets a fresh tmp_path, so a fresh file), since DATABASE_PRESETS_FILE
    points at a path rather than holding the JSON inline."""
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Demo", "url": "postgresql://realuser:realpass@realhost/realdb"},
    ])
    return {
        "K_SERVICE": "ydyl-service",
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": path,
    }


def test_cloud_run_starts_up_successfully_with_mocked_firestore(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    assert env.app_config.IS_CLOUD_RUN is True
    assert type(env.app_config.state_store).__name__ == "FirestoreStateStore"


def test_anonymous_visitor_never_receives_real_preset_connection_strings(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    data = env.client.get('/api/config').get_json()
    assert data['configured_databases'] == [{"name": "Demo", "type": "postgres"}]
    for db in data['configured_databases']:
        assert "url" not in db


def test_anonymous_visitor_active_connection_string_is_empty(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    data = env.client.get('/api/config').get_json()
    assert data['active_database_url'] == ""
    assert data['active_database_type'] == ""
    assert data['default_database_url'] == ""


def test_anonymous_visitor_selects_preset_by_index_not_url(app_factory, cloud_run_env, tmp_path):
    ab_path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "A", "url": "postgresql://real/a"},
        {"type": "postgres", "name": "B", "url": "postgresql://real/b"},
    ], filename="ab_presets.json")
    env = app_factory(env={
        **cloud_run_env,
        "DATABASE_PRESETS_FILE": ab_path,
    }, mock_firestore=True)
    resp = env.client.post('/api/config', json={"preset_index": 1})
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['database_name'] == "B"
    assert data['active_database_url'] == ""  # still never exposed


def test_two_concurrent_anonymous_visitors_dont_collide_on_active_preset(app_factory, cloud_run_env, tmp_path):
    # The regression this whole file's per-session anonymous identity
    # exists to fix (see auth.py's ANONYMOUS_USER_ID_PREFIX comment):
    # before, every anonymous visitor shared one literal "anonymous"
    # identity, so ALL anonymous requests read/wrote the exact same
    # state_store row - one visitor's preset selection silently became
    # every other anonymous visitor's active DB too. Two separate test
    # clients against the same app instance simulate two different
    # browsers (each gets its own cookie jar, so its own
    # crbot_session_id -> its own anonymous:<session_id> identity).
    ab_path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "A", "url": "postgresql://real/a"},
        {"type": "postgres", "name": "B", "url": "postgresql://real/b"},
    ], filename="ab_presets.json")
    env = app_factory(env={**cloud_run_env, "DATABASE_PRESETS_FILE": ab_path}, mock_firestore=True)
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    resp_one = browser_one.post('/api/config', json={"preset_index": 0})
    resp_two = browser_two.post('/api/config', json={"preset_index": 1})
    assert resp_one.status_code == 200 and resp_two.status_code == 200

    # Each browser must still see ITS OWN selection, not whichever request
    # happened last.
    assert browser_one.get('/api/config').get_json()['database_name'] == "A"
    assert browser_two.get('/api/config').get_json()['database_name'] == "B"

    # And a fresh, cookie-less request (a third, brand-new anonymous
    # visitor) sees neither - it falls back to the untouched first preset,
    # proving there's no single shared "the" active anonymous connection
    # left over from either browser above.
    browser_three = env.app_config.app.test_client()
    assert browser_three.get('/api/config').get_json()['database_name'] == "A"


def test_two_concurrent_anonymous_visitors_dont_collide_on_auto_sql_execute(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    browser_one.post('/api/config', json={"auto_sql_execute": False})
    browser_two.post('/api/config', json={"auto_sql_execute": True})

    assert browser_one.get('/api/config').get_json()['auto_sql_execute'] is False
    assert browser_two.get('/api/config').get_json()['auto_sql_execute'] is True


def test_anonymous_visitor_can_save_and_see_their_own_custom_connection(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['authenticated'] is False
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['name'] == "My DB"
    # Unlike a redacted preset, the visitor's OWN custom connection is not a
    # secret from them - the real URL/type round-trip.
    assert data['active_is_custom'] is True
    assert data['active_database_url'] == "postgresql://u:p@h/mydb"
    assert data['active_database_type'] == "postgres"
    # Other admin presets stay fully redacted regardless - this is about
    # THIS visitor's own connection, not a blanket un-redaction.
    for db in data['configured_databases']:
        assert "url" not in db


def test_anonymous_visitor_on_a_preset_still_gets_full_redaction_even_with_a_saved_custom_connection(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    # Save a custom connection, then switch back to the preset.
    env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    resp = env.client.post('/api/config', json={"preset_index": 0})
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    assert data['active_is_custom'] is False
    assert data['active_database_url'] == ""
    assert data['active_database_type'] == ""
    assert data['database_name'] == "Demo"
    # The previously-saved custom connection is still listed (never lost),
    # just no longer the active one.
    assert len(data['custom_databases']) == 1


def test_two_anonymous_visitors_dont_collide_on_custom_connections(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    browser_one.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/one",
        "database_name": "Browser One DB", "is_custom": True,
    })

    assert browser_one.get('/api/config').get_json()['custom_databases'][0]['name'] == "Browser One DB"
    assert browser_two.get('/api/config').get_json()['custom_databases'] == []


def test_authenticated_user_on_cloud_run_gets_real_connection_details(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['authenticated'] is True
    assert data['configured_databases'][0].get("url") == "postgresql://realuser:realpass@realhost/realdb"


def test_authenticated_user_on_cloud_run_can_save_a_custom_connection(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    resp = env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/mydb",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['name'] == "My DB"


def test_data_is_isolated_per_user_via_firestore(app_factory, cloud_run_env):
    env = app_factory(env=cloud_run_env, mock_firestore=True)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/alice-db",
        "database_name": "Alice's DB", "is_custom": True,
    })
    login_as(env.client, "bob@example.com")
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'] == []  # bob doesn't see alice's connection
