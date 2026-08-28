"""
Custom (user-saved) and admin-preset MongoDB Atlas SQL connections through
/api/config and /api/execute: MongoDB Atlas SQL is a hybrid dialect (see
backends/mongodb_sql.py's and config_routes.py's module docstrings) - it
has a real "url" like Postgres/MySQL (a bare mongodb:// deployment URI,
nothing else folded into it), but ALSO separate structured "database"/
"user"/"password" fields, same shape as Oracle/Redshift/SQL Server above.
There's no ca_cert_pem-style optional field: TLS trust lives inside the
URI's own "?ssl=true" query param.

The "type" value for this dialect is "MongoDB" (matching backends/
__init__.py's _BACKENDS dict key exactly, case-sensitively) even though
input parsing (config_routes.py/app_config.py) accepts it case-
insensitively, same "canonicalize on the way out" pattern every other
dialect here already follows - see config_routes.py's mongodb branch of
_parse_incoming_connection for the exact mechanism.

Mirrors test_config_mysql.py's shape (including its "doesn't collapse into
Postgres" regression coverage), plus what's unique to this dialect: its
four-field structured shape (closer to Redshift's/SQL Server's own tests
than to Postgres/MySQL's single-url tests) and its read-only enforcement
(a write statement must be rejected by /api/execute rather than reaching
the driver).
"""

from helpers import login_as, write_database_presets_file

# No "Driver={...}" clause, no leading "Uri=" key name, and no
# Database=/User=/Password= baked in - none of that is something a user
# (or a preset author) supplies anymore; backends/mongodb_sql.py's
# connect() reassembles/injects all of it itself (see that module's
# docstring). This bare URI is exactly the "url" shape a real user now
# pastes/saves, with "database"/"user"/"password" as separate fields.
_URI = "mongodb://atlas-sql-abc.mongodb.net/?ssl=true&authSource=admin"
_DATABASE = "mydb"
_USER = "alice"
_PASSWORD = "secret"

# What connect() actually hands pyodbc once it reassembles the four
# descriptor fields and injects the canonical Driver= clause and "Uri="
# key name - see test_mongodb_sql_connection_actually_dispatches_to_
# mongodb_sql_backend.
_FULL_ODBC_URL_WITH_DRIVER = (
    f"Driver={{MongoDB Atlas SQL ODBC Driver}};Uri={_URI}"
    f";Database={_DATABASE};User={_USER};Password={_PASSWORD}"
)


def test_custom_mongodb_sql_connection_persists_with_correct_type(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "Mongo Conn", "is_custom": True,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_type'] == "MongoDB"
    assert data['active_is_custom'] is True
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['type'] == "MongoDB"
    assert data['custom_databases'][0]['name'] == "Mongo Conn"
    # "url" round-trips as the bare URI - database/user land under
    # "config", not folded into url (see this module's docstring).
    assert data['custom_databases'][0]['url'] == _URI
    assert data['custom_databases'][0]['config']['database'] == _DATABASE
    assert data['custom_databases'][0]['config']['user'] == _USER
    # Password is a credential field - never round-tripped as plaintext
    # (see state_store.py's _CREDENTIAL_CONFIG_FIELDS).
    assert 'password' not in data['custom_databases'][0]['config']


def test_custom_mongodb_sql_connection_type_is_case_insensitive_on_input(app_env):
    # config_routes.py lowercases the incoming "database_type" before
    # matching, so "mongodb", "MONGODB", "MongoDB", etc. must all resolve
    # to this dialect - but the stored/exposed "type" is always the exact
    # canonical "MongoDB" spelling regardless of how it was submitted.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "MONGODB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "Mongo Conn", "is_custom": True,
    })
    assert resp.status_code == 200
    data = app_env.client.get('/api/config').get_json()
    assert data['active_database_type'] == "MongoDB"


def test_postgres_mysql_and_mongodb_sql_custom_connections_do_not_collapse_into_each_other(app_env):
    # Regression test widening test_config_mysql.py's equivalent to a third
    # dialect - config_routes.py's "anything that isn't one of the
    # structured dialects" fallback branch used to unconditionally relabel
    # every such connection "postgres" regardless of what was actually
    # selected (see _parse_incoming_connection's/_parse_incoming_custom_
    # databases' now-fixed final branches).
    login_as(app_env.client, "alice@example.com")
    payload = [
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://u:p@h/pgdb"},
        {"type": "mysql", "name": "MySQL Conn", "url": "mysql://u:p@h/mysqldb"},
        {"type": "MongoDB", "name": "Mongo Conn", "url": _URI, "database": _DATABASE, "user": _USER, "password": _PASSWORD},
    ]
    resp = app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "Mongo Conn", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 3
    types_by_name = {c["name"]: c["type"] for c in data['custom_databases']}
    assert types_by_name == {"PG Conn": "postgres", "MySQL Conn": "mysql", "Mongo Conn": "MongoDB"}


def test_missing_fields_are_treated_as_no_op_not_an_error(app_env):
    # Same "core identifying fields, nothing inferred" threshold every
    # other structured dialect uses - missing url/database/user isn't a
    # validation error yet, just not enough to identify a connection (e.g.
    # a fresh blank row) - see config_routes.py's mongodb branch.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_name": "Mongo Conn", "is_custom": True,
    })
    assert resp.status_code == 200


def test_missing_password_is_reported_as_an_error_not_a_silent_no_op(app_env):
    # Unlike a wholly blank row above, url+database+user with no password
    # (and nothing already saved server-side to fall back to) IS an error
    # - MongoDB Atlas SQL has no ambient/shared identity this app can fall
    # back to (see _CUSTOM_MONGODB_SQL_MISSING_FIELDS_ERROR).
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER,
        "database_name": "Mongo Conn", "is_custom": True,
    })
    assert resp.status_code == 400
    assert "password" in resp.get_json().get('error', '').lower()


def test_mongodb_sql_connection_actually_dispatches_to_mongodb_sql_backend(app_env, mongodb_sql_harness):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "Mongo Conn", "is_custom": True,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # >=1 rather than an exact count - see test_config_mysql.py's matching
    # test for why (the /api/config POST above also triggers its own
    # best-effort identity-label probe connect()).
    assert len(mongodb_sql_harness.calls) >= 1
    # connect() reassembles the four descriptor fields and injects the
    # canonical Driver= clause before handing the string to pyodbc - the
    # harness patches pyodbc.connect itself, so it sees that full string,
    # not any single field alone.
    assert mongodb_sql_harness.calls[-1]["url"] == _FULL_ODBC_URL_WITH_DRIVER


def test_write_statement_is_rejected_not_dispatched_to_the_driver(app_env, monkeypatch):
    # The one thing genuinely unique to this dialect: MongoDB Atlas SQL has
    # no write path at all (see backends/mongodb_sql.py's module
    # docstring), enforced defensively in execute() before the statement
    # ever reaches pyodbc - this is the end-to-end version of
    # test_mongodb_sql_backend.py's unit-level read-only tests.
    #
    # Deliberately does NOT use the shared mongodb_sql_harness fixture here:
    # that harness's connect() returns a bare object() (fine for the
    # "dispatches with the right kwargs" test above, which never looks at
    # /api/execute's response body) - but MongoSqlBackend.execute() calls
    # connection.cursor() before it ever reaches the per-statement
    # read-only check, so a connection with no working cursor() would raise
    # an unrelated AttributeError first and this test would never actually
    # exercise the read-only rejection it's meant to cover. A minimal local
    # fake with a working cursor()/close() (never executed against, since
    # _reject_if_not_read_only raises before cursor.execute() is called)
    # is enough.
    import backends.mongodb_sql as mongodb_sql_module

    class _FakeCursor:
        def close(self):
            pass

    class _FakeConnection:
        def cursor(self):
            return _FakeCursor()

        def close(self):
            pass

        def getinfo(self, info_type):
            return None

    monkeypatch.setattr(mongodb_sql_module.pyodbc, "connect", lambda *a, **kw: _FakeConnection())

    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "Mongo Conn", "is_custom": True,
    })
    resp = app_env.client.post('/api/execute', json={"sql": "DELETE FROM orders"})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert "read-only" in data['error']


# --- admin preset --------------------------------------------------------------

def test_mongodb_sql_preset_selectable_and_reported_active(app_factory, tmp_path):
    path = write_database_presets_file(tmp_path, [
        {"type": "MongoDB", "name": "Analytics (Mongo)", "url": _URI,
         "database": _DATABASE, "user": _USER, "password": _PASSWORD},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")

    # The auto-derived preset id (no explicit "id" given) falls back to
    # "{lowercased type}+{name}" - see app_config.py's preset-loading loop -
    # so it's "mongodb+..." here even though the stored "type" itself is
    # the canonical "MongoDB". The id is just an opaque lookup key, never
    # parsed back into a type value anywhere in this codebase.
    resp = env.client.post('/api/config', json={"preset_id": "mongodb+Analytics (Mongo)"})
    assert resp.status_code == 200

    data = env.client.get('/api/config').get_json()
    # active_database_type is blanked for an active preset (redacted, same
    # as any other admin preset) - type is confirmed via configured_databases
    # and active_preset_id instead, same pattern test_config_mysql.py's
    # equivalent test uses.
    assert data['active_preset_id'] == "mongodb+Analytics (Mongo)"
    assert data['active_database_type'] == ""
    assert data['active_is_custom'] is False
    assert data['configured_databases'][0]['type'] == "MongoDB"


def test_mongodb_sql_preset_missing_a_required_field_is_skipped_with_a_warning(app_factory, tmp_path, caplog):
    # Same "requires all four fields" validation as a custom connection,
    # but enforced at preset-load time in app_config.py instead - a preset
    # author who forgets one of url/database/user/password gets a logged
    # warning and the preset simply doesn't appear, rather than a startup
    # crash or a preset that can never actually connect.
    path = write_database_presets_file(tmp_path, [
        {"type": "MongoDB", "name": "Incomplete Mongo", "url": _URI, "database": _DATABASE, "user": _USER},
    ])
    env = app_factory(env={"DATABASE_PRESETS_FILE": path})
    login_as(env.client, "alice@example.com")
    data = env.client.get('/api/config').get_json()
    # The incomplete Mongo preset never made it into CONFIGURED_DBS at all
    # (not even redacted) - only the app's own fallback default remains.
    assert all(db['type'] != 'MongoDB' for db in data['configured_databases'])


# --- anonymous users -------------------------------------------------------------

def test_anonymous_user_can_save_a_custom_mongodb_sql_connection(app_factory, monkeypatch):
    from helpers import install_fake_pyodbc_connect
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com"})
    install_fake_pyodbc_connect(monkeypatch)
    resp = env.client.post('/api/config', json={
        "database_type": "MongoDB", "database_url": _URI,
        "database": _DATABASE, "user": _USER, "password": _PASSWORD,
        "database_name": "My Mongo DB", "is_custom": True,
    })
    assert resp.status_code == 200
    data = env.client.get('/api/config').get_json()
    assert data['custom_databases'][0]['type'] == 'MongoDB'
