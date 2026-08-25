"""
Encryption at rest for database_config (state_store.py's
DB_CONFIG_ENCRYPTION_KEY / _encrypt_config_to_text / _config_value_to_store
/ _loads_config / _decrypt_firestore_config - see that module's
"Encryption at rest for database_config" comment for the full design).

The whole database_config dict is encrypted as one opaque blob, not just
individual "credential-shaped" fields (contrast _CREDENTIAL_CONFIG_FIELDS,
a different allowlist used for API-response redaction) - so these tests
deliberately assert on the raw stored value having no plaintext trace of
the secret anywhere in it, not just that a specific field was handled.

SqliteStateStore/FirestoreStateStore are exercised directly here (same
style as test_state_store_sqlite.py/test_state_store_firestore.py), not
through app_factory/fresh_import - this lets each test just
monkeypatch.setenv/delenv DB_CONFIG_ENCRYPTION_KEY directly, since
_load_cipher() re-reads the environment on every call rather than caching
a cipher once at import time (see its docstring for why - this is exactly
what makes that convenient here). The one exception is the Cloud Run
startup-guard tests at the bottom, which need app_config.py's own
import-time logic and so go through app_factory as usual.
"""

import json
import sqlite3
import sys

import pytest

from helpers import SERVER_DIR

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from state_store import SqliteStateStore, FirestoreStateStore, is_db_config_encryption_configured
from helpers import FakeFirestoreClient, make_fernet_key, FAKE_DB_CONFIG_ENCRYPTION_KEY

SECRET_PASSWORD = "hunter2_super_secret"


def make_sqlite_store(tmp_path):
    store = SqliteStateStore(str(tmp_path / "state.db"))
    store.init()
    return store


def raw_sqlite_config(tmp_path, user_id):
    """Reads database_config straight off disk, bypassing
    SqliteStateStore/_loads_config entirely - the whole point of these
    tests is to inspect what's ACTUALLY sitting in the file. Assumes a
    single saved connection for that user (true for every test below that
    calls this), so no need to also compute/match its connection_key."""
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    row = conn.execute(
        "SELECT database_config FROM db_connections WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def make_firestore_store():
    client = FakeFirestoreClient()
    store = FirestoreStateStore(client)
    return store, client


# --- SQLite: no key configured (today's behavior, unchanged) ----------------

def test_sqlite_with_no_key_stores_plain_json_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_CONFIG_ENCRYPTION_KEY", raising=False)
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    raw = raw_sqlite_config(tmp_path, "alice")
    # Byte-for-byte still plain JSON - no behavior change for anyone who
    # hasn't opted into DB_CONFIG_ENCRYPTION_KEY.
    assert json.loads(raw)["password"] == SECRET_PASSWORD


# --- SQLite: valid key configured --------------------------------------------

def test_sqlite_with_valid_key_stores_ciphertext_not_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    raw = raw_sqlite_config(tmp_path, "alice")

    assert SECRET_PASSWORD not in raw
    with pytest.raises(Exception):
        json.loads(raw)  # genuinely ciphertext, not just reformatted JSON


def test_sqlite_with_valid_key_round_trips_correctly(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


def test_sqlite_custom_databases_list_form_also_encrypts_each_row(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store = make_sqlite_store(tmp_path)
    store.set_db_connections("alice", None, None, None, custom_databases=[
        {"type": "postgres", "name": "PG Conn", "url": "postgresql://a/b",
         "config": {"password": SECRET_PASSWORD}},
    ])
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    raw = conn.execute(
        "SELECT database_config FROM db_connections WHERE user_id = ?", ("alice",),
    ).fetchone()[0]
    conn.close()
    assert SECRET_PASSWORD not in raw

    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


# --- SQLite: backward compatibility / graceful degradation -------------------

def test_sqlite_legacy_plaintext_row_still_reads_after_a_key_is_configured(tmp_path, monkeypatch):
    # Simulates turning encryption on for the first time: this row was
    # written before DB_CONFIG_ENCRYPTION_KEY was ever set.
    monkeypatch.delenv("DB_CONFIG_ENCRYPTION_KEY", raising=False)
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )

    # Now a key gets configured (e.g. this deploy just turned the feature
    # on) - no migration step, the legacy row must still read correctly.
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


def test_sqlite_row_saved_under_one_key_is_unreadable_under_a_different_key(tmp_path, monkeypatch):
    # Not a crash - degrades to an empty config, same "never raise, just
    # lose the data it can't make sense of" contract _loads_config always
    # had for corrupt/foreign data.
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )

    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())  # a DIFFERENT key
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"] == {}


def test_sqlite_invalid_key_falls_back_to_plaintext_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", "not-a-real-fernet-key")
    store = make_sqlite_store(tmp_path)
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


# --- Firestore: no key configured (today's behavior, unchanged) -------------

def test_firestore_with_no_key_stores_native_map_as_before(monkeypatch):
    monkeypatch.delenv("DB_CONFIG_ENCRYPTION_KEY", raising=False)
    store, client = make_firestore_store()
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    docs = client.collection("db_connections").where("user_id", "==", "alice").stream()
    raw = next(iter(docs)).to_dict()
    assert isinstance(raw["database_config"], dict)
    assert raw["database_config"]["password"] == SECRET_PASSWORD


# --- Firestore: valid key configured -----------------------------------------

def test_firestore_with_valid_key_stores_string_ciphertext_not_a_map(monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store, client = make_firestore_store()
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    docs = client.collection("db_connections").where("user_id", "==", "alice").stream()
    raw = next(iter(docs)).to_dict()
    assert isinstance(raw["database_config"], str)
    assert SECRET_PASSWORD not in raw["database_config"]


def test_firestore_with_valid_key_round_trips_correctly(monkeypatch):
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    store, client = make_firestore_store()
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


def test_firestore_legacy_native_map_doc_still_reads_after_a_key_is_configured(monkeypatch):
    monkeypatch.delenv("DB_CONFIG_ENCRYPTION_KEY", raising=False)
    store, client = make_firestore_store()
    store.set_db_connections(
        "alice", "PG Conn", "postgres", "postgresql://a/b",
        db_config={"password": SECRET_PASSWORD},
    )

    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    conns = store.get_db_connections("alice", include_credentials=True)
    assert conns[0]["config"]["password"] == SECRET_PASSWORD


# --- Cloud Run startup guard (app_config.py) --------------------------------
# These need a real app_config.py import, unlike everything above - see
# module docstring.

def test_cloud_run_without_a_key_refuses_to_start(app_factory, tmp_path):
    from helpers import write_database_presets_file
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Demo", "url": "postgresql://u:p@h/db"},
    ])
    with pytest.raises(RuntimeError, match="DB_CONFIG_ENCRYPTION_KEY"):
        app_factory(env={
            "K_SERVICE": "ydyl-service",
            "GCP_PROJECT_ID": "fake-project",
            "DATABASE_PRESETS_FILE": path,
        }, mock_firestore=True)


def test_cloud_run_with_an_invalid_key_refuses_to_start(app_factory, tmp_path):
    from helpers import write_database_presets_file
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Demo", "url": "postgresql://u:p@h/db"},
    ])
    with pytest.raises(RuntimeError, match="DB_CONFIG_ENCRYPTION_KEY"):
        app_factory(env={
            "K_SERVICE": "ydyl-service",
            "GCP_PROJECT_ID": "fake-project",
            "DATABASE_PRESETS_FILE": path,
            "DB_CONFIG_ENCRYPTION_KEY": "not-a-real-fernet-key",
        }, mock_firestore=True)


def test_cloud_run_with_a_valid_key_starts_up_successfully(app_factory, tmp_path):
    from helpers import write_database_presets_file
    path = write_database_presets_file(tmp_path, [
        {"type": "postgres", "name": "Demo", "url": "postgresql://u:p@h/db"},
    ])
    env = app_factory(env={
        "K_SERVICE": "ydyl-service",
        "GCP_PROJECT_ID": "fake-project",
        "DATABASE_PRESETS_FILE": path,
        "DB_CONFIG_ENCRYPTION_KEY": FAKE_DB_CONFIG_ENCRYPTION_KEY,
    }, mock_firestore=True)
    assert env.app_config.IS_CLOUD_RUN is True


def test_locally_no_key_is_fine_not_an_error(app_factory):
    # The whole point of the local/Cloud-Run split: zero-config dev must
    # keep working exactly as it did before this feature existed.
    env = app_factory()
    assert env.app_config.IS_CLOUD_RUN is False


def test_is_db_config_encryption_configured_reflects_current_env(monkeypatch):
    monkeypatch.delenv("DB_CONFIG_ENCRYPTION_KEY", raising=False)
    assert is_db_config_encryption_configured() is False
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", make_fernet_key())
    assert is_db_config_encryption_configured() is True
    monkeypatch.setenv("DB_CONFIG_ENCRYPTION_KEY", "garbage")
    assert is_db_config_encryption_configured() is False
