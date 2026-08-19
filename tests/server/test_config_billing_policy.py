"""
The BigQuery billing-project policy end to end, through config_routes.py's
/api/config + /api/execute: presets bill against their own explicit
billing_project_id (app_config.py), with no env-var fallback of any kind;
custom (user-added) BigQuery connections must ALWAYS supply both an
explicit billing_project_id and their own credentials_json, rejected with
a clear 400 otherwise - never inferred from a preset, the key's own
embedded project, or this app's own identity. See config_routes.py's and
app_config.py's module docstrings for the full policy rationale (this
directly covers the regression the user originally reported: a custom
connection into bigquery-public-data/google_ads failing with "does not
have bigquery.jobs.create permission" because nothing was billing the
right project).
"""

import pytest

from helpers import install_fake_bigquery, make_service_account_key_json, login_as


def test_authenticated_preset_selection_bills_against_presets_billing_project(app_factory, monkeypatch):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"Public Data","project_id":"bigquery-public-data",'
            '"dataset":"usa_names","billing_project_id":"my-own-billing-project"}]'
        ),
    })
    harness = install_fake_bigquery(monkeypatch)
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data",
        "dataset": "usa_names", "database_name": "Public Data",
    })
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "my-own-billing-project"


def test_anonymous_preset_selection_bills_against_presets_billing_project(app_factory, monkeypatch):
    env = app_factory(env={
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"Public Data","project_id":"bigquery-public-data",'
            '"dataset":"usa_names","billing_project_id":"my-own-billing-project"}]'
        ),
    })
    harness = install_fake_bigquery(monkeypatch)

    resp = env.client.post('/api/config', json={"preset_index": 0})
    assert resp.status_code == 200

    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "my-own-billing-project"


def test_preset_without_billing_project_id_falls_back_to_its_own_project_and_still_403_shaped(app_factory, monkeypatch):
    # No billing_project_id on this preset - app_config.py's fallback
    # (bare project_id, with a warning) is exercised here through the full
    # request path, not just the CONFIGURED_DBS parsing unit test.
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"No Billing","project_id":"bigquery-public-data","dataset":"usa_names"}]'
        ),
    })
    harness = install_fake_bigquery(monkeypatch)
    login_as(env.client, "alice@example.com")
    env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data",
        "dataset": "usa_names", "database_name": "No Billing",
    })
    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert harness.client_calls[-1]["project"] == "bigquery-public-data"


def test_custom_bigquery_connection_missing_billing_project_id_is_rejected(app_env):
    key_json = make_service_account_key_json()
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_ads",
        "database_name": "My Google Ads", "credentials_json": key_json, "is_custom": True,
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert "billing project" in data['error'].lower()
    assert "service-account key" in data['error'].lower()


def test_custom_bigquery_connection_missing_credentials_json_is_rejected(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_ads",
        "database_name": "My Google Ads", "billing_project_id": "my-project", "is_custom": True,
    })
    assert resp.status_code == 400


def test_custom_bigquery_connection_missing_both_fields_is_rejected(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_ads",
        "database_name": "My Google Ads", "is_custom": True,
    })
    assert resp.status_code == 400


def test_rejected_custom_connection_is_not_persisted(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_ads",
        "database_name": "My Google Ads", "is_custom": True,
    })
    data = app_env.client.get('/api/config').get_json()
    assert data['custom_databases'] == []


def test_custom_bigquery_connection_with_both_fields_succeeds_and_bills_exact_project(app_env, monkeypatch):
    harness = install_fake_bigquery(monkeypatch)
    key_json = make_service_account_key_json(project_id="key-embedded-project")
    login_as(app_env.client, "alice@example.com")

    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_ads",
        "database_name": "My Google Ads", "credentials_json": key_json,
        "billing_project_id": "users-own-billing-project", "is_custom": True,
    })
    assert resp.status_code == 200

    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # Billed against the EXPLICIT billing_project_id, never the key's own
    # embedded project ("key-embedded-project") and never a preset's.
    assert harness.client_calls[-1]["project"] == "users-own-billing-project"


def test_custom_connection_never_borrows_a_matching_presets_billing_project(app_env, monkeypatch):
    # A custom connection whose project_id/dataset happen to match an
    # admin preset must still require its OWN explicit billing_project_id
    # and key - no "matches a preset, so borrow its billing" shortcut.
    key_json = make_service_account_key_json()
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_trends",
        "database_name": "My Trends Clone", "credentials_json": key_json, "is_custom": True,
    })
    assert resp.status_code == 400


# --- _parse_incoming_connection / _parse_incoming_custom_databases, direct ----

def test_parse_incoming_connection_preset_uses_matching_preset_billing_project(app_factory):
    env = app_factory(env={
        "DATABASE_PRESETS": (
            '[{"type":"bigquery","name":"Trends","project_id":"bigquery-public-data",'
            '"dataset":"google_trends","billing_project_id":"preset-billing-proj"}]'
        ),
    })
    db_type, db_url, db_config, error = env.config_routes._parse_incoming_connection(
        {"database_type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_trends"},
        "alice@example.com", False,
    )
    assert error is None
    assert db_config["billing_project_id"] == "preset-billing-proj"


def test_parse_incoming_connection_incomplete_bigquery_fields_returns_no_url_no_error(app_env):
    # Missing project_id/dataset entirely = "no connection change
    # requested", not an error - distinct from the custom-BigQuery
    # missing-billing/key case, which IS an error.
    db_type, db_url, db_config, error = app_env.config_routes._parse_incoming_connection(
        {"database_type": "bigquery"}, "alice@example.com", True,
    )
    assert db_url is None
    assert error is None


def test_parse_incoming_custom_databases_silently_skips_incomplete_bigquery_rows(app_env):
    # An in-progress "+ Add custom connection" row the user hasn't
    # finished filling in yet must not block saving the rest of the list.
    key_json = make_service_account_key_json()
    merged = app_env.config_routes._parse_incoming_custom_databases([
        {"type": "bigquery", "name": "Complete", "project_id": "p", "dataset": "d",
         "credentials_json": key_json, "billing_project_id": "b"},
        {"type": "bigquery", "name": "Incomplete - no key or billing", "project_id": "p2", "dataset": "d2"},
    ], "alice@example.com")
    names = {m["name"] for m in merged}
    assert names == {"Complete"}


def test_parse_incoming_custom_databases_none_means_leave_list_alone(app_env):
    assert app_env.config_routes._parse_incoming_custom_databases(None, "alice@example.com") is None


def test_parse_incoming_custom_databases_empty_list_means_clear_it(app_env):
    assert app_env.config_routes._parse_incoming_custom_databases([], "alice@example.com") == []
