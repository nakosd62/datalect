"""
db.py's _resolve_database_name: the human-readable name recorded alongside
each translation-history row for a CUSTOM connection of one of the 7
structured dialects (BigQuery/Snowflake/Databricks/Oracle/Redshift/MSSQL/
Sheets). These dialects have no real url of their own any more (see
config_routes.py's module docstring), so this can no longer match a saved
custom connection by url the way it still does for Postgres/MySQL - it has
to compare the resolved descriptor's own config fields against each saved
row's config instead (see _resolve_database_name's docstring in db.py).

Exercised at the db.py/state_store.py layer directly (not through
/api/translate's streaming response) since get_translation_history()
doesn't currently surface database_name back out through /api/history at
all - it's recorded for the row regardless, so this checks the actual
stored value via a raw query against the same SQLite file app_config's
state_store is using, the same way test_database_config_encryption.py's
raw_sqlite_config helper does.
"""

import sqlite3

from helpers import install_fake_bigquery, login_as, make_service_account_key_json


def _last_recorded_database_name(app_env):
    with sqlite3.connect(app_env.app_config.state_store.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT database_name FROM translations ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else None


def test_custom_bigquery_connections_own_name_is_recorded_not_a_cache_key(app_env, monkeypatch):
    install_fake_bigquery(monkeypatch)
    login_as(app_env.client, "alice@example.com")

    key = make_service_account_key_json(project_id="proj-a")
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "warehouse-project", "dataset": "sales",
        "billing_project_id": "warehouse-project", "credentials_json": key,
        "database_name": "My Sales Warehouse", "is_custom": True,
    })
    assert resp.status_code == 200

    import db
    descriptor, _missing = db.resolve_active_descriptor(
        app_env.app_config.state_store.get_session("alice@example.com"), "alice@example.com"
    )
    assert descriptor["type"] == "bigquery"
    # The bug this guards against: descriptor["url"] is None now (BigQuery
    # has no real url of its own), so a url-based match would find nothing
    # and silently fall back to the non-sensitive cache key (e.g.
    # "warehouse-project.sales") instead of the user's own saved name.
    assert descriptor["url"] is None

    db.record_translation(
        "alice@example.com", descriptor, "show total sales", "SELECT SUM(total) FROM sales;",
        "gemini-2.5-flash", 100, 5, 5, 10, 0, 0,
    )
    assert _last_recorded_database_name(app_env) == "My Sales Warehouse"


def test_two_custom_bigquery_connections_sharing_project_dataset_each_record_their_own_name(app_env, monkeypatch):
    install_fake_bigquery(monkeypatch)
    login_as(app_env.client, "alice@example.com")

    key_a = make_service_account_key_json(project_id="project-a")
    key_b = make_service_account_key_json(project_id="project-b")
    payload = [
        {"type": "bigquery", "name": "Conn A", "project_id": "shared-proj", "dataset": "shared_ds",
         "credentials_json": key_a, "billing_project_id": "project-a"},
        {"type": "bigquery", "name": "Conn B", "project_id": "shared-proj", "dataset": "shared_ds",
         "credentials_json": key_b, "billing_project_id": "project-b"},
    ]
    resp = app_env.client.post('/api/config', json={
        "database_type": "bigquery", "project_id": "shared-proj", "dataset": "shared_ds",
        "billing_project_id": "project-a", "credentials_json": key_a,
        "database_name": "Conn A", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    import db
    descriptor, _missing = db.resolve_active_descriptor(
        app_env.app_config.state_store.get_session("alice@example.com"), "alice@example.com"
    )
    db.record_translation(
        "alice@example.com", descriptor, "q", "SELECT 1;", "gemini-2.5-flash", 1, 1, 1, 1, 0, 0,
    )
    # Both connections share the exact same project/dataset (and hence the
    # same non-sensitive cache key) - only their own saved name (via
    # has_custom_credentials-aware config matching, not project/dataset
    # alone) tells them apart here.
    assert _last_recorded_database_name(app_env) == "Conn A"
