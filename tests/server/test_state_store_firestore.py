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


# --- Bring Your Own Key (llm_byok_keys/llm_byok_key_set/get_llm_byok_key) ------
# Same contract as test_state_store_sqlite.py's mirror section - see
# StateStore.set_session's docstring. The raw key is never exposed through
# get_session() (see llm_byok_key_set's docstring) - only get_llm_byok_key
# (the server-only, call-time accessor) ever returns it.

def test_get_session_defaults_byok_key_set_to_all_false():
    store, client = make_store()
    session = store.get_session("alice")
    assert session["llm_byok_key_set"] == {"google": False, "anthropic": False, "openai": False}


def test_get_llm_byok_key_returns_none_when_never_saved():
    store, client = make_store()
    assert store.get_llm_byok_key("alice", "google") is None


def test_set_session_byok_key_is_reflected_in_key_set_and_get_llm_byok_key():
    store, client = make_store()
    store.set_session("alice", llm_byok_keys={"google": "my-google-key"})
    session = store.get_session("alice")
    assert session["llm_byok_key_set"] == {"google": True, "anthropic": False, "openai": False}
    assert store.get_llm_byok_key("alice", "google") == "my-google-key"
    assert store.get_llm_byok_key("alice", "anthropic") is None


def test_set_session_byok_key_for_one_provider_does_not_touch_another():
    store, client = make_store()
    store.set_session("alice", llm_byok_keys={"google": "my-google-key"})
    store.set_session("alice", llm_byok_keys={"anthropic": "my-claude-key"})
    session = store.get_session("alice")
    assert session["llm_byok_key_set"] == {"google": True, "anthropic": True, "openai": False}
    assert store.get_llm_byok_key("alice", "google") == "my-google-key"
    assert store.get_llm_byok_key("alice", "anthropic") == "my-claude-key"


def test_set_session_byok_key_empty_string_clears_it():
    store, client = make_store()
    store.set_session("alice", llm_byok_keys={"google": "my-google-key"})
    store.set_session("alice", llm_byok_keys={"google": ""})
    session = store.get_session("alice")
    assert session["llm_byok_key_set"]["google"] is False
    assert store.get_llm_byok_key("alice", "google") is None


def test_set_session_without_llm_byok_keys_leaves_saved_keys_untouched():
    store, client = make_store()
    store.set_session("alice", llm_byok_keys={"google": "my-google-key"})
    store.set_session("alice", theme="light", auto_sql_execute=False)
    assert store.get_llm_byok_key("alice", "google") == "my-google-key"
    assert store.get_session("alice")["llm_byok_key_set"]["google"] is True


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


# --- translations (write-only NL->SQL audit log; see record_translation's
# own docstring in state_store.py - there's deliberately no read/purge
# coverage here anymore: get_translation_history()/purge_translation_history()
# and the /api/history[/purge] endpoints they backed were removed as dead
# code once the History modal stopped surfacing them - see
# chat_history_routes.py's module docstring for where that modal's data
# comes from today. record_translation() itself is still live, so it still
# gets a smoke test below, verified by poking the fake client's storage
# directly the same way this file's other tests do.) -------------------------

def test_record_translation_writes_a_doc():
    store, client = make_store()
    store.record_translation(
        "alice", "postgres", "My DB", "show users", "SELECT * FROM users;",
        "gemini-2.5-flash", 120, 10, 5, 15, 0, 0,
    )
    docs = list(client._collections.get("translations", {}).values())
    assert len(docs) == 1
    assert docs[0]["user_id"] == "alice"
    assert docs[0]["nl_prompt"] == "show users"
    assert docs[0]["sql_command"] == "SELECT * FROM users;"
    assert docs[0]["total_tokens"] == 15


def test_translations_are_recorded_independently_per_user():
    store, client = make_store()
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("bob", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    docs = list(client._collections.get("translations", {}).values())
    assert sorted(d["user_id"] for d in docs) == ["alice", "bob"]


# --- chat_history (persisted conversation buckets, client.js's
# chatStoresByBucket) - distinct from translations above, which is the
# separate NL->SQL audit log. ---------------------------------------------

def test_get_chat_history_defaults_to_empty_for_a_new_user():
    store, client = make_store()
    result = store.get_chat_history("alice")
    assert result == {"buckets": {}, "active_bucket_key": ""}


def test_save_and_get_chat_bucket_round_trips():
    store, client = make_store()
    turns = [{"role": "user", "text": "show users"}, {"role": "model", "text": "SELECT * FROM users;"}]
    store.save_chat_bucket("alice", "preset:1", turns)
    result = store.get_chat_history("alice")
    assert result["buckets"] == {"preset:1": turns}


def test_save_chat_bucket_upserts_in_place_not_duplicated():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "first"}])
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "second"}])
    result = store.get_chat_history("alice")
    assert result["buckets"] == {"preset:1": [{"role": "user", "text": "second"}]}


def test_multiple_buckets_for_the_same_user_are_all_returned():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "a"}])
    store.save_chat_bucket("alice", "all", [{"role": "user", "text": "b"}])
    result = store.get_chat_history("alice")
    assert set(result["buckets"].keys()) == {"preset:1", "all"}


def test_chat_history_isolated_per_user():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "a"}])
    store.save_chat_bucket("bob", "preset:1", [{"role": "user", "text": "b"}])
    alice_result = store.get_chat_history("alice")
    bob_result = store.get_chat_history("bob")
    assert alice_result["buckets"] == {"preset:1": [{"role": "user", "text": "a"}]}
    assert bob_result["buckets"] == {"preset:1": [{"role": "user", "text": "b"}]}


def test_save_chat_bucket_with_no_bucket_key_is_a_no_op():
    store, client = make_store()
    store.save_chat_bucket("alice", "", [{"role": "user", "text": "a"}])
    result = store.get_chat_history("alice")
    assert result["buckets"] == {}


def test_set_active_chat_bucket_round_trips():
    store, client = make_store()
    store.set_active_chat_bucket("alice", "preset:1")
    result = store.get_chat_history("alice")
    assert result["active_bucket_key"] == "preset:1"


def test_set_active_chat_bucket_never_touches_a_bucket_own_saved_turns():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "a"}])
    store.set_active_chat_bucket("alice", "all")
    result = store.get_chat_history("alice")
    assert result["buckets"] == {"preset:1": [{"role": "user", "text": "a"}]}
    assert result["active_bucket_key"] == "all"


def test_set_active_chat_bucket_does_not_clobber_an_existing_session_doc():
    # active_chat_bucket_key is stored on the SAME "sessions" doc
    # get_session()/set_session() use - merge=True must leave an already-
    # saved connection_id/theme/etc. completely untouched.
    store, client = make_store()
    store.set_session("alice", connection_id="key123", theme="dark")
    store.set_active_chat_bucket("alice", "preset:1")
    session = store.get_session("alice")
    assert session["connection_id"] == "key123"
    assert session["theme"] == "dark"
    result = store.get_chat_history("alice")
    assert result["active_bucket_key"] == "preset:1"


def test_save_chat_bucket_does_not_set_active_bucket():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "a"}])
    result = store.get_chat_history("alice")
    assert result["active_bucket_key"] == ""


def test_chat_history_bucket_with_unrecognized_future_schema_version_is_omitted():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "a"}])
    doc_id = "alice_preset:1"
    client._collections["chat_history"][doc_id]["schema_version"] = 999
    result = store.get_chat_history("alice")
    assert result["buckets"] == {}


def test_chat_history_bucket_with_non_list_payload_is_omitted():
    store, client = make_store()
    client._collections.setdefault("chat_history", {})["alice_preset:1"] = {
        "user_id": "alice", "bucket_key": "preset:1",
        "payload": {"not": "a list"}, "schema_version": 1,
    }
    result = store.get_chat_history("alice")
    assert result["buckets"] == {}


def test_one_corrupt_bucket_does_not_prevent_other_buckets_from_loading():
    store, client = make_store()
    store.save_chat_bucket("alice", "preset:1", [{"role": "user", "text": "good"}])
    store.save_chat_bucket("alice", "all", [{"role": "user", "text": "also good"}])
    client._collections["chat_history"]["alice_preset:1"]["schema_version"] = 999
    result = store.get_chat_history("alice")
    assert result["buckets"] == {"all": [{"role": "user", "text": "also good"}]}
