"""
LLM model-selection UI, exercised through /api/config: the GET response's
llm_providers/active_llm_provider/active_llm_model fields the model
badge/modal in webClient/client.js render from, and the POST handling that
persists a user's choice - see state_store.py's llm_provider/llm_model
session fields and translate_routes.py's list_llm_providers_info().

Mirrors the existing auto_sql_execute persistence tests in spirit (an
independent, badge-triggered session preference, save-then-reload round
trip, isolation per user) but for the model-selection feature specifically.
"""

from helpers import login_as, select_llm_provider


def test_get_config_exposes_llm_providers_organized_by_provider(app_env):
    data = app_env.client.get('/api/config').get_json()
    assert [p["name"] for p in data["llm_providers"]] == ["google", "anthropic", "openai"]
    google_info = data["llm_providers"][0]
    assert google_info["default_model"] == "gemini-3.6-flash"
    assert google_info["preset_models"] == ["gemini-3.6-flash"]


def test_get_config_default_active_model_matches_hardcoded_default(app_env):
    # No session selection has ever been saved - falls back to this app's
    # one hardcoded default (Google/gemini-3.6-flash - see
    # get_llm_provider()'s docstring), exactly the values a fresh install
    # with zero configuration would use.
    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "google"
    assert data["active_llm_model"] == "gemini-3.6-flash"


def test_get_config_active_model_reflects_persisted_provider_choice(app_factory):
    # Unlike the POST-round-trip tests below, this seeds the session
    # directly (bypassing the POST validation layer) to confirm GET's
    # resolution logic itself picks up whatever's actually persisted -
    # there's no env var anymore that could independently override which
    # provider is "active" fleet-wide (see translate_routes.py's module
    # docstring on why LLM_PROVIDER was removed).
    env = app_factory()
    select_llm_provider(env, "anthropic")
    data = env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "anthropic"
    assert data["active_llm_model"] == "claude-sonnet-5"


def test_post_config_persists_valid_model_selection(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "llm_provider": "anthropic", "llm_model": "claude-sonnet-5",
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "anthropic"
    assert data["active_llm_model"] == "claude-sonnet-5"


def test_post_config_model_selection_is_isolated_per_user(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"})

    login_as(app_env.client, "bob@example.com")
    data = app_env.client.get('/api/config').get_json()
    # bob never saved a selection - still sees the fleet-wide default, not
    # alice's saved choice.
    assert data["active_llm_provider"] == "google"
    assert data["active_llm_model"] == "gemini-3.6-flash"


def test_post_config_rejects_unregistered_provider_name(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "llm_provider": "not-a-real-provider", "llm_model": "whatever",
    })
    assert resp.status_code == 200  # silently ignored, not a hard error
    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "google"
    assert data["active_llm_model"] == "gemini-3.6-flash"


def test_post_config_rejects_stale_pre_rename_provider_name(app_env):
    """A provider name valid under this app's OLD labels ("gemini"/
    "claude", before the google/anthropic/openai rename) is just as
    unregistered as any other bogus string today - same silent-ignore
    behavior as test_post_config_rejects_unregistered_provider_name above,
    specifically covering the backward-compat case a stale client/bookmark
    could still send."""
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "llm_provider": "claude", "llm_model": "claude-sonnet-5",
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "google"
    assert data["active_llm_model"] == "gemini-3.6-flash"


def test_post_config_rejects_model_not_in_that_providers_preset_list(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "llm_provider": "anthropic", "llm_model": "not-a-real-claude-model",
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    # The provider itself IS still accepted/persisted (independently valid),
    # only the bogus model is dropped - falls back to that provider's own
    # default_model rather than persisting a model the provider doesn't
    # actually offer.
    assert data["active_llm_provider"] == "anthropic"
    assert data["active_llm_model"] == "claude-sonnet-5"


def test_post_config_model_selection_survives_alongside_auto_sql_execute_toggle(app_env):
    # llm_provider/llm_model are independent of auto_sql_execute (same
    # "a request can touch one, the other, both, or neither" reasoning
    # config_routes.py already documents for auto_sql_execute itself) - a
    # request touching only auto_sql_execute must never clobber a
    # previously-saved model choice, and vice versa.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_provider": "openai", "llm_model": "gpt-5.6-luna"})
    app_env.client.post('/api/config', json={"auto_sql_execute": False})

    data = app_env.client.get('/api/config').get_json()
    assert data["active_llm_provider"] == "openai"
    assert data["active_llm_model"] == "gpt-5.6-luna"
    assert data["auto_sql_execute"] is False


def test_post_config_model_only_save_does_not_touch_database_connection(app_env):
    login_as(app_env.client, "alice@example.com")
    before = app_env.client.get('/api/config').get_json()

    app_env.client.post('/api/config', json={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"})

    after = app_env.client.get('/api/config').get_json()
    assert after["active_database_url"] == before["active_database_url"]
    assert after["active_is_custom"] == before["active_is_custom"]


# --- Bring Your Own Key (Preferences dialog's third section) -----------------
# GET /api/config only ever exposes llm_byok_key_set - {"google": bool,
# "anthropic": bool, "openai": bool} - never the saved key itself (see
# state_store.py's get_session docstring on llm_byok_key_set). POST accepts
# llm_byok_keys as a dict of only the provider(s) being changed.

def test_get_config_default_byok_key_set_is_all_false(app_env):
    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"] == {"google": False, "anthropic": False, "openai": False}


def test_post_config_saves_byok_key_and_never_echoes_it_back(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={"llm_byok_keys": {"google": "my-secret-key"}})
    assert resp.status_code == 200
    # Not in the POST response either - grep the whole raw body, not just
    # the known fields, in case a future change accidentally adds a new
    # field that leaks it.
    assert "my-secret-key" not in resp.get_data(as_text=True)

    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"] == {"google": True, "anthropic": False, "openai": False}
    assert "my-secret-key" not in app_env.client.get('/api/config').get_data(as_text=True)


def test_post_config_byok_key_for_one_provider_does_not_touch_another(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_byok_keys": {"google": "google-key"}})
    app_env.client.post('/api/config', json={"llm_byok_keys": {"openai": "openai-key"}})

    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"] == {"google": True, "anthropic": False, "openai": True}


def test_post_config_byok_key_empty_string_clears_it(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_byok_keys": {"google": "google-key"}})
    app_env.client.post('/api/config', json={"llm_byok_keys": {"google": ""}})

    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"]["google"] is False


def test_post_config_byok_keys_ignores_unregistered_provider_name(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={"llm_byok_keys": {"not-a-real-provider": "whatever"}})
    assert resp.status_code == 200  # silently ignored, not a hard error
    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"] == {"google": False, "anthropic": False, "openai": False}


def test_post_config_byok_keys_only_save_does_not_touch_model_selection_or_connection(app_env):
    # No preset_id/is_custom/llm_provider at all - just the BYOK section of
    # the Preferences dialog being saved on its own (same "independent
    # concern" reasoning theme/auto_sql_execute already get - see the elif
    # branch in handle_config this exercises).
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_provider": "anthropic", "llm_model": "claude-sonnet-5"})
    before = app_env.client.get('/api/config').get_json()

    app_env.client.post('/api/config', json={"llm_byok_keys": {"google": "google-key"}})

    after = app_env.client.get('/api/config').get_json()
    assert after["active_llm_provider"] == before["active_llm_provider"]
    assert after["active_llm_model"] == before["active_llm_model"]
    assert after["active_database_url"] == before["active_database_url"]
    assert after["llm_byok_key_set"]["google"] is True


def test_post_config_byok_keys_survive_alongside_other_field_updates_untouched(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_byok_keys": {"anthropic": "claude-key"}})
    app_env.client.post('/api/config', json={"auto_sql_execute": False})

    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"]["anthropic"] is True
    assert data["auto_sql_execute"] is False


def test_post_config_byok_key_selection_is_isolated_per_user(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={"llm_byok_keys": {"google": "alice-key"}})

    login_as(app_env.client, "bob@example.com")
    data = app_env.client.get('/api/config').get_json()
    assert data["llm_byok_key_set"] == {"google": False, "anthropic": False, "openai": False}
