"""
tests/utils/test_export_state.py

Covers utils/export_state.py, with most of the weight on the guarantee that
motivated sort_columns_alphabetically(): every exported CSV's column order
must be alphabetical and reproducible, regardless of source (Datastore/
Firestore, SQLite, or the two unioned together) and regardless of anything
about the underlying data's own ordering (a Datastore entity's own property
order, or which row a query happens to return first). See that function's
docstring in export_state.py for the full reasoning.

Datastore is faked with a minimal stand-in for the tiny slice of the real
google-cloud-datastore API export_state.py actually calls (Client.query(),
Query.fetch()/keys_only(), and an Entity that behaves like a dict with a
.key.id_or_name) - there's no real GCP project/credentials in a test
environment, and none of the column-ordering logic under test cares about
anything past that surface. SQLite is exercised against a real, throwaway
on-disk database (via tmp_path) rather than faked, since fetch_sqlite's own
job here - schema-defined column order via `SELECT *` - is only meaningful
against a real sqlite3 connection.
"""

import csv
import os
import sqlite3

import pandas as pd
import pytest

from export_state import (
    fetch_datastore,
    fetch_sqlite,
    export_datastore,
    export_sqlite,
    export_union,
    sort_columns_alphabetically,
)


# ==========================================
# sort_columns_alphabetically()
# ==========================================

def test_sort_columns_alphabetically_reorders_columns_but_preserves_row_data():
    df = pd.DataFrame([
        {"zeta": 1, "alpha": "a", "mid": True},
        {"zeta": 2, "alpha": "b", "mid": False},
    ])
    sorted_df = sort_columns_alphabetically(df)

    assert list(sorted_df.columns) == ["alpha", "mid", "zeta"]
    # Row data travels with its own column, not just shuffled positionally.
    assert sorted_df.iloc[0].to_dict() == {"alpha": "a", "mid": True, "zeta": 1}
    assert sorted_df.iloc[1].to_dict() == {"alpha": "b", "mid": False, "zeta": 2}


def test_sort_columns_alphabetically_is_case_sensitive_ordinal_sort():
    # Documents the actual behavior (Python's default string sort, not a
    # locale-aware/case-insensitive one) rather than leaving it implicit -
    # "_source" sorts before any lowercase-starting column since '_' (0x5F)
    # is less than 'a' (0x61), and an all-uppercase name sorts before any
    # lowercase name for the same reason.
    df = pd.DataFrame([{"beta": 1, "_source": "x", "Alpha": 2}])
    assert list(sort_columns_alphabetically(df).columns) == ["Alpha", "_source", "beta"]


def test_sort_columns_alphabetically_handles_empty_dataframe():
    df = pd.DataFrame()
    assert list(sort_columns_alphabetically(df).columns) == []


# ==========================================
# fetch_sqlite()
# ==========================================

def _make_sqlite_db(tmp_path, filename="state.db"):
    db_path = str(tmp_path / filename)
    conn = sqlite3.connect(db_path)
    # Column order in the CREATE TABLE is deliberately NOT alphabetical -
    # this is the exact shape that used to leak straight into the CSV
    # unchanged (SQLite's `SELECT *` returns schema-defined order) before
    # sort_columns_alphabetically was applied here too.
    conn.execute("CREATE TABLE translations (zeta_col TEXT, id INTEGER, alpha_col TEXT)")
    conn.execute("INSERT INTO translations VALUES ('z1', 1, 'a1')")
    conn.execute("INSERT INTO translations VALUES ('z2', 2, 'a2')")
    conn.execute("CREATE TABLE sessions (session_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()
    return db_path


def test_fetch_sqlite_returns_columns_alphabetically_despite_non_alphabetical_schema(tmp_path):
    db_path = _make_sqlite_db(tmp_path)
    results = fetch_sqlite(db_path, "translations")

    assert list(results.keys()) == ["translations"]
    assert list(results["translations"].columns) == ["alpha_col", "id", "zeta_col"]


def test_fetch_sqlite_all_tables_each_come_back_sorted(tmp_path):
    db_path = _make_sqlite_db(tmp_path)
    results = fetch_sqlite(db_path, "all")

    assert set(results.keys()) == {"translations", "sessions"}
    assert list(results["translations"].columns) == ["alpha_col", "id", "zeta_col"]
    assert list(results["sessions"].columns) == ["created_at", "session_id"]


def test_fetch_sqlite_missing_file_returns_empty_dict(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.db")
    assert fetch_sqlite(missing_path, "all") == {}


def test_fetch_sqlite_skips_empty_tables(tmp_path):
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (session_id TEXT)")
    conn.commit()
    conn.close()

    assert fetch_sqlite(db_path, "all") == {}


# ==========================================
# fetch_datastore() - faked Datastore client
# ==========================================

class _FakeKey:
    def __init__(self, id_or_name):
        self.id_or_name = id_or_name
        self.id = id_or_name if isinstance(id_or_name, int) else None


class _FakeEntity(dict):
    """Stands in for google.cloud.datastore.Entity: behaves like a plain
    dict of the entity's own properties (so dict(entity) in fetch_datastore
    works unchanged) plus a .key carrying the doc's id_or_name, exactly the
    two things fetch_datastore actually reads off a real Entity."""
    def __init__(self, data, key_id_or_name):
        super().__init__(data)
        self.key = _FakeKey(key_id_or_name)


class _FakeQuery:
    def __init__(self, entities):
        self._entities = entities

    def keys_only(self):
        # Real Query.keys_only() mutates the query to return key-only
        # entities on fetch(); the fake's __kind__ entities are already
        # built key-only (see _FakeDatastoreClient.query below), so there's
        # nothing to change here - it just needs to exist and be callable.
        pass

    def fetch(self):
        return list(self._entities)


class _FakeDatastoreClient:
    """kind_entities: {kind_name: [_FakeEntity, ...]} - the fake's entire
    "database". Constructed via a closure in each test rather than taking
    real Client.__init__'s (project, database, namespace) args directly,
    since those are irrelevant to anything under test here."""
    def __init__(self, kind_entities):
        self._kind_entities = kind_entities

    def query(self, kind=None, namespace=None):
        if kind == "__kind__":
            return _FakeQuery([_FakeEntity({}, name) for name in self._kind_entities])
        return _FakeQuery(self._kind_entities.get(kind, []))


def _install_fake_datastore(monkeypatch, kind_entities):
    """Patches google.cloud.datastore.Client - NOT export_state's own
    namespace, since fetch_datastore does `from google.cloud import
    datastore` as a LOCAL import inside the function body, so there's no
    module-level `export_state.datastore` attribute to patch. The local
    name `datastore` that import binds still refers to the very same
    google.cloud.datastore module object, though, so patching Client on
    that real module is what the local import actually sees at call time."""
    import google.cloud.datastore as real_datastore_module

    def fake_client(project=None, database=None, namespace=None):
        return _FakeDatastoreClient(kind_entities)

    monkeypatch.setattr(real_datastore_module, "Client", fake_client)


def test_fetch_datastore_returns_columns_alphabetically_regardless_of_entity_property_order(monkeypatch):
    # Two entities whose OWN dict key insertion order differs from each
    # other (mirrors two real documents whose property order need not
    # match) - and neither order is alphabetical to begin with.
    entities = [
        _FakeEntity({"zeta_col": "z1", "alpha_col": "a1"}, key_id_or_name="doc1"),
        _FakeEntity({"alpha_col": "a2", "zeta_col": "z2"}, key_id_or_name="doc2"),
    ]
    _install_fake_datastore(monkeypatch, {"translations": entities})

    results = fetch_datastore("translations")

    assert list(results.keys()) == ["translations"]
    # doc_id is appended by fetch_datastore itself, so it's part of the
    # alphabetization too - no special-cased leading position.
    assert list(results["translations"].columns) == ["alpha_col", "doc_id", "zeta_col"]


def test_fetch_datastore_column_order_is_identical_regardless_of_row_fetch_order(monkeypatch):
    """The regression this whole fix exists for: pd.DataFrame(list_of_dicts)
    takes its column order from whichever row is processed FIRST, so if a
    query happens to return the same underlying rows in a different order
    on two separate runs (Datastore gives no ordering guarantee here - see
    sort_columns_alphabetically's docstring), the old code could produce a
    different CSV header between two exports of unchanged data. This
    fabricates that exact scenario - same two entities, reversed order -
    and asserts the resulting column order is now identical either way."""
    entity_a = _FakeEntity({"alpha_col": "a1", "beta_col": "b1"}, key_id_or_name="doc1")
    entity_b = _FakeEntity({"alpha_col": "a2", "gamma_col": "g2"}, key_id_or_name="doc2")

    _install_fake_datastore(monkeypatch, {"translations": [entity_a, entity_b]})
    columns_run_1 = list(fetch_datastore("translations")["translations"].columns)

    _install_fake_datastore(monkeypatch, {"translations": [entity_b, entity_a]})
    columns_run_2 = list(fetch_datastore("translations")["translations"].columns)

    assert columns_run_1 == columns_run_2 == ["alpha_col", "beta_col", "doc_id", "gamma_col"]


def test_fetch_datastore_uses_integer_key_id_when_no_name_is_set(monkeypatch):
    entity = _FakeEntity({"foo": "bar"}, key_id_or_name=42)
    _install_fake_datastore(monkeypatch, {"translations": [entity]})

    df = fetch_datastore("translations")["translations"]
    assert df.iloc[0]["doc_id"] == 42


def test_fetch_datastore_no_kinds_returns_empty_dict(monkeypatch):
    _install_fake_datastore(monkeypatch, {})
    assert fetch_datastore("all") == {}


# ==========================================
# export_union() - global alphabetical order across both sources
# ==========================================

def test_export_union_output_is_fully_alphabetical_across_both_sources(tmp_path, monkeypatch):
    # Datastore and SQLite each contribute a column the OTHER doesn't have
    # (datastore_only_col / sqlite_only_col) - this is the case
    # pd.concat(..., sort=False) alone gets wrong (see export_union's
    # comment): without the extra sort on the combined frame, every
    # datastore-sourced column would land before sqlite_only_col regardless
    # of alphabetical position.
    entity = _FakeEntity({"shared_col": "d", "datastore_only_col": "x"}, key_id_or_name="doc1")
    _install_fake_datastore(monkeypatch, {"translations": [entity]})

    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE translations (shared_col TEXT, sqlite_only_col TEXT)")
    conn.execute("INSERT INTO translations VALUES ('s', 'y')")
    conn.commit()
    conn.close()

    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)
    export_union("translations", output_dir, db_path)

    with open(os.path.join(output_dir, "translations.csv"), newline="") as f:
        header = next(csv.reader(f))

    assert header == sorted(header)
    assert set(header) == {
        "_source", "shared_col", "datastore_only_col", "sqlite_only_col", "doc_id",
    }


def test_export_union_with_only_sqlite_data_is_still_alphabetical(tmp_path, monkeypatch):
    _install_fake_datastore(monkeypatch, {})  # Datastore has nothing.

    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (zeta_col TEXT, alpha_col TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('z', 'a')")
    conn.commit()
    conn.close()

    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)
    export_union("sessions", output_dir, db_path)

    with open(os.path.join(output_dir, "sessions.csv"), newline="") as f:
        header = next(csv.reader(f))

    assert header == ["_source", "alpha_col", "zeta_col"]


# ==========================================
# export_datastore() / export_sqlite() - the CSV each actually writes
# ==========================================

def test_export_sqlite_writes_csv_with_alphabetical_header(tmp_path):
    db_path = _make_sqlite_db(tmp_path)
    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)

    export_sqlite(db_path, "translations", output_dir)

    with open(os.path.join(output_dir, "translations.csv"), newline="") as f:
        header = next(csv.reader(f))
    assert header == ["alpha_col", "id", "zeta_col"]


def test_export_datastore_writes_csv_with_alphabetical_header(tmp_path, monkeypatch):
    entity = _FakeEntity({"zeta_col": "z", "alpha_col": "a"}, key_id_or_name="doc1")
    _install_fake_datastore(monkeypatch, {"translations": [entity]})
    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)

    export_datastore("translations", output_dir)

    with open(os.path.join(output_dir, "translations.csv"), newline="") as f:
        header = next(csv.reader(f))
    assert header == ["alpha_col", "doc_id", "zeta_col"]
