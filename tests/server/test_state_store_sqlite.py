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

import state_store
from state_store import SqliteStateStore, compute_connection_key


def make_store(tmp_path):
    store = SqliteStateStore(str(tmp_path / "state.db"))
    store.init()
    return store


# --- sessions ------------------------------------------------------------------
# A session stores only an identity reference (is_custom, connection_id) -
# never a connection's actual details/credentials (see state_store.py's
# module/class docstrings and db.py's resolve_active_descriptor, which
# resolves those fresh every time something needs to actually connect).

def test_get_session_defaults_when_no_row_exists(tmp_path):
    store = make_store(tmp_path)
    session = store.get_session("alice")
    assert session["auto_sql_execute"] is True
    assert session["is_custom"] is False
    assert session["connection_id"] == ""


def test_set_and_get_session_round_trips_all_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session(
        "alice", connection_id="abc123", auto_sql_execute=False, is_custom=True,
    )
    session = store.get_session("alice")
    assert session["connection_id"] == "abc123"
    assert session["auto_sql_execute"] is False
    assert session["is_custom"] is True


def test_set_session_only_updates_passed_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="preset+Default DB", auto_sql_execute=True)
    store.set_session("alice", auto_sql_execute=False)  # only toggling this
    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"  # untouched
    assert session["auto_sql_execute"] is False


def test_set_session_with_all_none_is_a_no_op(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="preset+Default DB")
    store.set_session("alice")  # nothing passed
    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"


def test_set_session_can_explicitly_clear_connection_id(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="abc123", is_custom=True)
    store.set_session("alice", connection_id="", is_custom=False)
    session = store.get_session("alice")
    assert session["connection_id"] == ""
    assert session["is_custom"] is False


def test_sessions_are_isolated_per_user(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="alice-conn")
    store.set_session("bob", connection_id="bob-conn")
    assert store.get_session("alice")["connection_id"] == "alice-conn"
    assert store.get_session("bob")["connection_id"] == "bob-conn"


# --- llm_provider / llm_model (model-selection UI) ------------------------------
# Same "" -> "nothing explicitly selected yet, resolve the env-configured
# default elsewhere" convention connection_id already uses - see
# get_session's docstring.

def test_get_session_defaults_llm_fields_to_blank(tmp_path):
    store = make_store(tmp_path)
    session = store.get_session("alice")
    assert session["llm_provider"] == ""
    assert session["llm_model"] == ""


def test_set_and_get_session_round_trips_llm_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", llm_provider="anthropic", llm_model="claude-sonnet-5")
    session = store.get_session("alice")
    assert session["llm_provider"] == "anthropic"
    assert session["llm_model"] == "claude-sonnet-5"
    # Untouched by an llm-only save - same independence auto_sql_execute/
    # connection_id already have from each other.
    assert session["connection_id"] == ""


def test_set_session_llm_fields_do_not_clobber_connection_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="preset+Default DB", auto_sql_execute=False)
    store.set_session("alice", llm_provider="openai", llm_model="gpt-5.6-luna")
    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"
    assert session["auto_sql_execute"] is False
    assert session["llm_provider"] == "openai"
    assert session["llm_model"] == "gpt-5.6-luna"


def test_none_user_id_bucketed_under_global(tmp_path):
    store = make_store(tmp_path)
    store.set_session(None, connection_id="global-conn")
    assert store.get_session(None)["connection_id"] == "global-conn"
    assert store.get_session("global")["connection_id"] == "global-conn"


# --- theme (Preferences modal) --------------------------------------------------
# Same "" -> "nothing explicitly saved yet, let the client's own current/
# localStorage value keep applying" convention llm_provider/llm_model already
# use - see get_session's docstring - not auto_sql_execute's baked-in-default
# one.

def test_get_session_defaults_theme_to_blank(tmp_path):
    store = make_store(tmp_path)
    session = store.get_session("alice")
    assert session["theme"] == ""


def test_set_and_get_session_round_trips_theme(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", theme="light")
    session = store.get_session("alice")
    assert session["theme"] == "light"
    # Untouched by a theme-only save - same independence auto_sql_execute/
    # connection_id already have from each other.
    assert session["connection_id"] == ""


def test_set_session_theme_does_not_clobber_other_fields(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", connection_id="preset+Default DB", auto_sql_execute=False)
    store.set_session("alice", theme="light")
    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"
    assert session["auto_sql_execute"] is False
    assert session["theme"] == "light"


def test_set_session_other_fields_do_not_clobber_theme(tmp_path):
    store = make_store(tmp_path)
    store.set_session("alice", theme="light")
    store.set_session("alice", auto_sql_execute=False)
    assert store.get_session("alice")["theme"] == "light"


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


def test_single_connection_with_no_real_url_stores_and_returns_none_not_blank(tmp_path):
    # Mirrors how config_routes.py now saves a custom BigQuery/Snowflake/
    # Databricks/Oracle/Redshift/MSSQL/Sheets connection - those 7 dialects
    # have no real url of their own, so db_url is None, not "" (see that
    # module's docstring). database_url's column is nullable specifically
    # so this doesn't have to fake an empty string instead.
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", "BQ Conn", "bigquery", None,
        db_config={"project_id": "p", "dataset": "d"},
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["url"] is None


def test_custom_databases_list_form_with_no_real_url_stores_and_returns_none_not_blank(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections(
        "alice", None, None, None,
        custom_databases=[
            {"name": "BQ Conn", "type": "bigquery", "url": None,
             "config": {"project_id": "p", "dataset": "d"}},
        ],
    )
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["url"] is None


def test_legacy_not_null_database_url_column_is_migrated_to_nullable(tmp_path):
    # Simulates a database file created before database_url stopped being
    # NOT NULL (i.e. before BigQuery/Snowflake/etc. custom connections
    # stopped needing a fake "" placeholder there - see the migration's own
    # comment in state_store.py's init()). A fresh install never hits this
    # path (CREATE TABLE IF NOT EXISTS already creates the column nullable
    # today) - only an existing, pre-upgrade database file does, so this
    # test builds that legacy schema by hand rather than through the store.
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE db_connections (
            user_id TEXT,
            connection_key TEXT NOT NULL DEFAULT '',
            database_name TEXT NOT NULL,
            database_url TEXT NOT NULL,
            database_type TEXT NOT NULL DEFAULT 'postgres',
            database_config TEXT,
            PRIMARY KEY (user_id, connection_key)
        );
    """)
    conn.execute(
        "INSERT INTO db_connections (user_id, connection_key, database_name, database_url, database_type) "
        "VALUES ('alice', 'k1', 'My PG', 'postgresql://u:p@h/db', 'postgres')"
    )
    # A pre-existing row from before this change - the old "" placeholder
    # a structured-dialect connection used to be saved with. Migrating the
    # column to nullable doesn't retroactively touch this value; only
    # re-saving it through set_db_connections would.
    conn.execute(
        "INSERT INTO db_connections (user_id, connection_key, database_name, database_url, database_type) "
        "VALUES ('alice', 'k2', 'My BQ', '', 'bigquery')"
    )
    conn.commit()
    conn.close()

    store = SqliteStateStore(str(db_path))
    store.init()  # must not raise, and must actually relax the constraint

    conn = sqlite3.connect(str(db_path))
    cols = conn.execute("PRAGMA table_info(db_connections);").fetchall()
    conn.close()
    notnull = next(c[3] for c in cols if c[1] == "database_url")
    assert notnull == 0

    conns = store.get_db_connections("alice")
    urls_by_name = {c["name"]: c["url"] for c in conns}
    assert urls_by_name["My PG"] == "postgresql://u:p@h/db"
    assert urls_by_name["My BQ"] == ""  # preserved as-is, not retroactively changed

    # And a fresh save through the store now goes through None, not "".
    store.set_db_connections(
        "alice", "My BQ", "bigquery", None, db_config={"project_id": "p", "dataset": "d"},
        connection_key="k2",
    )
    refreshed = {c["name"]: c["url"] for c in store.get_db_connections("alice")}
    assert refreshed["My BQ"] is None


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


# --- translation history list cap (TRANSLATION_HISTORY_LIST_LIMIT) -------------
# record_translation()'s created_at defaults to CURRENT_TIMESTAMP (second
# granularity), so a tight loop of record_translation() calls can't be
# trusted to produce distinct, orderable timestamps within a single test.
# These tests instead insert rows directly with explicit, controlled
# created_at values - same "build the row by hand" approach the legacy-
# schema migration tests above already use for a different reason.

def _insert_translation_row(db_path, user_id, nl_prompt, created_at):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO translations "
        "(user_id, database_type, database_name, nl_prompt, sql_command, model, "
        " duration, input_tokens, output_tokens, total_tokens, thinking_tokens, "
        " cached_content_tokens, created_at) "
        "VALUES (?, 'postgres', 'DB', ?, 'SELECT 1;', 'm', 1, 1, 1, 2, 0, 0, ?)",
        (user_id, nl_prompt, created_at),
    )
    conn.commit()
    conn.close()


def test_get_translation_history_defaults_to_50_row_limit(tmp_path):
    store = make_store(tmp_path)
    db_path = tmp_path / "state.db"
    for i in range(60):
        _insert_translation_row(db_path, "alice", f"p{i}", f"2024-01-01 00:{i:02d}:00")
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 60  # uncapped
    assert len(rows) == 50  # capped
    assert sum(s["total_translations"] for s in stats) == 60  # stats: complete history


def test_get_translation_history_list_is_sorted_newest_first(tmp_path):
    store = make_store(tmp_path)
    db_path = tmp_path / "state.db"
    _insert_translation_row(db_path, "alice", "oldest", "2024-01-01 00:00:00")
    _insert_translation_row(db_path, "alice", "middle", "2024-01-02 00:00:00")
    _insert_translation_row(db_path, "alice", "newest", "2024-01-03 00:00:00")
    rows, _, _ = store.get_translation_history("alice")
    assert [r["nl_prompt"] for r in rows] == ["newest", "middle", "oldest"]


def test_get_translation_history_limit_is_configurable_via_env_var(tmp_path, monkeypatch):
    # Patches the SAME module object SqliteStateStore's globals resolve
    # TRANSLATION_HISTORY_LIST_LIMIT from - this file's own top-level
    # `import state_store` (not one done here, function-local) - since
    # other test files' app_factory/fresh_import calls swap sys.modules
    # entries for "state_store" during the test-execution phase, well
    # after this file's collection-time imports already bound both names
    # to the one original module. A function-local `import state_store`
    # here would risk fetching whatever the CURRENT sys.modules entry is
    # by the time this test runs (possibly a different, later-reloaded
    # module object than the one SqliteStateStore itself was defined in),
    # silently patching a module the code under test never reads from.
    monkeypatch.setattr(state_store, "TRANSLATION_HISTORY_LIST_LIMIT", 3)
    store = make_store(tmp_path)
    db_path = tmp_path / "state.db"
    for i in range(10):
        _insert_translation_row(db_path, "alice", f"p{i}", f"2024-01-01 00:{i:02d}:00")
    rows, stats, total_count = store.get_translation_history("alice")
    assert total_count == 10  # uncapped
    assert len(rows) == 3  # capped to the overridden limit
    assert [r["nl_prompt"] for r in rows] == ["p9", "p8", "p7"]  # still newest-first
    assert sum(s["total_translations"] for s in stats) == 10  # stats: complete history


# --- init() migrations: legacy schema upgrade paths ----------------------------

def test_init_migrates_sessions_table_predating_llm_fields(tmp_path):
    # A DB already upgraded past the connection_id rebuild (see the
    # pre_connection_id tests below) but predating llm_provider/llm_model -
    # a plain ALTER ADD COLUMN, no data backfill needed (unlike
    # connection_id's own migration), since a blank value already means
    # "fall back to the env-configured default" everywhere this is read.
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            auto_sql_execute INTEGER NOT NULL DEFAULT 1,
            is_custom INTEGER NOT NULL DEFAULT 0,
            connection_id TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "INSERT INTO sessions (session_id, auto_sql_execute, is_custom, connection_id) "
        "VALUES (?, ?, ?, ?)",
        ("alice", 1, 0, "preset+Default DB"),
    )
    conn.commit()
    conn.close()

    store = SqliteStateStore(db_path)
    store.init()  # must not raise

    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"  # untouched
    assert session["llm_provider"] == ""
    assert session["llm_model"] == ""

    store.set_session("alice", llm_provider="anthropic", llm_model="claude-sonnet-5")
    assert store.get_session("alice")["llm_provider"] == "anthropic"


def test_init_migrates_sessions_table_predating_theme(tmp_path):
    # A DB already upgraded past every other migration but predating the
    # theme column - same plain-ALTER, no-backfill-needed shape as the
    # llm_provider/llm_model migration above (a blank value already means
    # "leave whatever theme the client already has applied" everywhere
    # this is read).
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            auto_sql_execute INTEGER NOT NULL DEFAULT 1,
            is_custom INTEGER NOT NULL DEFAULT 0,
            connection_id TEXT NOT NULL DEFAULT '',
            llm_provider TEXT NOT NULL DEFAULT '',
            llm_model TEXT NOT NULL DEFAULT '',
            in_scope_preset_ids TEXT,
            in_scope_custom_connection_keys TEXT,
            in_scope_mode TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "INSERT INTO sessions (session_id, auto_sql_execute, is_custom, connection_id) "
        "VALUES (?, ?, ?, ?)",
        ("alice", 1, 0, "preset+Default DB"),
    )
    conn.commit()
    conn.close()

    store = SqliteStateStore(db_path)
    store.init()  # must not raise

    session = store.get_session("alice")
    assert session["connection_id"] == "preset+Default DB"  # untouched
    assert session["theme"] == ""

    store.set_session("alice", theme="light")
    assert store.get_session("alice")["theme"] == "light"


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

    store = SqliteStateStore(db_path)
    store.init()  # must not raise, and must preserve the legacy row

    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    assert conns[0]["name"] == "Legacy Conn"
    assert conns[0]["connection_key"]  # backfilled, non-empty


def test_init_migrates_pre_connection_id_sessions_table_for_custom_row(tmp_path):
    db_path = str(tmp_path / "state.db")
    # Simulate a pre-migration DB: sessions storing a full duplicated
    # descriptor (database_url/database_type/database_config/
    # custom_connection_key) rather than just an identity reference - see
    # state_store.py's init() migration comment for why this got scrubbed.
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            database_url TEXT,
            database_type TEXT,
            database_config TEXT,
            auto_sql_execute INTEGER NOT NULL DEFAULT 1,
            is_custom INTEGER NOT NULL DEFAULT 0,
            custom_connection_key TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "INSERT INTO sessions (session_id, database_url, database_type, database_config, "
        "auto_sql_execute, is_custom, custom_connection_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("alice", "bigquery://p/d", "bigquery", '{"project_id": "p", "dataset": "d"}', 0, 1, "custom-key-123"),
    )
    conn.commit()
    conn.close()

    store = SqliteStateStore(db_path)
    store.init()  # must not raise, and must preserve/scrub the legacy row

    session = store.get_session("alice")
    assert session["is_custom"] is True
    assert session["connection_id"] == "custom-key-123"  # reused as-is
    assert session["auto_sql_execute"] is False
    # llm_provider/llm_model survive this rebuild too (added via the
    # earlier ALTER guards, before this table gets renamed/rebuilt) -
    # blank here since this legacy row predates them entirely.
    assert session["llm_provider"] == ""
    assert session["llm_model"] == ""

    # The old descriptor columns must actually be gone (a hard rebuild, not
    # just an added column) - no lingering credentials in an unread column.
    with store._connect() as check_conn:
        cursor = check_conn.cursor()
        cursor.execute("PRAGMA table_info(sessions);")
        cols = {c[1] for c in cursor.fetchall()}
    assert cols == {
        "session_id", "auto_sql_execute", "is_custom", "connection_id",
        "llm_provider", "llm_model",
        "in_scope_preset_ids", "in_scope_custom_connection_keys", "in_scope_mode",
        "theme",
        "updated_at",
    }


def test_init_migrates_pre_connection_id_sessions_table_for_preset_row(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            database_url TEXT,
            database_type TEXT,
            database_config TEXT,
            auto_sql_execute INTEGER NOT NULL DEFAULT 1,
            is_custom INTEGER NOT NULL DEFAULT 0,
            custom_connection_key TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "INSERT INTO sessions (session_id, database_url, database_type, database_config, "
        "auto_sql_execute, is_custom, custom_connection_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bob", "postgresql://preset-match/db", "postgres", None, 1, 0, ""),
    )
    conn.execute(
        "INSERT INTO sessions (session_id, database_url, database_type, database_config, "
        "auto_sql_execute, is_custom, custom_connection_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("carol", "postgresql://no-longer-configured/db", "postgres", None, 1, 0, ""),
    )
    conn.commit()
    conn.close()

    # The migration's "from app_config import CONFIGURED_DBS" is a local
    # import (see state_store.py's init() comment on why - app_config.py
    # itself imports this module while still building CONFIGURED_DBS, so a
    # top-level import here would be circular). Rather than importing the
    # real app_config module (which this file's own docstring says to
    # avoid - these tests exercise SqliteStateStore with no Flask/app_config
    # involvement), inject a minimal fake module under that name so the
    # local import resolves to it instead.
    import types
    fake_app_config = types.ModuleType("app_config")
    fake_app_config.CONFIGURED_DBS = [
        {"id": "postgres+Preset Match", "name": "Preset Match", "type": "postgres",
         "url": "postgresql://preset-match/db"},
    ]
    monkeypatch.setitem(sys.modules, "app_config", fake_app_config)

    store = SqliteStateStore(db_path)
    store.init()

    # bob's old database_url matches a configured preset by url -> backfilled
    # to that preset's stable id.
    bob_session = store.get_session("bob")
    assert bob_session["is_custom"] is False
    assert bob_session["connection_id"] == "postgres+Preset Match"

    # carol's old database_url doesn't match anything currently configured
    # (the preset it once pointed at is gone) -> blank connection_id, which
    # resolves to the app default going forward (see db.py's
    # resolve_active_descriptor) rather than raising or guessing.
    carol_session = store.get_session("carol")
    assert carol_session["is_custom"] is False
    assert carol_session["connection_id"] == ""


def test_init_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.set_db_connections("alice", "Conn", "postgres", "postgresql://a/b")
    store.set_session("alice", connection_id="some-id", is_custom=True)
    store.init()  # calling init() again must not lose data or error
    conns = store.get_db_connections("alice")
    assert len(conns) == 1
    session = store.get_session("alice")
    assert session["connection_id"] == "some-id"
    assert session["is_custom"] is True
