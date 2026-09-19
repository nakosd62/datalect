"""
backends/bigquery.py, driven against the fake BigQuery client harness
(helpers.install_fake_bigquery / schema_query_handler) - no real GCP
project or credentials needed.
"""

import sys
from decimal import Decimal
from datetime import date

import pytest
from google.api_core import exceptions as gcloud_exceptions

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from backends.bigquery import BigQueryBackend
from backends.base import SqlExecutionError, format_dataset_size_line
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
    # The heading's own leading label is the bare pattern - same
    # convention as a plain "Table: <name>" heading, and the same one
    # every other dialect's own shard-family heading already follows
    # (see e.g. test_postgres_backend.py's "Table family: events_<date>").
    # BigQuery is the one dialect that also needs a fully-qualified,
    # backtick-quoted wildcard form to actually query the family (unlike
    # every other dialect's plain "substitute the exact date" instruction) -
    # that's still given verbatim in the entry's own descriptive text, just
    # no longer duplicated as the heading's leading token too.
    assert "Table family: events_*" in schema
    assert "`p.d.events_*`" in schema
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


def test_get_schema_shallow_returns_none_when_no_tables(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(tables=[]))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    assert backend.get_schema_shallow(conn) is None


def test_get_schema_shallow_lists_plain_table_with_columns(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO"), ("customers", "name", "STRING", "YES")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Table: customers" in schema
    assert "id INT64 NOT NULL" in schema
    assert "name STRING NULL" in schema


# --- Phase 1: table/column comments -------------------------------------------

def test_get_schema_shallow_includes_table_comment(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        table_comments={"customers": "Customer master table"},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Table comments:" in schema
    assert "customers: Customer master table" in schema


def test_get_schema_shallow_omits_table_comments_section_when_none_present(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"], columns=[("customers", "id", "INT64", "NO")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Table comments:" not in schema


def test_get_schema_survives_table_options_query_failure(monkeypatch):
    # TABLE_OPTIONS backs both table comments and the require_partition_filter
    # flag - a failure here must degrade to "skip both", not fail the whole
    # schema fetch (mirrors the existing constraints-query resilience test).
    backend, harness = _bq(monkeypatch)

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA.TABLES" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": "orders", "table_type": "BASE TABLE"}],
                columns=["table_name", "table_type"],
            )
        if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
            return FakeBQQueryJob(
                rows=[{
                    "table_name": "orders", "column_name": "id", "data_type": "INT64",
                    "is_nullable": "NO", "is_partitioning_column": "NO",
                    "clustering_ordinal_position": None,
                }],
                columns=["table_name", "column_name", "data_type", "is_nullable",
                         "is_partitioning_column", "clustering_ordinal_position"],
            )
        if "INFORMATION_SCHEMA.TABLE_OPTIONS" in sql_text:
            raise Exception("TABLE_OPTIONS not supported in this region")
        return FakeBQQueryJob(rows=[])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    assert "Table comments:" not in schema
    assert "REQUIRES PARTITION FILTER" not in schema


# --- Phase 1: row-count estimate (INFORMATION_SCHEMA.TABLE_STORAGE) -----------

def test_get_schema_shallow_includes_row_count_estimate(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        table_storage={"customers": 12345},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Row count estimates:" in schema
    assert "customers: ~12345 rows (estimate)" in schema


def _bq_table_storage_handler(sql_text, exc):
    """Shared handler builder for the three failure-mode tests below - every
    other query (TABLES/COLUMNS/...) still succeeds normally via
    schema_query_handler(); only TABLE_STORAGE raises `exc`."""
    if "INFORMATION_SCHEMA.TABLES" in sql_text:
        return FakeBQQueryJob(
            rows=[{"table_name": "orders", "table_type": "BASE TABLE"}],
            columns=["table_name", "table_type"],
        )
    if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
        return FakeBQQueryJob(
            rows=[{
                "table_name": "orders", "column_name": "id", "data_type": "INT64",
                "is_nullable": "NO", "is_partitioning_column": "NO",
                "clustering_ordinal_position": None,
            }],
            columns=["table_name", "column_name", "data_type", "is_nullable",
                     "is_partitioning_column", "clustering_ordinal_position"],
        )
    if "INFORMATION_SCHEMA.TABLE_STORAGE" in sql_text:
        raise exc
    return FakeBQQueryJob(rows=[])


def test_get_schema_survives_table_storage_query_failure(monkeypatch, caplog):
    # TABLE_STORAGE is flagged best-effort in the plan (can be slower/less
    # available than TABLES/COLUMNS) - a failure here must not break the
    # rest of the fetch. It must also not be swallowed silently - see the
    # module's own comment on this except block for why the real cause
    # is now logged rather than just discarded. A 403 Forbidden here really
    # is the missing bigquery.tables.list permission case.
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql_text, job_config: _bq_table_storage_handler(
        sql_text, gcloud_exceptions.Forbidden("Access Denied: Permission bigquery.tables.list denied"),
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    assert "Row count estimates:" not in schema
    assert any(
        "TABLE_STORAGE row-count-estimate query failed" in r.getMessage()
        and "bigquery.tables.list" in r.getMessage()
        for r in caplog.records
    )


def test_get_schema_survives_table_storage_not_found_for_a_cross_project_dataset(monkeypatch, caplog):
    # Root-caused against a real connection to a Google-managed public
    # dataset (bigquery-public-data): TABLE_STORAGE 404s there, NOT 403s -
    # querying storage/row-count metadata for a dataset owned by a
    # different project than the one being billed simply isn't exposed,
    # regardless of what's granted. This must NOT be reported as a missing
    # permission (see _table_storage_failure_reason()'s own docstring) -
    # there is nothing to grant, and the old message actively misled a real
    # user into thinking a GRANT would fix it.
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql_text, job_config: _bq_table_storage_handler(
        sql_text, gcloud_exceptions.NotFound(
            "Dataset bigquery-public-data:google_trends.INFORMATION_SCHEMA "
            "was not found in location US"
        ),
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_trends"})
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    assert "Row count estimates:" not in schema
    matches = [r for r in caplog.records if "TABLE_STORAGE row-count-estimate query failed" in r.getMessage()]
    assert len(matches) == 1
    message = matches[0].getMessage()
    assert "bigquery.tables.list" not in message
    assert "different project" in message
    assert "does not expose storage/row-count metadata" in message


def test_get_schema_survives_table_storage_unknown_failure_with_a_generic_message(monkeypatch, caplog):
    # Neither a 404 nor a 403 - a transient/unclassified failure still logs
    # (never silently swallowed) but doesn't guess a specific cause it
    # can't actually confirm.
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql_text, job_config: _bq_table_storage_handler(
        sql_text, TimeoutError("deadline exceeded"),
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    matches = [r for r in caplog.records if "TABLE_STORAGE row-count-estimate query failed" in r.getMessage()]
    assert len(matches) == 1
    message = matches[0].getMessage()
    assert "bigquery.tables.list" not in message
    assert "different project" not in message
    assert "reason unknown" in message


# --- Phase 2 (deep-only): dataset-wide size estimate ---------------------------
# get_schema()'s own separate INFORMATION_SCHEMA.TABLE_STORAGE aggregate
# (SUM(total_rows)/SUM(total_logical_bytes)/COUNT(*), no kept_names filter) -
# distinct from the per-table Phase 1 TABLE_STORAGE query covered above,
# though both queries share the "INFORMATION_SCHEMA.TABLE_STORAGE" substring
# schema_query_handler() matches on, so these tests intercept it themselves
# with a custom handler that delegates everything else to schema_query_handler.

def test_get_schema_deep_appends_dataset_size_estimate_line(monkeypatch):
    backend, harness = _bq(monkeypatch)
    base_handler = schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
    )

    def handler(sql_text, job_config):
        if "SUM(total_rows)" in sql_text:
            assert "INFORMATION_SCHEMA.TABLE_STORAGE" in sql_text
            return FakeBQQueryJob(
                rows=[{"total_rows": 1234567, "total_bytes": 3_400_000_000, "table_count": 42}],
                columns=["total_rows", "total_bytes", "table_count"],
            )
        return base_handler(sql_text, job_config)

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    expected_line = format_dataset_size_line(
        total_rows=1234567, total_bytes=3_400_000_000,
    )
    assert expected_line in schema


def test_get_schema_deep_survives_dataset_size_estimate_query_failure(monkeypatch, caplog):
    # Same "don't swallow the real reason" coverage as
    # test_get_schema_survives_table_storage_query_failure above, for this
    # separate dataset-wide aggregate query. A 403 Forbidden here really is
    # the missing bigquery.tables.list permission case.
    backend, harness = _bq(monkeypatch)
    base_handler = schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
    )

    def handler(sql_text, job_config):
        if "SUM(total_rows)" in sql_text:
            raise gcloud_exceptions.Forbidden("Access Denied: Permission bigquery.tables.list denied")
        return base_handler(sql_text, job_config)

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "Estimated dataset size" not in schema
    assert any(
        "TABLE_STORAGE dataset-size query failed" in r.getMessage()
        and "bigquery.tables.list" in r.getMessage()
        for r in caplog.records
    )


def test_get_schema_deep_survives_dataset_size_not_found_for_a_cross_project_dataset(monkeypatch, caplog):
    # Same cross-project/public-dataset 404 coverage as
    # test_get_schema_survives_table_storage_not_found_for_a_cross_project_
    # dataset above, for this separate dataset-wide aggregate query - must
    # not blame a missing permission for something no GRANT could ever fix.
    backend, harness = _bq(monkeypatch)
    base_handler = schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
    )

    def handler(sql_text, job_config):
        if "SUM(total_rows)" in sql_text:
            raise gcloud_exceptions.NotFound(
                "Dataset bigquery-public-data:google_trends.INFORMATION_SCHEMA "
                "was not found in location US"
            )
        return base_handler(sql_text, job_config)

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "bigquery-public-data", "dataset": "google_trends"})
    with caplog.at_level("WARNING"):
        schema = backend.get_schema(conn)
    assert "Table: customers" in schema
    assert "Estimated dataset size" not in schema
    matches = [r for r in caplog.records if "TABLE_STORAGE dataset-size query failed" in r.getMessage()]
    assert len(matches) == 1
    message = matches[0].getMessage()
    assert "bigquery.tables.list" not in message
    assert "different project" in message


# --- Phase 1: routines (existence + signature, no body) -----------------------

def test_get_schema_shallow_includes_routine_signature_without_body(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["orders"],
        columns=[("orders", "id", "INT64", "NO")],
        routines=[("total_rev", "total_rev_1", "FLOAT64", "SELECT SUM(amount) FROM orders")],
        routine_params=[("total_rev_1", "region", "STRING", 1)],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Routines:" in schema
    assert "total_rev(region STRING) -> FLOAT64" in schema
    assert "SELECT SUM(amount) FROM orders" not in schema


def test_get_schema_includes_routine_body_in_deep_but_not_shallow(monkeypatch):
    backend, harness = _bq(monkeypatch)
    handler = schema_query_handler(
        tables=["orders"],
        columns=[("orders", "id", "INT64", "NO")],
        routines=[("total_rev", "total_rev_1", "FLOAT64", "SELECT SUM(amount) FROM orders")],
    )
    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    shallow = backend.get_schema_shallow(conn)
    deep = backend.get_schema(conn)
    assert "total_rev() -> FLOAT64" in shallow
    assert "SELECT SUM(amount) FROM orders" not in shallow
    assert "Routine definitions:" in deep
    assert "SELECT SUM(amount) FROM orders" in deep


def test_get_schema_routine_signature_survives_parameters_query_failure(monkeypatch):
    backend, harness = _bq(monkeypatch)

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA.TABLES" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": "orders", "table_type": "BASE TABLE"}],
                columns=["table_name", "table_type"],
            )
        if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
            return FakeBQQueryJob(
                rows=[{
                    "table_name": "orders", "column_name": "id", "data_type": "INT64",
                    "is_nullable": "NO", "is_partitioning_column": "NO",
                    "clustering_ordinal_position": None,
                }],
                columns=["table_name", "column_name", "data_type", "is_nullable",
                         "is_partitioning_column", "clustering_ordinal_position"],
            )
        if "INFORMATION_SCHEMA.ROUTINES" in sql_text:
            return FakeBQQueryJob(
                rows=[{
                    "routine_name": "total_rev", "specific_name": "total_rev_1",
                    "data_type": "FLOAT64", "routine_definition": "SELECT SUM(amount) FROM orders",
                }],
                columns=["routine_name", "specific_name", "data_type", "routine_definition"],
            )
        if "INFORMATION_SCHEMA.PARAMETERS" in sql_text:
            raise Exception("PARAMETERS not supported in this region")
        return FakeBQQueryJob(rows=[])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "total_rev() -> FLOAT64" in schema


def test_get_schema_survives_routines_query_failure(monkeypatch):
    backend, harness = _bq(monkeypatch)

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA.TABLES" in sql_text:
            return FakeBQQueryJob(
                rows=[{"table_name": "orders", "table_type": "BASE TABLE"}],
                columns=["table_name", "table_type"],
            )
        if "INFORMATION_SCHEMA.COLUMNS" in sql_text:
            return FakeBQQueryJob(
                rows=[{
                    "table_name": "orders", "column_name": "id", "data_type": "INT64",
                    "is_nullable": "NO", "is_partitioning_column": "NO",
                    "clustering_ordinal_position": None,
                }],
                columns=["table_name", "column_name", "data_type", "is_nullable",
                         "is_partitioning_column", "clustering_ordinal_position"],
            )
        if "INFORMATION_SCHEMA.ROUTINES" in sql_text:
            raise Exception("ROUTINES not supported in this region")
        return FakeBQQueryJob(rows=[])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Table: orders" in schema
    assert "Routines:" not in schema


# --- Phase 1: distribution/clustering/partition columns (free COLUMNS add) ----

def test_get_schema_shallow_includes_partitioning_and_clustering_markers(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["events"],
        columns=[
            ("events", "event_date", "DATE", "NO"),
            ("events", "user_id", "STRING", "NO"),
            ("events", "country", "STRING", "YES"),
        ],
        partitioning_columns={"events": "event_date"},
        clustering_columns={"events": ["user_id", "country"]},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "event_date DATE NOT NULL [PARTITION]" in schema
    assert "user_id STRING NOT NULL [CLUSTER #1]" in schema
    assert "country STRING NULL [CLUSTER #2]" in schema


# --- Phase 1: require_partition_filter (correctness-gating) -------------------

def test_get_schema_shallow_flags_require_partition_filter(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["events"],
        columns=[("events", "event_date", "DATE", "NO")],
        partitioning_columns={"events": "event_date"},
        require_partition_filter={"events": True},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "REQUIRES PARTITION FILTER on event_date" in schema


def test_get_schema_shallow_omits_partition_filter_flag_when_false(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        require_partition_filter={"customers": False},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "REQUIRES PARTITION FILTER" not in schema


# --- Phase 1: external tables --------------------------------------------------

def test_get_schema_shallow_flags_external_table(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["ext_data"],
        columns=[("ext_data", "id", "INT64", "NO")],
        table_types={"ext_data": "EXTERNAL"},
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema_shallow(conn)
    assert "Table: ext_data [external table]" in schema


# --- Phase 1 vs Phase 2: shallow excludes Phase 2 / full bodies; deep is a ----
# --- superset -------------------------------------------------------------

def test_get_schema_shallow_excludes_phase2_content_and_full_bodies(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        views=[("customer_orders", "SELECT * FROM orders")],
        routines=[("total", "total_1", "INT64", "SELECT 1")],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    shallow = backend.get_schema_shallow(conn)
    assert "Views:" in shallow
    assert "customer_orders" in shallow
    assert "SELECT * FROM orders" not in shallow
    assert "Routines:" in shallow
    assert "total() -> INT64" in shallow
    assert "SELECT 1" not in shallow
    assert "View definitions:" not in shallow
    assert "Routine definitions:" not in shallow
    assert "Live row counts:" not in shallow
    assert "Column value samples:" not in shallow


def test_get_schema_deep_is_superset_of_shallow(monkeypatch):
    backend, harness = _bq(monkeypatch)
    handler = schema_query_handler(
        tables=["customers"],
        columns=[("customers", "id", "INT64", "NO")],
        views=[("customer_orders", "SELECT * FROM orders")],
    )
    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    shallow = backend.get_schema_shallow(conn)
    deep = backend.get_schema(conn)
    assert deep.startswith(shallow)
    assert "View definitions:" in deep
    assert "SELECT * FROM orders" in deep


# --- Phase 2: live row counts, min/max, frequent values, cardinality gate ----

def test_get_schema_includes_live_row_count_min_max_and_frequent_values(monkeypatch):
    backend, harness = _bq(monkeypatch)
    base = schema_query_handler(
        tables=["orders"],
        columns=[
            ("orders", "amount", "FLOAT64", "NO"),
            ("orders", "status", "STRING", "NO"),
        ],
    )

    def handler(sql_text, job_config):
        if "SELECT COUNT(*) AS n FROM" in sql_text:
            return FakeBQQueryJob(rows=[{"n": 50}])
        if "MIN(" in sql_text:
            return FakeBQQueryJob(rows=[{"min_0": 1.5, "max_0": 999.0}])
        if "APPROX_COUNT_DISTINCT(" in sql_text:
            return FakeBQQueryJob(rows=[{"distinct_0": 3}])
        if "TABLESAMPLE" in sql_text:
            assert "TABLESAMPLE SYSTEM (10 PERCENT)" in sql_text
            return FakeBQQueryJob(rows=[{"val": "shipped", "cnt": 40}, {"val": "pending", "cnt": 10}])
        return base(sql_text, job_config)

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Live row counts:" in schema
    assert "orders: 50 rows (live, authoritative)" in schema
    assert "Column value samples:" in schema
    assert "amount: range [1.5 .. 999.0]" in schema
    assert "status: frequent values = shipped (40), pending (10)" in schema


def test_get_schema_skips_frequent_values_for_near_unique_column(monkeypatch):
    # Cardinality gate: APPROX_COUNT_DISTINCT close to the live row count
    # marks a column near-unique (e.g. a UUID) - not worth sampling.
    backend, harness = _bq(monkeypatch)
    base = schema_query_handler(tables=["orders"], columns=[("orders", "order_uuid", "STRING", "NO")])

    def handler(sql_text, job_config):
        if "SELECT COUNT(*) AS n FROM" in sql_text:
            return FakeBQQueryJob(rows=[{"n": 100}])
        if "APPROX_COUNT_DISTINCT(" in sql_text:
            return FakeBQQueryJob(rows=[{"distinct_0": 99}])
        if "TABLESAMPLE" in sql_text:
            return FakeBQQueryJob(rows=[{"val": "x", "cnt": 1}])
        return base(sql_text, job_config)

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Column value samples:" not in schema


def test_get_schema_includes_naming_convention_relationships(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(schema_query_handler(
        tables=["customers", "orders"],
        columns=[
            ("customers", "id", "INT64", "NO"),
            ("orders", "id", "INT64", "NO"),
            ("orders", "customer_id", "INT64", "NO"),
        ],
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)
    assert "Likely relationships (naming convention, unconfirmed):" in schema
    assert "orders.customer_id" in schema


# --- Phase 2: require_partition_filter threaded into Phase 2's own queries ---

def test_get_schema_threads_partition_filter_into_phase2_queries(monkeypatch):
    from backends.bigquery import _bigquery_partition_filter_clause

    backend, harness = _bq(monkeypatch)
    base = schema_query_handler(
        tables=["events"],
        columns=[
            ("events", "event_date", "DATE", "NO"),
            ("events", "region", "STRING", "NO"),
        ],
        partitioning_columns={"events": "event_date"},
        require_partition_filter={"events": True},
    )
    captured = []

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA" in sql_text:
            return base(sql_text, job_config)
        captured.append(sql_text)
        if "SELECT COUNT(*) AS n FROM" in sql_text:
            return FakeBQQueryJob(rows=[{"n": 10}])
        if "MIN(" in sql_text:
            return FakeBQQueryJob(rows=[{"min_0": "2024-01-01"}])
        if "APPROX_COUNT_DISTINCT(" in sql_text:
            return FakeBQQueryJob(rows=[{"distinct_0": 2}])
        if "TABLESAMPLE" in sql_text:
            return FakeBQQueryJob(rows=[{"val": "us", "cnt": 5}])
        return FakeBQQueryJob(rows=[])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)

    # Detected in Phase 1, rendered on the table's own heading.
    assert "REQUIRES PARTITION FILTER on event_date" in schema

    # Threaded into every one of Phase 2's own new queries for this table -
    # not skipped, not sent unfiltered.
    expected_clause = _bigquery_partition_filter_clause("event_date", "DATE")
    assert captured, "expected at least one Phase 2 query for the partitioned table"
    for sql_text in captured:
        assert expected_clause in sql_text


def test_get_schema_skips_phase2_queries_when_partition_column_unknown(monkeypatch):
    # require_partition_filter is true but Phase 1 couldn't identify which
    # column is the partitioning column - Phase 2 must skip this table's
    # own new queries entirely rather than send one unfiltered (which
    # BigQuery would reject for exactly this table).
    backend, harness = _bq(monkeypatch)
    base = schema_query_handler(
        tables=["orders"],
        columns=[("orders", "id", "INT64", "NO")],
        require_partition_filter={"orders": True},
    )
    captured = []

    def handler(sql_text, job_config):
        if "INFORMATION_SCHEMA" in sql_text:
            return base(sql_text, job_config)
        captured.append(sql_text)
        return FakeBQQueryJob(rows=[{"n": 999}])

    harness.set_handler(handler)
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    schema = backend.get_schema(conn)

    assert "REQUIRES PARTITION FILTER" in schema
    assert not captured, "Phase 2 must not query this table without a partition filter"
    assert "Live row counts:" not in schema


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


# --- execute(): EXECUTE_RESULTS_MAX_ROWS cap ----------------------------------
# See test_postgres_backend.py's identically-named tests for the full
# rationale. BigQuery has no cursor/fetchmany() to route through
# fetch_capped_rows() the way the other backends do - it caps via
# query_job.result(max_results=...) instead (see backends/bigquery.py's
# execute()) and detects truncation via the REAL RowIterator.total_rows,
# not by comparing against how many rows this test iterated - so
# FakeBQRowIterator's own total_rows (helpers.py) is what actually proves
# this out, not just how many dict rows this fake happens to hold.

def test_execute_caps_rows_and_flags_truncated_past_the_default_limit(monkeypatch):
    from backends.base import EXECUTE_RESULTS_MAX_ROWS
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql, jc: FakeBQQueryJob(
        rows=[{"n": i} for i in range(EXECUTE_RESULTS_MAX_ROWS + 1)]
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "SELECT n FROM huge_table;")
    assert results[0]["rowCount"] == EXECUTE_RESULTS_MAX_ROWS
    assert len(results[0]["rows"]) == EXECUTE_RESULTS_MAX_ROWS
    assert results[0]["truncated"] is True


def test_execute_omits_truncated_key_entirely_when_not_truncated(monkeypatch):
    backend, harness = _bq(monkeypatch)
    harness.set_handler(lambda sql, jc: FakeBQQueryJob(
        rows=[{"id": 1, "name": "Alice"}]
    ))
    conn = backend.connect({"type": "bigquery", "project_id": "p", "dataset": "d"})
    results = backend.execute(conn, "SELECT id, name FROM t;")
    assert "truncated" not in results[0]
