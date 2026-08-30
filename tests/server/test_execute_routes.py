"""
execute_routes.py: /api/execute. Patches execute_routes.get_backend
directly with a fake Backend so these tests exercise only this route's own
logic (empty-SQL validation, response shaping, the intentional raw-error-
message-on-failure behavior) rather than a specific dialect's backend -
those are covered in test_postgres_backend.py/test_bigquery_backend.py.

The SqlExecutionError-specific tests below cover this route's OWN handling
of that exception (catching it ahead of the plain-Exception branch,
shaping the richer partial-results response) - the backends' own raising
of it on a mid-script failure is covered per-dialect in each
test_*_backend.py file instead (e.g. test_postgres_backend.py's
test_execute_mid_script_failure_raises_sql_execution_error_with_partial_
results).

Those tests build their SqlExecutionError instance off app_env.execute_
routes.SqlExecutionError (the class object the fresh-imported module under
test actually holds - see helpers.fresh_import()'s module-reimport
mechanics) rather than a top-level `from backends.base import
SqlExecutionError`, which would bind a DIFFERENT class object each test
gets a fresh reimport, silently failing execute_routes.py's `except
SqlExecutionError` isinstance check.
"""

import threading
import time

import pytest


class _FakeBackend:
    def __init__(self, results=None, raise_exc=None, connect_exc=None, liveness_sql="SELECT 1", delay=0):
        self._results = results if results is not None else []
        self._raise_exc = raise_exc
        self._connect_exc = connect_exc
        self.closed_conn = None
        self.connected = False
        # Matches backends/base.py's Backend.liveness_sql - defaults to the
        # same "SELECT 1" every real backend but Oracle uses, overridable
        # per test so /api/ping's tests below can assert it's actually
        # this attribute (not a hardcoded string) that gets executed.
        self.liveness_sql = liveness_sql
        self.executed_sql = None
        # Seconds execute() blocks before returning/raising - see
        # test_execute_times_out_and_returns_friendly_error below (and its
        # neighbors), the only tests that pass a nonzero delay to simulate a
        # runaway/hung query against _execute_with_timeout's thread race.
        self._delay = delay
        # Set once execute()'s sleep actually finishes - lets a timeout test
        # confirm the abandoned worker thread really did keep running past
        # the timeout (proving the timeout fired while it was still in
        # flight, not just because it happened to be slower than expected)
        # without the test itself needing to sleep any longer than the
        # timeout it's testing.
        self.execute_finished = threading.Event()

    def connect(self, descriptor):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
        return object()

    def close(self, connection):
        self.closed_conn = connection

    def execute(self, connection, sql_text):
        self.executed_sql = sql_text
        if self._delay:
            time.sleep(self._delay)
        self.execute_finished.set()
        if self._raise_exc:
            raise self._raise_exc
        return self._results


def _patch_backend(monkeypatch, app_env, fake_backend):
    monkeypatch.setattr(app_env.execute_routes, "get_backend", lambda descriptor: fake_backend)


def test_execute_empty_sql_returns_400(client):
    resp = client.post('/api/execute', json={'sql': ''})
    assert resp.status_code == 400
    assert "Query cannot be empty" in resp.get_json()['error']


def test_execute_missing_sql_key_returns_400(client):
    resp = client.post('/api/execute', json={})
    assert resp.status_code == 400


def test_execute_whitespace_only_sql_returns_400(client):
    resp = client.post('/api/execute', json={'sql': '   '})
    assert resp.status_code == 400


def test_execute_accepts_query_key_as_alias_for_sql(app_env, monkeypatch):
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'query': 'SELECT 1;'})
    assert resp.status_code == 200


def test_execute_success_returns_results_and_row_count(app_env, monkeypatch):
    fake = _FakeBackend(results=[
        {"statement": "SELECT * FROM users", "columns": ["id", "name"],
         "rows": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}], "rowCount": 2},
    ])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELECT * FROM users;'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['rowCount'] == 2
    assert data['results'][0]['rows'] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert 'executionTimeMs' in data


def test_execute_sums_row_count_across_multiple_statements(app_env, monkeypatch):
    fake = _FakeBackend(results=[
        {"statement": "UPDATE t SET x=1", "columns": None, "rows": None, "rowCount": 3},
        {"statement": "SELECT * FROM t", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'UPDATE t SET x=1; SELECT * FROM t;'})
    data = resp.get_json()
    assert data['rowCount'] == 4


def test_execute_failure_returns_raw_error_message(app_env, monkeypatch):
    # Intentional behavior (see execute_routes.py's module docstring):
    # unlike every other endpoint in this app, /api/execute returns the
    # real backend error text, not a generalized/logged one - the user
    # needs the actual SQL error to fix their query.
    fake = _FakeBackend(raise_exc=Exception('column "foo" does not exist'))
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELECT foo FROM t;'})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert data['error'] == 'column "foo" does not exist'
    # A plain (non-SqlExecutionError) failure keeps the original flat
    # shape - no partial-results keys at all, since there's no positional
    # detail to report (see test_execute_mid_script_failure_below for the
    # richer shape a SqlExecutionError produces instead).
    assert 'results' not in data
    assert 'failedStatement' not in data


def test_execute_mid_script_failure_returns_partial_results_and_failed_statement_info(app_env, monkeypatch):
    """Core regression guard for the "one tab per statement, including the
    failed one" UI feature: when the backend raises SqlExecutionError (a
    statement partway through a multi-statement script failed), the route
    must surface the successful statements' results plus which statement
    failed - not just a flat error that loses that detail."""
    succeeded = [{"statement": "UPDATE t SET x=1", "columns": None, "rows": None, "rowCount": 3}]
    exc = app_env.execute_routes.SqlExecutionError(
        'syntax error at or near "SELEC"',
        results=succeeded,
        failed_statement="SELEC bad syntax",
        statement_index=1,
        total_statements=3,
    )
    fake = _FakeBackend(raise_exc=exc)
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post(
        '/api/execute',
        json={'sql': 'UPDATE t SET x=1; SELEC bad syntax; SELECT 1;'}
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert data['error'] == 'syntax error at or near "SELEC"'
    assert data['results'] == succeeded
    assert data['failedStatement'] == "SELEC bad syntax"
    assert data['failedIndex'] == 1
    assert data['totalStatements'] == 3
    assert 'executionTimeMs' in data


def test_execute_mid_script_failure_with_no_prior_successes_still_includes_empty_results(app_env, monkeypatch):
    """The very first statement failing is still a SqlExecutionError (just
    with an empty `results` list) - the response shape stays consistent
    rather than falling back to the flat shape only in this one case."""
    exc = app_env.execute_routes.SqlExecutionError(
        "syntax error", results=[], failed_statement="SELEC bad", statement_index=0, total_statements=2,
    )
    fake = _FakeBackend(raise_exc=exc)
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELEC bad; SELECT 1;'})
    data = resp.get_json()
    assert data['results'] == []
    assert data['failedIndex'] == 0
    assert data['totalStatements'] == 2


def test_execute_connect_failure_also_returns_400_with_raw_message(app_env, monkeypatch):
    fake = _FakeBackend(connect_exc=Exception("could not connect to server"))
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == "could not connect to server"


def test_execute_always_closes_connection_on_success(app_env, monkeypatch):
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert fake.closed_conn is not None


def test_execute_always_closes_connection_on_query_failure(app_env, monkeypatch):
    fake = _FakeBackend(raise_exc=Exception("boom"))
    _patch_backend(monkeypatch, app_env, fake)
    app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert fake.closed_conn is not None


def test_execute_sets_session_cookie(app_env, monkeypatch):
    fake = _FakeBackend(results=[])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert "crbot_session_id" in resp.headers.get("Set-Cookie", "")


# --- SQL execution timeout ---------------------------------------------------
# SQL_EXECUTE_TIMEOUT_SECONDS/_execute_with_timeout - the execute-time
# counterpart to DB_CONNECT_TIMEOUT_SECONDS (backends/base.py), which only
# bounds connect(). _FakeBackend's `delay` param (see its docstring above)
# simulates a runaway/hung query long enough for the timeout to actually
# fire mid-flight, using a tiny SQL_EXECUTE_TIMEOUT_SECONDS so these tests
# stay fast rather than actually waiting out a realistic default.

def test_sql_execute_timeout_seconds_defaults_to_30(app_env):
    assert app_env.execute_routes.SQL_EXECUTE_TIMEOUT_SECONDS == 30


def test_sql_execute_timeout_seconds_env_var_overrides_default(app_factory):
    env = app_factory(env={"SQL_EXECUTE_TIMEOUT_SECONDS": "5"})
    assert env.execute_routes.SQL_EXECUTE_TIMEOUT_SECONDS == 5


def test_execute_times_out_and_returns_friendly_error(app_factory, monkeypatch):
    env = app_factory(env={"SQL_EXECUTE_TIMEOUT_SECONDS": "0.05"})
    fake = _FakeBackend(
        results=[{"statement": "SELECT pg_sleep(999)", "columns": None, "rows": None, "rowCount": 0}],
        delay=0.3,
    )
    _patch_backend(monkeypatch, env, fake)
    resp = env.client.post('/api/execute', json={'sql': 'SELECT pg_sleep(999);'})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert data['error'] == "Query execution timed out after 0.05 seconds"
    # No partial-results keys - a timeout isn't a SqlExecutionError, so it
    # keeps the same flat shape any other plain backend.execute() exception
    # produces (see test_execute_failure_returns_raw_error_message above).
    assert 'results' not in data
    # Proves this test actually exercised the timeout path (the abandoned
    # worker thread was still mid-sleep when the response was built), not
    # just that the fake happened to be a bit slow.
    assert not fake.execute_finished.is_set()
    # Let the abandoned thread actually finish before the test ends, so it
    # doesn't leak past this test's own lifetime.
    fake.execute_finished.wait(timeout=1)


def test_execute_closes_connection_after_timeout(app_factory, monkeypatch):
    env = app_factory(env={"SQL_EXECUTE_TIMEOUT_SECONDS": "0.05"})
    fake = _FakeBackend(results=[], delay=0.3)
    _patch_backend(monkeypatch, env, fake)
    env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    # The existing finally-block close() (execute_query()'s own, not
    # _execute_with_timeout's) still runs after a timeout, same as any
    # other failure.
    assert fake.closed_conn is not None
    fake.execute_finished.wait(timeout=1)


def test_sql_execute_timeout_disabled_when_set_to_zero(app_factory, monkeypatch):
    env = app_factory(env={"SQL_EXECUTE_TIMEOUT_SECONDS": "0"})
    fake = _FakeBackend(
        results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}],
        delay=0.1,
    )
    _patch_backend(monkeypatch, env, fake)
    resp = env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    # 0 disables the timeout entirely (see SQL_EXECUTE_TIMEOUT_SECONDS's
    # docstring) - a query slower than what a nonzero timeout would have
    # allowed still succeeds.
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_ping_times_out_and_returns_success_false(app_factory, monkeypatch):
    env = app_factory(env={"SQL_EXECUTE_TIMEOUT_SECONDS": "0.05"})
    fake = _FakeBackend(results=[], delay=0.3)
    _patch_backend(monkeypatch, env, fake)
    resp = env.client.get('/api/ping')
    # Same "never leak error detail" posture as any other /api/ping failure
    # (see test_ping_query_failure_returns_400_and_success_false_without_
    # leaking_error_detail above) - a timeout is just another failure as far
    # as /api/ping's response shape is concerned.
    assert resp.status_code == 400
    assert resp.get_json() == {"success": False}
    fake.execute_finished.wait(timeout=1)


# --- /api/ping ---------------------------------------------------------------
# Regression coverage for the real-world bug this route fixes: the status
# dot used to POST a hardcoded "SELECT 1;" to /api/execute, which is
# invalid against Oracle (no SELECT-without-FROM form there) and
# permanently showed a working Oracle connection as disconnected. /api/ping
# instead asks the resolved backend for its own liveness_sql (see
# backends/base.py/backends/oracle.py) rather than the client guessing a
# query string that happens to work for most dialects.

def test_ping_runs_the_backends_own_liveness_sql_not_a_hardcoded_string(app_env, monkeypatch):
    fake = _FakeBackend(results=[{"statement": "SELECT 1 FROM DUAL", "columns": ["1"], "rows": [{"1": 1}], "rowCount": 1}],
                         liveness_sql="SELECT 1 FROM DUAL")
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.get('/api/ping')
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert fake.executed_sql == "SELECT 1 FROM DUAL"


def test_ping_success_returns_200_and_success_true(app_env, monkeypatch):
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["1"], "rows": [{"1": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.get('/api/ping')
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}


def test_ping_query_failure_returns_400_and_success_false_without_leaking_error_detail(app_env, monkeypatch):
    # Unlike /api/execute, /api/ping never surfaces the raw exception text -
    # the status dot only ever shows connected/disconnected (see the
    # route's own docstring).
    fake = _FakeBackend(raise_exc=Exception("ORA-00923: FROM keyword not found where expected"))
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.get('/api/ping')
    assert resp.status_code == 400
    data = resp.get_json()
    assert data == {"success": False}
    assert "ORA-00923" not in resp.get_data(as_text=True)


def test_ping_connect_failure_returns_400_and_success_false(app_env, monkeypatch):
    fake = _FakeBackend(connect_exc=Exception("could not connect to server"))
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.get('/api/ping')
    assert resp.status_code == 400
    assert resp.get_json() == {"success": False}


def test_ping_always_closes_connection_on_success(app_env, monkeypatch):
    fake = _FakeBackend(results=[])
    _patch_backend(monkeypatch, app_env, fake)
    app_env.client.get('/api/ping')
    assert fake.closed_conn is not None


def test_ping_always_closes_connection_on_failure(app_env, monkeypatch):
    fake = _FakeBackend(raise_exc=Exception("boom"))
    _patch_backend(monkeypatch, app_env, fake)
    app_env.client.get('/api/ping')
    assert fake.closed_conn is not None


def test_ping_sets_session_cookie(app_env, monkeypatch):
    fake = _FakeBackend(results=[])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.get('/api/ping')
    assert "crbot_session_id" in resp.headers.get("Set-Cookie", "")


# --- Multi-database dispatch --------------------------------------------------
# See execute_routes.py's "Multi-database dispatch" module-level comment and
# _execute_multi_database's docstring, and translate_routes.py's module
# docstring for the feature as a whole. These tests stub
# execute_routes.resolve_descriptor_by_reference directly (same idea as
# _patch_backend above stubbing get_backend) rather than going through real
# sessions/presets - _execute_multi_database only ever calls that one
# function by (kind, ref_id), so a plain dict-backed stub is enough to
# exercise this route's own dispatch/reassembly logic in isolation.

class _FakeMultiBackend:
    """Like _FakeBackend above, but tracks how many times connect() was
    called and every execute() call's SQL text (not just the last one) -
    needed to assert a connection referenced by multiple, possibly
    non-contiguous marker chunks is still opened exactly once and receives
    its statements concatenated in their original relative order."""

    def __init__(self, results=None, raise_exc=None, delay=0):
        self._results = results if results is not None else []
        self._raise_exc = raise_exc
        self.connect_count = 0
        self.closed_conn = None
        self.executed_sql_calls = []
        # Seconds execute() blocks before returning/raising - see the
        # concurrency tests below, which use this to prove groups run in
        # parallel (not serially) and that ordering in the final response
        # is driven by group_order, not by which group's thread finishes
        # first.
        self._delay = delay
        self.execute_started = threading.Event()

    def connect(self, descriptor):
        self.connect_count += 1
        return object()

    def close(self, connection):
        self.closed_conn = connection

    def execute(self, connection, sql_text):
        self.executed_sql_calls.append(sql_text)
        self.execute_started.set()
        if self._delay:
            time.sleep(self._delay)
        if self._raise_exc:
            raise self._raise_exc
        return self._results


def _descriptor_resolver(mapping):
    """Builds a resolve_descriptor_by_reference stand-in from a
    {(kind, ref_id): (descriptor, name)} mapping - an unmapped (kind,
    ref_id) resolves to (None, None), same as a stale/removed reference
    would for real."""
    def _resolve(kind, ref_id, user_identity):
        return mapping.get((kind, ref_id), (None, None))
    return _resolve


def _backend_router(backends_by_descriptor_marker):
    """Builds a get_backend stand-in that looks up the fake backend by a
    "marker" key on the (fake) descriptor dict - each connection's
    descriptor in these tests is just {"marker": <str>}, since get_backend
    itself is faked and never needs a real connection shape."""
    def _get_backend(descriptor):
        return backends_by_descriptor_marker[descriptor["marker"]]
    return _get_backend


def _patch_multi_db(monkeypatch, app_env, resolver_mapping, backends_by_marker):
    monkeypatch.setattr(app_env.execute_routes, "resolve_descriptor_by_reference", _descriptor_resolver(resolver_mapping))
    monkeypatch.setattr(app_env.execute_routes, "get_backend", _backend_router(backends_by_marker))


def test_marker_free_script_is_byte_identical_to_today(app_env, monkeypatch):
    # Core regression guard: a script with no '-- database: ...' marker
    # anywhere never enters the multi-database dispatch path at all - same
    # flat response shape as before this feature existed, no "failures"
    # key, no "database" field on any result.
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': 'SELECT 1;'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'failures' not in data
    assert 'database' not in data['results'][0]


def test_two_connection_script_dispatches_to_both_and_reassembles(app_env, monkeypatch):
    fake_a = _FakeMultiBackend(results=[
        {"statement": "INSERT INTO a VALUES (1)", "columns": None, "rows": None, "rowCount": 1},
    ])
    fake_b = _FakeMultiBackend(results=[
        {"statement": "INSERT INTO b VALUES (2)", "columns": None, "rows": None, "rowCount": 1},
    ])
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a (Sales)\nINSERT INTO a VALUES (1);\n\n"
        "-- database: preset:pg-b (Marketing)\nINSERT INTO b VALUES (2);"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'failures' not in data
    assert data['rowCount'] == 2
    assert [r['database'] for r in data['results']] == [
        {"kind": "preset", "id": "pg-a", "name": "Sales"},
        {"kind": "preset", "id": "pg-b", "name": "Marketing"},
    ]
    assert fake_a.connect_count == 1
    assert fake_b.connect_count == 1
    assert fake_a.closed_conn is not None
    assert fake_b.closed_conn is not None


def test_strip_database_marker_lines_removes_only_the_marker_line(app_env):
    # Uses app_env.execute_routes (the fresh-imported module this app
    # instance actually holds), not a bare top-level `import
    # execute_routes` - see this file's module docstring on why that
    # matters (a different fresh-import elsewhere in the suite can leave
    # a bare import bound to a different module object).
    strip = app_env.execute_routes._strip_database_marker_lines
    text = "-- database: preset:pg-a (Sales Postgres)\nSELECT 1;\n-- database: preset:pg-a (Sales Postgres)\nSELECT 3;"
    assert strip(text) == "SELECT 1;\nSELECT 3;"


def test_strip_database_marker_lines_is_a_noop_for_marker_free_text(app_env):
    strip = app_env.execute_routes._strip_database_marker_lines
    assert strip("  SELECT 1;  ") == "SELECT 1;"


def test_marker_line_never_reaches_the_backends_own_execute_call(app_env, monkeypatch):
    """The core bug this guards against: Google Sheets' GViz query
    language (backends/sheets.py) has no '--' comment syntax at all, so a
    marker-tagged multi-database script that reaches gviz with its
    leading '-- database: ...' line still attached is a syntax error, not
    a harmless no-op like it is for every real SQL dialect. This doesn't
    need a real Sheets fake to prove: any backend that raises on a leading
    '--' line demonstrates the marker is stripped before execute() ever
    sees it, regardless of which dialect is on the other end."""

    class _RejectsCommentSyntaxBackend:
        """Stands in for backends/sheets.py's execute(), which has no
        concept of '--' as a comment - a leading one is a hard syntax
        error there, not silently ignored the way a real SQL engine
        would. Raising here if the marker line ever leaks through is
        exactly that failure mode."""

        def connect(self, descriptor):
            return object()

        def close(self, connection):
            pass

        def execute(self, connection, sql_text):
            if sql_text.strip().startswith("--"):
                raise Exception("GViz syntax error: unexpected '--'")
            return [{"statement": sql_text, "columns": ["A"], "rows": [{"A": 1}], "rowCount": 1}]

    fake_sheet = _RejectsCommentSyntaxBackend()
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "sheet-a"): ({"marker": "sheet"}, "My Sheet")},
        {"sheet": fake_sheet},
    )
    sql = "-- database: preset:sheet-a (My Sheet)\nSELECT A WHERE A > 0"
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'failures' not in data
    assert data['results'][0]['statement'] == "SELECT A WHERE A > 0"


def test_one_connection_failing_still_runs_and_returns_the_other(app_env, monkeypatch):
    fake_a = _FakeMultiBackend(raise_exc=Exception("relation \"a\" does not exist"))
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ])
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nSELECT * FROM a;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    # The independent, unrelated database's statement still ran and
    # returned its result - a failure in one group must not block the
    # other (see execute_routes.py's module docstring).
    assert len(data['results']) == 1
    assert data['results'][0]['database'] == {"kind": "preset", "id": "pg-b", "name": "Marketing"}
    assert len(data['failures']) == 1
    assert data['failures'][0]['database'] == {"kind": "preset", "id": "pg-a", "name": "Sales"}
    assert data['failures'][0]['error'] == 'relation "a" does not exist'


def test_group_mid_script_failure_still_reports_its_own_partial_successes(app_env, monkeypatch):
    """A SqlExecutionError raised for one connection group's own
    multi-statement script still surfaces that group's successful
    statements (same partial-results contract as the single-connection
    path), tagged with its database, alongside the OTHER group's own
    (unrelated) results."""
    succeeded = [{"statement": "UPDATE a SET x=1", "columns": None, "rows": None, "rowCount": 1}]
    exc = app_env.execute_routes.SqlExecutionError(
        'syntax error', results=succeeded, failed_statement="SELEC bad", statement_index=1, total_statements=2,
    )
    fake_a = _FakeMultiBackend(raise_exc=exc)
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ])
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nUPDATE a SET x=1;\n-- database: preset:pg-a\nSELEC bad;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    data = resp.get_json()
    assert data['success'] is False
    databases_in_results = [r['database']['id'] for r in data['results']]
    assert databases_in_results == ["pg-a", "pg-b"]
    assert data['failures'][0]['failedStatement'] == "SELEC bad"
    assert data['failures'][0]['totalStatements'] == 2


def test_connection_reference_that_no_longer_resolves_is_its_own_failure(app_env, monkeypatch):
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ])
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-b"): ({"marker": "b"}, "Marketing")},  # pg-a deliberately NOT in the mapping
        {"b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nSELECT * FROM a;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert len(data['results']) == 1  # pg-b's still ran
    assert len(data['failures']) == 1
    assert data['failures'][0]['database'] == {"kind": "preset", "id": "pg-a", "name": None}
    assert "no longer exists" in data['failures'][0]['error']


def test_same_connection_referenced_twice_non_contiguously_opens_once_and_concatenates_in_order(app_env, monkeypatch):
    fake_a = _FakeMultiBackend(results=[
        {"statement": "s1", "columns": None, "rows": None, "rowCount": 1},
        {"statement": "s2", "columns": None, "rows": None, "rowCount": 1},
    ])
    fake_b = _FakeMultiBackend(results=[
        {"statement": "s-mid", "columns": None, "rows": None, "rowCount": 1},
    ])
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    # pg-a's two statements are interleaved with pg-b's single statement in
    # the original text - still one connect() call for pg-a, with both of
    # its chunks concatenated (in their original relative order) into one
    # execute() call.
    sql = (
        "-- database: preset:pg-a\nSELECT 1;\n\n"
        "-- database: preset:pg-b\nSELECT 2;\n\n"
        "-- database: preset:pg-a\nSELECT 3;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 200
    assert fake_a.connect_count == 1
    assert fake_b.connect_count == 1
    assert len(fake_a.executed_sql_calls) == 1
    assert "SELECT 1;" in fake_a.executed_sql_calls[0]
    assert "SELECT 3;" in fake_a.executed_sql_calls[0]
    assert fake_a.executed_sql_calls[0].index("SELECT 1;") < fake_a.executed_sql_calls[0].index("SELECT 3;")
    # Results are grouped by each connection's first appearance (pg-a
    # first, since its marker appears first in the text), not replayed in
    # strict original per-statement text order - a documented v1 scope cut
    # (see _execute_multi_database's docstring).
    data = resp.get_json()
    assert [r['database']['id'] for r in data['results']] == ["pg-a", "pg-a", "pg-b"]


def test_multi_database_groups_execute_concurrently_not_serially(app_env, monkeypatch):
    # _execute_multi_database now runs each distinct connection group's
    # execute() in its own ThreadPoolExecutor worker (see that function's
    # docstring) - both groups here block for delay_seconds; run serially
    # this would take at least 2 * delay_seconds, run in parallel it stays
    # well under that.
    delay_seconds = 0.4
    fake_a = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM a", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ], delay=delay_seconds)
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ], delay=delay_seconds)
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nSELECT * FROM a;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    start = time.perf_counter()
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert elapsed < delay_seconds * 1.8


def test_slower_group_listed_first_still_returns_results_in_original_group_order(app_env, monkeypatch):
    # pg-a is listed FIRST in the script (and so is first in group_order)
    # but is the SLOWER of the two to finish; pg-b is listed second but
    # finishes first. The final `results` order must still follow
    # group_order (pg-a's statement before pg-b's), not completion order -
    # this is what the pre-allocated, index-addressed `outcomes` list in
    # _execute_multi_database guarantees under real concurrency.
    fake_a = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM a", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ], delay=0.3)
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 2}], "rowCount": 1},
    ], delay=0)
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nSELECT * FROM a;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert [r['database']['id'] for r in data['results']] == ["pg-a", "pg-b"]


def test_one_group_crashing_unexpectedly_does_not_prevent_others_from_completing(app_env, monkeypatch):
    # Same tolerant, per-group failure isolation as
    # test_one_connection_failing_still_runs_and_returns_the_other above,
    # but now proven under genuine concurrency: pg-a's worker thread
    # raises partway through its (slower) run while pg-b's worker is still
    # in flight - pg-b's result must still come back, and pg-a's crash
    # must show up as its own failure entry, not abort the whole request
    # or corrupt pg-b's outcome.
    fake_a = _FakeMultiBackend(raise_exc=Exception("connection reset by peer"), delay=0.3)
    fake_b = _FakeMultiBackend(results=[
        {"statement": "SELECT * FROM b", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1},
    ], delay=0)
    _patch_multi_db(
        monkeypatch, app_env,
        {("preset", "pg-a"): ({"marker": "a"}, "Sales"), ("preset", "pg-b"): ({"marker": "b"}, "Marketing")},
        {"a": fake_a, "b": fake_b},
    )
    sql = (
        "-- database: preset:pg-a\nSELECT * FROM a;\n\n"
        "-- database: preset:pg-b\nSELECT * FROM b;"
    )
    resp = app_env.client.post('/api/execute', json={'sql': sql})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert [r['database']['id'] for r in data['results']] == ["pg-b"]
    assert len(data['failures']) == 1
    assert data['failures'][0]['database']['id'] == "pg-a"
    assert "connection reset by peer" in data['failures'][0]['error']


def test_malformed_database_marker_falls_back_to_single_connection_path(app_env, monkeypatch):
    # "BOGUS" isn't "preset"/"custom" - _DB_MARKER_RE never matches it, so
    # this script is treated as marker-free entirely (falls straight
    # through to the single-connection path, honoring whatever's pinned/
    # resolved for the session) rather than erroring.
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    resp = app_env.client.post('/api/execute', json={'sql': '-- database: BOGUS\nSELECT 1;'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert 'failures' not in data


def test_marker_free_script_uses_pinned_connection_over_session_primary(app_env, monkeypatch):
    # A marker-free (hand-typed/edited) script still honors a client-
    # echoed pinned connection over the session's single "primary"
    # connection - see execute_query()'s own comment on this.
    pinned_fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    seen_descriptors = []

    def _get_backend(descriptor):
        seen_descriptors.append(descriptor)
        return pinned_fake

    monkeypatch.setattr(
        app_env.execute_routes, "resolve_descriptor_by_reference",
        lambda kind, ref_id, user_identity: ({"marker": "pinned"}, "Pinned DB") if (kind, ref_id) == ("preset", "pg-a") else (None, None),
    )
    monkeypatch.setattr(app_env.execute_routes, "get_backend", _get_backend)

    resp = app_env.client.post('/api/execute', json={
        'sql': 'SELECT 1;',
        'pinned_connections': [{"kind": "preset", "id": "pg-a"}],
    })
    assert resp.status_code == 200
    assert seen_descriptors == [{"marker": "pinned"}]


def test_marker_free_script_falls_back_to_session_default_when_pin_is_stale(app_env, monkeypatch):
    fake = _FakeBackend(results=[{"statement": "SELECT 1", "columns": ["x"], "rows": [{"x": 1}], "rowCount": 1}])
    _patch_backend(monkeypatch, app_env, fake)
    monkeypatch.setattr(
        app_env.execute_routes, "resolve_descriptor_by_reference",
        lambda kind, ref_id, user_identity: (None, None),
    )
    resp = app_env.client.post('/api/execute', json={
        'sql': 'SELECT 1;',
        'pinned_connections': [{"kind": "preset", "id": "deleted-connection"}],
    })
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True
