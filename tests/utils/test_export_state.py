"""
tests/utils/test_export_state.py

Covers utils/export_state.py, with most of the weight on the guarantee that
motivated sort_columns_alphabetically(): every exported CSV's column order
must be alphabetical and reproducible, regardless of source (Datastore/
Firestore, SQLite, or the two unioned together) and regardless of anything
about the underlying data's own ordering (a Datastore entity's own property
order, or which row a query happens to return first). See that function's
docstring in export_state.py for the full reasoning.

Firestore is faked with a minimal stand-in for the tiny slice of the real
google-cloud-firestore API export_state.py actually calls
(Client.collections(), Client.collection(name).stream(), and a document
snapshot that behaves like an object with .id/.to_dict()) - there's no
real GCP project/credentials in a test environment, and none of the
column-ordering logic under test cares about anything past that surface.
SQLite is exercised against a real, throwaway on-disk database (via
tmp_path) rather than faked, since fetch_sqlite's own job here -
schema-defined column order via `SELECT *` - is only meaningful against a
real sqlite3 connection.

fetch_firestore()/export_firestore() were previously named
fetch_datastore()/export_datastore() and faked a completely different
API shape here (google.cloud.datastore.Client.query()/Query.fetch()/
Entity) - see export_state.py's own fetch_firestore() docstring for why
that implementation was actually broken against this app's real Firestore
database (wrong database name, and more fundamentally the wrong client
API/mode entirely) despite this same test file passing 100% green the
whole time - these fakes mocked away the exact two things that were
wrong, which is precisely why the bug went undetected here.
"""

import csv
import os
import sqlite3

import pandas as pd
import pytest

from export_state import (
    VALID_TABLES,
    fetch_firestore,
    fetch_sqlite,
    export_firestore,
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
# llm_usage - the token-usage tracking table (separate from "translations",
# see state_store.py's StateStore.record_llm_usage docstring), covered on
# its own here to confirm export_state.py needs no table-specific logic to
# support a new table beyond being told it's valid (VALID_TABLES/the
# --table CLI choices) - fetch_sqlite/export_sqlite themselves are already
# fully generic over column names/table names.
# ==========================================

def test_valid_tables_includes_llm_usage():
    assert "llm_usage" in VALID_TABLES


def _make_llm_usage_sqlite_db(tmp_path, filename="state.db"):
    db_path = str(tmp_path / filename)
    conn = sqlite3.connect(db_path)
    # Same column shape/order as state_store.py's own CREATE TABLE - not
    # alphabetical, same "leaks straight into SELECT * order" case
    # sort_columns_alphabetically exists to normalize away.
    conn.execute("""
        CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            call_type TEXT,
            dataset_type TEXT,
            dataset_name TEXT,
            model TEXT,
            input_tokens INTEGER,
            cached_content_tokens INTEGER,
            thinking_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO llm_usage ("
        "user_id, call_type, dataset_type, dataset_name, model, "
        "input_tokens, cached_content_tokens, thinking_tokens, output_tokens, total_tokens"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("anonymous:abc123", "triage", "postgres", "E-Commerce Store", "gemini-3.6-flash", 100, 10, 5, 20, 120),
    )
    conn.commit()
    conn.close()
    return db_path


def test_fetch_sqlite_exports_llm_usage_table_alphabetically(tmp_path):
    db_path = _make_llm_usage_sqlite_db(tmp_path)
    results = fetch_sqlite(db_path, "llm_usage")

    assert list(results.keys()) == ["llm_usage"]
    assert list(results["llm_usage"].columns) == [
        "cached_content_tokens", "call_type", "created_at", "dataset_name", "dataset_type",
        "id", "input_tokens", "model", "output_tokens", "thinking_tokens", "total_tokens", "user_id",
    ]
    row = results["llm_usage"].iloc[0]
    assert row["call_type"] == "triage"
    assert row["dataset_type"] == "postgres"
    assert row["dataset_name"] == "E-Commerce Store"
    assert row["user_id"] == "anonymous:abc123"
    assert row["total_tokens"] == 120


def test_fetch_sqlite_all_includes_llm_usage_alongside_other_tables(tmp_path):
    db_path = _make_sqlite_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE llm_usage (call_type TEXT, model TEXT)")
    conn.execute("INSERT INTO llm_usage VALUES ('summary', 'gpt-6-sol')")
    conn.commit()
    conn.close()

    results = fetch_sqlite(db_path, "all")

    assert set(results.keys()) == {"translations", "sessions", "llm_usage"}


def test_export_sqlite_writes_llm_usage_csv_with_alphabetical_header(tmp_path):
    db_path = _make_llm_usage_sqlite_db(tmp_path)
    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)

    export_sqlite(db_path, "llm_usage", output_dir)

    with open(os.path.join(output_dir, "llm_usage.csv"), newline="") as f:
        header = next(csv.reader(f))
    assert header == sorted(header)
    assert "call_type" in header and "dataset_type" in header and "dataset_name" in header


# ==========================================
# fetch_firestore() - faked Firestore client
# ==========================================

class _FakeDocSnapshot:
    """Stands in for a real google.cloud.firestore DocumentSnapshot:
    .id plus a .to_dict() of the document's own fields, exactly the two
    things fetch_firestore actually reads off a real snapshot."""
    def __init__(self, data, doc_id):
        self._data = data
        self.id = doc_id

    def to_dict(self):
        return dict(self._data)


class _FakeCollectionRef:
    def __init__(self, docs):
        self._docs = docs

    def stream(self):
        return list(self._docs)


class _FakeCollectionId:
    """Stands in for a real CollectionReference as returned by
    Client.collections() - fetch_firestore only ever reads its .id."""
    def __init__(self, collection_id):
        self.id = collection_id


class _FakeFirestoreClient:
    """collection_docs: {collection_name: [_FakeDocSnapshot, ...]} - the
    fake's entire "database". Constructed via a closure in each test
    rather than taking real Client.__init__'s (project,) arg directly,
    since that's irrelevant to anything under test here."""
    def __init__(self, collection_docs):
        self._collection_docs = collection_docs

    def collections(self):
        return [_FakeCollectionId(name) for name in self._collection_docs]

    def collection(self, name):
        return _FakeCollectionRef(self._collection_docs.get(name, []))


def _install_fake_firestore(monkeypatch, collection_docs):
    """Patches google.cloud.firestore.Client - NOT export_state's own
    namespace, since fetch_firestore does `from google.cloud import
    firestore` as a LOCAL import inside the function body, so there's no
    module-level `export_state.firestore` attribute to patch. The local
    name `firestore` that import binds still refers to the very same
    google.cloud.firestore module object, though, so patching Client on
    that real module is what the local import actually sees at call time."""
    import google.cloud.firestore as real_firestore_module

    def fake_client(project=None):
        return _FakeFirestoreClient(collection_docs)

    monkeypatch.setattr(real_firestore_module, "Client", fake_client)


def test_fetch_firestore_returns_columns_alphabetically_regardless_of_document_field_order(monkeypatch):
    # Two documents whose OWN field insertion order differs from each
    # other - and neither order is alphabetical to begin with.
    docs = [
        _FakeDocSnapshot({"zeta_col": "z1", "alpha_col": "a1"}, doc_id="doc1"),
        _FakeDocSnapshot({"alpha_col": "a2", "zeta_col": "z2"}, doc_id="doc2"),
    ]
    _install_fake_firestore(monkeypatch, {"translations": docs})

    results = fetch_firestore("translations")

    assert list(results.keys()) == ["translations"]
    # doc_id is appended by fetch_firestore itself, so it's part of the
    # alphabetization too - no special-cased leading position.
    assert list(results["translations"].columns) == ["alpha_col", "doc_id", "zeta_col"]


def test_fetch_firestore_column_order_is_identical_regardless_of_row_fetch_order(monkeypatch):
    """The regression this whole fix exists for: pd.DataFrame(list_of_dicts)
    takes its column order from whichever row is processed FIRST, so if a
    query happens to return the same underlying rows in a different order
    on two separate runs (Firestore gives no ordering guarantee here - see
    sort_columns_alphabetically's docstring), the old code could produce a
    different CSV header between two exports of unchanged data. This
    fabricates that exact scenario - same two documents, reversed order -
    and asserts the resulting column order is now identical either way."""
    doc_a = _FakeDocSnapshot({"alpha_col": "a1", "beta_col": "b1"}, doc_id="doc1")
    doc_b = _FakeDocSnapshot({"alpha_col": "a2", "gamma_col": "g2"}, doc_id="doc2")

    _install_fake_firestore(monkeypatch, {"translations": [doc_a, doc_b]})
    columns_run_1 = list(fetch_firestore("translations")["translations"].columns)

    _install_fake_firestore(monkeypatch, {"translations": [doc_b, doc_a]})
    columns_run_2 = list(fetch_firestore("translations")["translations"].columns)

    assert columns_run_1 == columns_run_2 == ["alpha_col", "beta_col", "doc_id", "gamma_col"]


def test_fetch_firestore_uses_the_real_document_id(monkeypatch):
    doc = _FakeDocSnapshot({"foo": "bar"}, doc_id="world-stats-conn-hash")
    _install_fake_firestore(monkeypatch, {"translations": [doc]})

    df = fetch_firestore("translations")["translations"]
    assert df.iloc[0]["doc_id"] == "world-stats-conn-hash"


def test_fetch_firestore_no_collections_returns_empty_dict(monkeypatch):
    _install_fake_firestore(monkeypatch, {})
    assert fetch_firestore("all") == {}


def test_fetch_firestore_all_includes_schema_cache_alongside_other_collections(monkeypatch):
    # Regression test for the missing-from-VALID_TABLES bug: "all" must
    # actually reach schema_cache like every other real collection, not
    # just the five this file originally shipped with.
    doc = _FakeDocSnapshot({"schema_text": "CREATE TABLE ..."}, doc_id="postgres+World Stats")
    _install_fake_firestore(monkeypatch, {
        "translations": [_FakeDocSnapshot({"nl_prompt": "hi"}, doc_id="t1")],
        "schema_cache": [doc],
    })

    results = fetch_firestore("all")

    assert set(results.keys()) == {"translations", "schema_cache"}
    assert results["schema_cache"].iloc[0]["doc_id"] == "postgres+World Stats"


# ==========================================
# export_union() - global alphabetical order across both sources
# ==========================================

def test_export_union_output_is_fully_alphabetical_across_both_sources(tmp_path, monkeypatch):
    # Firestore and SQLite each contribute a column the OTHER doesn't have
    # (firestore_only_col / sqlite_only_col) - this is the case
    # pd.concat(..., sort=False) alone gets wrong (see export_union's
    # comment): without the extra sort on the combined frame, every
    # firestore-sourced column would land before sqlite_only_col regardless
    # of alphabetical position.
    doc = _FakeDocSnapshot({"shared_col": "d", "firestore_only_col": "x"}, doc_id="doc1")
    _install_fake_firestore(monkeypatch, {"translations": [doc]})

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
        "_source", "shared_col", "firestore_only_col", "sqlite_only_col", "doc_id",
    }


def test_export_union_with_only_sqlite_data_is_still_alphabetical(tmp_path, monkeypatch):
    _install_fake_firestore(monkeypatch, {})  # Firestore has nothing.

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
# export_firestore() / export_sqlite() - the CSV each actually writes
# ==========================================

def test_export_sqlite_writes_csv_with_alphabetical_header(tmp_path):
    db_path = _make_sqlite_db(tmp_path)
    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)

    export_sqlite(db_path, "translations", output_dir)

    with open(os.path.join(output_dir, "translations.csv"), newline="") as f:
        header = next(csv.reader(f))
    assert header == ["alpha_col", "id", "zeta_col"]


def test_export_firestore_writes_csv_with_alphabetical_header(tmp_path, monkeypatch):
    doc = _FakeDocSnapshot({"zeta_col": "z", "alpha_col": "a"}, doc_id="doc1")
    _install_fake_firestore(monkeypatch, {"translations": [doc]})
    output_dir = str(tmp_path / "out")
    os.makedirs(output_dir)

    export_firestore("translations", output_dir)

    with open(os.path.join(output_dir, "translations.csv"), newline="") as f:
        header = next(csv.reader(f))
    assert header == ["alpha_col", "doc_id", "zeta_col"]


# ==========================================
# VALID_TABLES - schema_cache regression coverage
# ==========================================

def test_valid_tables_includes_schema_cache():
    # schema_cache was missing from VALID_TABLES for a while - this
    # utility predates that collection - so "export everything" silently
    # never reached it even once the rest of this file was pointed at the
    # right database/API. See utils/migrate_firestore_to_default_db.py's
    # own docstring for the full list of six collections this app
    # actually uses.
    assert "schema_cache" in VALID_TABLES
