"""
GET /api/config's 'configured_databases' entries each carry a display-only
'dialect_name' field (e.g. "PostgreSQL", "BigQuery Standard SQL") alongside
the pre-existing raw 'type' key (e.g. "postgres", "bigquery") -
config_routes.py's own _redact_preset_for_client()/_preset_dialect_name()
helpers, added so client.js's connection picker (renderDbRadioButtons())
can show every preset as "<name> (<dialect_name>)", matching the exact
terminology the single-connection Schema Viewer's own "<name> in <dialect>"
title and the dataset-group Schema Viewer's "Type" column both already use
(all three are ultimately the same Backend.dialect_name - see
db.py's build_group_schema_summaries()).

Deliberately covers several distinct dialects in one request rather than
one dialect per test file (unlike test_config_<dialect>_presets.py, which
each already assert dialect_name as part of their own preset's full
redacted shape) - this file's only job is proving the mapping itself is
right across dialects, in one place.
"""

from helpers import write_database_presets_file, FAKE_DB_CONFIG_ENCRYPTION_KEY, FAKE_SESSION_SIGNING_KEY


def test_configured_databases_carries_the_right_dialect_name_per_dialect(app_factory, tmp_path):
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "my-a", "name": "MySQL A", "type": "mysql", "url": "mysql://u:p@h/a"},
        {"id": "bq-a", "name": "BigQuery A", "type": "bigquery", "project_id": "p", "dataset": "d"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": presets_path})

    data = env.client.get('/api/config').get_json()
    by_id = {db["id"]: db for db in data["configured_databases"]}

    assert by_id["pg-a"]["type"] == "postgres"
    assert by_id["pg-a"]["dialect_name"] == "PostgreSQL"
    assert by_id["my-a"]["type"] == "mysql"
    assert by_id["my-a"]["dialect_name"] == "MySQL"
    assert by_id["bq-a"]["type"] == "bigquery"
    assert by_id["bq-a"]["dialect_name"] == "BigQuery Standard SQL"


def test_configured_databases_dialect_name_survives_anonymous_redaction(app_factory, tmp_path):
    # dialect_name is exactly as safe to send as the "type" key it's
    # derived from (get_backend() only needs {"type": ...}, never a real
    # connection string) - a Cloud Run anonymous visitor still gets it,
    # the same as every other admin preset's id/name/type (mirrors
    # test_config_databricks_presets.py's own
    # test_anonymous_visitor_never_receives_the_presets_credential setup).
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
    ])
    env = app_factory(env={
        "K_SERVICE": "ydyl-service",
        "DB_CONFIG_ENCRYPTION_KEY": FAKE_DB_CONFIG_ENCRYPTION_KEY,
        "SESSION_SIGNING_KEY": FAKE_SESSION_SIGNING_KEY,
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": presets_path,
    }, mock_firestore=True)

    data = env.client.get('/api/config').get_json()
    assert data["configured_databases"] == [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "dialect_name": "PostgreSQL"}
    ]
