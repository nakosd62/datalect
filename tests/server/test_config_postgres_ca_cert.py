"""
The ca_cert_pem field through /api/config: a config_routes.py-level
addition shared by both "simple URL" dialects (see backends/mysql.py's
module docstring) - Postgres and MySQL alike. Unlike password/
credentials_json/access_token, a CA certificate is not a credential (see
config_routes.py's module docstring and state_store.py's
_CREDENTIAL_CONFIG_FIELDS) - it's public information used to populate
libpq's "sslrootcert" (Postgres) or an ssl.SSLContext's trusted CA (MySQL)
for a "sslmode=verify-ca"/"verify-full" connection (see
backends/postgres.py's and backends/mysql.py's module docstrings), so it's
never stripped/redacted and always round-trips back to the frontend as-is,
unlike every credential field covered by test_config_custom_connections.py.

This file focuses on config_routes.py's parsing/storage of the field
(single-connection and custom_databases-list forms) plus one true
end-to-end Postgres dispatch test. The mirror-image MySQL dispatch test,
and the dialect-specific sslmode->driver-kwargs mapping for each backend,
live in test_mysql_backend.py/test_postgres_backend.py (connect()-level
unit coverage) and test_config_mysql.py (MySQL's own end-to-end coverage).
"""

from helpers import login_as

CA_CERT_PEM = "-----BEGIN CERTIFICATE-----\nFAKEFAKEFAKE\n-----END CERTIFICATE-----"
OTHER_CA_CERT_PEM = "-----BEGIN CERTIFICATE-----\nDIFFERENTFAKE\n-----END CERTIFICATE-----"


def test_custom_postgres_connection_persists_ca_cert_pem(app_env):
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True, "ca_cert_pem": CA_CERT_PEM,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 1
    assert data['custom_databases'][0]['config']['ca_cert_pem'] == CA_CERT_PEM


def test_ca_cert_pem_is_not_a_credential_and_always_round_trips(app_env):
    # The regression this test guards: unlike password/credentials_json,
    # get_db_connections' _strip_credentials must NOT touch ca_cert_pem -
    # it's public information, not a secret (see module docstring).
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True, "ca_cert_pem": CA_CERT_PEM,
    })
    # Two separate GETs (a fresh request each time, same as a real page
    # reload) - a stripped/one-time-only field would only survive the
    # first.
    for _ in range(2):
        data = app_env.client.get('/api/config').get_json()
        assert data['custom_databases'][0]['config']['ca_cert_pem'] == CA_CERT_PEM


def test_postgres_connection_without_ca_cert_pem_still_saves_fine(app_env):
    # Regression guard: ca_cert_pem is optional - a plain connection with
    # no such field at all (the overwhelming common case) must keep
    # working exactly as it did before this feature existed.
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h:5432/db",
        "database_name": "PG Conn", "is_custom": True,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert 'ca_cert_pem' not in data['custom_databases'][0]['config']


def test_custom_databases_list_form_persists_ca_cert_pem_for_both_simple_url_dialects(app_env):
    # ca_cert_pem is shared by Postgres and MySQL (both "simple URL"
    # dialects - see backends/mysql.py's module docstring) - a mixed batch
    # containing one of each must persist ca_cert_pem for both, and each
    # row's own value must stay independent of the other's (not merged/
    # confused across rows).
    login_as(app_env.client, "alice@example.com")
    payload = [
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://u:p@h/pgdb", "ca_cert_pem": CA_CERT_PEM},
        {"type": "mysql", "name": "MySQL Conn", "url": "mysql://u:p@h/mysqldb", "ca_cert_pem": OTHER_CA_CERT_PEM},
    ]
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/pgdb",
        "database_name": "PG Conn", "is_custom": True, "custom_databases": payload,
    })
    assert resp.status_code == 200

    data = app_env.client.get('/api/config').get_json()
    assert len(data['custom_databases']) == 2
    by_name = {c["name"]: c for c in data['custom_databases']}
    assert by_name["PG Conn"]["config"]["ca_cert_pem"] == CA_CERT_PEM
    assert by_name["MySQL Conn"]["config"]["ca_cert_pem"] == OTHER_CA_CERT_PEM


def test_connect_dispatches_ca_cert_pem_through_to_backend_connect(app_env, postgres_harness):
    # The real end-to-end check: not just that config_routes.py stores
    # ca_cert_pem, but that it actually reaches
    # backends.postgres.PostgresBackend.connect() as "sslrootcert" on a
    # real /api/execute call - see test_postgres_backend.py for the
    # connect()-level unit coverage of the tempfile plumbing itself.
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://alice:secret@dbhost:5432/salesdb",
        "database_name": "PG Conn", "is_custom": True, "ca_cert_pem": CA_CERT_PEM,
    })
    app_env.client.post('/api/execute', json={"sql": "SELECT 1;"})
    # >=1 rather than an exact count: the /api/config POST above also
    # triggers its own best-effort connect() for schema/identity purposes -
    # see test_config_mysql.py's matching comment for the equivalent MySQL
    # case. The assertion only cares that /api/execute's own connect() call
    # carried ca_cert_pem through correctly.
    assert len(postgres_harness.calls) >= 1
    _, kwargs = postgres_harness.calls[-1]
    assert kwargs.get("sslrootcert")
    call_index = len(postgres_harness.calls) - 1
    assert postgres_harness.sslrootcert_contents[call_index] == CA_CERT_PEM
