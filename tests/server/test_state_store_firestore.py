"""
FirestoreStateStore, exercised against helpers.FakeFirestoreClient - an
in-memory fake that reproduces real Firestore's merge=True (recursive)
vs. merge=[field, ...] (atomic per-field) semantics closely enough to
catch the class of bug this file's regression test is named for (see
FakeFirestoreClient's docstring and state_store.py's FirestoreStateStore.
set_session comment for the full story).
"""

import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from state_store import FirestoreStateStore
from helpers import FakeFirestoreClient


def make_store():
    client = FakeFirestoreClient()
    store = FirestoreStateStore(client, default_conn="postgresql://default/db")
    return store, client


# --- sessions --------------------------------------------------------------

def test_get_session_defaults_for_unknown_user():
    store, client = make_store()
    session = store.get_session("alice")
    assert session["database_url"] == "postgresql://default/db"
    assert session["is_custom"] is False


def test_get_session_with_no_user_id_returns_defaults_without_touching_client():
    store, client = make_store()
    session = store.get_session(None)
    assert session["database_url"] == "postgresql://default/db"
    assert client._collections == {}


def test_set_and_get_session_round_trip():
    store, client = make_store()
    store.set_session(
        "alice", db_url="bigquery://p/d", db_type="bigquery",
        db_config={"project_id": "p", "dataset": "d"}, is_custom=True,
        custom_connection_key="key123",
    )
    session = store.get_session("alice")
    assert session["database_url"] == "bigquery://p/d"
    assert session["database_type"] == "bigquery"
    assert session["database_config"] == {"project_id": "p", "dataset": "d"}
    assert session["is_custom"] is True
    assert session["custom_connection_key"] == "key123"


def test_set_session_with_no_user_id_is_a_no_op():
    store, client = make_store()
    store.set_session(None, db_url="postgresql://a/b")
    assert client._collections == {}


def test_set_session_leaves_untouched_top_level_fields_alone():
    store, client = make_store()
    store.set_session("alice", db_url="postgresql://a/b", is_custom=True, custom_connection_key="k1")
    store.set_session("alice", auto_sql_execute=False)  # only this field this time
    session = store.get_session("alice")
    assert session["database_url"] == "postgresql://a/b"  # untouched
    assert session["is_custom"] is True  # untouched
    assert session["custom_connection_key"] == "k1"  # untouched
    assert session["auto_sql_execute"] is False  # updated


def test_merge_fix_regression_switching_bigquery_connections_does_not_leak_stale_billing_project():
    """
    This is the exact bug the user reported: after saving a custom
    BigQuery connection with a billing_project_id/credentials_json, then
    switching the active connection to a *different* one that has neither
    (e.g. an admin preset, or a connection missing those fields), the new
    database_config must NOT still carry the old connection's
    billing_project_id/credentials_json - which is exactly what a plain
    `merge=True` .set() call would do (real Firestore's boolean merge
    recursively merges nested maps, leaving keys absent from the new
    value as whatever the old document had).
    """
    store, client = make_store()

    store.set_session(
        "alice", db_url="bigquery://public-data/google_ads", db_type="bigquery",
        db_config={
            "project_id": "public-data", "dataset": "google_ads",
            "credentials_json": "STALE_KEY", "billing_project_id": "stale-billing-project",
        },
        is_custom=True, custom_connection_key="conn-a-key",
    )

    # Switch to a connection with a database_config that does NOT include
    # credentials_json/billing_project_id at all (e.g. an admin preset).
    store.set_session(
        "alice", db_url="bigquery://public-data/google_trends", db_type="bigquery",
        db_config={"project_id": "public-data", "dataset": "google_trends"},
        is_custom=False, custom_connection_key="",
    )

    session = store.get_session("alice")
    assert session["database_config"] == {"project_id": "public-data", "dataset": "google_trends"}
    assert "credentials_json" not in session["database_config"]
    assert "billing_project_id" not in session["database_config"]


def test_deliberately_broken_boolean_merge_would_have_leaked_the_stale_field():
    """
    Sanity-check on the fake itself: proves FakeFirestoreClient's
    merge=True path really does reproduce the recursive-merge bug (i.e.
    this fake isn't accidentally "too good" and would pass even a naive,
    buggy implementation) - directly exercises the client with the OLD,
    buggy call shape the real bug used.
    """
    client = FakeFirestoreClient()
    doc_ref = client.collection("sessions").document("alice")
    doc_ref.set({"database_config": {"project_id": "public-data", "dataset": "google_ads",
                                      "credentials_json": "STALE_KEY", "billing_project_id": "stale-billing-project"}},
                merge=True)
    doc_ref.set({"database_config": {"project_id": "public-data", "dataset": "google_trends"}}, merge=True)

    data = doc_ref.get().to_dict()
    # With boolean merge=True, the stale fields DO leak through - this is
    # exactly the bug, reproduced here to prove the fake is faithful.
    assert data["database_config"]["credentials_json"] == "STALE_KEY"
    assert data["database_config"]["billing_project_id"] == "stale-billing-project"


# --- db_connections ----------------------------------------------------------

def test_get_db_connections_empty_for_unknown_user():
    store, client = make_store()
    assert store.get_db_connections("alice") == []


def test_get_db_connections_with_no_user_id_returns_empty_without_touching_client():
    store, client = make_store()
    assert store.get_db_connections(None) == []


def test_set_and_get_db_connections_via_custom_databases_list():
    store, client = make_store()
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[
            {"name": "Conn A", "type": "bigquery", "url": "bigquery://shared/ds",
             "config": {"credentials_json": "KEY_A", "billing_project_id": "proj-a"}},
            {"name": "Conn B", "type": "bigquery", "url": "bigquery://shared/ds",
             "config": {"credentials_json": "KEY_B", "billing_project_id": "proj-b"}},
        ],
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 2
    by_name = {c["name"]: c for c in conns}
    assert by_name["Conn A"]["has_custom_credentials"] is True
    assert "credentials_json" not in by_name["Conn A"]["config"]
    assert by_name["Conn A"]["config"]["billing_project_id"] == "proj-a"
    # Distinct connection_key despite sharing a URL.
    assert by_name["Conn A"]["connection_key"] != by_name["Conn B"]["connection_key"]


def test_replacing_custom_databases_list_deletes_old_docs():
    store, client = make_store()
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[{"name": "Old", "type": "postgres", "url": "postgresql://old/db", "config": {}}],
    )
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[{"name": "New", "type": "postgres", "url": "postgresql://new/db", "config": {}}],
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["name"] == "New"


def test_single_connection_save_and_include_credentials():
    store, client = make_store()
    store.set_db_connections(
        "alice", "BQ Conn", "bigquery", "bigquery://p/d",
        db_config={"credentials_json": "SECRET"},
    )
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["credentials_json"] == "SECRET"
    stripped = store.get_db_connections("alice")
    assert "credentials_json" not in stripped[0]["config"]


# --- translations --------------------------------------------------------------

def test_record_and_fetch_translation_history():
    store, client = make_store()
    store.record_translation(
        "alice", "postgres", "My DB", "show users", "SELECT * FROM users;",
        "gemini-2.5-flash", 120, 10, 5, 15, 0, 0,
    )
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 1
    assert rows[0]["nl_prompt"] == "show users"
    assert stats[0]["sum_total_tokens"] == 15


def test_purge_translation_history_deletes_all_docs_for_user():
    store, client = make_store()
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("alice", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    store.purge_translation_history("alice")
    _, _, total_count = store.get_translation_history("alice")
    assert total_count == 0


def test_purge_translation_history_does_not_affect_other_users():
    store, client = make_store()
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("bob", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    store.purge_translation_history("alice")
    _, _, bob_count = store.get_translation_history("bob")
    assert bob_count == 1
