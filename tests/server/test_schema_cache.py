"""
schema_cache.py has NO process-local state of its own any more - every
get()/set()/invalidate()/mark_fetch_*() call goes straight through to
whatever durable store _state_store() resolves to (state_store.py -
SQLite locally, Firestore on Cloud Run). See that module's own docstring
for why: a process-local in-memory layer was actively wrong on
multi-instance Cloud Run (one instance's invalidate/refresh was invisible
to another's already-warm copy), not just a missed optimization, so it
was removed rather than bounded with a TTL.

Every test below monkeypatches schema_cache's own _state_store() to
return a small, fully in-process fake (see _FakeDurableStore) rather than
letting it lazily default to a REAL SqliteStateStore at app_config.py's
own hardcoded "state/ydyl_state.db" path - keeping this file's original
"no app import needed, just exercise it directly" spirit intact. See
state_store.py's own SqliteStateStore/FirestoreStateStore tests for
coverage of the REAL backends' storage semantics; this file only cares
that schema_cache.py itself calls the right method, with the right
arguments, and returns/derives the right thing from what comes back.
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


class _FakeDurableStore:
    """Minimal stand-in for the seven schema-cache methods every real
    StateStore backend implements (see state_store.py) - `rows` is a plain
    dict keyed by cache_key, each value shaped like state_store.py's own
    get_cached_schema()'s {"schema_text", "cached_at", "overview"} return
    value; `status` is a separate dict keyed by cache_key, each value
    shaped like get_schema_fetch_status()'s own {"pending", "error"}
    return value - kept apart because the real backends store them as
    independent fields/columns on the same row/doc (see
    mark_schema_fetch_pending's own docstring on why it never touches
    fetch_error, mirrored here as `status` and `rows` never touching each
    other's dict). Records every write call it receives so tests can
    assert not just the end state but which method schema_cache.py
    actually called."""

    def __init__(self, rows=None, status=None):
        self.rows = rows or {}
        self.status = status or {}
        self.set_cached_schema_calls = []
        self.set_overview_calls = []
        self.delete_calls = []
        self.mark_pending_calls = []
        self.mark_done_calls = []

    def get_cached_schema(self, cache_key):
        return self.rows.get(cache_key)

    def set_cached_schema(self, cache_key, schema_text, cached_at):
        self.set_cached_schema_calls.append((cache_key, schema_text, cached_at))
        row = self.rows.setdefault(cache_key, {"schema_text": None, "cached_at": None, "overview": None})
        row["schema_text"] = schema_text
        row["cached_at"] = cached_at

    def set_cached_schema_overview(self, cache_key, overview):
        self.set_overview_calls.append((cache_key, overview))
        row = self.rows.setdefault(cache_key, {"schema_text": None, "cached_at": None, "overview": None})
        row["overview"] = overview

    def delete_cached_schema(self, cache_key):
        self.delete_calls.append(cache_key)
        self.rows.pop(cache_key, None)
        self.status.pop(cache_key, None)

    def mark_schema_fetch_pending(self, cache_key):
        self.mark_pending_calls.append(cache_key)
        entry = self.status.setdefault(cache_key, {"pending": False, "error": None})
        entry["pending"] = True

    def mark_schema_fetch_done(self, cache_key, error=None):
        self.mark_done_calls.append((cache_key, error))
        entry = self.status.setdefault(cache_key, {"pending": False, "error": None})
        entry["pending"] = False
        entry["error"] = error

    def get_schema_fetch_status(self, cache_key):
        return dict(self.status.get(cache_key, {"pending": False, "error": None}))

    def list_cached_schema_texts(self):
        return {k: v["schema_text"] for k, v in self.rows.items() if v.get("schema_text") is not None}


def _fresh_module_with_durable(monkeypatch, rows=None, status=None):
    for mod_name in ("schema_cache",):
        sys.modules.pop(mod_name, None)
    import schema_cache
    durable = _FakeDurableStore(rows, status)
    monkeypatch.setattr(schema_cache, "_state_store", lambda: durable)
    return schema_cache, durable


# --- get()/set() ------------------------------------------------------------

def test_get_missing_key_returns_none(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    assert cache.get("nope") is None


def test_set_then_get_returns_value(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "some schema text")
    assert cache.get("k1") == "some schema text"


def test_get_reads_through_to_the_durable_store_every_time(monkeypatch):
    # No warming, no L1 - a second get() for the same key hits the durable
    # store again rather than answering from a memoized copy (unlike this
    # module's old L1/L2 design - see its own module docstring on why).
    cache, durable = _fresh_module_with_durable(monkeypatch, rows={
        "k1": {"schema_text": "DURABLE TEXT", "cached_at": "2026-01-01T00:00:00+00:00", "overview": None},
    })
    assert cache.get("k1") == "DURABLE TEXT"
    durable.rows["k1"]["schema_text"] = "UPDATED ELSEWHERE"
    # A change made "elsewhere" (a different process/instance's write,
    # simulated here by mutating the fake's own rows directly) is visible
    # on the very next get() - this is the whole point of the redesign.
    assert cache.get("k1") == "UPDATED ELSEWHERE"


def test_get_on_total_miss_returns_none_without_raising(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    assert cache.get("nope") is None


def test_entry_never_expires_on_its_own(monkeypatch):
    # No TTL to wait out - a cached entry is returned unchanged no matter
    # how many times it's read; only invalidate() ever removes it.
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "schema text")
    for _ in range(3):
        assert cache.get("k1") == "schema text"


def test_set_again_replaces_the_cached_value(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "old text")
    cache.set("k1", "new text")
    assert cache.get("k1") == "new text"


def test_set_writes_through_with_a_timestamp_get_cached_at_reports_back(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "schema text")

    assert len(durable.set_cached_schema_calls) == 1
    written_key, written_text, written_cached_at = durable.set_cached_schema_calls[0]
    assert written_key == "k1"
    assert written_text == "schema text"
    assert written_cached_at == cache.get_cached_at("k1")


def test_get_cached_at_reads_the_durable_store(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch, rows={
        "k1": {"schema_text": "text", "cached_at": "2026-02-03T04:05:06+00:00", "overview": None},
    })
    assert cache.get_cached_at("k1") == "2026-02-03T04:05:06+00:00"


def test_get_cached_at_on_a_never_cached_key_returns_none(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    assert cache.get_cached_at("nope") is None


# --- overview -----------------------------------------------------------

def test_get_overview_reads_the_durable_store(monkeypatch):
    overview = {"prose": "A sales dataset.", "questions": ["Top region?"], "generated_at": "2026-01-01T00:00:00+00:00"}
    cache, durable = _fresh_module_with_durable(monkeypatch, rows={
        "k1": {"schema_text": "text", "cached_at": "2026-01-01T00:00:00+00:00", "overview": overview},
    })
    assert cache.get_overview("k1") == overview


def test_get_overview_returns_none_when_durable_row_exists_but_overview_is_still_none(monkeypatch):
    # A schema can be durably cached (schema_text set) well before its
    # overview generation has ever succeeded (or it may never succeed at
    # all - see schema_cache.py's own "best-effort" docstring for
    # set_overview/get_overview) - this must read back as "nothing to show
    # yet", not raise or return some placeholder.
    cache, durable = _fresh_module_with_durable(monkeypatch, rows={
        "k1": {"schema_text": "text", "cached_at": "2026-01-01T00:00:00+00:00", "overview": None},
    })
    assert cache.get_overview("k1") is None


def test_set_overview_writes_through_via_its_own_dedicated_durable_method(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    overview = {"prose": "desc", "questions": ["q1"], "generated_at": "2026-01-01T00:00:00+00:00"}

    cache.set_overview("k1", overview)

    assert cache.get_overview("k1") == overview
    assert durable.set_overview_calls == [("k1", overview)]
    # set_overview() must never go through set_cached_schema() (which would
    # also stamp/clobber schema_text+cached_at for a key that may not even
    # have a schema fetched yet) - it has its own dedicated write-through
    # method for exactly this reason (see state_store.py's own set_cached_
    # schema/set_cached_schema_overview independence).
    assert durable.set_cached_schema_calls == []


def test_set_then_set_overview_leaves_the_schema_text_and_cached_at_untouched(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "schema text")
    cached_at_after_set = cache.get_cached_at("k1")

    cache.set_overview("k1", {"prose": "desc", "questions": [], "generated_at": "later"})

    assert cache.get("k1") == "schema text"
    assert cache.get_cached_at("k1") == cached_at_after_set


# --- invalidate() -----------------------------------------------------------

def test_invalidate_drops_entry(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "schema text")
    cache.invalidate("k1")
    assert cache.get("k1") is None


def test_invalidate_missing_key_is_a_no_op(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.invalidate("never-set")  # must not raise
    assert durable.delete_calls == ["never-set"]


def test_invalidate_calls_the_durable_delete(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "schema text")

    cache.invalidate("k1")

    assert durable.delete_calls == ["k1"]
    assert "k1" not in durable.rows


# --- fetch-pending/last-error status ----------------------------------------
#
# Durable now for the same cross-instance reason schema_text itself is -
# see schema_cache.py's own module docstring on why a poll landing on a
# different Cloud Run instance than the one running the background fetch
# must still see the correct in-flight status.

def test_is_fetch_pending_is_false_for_a_key_never_fetched_at_all(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    assert cache.is_fetch_pending("nope") is False


def test_get_last_fetch_error_is_none_for_a_key_never_fetched_at_all(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    assert cache.get_last_fetch_error("nope") is None


def test_mark_fetch_pending_writes_through_and_is_fetch_pending_reports_it(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    assert durable.mark_pending_calls == ["k1"]
    assert cache.is_fetch_pending("k1") is True


def test_mark_fetch_done_with_no_error_clears_pending_and_reports_no_error(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    cache.mark_fetch_done("k1")
    assert durable.mark_done_calls == [("k1", None)]
    assert cache.is_fetch_pending("k1") is False
    assert cache.get_last_fetch_error("k1") is None


def test_mark_fetch_done_with_an_error_clears_pending_and_records_it(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    cache.mark_fetch_done("k1", error="TIMEOUT")
    assert durable.mark_done_calls == [("k1", "TIMEOUT")]
    assert cache.is_fetch_pending("k1") is False
    assert cache.get_last_fetch_error("k1") == "TIMEOUT"


def test_a_later_successful_fetch_clears_a_previously_recorded_error(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    cache.mark_fetch_done("k1", error="FATAL")
    assert cache.get_last_fetch_error("k1") == "FATAL"

    cache.mark_fetch_pending("k1")
    cache.mark_fetch_done("k1")  # this attempt succeeded
    assert cache.get_last_fetch_error("k1") is None


def test_invalidate_clears_fetch_status_too(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    cache.mark_fetch_done("k1", error="FATAL")

    cache.invalidate("k1")

    assert cache.is_fetch_pending("k1") is False
    assert cache.get_last_fetch_error("k1") is None


# --- dump() -------------------------------------------------------------

def test_dump_returns_every_cached_schema_text(monkeypatch):
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.set("k1", "a")
    cache.set("k2", "b")
    assert cache.dump() == {"k1": "a", "k2": "b"}


def test_dump_omits_a_key_with_no_schema_text_yet(monkeypatch):
    # A fetch that's pending, or a key whose only durable content so far is
    # an overview, has nothing worth dumping as "cached schema" - matches
    # the old in-memory dump()'s behavior (it only ever held keys set()
    # had actually been called for).
    cache, durable = _fresh_module_with_durable(monkeypatch)
    cache.mark_fetch_pending("k1")
    cache.set_overview("k2", {"prose": "x", "questions": [], "generated_at": "t"})
    assert cache.dump() == {}
