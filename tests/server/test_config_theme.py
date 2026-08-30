"""
Theme (Preferences modal's color-scheme choice), exercised through
/api/config: the GET response's "theme" field the Preferences modal/
client.js's fetchBackendConfig() reconciles against document.documentElement's
data-theme attribute, and the POST handling that persists a user's choice -
see state_store.py's "theme" session field.

Mirrors test_config_model_selection.py in spirit (an independent,
Preferences-modal-triggered session preference, save-then-reload round trip,
isolation per user, independence from auto_sql_execute) but for the theme
feature specifically. Unlike llm_provider/llm_model, there's no fleet-wide
env-configured default to fall back to here - a session that's never
explicitly saved a theme reports back "" ("nothing explicitly saved yet"),
and client.js is what decides to leave the client's own current/localStorage
value alone in that case (see fetchBackendConfig()'s own comment).
"""

from helpers import login_as


def test_get_config_default_theme_is_blank(app_env):
    # No session selection has ever been saved - "" ("nothing explicitly
    # saved yet"), not a hardcoded "dark"/"light" - see state_store.py's
    # get_session docstring on why this field doesn't work like
    # auto_sql_execute's baked-in default.
    data = app_env.client.get('/api/config').get_json()
    assert data["theme"] == ""


def test_post_config_persists_valid_theme_choice(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={"theme": "light"})
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data["theme"] == "light"


def test_post_config_theme_choice_is_isolated_per_user(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"theme": "light"})

    login_as(app_env.client, "bob@example.com")
    data = app_env.client.get('/api/config').get_json()
    # bob never saved a choice - still blank, not alice's saved "light".
    assert data["theme"] == ""


def test_post_config_rejects_invalid_theme_value(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={"theme": "solarized"})
    assert resp.status_code == 200  # silently ignored, not a hard error
    data = app_env.client.get('/api/config').get_json()
    assert data["theme"] == ""


def test_post_config_theme_survives_alongside_auto_sql_execute_toggle(app_env):
    # theme is independent of auto_sql_execute (same "a request can touch
    # one, the other, both, or neither" reasoning config_routes.py already
    # documents for auto_sql_execute itself) - a request touching only
    # auto_sql_execute must never clobber a previously-saved theme, and
    # vice versa.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"theme": "light"})
    app_env.client.post('/api/config', json={"auto_sql_execute": False})

    data = app_env.client.get('/api/config').get_json()
    assert data["theme"] == "light"
    assert data["auto_sql_execute"] is False


def test_post_config_auto_sql_execute_only_save_does_not_touch_theme(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"theme": "light"})
    app_env.client.post('/api/config', json={"auto_sql_execute": False})
    app_env.client.post('/api/config', json={"auto_sql_execute": True})

    data = app_env.client.get('/api/config').get_json()
    assert data["theme"] == "light"  # untouched by either auto_sql_execute save


def test_post_config_theme_only_save_does_not_touch_database_connection(app_env):
    login_as(app_env.client, "alice@example.com")
    before = app_env.client.get('/api/config').get_json()

    app_env.client.post('/api/config', json={"theme": "light"})

    after = app_env.client.get('/api/config').get_json()
    assert after["active_database_url"] == before["active_database_url"]
    assert after["active_is_custom"] == before["active_is_custom"]


def test_post_config_theme_only_save_does_not_touch_model_selection(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"})

    app_env.client.post('/api/config', json={"theme": "light"})

    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "anthropic"
    assert data["active_llm_model"] == "claude-sonnet-5"
    assert data["theme"] == "light"
