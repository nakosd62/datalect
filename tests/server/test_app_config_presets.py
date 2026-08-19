"""
app_config.py's DATABASE_PRESETS parsing into CONFIGURED_DBS - covers
Postgres/BigQuery preset shapes, malformed input handling, and the
default-fallback/DEFAULT_CONN derivation rules. Each test builds its own
fresh app instance (via app_factory) since CONFIGURED_DBS is computed once
at import time from the env.
"""

import logging


def test_no_presets_env_falls_back_to_single_default_db(app_factory):
    env = app_factory(env={})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"
    assert env.app_config.CONFIGURED_DBS[0]["type"] == "postgres"


def test_postgres_preset_parsed(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": '[{"type":"postgres","name":"Shop","url":"postgresql://u:p@h/db"}]',
    })
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {"name": "Shop", "type": "postgres", "url": "postgresql://u:p@h/db"}


def test_postgres_preset_missing_url_is_skipped(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": '[{"type":"postgres","name":"Shop"}]',
    })
    # Skipped entirely -> falls back to the single synthetic default.
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_preset_missing_name_is_skipped(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": '[{"type":"postgres","url":"postgresql://u:p@h/db"}]',
    })
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_bigquery_preset_with_explicit_billing_project_id(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"Trends","project_id":"bigquery-public-data",'
            '"dataset":"google_trends","billing_project_id":"my-billing-proj"}]'
        ),
    })
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["type"] == "bigquery"
    assert db["url"] == "bigquery://bigquery-public-data/google_trends"
    assert db["billing_project_id"] == "my-billing-proj"


def test_bigquery_preset_missing_project_id_or_dataset_is_skipped(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": '[{"type":"bigquery","name":"Bad","project_id":"p"}]',
    })
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_bigquery_preset_with_no_billing_project_id_falls_back_to_project_id_and_warns(app_factory, caplog):
    with caplog.at_level(logging.WARNING, logger="ydyl"):
        env = app_factory(env={
            "DATABASE_PRESETS": (
                '[{"type":"bigquery","name":"Trends","project_id":"bigquery-public-data",'
                '"dataset":"google_trends"}]'
            ),
        })
    db = env.app_config.CONFIGURED_DBS[0]
    # No env-var fallback exists anymore - falls back to its own
    # project_id, with a warning explaining why that will 403 for data
    # this app's identity doesn't own.
    assert db["billing_project_id"] == "bigquery-public-data"
    assert any("billing_project_id" in rec.message for rec in caplog.records)


def test_no_bigquery_billing_project_id_env_var_exists_anymore(app_factory):
    # Explicit regression guard for the user's instruction: get rid of
    # BIGQUERY_BILLING_PROJECT_ID entirely, no replacement fallback.
    env = app_factory(env={})
    assert not hasattr(env.app_config, "BIGQUERY_BILLING_PROJECT_ID")


def test_unsupported_preset_type_is_skipped(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": '[{"type":"mysql","name":"Bad","url":"mysql://x"}]',
    })
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_non_dict_preset_entry_is_skipped(app_factory):
    env = app_factory(env={"DATABASE_PRESETS": '["just a string", 42]'})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_malformed_json_is_ignored_entirely(app_factory):
    env = app_factory(env={"DATABASE_PRESETS": "{not valid json"})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_non_array_json_is_ignored_entirely(app_factory):
    env = app_factory(env={"DATABASE_PRESETS": '{"type":"postgres","name":"x","url":"y"}'})
    assert len(env.app_config.CONFIGURED_DBS) == 1


def test_multiple_presets_of_mixed_types(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"postgres","name":"PG","url":"postgresql://u:p@h/db"},'
            '{"type":"bigquery","name":"BQ","project_id":"p","dataset":"d","billing_project_id":"p"}]'
        ),
    })
    assert len(env.app_config.CONFIGURED_DBS) == 2
    types = {db["type"] for db in env.app_config.CONFIGURED_DBS}
    assert types == {"postgres", "bigquery"}


def test_default_conn_is_first_postgres_preset_even_if_bigquery_listed_first(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"BQ","project_id":"p","dataset":"d","billing_project_id":"p"},'
            '{"type":"postgres","name":"PG","url":"postgresql://u:p@h/pgdb"}]'
        ),
    })
    assert env.app_config.DEFAULT_CONN == "postgresql://u:p@h/pgdb"


def test_default_conn_falls_back_to_hardcoded_when_only_bigquery_presets_exist(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"BQ","project_id":"p","dataset":"d","billing_project_id":"p"}]'
        ),
    })
    assert env.app_config.DEFAULT_CONN.startswith("postgresql://postgres:password@")


def test_gemini_model_defaults_and_preset_models_include_it(app_factory):
    env = app_factory(env={})
    assert env.app_config.DEFAULT_MODEL == "gemini-2.5-flash"
    assert env.app_config.DEFAULT_MODEL in env.app_config.PRESET_MODELS


def test_custom_gemini_model_gets_added_to_preset_models(app_factory):
    env = app_factory(env={"GEMINI_MODEL": "gemini-custom-model"})
    assert env.app_config.PRESET_MODELS[0] == "gemini-custom-model"


def test_local_dev_uses_sqlite_state_store_by_default(app_factory):
    env = app_factory(env={})
    assert type(env.app_config.state_store).__name__ == "SqliteStateStore"


def test_auth_disabled_without_google_client_id(app_factory):
    env = app_factory(env={})
    assert env.app_config.AUTH_ENABLED is False


def test_auth_enabled_with_google_client_id(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    assert env.app_config.AUTH_ENABLED is True
