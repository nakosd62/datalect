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
_CREDENTIAL_CONFIG_FIELDS = {"credentials_json"}


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


class StateStore(ABC):
    """Backend-agnostic persistence for sessions, saved DB connections, and
    translation history/stats."""

    def __init__(self, default_conn):
        self.default_conn = default_conn

    @abstractmethod
    def init(self):
        """One-time setup (schema creation, migrations). Safe to call on every startup."""

    @abstractmethod
    def get_session(self, user_id):
        """Returns {"database_url", "auto_sql_execute", "database_type",
        "database_config", "is_custom"} for a user/session id. "database_type"
        defaults to "postgres" for legacy rows that predate multi-dialect
        support. "database_config" is a dict of dialect-specific fields beyond
        database_url (e.g. BigQuery's project_id/dataset/credentials_json)
        - {} for Postgres. "is_custom" (defaults to False for legacy rows)
        records whether the active connection was selected *as a saved
        custom connection* rather than a preset - needed because a custom
        connection's URL can collide with a preset's (see config_routes.py's
        handle_config), in which case URL equality alone can't tell the two
        apart and would silently attribute the active connection to whichever
        one happens to match. Unlike get_db_connections(), this always
        includes any credentials in database_config, since it's the method
        db.py uses internally to actually open a connection - callers that
        expose this data over the API (config_routes.py) must pick out
        only the fields they intend to return, never forward this dict
        as-is to a jsonify() call."""

    @abstractmethod
    def set_session(self, user_id, db_url=None, auto_sql_execute=None, db_type=None, db_config=None, is_custom=None):
        """Persists the active connection (database_url, db_type, db_config,
        is_custom) and/or auto_sql_execute flag for a user/session id. Only
        the fields passed (not None) are changed - the others are left as-is."""

    @abstractmethod
    def get_db_connections(self, user_id, include_credentials=False):
        """Returns a list of {"name", "type", "url", "config"} saved
        connections for a user. By default, any credential fields (e.g.
        BigQuery's credentials_json) are stripped from "config" - this
        method's normal caller is config_routes.py building an API
        response, and credentials must never round-trip to the frontend.
        Pass include_credentials=True only for server-side use (e.g.
        merging in a previously-saved credential when a user edits a
        connection without re-pasting its key)."""

    @abstractmethod
    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None, custom_databases=None):
        """Saves a single connection, or replaces the whole saved list if
        custom_databases is provided (each item shaped like the dicts
        get_db_connections returns, i.e. {"name", "type", "url", "config"})."""

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
    def __init__(self, db_path, default_conn):
        super().__init__(default_conn)
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
                        database_url TEXT NOT NULL,
                        auto_sql_execute INTEGER NOT NULL DEFAULT 1,
                        database_type TEXT NOT NULL DEFAULT 'postgres',
                        database_config TEXT,
                        is_custom INTEGER NOT NULL DEFAULT 0,
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
                # Migration: existing DBs created before multi-dialect support.
                # database_type defaults to 'postgres' - every row that predates
                # this column was, by definition, a Postgres connection string.
                # database_config holds dialect-specific fields beyond
                # database_url (e.g. BigQuery's project_id/dataset/
                # credentials_json, JSON-encoded) - NULL/absent for Postgres.
                if "database_type" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN database_type TEXT NOT NULL DEFAULT 'postgres';"
                    )
                if "database_config" not in session_columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN database_config TEXT;")
                # Migration: existing DBs created before is_custom existed.
                # Defaults to 0/False - every legacy row predates the
                # preset/custom-URL-collision fix, and the safest default is
                # "not explicitly a custom pick" (matches the old, simpler
                # behavior of just matching by URL against presets first).
                if "is_custom" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0;"
                    )

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
                        database_name TEXT NOT NULL,
                        database_url TEXT NOT NULL,
                        database_type TEXT NOT NULL DEFAULT 'postgres',
                        database_config TEXT,
                        PRIMARY KEY (user_id, database_url)
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
                    "SELECT database_url, auto_sql_execute, database_type, database_config, is_custom "
                    "FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "database_url": row[0] or self.default_conn,
                        "auto_sql_execute": bool(row[1]),
                        "database_type": row[2] or "postgres",
                        "database_config": _loads_config(row[3]),
                        "is_custom": bool(row[4]),
                    }
        except Exception:
            logger.exception("Error fetching session from SQLite")
        return {
            "database_url": self.default_conn,
            "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
            "database_type": "postgres",
            "database_config": {},
            "is_custom": False,
        }

    def set_session(self, user_id, db_url=None, auto_sql_execute=None, db_type=None, db_config=None, is_custom=None):
        if (db_url is None and auto_sql_execute is None and db_type is None
                and db_config is None and is_custom is None):
            return
        effective_user = _effective_user(user_id)
        db_config_json = json.dumps(db_config) if db_config is not None else None
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # Ensure a row exists first (defaults for whichever field
                # isn't being set), then patch only the field(s) actually
                # passed in - so e.g. toggling auto_sql_execute alone never
                # clobbers an already-saved database_url/type/config/
                # is_custom, or vice versa. A brand-new row that isn't
                # explicitly setting auto_sql_execute here still gets
                # DEFAULT_AUTO_SQL_EXECUTE (matching the column's own
                # DEFAULT 1), not False.
                insert_auto_sql_execute = (
                    auto_sql_execute if auto_sql_execute is not None else DEFAULT_AUTO_SQL_EXECUTE
                )
                cursor.execute("""
                    INSERT INTO sessions (session_id, database_url, auto_sql_execute, database_type, database_config, is_custom)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO NOTHING;
                """, (
                    effective_user,
                    db_url or self.default_conn,
                    1 if insert_auto_sql_execute else 0,
                    db_type or "postgres",
                    db_config_json,
                    1 if is_custom else 0,
                ))

                updates = []
                params = []
                if db_url is not None:
                    updates.append("database_url = ?")
                    params.append(db_url)
                if auto_sql_execute is not None:
                    updates.append("auto_sql_execute = ?")
                    params.append(1 if auto_sql_execute else 0)
                if db_type is not None:
                    updates.append("database_type = ?")
                    params.append(db_type)
                if db_config is not None:
                    updates.append("database_config = ?")
                    params.append(db_config_json)
                if is_custom is not None:
                    updates.append("is_custom = ?")
                    params.append(1 if is_custom else 0)
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
                    "SELECT database_name, database_url, database_type, database_config "
                    "FROM db_connections WHERE user_id = ?",
                    (effective_user,),
                )
                for name, url, db_type, db_config_raw in cursor.fetchall():
                    config = _loads_config(db_config_raw)
                    if not include_credentials:
                        config = _strip_credentials(config)
                    custom_dbs.append({
                        "name": name,
                        "type": db_type or "postgres",
                        "url": url,
                        "config": config,
                    })
        except Exception:
            logger.exception("Error fetching db_connection from SQLite")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None, custom_databases=None):
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
                        if u:
                            cursor.execute("""
                                INSERT OR REPLACE INTO db_connections
                                    (user_id, database_name, database_url, database_type, database_config)
                                VALUES (?, ?, ?, ?, ?);
                            """, (effective_user, n or "Custom", u, t, json.dumps(cfg) if cfg else None))
                    conn.commit()
            except Exception:
                logger.exception("Error replacing custom connections in SQLite")
            return

        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO db_connections
                        (user_id, database_name, database_url, database_type, database_config)
                    VALUES (?, ?, ?, ?, ?);
                """, (
                    effective_user, db_name, db_url, db_type or "postgres",
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
    def __init__(self, client, default_conn):
        super().__init__(default_conn)
        self.client = client

    def init(self):
        # No schema/migrations needed for Firestore.
        pass

    def get_session(self, user_id):
        if not user_id:
            return {
                "database_url": self.default_conn,
                "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
                "database_type": "postgres",
                "database_config": {},
                "is_custom": False,
            }
        try:
            doc = self.client.collection("sessions").document(user_id).get()
            if doc.exists:
                data = doc.to_dict() or {}
                return {
                    "database_url": data.get("database_url", self.default_conn),
                    "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                    "database_type": data.get("database_type") or "postgres",
                    "database_config": data.get("database_config") or {},
                    "is_custom": bool(data.get("is_custom", False)),
                }
        except Exception:
            logger.exception("Error fetching session from Firestore")
        return {
            "database_url": self.default_conn,
            "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
            "database_type": "postgres",
            "database_config": {},
            "is_custom": False,
        }

    def set_session(self, user_id, db_url=None, auto_sql_execute=None, db_type=None, db_config=None, is_custom=None):
        if not user_id or (db_url is None and auto_sql_execute is None and db_type is None
                           and db_config is None and is_custom is None):
            return
        update_data = {"updated_at": firestore.SERVER_TIMESTAMP}
        if db_url is not None:
            update_data["database_url"] = db_url
        if auto_sql_execute is not None:
            update_data["auto_sql_execute"] = bool(auto_sql_execute)
        if db_type is not None:
            update_data["database_type"] = db_type
        if is_custom is not None:
            update_data["is_custom"] = bool(is_custom)
        if db_config is not None:
            # Overwrite (not merge) the config map itself, wrapped inside
            # the merge=True call below - Firestore's merge=True only
            # avoids clobbering *other top-level fields* on the document
            # (like database_url); this nested map is still replaced
            # wholesale, which is what we want (a stale bigquery config
            # left behind after switching back to postgres would be
            # confusing, not helpful).
            update_data["database_config"] = db_config
        try:
            self.client.collection("sessions").document(user_id).set(update_data, merge=True)
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
                    if not include_credentials:
                        config = _strip_credentials(config)
                    custom_dbs.append({
                        "name": data.get("database_name", "Custom"),
                        "type": data.get("database_type") or "postgres",
                        "url": data.get("database_url", ""),
                        "config": config,
                    })
        except Exception:
            logger.exception("Error fetching db_connection from Firestore")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_type, db_url, db_config=None, custom_databases=None):
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
                    if u:
                        url_hash = hashlib.sha256(u.encode("utf-8")).hexdigest()
                        doc_id = f"{effective_user}_{url_hash}"
                        self.client.collection("db_connections").document(doc_id).set({
                            "user_id": effective_user,
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
            url_hash = hashlib.sha256(db_url.encode("utf-8")).hexdigest()
            doc_id = f"{effective_user}_{url_hash}"
            self.client.collection("db_connections").document(doc_id).set({
                "user_id": effective_user,
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