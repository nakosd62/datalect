"""
FirestoreStateStore, exercised against helpers.FakeFirestoreClient - an
in-memory fake that reproduces real Firestore's merge=True (recursive)
vs. merge=[field, ...] (atomic per-field) semantics, and firestore.
DELETE_FIELD, closely enough to catch the class of bug this file's
regression tests are named for (see FakeFirestoreClient's docstring and
state_store.py's FirestoreStateStore.set_session comment for the full
story).
"""

import hashlib
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


# --- schema_cache (the only storage schema_cache.py has - see that module's
# own docstring for why it no longer keeps any process-local copy alongside
# this) ---------------------------------------------------------------------
# cache_key here is whatever db.py's get_conn_identifier() produced for a
# real connection - for several dialects (Postgres/MySQL, in particular)
# that's a literal "user@host:port/dbname", containing "/" - the exact
# shape _schema_cache_doc_id() exists to hash into a plain hex string
# rather than pass straight to .document(), which would otherwise split it
# into alternating collection/document path segments (see that method's
# own docstring in state_store.py). Every test below uses a cache_key
# containing "/" specifically so it can't silently pass by accident.

def test_get_cached_schema_returns_none_for_an_unknown_key():
    store, client = make_store()
    assert store.get_cached_schema("alice@host:5432/db") is None


def test_set_then_get_cached_schema_round_trips_text_and_cached_at():
    store, client = make_store()
    store.set_cached_schema("alice@host:5432/db", "Table: t\n  id integer NOT NULL", "2026-01-01T00:00:00+00:00")
    row = store.get_cached_schema("alice@host:5432/db")
    assert row["schema_text"] == "Table: t\n  id integer NOT NULL"
    assert row["cached_at"] == "2026-01-01T00:00:00+00:00"
    assert row["overview"] is None


def test_cache_key_containing_a_slash_is_stored_under_a_hashed_document_id_not_the_raw_key():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema(cache_key, "Table: t", "2026-01-01T00:00:00+00:00")

    expected_doc_id = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    coll = client._collections["schema_cache"]
    assert expected_doc_id in coll
    assert cache_key not in coll  # never used as the raw document id
    # The original cache_key is still stored as a plain field, so a saved
    # document can be identified/debugged without reversing the hash.
    assert coll[expected_doc_id]["cache_key"] == cache_key


def test_two_different_cache_keys_that_both_contain_slashes_do_not_collide():
    store, client = make_store()
    store.set_cached_schema("alice@host:5432/db_one", "SCHEMA ONE", "2026-01-01T00:00:00+00:00")
    store.set_cached_schema("bob@host:5432/db_two", "SCHEMA TWO", "2026-01-02T00:00:00+00:00")
    assert store.get_cached_schema("alice@host:5432/db_one")["schema_text"] == "SCHEMA ONE"
    assert store.get_cached_schema("bob@host:5432/db_two")["schema_text"] == "SCHEMA TWO"


def test_set_cached_schema_never_clobbers_a_previously_saved_overview():
    # merge=True semantics: a later set_cached_schema() call for the same
    # key must never wipe out an overview a separate, earlier LLM call
    # already saved - see set_cached_schema's own comment in state_store.py.
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema(cache_key, "FIRST", "2026-01-01T00:00:00+00:00")
    store.set_cached_schema_overview(cache_key, {"prose": "A sales dataset.", "questions": ["Top region?"]})

    store.set_cached_schema(cache_key, "SECOND", "2026-02-02T00:00:00+00:00")

    row = store.get_cached_schema(cache_key)
    assert row["schema_text"] == "SECOND"
    assert row["overview"] == {"prose": "A sales dataset.", "questions": ["Top region?"]}


def test_set_cached_schema_overview_never_clobbers_schema_text_or_cached_at():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema(cache_key, "Table: t", "2026-01-01T00:00:00+00:00")

    store.set_cached_schema_overview(cache_key, {"prose": "desc", "questions": ["q1"]})

    row = store.get_cached_schema(cache_key)
    assert row["overview"] == {"prose": "desc", "questions": ["q1"]}
    assert row["schema_text"] == "Table: t"
    assert row["cached_at"] == "2026-01-01T00:00:00+00:00"


def test_set_cached_schema_overview_before_any_schema_text_exists_still_works():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema_overview(cache_key, {"prose": "desc", "questions": []})
    row = store.get_cached_schema(cache_key)
    assert row["overview"] == {"prose": "desc", "questions": []}
    assert row["schema_text"] is None
    assert row["cached_at"] is None


def test_delete_cached_schema_removes_the_document():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema(cache_key, "Table: t", "2026-01-01T00:00:00+00:00")

    store.delete_cached_schema(cache_key)

    assert store.get_cached_schema(cache_key) is None
    doc_id = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    assert doc_id not in client._collections.get("schema_cache", {})


def test_delete_cached_schema_for_a_never_cached_key_is_a_no_op():
    store, client = make_store()
    store.delete_cached_schema("never@cached:5432/db")  # must not raise


# --- schema_cache fetch-pending/fetch-error status --------------------------
# Durable now for the same cross-instance-visibility reason schema_text
# itself is (see schema_cache.py's own module docstring): a background
# refetch's in-flight status must be visible to a poll landing on a
# different Cloud Run instance than the one running the fetch.

def test_get_schema_fetch_status_for_a_never_fetched_key_is_not_pending_no_error():
    store, client = make_store()
    assert store.get_schema_fetch_status("alice@host:5432/db") == {"pending": False, "error": None}


def test_mark_schema_fetch_pending_then_get_schema_fetch_status_reports_it():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.mark_schema_fetch_pending(cache_key)
    assert store.get_schema_fetch_status(cache_key) == {"pending": True, "error": None}


def test_mark_schema_fetch_pending_never_touches_a_previously_recorded_error():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.mark_schema_fetch_done(cache_key, error="FATAL")
    store.mark_schema_fetch_pending(cache_key)
    assert store.get_schema_fetch_status(cache_key) == {"pending": True, "error": "FATAL"}


def test_mark_schema_fetch_done_with_no_error_clears_pending_and_any_previous_error():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.mark_schema_fetch_pending(cache_key)
    store.mark_schema_fetch_done(cache_key, error="TIMEOUT")
    assert store.get_schema_fetch_status(cache_key)["error"] == "TIMEOUT"

    store.mark_schema_fetch_pending(cache_key)
    store.mark_schema_fetch_done(cache_key)  # this attempt succeeded
    assert store.get_schema_fetch_status(cache_key) == {"pending": False, "error": None}


def test_mark_schema_fetch_pending_and_done_never_touch_schema_text_or_overview():
    # merge=True semantics, same guarantee set_cached_schema/set_cached_
    # schema_overview already give each other (see this file's own tests
    # above) - a fetch-status write must never clobber the actual schema
    # content living in the same document.
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.set_cached_schema(cache_key, "Table: t", "2026-01-01T00:00:00+00:00")
    store.set_cached_schema_overview(cache_key, {"prose": "desc", "questions": []})

    store.mark_schema_fetch_pending(cache_key)
    store.mark_schema_fetch_done(cache_key, error="EMPTY")

    row = store.get_cached_schema(cache_key)
    assert row["schema_text"] == "Table: t"
    assert row["overview"] == {"prose": "desc", "questions": []}


def test_delete_cached_schema_also_clears_fetch_status():
    store, client = make_store()
    cache_key = "alice@host:5432/db"
    store.mark_schema_fetch_pending(cache_key)
    store.mark_schema_fetch_done(cache_key, error="FATAL")

    store.delete_cached_schema(cache_key)

    assert store.get_schema_fetch_status(cache_key) == {"pending": False, "error": None}


def test_fetch_status_is_isolated_per_cache_key():
    store, client = make_store()
    store.mark_schema_fetch_pending("alice@host:5432/db_one")
    store.mark_schema_fetch_done("bob@host:5432/db_two", error="FATAL")
    assert store.get_schema_fetch_status("alice@host:5432/db_one") == {"pending": True, "error": None}
    assert store.get_schema_fetch_status("bob@host:5432/db_two") == {"pending": False, "error": "FATAL"}


# --- list_cached_schema_texts() (local-dev /api/debug/schema-cache) --------

def test_list_cached_schema_texts_is_empty_when_nothing_is_cached():
    store, client = make_store()
    assert store.list_cached_schema_texts() == {}


def test_list_cached_schema_texts_returns_every_cached_entry():
    store, client = make_store()
    store.set_cached_schema("alice@host:5432/db_one", "SCHEMA ONE", "2026-01-01T00:00:00+00:00")
    store.set_cached_schema("bob@host:5432/db_two", "SCHEMA TWO", "2026-01-02T00:00:00+00:00")
    assert store.list_cached_schema_texts() == {
        "alice@host:5432/db_one": "SCHEMA ONE",
        "bob@host:5432/db_two": "SCHEMA TWO",
    }


def test_list_cached_schema_texts_omits_a_key_with_no_schema_text_yet():
    store, client = make_store()
    store.mark_schema_fetch_pending("pending@host:5432/db")
    store.set_cached_schema_overview("overview-only@host:5432/db", {"prose": "x", "questions": []})
    store.set_cached_schema("alice@host:5432/db", "SCHEMA ONE", "2026-01-01T00:00:00+00:00")
    assert store.list_cached_schema_texts() == {"alice@host:5432/db": "SCHEMA ONE"}
