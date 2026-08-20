"""
SqliteStateStore, exercised directly (no Flask/app_config involvement) -
just point it at a fresh file under tmp_path per test.
"""

import os
import sqlite3
import sys

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from state_store import SqliteStateStore, compute_connection_key


def make_store(tmp_path):
    store = SqliteStateStore(str(tmp_path / "state.db"), default_conn="postgresql://default/db")
    store.init()
    return store


# --- sessions ------------------------------------------------------------------

def test_get_session_defaults_when_no_row_exists(tmp_path):
    store = make_store(tmp_path)
    session = store.get_session("alice")
    assert session["database_url"] == "postgresql://default/db"
    assert session["auto_sql_execute"] is True
    assert session["database_type"] == "postgres"
    assert session["database_config"] == {}
    assert session["is_custom"] is False
    assert session["custom_connection_key"] == ""


def test_set_and_get_session_round_trips_all_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session(
        "alice", db_url="bigquery://p/d", auto_sql_execute=False, db_type="bigquery",
        db_config={"project_id": "p", "dataset": "d"}, is_custom=True,
        custom_connection_key="abc123",
    )
    session = store.get_session("alice")
    assert session["database_url"] == "bigquery://p/d"
    assert session["auto_sql_execute"] is False
    assert session["database_type"] == "bigquery"
    assert session["database_config"] == {"project_id": "p", "dataset": "d"}
    assert session["is_custom"] is True
    assert session["custom_connection_key"] == "abc123"


def test_set_session_only_updates_passed_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", db_url="postgresql://a/b", auto_sql_execute=True)
    store.set_session("alice", auto_sql_execute=False)  # only toggling this
    session = store.get_session("alice")
    assert session["database_url"] == "postgresql://a/b"  # untouched
    assert session["auto_sql_execute"] is False


def test_set_session_with_all_none_is_a_no_op(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", db_url="postgresql://a/b")
    store.set_session("alice")  # nothing passed
    session = store.get_session("alice")
    assert session["database_url"] == "postgresql://a/b"


def test_set_session_can_explicitly_clear_custom_connection_key(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", db_url="bigquery://p/d", is_custom=True, custom_connection_key="abc123")
    store.set_session("alice", db_url="postgresql://a/b", is_custom=False, custom_connection_key="")
    session = store.get_session("alice")
    assert session["custom_connection_key"] == ""
    assert session["is_custom"] is False


def test_sessions_are_isolated_per_user(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", db_url="postgresql://alice-db")
    store.set_session("bob", db_url="postgresql://bob-db")
    assert store.get_session("alice")["database_url"] == "postgresql://alice-db"
    assert store.get_session("bob")["database_url"] == "postgresql://bob-db"


def test_none_user_id_bucketed_under_global(tmp_path):
    store = make_store(tmp_path)
    store.set_session(None, db_url="postgresql://global-db")
    assert store.get_session(None)["database_url"] == "postgresql://global-db"
    assert store.get_session("global")["database_url"] == "postgresql://global-db"


# --- db_connections (saved custom connections) --------------------------------

def test_get_db_connections_empty_for_new_user(tmp_path):
    store = make_store(tmp_path)
    assert store.get_db_connections("alice") == []


def test_set_and_get_single_db_connection(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections("alice", "My DB", "postgres", "postgresql://a/b", db_config={})
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["name"] == "My DB"
    assert conns[0]["type"] == "postgres"
    assert conns[0]["url"] == "postgresql://a/b"


def test_has_custom_credentials_true_when_credentials_json_present(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", "BQ Conn", "bigquery", "bigquery://p/d",
        db_config={"project_id": "p", "dataset": "d", "credentials_json": "{...}", "billing_project_id": "p"},
    )
    conns = store.get_db_connections("alice")
    assert conns[0]["has_custom_credentials"] is True
    # And the credential itself must never leak by default.
    assert "credentials_json" not in conns[0]["config"]


def test_has_custom_credentials_false_when_no_credentials_json(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections("alice", "PG Conn", "postgres", "postgresql://a/b", db_config={})
    conns = store.get_db_connections("alice")
    assert conns[0]["has_custom_credentials"] is False


def test_include_credentials_true_returns_the_actual_key(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", "BQ Conn", "bigquery", "bigquery://p/d",
        db_config={"credentials_json": "SECRET_KEY_BLOB"},
    )
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["credentials_json"] == "SECRET_KEY_BLOB"


def test_replace_custom_databases_list_via_custom_databases_param(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections("alice", "Old Conn", "postgres", "postgresql://old/db")
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[
            {"name": "Conn A", "type": "postgres", "url": "postgresql://a/db", "config": {}},
            {"name": "Conn B", "type": "postgres", "url": "postgresql://b/db", "config": {}},
        ],
    )
    conns = store.get_db_connections("alice")
    names = {c["name"] for c in conns}
    assert names == {"Conn A", "Conn B"}
    assert "Old Conn" not in names


def test_two_connections_sharing_url_but_different_credentials_dont_collide(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[
            {"name": "Conn A", "type": "bigquery", "url": "bigquery://shared/ds",
             "config": {"credentials_json": "KEY_A"}},
            {"name": "Conn B", "type": "bigquery", "url": "bigquery://shared/ds",
             "config": {"credentials_json": "KEY_B"}},
        ],
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 2
    keys = {c["connection_key"] for c in conns}
    assert len(keys) == 2  # distinct connection_key per row, not overwritten


def test_has_custom_credentials_true_for_snowflake_password(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", "SF Conn", "snowflake", "snowflake://acc/db/public",
        db_config={"account": "acc", "user": "u", "warehouse": "wh", "database": "db", "password": "hunter2"},
    )
    conns = store.get_db_connections("alice")
    assert conns[0]["has_custom_credentials"] is True
    assert "password" not in conns[0]["config"]


def test_has_custom_credentials_true_for_snowflake_private_key(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", "SF Conn", "snowflake", "snowflake://acc/db/public",
        db_config={"account": "acc", "user": "u", "warehouse": "wh", "database": "db", "private_key": "PEM"},
    )
    conns = store.get_db_connections("alice")
    assert conns[0]["has_custom_credentials"] is True
    assert "private_key" not in conns[0]["config"]


def test_two_snowflake_connections_sharing_url_but_different_passwords_dont_collide(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[
            {"name": "Conn A", "type": "snowflake", "url": "snowflake://shared/db/public",
             "config": {"password": "PASS_A"}},
            {"name": "Conn B", "type": "snowflake", "url": "snowflake://shared/db/public",
             "config": {"password": "PASS_B"}},
        ],
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 2
    keys = {c["connection_key"] for c in conns}
    assert len(keys) == 2


def test_switching_snowflake_auth_method_changes_connection_key(tmp_path):
    # Same name/url, password -> private_key: must be treated as a
    # genuinely different credential, not silently reuse the old key.
    from state_store import _credential_value_for_key

    key_no_creds = compute_connection_key(
        "SF Conn", "snowflake://acc/db/public", _credential_value_for_key({})
    )
    key_via_password = compute_connection_key(
        "SF Conn", "snowflake://acc/db/public", _credential_value_for_key({"password": "hunter2"})
    )
    key_via_private_key = compute_connection_key(
        "SF Conn", "snowflake://acc/db/public", _credential_value_for_key({"private_key": "PEM"})
    )
    assert len({key_no_creds, key_via_password, key_via_private_key}) == 3


def test_credential_value_for_key_captures_both_private_key_fields():
    from state_store import _credential_value_for_key
    base = _credential_value_for_key({"private_key": "PEM"})
    with_passphrase = _credential_value_for_key({"private_key": "PEM", "private_key_passphrase": "shh"})
    assert base != with_passphrase  # passphrase alone must still affect the hash


def test_credential_value_for_key_is_order_independent_of_dict_insertion(tmp_path):
    from state_store import _credential_value_for_key
    a = _credential_value_for_key({"private_key": "PEM", "private_key_passphrase": "shh"})
    b = _credential_value_for_key({"private_key_passphrase": "shh", "private_key": "PEM"})
    assert a == b


def test_compute_connection_key_differs_by_name_url_and_credentials():
    k1 = compute_connection_key("A", "url1", "cred1")
    k2 = compute_connection_key("B", "url1", "cred1")
    k3 = compute_connection_key("A", "url2", "cred1")
    k4 = compute_connection_key("A", "url1", "cred2")
    assert len({k1, k2, k3, k4}) == 4


def test_compute_connection_key_deterministic():
    assert compute_connection_key("A", "url1", "cred1") == compute_connection_key("A", "url1", "cred1")


# --- translations / history -----------------------------------------------------

def test_record_and_fetch_translation_history(tmp_path):
    store = make_store(tmp_path)
    store.record_translation(
        "alice", "postgres", "My DB", "show users", "SELECT * FROM users;",
        "gemini-2.5-flash", 120, 10, 5, 15, 0, 0,
    )
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 1
    assert rows[0]["nl_prompt"] == "show users"
    assert rows[0]["sql_command"] == "SELECT * FROM users;"
    assert len(stats) == 1
    assert stats[0]["total_translations"] == 1
    assert stats[0]["sum_total_tokens"] == 15


def test_translation_history_isolated_per_user(tmp_path):
    store = make_store(tmp_path)
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("bob", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    _, _, alice_count = store.get_translation_history("alice")
    _, _, bob_count = store.get_translation_history("bob")
    assert alice_count == 1
    assert bob_count == 1


def test_purge_translation_history_deletes_all_rows_for_user(tmp_path):
    store = make_store(tmp_path)
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("alice", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    store.purge_translation_history("alice")
    _, _, total_count = store.get_translation_history("alice")
    assert total_count == 0


def test_purge_translation_history_does_not_affect_other_users(tmp_path):
    store = make_store(tmp_path)
    store.record_translation("alice", "postgres", "DB", "p1", "SELECT 1;", "m", 1, 1, 1, 2, 0, 0)
    store.record_translation("bob", "postgres", "DB", "p2", "SELECT 2;", "m", 1, 1, 1, 2, 0, 0)
    store.purge_translation_history("alice")
    _, _, bob_count = store.get_translation_history("bob")
    assert bob_count == 1


# --- init() migrations: legacy schema upgrade paths ----------------------------

def test_init_migrates_pre_connection_key_db_connections_table(tmp_path):
    db_path = str(tmp_path / "state.db")
    # Simulate a pre-migration DB: db_connections keyed by (user_id,
    # database_url) only, no connection_key column at all.
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE db_connections (
            user_id TEXT,
            database_name TEXT NOT NULL,
            database_url TEXT NOT NULL,
            database_type TEXT NOT NULL DEFAULT 'postgres',
            database_config TEXT,
            PRIMARY KEY (user_id, database_url)
        );
    """)
    conn.execute(
        "INSERT INTO db_connections (user_id, database_name, database_url, database_type, database_config) "
        "VALUES (?, ?, ?, ?, ?)",
        ("alice", "Legacy Conn", "postgresql://legacy/db", "postgres", None),
    )
    conn.commit()
    conn.close()

    store = SqliteStateStore(db_path, default_conn="postgresql://default/db")
    store.init()  # must not raise, and must preserve the legacy row

    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["name"] == "Legacy Conn"
    assert conns[0]["connection_key"]  # backfilled, non-empty


def test_init_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections("alice", "Conn", "postgres", "postgresql://a/b")
    store.init()  # calling init() again must not lose data or error
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
