"""
The "schema" field for CUSTOM Postgres connections through /api/config - a
config_routes.py-level addition mirroring the ALREADY-SHIPPED admin-preset
side of this same feature (see app_config.py's Postgres preset-parsing
block and its own test_app_config_presets.py coverage) plus the pattern
every other structured dialect (Snowflake/Databricks/Oracle/Redshift/
MSSQL) already uses for its own optional "schema" field in this same
module. Postgres and MySQL share a single "simple URL" code path in
config_routes.py (see that module's docstring and
test_config_postgres_ca_cert.py's own docstring for ca_cert_pem's parallel
history) - "schema" is Postgres-only, since MySQL has no separate schema
concept of its own (a MySQL schema IS the database, already named in its
connection URL - see backends/mysql.py's module docstring), so this file
also guards that MySQL rows never pick up a stray "schema" key.

This file focuses on config_routes.py's parsing/storage of the field
(single-connection and custom_databases-list forms) plus one true
end-to-end dispatch test confirming it actually reaches
backends.postgres.PostgresBackend.connect() as a "SET search_path"
statement - the connect()-level unit coverage of that mechanism itself
(including the get_schema() current_schema() scoping) lives in
test_postgres_backend.py.
"""

from helpers import login_as


def test_custom_postgres_connection_persists_schema(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True, "schema": "golf",
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['config']['schema'] == "golf"


def test_postgres_connection_without_schema_still_saves_fine(app_env):
    # Regression guard: schema is optional - a plain connection with no
    # such field at all (the overwhelming common case) must keep working
    # exactly as it did before this feature existed, and must NOT have a
    # "schema" key silently defaulted in (e.g. to the literal string
    # "public") - Postgres's own ordinary search_path default already
    # covers that case with no stored config at all.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert 'schema' not in data['custom_databases'][0]['config']


def test_blank_schema_is_not_persisted(app_env):
    # A whitespace-only value must be treated the same as omitting the
    # field entirely - same convention every other dialect's own schema
    # field already follows in this module.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True, "schema": "   ",
    })

    data = app_env.client.get('/api/config').get_json()
    assert 'schema' not in data['custom_databases'][0]['config']


def test_custom_databases_list_form_persists_schema_for_postgres_only(app_env):
    # schema is Postgres-only - a mixed batch containing one of each
    # dialect must persist schema for the Postgres row and must NOT pick up
    # a stray "schema" key on the MySQL row even when one is (incorrectly)
    # supplied in the request for it.
    login_as(app_env.client, "alice@example.com")
    payload = [
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://u:p@h/pgdb", "schema": "golf"},
        {"type": "mysql", "name": "MySQL Conn", "url": "mysql://u:p@h/mysqldb", "schema": "should_be_ignored"},
    ]
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/pgdb",
        "database_name": "PG Conn", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    by_name = {c["name"]: c for c in data['custom_databases']}
    assert by_name["PG Conn"]["config"]["schema"] == "golf"
    assert 'schema' not in by_name["MySQL Conn"]["config"]


def test_connect_dispatches_schema_through_to_backend_as_search_path(app_env, postgres_harness):
    # The real end-to-end check: not just that config_routes.py stores
    # "schema", but that db.py's already-generic descriptor flattening
    # (descriptor.update(db.get("config") or {})) carries it all the way
    # to backends.postgres.PostgresBackend.connect(), which then issues a
    # real "SET search_path" - see test_postgres_backend.py for the
    # connect()-level unit coverage of that mechanism in isolation.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://alice:secret@dbhost:5432/salesdb",
        "database_name": "PG Conn", "is_custom": True, "schema": "golf",
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # >=1 rather than an exact count: the /api/config POST above also
    # triggers its own best-effort connect() for schema/identity purposes -
    # see test_config_postgres_ca_cert.py's matching comment for the
    # equivalent ca_cert_pem case.
    assert len(postgres_harness.connections) >= 1
    connection = postgres_harness.connections[-1]
    # The fake connection's single cursor() records every execute() call
    # made through it, not just connect()'s own "SET search_path" - the
    # /api/execute call above issues the user's actual "SELECT 1;" through
    # the same fake cursor afterward, so this only asserts the "SET
    # search_path" statement is present (as the first call), not that it's
    # the cursor's only call.
    assert connection.search_path_calls[0] == 'SET search_path TO "golf", public'
    assert connection.committed is True
