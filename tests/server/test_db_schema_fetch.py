"""
db.py's _fetch_database_schema(): the thin wrapper around
backend.get_schema() that every dialect's own backend module funnels
through. Every backend's get_schema() (see e.g. backends/postgres.py's
"if not all_table_names: return None") treats "connected fine, ran the
introspection queries fine, there's just nothing to describe (e.g. a
schema made up entirely of views, with zero BASE TABLEs)" as a normal,
non-exceptional None/"" return - NOT an error. Before the fix this
covers, that fell through to _SCHEMA_FETCH_FAILED completely silently:
logger.exception is only ever reached from the `except Exception` branch
below it, which this path never raises into. A real-world case (a
Postgres schema made entirely of views) hit exactly this path and left
nothing in the logs at all - "no schema" with nothing to explain why.
"""

from helpers import SERVER_DIR

import sys
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


class _FakeBackend:
    """Stands in for a real dialect backend - only the methods
    _fetch_database_schema/get_conn_identifier actually call.

    get_schema_shallow returns a distinct, separately-controllable text
    from get_schema (deep) - by default derived from the deep text so a
    test that doesn't care about the shallow/deep distinction can still
    tell, from the returned string alone, which method actually got
    called (see the "deep=False dispatches to get_schema_shallow, not
    get_schema" tests below)."""

    def __init__(self, schema_text, raise_on_get_schema=None, shallow_schema_text=None):
        self._schema_text = schema_text
        self._raise_on_get_schema = raise_on_get_schema
        self._shallow_schema_text = (
            shallow_schema_text if shallow_schema_text is not None
            else (f"[shallow] {schema_text}" if schema_text else schema_text)
        )
        self.connect_calls = []
        self.closed_connections = []
        self.get_schema_calls = 0
        self.get_schema_shallow_calls = 0
        self.dialect_name = "SQL"

    def connect(self, descriptor):
        self.connect_calls.append(descriptor)
        return object()

    def get_schema(self, connection):
        self.get_schema_calls += 1
        if self._raise_on_get_schema:
            raise self._raise_on_get_schema
        return self._schema_text

    def get_schema_shallow(self, connection):
        self.get_schema_shallow_calls += 1
        if self._raise_on_get_schema:
            raise self._raise_on_get_schema
        return self._shallow_schema_text

    def cache_key(self, descriptor):
        return "fake-user@fake-host/fake-db"

    def close(self, connection):
        self.closed_connections.append(connection)


def _install_fake_backend(monkeypatch, schema_text=None, raise_on_get_schema=None, shallow_schema_text=None):
    import db as db_module
    fake = _FakeBackend(schema_text, raise_on_get_schema=raise_on_get_schema, shallow_schema_text=shallow_schema_text)
    monkeypatch.setattr(db_module, "get_backend", lambda descriptor: fake)
    return db_module, fake


def test_none_schema_text_logs_a_warning_naming_the_connection(app_factory, monkeypatch, caplog):
    app_factory()  # establishes db.py's own module-level deps (app_config etc.)
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "fake-user@fake-host/fake-db" in warnings[0].getMessage()
    # No exception was raised, so the pre-existing logger.exception branch
    # must NOT also fire for this path - only the new warning.
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_empty_string_schema_text_also_logs_a_warning(app_factory, monkeypatch, caplog):
    # "" is just as falsy as None and hits the exact same branch - a
    # backend could plausibly return either for "nothing to describe".
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="")

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_real_schema_text_logs_no_warning(app_factory, monkeypatch, caplog):
    # The common, successful case must stay exactly as quiet as before -
    # this fix only adds visibility for the previously-silent failure
    # path, not new log noise for every ordinary schema fetch.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: customers\n  id integer NOT NULL")

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == "Table: customers\n  id integer NOT NULL"
    assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)


def test_real_exception_still_logs_via_exception_not_the_new_warning(app_factory, monkeypatch, caplog):
    # Regression guard: a genuine connection/query failure must still take
    # the pre-existing `except Exception` -> logger.exception(...) path,
    # not get reclassified as the new "no schema text" warning just
    # because both end up returning _SCHEMA_FETCH_FAILED.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, raise_on_get_schema=RuntimeError("connection reset"))

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == db_module._SCHEMA_FETCH_FAILED
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


# --- SCHEMA_MAX_CHARS truncation visibility ------------------------------------
#
# Companion fix to the "no schema text" warning above, for the OTHER
# previously-silent case: cap_schema_text() (backends/base.py) already
# embeds a truncation note directly in the schema text itself, so the
# model always sees it, but nothing recorded server-side which connection
# actually hits the SCHEMA_MAX_CHARS ceiling, or how often. These use a
# real cap_schema_text() call to produce a genuinely truncated schema
# string (rather than hand-writing the marker text), so a future change to
# the marker's exact wording can't silently desync these tests from what
# schema_text_was_truncated() actually recognizes.

def test_truncated_schema_text_logs_a_warning_naming_the_connection(app_factory, monkeypatch, caplog):
    app_factory()
    from backends.base import cap_schema_text
    truncated_text = cap_schema_text("A" * 200, max_chars=50)
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=truncated_text)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == truncated_text  # unchanged - this is a visibility fix, not a behavior change
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "fake-user@fake-host/fake-db" in warnings[0].getMessage()
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_untruncated_schema_text_logs_no_warning(app_factory, monkeypatch, caplog):
    # Regression guard against the obvious false-positive: ordinary schema
    # text that happens to be long, but never actually hit
    # SCHEMA_MAX_CHARS, must not be misreported as truncated.
    app_factory()
    from backends.base import cap_schema_text
    untruncated_text = cap_schema_text("Table: customers\n  id integer NOT NULL", max_chars=10_000)
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=untruncated_text)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == untruncated_text
    assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)


# --- SCHEMA_MAX_TABLES omission visibility --------------------------------------
#
# Companion fix to the SCHEMA_MAX_CHARS one above, for the OTHER cap: a
# schema that has more tables than SCHEMA_MAX_TABLES allows gets an
# "N more table(s)... not shown" note appended by the backend itself (see
# e.g. backends/postgres.py's own `if omitted_count:` block), but nothing
# recorded server-side which connection actually hits that ceiling, or how
# often. These use the real marker text schema_text_has_omitted_tables()
# recognizes (rather than a full cap_kept_tables() call, since the note
# text itself is built individually by each backend, not by cap_kept_tables)
# so a future rewording that keeps the shared "more table(s)" substring
# can't silently desync these tests from the real backends' output.

def test_omitted_tables_schema_text_logs_a_warning_naming_the_connection(app_factory, monkeypatch, caplog):
    app_factory()
    schema_text_with_omissions = (
        "Table: a\n  id integer NOT NULL\n\n"
        "[... 5 more table(s)/table-family(ies) not shown - this schema has "
        "more than the 200-table summary limit. Ask about a narrower set of "
        "tables to see the rest.]"
    )
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=schema_text_with_omissions)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == schema_text_with_omissions  # unchanged - this is a visibility fix, not a behavior change
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "fake-user@fake-host/fake-db" in warnings[0].getMessage()
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_schema_text_with_every_table_shown_logs_no_warning(app_factory, monkeypatch, caplog):
    # Regression guard against the obvious false-positive: an ordinary
    # schema that described every table it found (no omission note) must
    # not be misreported as having omitted tables.
    app_factory()
    complete_text = "Table: a\n  id integer NOT NULL\n\nTable: b\n  id integer NOT NULL"
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=complete_text)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == complete_text
    assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)


def test_schema_text_with_both_kinds_of_truncation_logs_two_independent_warnings(app_factory, monkeypatch, caplog):
    # Regression guard on the if/if (not if/elif) restructuring in
    # _fetch_database_schema: a schema can both omit tables (SCHEMA_MAX_TABLES)
    # AND have its kept text overflow SCHEMA_MAX_CHARS - these are
    # independent causes, so both warnings must fire for the same fetch,
    # not just whichever check happens to come first.
    app_factory()
    # Hand-built rather than routed through a single cap_schema_text() call:
    # a real max_chars cutoff could land inside the omitted-tables note text
    # itself (truncating away the very "more table(s)" substring this test
    # needs to keep), which would only be an artifact of the chosen cutoff
    # point, not a real behavior to pin down. Both marker substrings are
    # each exactly what a real backend + cap_schema_text would produce when
    # both caps are hit for the same schema - this just avoids coupling the
    # test to a specific cutoff length that happens to preserve both.
    both_truncated_text = (
        "Table: a\n  id integer NOT NULL\n\n"
        "[... 5 more table(s)/table-family(ies) not shown - this schema has "
        "more than the 200-table summary limit. Ask about a narrower set of "
        "tables to see the rest.]\n\n"
        "[... schema truncated: exceeded 50,000 characters. Ask about fewer "
        "tables at once, or reduce SCHEMA_MAX_CHARS/SCHEMA_MAX_TABLES scope "
        "on this connection's dataset, to see more of it.]"
    )
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=both_truncated_text)

    with caplog.at_level("WARNING"):
        result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert result == both_truncated_text
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert not any(r.levelname == "ERROR" for r in caplog.records)


# --- deep/shallow schema-fetch split --------------------------------------------
#
# The two-phase schema-introspection architecture (see docs/schema_two_phase_proposal.md
# and the backends/*.py get_schema_shallow() split): _fetch_database_schema
# and get_database_schema both grew a `deep` parameter (default True, so
# every pre-existing caller keeps getting exactly what it always got) that
# picks between backend.get_schema() (deep - Phase 1 + Phase 2) and
# backend.get_schema_shallow() (Phase 1 only) - and get_database_schema
# caches the two under independent keys so a connection can have both
# cached at once without one clobbering the other.

def test_fetch_database_schema_deep_true_calls_get_schema_not_shallow(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")

    result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"}, deep=True)

    assert result == "Table: t\n  id integer NOT NULL"
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 0


def test_fetch_database_schema_deep_false_calls_get_schema_shallow_not_deep(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")

    result = db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"}, deep=False)

    assert result == "[shallow] Table: t\n  id integer NOT NULL"
    assert fake.get_schema_calls == 0
    assert fake.get_schema_shallow_calls == 1


def test_fetch_database_schema_defaults_to_deep(app_factory, monkeypatch):
    # Every caller that existed before this split (single-connection mode,
    # Phase B generation) calls _fetch_database_schema/get_database_schema
    # with no `deep` argument at all - the default must still mean "deep",
    # not silently switch anyone over to the cheaper shallow fetch.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")

    db_module._fetch_database_schema({"type": "postgres", "url": "postgresql://u:p@host/db"})

    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 0


def test_get_database_schema_deep_and_shallow_cache_under_independent_keys(app_factory, monkeypatch):
    # A deep fetch and a shallow fetch for the SAME connection must not
    # share a cache slot - otherwise whichever one ran first would get
    # served back to a caller that asked for the other (e.g. all-dbs
    # triage's cheap shallow fetch silently answering a real generation
    # call that needs the full deep text, or vice versa).
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="DEEP TEXT")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    deep_result = db_module.get_database_schema(descriptor, deep=True)
    shallow_result = db_module.get_database_schema(descriptor, deep=False)

    assert deep_result == "DEEP TEXT"
    assert shallow_result == "[shallow] DEEP TEXT"
    # Each was actually fetched once (not served from the other's cache
    # entry) - one get_schema call, one get_schema_shallow call.
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 1

    # And each is independently cached from here on - calling either again
    # doesn't trigger a second real fetch.
    db_module.get_database_schema(descriptor, deep=True)
    db_module.get_database_schema(descriptor, deep=False)
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 1


def test_invalidate_schema_cache_clears_both_deep_and_shallow_entries(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="DEEP TEXT")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    db_module.get_database_schema(descriptor, deep=True)
    db_module.get_database_schema(descriptor, deep=False)
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 1

    db_module.invalidate_schema_cache(db_module.get_conn_identifier(descriptor))

    # Both entries were dropped - the next call to either re-fetches live
    # rather than serving stale cached text.
    db_module.get_database_schema(descriptor, deep=True)
    db_module.get_database_schema(descriptor, deep=False)
    assert fake.get_schema_calls == 2
    assert fake.get_schema_shallow_calls == 2


def test_build_router_candidate_summaries_reads_only_the_cached_deep_entry_no_live_fetch(
        app_factory, monkeypatch):
    # build_router_candidate_summaries() must NEVER connect to or query a
    # real database - not even the cheaper Phase-1-only introspection.
    # There used to be a genuinely separate, independently-fetched
    # "shallow" cache entry just for this - removed because the deep text
    # was always a superset of it anyway (see db.py's own docstring). Now
    # this reads ONLY whatever deep entry is already sitting in
    # schema_cache, in memory, and reduces it via
    # extract_entry_names_from_schema_text - zero backend calls either way.
    app_factory()
    db_module, fake = _install_fake_backend(
        monkeypatch, schema_text="Table: customers\n  id integer NOT NULL",
    )
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    # Nothing cached yet - degrades to an empty table list, still with no
    # backend call of any kind.
    summaries = db_module.build_router_candidate_summaries(
        [{"name": "My DB", "descriptor": descriptor}], user_id=None,
    )
    assert summaries == [{"name": "My DB", "dialect": "SQL", "table_names": []}]
    assert fake.get_schema_calls == 0
    assert fake.get_schema_shallow_calls == 0

    # Warm the deep cache the ordinary way (a ordinary generation call, or
    # a prefetch/refresh would do this in real life) - now the SAME cached
    # deep text is what triage's summary is derived from.
    db_module.get_database_schema(descriptor)
    assert fake.get_schema_calls == 1

    summaries = db_module.build_router_candidate_summaries(
        [{"name": "My DB", "descriptor": descriptor}], user_id=None,
    )
    assert summaries == [{"name": "My DB", "dialect": "SQL", "table_names": ["customers"]}]
    # Still no shallow call, and no SECOND deep call either - this was a
    # pure cache read, not a fetch of any kind.
    assert fake.get_schema_shallow_calls == 0
    assert fake.get_schema_calls == 1


def test_invalidate_schema_cache_is_safe_when_nothing_was_cached(app_factory):
    # Mirrors schema_cache.invalidate()'s own "safe to call even if not
    # cached" contract - invalidate_schema_cache must not raise just
    # because a connection was never fetched (deep or shallow) at all.
    app_factory()
    import db as db_module
    db_module.invalidate_schema_cache("never-fetched@nowhere/nothing")


# --- every successful fetch is cached indefinitely / prime_schema_cache / --------
# --- startup prefetch ------------------------------------------------------------
#
# schema_cache.py has no TTL/expiry concept at all any more - EVERY
# successful fetch (an ordinary /api/translate lookup, force_refresh=True
# from the in-conversation checkbox, the "Refresh Schema" button, or the
# startup preset prefetch) is cached until something explicitly
# invalidates it or the process restarts. See db.py's
# get_database_schema()/prime_schema_cache()/prefetch_all_preset_schemas()
# docstrings.

def test_an_ordinary_fetch_is_cached_and_never_re_fetched_again(app_factory, monkeypatch):
    # No special flag needed - a plain get_database_schema() call, cached
    # once, is served from cache indefinitely afterward.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    for _ in range(3):
        result = db_module.get_database_schema(descriptor)
        assert result == "Table: t\n  id integer NOT NULL"

    assert fake.get_schema_calls == 1


def test_force_refresh_replaces_the_cached_entry_which_then_stays_cached(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="FIRST")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    assert db_module.get_database_schema(descriptor) == "FIRST"

    fake._schema_text = "SECOND"
    assert db_module.get_database_schema(descriptor, force_refresh=True) == "SECOND"
    assert fake.get_schema_calls == 2

    # The new value now stays cached, exactly like the first one did -
    # nothing reverts it to any kind of shorter-lived state.
    assert db_module.get_database_schema(descriptor) == "SECOND"
    assert fake.get_schema_calls == 2


def test_prime_schema_cache_fetches_and_caches_the_deep_entry_on_success(app_factory, monkeypatch):
    # prime_schema_cache() used to also force-fetch a second, independent
    # "shallow" cache entry here - removed (see db.py's own docstring):
    # build_router_candidate_summaries() no longer reads (or needs) any
    # shallow entry at all, it derives its summary straight from this same
    # deep entry, so there's nothing left for prime_schema_cache to warm
    # besides the deep one.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="DEEP TEXT")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    result = db_module.prime_schema_cache(descriptor)

    assert result is True
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 0
    # The deep entry is now cached - a plain get_database_schema call
    # afterward is served from cache, not re-fetched.
    assert db_module.get_database_schema(descriptor, deep=True) == "DEEP TEXT"
    assert fake.get_schema_calls == 1


def test_prime_schema_cache_returns_false_when_deep_fetch_fails(app_factory, monkeypatch):
    # The caller (the refresh endpoint, or startup prefetch) must be able
    # to tell a failed fetch failed.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    result = db_module.prime_schema_cache(descriptor)

    assert result is False
    assert fake.get_schema_calls == 1
    assert fake.get_schema_shallow_calls == 0


def test_prime_schema_cache_with_reason_reports_empty_when_no_base_tables(app_factory, monkeypatch):
    # get_schema() returning None/"" (no exception) - a views-only or
    # genuinely empty schema - is a completely different situation from a
    # real connect()/query error, and config_routes.py's /api/config/
    # refresh-schema needs to tell them apart to show an accurate message
    # (see its own comment on SCHEMA_FETCH_FAILURE_REASON_EMPTY).
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    success, reason = db_module.prime_schema_cache_with_reason(descriptor)

    assert success is False
    assert reason == db_module.SCHEMA_FETCH_FAILURE_REASON_EMPTY


def test_prime_schema_cache_with_reason_reports_fatal_on_a_non_timeout_exception(app_factory, monkeypatch):
    # "connection reset" mentions neither "timeout" nor "timed out" - a
    # definitive, non-timeout failure, classified FATAL (see
    # _looks_like_timeout_error()'s own docstring).
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, raise_on_get_schema=RuntimeError("connection reset"))
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    success, reason = db_module.prime_schema_cache_with_reason(descriptor)

    assert success is False
    assert reason == db_module.SCHEMA_FETCH_FAILURE_REASON_FATAL


def test_prime_schema_cache_with_reason_reports_timeout_on_a_timeout_error(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, raise_on_get_schema=TimeoutError("connect timed out"))
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    success, reason = db_module.prime_schema_cache_with_reason(descriptor)

    assert success is False
    assert reason == db_module.SCHEMA_FETCH_FAILURE_REASON_TIMEOUT


def test_prime_schema_cache_with_reason_reports_timeout_when_message_mentions_it(app_factory, monkeypatch):
    # Most drivers (psycopg2, pymysql, oracledb, ...) don't raise a
    # dedicated timeout exception TYPE for a connect_timeout expiry - just
    # their own generic connection-error class with "timeout"/"timed out"
    # somewhere in the message. This is the fallback that covers them.
    app_factory()
    db_module, fake = _install_fake_backend(
        monkeypatch, raise_on_get_schema=RuntimeError("connection to server at \"host\" timed out"),
    )
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    success, reason = db_module.prime_schema_cache_with_reason(descriptor)

    assert success is False
    assert reason == db_module.SCHEMA_FETCH_FAILURE_REASON_TIMEOUT


def test_prime_schema_cache_with_reason_reports_no_reason_on_success(app_factory, monkeypatch):
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    success, reason = db_module.prime_schema_cache_with_reason(descriptor)

    assert success is True
    assert reason is None


def test_prime_schema_cache_is_unaffected_by_the_with_reason_split(app_factory, monkeypatch):
    # prime_schema_cache() is now a thin wrapper around
    # prime_schema_cache_with_reason() - this just re-confirms its own
    # plain bool-only contract (every pre-existing caller/test) still
    # holds after that refactor.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    assert db_module.prime_schema_cache(descriptor) is False


def test_get_database_schema_with_reason_reports_none_on_a_cache_hit(app_factory, monkeypatch):
    # A cache hit is never a failure of any kind - reason must be None,
    # not just falsy-by-coincidence.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="Table: t\n  id integer NOT NULL")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    db_module.get_database_schema(descriptor)  # warms the cache
    text, reason = db_module.get_database_schema_with_reason(descriptor)

    assert text == "Table: t\n  id integer NOT NULL"
    assert reason is None
    assert fake.get_schema_calls == 1  # the cache hit didn't fetch again


def test_prime_schema_cache_always_force_refreshes(app_factory, monkeypatch):
    # prime_schema_cache is always an explicit "fetch this now" request
    # (startup, or a user clicking "Refresh Schema") - it must never just
    # serve back whatever's already cached, even if something is.
    app_factory()
    db_module, fake = _install_fake_backend(monkeypatch, schema_text="FIRST")
    descriptor = {"type": "postgres", "url": "postgresql://u:p@host/db"}

    db_module.get_database_schema(descriptor, deep=True)
    assert fake.get_schema_calls == 1

    db_module.prime_schema_cache(descriptor)

    assert fake.get_schema_calls == 2
    # prime_schema_cache only ever force-fetches the deep entry now - see
    # this function's own docstring on the removed independent shallow
    # fetch.
    assert fake.get_schema_shallow_calls == 0


def test_prefetch_all_preset_schemas_calls_prime_schema_cache_once_per_preset(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    import db as db_module

    calls = []
    monkeypatch.setattr(
        db_module, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (calls.append(descriptor), (True, None))[1],
    )

    db_module.prefetch_all_preset_schemas()

    assert len(calls) == 2
    assert {c["url"] for c in calls} == {"postgresql://u:p@h/a", "postgresql://u:p@h/b"}
    # user_id is irrelevant for presets - always None.
    assert all("id" not in c and "name" not in c for c in calls)


def test_prefetch_all_preset_schemas_swallows_a_single_preset_failure(app_factory, monkeypatch, tmp_path, caplog):
    # One bad/unreachable preset must not raise past this function - it
    # can't be allowed to block server startup or take other presets
    # down with it.
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    import db as db_module

    calls = []

    def _fake_prime_with_reason(descriptor, user_id=None):
        calls.append(descriptor)
        if descriptor["url"].endswith("/a"):
            raise RuntimeError("connection reset")
        return True, None

    monkeypatch.setattr(db_module, "prime_schema_cache_with_reason", _fake_prime_with_reason)

    with caplog.at_level("WARNING"):
        db_module.prefetch_all_preset_schemas()  # must not raise

    assert len(calls) == 2  # the second preset still got its own attempt
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_prefetch_all_preset_schemas_skips_live_fetch_when_a_durable_copy_already_exists(app_factory, monkeypatch, tmp_path):
    # The whole point of schema_cache.py's durable (state_store-backed) L2
    # layer, for presets specifically: a restart/redeploy that finds an
    # already-durably-cached schema for a preset must NOT pay a live fetch
    # (plus the schema-overview LLM call) all over again - see this
    # function's own docstring. Simulates that exact scenario: nothing in
    # THIS process's own memory yet (a fresh process, like right after a
    # restart), but a durable store that already has the deep entry from
    # an earlier process lifetime. (There used to also be a separate
    # "shallow" entry this function warmed here too - removed along with
    # the independent shallow fetch/cache it existed to serve; see this
    # function's own docstring.)
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    import db as db_module

    descriptor = {"type": "postgres", "url": "postgresql://u:p@h/a"}
    cache_key = db_module.get_conn_identifier(descriptor)

    class _FakeDurableStore:
        def __init__(self, rows):
            self.rows = rows

        def get_cached_schema(self, key):
            return self.rows.get(key)

        def set_cached_schema(self, key, schema_text, cached_at):
            raise AssertionError("must not write through - nothing was fetched live")

        def set_cached_schema_overview(self, key, overview):
            raise AssertionError("must not write through - nothing was fetched live")

        def delete_cached_schema(self, key):
            raise AssertionError("must not delete anything during a prefetch")

    durable = _FakeDurableStore({
        cache_key: {"schema_text": "DURABLE DEEP TEXT", "cached_at": "2026-01-01T00:00:00+00:00", "overview": None},
    })
    monkeypatch.setattr(db_module.schema_cache, "_state_store", lambda: durable)

    calls = []
    monkeypatch.setattr(
        db_module, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (calls.append(descriptor), (True, None))[1],
    )

    db_module.prefetch_all_preset_schemas()

    assert calls == []  # no live fetch, no LLM overview call, at all
    assert db_module.schema_cache.get(cache_key) == "DURABLE DEEP TEXT"


def test_prefetch_all_preset_schemas_fetches_live_when_nothing_durable_exists_yet(app_factory, monkeypatch, tmp_path):
    # The other half of the same behavior, as a direct contrast: a
    # genuinely new preset (or one whose durable entry was explicitly
    # invalidated) has nothing for schema_cache.get() to find, so it must
    # still fall through to a real live fetch exactly as before this
    # durable-aware check existed.
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    import db as db_module

    class _EmptyDurableStore:
        def get_cached_schema(self, key):
            return None

        def set_cached_schema(self, key, schema_text, cached_at):
            pass

        def set_cached_schema_overview(self, key, overview):
            pass

        def delete_cached_schema(self, key):
            pass

    monkeypatch.setattr(db_module.schema_cache, "_state_store", lambda: _EmptyDurableStore())

    calls = []
    monkeypatch.setattr(
        db_module, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (calls.append(descriptor), (True, None))[1],
    )

    db_module.prefetch_all_preset_schemas()

    assert len(calls) == 1
    assert calls[0]["url"] == "postgresql://u:p@h/a"


def test_prefetch_all_preset_schemas_skips_the_local_dev_default_fallback_preset(app_factory, monkeypatch):
    # No DATABASE_PRESETS_FILE configured -> app_config.py synthesizes a
    # single "Default DB" fallback preset (see its own "if not
    # CONFIGURED_DBS:" line) purely so the connections dialog always has
    # something to show - it's not an admin's deliberate preset choice, so
    # prefetch (and the fatal-exclusion it can trigger) must be a full
    # no-op here, not treat it like a real preset. This was flipped from
    # the feature's original behavior (which prefetched this fallback too)
    # after it caused a real bug: the e2e suite runs with no
    # DATABASE_PRESETS_FILE, so its synthetic default's intentionally
    # bogus host got fatally excluded at every startup, collapsing
    # "configured_databases" to [] before any test ran.
    app_factory()
    import db as db_module

    calls = []
    monkeypatch.setattr(
        db_module, "prime_schema_cache_with_reason",
        lambda descriptor, user_id=None: (calls.append(descriptor), (True, None))[1],
    )

    db_module.prefetch_all_preset_schemas()

    assert len(calls) == 0


# --- a preset that fails startup prefetch just gets cached on its next real success --
#
# A preset that's briefly unreachable at boot needs no special retry/
# pending bookkeeping any more: since EVERY successful fetch is cached
# indefinitely regardless of how it was triggered, the next real request
# against that preset - whenever the DB happens to become reachable -
# fetches and caches it exactly like any other successful fetch, with
# nothing left over from the earlier failure to reconcile.

def test_a_preset_that_fails_prefetch_is_simply_uncached_afterward(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)  # "fails" (no schema text)
    descriptor = {"type": "postgres", "url": "postgresql://u:p@h/a"}

    db_module.prefetch_all_preset_schemas()

    import schema_cache
    assert schema_cache.get(db_module.get_conn_identifier(descriptor)) is None


def test_a_preset_that_fails_prefetch_is_cached_normally_on_its_next_successful_fetch(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)
    descriptor = {"type": "postgres", "url": "postgresql://u:p@h/a"}

    db_module.prefetch_all_preset_schemas()  # fails - nothing cached
    assert fake.get_schema_calls == 1  # the failed prefetch attempt itself

    # The DB is reachable again - an ordinary request (exactly what a real
    # /api/translate call would do) succeeds.
    fake._schema_text = "Table: t\n  id integer NOT NULL"
    result = db_module.get_database_schema(descriptor)

    assert result == "Table: t\n  id integer NOT NULL"
    assert fake.get_schema_calls == 2  # this fresh, uncached fetch
    # And it's cached from here on, same as any other successful fetch -
    # no further live fetches for this connection.
    assert db_module.get_database_schema(descriptor) == "Table: t\n  id integer NOT NULL"
    assert fake.get_schema_calls == 2


# --- a preset that fails prefetch FATALLY is excluded until the next restart ---
#
# Unlike EMPTY/TIMEOUT (left alone, retried lazily on next use - the tests
# above), a FATAL failure (a definitive, non-timeout rejection: bad
# credentials, a missing driver, a malformed request, ...) means retrying
# won't help until an admin actually fixes something - so this preset is
# excluded from visible_configured_dbs() (and everything that reads
# through it: resolve_active_descriptor, resolve_descriptor_by_reference,
# _resolve_all_configured_descriptors, and config_routes.py's preset-
# listing/selection code) until the next server restart re-runs prefetch
# from scratch and gives it another chance.

def test_a_preset_that_fails_prefetch_fatally_is_excluded_from_visible_configured_dbs(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-bad", "name": "Bad Postgres", "type": "postgres", "url": "postgresql://u:p@h/bad"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    db_module, fake = _install_fake_backend(
        monkeypatch, raise_on_get_schema=RuntimeError("password authentication failed"),
    )

    db_module.prefetch_all_preset_schemas()

    visible_ids = {db.get("id") for db in db_module.visible_configured_dbs()}
    assert "pg-bad" not in visible_ids
    # Every selectable-preset resolution path agrees it's gone, not just
    # the raw visible_configured_dbs() list.
    assert db_module.resolve_descriptor_by_reference("preset", "pg-bad", None) == (None, None)


def test_a_preset_that_times_out_during_prefetch_stays_visible(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-slow", "name": "Slow Postgres", "type": "postgres", "url": "postgresql://u:p@h/slow"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    db_module, fake = _install_fake_backend(
        monkeypatch, raise_on_get_schema=TimeoutError("connect timed out"),
    )

    db_module.prefetch_all_preset_schemas()

    visible_ids = {db.get("id") for db in db_module.visible_configured_dbs()}
    assert "pg-slow" in visible_ids
    descriptor, name = db_module.resolve_descriptor_by_reference("preset", "pg-slow", None)
    assert descriptor is not None
    assert name == "Slow Postgres"


def test_a_preset_with_an_empty_schema_stays_visible(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-empty", "name": "Empty Postgres", "type": "postgres", "url": "postgresql://u:p@h/empty"},
    ])
    app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    db_module, fake = _install_fake_backend(monkeypatch, schema_text=None)  # no exception - just empty

    db_module.prefetch_all_preset_schemas()

    visible_ids = {db.get("id") for db in db_module.visible_configured_dbs()}
    assert "pg-empty" in visible_ids


def test_visible_configured_dbs_is_unaffected_when_nothing_has_failed_fatally(app_factory, monkeypatch, tmp_path):
    from helpers import write_database_presets_file
    presets_path = write_database_presets_file(tmp_path, [
        {"id": "pg-a", "name": "Postgres A", "type": "postgres", "url": "postgresql://u:p@h/a"},
        {"id": "pg-b", "name": "Postgres B", "type": "postgres", "url": "postgresql://u:p@h/b"},
    ])
    app_env = app_factory(env={"DATABASE_PRESETS_FILE": presets_path})
    import db as db_module

    # No prefetch has run at all in this test - the plain, unfiltered case
    # (the overwhelmingly common one) must return CONFIGURED_DBS exactly.
    assert db_module.visible_configured_dbs() == db_module.CONFIGURED_DBS
