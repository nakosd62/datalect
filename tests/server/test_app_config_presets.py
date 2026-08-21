"""
app_config.py's DATABASE_PRESETS_FILE parsing into CONFIGURED_DBS - covers
Postgres/BigQuery/Snowflake preset shapes, malformed input handling,
missing-file handling, and the default-fallback/DEFAULT_CONN derivation
rules. Each test builds its own fresh app instance (via app_factory) since
CONFIGURED_DBS is computed once at import time from the env - and writes
its own presets file under tmp_path (see helpers.write_database_presets_file
/ write_database_presets_file_raw) since DATABASE_PRESETS_FILE points at a
file path rather than holding JSON inline.
"""

import logging

from helpers import write_database_presets_file, write_database_presets_file_raw


def test_no_presets_env_falls_back_to_single_default_db(app_factory):
    env = app_factory(env={})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"
    assert env.app_config.CONFIGURED_DBS[0]["type"] == "postgres"


def test_postgres_preset_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Shop", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {"id": "postgres+Shop", "name": "Shop", "type": "postgres", "url": "postgresql://u:p@h/db"}


def test_postgres_preset_missing_url_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{"type": "postgres", "name": "Shop"}])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    # Skipped entirely -> falls back to the single synthetic default.
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_mysql_preset_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "mysql", "name": "Sales", "url": "mysql://u:p@h:3306/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {"id": "mysql+Sales", "name": "Sales", "type": "mysql", "url": "mysql://u:p@h:3306/db"}


def test_mysql_preset_missing_url_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{"type": "mysql", "name": "Sales"}])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    # Skipped entirely -> falls back to the single synthetic default.
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_mysql_preset_type_is_case_insensitive(app_factory, tmp_path):
    # "type" is lowercased before dispatch (see app_config.py's presets
    # loop), so a preset authored with mixed case (e.g. "mySQL", as a
    # human might naturally type it) still parses instead of being
    # skipped as an unsupported type.
    path = write_database_presets_file(tmp_path, [
        {"type": "mySQL", "name": "Sales", "url": "mysql://u:p@h:3306/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["type"] == "mysql"


def test_mysql_preset_with_cloud_sql_unix_socket_url_parsed(app_factory, tmp_path):
    # Cloud SQL connections carry their socket path in the URL's query
    # string rather than a real host - see backends/mysql.py's module
    # docstring - app_config.py's preset parsing doesn't need to know
    # anything about that (it just stores the url string as-is), but this
    # locks in that such a URL round-trips through unchanged rather than
    # being mangled or rejected here.
    url = "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance"
    path = write_database_presets_file(tmp_path, [
        {"type": "mySQL", "name": "Sales Mgmt (CloudSQL/MySQL)", "url": url},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["type"] == "mysql"
    assert db["url"] == url


def test_preset_missing_name_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_bigquery_preset_with_explicit_billing_project_id(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "bigquery", "name": "Trends", "project_id": "bigquery-public-data",
        "dataset": "google_trends", "billing_project_id": "my-billing-proj",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["type"] == "bigquery"
    assert db["url"] == "bigquery://bigquery-public-data/google_trends"
    assert db["billing_project_id"] == "my-billing-proj"


def test_bigquery_preset_missing_project_id_or_dataset_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "bigquery", "name": "Bad", "project_id": "p"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_bigquery_preset_with_no_billing_project_id_falls_back_to_project_id_and_warns(app_factory, tmp_path, caplog):
    path = write_database_presets_file(tmp_path, [{
        "type": "bigquery", "name": "Trends", "project_id": "bigquery-public-data",
        "dataset": "google_trends",
    }])
    with caplog.at_level(logging.WARNING, logger="ydyl"):
        env = app_factory(env={"DATABASE_PRESETS_FILE": path})
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


def test_snowflake_preset_with_password_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "Sample", "account": "myorg-myacct", "user": "svc",
        "warehouse": "COMPUTE_WH", "database": "SNOWFLAKE_SAMPLE_DATA", "schema": "TPCH_SF1",
        "role": "ACCOUNTADMIN", "password": "hunter2",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {
        "id": "snowflake+Sample", "name": "Sample", "type": "snowflake",
        "url": "snowflake://myorg-myacct/SNOWFLAKE_SAMPLE_DATA/TPCH_SF1",
        "account": "myorg-myacct", "user": "svc", "warehouse": "COMPUTE_WH",
        "database": "SNOWFLAKE_SAMPLE_DATA", "schema": "TPCH_SF1", "role": "ACCOUNTADMIN",
        "password": "hunter2",
    }


def test_snowflake_preset_with_private_key_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "KP", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db", "private_key": "PEM",
        "private_key_passphrase": "shh",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["private_key"] == "PEM"
    assert db["private_key_passphrase"] == "shh"
    assert "password" not in db


def test_snowflake_preset_omits_optional_schema_and_role_when_blank(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "NoSchema", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db", "password": "x",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["url"] == "snowflake://acct/db"
    assert "schema" not in db
    assert "role" not in db


def test_snowflake_preset_missing_core_fields_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "snowflake", "name": "Bad", "account": "acct", "password": "x"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_snowflake_preset_missing_credential_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "NoCred", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_snowflake_preset_with_both_password_and_private_key_is_skipped(app_factory, tmp_path):
    # Exactly one credential shape, same rule as the custom-connection path
    # (state_store.py/config_routes.py) - ambiguous otherwise.
    path = write_database_presets_file(tmp_path, [{
        "type": "snowflake", "name": "Ambiguous", "account": "acct", "user": "svc",
        "warehouse": "wh", "database": "db", "password": "x", "private_key": "y",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_databricks_preset_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "databricks", "name": "Sample Lakehouse", "server_hostname": "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/0123456789abcdef", "catalog": "main", "schema": "sales",
        "access_token": "dapi-secret",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {
        "id": "databricks+Sample Lakehouse", "name": "Sample Lakehouse", "type": "databricks",
        "url": "databricks://dbc-a1b2c3d4-e5f6.cloud.databricks.com/sql/1.0/warehouses/0123456789abcdef",
        "server_hostname": "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/0123456789abcdef",
        "catalog": "main", "schema": "sales", "access_token": "dapi-secret",
    }


def test_databricks_preset_omits_optional_catalog_and_schema_when_blank(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "databricks", "name": "NoCatalog", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc", "access_token": "tok",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["url"] == "databricks://dbc-x.cloud.databricks.com/sql/1.0/warehouses/abc"
    assert "catalog" not in db
    assert "schema" not in db


def test_databricks_preset_missing_core_fields_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "databricks", "name": "Bad", "server_hostname": "dbc-x.cloud.databricks.com", "access_token": "tok"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_databricks_preset_missing_credential_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "databricks", "name": "NoCred", "server_hostname": "dbc-x.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_oracle_preset_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "Orders (Oracle)", "host": "db.example.com", "port": 1521,
        "service_name": "ORCLPDB1", "user": "svc_ydyl", "password": "hunter2", "schema": "sales",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    db = env.app_config.CONFIGURED_DBS[0]
    assert db == {
        "id": "oracle+Orders (Oracle)", "name": "Orders (Oracle)", "type": "oracle",
        "url": "oracle://db.example.com:1521/ORCLPDB1",
        "host": "db.example.com", "port": 1521, "user": "svc_ydyl",
        "password": "hunter2", "service_name": "ORCLPDB1", "schema": "sales",
    }


def test_oracle_preset_with_sid_parsed(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "Legacy", "host": "db.example.com", "sid": "XE",
        "user": "svc", "password": "x",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["sid"] == "XE"
    assert "service_name" not in db
    assert db["url"] == "oracle://db.example.com:1521/XE"


def test_oracle_preset_defaults_port_to_1521_when_omitted(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "NoPort", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "svc", "password": "x",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["port"] == 1521


def test_oracle_preset_omits_optional_schema_when_blank(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "NoSchema", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "svc", "password": "x",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert "schema" not in db


def test_oracle_preset_omits_ssl_when_blank(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "NoSsl", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "svc", "password": "x",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert "ssl" not in db


def test_oracle_preset_carries_ssl_true_through(app_factory, tmp_path):
    # Regression coverage for the real-world bug "ssl" fixes - an Oracle
    # Cloud/Autonomous Database preset needs this threaded through to
    # connect() as a TLS handshake, or it fails with a confusing
    # DPY-4011/DPY-6005 "connection reset" against the TLS-only listener
    # (see backends/oracle.py's module docstring).
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "ADB", "host": "adb.us-ashburn-1.oraclecloud.com",
        "port": 1522, "service_name": "myatp_high.adb.oraclecloud.com",
        "user": "admin", "password": "x", "ssl": True,
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    db = env.app_config.CONFIGURED_DBS[0]
    assert db["ssl"] is True


def test_oracle_preset_missing_core_fields_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "oracle", "name": "Bad", "host": "db.example.com", "password": "x"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_oracle_preset_missing_credential_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [{
        "type": "oracle", "name": "NoCred", "host": "db.example.com", "service_name": "ORCLPDB1",
        "user": "svc",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_unsupported_preset_type_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "oracle", "name": "Bad", "url": "oracle://x"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_non_dict_preset_entry_is_skipped(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, ["just a string", 42])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_malformed_json_is_ignored_entirely(app_factory, tmp_path):
    path = write_database_presets_file_raw(tmp_path, "{not valid json")
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"


def test_non_array_json_is_ignored_entirely(app_factory, tmp_path):
    path = write_database_presets_file_raw(
        tmp_path, '{"type":"postgres","name":"x","url":"y"}',
    )
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1


def test_missing_presets_file_is_ignored_entirely_and_logs_an_error(app_factory, tmp_path, caplog):
    # DATABASE_PRESETS_FILE points somewhere that doesn't exist - e.g. a
    # typo'd path, or a Cloud Run secret volume that failed to mount.
    # Should behave exactly like "no presets configured" rather than
    # crashing app_config.py's module-level import.
    missing_path = str(tmp_path / "does_not_exist.json")
    with caplog.at_level(logging.ERROR, logger="ydyl"):
        env = app_factory(env={"DATABASE_PRESETS_FILE": missing_path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "Default DB"
    assert any("DATABASE_PRESETS_FILE" in rec.message for rec in caplog.records)


def test_multiple_presets_of_mixed_types(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "PG", "url": "postgresql://u:p@h/db"},
        {"type": "mysql", "name": "MySQL DB", "url": "mysql://u:p@h/mysqldb"},
        {"type": "bigquery", "name": "BQ", "project_id": "p", "dataset": "d", "billing_project_id": "p"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 3
    types = {db["type"] for db in env.app_config.CONFIGURED_DBS}
    assert types == {"postgres", "mysql", "bigquery"}


# --- preset "id" ---------------------------------------------------------
# Slice A of the connection-identity redesign: presets get a stable,
# admin-chosen "id" (see the DATABASE_PRESETS_FILE comment) rather than
# being identified by URL (ambiguous once a custom connection can share
# one) or by position in this file (silently wrong the moment a preset is
# reordered/removed/added).

def test_explicit_preset_id_is_used_as_is(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"id": "ecommerce-prod", "type": "postgres", "name": "Shop", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert env.app_config.CONFIGURED_DBS[0]["id"] == "ecommerce-prod"


def test_preset_id_works_uniformly_across_every_dialect(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"id": "pg-1", "type": "postgres", "name": "PG", "url": "postgresql://u:p@h/pgdb"},
        {"id": "mysql-1", "type": "mysql", "name": "MySQL", "url": "mysql://u:p@h/mysqldb"},
        {"id": "bq-1", "type": "bigquery", "name": "BQ", "project_id": "p", "dataset": "d", "billing_project_id": "p"},
        {
            "id": "sf-1", "type": "snowflake", "name": "SF", "account": "acc", "user": "u",
            "warehouse": "wh", "database": "db", "password": "hunter2",
        },
        {
            "id": "dbx-1", "type": "databricks", "name": "DBX", "server_hostname": "host",
            "http_path": "/path", "access_token": "tok",
        },
        {
            "id": "ora-1", "type": "oracle", "name": "ORA", "host": "h", "service_name": "svc",
            "user": "u", "password": "p",
        },
        {
            "id": "rs-1", "type": "redshift", "name": "RS", "host": "h", "database": "db",
            "user": "u", "password": "p",
        },
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    ids = {db["type"]: db["id"] for db in env.app_config.CONFIGURED_DBS}
    assert ids == {
        "postgres": "pg-1", "mysql": "mysql-1", "bigquery": "bq-1", "snowflake": "sf-1",
        "databricks": "dbx-1", "oracle": "ora-1", "redshift": "rs-1",
    }


def test_preset_missing_id_falls_back_to_type_and_name(app_factory, tmp_path):
    # Migration aid, not the recommended long-term state (see the
    # DATABASE_PRESETS_FILE comment) - a preset saved before "id" existed,
    # or one an admin hasn't gotten to yet, still gets a usable identity
    # rather than breaking outright.
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "First", "url": "postgresql://u:p@h/db1"},
        {"type": "postgres", "name": "Second", "url": "postgresql://u:p@h/db2"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert [db["id"] for db in env.app_config.CONFIGURED_DBS] == ["postgres+First", "postgres+Second"]


def test_preset_id_fallback_survives_reordering_and_earlier_entries_being_skipped(app_factory, tmp_path):
    # Unlike a position-based fallback, "{type}+{name}" doesn't shift when
    # an earlier entry in the file is skipped (e.g. missing "url") or when
    # presets are reordered - it depends only on this preset's own type and
    # name, never on where it happens to sit in the file.
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Bad"},  # missing "url" - skipped
        {"type": "postgres", "name": "Good", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["id"] == "postgres+Good"


def test_blank_explicit_id_falls_back_to_type_and_name(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"id": "   ", "type": "postgres", "name": "Shop", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert env.app_config.CONFIGURED_DBS[0]["id"] == "postgres+Shop"


def test_duplicate_preset_id_is_skipped_like_any_other_malformed_preset(app_factory, tmp_path, caplog):
    # A duplicate id gets the same treatment as every other malformed
    # preset in this loop (missing "name"/"url"/credential) - skipped
    # entirely, never loaded - rather than loading anyway and leaving a
    # config-modal radio around that silently activates the WRONG
    # connection (the first preset with that id) whenever it's clicked.
    path = write_database_presets_file(tmp_path, [
        {"id": "dup", "type": "postgres", "name": "First", "url": "postgresql://u:p@h/db1"},
        {"id": "dup", "type": "postgres", "name": "Second", "url": "postgresql://u:p@h/db2"},
    ])
    with caplog.at_level(logging.WARNING, logger="ydyl"):
        env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["name"] == "First"
    assert any("Skipping database preset 'Second'" in rec.message for rec in caplog.records)


def test_duplicate_preset_id_from_two_colliding_fallback_ids_is_also_skipped(app_factory, tmp_path, caplog):
    # The fallback id ("{type}+{name}") can collide too, e.g. two presets
    # of the same type that an admin happened to give the same name - same
    # skip-the-later-one behavior as an explicit duplicate "id".
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Demo", "url": "postgresql://u:p@h/db1"},
        {"type": "postgres", "name": "Demo", "url": "postgresql://u:p@h/db2"},
    ])
    with caplog.at_level(logging.WARNING, logger="ydyl"):
        env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert len(env.app_config.CONFIGURED_DBS) == 1
    assert env.app_config.CONFIGURED_DBS[0]["url"] == "postgresql://u:p@h/db1"
    assert any("Skipping database preset 'Demo'" in rec.message for rec in caplog.records)


def test_default_fallback_db_has_an_id(app_factory):
    # The synthetic "Default DB" used when no presets file is configured at
    # all must still carry an id - every other code path that reads
    # CONFIGURED_DBS can assume "id" is always present.
    env = app_factory(env={})
    assert env.app_config.CONFIGURED_DBS[0]["id"] == "postgres+Default DB"


def test_default_conn_is_first_postgres_preset_even_if_bigquery_listed_first(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "bigquery", "name": "BQ", "project_id": "p", "dataset": "d", "billing_project_id": "p"},
        {"type": "postgres", "name": "PG", "url": "postgresql://u:p@h/pgdb"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert env.app_config.DEFAULT_CONN == "postgresql://u:p@h/pgdb"


def test_default_conn_is_first_postgres_preset_even_if_mysql_listed_first(app_factory, tmp_path):
    # DEFAULT_CONN/the state-store fallback assume a plain Postgres URL
    # literal (see app_config.py's comment above _postgres_presets) - a
    # MySQL preset, even though it's also a simple URL-based dialect, must
    # not be picked as the default just because it's listed first.
    path = write_database_presets_file(tmp_path, [
        {"type": "mysql", "name": "MySQL DB", "url": "mysql://u:p@h/mysqldb"},
        {"type": "postgres", "name": "PG", "url": "postgresql://u:p@h/pgdb"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert env.app_config.DEFAULT_CONN == "postgresql://u:p@h/pgdb"


def test_default_conn_falls_back_to_hardcoded_when_only_bigquery_presets_exist(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "bigquery", "name": "BQ", "project_id": "p", "dataset": "d", "billing_project_id": "p"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    assert env.app_config.DEFAULT_CONN.startswith("postgresql://postgres:password@")


def test_default_conn_falls_back_to_hardcoded_when_only_mysql_presets_exist(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "mysql", "name": "MySQL DB", "url": "mysql://u:p@h/mysqldb"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
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
