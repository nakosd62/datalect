"""
backends/bigquery.py, driven against the fake BigQuery client harness
(helpers.install_fake_bigquery / schema_query_handler) - no real GCP
project or credentials needed.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.bigquery import BigQueryBackend
from backends.base import SqlExecutionError
from helpers import (
    install_fake_bigquery, schema_query_handler, make_service_account_key_json,
    FakeBQQueryJob,
)


def _bq(monkeypatch):
    """Fresh backend + harness pair - each test patches its own copy of
    backends.bigquery's bigquery.* names."""
    harness = install_fake_bigquery(monkeypatch)
    return BigQueryBackend(), harness


# --- connect(): billing vs data project split ---------------------------------

def test_connect_uses_billing_project_id_for_client_when_given(monkeypatch):
    backend, harness = _bq(monkeypatch)
    conn = backend.connect({
        "type": "bigquery", "project_id": "public-data-proj", "dataset": "ds",
        "billing_project_id": "my-billing-proj",
    })
    assert harness.client_calls[-1]["project"] == "my-billing-proj"
    # get_schema()/identity_label() still need the DATA project/dataset,
    # not the billing one - stashed separately on the client object.
    assert conn._ydyl_project_id == "public-data-proj"
    assert conn._ydyl_dataset == "ds"


def test_connect_falls_back_to_project_id_when_no_billing_project_given(monkeypatch):
    backend, harness = _bq(monkeypatch)
    backend.connect({"type": "bigquery", "project_id": "my-own-proj", "dataset": "ds"})
    assert harness.client_calls[-1]["project"] == "my-own-proj"


def test_connect_with_credentials_json_derives_project_id_from_key_when_missing(monkeypatch):
    backend, harness = _bq(monkeypatch)
    key_json = make_service_account_key_json(project_id="key-embedded-proj")
    conn = backend.connect({
        "type": "bigquery", "dataset": "ds", "credentials_json": key_json,
    })
    assert conn._ydyl_project_id == "key-embedded-proj"
    # No explicit billing_project_id given either - falls back to the
    # (key-derived) project_id, same as the no-key case.
    assert harness.client_calls[-1]["project"] == "key-embedded-proj"
    assert harness.client_calls[-1]["credentials"] is not None


def test_connect_explicit_project_id_wins_over_key_embedded_one(monkeypatch):
    backend, harness = _bq(monkeypatch)
    key_json = make_service_account_key_json(project_id="key-embedded-proj")
    conn = backend.connect({
        "type": "bigquery", "project_id": "explicit-proj", "dataset": "ds",
        "credentials_json": key_json,
    })
    assert conn._ydyl_project_id == "explicit-proj"


# --- cache_key / identity_label -----------------------------------------------

def test_cache_key_is_project_dot_dataset():
    backend = BigQueryBackend()
    assert backend.cache_key({"project_id": "p", "dataset": "d"}) == "p.d"


def test_cache_key_handles_missing_fields():
    backend = BigQueryBackend()
    assert backend.cache_key({}) == "unknown.unknown"


def test_identity_label_returns_dataset_and_project(monkeypatch):
    backend, harness = _bq(monkeypatch)
    conn = backend.connect({"type": "bigquery", "project_id": "p1", "dataset": "d1"})
    dataset, project = backend.identity_label(conn)
    assert dataset == "d1"
    assert project == "p1"


# --- get_schema ----------------------------------------------------------------

def test_get_schema_returns_none_when_no_tables(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(tables=[]))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    assert backend.get_schema(conn) is None


def test_get_schema_lists_plain_table_with_columns(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO"), ("customers", "name", "STRING", "YES")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "id INT64 NOT NULL" in schema
    assert "name STRING NULL" in schema


def test_get_schema_collapses_shard_family_into_wildcard_table_syntax(monkeypatch):
    backend, harness = _bq(monkeypatch)
    members = [f"events_2024010{i}" for i in range(1, 6)]
    harness.set_handler(schema_query_handler(
        tables=members,
        columns=[(members[-1], "id", "INT64", "NO")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Table family: `p.d.events_*`" in schema
    assert "_TABLE_SUFFIX" in schema
    assert "Table: events_20240102" not in schema


def test_get_schema_includes_views_unscoped_regression(monkeypatch):
    # Same regression as postgres.py's: views must never be scoped to
    # kept_names (BASE-TABLE-only), or the Views section always comes
    # back empty.
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        views=[("customer_orders", "SELECT * FROM orders")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Views:" in schema
    assert "customer_orders" in schema


def test_get_schema_includes_constraints_section(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["orders"],
        columns=[("orders", "id", "INT64", "NO")],
        constraints=[("orders", "orders_pk", "PRIMARY KEY", "id")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Constraints:" in schema
    assert "orders_pk" in schema


def test_get_schema_survives_constraints_query_failure(monkeypatch):
    # Best-effort: TABLE_CONSTRAINTS/KEY_COLUMN_USAGE can 404 on some
    # BigQuery datasets/regions - that must degrade to "skip this
    # section", not fail the whole schema fetch.
    backend, harness = _bq(monkeypatch)

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA.TABLES" in sql_text:
            return FakeBQQueryJob(rows=[{"table_name": "orders"}], columns=["table_name"])
        if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": "orders", "column_name": "id", "data_type": "INT64", "is_nullable": "NO"}],
                columns=["table_name", "column_name", "data_type", "is_nullable"],
            )
        if "TABLE_CONSTRAINTS" in sql_text:
            raise Exception("404: constraints not supported in this region")
        return FakeBQQueryJob(rows=[])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    assert "Constraints:" not in schema


def test_get_schema_scopes_columns_query_with_unnest_param_not_string_formatting(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["t1", "t2"],
        columns=[("t1", "id", "INT64", "NO"), ("t2", "id", "INT64", "NO")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    backend.get_schema(conn)

    columns_call = next(
        (sql, jc) for sql, jc in harness.query_calls if "INFORMATION_SCHEMA.COLUMNS" in sql
    )
    sql_text, job_config = columns_call
    assert "@kept_names" in sql_text
    assert "t1" not in sql_text  # never string-formatted directly into SQL
    param = job_config.query_parameters[0]
    assert param.name == "kept_names"
    assert set(param.values) == {"t1", "t2"}


# --- execute ---------------------------------------------------------------

def test_execute_select_shapes_rows_as_dicts(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql, jc: FakeBQQueryJob(
        rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "SELECT id, name FROM t;")
    assert results[0]["columns"] == ["id", "name"]
    assert results[0]["rows"] == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    assert results[0]["rowCount"] == 2


def test_execute_dml_uses_num_dml_affected_rows(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql, jc: FakeBQQueryJob(rows=[], columns=[], num_dml_affected_rows=5))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "DELETE FROM t WHERE x=1;")
    assert results[0]["columns"] is None
    assert results[0]["rowCount"] == 5


def test_execute_converts_decimal_and_datetime(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql, jc: FakeBQQueryJob(
        rows=[{"price": Decimal("19.99"), "d": date(2024, 1, 15)}]
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "SELECT price, d FROM t;")
    row = results[0]["rows"][0]
    assert row["price"] == 19.99
    assert isinstance(row["price"], float)
    assert row["d"] == "2024-01-15"


def test_execute_multiple_statements(monkeypatch):
    backend, harness = _bq(monkeypatch)
    calls = {"n": 0}

    def handler(sql, jc):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeBQQueryJob(rows=[], columns=[], num_dml_affected_rows=1)
        return FakeBQQueryJob(rows=[{"id": 1}])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "UPDATE t SET x=1; SELECT id FROM t;")
    assert len(results) == 2
    assert results[1]["rows"] == [{"id": 1}]


def test_execute_mid_script_failure_raises_sql_execution_error_with_partial_results(monkeypatch):
    """Regression guard for the multi-statement "one tab per statement,
    including the failed one" UI feature - see SqlExecutionError's
    docstring in backends/base.py."""
    backend, harness = _bq(monkeypatch)
    calls = {"n": 0}

    def handler(sql, jc):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeBQQueryJob(rows=[], columns=[], num_dml_affected_rows=1)
        raise RuntimeError("Syntax error: Unexpected keyword SELEC")

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    with pytest.raises(SqlExecutionError) as exc_info:
        backend.execute(conn, "UPDATE t SET x=1; SELEC bad syntax; SELECT 1;")

    err = exc_info.value
    assert len(err.results) == 1
    assert err.results[0]["rowCount"] == 1
    assert err.failed_statement == "SELEC bad syntax"
    assert err.statement_index == 1
    assert err.total_statements == 3
    assert "Syntax error" in str(err)
