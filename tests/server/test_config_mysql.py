"""
Custom (user-saved) and admin-preset MySQL connections through
/api/config: MySQL is the "simple URL" dialect pattern (see
backends/mysql.py's module docstring) - a single connection-string URL
carries everything, same as Postgres, with no separate credential field
and no BigQuery-style billing project or Snowflake-style always-explicit
multi-field descriptor.

The specific thing worth regression-testing here: before this dialect was
added, config_routes.py's fallback branches for "anything that isn't
BigQuery/Snowflake" unconditionally relabeled the connection "postgres"
regardless of what was actually selected (see _parse_incoming_connection's
and _parse_incoming_custom_databases' now-fixed final branches) - so a
MySQL connection could previously only have been silently saved/activated
as a mislabeled Postgres one. These tests specifically assert the type
comes back as "mysql", not "postgres", and that mixing a Postgres and a
MySQL custom connection in the same request doesn't collapse either one
into the other.
"""

from helpers import login_as, write_database_presets_file, install_fake_pymysql_connect


def test_custom_mysql_connection_persists_with_correct_type(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mysql", "database_url": "mysql://u:p@h:3306/db",
        "database_name": "MySQL Conn", "is_custom": True,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_type'] == "mysql"
    assert data['active_is_custom'] is True
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['type'] == "mysql"
    assert data['custom_databases'][0]['name'] == "MySQL Conn"


def test_mysql_and_postgres_custom_connections_do_not_collapse_into_each_other(app_env):
    # Regression test for the exact bug this dialect's addition surfaced -
    # see module docstring.
    login_as(app_env.client, "alice@example.com")
    payload = [
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://u:p@h/pgdb"},
        {"type": "mysql", "name": "MySQL Conn", "url": "mysql://u:p@h/mysqldb"},
    ]
    resp = app_env.client.post('/api/config', json={
        "database_type": "mysql", "database_url": "mysql://u:p@h/mysqldb",
        "database_name": "MySQL Conn", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    types_by_name = {c["name"]: c["type"] for c in data['custom_databases']}
    assert types_by_name == {"PG Conn": "postgres", "MySQL Conn": "mysql"}


def test_switching_from_mysql_to_postgres_custom_connection_preserves_both_types(app_env):
    login_as(app_env.client, "alice@example.com")
    payload = [
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://u:p@h/pgdb"},
        {"type": "mysql", "name": "MySQL Conn", "url": "mysql://u:p@h/mysqldb"},
    ]
    app_env.client.post('/api/config', json={
        "database_type": "mysql", "database_url": "mysql://u:p@h/mysqldb",
        "database_name": "MySQL Conn", "is_custom": True, "custom_databases": payload,
    })
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/pgdb",
        "database_name": "PG Conn", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_type'] == "postgres"
    types_by_name = {c["name"]: c["type"] for c in data['custom_databases']}
    assert types_by_name == {"PG Conn": "postgres", "MySQL Conn": "mysql"}


def test_mysql_connection_actually_dispatches_to_mysql_backend(app_env, mysql_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "mysql", "database_url": "mysql://alice:secret@dbhost:3306/salesdb",
        "database_name": "MySQL Conn", "is_custom": True,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # >=1 rather than an exact count: the /api/config POST above also
    # triggers its own best-effort connect() for the "which DB am I
    # connected to" identity-label probe (config_routes.py's handle_config)
    # - the assertion here only cares that /api/execute's connect() carried
    # the right, fully round-tripped connection details.
    assert len(mysql_harness.calls) >= 1
    kwargs = mysql_harness.calls[-1]
    assert kwargs["host"] == "dbhost"
    assert kwargs["user"] == "alice"
    assert kwargs["password"] == "secret"
    assert kwargs["database"] == "salesdb"


def test_cloud_sql_unix_socket_preset_dispatches_with_socket_not_localhost(app_factory, tmp_path, monkeypatch):
    # Regression test for a real Cloud Run failure: a Cloud SQL preset's
    # URL carries its connection details via a unix_socket query param
    # rather than a real TCP host (see backends/mysql.py's module
    # docstring) - the original connect() ignored the query string and
    # fell back to "localhost", which doesn't exist on Cloud Run.
    from helpers import install_fake_pymysql_connect
    path = write_database_presets_file(tmp_path, [{
        "type": "mysql", "name": "Sales Mgmt (CloudSQL/MySQL)",
        "url": "mysql://trial:FooBar@/classicmodels?unix_socket=/cloudsql/proj:us-east1:instance",
    }])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    harness = install_fake_pymysql_connect(monkeypatch)
    login_as(env.client, "alice@example.com")

    env.client.post('/api/config', json={"preset_id": "mysql+Sales Mgmt (CloudSQL/MySQL)"})
    env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    assert len(harness.calls) >= 1
    kwargs = harness.calls[-1]
    assert kwargs["unix_socket"] == "/cloudsql/proj:us-east1:instance"
    assert kwargs.get("host") != "localhost"
    assert "host" not in kwargs


def test_missing_url_is_treated_as_no_op_not_an_error(app_env):
    # Mirrors Postgres's/Snowflake's "not enough to identify a connection
    # yet" behavior - a blank database_url is a silent no-op, not a 400.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "mysql", "database_name": "MySQL Conn", "is_custom": True,
    })
    assert resp.status_code == 200


# --- admin preset ------------------------------------------------------------

def test_mysql_preset_selectable_and_reported_active(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "mysql", "name": "Sales (MySQL)", "url": "mysql://demo:pw@host:3306/sales"},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    resp = env.client.post('/api/config', json={"preset_id": "mysql+Sales (MySQL)"})
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    # active_database_type is blanked for an active preset (redacted, same
    # as any other admin preset - see config_routes.py's handle_config);
    # the type is instead confirmed via configured_databases (below) and
    # via active_preset_id matching the mysql preset's own stable id, not a
    # postgres one (the regression this test exists for - see module
    # docstring).
    assert data['active_preset_id'] == "mysql+Sales (MySQL)"
    assert data['active_database_type'] == ""
    assert data['active_is_custom'] is False
    assert data['configured_databases'][0]['type'] == "mysql"


# --- anonymous users -----------------------------------------------------------

def test_anonymous_user_can_save_a_custom_mysql_connection(app_factory, monkeypatch):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_pymysql_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "mysql", "database_url": "mysql://u:p@h/db",
        "database_name": "My DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'mysql'
