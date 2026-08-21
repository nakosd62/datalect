"""
state_store.py

Abstraction over the app's persistent state backend.

Previously, every piece of app state (sessions, saved DB connections,
translation history) had its own function with an `if firestore_client: ...
else: <sqlite> ...` branch baked in, repeated seven times across server.py.
That made each function roughly twice as long as necessary, and it meant a
change to one backend's behavior (e.g. error handling, or the "effective
user" fallback rule) had to be remembered and repeated at every call site.

This module pulls that branching out to a single decision, made once at
startup: which concrete StateStore to construct. Route handlers in
server.py then just call `state_store.<method>(...)` and don't need to know
or care whether they're talking to SQLite or Firestore.
"""

import hashlib
import json
import logging
import os
import sqlite3
from abc import ABC, abstractmethod

from google.cloud import firestore

# Reuses the same logger name/config server.py sets up (root logger stays
# quiet at WARNING; this "ydyl" logger is bumped to LOG_LEVEL/INFO there).
# If this module is ever imported standalone without server.py's config
# having run, it still works - it just falls back to logging defaults.
logger = logging.getLogger("ydyl")


def _effective_user(user_id):
    """Local/anonymous requests are bucketed under a single 'global' identity."""
    return user_id or "global"


# Default value for a session's "Automatic SQL Execution" preference before
# it's ever been explicitly set. Applies to brand-new sessions in both
# backends below (SQLite's schema default and Firestore's missing-field
# fallback).
DEFAULT_AUTO_SQL_EXECUTE = True

# Config fields that are credentials rather than plain identifiers - never
# returned by get_db_connections() unless include_credentials=True is
# passed explicitly (server-side use only, e.g. merging a previously-saved
# key back in when a user edits a connection without re-pasting it).
# "credentials_json" is BigQuery's service-account key; "password" and
# "private_key"/"private_key_passphrase" are Snowflake's two supported
# auth methods (see backends/snowflake.py's module docstring) - added here
# even before config_routes.py's Snowflake wiring lands, so there's no
# window where a Snowflake config field could round-trip to the frontend
# unstripped. "access_token" is Databricks' Personal Access Token (see
# backends/databricks.py's module docstring) - same reasoning. Oracle's
# standalone password (backends/oracle.py - Oracle has no connection-
# string url of its own to embed one in, unlike Postgres/MySQL) reuses
# "password", already covered here.
_CREDENTIAL_CONFIG_FIELDS = {"credentials_json", "password", "private_key", "private_key_passphrase", "access_token"}


def _loads_config(raw_json):
    """Best-effort JSON decode for a stored database_config value. Never
    raises - a corrupt/legacy value just degrades to an empty config
    rather than breaking session/connection loading entirely."""
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json) or {}
    except Exception:
        logger.warning("Failed to parse stored database_config JSON; ignoring it.")
        return {}


def _strip_credentials(config):
    """Returns a copy of `config` with credential fields removed, for
    responses that may end up in an API response to the frontend."""
    return {k: v for k, v in (config or {}).items() if k not in _CREDENTIAL_CONFIG_FIELDS}


def _has_any_credential(config):
    """Whether `config` carries ANY credential field - not just BigQuery's
    "credentials_json", since Snowflake's two auth methods use "password"
    or "private_key" instead (see _CREDENTIAL_CONFIG_FIELDS above). Used
    for the "has_custom_credentials" flag get_db_connections() returns, so
    the frontend can tell "a key/password is saved server-side" apart from
    "nothing saved yet" without ever seeing the credential itself."""
    config = config or {}
    return any(config.get(field) for field in _CREDENTIAL_CONFIG_FIELDS)


def _credential_value_for_key(config):
    """A single string folding in EVERY credential field `config` carries,
    for feeding into compute_connection_key()'s credentials_json parameter
    - that parameter is really just "fold this raw credential blob into
    the key hash", not something that literally has to be BigQuery's
    credentials_json. Concatenates every _CREDENTIAL_CONFIG_FIELDS value in
    a fixed (sorted) field-name order - never just the first non-empty one
    found, since iterating a set's natural order isn't guaranteed stable
    across process restarts (Python's string hash randomization), which
    would otherwise risk the same saved connection computing a *different*
    connection_key after an app restart. Sorted-and-joined is also more
    correct for Snowflake's key-pair auth specifically, where a config can
    carry two credential fields at once (private_key AND
    private_key_passphrase) - both must affect the hash, not just
    whichever happened to be checked first."""
    config = config or {}
    return "\x00".join(str(config.get(field) or "") for field in sorted(_CREDENTIAL_CONFIG_FIELDS))


def compute_connection_key(name, url, credentials_json=None):
    """Stable identity for one saved custom connection, independent of its
    position in the list or which storage backend holds it. "url" alone
    (the identity this replaced) can't tell two custom connections apart
    when it doesn't fully encode the credential - true for BigQuery, where
    "url" is just the synthetic bigquery://project/dataset identifier (see
    backends/bigquery.py) and two different service-account keys - or just
    two connections saved under different display names - can legitimately
    point at the exact same project/dataset. Folding name and
    credentials_json into the key means those get treated as genuinely
    different saved connections instead of one silently overwriting the
    other (the bug this exists to fix). Never leaks credentials_json
    itself - only its hash contributes here, so this value is safe to
    return to the frontend or log. config_routes.py is the single place
    that calls this (both when replacing a user's whole saved-connection
    list and when resolving which one is "active" for the session), so key
    derivation can't drift between call sites."""
    raw = f"{name or ''}\x00{url or ''}\x00{credentials_json or ''}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


class StateStore(ABC):
    """Backend-agnostic persistence for sessions, saved DB connections, and
    translation history/stats. Deliberately holds no notion of "the default
    connection" itself (there used to be a default_conn constructor param
    for exactly that) - a session only ever stores an identity reference
    (connection_id/is_custom, see get_session's docstring), never a
    connection's actual details, so there's nothing here that would need a
    fallback URL to seed a blank row with. db.py's resolve_active_descriptor
    is what applies DEFAULT_CONN (imported directly from app_config.py) when
    a session's connection_id is blank."""

    @abstractmethod
    def init(self):
        """One-time setup (schema creation, migrations). Safe to call on every startup."""

    @abstractmethod
    def get_session(self, user_id):
        """Returns {"auto_sql_execute", "is_custom", "connection_id"} for a
        user/session id - identity only, never a connection's actual
        details/credentials (see db.py's resolve_active_descriptor, which
        resolves those FRESH from CONFIGURED_DBS or get_db_connections()
        every time something needs to actually connect, rather than trusting
        anything cached here). "is_custom" (defaults to False for legacy
        rows) records whether the active connection is a saved custom
        connection rather than a preset. "connection_id" (defaults to "" -
        "nothing explicitly selected yet") is, depending on is_custom: a
        preset's stable CONFIGURED_DBS "id" (see app_config.py's
        DATABASE_PRESETS_FILE comment) when is_custom is False, or a saved
        custom connection's compute_connection_key() value when is_custom is
        True - either way, a single opaque reference resolved fresh at
        connect time, never a duplicated copy of the connection itself. This
        also means a removed preset or a deleted custom connection is
        immediately reflected everywhere (no drift) - see
        resolve_active_descriptor's "missing" return for how a
        connection_id that no longer resolves to anything real is handled."""

    @abstractmethod
    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None):
        """Persists the active connection reference (connection_id,
        is_custom) and/or auto_sql_execute flag for a user/session id. Only
        the fields passed (not None) are changed - the others are left
        as-is. Pass connection_id="" (not None) to explicitly clear it, e.g.
        when switching to a fresh/default connection - same
        not-None-means-"change this" convention is_custom already uses."""

    @abstractmethod
    def get_db_connections(self, user_id, include_credentials=False):
        """Returns a list of {"connection_key", "name", "type", "url",
        "config", "has_custom_credentials"} saved connections for a user.
        "connection_key" is that row's compute_connection_key() value - the
        actual identity used for storage/lookup now (see that function's
        docstring for why url alone stopped being sufficient); "" for any
        legacy row saved before this existed and not yet re-saved. By
        default, any credential fields (e.g. BigQuery's credentials_json)
        are stripped from "config" - this method's normal caller is
        config_routes.py building an API response, and credentials must
        never round-trip to the frontend. Pass include_credentials=True
        only for server-side use (e.g. merging in a previously-saved
        credential when a user edits a connection without re-pasting its
        key).

        "has_custom_credentials" is a plain boolean - never the credential
        itself - reporting whether a credential (currently just BigQuery's
        credentials_json) is saved for that connection, computed before any
        stripping so it's accurate regardless of include_credentials. It
        exists so the frontend can indicate "a custom key is already saved
        for this connection" without ever seeing the key - previously
        there was no way to distinguish "no key was ever saved" from "a key
        is saved but withheld", so the UI had no way to show a saved custom
        BigQuery connection was actually using its own service-account key
        rather than the app's ambient credentials."""

    @abstractmethod
    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None,
                            custom_databases=None, connection_key=None):
        """Saves a single connection, or replaces the whole saved list if
        custom_databases is provided (each item shaped like the dicts
        get_db_connections returns, i.e. {"connection_key", "name", "type",
        "url", "config"} - "connection_key" is optional per item; when
        absent it's derived with compute_connection_key(name, url,
        config.get("credentials_json"))). For the single-connection form,
        connection_key is likewise derived from (db_name, db_url,
        db_config's credentials_json) when not passed explicitly."""

    @abstractmethod
    def record_translation(self, user_id, db_type, db_name, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        """Logs one NL->SQL translation event, tagged with the resolved
        connection's dialect (db_type, e.g. "postgres"/"bigquery") and its
        human-readable name (db_name, e.g. "E-Commerce Store") - replaces
        the old single "connect_string" identifier, which stopped being
        meaningful once presets could span multiple dialects/names rather
        than always being a single parseable Postgres URL."""

    @abstractmethod
    def get_translation_history(self, user_id):
        """Returns (rows, daily_stats, total_count) for a user."""

    @abstractmethod
    def purge_translation_history(self, user_id):
        """Deletes all translation history for a user."""


# --------------------------------------------------------------------------
# SQLite backend (local dev)
# --------------------------------------------------------------------------

class SqliteStateStore(StateStore):
    def __init__(self, db_path):
        self.db_path = db_path

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def init(self):
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

            with self._connect() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS translations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        database_type TEXT,
                        database_name TEXT,
                        nl_prompt TEXT,
                        sql_command TEXT,
                        model TEXT,
                        duration INTEGER,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        total_tokens INTEGER,
                        thinking_tokens INTEGER,
                        cached_content_tokens INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        auto_sql_execute INTEGER NOT NULL DEFAULT 1,
                        is_custom INTEGER NOT NULL DEFAULT 0,
                        connection_id TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Migration: existing DBs created before auto_sql_execute existed.
                cursor.execute("PRAGMA table_info(sessions);")
                session_columns = [column[1] for column in cursor.fetchall()]
                if "auto_sql_execute" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN auto_sql_execute INTEGER NOT NULL DEFAULT 1;"
                    )
                # Migration: existing DBs created before is_custom existed.
                # Defaults to 0/False - every legacy row predates the
                # preset/custom-URL-collision fix, and the safest default is
                # "not explicitly a custom pick" (matches the old, simpler
                # behavior of just matching by URL against presets first).
                if "is_custom" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0;"
                    )
                # Migration: existing DBs created before connection_id existed,
                # i.e. before a session's active connection was anything more
                # than a duplicated (database_url, database_type,
                # database_config) copy of the connection itself - see
                # get_session's docstring for why that stopped being
                # acceptable (drift when a preset/custom connection is later
                # edited or removed, and it kept credentials sitting in this
                # table redundantly). SQLite can't just ALTER a column away
                # cleanly here either (dropping database_url/database_type/
                # database_config/custom_connection_key outright), so this
                # rebuilds the table under the new schema - same pattern as
                # the db_connections connection_key migration just below -
                # backfilling connection_id for each existing row from data
                # it already has: a legacy is_custom row's own
                # custom_connection_key IS already exactly the right value
                # (reused as-is); a legacy preset row's connection_id is
                # recovered by reverse-matching its stored database_url
                # against CONFIGURED_DBS's "url" field (the same matching
                # config_routes.py used to do for the old "active_preset_id"
                # response field, before presets carried a stable id through
                # the session itself) - "" (falls back to the default
                # connection) if nothing matches, e.g. the preset was
                # renamed/removed since. This is a genuine one-way rebuild,
                # not just an added column: it's what actually scrubs any
                # previously-duplicated credentials (a preset's password, a
                # custom BigQuery key, ...) out of this table rather than
                # just leaving them sitting in an unread column forever.
                if "connection_id" not in session_columns:
                    # Deferred import, not at module level: app_config.py
                    # imports SqliteStateStore/FirestoreStateStore from this
                    # module while it's still building CONFIGURED_DBS, so a
                    # top-level "from app_config import CONFIGURED_DBS" here
                    # would be a circular import that fails at startup. By
                    # the time init() actually runs (server.py, after
                    # app_config.py has fully finished importing), the real,
                    # fully-populated module is safely importable.
                    from app_config import CONFIGURED_DBS
                    cursor.execute("ALTER TABLE sessions RENAME TO sessions_old;")
                    cursor.execute("""
                        CREATE TABLE sessions (
                            session_id TEXT PRIMARY KEY,
                            auto_sql_execute INTEGER NOT NULL DEFAULT 1,
                            is_custom INTEGER NOT NULL DEFAULT 0,
                            connection_id TEXT NOT NULL DEFAULT '',
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    cursor.execute("PRAGMA table_info(sessions_old);")
                    old_columns = {column[1] for column in cursor.fetchall()}
                    old_has_custom_key = "custom_connection_key" in old_columns
                    old_has_url = "database_url" in old_columns
                    select_cols = "session_id, auto_sql_execute, is_custom"
                    select_cols += ", custom_connection_key" if old_has_custom_key else ", NULL"
                    select_cols += ", database_url" if old_has_url else ", NULL"
                    select_cols += ", updated_at" if "updated_at" in old_columns else ", CURRENT_TIMESTAMP"
                    cursor.execute(f"SELECT {select_cols} FROM sessions_old;")
                    for (old_session_id, old_auto_exec, old_is_custom,
                         old_custom_key, old_url, old_updated_at) in cursor.fetchall():
                        if old_is_custom and old_custom_key:
                            new_connection_id = old_custom_key
                        elif not old_is_custom and old_url:
                            new_connection_id = next(
                                (db["id"] for db in CONFIGURED_DBS if db.get("url") == old_url), ""
                            )
                        else:
                            new_connection_id = ""
                        cursor.execute("""
                            INSERT OR REPLACE INTO sessions
                                (session_id, auto_sql_execute, is_custom, connection_id, updated_at)
                            VALUES (?, ?, ?, ?, ?);
                        """, (
                            old_session_id, old_auto_exec, old_is_custom,
                            new_connection_id, old_updated_at,
                        ))
                    cursor.execute("DROP TABLE sessions_old;")

                # Drop table if it exists under the old schema (where user_id was
                # the single primary key) or if the temporary custom_databases
                # column is present.
                try:
                    cursor.execute("PRAGMA table_info(db_connections);")
                    cols = cursor.fetchall()
                    if cols:
                        col_names = [c[1] for c in cols]
                        pk_cols = [c[1] for c in cols if c[5] > 0]
                        if (len(pk_cols) == 1 and pk_cols[0] == "user_id") or "custom_databases" in col_names:
                            cursor.execute("DROP TABLE db_connections;")
                except Exception:
                    pass

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS db_connections (
                        user_id TEXT,
                        connection_key TEXT NOT NULL DEFAULT '',
                        database_name TEXT NOT NULL,
                        database_url TEXT NOT NULL,
                        database_type TEXT NOT NULL DEFAULT 'postgres',
                        database_config TEXT,
                        PRIMARY KEY (user_id, connection_key)
                    );
                """)

                # Migration: existing DBs created before multi-dialect support -
                # same rationale as the sessions table migration above.
                cursor.execute("PRAGMA table_info(db_connections);")
                db_connection_columns = [column[1] for column in cursor.fetchall()]
                if "database_type" not in db_connection_columns:
                    cursor.execute(
                        "ALTER TABLE db_connections ADD COLUMN database_type TEXT NOT NULL DEFAULT 'postgres';"
                    )
                if "database_config" not in db_connection_columns:
                    cursor.execute("ALTER TABLE db_connections ADD COLUMN database_config TEXT;")

                # Migration: existing DBs created before connection_key existed,
                # i.e. before a saved connection's identity was anything more
                # than (user_id, database_url) - see compute_connection_key's
                # docstring for why url alone stopped being enough (it doesn't
                # encode name or credentials, so two custom BigQuery
                # connections on the same project/dataset with different
                # service-account keys used to silently overwrite each other).
                # SQLite can't ALTER a table's primary key in place, so this
                # rebuilds the table under the new schema, backfilling
                # connection_key for every existing row from data it already
                # has (name/url/whatever credentials are in database_config) -
                # computed the exact same way compute_connection_key() derives
                # it for new saves, so a row that's re-saved unchanged after
                # this migration keeps the same key rather than duplicating.
                if "connection_key" not in db_connection_columns:
                    cursor.execute("ALTER TABLE db_connections RENAME TO db_connections_old;")
                    cursor.execute("""
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
                    cursor.execute(
                        "SELECT user_id, database_name, database_url, database_type, database_config "
                        "FROM db_connections_old;"
                    )
                    for old_user_id, old_name, old_url, old_type, old_config_raw in cursor.fetchall():
                        old_credentials = _loads_config(old_config_raw).get("credentials_json")
                        old_key = compute_connection_key(old_name, old_url, old_credentials)
                        cursor.execute("""
                            INSERT OR REPLACE INTO db_connections
                                (user_id, connection_key, database_name, database_url, database_type, database_config)
                            VALUES (?, ?, ?, ?, ?, ?);
                        """, (old_user_id, old_key, old_name, old_url, old_type, old_config_raw))
                    cursor.execute("DROP TABLE db_connections_old;")

                cursor.execute("PRAGMA table_info(translations);")
                columns = [column[1] for column in cursor.fetchall()]
                if "user_id" not in columns:
                    cursor.execute("ALTER TABLE translations ADD COLUMN user_id TEXT;")
                # Migration: existing DBs created before the connect_string ->
                # (database_type, database_name) rename. The old connect_string
                # column (if present) is left in place untouched for any
                # legacy rows - it's just no longer read or written going
                # forward, since it stopped being a meaningful identifier
                # once presets could be BigQuery as well as Postgres.
                if "database_type" not in columns:
                    cursor.execute("ALTER TABLE translations ADD COLUMN database_type TEXT;")
                if "database_name" not in columns:
                    cursor.execute("ALTER TABLE translations ADD COLUMN database_name TEXT;")

                conn.commit()
        except Exception:
            logger.exception("Error initializing SQLite stats DB")

    def get_session(self, user_id):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT auto_sql_execute, is_custom, connection_id "
                    "FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "auto_sql_execute": bool(row[0]),
                        "is_custom": bool(row[1]),
                        "connection_id": row[2] or "",
                    }
        except Exception:
            logger.exception("Error fetching session from SQLite")
        return {
            "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
            "is_custom": False,
            "connection_id": "",
        }

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None):
        if connection_id is None and auto_sql_execute is None and is_custom is None:
            return
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # Ensure a row exists first (defaults for whichever field
                # isn't being set), then patch only the field(s) actually
                # passed in - so e.g. toggling auto_sql_execute alone never
                # clobbers an already-saved connection_id/is_custom, or vice
                # versa. A brand-new row that isn't explicitly setting
                # auto_sql_execute here still gets DEFAULT_AUTO_SQL_EXECUTE
                # (matching the column's own DEFAULT 1), not False.
                insert_auto_sql_execute = (
                    auto_sql_execute if auto_sql_execute is not None else DEFAULT_AUTO_SQL_EXECUTE
                )
                cursor.execute("""
                    INSERT INTO sessions (session_id, auto_sql_execute, is_custom, connection_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING;
                """, (
                    effective_user,
                    1 if insert_auto_sql_execute else 0,
                    1 if is_custom else 0,
                    connection_id or "",
                ))

                updates = []
                params = []
                if auto_sql_execute is not None:
                    updates.append("auto_sql_execute = ?")
                    params.append(1 if auto_sql_execute else 0)
                if is_custom is not None:
                    updates.append("is_custom = ?")
                    params.append(1 if is_custom else 0)
                if connection_id is not None:
                    updates.append("connection_id = ?")
                    params.append(connection_id)
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(effective_user)
                cursor.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
                    params,
                )
                conn.commit()
        except Exception:
            logger.exception("Error saving session to SQLite")

    def get_db_connections(self, user_id, include_credentials=False):
        effective_user = _effective_user(user_id)
        custom_dbs = []
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT connection_key, database_name, database_url, database_type, database_config "
                    "FROM db_connections WHERE user_id = ?",
                    (effective_user,),
                )
                for key, name, url, db_type, db_config_raw in cursor.fetchall():
                    config = _loads_config(db_config_raw)
                    has_custom_credentials = _has_any_credential(config)
                    if not include_credentials:
                        config = _strip_credentials(config)
                    custom_dbs.append({
                        "connection_key": key or "",
                        "name": name,
                        "type": db_type or "postgres",
                        "url": url,
                        "config": config,
                        "has_custom_credentials": has_custom_credentials,
                    })
        except Exception:
            logger.exception("Error fetching db_connection from SQLite")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None,
                            custom_databases=None, connection_key=None):
        effective_user = _effective_user(user_id)

        if custom_databases is not None:
            try:
                with self._connect() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM db_connections WHERE user_id = ?", (effective_user,))
                    for db in custom_databases:
                        u = db.get("url")
                        n = db.get("name")
                        t = db.get("type") or "postgres"
                        cfg = db.get("config") or {}
                        key = db.get("connection_key") or compute_connection_key(n, u, _credential_value_for_key(cfg))
                        if u:
                            cursor.execute("""
                                INSERT OR REPLACE INTO db_connections
                                    (user_id, connection_key, database_name, database_url, database_type, database_config)
                                VALUES (?, ?, ?, ?, ?, ?);
                            """, (effective_user, key, n or "Custom", u, t, json.dumps(cfg) if cfg else None))
                    conn.commit()
            except Exception:
                logger.exception("Error replacing custom connections in SQLite")
            return

        try:
            key = connection_key or compute_connection_key(db_name, db_url, _credential_value_for_key(db_config))
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO db_connections
                        (user_id, connection_key, database_name, database_url, database_type, database_config)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (
                    effective_user, key, db_name, db_url, db_type or "postgres",
                    json.dumps(db_config) if db_config else None,
                ))
                conn.commit()
        except Exception:
            logger.exception("Error saving single db_connection to SQLite")

    def record_translation(self, user_id, db_type, db_name, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO translations (
                        user_id, database_type, database_name, nl_prompt, sql_command, model,
                        duration, input_tokens, output_tokens, total_tokens,
                        thinking_tokens, cached_content_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    effective_user, db_type, db_name, nl_prompt, sql_command, model,
                    duration, input_tokens, output_tokens, total_tokens,
                    thinking_tokens, cached_content_tokens,
                ))
                conn.commit()
        except Exception:
            logger.exception("Error recording translation")

    def get_translation_history(self, user_id):
        effective_user = _effective_user(user_id)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT COUNT(*) as total_count FROM translations WHERE user_id = ?",
                (effective_user,),
            )
            total_row = cursor.fetchone()
            total_count = total_row["total_count"] if total_row else 0

            cursor.execute("""
                SELECT nl_prompt, sql_command, created_at
                FROM translations WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 50
            """, (effective_user,))
            rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT
                    DATE(created_at) as day_date,
                    COUNT(*) as total_translations,
                    SUM(total_tokens) as sum_total_tokens,
                    SUM(input_tokens) as sum_input_tokens
                FROM translations WHERE user_id = ?
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at) ASC
            """, (effective_user,))
            stats = [dict(row) for row in cursor.fetchall()]

        return rows, stats, total_count

    def purge_translation_history(self, user_id):
        effective_user = _effective_user(user_id)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM translations WHERE user_id = ?", (effective_user,))
            conn.commit()


# --------------------------------------------------------------------------
# Firestore backend (Cloud Run)
# --------------------------------------------------------------------------

class FirestoreStateStore(StateStore):
    def __init__(self, client):
        self.client = client

    def init(self):
        # No schema/migrations needed for Firestore.
        pass

    def get_session(self, user_id):
        default_session = {
            "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
            "is_custom": False,
            "connection_id": "",
        }
        if not user_id:
            return default_session
        try:
            doc_ref = self.client.collection("sessions").document(user_id)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict() or {}
                if "connection_id" not in data:
                    # Lazy migration, on first read after upgrading: this doc
                    # predates connection_id and still carries the old
                    # duplicated (database_url, database_type,
                    # database_config, custom_connection_key) shape (see
                    # get_session's docstring for why that stopped being
                    # acceptable). Recover connection_id from data it already
                    # has - a legacy is_custom doc's own custom_connection_key
                    # IS already exactly the right value; a legacy preset
                    # doc's connection_id is recovered by reverse-matching
                    # its stored database_url against CONFIGURED_DBS's "url"
                    # field - then write back a cleaned doc that actually
                    # deletes the old fields (firestore.DELETE_FIELD), not
                    # just adds connection_id alongside them, so credentials
                    # (a preset's password, a custom BigQuery key, ...) don't
                    # linger in this document indefinitely. Deferred import,
                    # not at module level - see the matching comment in
                    # SqliteStateStore.init() for why (app_config.py imports
                    # this module while still building CONFIGURED_DBS).
                    from app_config import CONFIGURED_DBS
                    old_is_custom = bool(data.get("is_custom", False))
                    old_custom_key = data.get("custom_connection_key") or ""
                    old_url = data.get("database_url") or ""
                    if old_is_custom and old_custom_key:
                        connection_id = old_custom_key
                    elif not old_is_custom and old_url:
                        connection_id = next(
                            (db["id"] for db in CONFIGURED_DBS if db.get("url") == old_url), ""
                        )
                    else:
                        connection_id = ""
                    try:
                        doc_ref.set({
                            "is_custom": old_is_custom,
                            "connection_id": connection_id,
                            "auto_sql_execute": data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE),
                            "database_url": firestore.DELETE_FIELD,
                            "database_type": firestore.DELETE_FIELD,
                            "database_config": firestore.DELETE_FIELD,
                            "custom_connection_key": firestore.DELETE_FIELD,
                        }, merge=True)
                    except Exception:
                        logger.exception("Error scrubbing legacy session fields in Firestore")
                    return {
                        "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                        "is_custom": old_is_custom,
                        "connection_id": connection_id,
                    }
                return {
                    "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                    "is_custom": bool(data.get("is_custom", False)),
                    "connection_id": data.get("connection_id") or "",
                }
        except Exception:
            logger.exception("Error fetching session from Firestore")
        return default_session

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None):
        if not user_id or (connection_id is None and auto_sql_execute is None and is_custom is None):
            return
        update_data = {"updated_at": firestore.SERVER_TIMESTAMP}
        if connection_id is not None:
            update_data["connection_id"] = connection_id
        if auto_sql_execute is not None:
            update_data["auto_sql_execute"] = bool(auto_sql_execute)
        if is_custom is not None:
            update_data["is_custom"] = bool(is_custom)
        try:
            # merge=list(update_data.keys()) - NOT the boolean merge=True -
            # is what actually gives "patch these top-level fields, leave
            # the rest of the document alone" semantics here (e.g. leaving
            # connection_id/is_custom untouched on an auto_sql_execute-only
            # call). See set_session's docstring/callers - most calls only
            # pass a subset of fields.
            self.client.collection("sessions").document(user_id).set(
                update_data, merge=list(update_data.keys())
            )
        except Exception:
            logger.exception("Error saving session to Firestore")

    def get_db_connections(self, user_id, include_credentials=False):
        if not user_id:
            return []
        effective_user = _effective_user(user_id)
        custom_dbs = []
        try:
            docs = self.client.collection("db_connections").where("user_id", "==", effective_user).stream()
            for doc in docs:
                data = doc.to_dict()
                if data and data.get("database_url"):
                    config = dict(data.get("database_config") or {})
                    has_custom_credentials = _has_any_credential(config)
                    if not include_credentials:
                        config = _strip_credentials(config)
                    custom_dbs.append({
                        # "" for any doc written before connection_key existed
                        # and not yet re-saved - see compute_connection_key's
                        # docstring/set_db_connections below.
                        "connection_key": data.get("connection_key") or "",
                        "name": data.get("database_name", "Custom"),
                        "type": data.get("database_type") or "postgres",
                        "url": data.get("database_url", ""),
                        "config": config,
                        "has_custom_credentials": has_custom_credentials,
                    })
        except Exception:
            logger.exception("Error fetching db_connection from Firestore")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None,
                            custom_databases=None, connection_key=None):
        effective_user = _effective_user(user_id)

        if custom_databases is not None:
            try:
                docs = self.client.collection("db_connections").where("user_id", "==", effective_user).stream()
                for doc in docs:
                    doc.reference.delete()
                for db in custom_databases:
                    u = db.get("url")
                    n = db.get("name")
                    t = db.get("type") or "postgres"
                    cfg = db.get("config") or {}
                    key = db.get("connection_key") or compute_connection_key(n, u, _credential_value_for_key(cfg))
                    if u:
                        doc_id = f"{effective_user}_{key}"
                        self.client.collection("db_connections").document(doc_id).set({
                            "user_id": effective_user,
                            "connection_key": key,
                            "database_name": n or "Custom",
                            "database_type": t,
                            "database_url": u,
                            "database_config": cfg,
                            "updated_at": firestore.SERVER_TIMESTAMP,
                        })
            except Exception:
                logger.exception("Error replacing custom connections in Firestore")
            return

        try:
            key = connection_key or compute_connection_key(db_name, db_url, _credential_value_for_key(db_config))
            doc_id = f"{effective_user}_{key}"
            self.client.collection("db_connections").document(doc_id).set({
                "user_id": effective_user,
                "connection_key": key,
                "database_name": db_name,
                "database_type": db_type or "postgres",
                "database_url": db_url,
                "database_config": db_config or {},
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            logger.exception("Error saving single db_connection to Firestore")

    def record_translation(self, user_id, db_type, db_name, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        effective_user = _effective_user(user_id)
        try:
            self.client.collection("translations").add({
                "user_id": effective_user,
                "database_type": db_type,
                "database_name": db_name,
                "nl_prompt": nl_prompt,
                "sql_command": sql_command,
                "model": model,
                "duration": duration,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "thinking_tokens": thinking_tokens,
                "cached_content_tokens": cached_content_tokens,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            logger.exception("Error recording translation in Firestore")

    def get_translation_history(self, user_id):
        # Note: matches prior behavior of querying by the raw user_id here
        # (unlike purge_translation_history, which uses the "global" fallback).
        # This code path only runs on Cloud Run, where auth is enforced and
        # user_id is never empty, so the distinction is not user-visible.
        docs = (
            self.client.collection("translations")
            .where("user_id", "==", user_id)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(50)
            .stream()
        )
        rows = []
        for doc in docs:
            d = doc.to_dict()
            created_at = d.get("created_at")
            if created_at:
                created_at = created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created_at, "strftime") else str(created_at)
            else:
                created_at = ""
            rows.append({
                "nl_prompt": d.get("nl_prompt", ""),
                "sql_command": d.get("sql_command", ""),
                "created_at": created_at,
            })

        docs_all = self.client.collection("translations").where("user_id", "==", user_id).stream()
        daily = {}
        total_count = 0
        for doc in docs_all:
            d = doc.to_dict()
            total_count += 1
            dt = d.get("created_at")
            if not dt:
                continue
            day_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]

            bucket = daily.setdefault(day_str, {
                "day_date": day_str,
                "total_translations": 0,
                "sum_total_tokens": 0,
                "sum_input_tokens": 0,
            })
            bucket["total_translations"] += 1
            bucket["sum_total_tokens"] += d.get("total_tokens", 0) or 0
            bucket["sum_input_tokens"] += d.get("input_tokens", 0) or 0

        stats = sorted(daily.values(), key=lambda x: x["day_date"])
        return rows, stats, total_count

    def purge_translation_history(self, user_id):
        effective_user = _effective_user(user_id)
        docs = self.client.collection("translations").where("user_id", "==", effective_user).stream()
        batch = self.client.batch()
        count = 0
        for doc in docs:
            batch.delete(doc.reference)
            count += 1
            if count >= 400:
                batch.commit()
                batch = self.client.batch()
                count = 0
        if count > 0:
            batch.commit()