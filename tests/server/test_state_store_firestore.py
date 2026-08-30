"""
FirestoreStateStore, exercised against helpers.FakeFirestoreClient - an
in-memory fake that reproduces real Firestore's merge=True (recursive)
vs. merge=[field, ...] (atomic per-field) semantics, and firestore.
DELETE_FIELD, closely enough to catch the class of bug this file's
regression tests are named for (see FakeFirestoreClient's docstring and
state_store.py's FirestoreStateStore.set_session comment for the full
story).
"""

import sys
import types

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import state_store
from state_store import FirestoreStateStore
from helpers import FakeFirestoreClient


def make_store():
    client = FakeFirestoreClient()
    store = FirestoreStateStore(client)
    return store, client


# --- sessions --------------------------------------------------------------
# A session stores only an identity reference (is_custom, connection_id) -
# never a connection's actual details/credentials (see state_store.py's
# module/class docstrings and db.py's resolve_active_descriptor, which
# resolves those fresh every time something needs to actually connect).

def test_get_session_defaults_for_unknown_user():
    store, client = make_store()
    session = store.get_session("alice")
    assert session["is_custom"] is False
    assert session["connection_id"] == ""
    assert session["auto_sql_execute"] is True


def test_get_session_with_no_user_id_returns_defaults_without_touching_client():
    store, client = make_store()
    session = store.get_session(None)
    assert session["connection_id"] == ""
    assert client._collections == {}


def test_set_and_get_session_round_trip():
    store, client = make_store()
    store.set_session(
        "alice", connection_id="key123", is_custom=True, auto_sql_execute=False,
    )
    session = store.get_session("alice")
    assert session["connection_id"] == "key123"
    assert session["is_custom"] is True
    assert session["auto_sql_execute"] is False


def test_set_session_with_no_user_id_is_a_no_op():
    store, client = make_store()
    store.set_session(None, connection_id="abc")
    assert client._collections == {}


# --- llm_provider / llm_model (model-selection UI) --------------------------
# Same "" -> "nothing explicitly selected yet" convention connection_id
# already uses - see get_session's docstring in state_store.py.

def test_get_session_defaults_llm_fields_to_blank():
    store, client = make_store()
    session = store.get_session("alice")
    assert session["llm_provider"] == ""
    assert session["llm_model"] == ""


def test_set_and_get_session_round_trips_llm_fields():
    store, client = make_store()
    store.set_session("alice", llm_provider="openai", llm_model="gpt-5.6-luna")
    session = store.get_session("alice")
    assert session["llm_provider"] == "openai"
    assert session["llm_model"] == "gpt-5.6-luna"
    assert session["connection_id"] == ""  # untouched


def test_set_session_llm_fields_do_not_clobber_connection_fields():
    store, client = make_store()
    store.set_session("alice", connection_id="k1", is_custom=True)
    store.set_session("alice", llm_provider="anthropic", llm_model="claude-sonnet-5")
    session = store.get_session("alice")
    assert session["connection_id"] == "k1"  # untouched
    assert session["is_custom"] is True  # untouched
    assert session["llm_provider"] == "anthropic"
    assert session["llm_model"] == "claude-sonnet-5"


def test_set_session_leaves_untouched_top_level_fields_alone():
    store, client = make_store()
    store.set_session("alice", connection_id="k1", is_custom=True)
    store.set_session("alice", auto_sql_execute=False)  # only this field this time
    session = store.get_session("alice")
    assert session["connection_id"] == "k1"  # untouched
    assert session["is_custom"] is True  # untouched
    assert session["auto_sql_execute"] is False  # updated


# --- theme (Preferences modal) -----------------------------------------------
# Same "" -> "nothing explicitly saved yet, let the client's own current/
# localStorage value keep applying" convention llm_provider/llm_model already
# use - see get_session's docstring in state_store.py - not
# auto_sql_execute's baked-in-default one.

def test_get_session_defaults_theme_to_blank():
    store, client = make_store()
    session = store.get_session("alice")
    assert session["theme"] == ""


def test_set_and_get_session_round_trips_theme():
    store, client = make_store()
    store.set_session("alice", theme="light")
    session = store.get_session("alice")
    assert session["theme"] == "light"
    assert session["connection_id"] == ""  # untouched


def test_set_session_theme_does_not_clobber_connection_fields():
    store, client = make_store()
    store.set_session("alice", connection_id="k1", is_custom=True)
    store.set_session("alice", theme="light")
    session = store.get_session("alice")
    assert session["connection_id"] == "k1"  # untouched
    assert session["is_custom"] is True  # untouched
    assert session["theme"] == "light"


def test_set_session_leaves_theme_untouched_by_other_field_updates():
    store, client = make_store()
    store.set_session("alice", theme="light")
    store.set_session("alice", auto_sql_execute=False)  # only this field this time
    session = store.get_session("alice")
    assert session["theme"] == "light"  # untouched
    assert session["auto_sql_execute"] is False  # updated


# --- lazy migration: legacy sessions predating connection_id ------------------

def test_get_session_lazily_migrates_legacy_custom_connection_doc():
    store, client = make_store()
    # Simulate a pre-migration doc: the full duplicated descriptor shape,
    # no connection_id field at all yet.
    client.collection("sessions").document("alice").set({
        "database_url": "bigquery://p/d",
        "database_type": "bigquery",
        "database_config": {"project_id": "p", "dataset": "d", "credentials_json": "STALE_KEY"},
        "is_custom": True,
        "custom_connection_key": "custom-key-123",
        "auto_sql_execute": False,
    })

    session = store.get_session("alice")
    assert session["is_custom"] is True
    assert session["connection_id"] == "custom-key-123"  # reused as-is
    assert session["auto_sql_execute"] is False

    # The write-back must actually delete the old fields (firestore.
    # DELETE_FIELD), not just add connection_id alongside them - a
    # credential (credentials_json here) must not linger in the document.
    raw = client.collection("sessions").document("alice").get().to_dict()
    assert "database_url" not in raw
    assert "database_type" not in raw
    assert "database_config" not in raw
    assert "custom_connection_key" not in raw
    assert raw["connection_id"] == "custom-key-123"
    assert raw["is_custom"] is True

    # And a second read (now already migrated) is stable/idempotent.
    session_again = store.get_session("alice")
    assert session_again["connection_id"] == "custom-key-123"


def test_get_session_lazily_migrates_legacy_preset_doc_by_matching_url(monkeypatch):
    store, client = make_store()
    client.collection("sessions").document("bob").set({
        "database_url": "postgresql://preset-match/db",
        "database_type": "postgres",
        "is_custom": False,
        "custom_connection_key": "",
        "auto_sql_execute": True,
    })

    fake_app_config = types.ModuleType("app_config")
    fake_app_config.CONFIGURED_DBS = [
        {"id": "postgres+Preset Match", "name": "Preset Match", "type": "postgres",
         "url": "postgresql://preset-match/db"},
    ]
    monkeypatch.setitem(sys.modules, "app_config", fake_app_config)

    session = store.get_session("bob")
    assert session["is_custom"] is False
    assert session["connection_id"] == "postgres+Preset Match"


def test_get_session_lazily_migrates_legacy_preset_doc_with_no_matching_preset(monkeypatch):
    store, client = make_store()
    client.collection("sessions").document("carol").set({
        "database_url": "postgresql://no-longer-configured/db",
        "database_type": "postgres",
        "is_custom": False,
        "custom_connection_key": "",
        "auto_sql_execute": True,
    })

    fake_app_config = types.ModuleType("app_config")
    fake_app_config.CONFIGURED_DBS = []
    monkeypatch.setitem(sys.modules, "app_config", fake_app_config)

    session = store.get_session("carol")
    assert session["is_custom"] is False
    assert session["connection_id"] == ""  # nothing matched -> resolves to app default downstream


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


# --- translation history list cap (TRANSLATION_HISTORY_LIST_LIMIT) -------------
# record_translation() stamps created_at with firestore.SERVER_TIMESTAMP,
# which the fake client stores verbatim (it has no notion of "server time")
# - not something these tests can use to control ordering. These insert
# docs directly into client._collections instead, using plain datetime
# objects as created_at (real Firestore's own representation once a
# SERVER_TIMESTAMP resolves), same "poke the fake's storage directly" idiom
# FakeFirestoreClient's own docstring calls out.

def _insert_translation_doc(client, doc_id, user_id, nl_prompt, created_at):
    coll = client._collections.setdefault("translations", {})
    coll[doc_id] = {
        "user_id": user_id, "database_type": "postgres", "database_name": "DB",
        "nl_prompt": nl_prompt, "sql_command": "SELECT 1;", "model": "m",
        "duration": 1, "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        "thinking_tokens": 0, "cached_content_tokens": 0, "created_at": created_at,
    }


def test_get_translation_history_defaults_to_50_row_limit():
    import datetime
    store, client = make_store()
    for i in range(60):
        _insert_translation_doc(
            client, f"doc{i}", "alice", f"p{i}",
            datetime.datetime(2024, 1, 1, 0, i, 0),
        )
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 60  # uncapped
    assert len(rows) == 50  # capped
    assert sum(s["total_translations"] for s in stats) == 60  # stats: complete history


def test_get_translation_history_list_is_sorted_newest_first():
    import datetime
    store, client = make_store()
    _insert_translation_doc(client, "d1", "alice", "oldest", datetime.datetime(2024, 1, 1))
    _insert_translation_doc(client, "d2", "alice", "middle", datetime.datetime(2024, 1, 2))
    _insert_translation_doc(client, "d3", "alice", "newest", datetime.datetime(2024, 1, 3))
    rows, _, _ = store.get_translation_history("alice")
    assert [r["nl_prompt"] for r in rows] == ["newest", "middle", "oldest"]


def test_get_translation_history_limit_is_configurable_via_env_var(monkeypatch):
    import datetime
    # Patches the SAME module object FirestoreStateStore's globals resolve
    # TRANSLATION_HISTORY_LIST_LIMIT from - see the equivalent SQLite test's
    # comment in test_state_store_sqlite.py for why this must be the
    # module-level `import state_store` above, not a function-local one.
    monkeypatch.setattr(state_store, "TRANSLATION_HISTORY_LIST_LIMIT", 3)
    store, client = make_store()
    for i in range(10):
        _insert_translation_doc(
            client, f"doc{i}", "alice", f"p{i}",
            datetime.datetime(2024, 1, 1, 0, i, 0),
        )
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 10  # uncapped
    assert len(rows) == 3  # capped to the overridden limit
    assert [r["nl_prompt"] for r in rows] == ["p9", "p8", "p7"]  # still newest-first
    assert sum(s["total_translations"] for s in stats) == 10  # stats: complete history
