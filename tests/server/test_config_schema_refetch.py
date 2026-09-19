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

The flip side matters just as much, and is what most of this file actually
covers: merely RE-SELECTING an already-active custom connection - the
exact same connection, nothing edited - must NOT re-trigger any of this.
Regression coverage for a real bug (see
test_reselecting_the_same_structured_dialect_connection_does_not_refetch
below): for the 7 "structured" dialects with no real url of their own
(BigQuery/Snowflake/Databricks/Oracle/Redshift/MSSQL/Sheets - see this
module's own docstring on _STRUCTURED_DIALECTS_WITHOUT_A_REAL_URL),
config_routes.py's "is the connection changing" check used to compare the
freshly-parsed request's own internal synthetic identity string (always
non-None once a connection is fully identified) against
prior_descriptor["url"] (always None for these same dialects - that's
what's actually persisted/resolved back for them). Those two could never
be equal, so this branch fired on literally EVERY save that touched one of
these connections, changed or not - invalidating and synchronously
refetching the full schema (plus the schema-overview LLM call) purely from
re-selecting the same, already-cached connection. Postgres/MySQL never hit
this (their url IS the real, persisted value on both sides of the
comparison), which is why it went unnoticed for those two dialects.

These tests monkeypatch db.get_backend (the same seam
tests/server/test_db_schema_fetch.py's _install_fake_backend uses) so a
postgres custom connection's schema fetch is fully controllable without
a real database.
"""

import sys

from helpers import login_as, SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from config_routes import _describe_config_diff


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


def _wait_until(condition, timeout=2.0, interval=0.01):
    """Blocks until `condition()` is truthy, or raises after `timeout`
    seconds - used below wherever a test needs to observe the effect of
    config_routes.py's background schema-(re)fetch thread (handle_config()'s
    threading.Thread(..., daemon=True).start(), started right before the
    /api/config response is returned - see that call site's own comment on
    why it's backgrounded at all).

    Before schema_cache.py's fetch-pending/schema-text bookkeeping became
    fully durable (state_store-backed, no in-memory copy at all - see that
    module's own docstring), that background thread's work was fast enough
    (pure in-memory dict writes) that asserting on its result immediately
    after client.post() returns almost always "worked" anyway, by luck of
    thread scheduling. Now every one of those writes is a real state_store
    round trip (a fresh sqlite3.connect() per call locally, or a network
    call to Firestore in production - see SqliteStateStore._connect()/
    FirestoreStateStore), which is still fast in absolute terms but no
    longer fast enough to treat as instantaneous relative to the main
    thread reaching the very next line of test code - this is a real,
    intermittent race now, not just a theoretical one (confirmed by
    running the affected tests repeatedly before adding this helper).
    Polling for the actual observable side effect (here: the fake
    backend's own call count, never a timing assumption) is the correct
    fix, not a workaround - it's the same "wait for it to actually be
    done" contract webClient's own /api/config/schema-fetch-status polling
    loop already has to follow for exactly the same reason."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError("Timed out waiting for the background schema fetch to finish")


def test_saving_a_new_custom_connection_immediately_caches_a_fresh_schema(app_env, monkeypatch):
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="Table: t\n  id integer NOT NULL")

    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })

    assert resp.status_code == 200
    # The schema was fetched right away, as part of this save (backgrounded,
    # not blocking the response itself - see _wait_until's own docstring) -
    # not left for a later /api/translate call to warm lazily. Waiting on
    # schema_cache.get() itself (not fake.get_schema_calls, which
    # increments a step earlier, before the durable schema_cache.set()
    # call that follows it) is what this test actually cares about.
    import schema_cache
    cache_key = db_module.get_conn_identifier({"type": "postgres", "url": "postgresql://u:p@h/db1"})
    _wait_until(lambda: schema_cache.get(cache_key) is not None)
    assert fake.get_schema_calls == 1
    assert schema_cache.get(cache_key) == "Table: t\n  id integer NOT NULL"


class _FakeStructuredDialectBackend(_FakeBackend):
    """Same fake as above, but keyed like a real structured-dialect backend
    (account+database, never a url - see backends/snowflake.py's own
    cache_key()) rather than _FakeBackend's own url-based one, since url is
    always None for this whole dialect family and every connection here
    would otherwise collide on the same "unknown" cache key."""

    def cache_key(self, descriptor):
        return f"{descriptor.get('account')}/{descriptor.get('database')}"


def test_reselecting_the_same_structured_dialect_connection_does_not_refetch(app_env, monkeypatch):
    # Snowflake stands in for the whole "no real url" dialect family here
    # (BigQuery/Databricks/Oracle/Redshift/MSSQL/Sheets all share the exact
    # same new_db_url-is-a-synthetic-identity-string shape - see this
    # module's own docstring and _parse_incoming_connection's in
    # config_routes.py).
    login_as(app_env.client, "alice@example.com")
    import db as db_module
    fake = _FakeStructuredDialectBackend("Table: t\n  id integer NOT NULL")
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)

    body = {
        "database_type": "snowflake", "is_custom": True,
        "database_name": "SF1",
        "account": "acct1", "user": "u1", "warehouse": "wh1", "database": "db1",
        "password": "secretpw",
    }
    resp1 = app_env.client.post('/api/config', json=body)
    assert resp1.status_code == 200
    _wait_until(lambda: fake.get_schema_calls == 1)

    # Re-selecting the SAME saved connection, exactly as the real client
    # does: the password is never redisplayed, so it's simply omitted here
    # (config_routes.py's _resolve_snowflake_credentials falls back to the
    # already-saved one - this isn't what used to break).
    reselect_body = dict(body)
    reselect_body.pop("password")
    resp2 = app_env.client.post('/api/config', json=reselect_body)
    assert resp2.status_code == 200
    assert fake.get_schema_calls == 1  # NOT re-fetched - nothing changed
    assert resp2.get_json()["schema_fetch_pending"] is False

    # And once more with the password resent too, byte-for-byte identical
    # to the very first save - still no refetch.
    resp3 = app_env.client.post('/api/config', json=body)
    assert resp3.status_code == 200
    assert fake.get_schema_calls == 1
    assert resp3.get_json()["schema_fetch_pending"] is False


def test_switching_to_a_preset_and_back_to_an_unmodified_custom_connection_does_not_refetch(app_env, monkeypatch):
    # The actual real-world bug this covers (confirmed against a real
    # server.log): selecting a custom connection (a correct, expected
    # refetch, since it's the first activation), then switching to a
    # PRESET, then switching BACK to the exact same, unmodified custom
    # connection - that last step must NOT refetch either, since nothing
    # about the custom connection itself ever changed. The previous
    # comparison (against resolve_active_descriptor's result, i.e.
    # whatever connection was ACTIVE a moment ago) got this backwards: it
    # compared the custom connection's fields against the PRESET's
    # descriptor - two unrelated connections - which are of course
    # "different", so it invalidated and live-refetched the custom
    # connection's already-cached, still-valid schema on every single
    # return visit. The fix compares against this connection's own
    # previously-saved state instead (matched by connection_key), which
    # is unaffected by whatever else was active in between.
    login_as(app_env.client, "alice@example.com")
    import db as db_module
    fake = _FakeStructuredDialectBackend("Table: t\n  id integer NOT NULL")
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)

    body = {
        "database_type": "snowflake", "is_custom": True,
        "database_name": "SF1",
        "account": "acct1", "user": "u1", "warehouse": "wh1", "database": "db1",
        "password": "secretpw",
    }
    resp1 = app_env.client.post('/api/config', json=body)
    assert resp1.status_code == 200
    _wait_until(lambda: fake.get_schema_calls == 1)

    # Switch away to a preset (the default synthetic "Default DB" preset
    # app_env always has - see conftest.py) - matches the user's own "select
    # a preset dataset" step, which correctly does NOT refetch anything on
    # its own (no invalidate/prime call in the preset branch at all).
    get_resp = app_env.client.get('/api/config')
    preset_id = get_resp.get_json()["configured_databases"][0]["id"]
    resp_preset = app_env.client.post('/api/config', json={"preset_id": preset_id})
    assert resp_preset.status_code == 200
    calls_after_preset_switch = fake.get_schema_calls

    # Switch BACK to the exact same, unmodified Snowflake connection -
    # this must not trigger a new live fetch.
    reselect_body = dict(body)
    reselect_body.pop("password")
    resp2 = app_env.client.post('/api/config', json=reselect_body)
    assert resp2.status_code == 200
    assert fake.get_schema_calls == calls_after_preset_switch  # NOT re-fetched
    assert resp2.get_json()["schema_fetch_pending"] is False


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
    # Wait on schema_cache.get() itself, not fake.get_schema_calls - the
    # fake's own call count increments a step before schema_cache.set()
    # durably writes it (see get_database_schema_with_reason() in db.py),
    # so that's not yet true when this test actually needs it to be.
    _wait_until(lambda: schema_cache.get(cache_key) is not None)
    assert schema_cache.get(cache_key) == "OLD SCHEMA"

    fake._schema_text = "NEW SCHEMA"
    resp = app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True, "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
    })

    assert resp.status_code == 200
    _wait_until(lambda: schema_cache.get(cache_key) == "NEW SCHEMA")
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
    _wait_until(lambda: schema_cache.get(cache_key) is not None)
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
    # invalidate_schema_cache() runs synchronously, inline in the request,
    # before the (backgrounded) refetch attempt - so the cache is already
    # empty for this connection by the time the response comes back,
    # regardless of whether the background refetch attempt has finished
    # yet (it fails either way - see fake._schema_text above). NOT the old
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
        # The "Schema refetch failed" warning is logged by the background
        # thread itself, after prime_schema_cache() (and its own internal
        # mark_fetch_done()) has already returned - polling directly for
        # the log line itself (rather than a proxy signal like
        # fake.get_schema_calls, which increments a couple of steps
        # earlier, before mark_fetch_done()'s own durable write and the
        # logger.warning() call that follows it) is what actually makes
        # this deterministic - see _wait_until's own docstring for why
        # this can't be assumed to have already happened just because the
        # HTTP response came back.
        _wait_until(lambda: any("Schema refetch failed" in r.getMessage() for r in caplog.records))

    assert any("Schema refetch failed" in r.getMessage() for r in caplog.records)


# --- _describe_config_diff() ---------------------------------------------
# The diagnostic log line the "connection is changing" branch above now
# emits every time it fires (see that branch's own comment) is only useful
# if it actually names the right field(s) and never leaks a credential -
# tested directly here, independent of a full /api/config round trip.

def test_describe_config_diff_reports_no_op_when_configs_are_identical():
    config = {"host": "h", "port": 5439, "database": "db1", "user": "u1", "password": "secretpw"}
    assert _describe_config_diff(config, dict(config)) == "(no config key differs - url alone changed)"


def test_describe_config_diff_names_a_changed_non_credential_field():
    prior = {"host": "h", "port": 5439, "database": "db1", "user": "u1"}
    new = {"host": "h", "port": 5440, "database": "db1", "user": "u1"}
    diff = _describe_config_diff(prior, new)
    assert diff == "port: 5439 -> 5440"


def test_describe_config_diff_names_an_added_and_a_removed_field():
    prior = {"host": "h", "database": "db1", "user": "u1"}
    new = {"host": "h", "database": "db1", "user": "u1", "schema": "reporting"}
    diff = _describe_config_diff(prior, new)
    assert diff == "schema: <absent> -> 'reporting'"

    diff_reverse = _describe_config_diff(new, prior)
    assert diff_reverse == "schema: 'reporting' -> <absent>"


def test_describe_config_diff_never_includes_a_raw_credential_value():
    # Every _CREDENTIAL_CONFIG_FIELDS-equivalent name this module tracks -
    # a changed, added, or removed credential must show up as a named key
    # so the log line still says WHICH field changed, but the value itself
    # (on either side) must never appear in it.
    prior = {
        "host": "h", "password": "old-secret", "credentials_json": '{"a": "b"}',
        "private_key": "old-key", "private_key_passphrase": "old-pass", "access_token": "old-tok",
    }
    new = {
        "host": "h", "password": "new-secret", "credentials_json": '{"c": "d"}',
        "private_key": "new-key", "private_key_passphrase": "new-pass", "access_token": "new-tok",
    }
    diff = _describe_config_diff(prior, new)
    for field in ("password", "credentials_json", "private_key", "private_key_passphrase", "access_token"):
        assert f"{field}: <redacted> -> <redacted>" in diff
    for secret in ("old-secret", "new-secret", "old-key", "new-key", "old-pass", "new-pass",
                   "old-tok", "new-tok", '{"a": "b"}', '{"c": "d"}'):
        assert secret not in diff


def test_describe_config_diff_handles_none_configs():
    assert _describe_config_diff(None, None) == "(no config key differs - url alone changed)"
    assert _describe_config_diff(None, {"host": "h"}) == "host: <absent> -> 'h'"


# --- The diagnostic log line itself, end to end via /api/config ----------

def test_connection_changed_log_line_names_the_field_that_actually_changed(app_env, monkeypatch, caplog):
    # Same scenario as test_a_config_only_change_with_the_same_url_replaces_
    # the_cached_schema above (a ca_cert_pem-only change on an otherwise
    # unmodified Postgres connection) - this just additionally asserts the
    # new diagnostic log line names "ca_cert_pem" specifically, not just
    # that a refetch happened.
    login_as(app_env.client, "alice@example.com")
    db_module, fake = _install_fake_backend(monkeypatch, app_env, schema_text="OLD SCHEMA")

    app_env.client.post('/api/config', json={
        "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
        "database_name": "DB1", "is_custom": True,
    })
    # That first save is itself a genuine "connection changing" event (from
    # whatever was previously active to this brand-new one) and trips the
    # same log line - cleared here so only the SECOND request's own record
    # is asserted on below.
    caplog.clear()

    with caplog.at_level("INFO"):
        resp = app_env.client.post('/api/config', json={
            "database_type": "postgres", "database_url": "postgresql://u:p@h/db1",
            "database_name": "DB1", "is_custom": True,
            "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
        })

    assert resp.status_code == 200
    matches = [r for r in caplog.records if "Connection-changed check tripped" in r.getMessage()]
    assert len(matches) == 1
    assert "ca_cert_pem" in matches[0].getMessage()


def test_connection_changed_log_line_is_absent_on_an_unmodified_reselect(app_env, monkeypatch, caplog):
    # The flip side of the test above: reselecting a structured-dialect
    # connection with nothing actually changed (the exact regression
    # test_reselecting_the_same_structured_dialect_connection_does_not_
    # refetch above covers) must NOT emit this diagnostic line at all -
    # it only fires inside the branch that decided something changed.
    login_as(app_env.client, "alice@example.com")
    import db as db_module
    fake = _FakeStructuredDialectBackend("Table: t\n  id integer NOT NULL")
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)

    body = {
        "database_type": "snowflake", "is_custom": True,
        "database_name": "SF1",
        "account": "acct1", "user": "u1", "warehouse": "wh1", "database": "db1",
        "password": "secretpw",
    }
    app_env.client.post('/api/config', json=body)
    # That first save is itself a genuine "connection changing" event and
    # trips the log line too (expected) - cleared here so only the
    # reselect's own (lack of) log output is asserted on below.
    caplog.clear()

    reselect_body = dict(body)
    reselect_body.pop("password")
    with caplog.at_level("INFO"):
        resp = app_env.client.post('/api/config', json=reselect_body)

    assert resp.status_code == 200
    assert not any("Connection-changed check tripped" in r.getMessage() for r in caplog.records)
