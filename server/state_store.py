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


class StateStore(ABC):
    """Backend-agnostic persistence for sessions, saved DB connections, and
    translation history/stats."""

    def __init__(self, default_conn, default_model):
        self.default_conn = default_conn
        self.default_model = default_model

    @abstractmethod
    def init(self):
        """One-time setup (schema creation, migrations). Safe to call on every startup."""

    @abstractmethod
    def get_session(self, user_id):
        """Returns (database_url, active_model) for a user/session id."""

    @abstractmethod
    def set_session(self, user_id, db_url=None, model=None):
        """Persists the active database_url and/or model for a user/session id."""

    @abstractmethod
    def get_db_connections(self, user_id):
        """Returns a list of {"name", "url"} saved connections for a user."""

    @abstractmethod
    def set_db_connections(self, user_id, db_name, db_url, custom_databases=None):
        """Saves a single connection, or replaces the whole saved list if
        custom_databases is provided."""

    @abstractmethod
    def record_translation(self, user_id, conn_identifier, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        """Logs one NL->SQL translation event."""

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
    def __init__(self, db_path, default_conn, default_model):
        super().__init__(default_conn, default_model)
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
                        connect_string TEXT,
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
                        active_model TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

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
                        PRIMARY KEY (user_id, database_url)
                    );
                """)

                cursor.execute("PRAGMA table_info(translations);")
                columns = [column[1] for column in cursor.fetchall()]
                if "user_id" not in columns:
                    cursor.execute("ALTER TABLE translations ADD COLUMN user_id TEXT;")

                cursor.execute("PRAGMA table_info(sessions);")
                session_columns = [column[1] for column in cursor.fetchall()]
                if "active_model" not in session_columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN active_model TEXT;")

                conn.commit()
        except Exception:
            logger.exception("Error initializing SQLite stats DB")

    def get_session(self, user_id):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT database_url, active_model FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                if row:
                    return (row[0] or self.default_conn), (row[1] or self.default_model)
        except Exception:
            logger.exception("Error fetching session from SQLite")
        return self.default_conn, self.default_model

    def set_session(self, user_id, db_url=None, model=None):
        if not db_url and not model:
            return
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO sessions (session_id, database_url, active_model, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(session_id) DO UPDATE SET
                        database_url = COALESCE(excluded.database_url, sessions.database_url),
                        active_model = COALESCE(excluded.active_model, sessions.active_model),
                        updated_at = CURRENT_TIMESTAMP;
                """, (effective_user, db_url or self.default_conn, model or self.default_model))
                conn.commit()
        except Exception:
            logger.exception("Error saving session to SQLite")

    def get_db_connections(self, user_id):
        effective_user = _effective_user(user_id)
        custom_dbs = []
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT database_name, database_url FROM db_connections WHERE user_id = ?",
                    (effective_user,),
                )
                for name, url in cursor.fetchall():
                    custom_dbs.append({"name": name, "url": url})
        except Exception:
            logger.exception("Error fetching db_connection from SQLite")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_url, custom_databases=None):
        effective_user = _effective_user(user_id)

        if custom_databases is not None:
            try:
                with self._connect() as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM db_connections WHERE user_id = ?", (effective_user,))
                    for db in custom_databases:
                        u = db.get("url")
                        n = db.get("name")
                        if u:
                            cursor.execute("""
                                INSERT OR REPLACE INTO db_connections (user_id, database_name, database_url)
                                VALUES (?, ?, ?);
                            """, (effective_user, n or "Custom", u))
                    conn.commit()
            except Exception:
                logger.exception("Error replacing custom connections in SQLite")
            return

        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO db_connections (user_id, database_name, database_url)
                    VALUES (?, ?, ?);
                """, (effective_user, db_name, db_url))
                conn.commit()
        except Exception:
            logger.exception("Error saving single db_connection to SQLite")

    def record_translation(self, user_id, conn_identifier, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO translations (
                        user_id, connect_string, nl_prompt, sql_command, model,
                        duration, input_tokens, output_tokens, total_tokens,
                        thinking_tokens, cached_content_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    effective_user, conn_identifier, nl_prompt, sql_command, model,
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
    def __init__(self, client, default_conn, default_model):
        super().__init__(default_conn, default_model)
        self.client = client

    def init(self):
        # No schema/migrations needed for Firestore.
        pass

    def get_session(self, user_id):
        if not user_id:
            return self.default_conn, self.default_model
        try:
            doc = self.client.collection("sessions").document(user_id).get()
            if doc.exists:
                data = doc.to_dict() or {}
                return (
                    data.get("database_url", self.default_conn),
                    data.get("active_model", self.default_model),
                )
        except Exception:
            logger.exception("Error fetching session from Firestore")
        return self.default_conn, self.default_model

    def set_session(self, user_id, db_url=None, model=None):
        if not user_id or (not db_url and not model):
            return
        update_data = {"updated_at": firestore.SERVER_TIMESTAMP}
        if db_url:
            update_data["database_url"] = db_url
        if model:
            update_data["active_model"] = model
        try:
            self.client.collection("sessions").document(user_id).set(update_data, merge=True)
        except Exception:
            logger.exception("Error saving session to Firestore")

    def get_db_connections(self, user_id):
        if not user_id:
            return []
        effective_user = _effective_user(user_id)
        custom_dbs = []
        try:
            docs = self.client.collection("db_connections").where("user_id", "==", effective_user).stream()
            for doc in docs:
                data = doc.to_dict()
                if data and data.get("database_url"):
                    custom_dbs.append({
                        "name": data.get("database_name", "Custom"),
                        "url": data.get("database_url", ""),
                    })
        except Exception:
            logger.exception("Error fetching db_connection from Firestore")
        return custom_dbs

    def set_db_connections(self, user_id, db_name, db_url, custom_databases=None):
        effective_user = _effective_user(user_id)

        if custom_databases is not None:
            try:
                docs = self.client.collection("db_connections").where("user_id", "==", effective_user).stream()
                for doc in docs:
                    doc.reference.delete()
                for db in custom_databases:
                    u = db.get("url")
                    n = db.get("name")
                    if u:
                        url_hash = hashlib.sha256(u.encode("utf-8")).hexdigest()
                        doc_id = f"{effective_user}_{url_hash}"
                        self.client.collection("db_connections").document(doc_id).set({
                            "user_id": effective_user,
                            "database_name": n or "Custom",
                            "database_url": u,
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
                "database_url": db_url,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            logger.exception("Error saving single db_connection to Firestore")

    def record_translation(self, user_id, conn_identifier, nl_prompt, sql_command,
                            model, duration, input_tokens, output_tokens,
                            total_tokens, thinking_tokens, cached_content_tokens):
        effective_user = _effective_user(user_id)
        try:
            self.client.collection("translations").add({
                "user_id": effective_user,
                "connect_string": conn_identifier,
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