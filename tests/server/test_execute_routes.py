"""
execute_routes.py: /api/execute. Patches execute_routes.get_backend
directly with a fake Backend so these tests exercise only this route's own
logic (empty-SQL validation, response shaping, the intentional raw-error-
message-on-failure behavior) rather than a specific dialect's backend -
those are covered in test_postgres_backend.py/test_bigquery_backend.py.
"""

import pytest


class _FakeBackend:
    def __init__(self, results=None, raise_exc=None, connect_exc=None):
        self._results = results if results is not None else []
        self._raise_exc = raise_exc
        self._connect_exc = connect_exc
        self.closed_conn = None
        self.connected = False

    def connect(self, descriptor):
        if self._connect_exc:
            raise self._connect_exc
        self.connected = True
        return object()

    def close(self, connection):
        self.closed_conn = connection

    def execute(self, connection, sql_text):
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
