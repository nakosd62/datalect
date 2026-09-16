"""
Saving a custom connection through /api/config, when the resulting active
connection differs from what was active before (a genuinely different
connection selected, or the SAME connection's own fields edited - e.g. a
new ca_cert_pem with the same url/host/dbname), doesn't just drop the old
cached schema and leave it to be lazily refetched on the next
/api/translate call. It immediately (synchronously, as part of the
/api/config request) refetches a fresh one - see config_routes.py's
"The DB connection is changing" branch, which now calls
db.prime_schema_cache() right after invalidate_schema_cache().

Critically: if that immediate refetch fails (DB unreachable, bad
credentials), NO schema is left cached for that connection - not the
stale one from before the change, and not a failure placeholder either.
invalidate_schema_cache() runs BEFORE the refetch attempt specifically to
guarantee this: a failed prime_schema_cache() call caches nothing on its
own (see db.py), so without the prior invalidation an old, no-longer-
applicable entry would otherwise be left silently serving stale answers
forever (schema_cache.py has no TTL - see its own module docstring).

These tests monkeypatch db.get_backend (the same seam
tests/server/test_db_schema_fetch.py's _install_fake_backend uses) so a
postgres custom connection's schema fetch is fully controllable without
a real database.
"""

from helpers import login_as


class _FakeBackend:
    def __init__(self, schema_text):
        self._schema_text = schema_text
        self.get_schema_calls = 0

    def connect(self, descriptor):
        return object()

    def get_schema(self, connection):
        self.get_schema_calls += 1
        return self._schema_text

    def get_schema_shallow(self, connection):
        return self._schema_text

    def cache_key(self, descriptor):
        # Mirrors backends/postgres.py's real cache_key shape closely
        # enough for these tests: identical url -> identical cache_key,
        # so a ca_cert_pem-only change (same url) lands on the SAME
        # cache_key, which is exactly the scenario that matters here.
        return descriptor.get("url") or "unknown"

    def close(self, connection):
        pass


def _install_fake_backend(monkeypatch, app_env, schema_text):
    import db as db_module
    fake = _FakeBackend(schema_text)
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)
    return db_module, fake


def test_saving_a_new_custom_connection_immediately_caches_a_fresh_schema(app_env, monkeypatch):
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="Table: t\n  id integer NOT NULL")

    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })

    assert resp.status_code == 200
    # The schema was fetched right away, as part of this save - not left
    # for a later /api/translate call to warm lazily.
    assert fake.get_schema_calls == 1
    import schema_cache
    cache_key = db_module.get_conn_identifier({"type": "postgres", "url": "postgresql://u:p@h/db1"})
    assert schema_cache.get(cache_key) == "Table: t\n  id integer NOT NULL"


def test_a_config_only_change_with_the_same_url_replaces_the_cached_schema(app_env, monkeypatch):
    # Same host/dbname (so the cache_key is unchanged - see
    # _FakeBackend.cache_key above), only ca_cert_pem differs. This is
    # exactly the case the module docstring calls out: no url change to
    # naturally land on a fresh cache_key, so invalidate+refetch is the
    # only thing that keeps the schema from silently going stale.
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="OLD SCHEMA")

    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })
    import schema_cache
    cache_key = db_module.get_conn_identifier({"type": "postgres", "url": "postgresql://u:p@h/db1"})
    assert schema_cache.get(cache_key) == "OLD SCHEMA"

    fake._schema_text = "NEW SCHEMA"
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True, "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
    })

    assert resp.status_code == 200
    assert fake.get_schema_calls == 2
    # The cache now holds the freshly-refetched schema, not the old one.
    assert schema_cache.get(cache_key) == "NEW SCHEMA"


def test_a_failed_refetch_after_config_change_leaves_no_schema_cached_at_all(app_env, monkeypatch):
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="OLD SCHEMA")

    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })
    import schema_cache
    cache_key = db_module.get_conn_identifier({"type": "postgres", "url": "postgresql://u:p@h/db1"})
    assert schema_cache.get(cache_key) == "OLD SCHEMA"

    # The config changes, but the DB happens to be unreachable/broken now.
    fake._schema_text = None
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True, "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
    })

    # The save itself still succeeds - a schema-refetch failure doesn't
    # fail the connection save.
    assert resp.status_code == 200
    # But the cache is empty for this connection now - NOT the old
    # "OLD SCHEMA" value, and nothing else either.
    assert schema_cache.get(cache_key) is None


def test_a_failed_refetch_is_logged_but_does_not_raise(app_env, monkeypatch, caplog):
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="OLD SCHEMA")

    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })

    fake._schema_text = None
    with caplog.at_level("WARNING"):
        resp = app_env.client.post('/api/config', json={
            "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
            "database_name": "DB1", "is_custom": True, "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
        })

    assert resp.status_code == 200
    assert any("Schema refetch failed" in r.getMessage() for r in caplog.records)
