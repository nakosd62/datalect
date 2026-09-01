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

from cryptography.fernet import Fernet
from google.cloud import firestore

# Reuses the same logger name/config server.py sets up (root logger stays
# quiet at WARNING; this "ydyl" logger is bumped to LOG_LEVEL/INFO there).
# If this module is ever imported standalone without server.py's config
# having run, it still works - it just falls back to logging defaults.
logger = logging.getLogger("ydyl")


# --- Encryption at rest for database_config ---------------------------------
#
# database_config (see below) can carry a saved connection's password, a
# BigQuery service-account key, a Snowflake private key, a Postgres/MySQL
# CA certificate, and so on. Rather than maintaining a field-by-field
# allowlist of "these specific keys are sensitive, encrypt just those"
# (easy to miss a newly-added field one day - see _CREDENTIAL_CONFIG_FIELDS
# above, a similar-looking allowlist but for a completely different
# purpose: API-response redaction, not storage), the WHOLE database_config
# dict is encrypted as one opaque blob before it's ever written to SQLite
# or Firestore, and decrypted transparently on read. A field added to any
# backend's config in the future is automatically covered without anyone
# needing to remember to add it to a list here.
#
# The key itself is never stored alongside the data it protects - it's
# read from DB_CONFIG_ENCRYPTION_KEY_ENV_VAR (a Fernet key: AES-128-CBC +
# HMAC-SHA256, from the `cryptography` package, already a production
# dependency - see requirements.txt), the same way GOOGLE_CLIENT_ID/
# GEMINI_API_KEY/etc are already read from the environment
# (app_config.py) - via a real secret manager (e.g. Cloud Run's Secret
# Manager integration) in production, a plain .env locally. See
# app_config.py's "Startup / Module Scope Guard" section for what happens
# when this is missing/invalid on Cloud Run specifically.
#
# Backward compatibility for rows written before this existed (or written
# while no/an invalid key was configured) needs no separate migration
# step: decryption is attempted first, and ANY failure (no cipher
# configured, wrong/rotated key, or the value was never encrypted to
# begin with) falls back to treating the stored value as the plain,
# unencrypted representation this module always used before - see
# _loads_config (SQLite's TEXT column - always a string either way) and
# _decrypt_firestore_config (Firestore's field - a native map before this
# existed, a string once a valid key is configured) below. A legacy row
# is transparently re-encrypted the next time it's saved, not proactively
# rewritten by this module.
DB_CONFIG_ENCRYPTION_KEY_ENV_VAR = "DB_CONFIG_ENCRYPTION_KEY"


def _load_cipher():
    """Returns a fresh Fernet cipher built from the CURRENT
    DB_CONFIG_ENCRYPTION_KEY_ENV_VAR value, or None if it's unset or not a
    valid Fernet key. Deliberately re-reads the env var and reconstructs
    the Fernet object on every call rather than caching it once at import
    time - the actual cost of doing so is negligible (base64-decoding a
    32-byte key; no KDF involved), and this way a changed env var takes
    effect on the very next call with no special re-import/restart step
    needed to pick it up. A None result means database_config is stored
    as plain JSON text / a native Firestore map, exactly as it was before
    this feature existed - this function itself stays permissive so
    purely-local dev keeps working with zero configuration, same as
    GOOGLE_CLIENT_ID being unset today; app_config.py's startup guard is
    what turns "no valid key" into a hard failure specifically on Cloud
    Run."""
    raw_key = os.environ.get(DB_CONFIG_ENCRYPTION_KEY_ENV_VAR, "").strip()
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception:
        logger.error(
            "%s is set but is not a valid Fernet key - database_config will be "
            "stored UNENCRYPTED until this is fixed. Generate a valid key with: "
            'python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
            DB_CONFIG_ENCRYPTION_KEY_ENV_VAR,
        )
        return None


def is_db_config_encryption_configured():
    """Whether a valid encryption key is configured right now - used by
    app_config.py's startup guard to decide whether to halt startup on
    Cloud Run (see this module's encryption-at-rest comment above)."""
    return _load_cipher() is not None


def _encrypt_config_to_text(config):
    """The value to actually persist for a database_config dict in
    SQLite's TEXT column: Fernet-encrypted JSON when a cipher is
    configured, or the same plain JSON text this stored before encryption
    at rest existed when it isn't (see _load_cipher) - either way, a str,
    matching the column's type. Firestore's write path
    (_config_value_to_store below) has its own wrapper, since a Firestore
    field isn't limited to text the way a SQLite column is."""
    raw = json.dumps(config or {})
    cipher = _load_cipher()
    if cipher is None:
        return raw
    return cipher.encrypt(raw.encode("utf-8")).decode("utf-8")


def _config_value_to_store(config):
    """The value to actually persist for a database_config field in
    Firestore. Contrast _encrypt_config_to_text just above, which always
    returns a str for SQLite's TEXT column - Firestore has no such
    constraint, so when no cipher is configured this keeps writing the
    native map Firestore always wrote for this field before encryption at
    rest existed, rather than a JSON-text string it would then have to be
    told apart from by type on read (see _decrypt_firestore_config)."""
    cipher = _load_cipher()
    if cipher is None:
        return config or {}
    return _encrypt_config_to_text(config)


def _effective_user(user_id):
    """Local/anonymous requests are bucketed under a single 'global' identity."""
    return user_id or "global"


def _lazy_derive_in_scope(connection_id, is_custom):
    """Fallback (preset_ids, custom_keys) pair for a session that predates
    the in-scope-connections feature (see get_session's docstring) - i.e.
    one that has never explicitly saved in_scope_preset_ids/
    in_scope_custom_connection_keys. Derives a single-entry in-scope set
    from the session's existing (connection_id, is_custom) identity
    reference, so an existing session's current connection becomes its
    sole initially-"checked" box for free, with no proactive migration/
    rewrite needed - this just runs again on every read until the session
    is explicitly saved with the new fields (e.g. the first time the user
    opens the now-checkbox connection picker and hits Save).

    connection_id == "" (nothing ever explicitly selected) derives to two
    empty lists - db.py's resolution layer already treats an empty in-scope
    set as "nothing configured, fall back to the app default", the same
    convention resolve_active_descriptor uses for a blank connection_id."""
    if not connection_id:
        return [], []
    if is_custom:
        return [], [connection_id]
    return [connection_id], []


def _encode_in_scope_list(value):
    """JSON-encodes an in-scope id/key list for a SQLite TEXT column."""
    return json.dumps(list(value) if value is not None else [])


def _decode_in_scope_list(raw_json):
    """Best-effort decode for a stored in-scope id/key list column - never
    raises, degrades to [] on anything malformed/foreign, same posture as
    _loads_config above."""
    if not raw_json:
        return []
    try:
        decoded = json.loads(raw_json)
        return decoded if isinstance(decoded, list) else []
    except Exception:
        return []


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
    """Best-effort decode for a stored database_config value: SQLite's
    TEXT column value directly, or (via _decrypt_firestore_config)
    Firestore's field value once that's already been confirmed to be a
    str there. Tries Fernet-decryption first when a cipher is configured
    (see _load_cipher above), then falls through to parsing the result as
    plain JSON regardless of whether decryption ran at all - that's what
    makes a legacy plaintext row (written before encryption at rest
    existed, or while no/a different key was configured) keep reading
    correctly under a newly-configured key, with no separate migration
    step required. Never raises - a corrupt/foreign value just degrades
    to an empty config rather than breaking session/connection loading
    entirely."""
    if not raw_json:
        return {}
    cipher = _load_cipher()
    if cipher is not None:
        try:
            raw_json = cipher.decrypt(raw_json.encode("utf-8")).decode("utf-8")
        except Exception:
            # Not (or no longer) valid ciphertext under this key - fall
            # through and try it as plain JSON below instead of treating
            # this as an error; see this function's docstring.
            pass
    try:
        return json.loads(raw_json) or {}
    except Exception:
        logger.warning("Failed to parse stored database_config JSON; ignoring it.")
        return {}


def _decrypt_firestore_config(value):
    """Inverse of _config_value_to_store for a database_config field
    already read back from Firestore. A dict means it was written as a
    native map - either before encryption at rest existed, or while no
    cipher was configured at write time - and is returned as-is. A str
    means it was written as Fernet-encrypted text (_config_value_to_store
    only ever produces a str when a cipher IS configured), so it's run
    through _loads_config's decrypt-then-fall-back-to-plain-JSON logic -
    note a plain JSON *string* is not a representation Firestore itself
    ever wrote for this field, so falling all the way through to that
    branch here means either a value encrypted under a different/
    no-longer-configured key, or genuinely foreign data; either way it
    degrades to {} rather than raising, same as everywhere else in this
    module."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _loads_config(value)
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


# How many rows get_translation_history() returns for the history popup's
# translations list, most-recent first - NOT a cap on how much history is
# actually stored (purge is still the only way to remove rows) and NOT a
# cap on the aggregated per-day stats the same call returns alongside it
# (see get_translation_history's docstring below and both backends'
# implementations: the stats query has no LIMIT, so the stats tab always
# reflects the complete history even once the list has been truncated to
# this many rows). Same "env var, sane default" pattern as app_config.py's
# MAX_IN_SCOPE_CONNECTIONS and translate_routes.py's HISTORY_RESULT_MAX_ROWS/
# HISTORY_MAX_TURNS, but defined here (not app_config.py) since app_config.py
# imports FROM this module (see its own "Startup / Module Scope Guard"
# section) - state_store.py importing back from app_config.py would be
# circular, and this constant has exactly one consumer (this module's two
# StateStore implementations) so there's no shared-module reason to hoist
# it up there anyway.
TRANSLATION_HISTORY_LIST_LIMIT = int(os.environ.get("TRANSLATION_HISTORY_LIST_LIMIT", 50))


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
        """Returns {"auto_sql_execute", "is_custom", "connection_id",
        "llm_provider", "llm_model", "in_scope_preset_ids",
        "in_scope_custom_connection_keys", "in_scope_mode", "theme"} for a
        user/session id - identity only,
        never a connection's actual details/credentials (see db.py's
        resolve_active_descriptor, which resolves those FRESH from
        CONFIGURED_DBS or get_db_connections() every time something needs
        to actually connect, rather than trusting anything cached here).
        "is_custom" (defaults to False for legacy rows) records whether the
        active connection is a saved custom connection rather than a
        preset. "connection_id" (defaults to "" - "nothing explicitly
        selected yet") is, depending on is_custom: a preset's stable
        CONFIGURED_DBS "id" (see app_config.py's DATABASE_PRESETS_FILE
        comment) when is_custom is False, or a saved custom connection's
        compute_connection_key() value when is_custom is True - either way,
        a single opaque reference resolved fresh at connect time, never a
        duplicated copy of the connection itself. This also means a removed
        preset or a deleted custom connection is immediately reflected
        everywhere (no drift) - see resolve_active_descriptor's "missing"
        return for how a connection_id that no longer resolves to anything
        real is handled.

        "llm_provider"/"llm_model" (both default to "" - "nothing
        explicitly selected yet", same convention as connection_id above,
        not auto_sql_execute's baked-in-default one) are the user's saved
        model-selection choice (see translate_routes.py's LlmProvider/
        get_llm_provider). A blank value means "use this app's one
        hardcoded default (Google/gemini-3.6-flash)" - resolved at the
        point of use (translate_query() calls get_llm_provider(''), whose
        own fallback IS that hardcoded default - see its docstring), not
        baked into a default here, since unlike auto_sql_execute's
        True/False there's no single hardcoded stand-in value that would
        stay correct if that hardcoded default ever changed.

        "in_scope_preset_ids"/"in_scope_custom_connection_keys" (both lists
        of ids/keys, in the same reference space as connection_id/is_custom
        above - a preset's CONFIGURED_DBS "id", or a saved custom
        connection's compute_connection_key() value) are the set of
        connections a question may ever be routed to, per the multi-
        database question-answering feature - a separate, broader concept
        from connection_id/is_custom, which now specifically means "the
        primary connection" (the first entry, in stable display order, of
        this set) rather than "the one connection in use". A session that
        has never explicitly saved these two fields has them lazily
        derived from its existing connection_id/is_custom on every read
        (see _lazy_derive_in_scope) rather than migrated/rewritten
        proactively - so an existing session's current connection becomes
        its sole initially-in-scope entry for free. Empty lists (for a
        brand-new session with connection_id == "") mean "nothing
        explicitly configured yet"; db.py's resolution layer treats that
        the same way resolve_active_descriptor treats a blank
        connection_id - falling back to the app default connection.

        "in_scope_mode" ("single" or "all", defaulting to "single" for a
        session that's never explicitly saved it) is the connection
        picker's binary choice (see webClient/client.js's
        renderDbRadioButtons()): "single" means in_scope_preset_ids/
        in_scope_custom_connection_keys above are the actual in-scope set,
        exactly as described above; "all" means db.py's
        resolve_in_scope_descriptors ignores those two lists entirely and
        instead resolves EVERY currently-configured preset plus every one
        of this user's saved custom connections, fresh, on every request -
        a dynamic set that automatically includes a connection added after
        this was saved, not a list frozen at Save time. That's the whole
        reason this is a separate field rather than just a third possible
        shape for in_scope_preset_ids/in_scope_custom_connection_keys.

        "theme" ("dark" or "light", defaulting to "" - "nothing explicitly
        saved yet") is the Preferences modal's color-scheme choice,
        persisted per session/user like every other field here (see
        get_current_user_identity) rather than only in the browser's
        localStorage. Same blank-means-unset convention as llm_provider/
        llm_model, not auto_sql_execute's baked-in-default one: a blank
        value means the client's own existing default/localStorage value
        keeps applying (see client.js's getCurrentTheme(), which already
        defaults to "dark") rather than this layer forcing a particular
        theme on a session that never explicitly chose one."""

    @abstractmethod
    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None,
                     llm_provider=None, llm_model=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, theme=None):
        """Persists the active connection reference (connection_id,
        is_custom), auto_sql_execute flag, llm_provider/llm_model
        selection, in-scope connection set, in-scope mode, and/or theme
        for a user/session id. Only the fields passed (not None) are
        changed - the others are left as-is. Pass connection_id="" (not
        None) to explicitly clear it, e.g. when switching to a fresh/
        default connection - same not-None-means-"change this" convention
        is_custom/llm_provider/llm_model/in_scope_mode/theme already use.
        in_scope_preset_ids/in_scope_custom_connection_keys follow the
        same convention: pass [] (not None) to explicitly clear one to
        empty, None to leave it untouched - callers that mean to update
        the in-scope set always pass both together (see config_routes.py),
        since a partial update would leave the two lists describing an
        inconsistent set."""

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
        """Returns (rows, daily_stats, total_count) for a user. rows is
        capped at TRANSLATION_HISTORY_LIST_LIMIT, sorted newest-first;
        daily_stats and total_count are always computed over the user's
        COMPLETE history, uncapped, so the history popup's aggregated
        stats tab stays accurate even once the translations list itself
        has been truncated."""

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
                        llm_provider TEXT NOT NULL DEFAULT '',
                        llm_model TEXT NOT NULL DEFAULT '',
                        in_scope_preset_ids TEXT,
                        in_scope_custom_connection_keys TEXT,
                        in_scope_mode TEXT,
                        theme TEXT NOT NULL DEFAULT '',
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
                # Migration: existing DBs created before llm_provider/llm_model
                # existed. Both default to '' ("nothing explicitly selected
                # yet", same convention connection_id already uses below) -
                # every pre-existing row predates per-user model selection, and
                # get_session()/translate_query() already treat a blank value
                # as "fall back to the env-configured default", so a plain
                # ALTER (no data backfill needed, unlike connection_id's own
                # migration further down) is sufficient here.
                if "llm_provider" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN llm_provider TEXT NOT NULL DEFAULT '';"
                    )
                if "llm_model" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN llm_model TEXT NOT NULL DEFAULT '';"
                    )
                # Migration: existing DBs created before the in-scope-
                # connections feature. NULL (not '[]') is the default and
                # stays meaningfully different from an explicit '[]' -
                # get_session() below treats NULL as "never explicitly
                # saved, lazily derive from connection_id/is_custom" and an
                # explicit '[]' as "explicitly saved as empty" (see
                # _lazy_derive_in_scope / StateStore.set_session's
                # docstring for why an explicit empty save is otherwise
                # rejected before it ever reaches here - config_routes.py
                # requires at least one in-scope connection).
                if "in_scope_preset_ids" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN in_scope_preset_ids TEXT;"
                    )
                if "in_scope_custom_connection_keys" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN in_scope_custom_connection_keys TEXT;"
                    )
                # Migration: existing DBs created before the binary
                # single/all in-scope-mode choice existed (see
                # get_session's docstring on in_scope_mode). NULL is the
                # default and means "single" (get_session() below), same
                # "never explicitly saved" convention in_scope_preset_ids/
                # in_scope_custom_connection_keys already use above.
                if "in_scope_mode" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN in_scope_mode TEXT;"
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
                            llm_provider TEXT NOT NULL DEFAULT '',
                            llm_model TEXT NOT NULL DEFAULT '',
                            in_scope_preset_ids TEXT,
                            in_scope_custom_connection_keys TEXT,
                            in_scope_mode TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                    """)
                    cursor.execute("PRAGMA table_info(sessions_old);")
                    old_columns = {column[1] for column in cursor.fetchall()}
                    old_has_custom_key = "custom_connection_key" in old_columns
                    old_has_url = "database_url" in old_columns
                    # llm_provider/llm_model are always present on sessions_old
                    # by this point (the migration guards above already ALTER
                    # them onto "sessions" before this rebuild ever runs) -
                    # selected defensively via old_columns anyway, matching
                    # custom_connection_key/database_url's own
                    # already-established defensive pattern just above, in
                    # case this rebuild path is ever reordered ahead of those
                    # guards in the future. Same reasoning for
                    # in_scope_preset_ids/in_scope_custom_connection_keys/
                    # in_scope_mode, added by this same guard mechanism just
                    # before this rebuild - a legacy DB migrating for the
                    # first time after one of these features shipped never
                    # has them yet on sessions_old, so they fall back to NULL
                    # (lazy-derived / "single" on next read, same as any
                    # other session).
                    old_has_llm_provider = "llm_provider" in old_columns
                    old_has_llm_model = "llm_model" in old_columns
                    old_has_in_scope_presets = "in_scope_preset_ids" in old_columns
                    old_has_in_scope_custom = "in_scope_custom_connection_keys" in old_columns
                    old_has_in_scope_mode = "in_scope_mode" in old_columns
                    select_cols = "session_id, auto_sql_execute, is_custom"
                    select_cols += ", custom_connection_key" if old_has_custom_key else ", NULL"
                    select_cols += ", database_url" if old_has_url else ", NULL"
                    select_cols += ", llm_provider" if old_has_llm_provider else ", ''"
                    select_cols += ", llm_model" if old_has_llm_model else ", ''"
                    select_cols += ", in_scope_preset_ids" if old_has_in_scope_presets else ", NULL"
                    select_cols += ", in_scope_custom_connection_keys" if old_has_in_scope_custom else ", NULL"
                    select_cols += ", in_scope_mode" if old_has_in_scope_mode else ", NULL"
                    select_cols += ", updated_at" if "updated_at" in old_columns else ", CURRENT_TIMESTAMP"
                    cursor.execute(f"SELECT {select_cols} FROM sessions_old;")
                    for (old_session_id, old_auto_exec, old_is_custom,
                         old_custom_key, old_url, old_llm_provider, old_llm_model,
                         old_in_scope_presets, old_in_scope_custom, old_in_scope_mode,
                         old_updated_at) in cursor.fetchall():
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
                                (session_id, auto_sql_execute, is_custom, connection_id,
                                 llm_provider, llm_model, in_scope_preset_ids,
                                 in_scope_custom_connection_keys, in_scope_mode, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            old_session_id, old_auto_exec, old_is_custom,
                            new_connection_id, old_llm_provider or '', old_llm_model or '',
                            old_in_scope_presets, old_in_scope_custom, old_in_scope_mode,
                            old_updated_at,
                        ))
                    cursor.execute("DROP TABLE sessions_old;")

                # Migration: existing DBs created before the theme preference
                # existed - placed after the connection_id rebuild above (not
                # baked into that rebuild's own CREATE TABLE) so it correctly
                # covers both cases with one check: a legacy pre-connection_id
                # DB that just went through the rebuild (whose freshly-created
                # table above predates this field too) and a DB that already
                # had connection_id and skipped the rebuild entirely. Defaults
                # to '' - "nothing explicitly saved yet" - same convention
                # llm_provider/llm_model already use, not auto_sql_execute's
                # baked-in-default one (see get_session's docstring).
                if "theme" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN theme TEXT NOT NULL DEFAULT '';"
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
                        connection_key TEXT NOT NULL DEFAULT '',
                        database_name TEXT NOT NULL,
                        database_url TEXT,
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

                # Migration: existing DBs created before BigQuery/Snowflake/
                # Databricks/Oracle/Redshift/MSSQL/Sheets custom connections
                # stopped carrying a synthetic, made-up database_url (see
                # config_routes.py's module docstring - those 7 dialects
                # have no real url of their own, so there's nothing genuine
                # to store here for them any more). NOT NULL made sense back
                # when every row had *something* to put there; now it'd
                # force storing an empty string standing in for "no url",
                # which is exactly the fake value this change is trying to
                # stop persisting. SQLite can't relax a column's NOT NULL in
                # place, so this is the same rebuild-and-copy pattern as the
                # connection_key migration just above - existing rows
                # (including any old synthetic url for the 7 dialects) are
                # carried over completely as-is; nothing is backfilled to
                # NULL retroactively, since re-saving each connection
                # through /api/config is what actually clears it.
                cursor.execute("PRAGMA table_info(db_connections);")
                if any(col[1] == "database_url" and col[3] for col in cursor.fetchall()):
                    cursor.execute("ALTER TABLE db_connections RENAME TO db_connections_old;")
                    cursor.execute("""
                        CREATE TABLE db_connections (
                            user_id TEXT,
                            connection_key TEXT NOT NULL DEFAULT '',
                            database_name TEXT NOT NULL,
                            database_url TEXT,
                            database_type TEXT NOT NULL DEFAULT 'postgres',
                            database_config TEXT,
                            PRIMARY KEY (user_id, connection_key)
                        );
                    """)
                    cursor.execute("""
                        INSERT INTO db_connections
                            (user_id, connection_key, database_name, database_url, database_type, database_config)
                        SELECT user_id, connection_key, database_name, database_url, database_type, database_config
                        FROM db_connections_old;
                    """)
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
                    "SELECT auto_sql_execute, is_custom, connection_id, llm_provider, llm_model, "
                    "in_scope_preset_ids, in_scope_custom_connection_keys, in_scope_mode, theme "
                    "FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                if row:
                    connection_id = row[2] or ""
                    is_custom = bool(row[1])
                    if row[5] is None and row[6] is None:
                        # Never explicitly saved - lazily derive from this
                        # row's own connection_id/is_custom (see
                        # _lazy_derive_in_scope's docstring).
                        in_scope_preset_ids, in_scope_custom_connection_keys = (
                            _lazy_derive_in_scope(connection_id, is_custom)
                        )
                    else:
                        in_scope_preset_ids = _decode_in_scope_list(row[5])
                        in_scope_custom_connection_keys = _decode_in_scope_list(row[6])
                    return {
                        "auto_sql_execute": bool(row[0]),
                        "is_custom": is_custom,
                        "connection_id": connection_id,
                        "llm_provider": row[3] or "",
                        "llm_model": row[4] or "",
                        "in_scope_preset_ids": in_scope_preset_ids,
                        "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                        "in_scope_mode": row[7] or "single",
                        "theme": row[8] or "",
                    }
        except Exception:
            logger.exception("Error fetching session from SQLite")
        return {
            "auto_sql_execute": DEFAULT_AUTO_SQL_EXECUTE,
            "is_custom": False,
            "connection_id": "",
            "llm_provider": "",
            "llm_model": "",
            "in_scope_preset_ids": [],
            "in_scope_custom_connection_keys": [],
            "in_scope_mode": "single",
            "theme": "",
        }

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None,
                     llm_provider=None, llm_model=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, theme=None):
        if (connection_id is None and auto_sql_execute is None and is_custom is None
                and llm_provider is None and llm_model is None
                and in_scope_preset_ids is None and in_scope_custom_connection_keys is None
                and in_scope_mode is None and theme is None):
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
                # (matching the column's own DEFAULT 1), not False. The two
                # in_scope_* columns are left NULL on this initial insert
                # when not being explicitly set here (SQLite's implicit
                # column default for an omitted column), same "never
                # explicitly saved yet" meaning as a brand-new row always
                # had for these two before this INSERT even ran.
                insert_auto_sql_execute = (
                    auto_sql_execute if auto_sql_execute is not None else DEFAULT_AUTO_SQL_EXECUTE
                )
                insert_cols = ["session_id", "auto_sql_execute", "is_custom", "connection_id",
                               "llm_provider", "llm_model"]
                insert_vals = [
                    effective_user,
                    1 if insert_auto_sql_execute else 0,
                    1 if is_custom else 0,
                    connection_id or "",
                    llm_provider or "",
                    llm_model or "",
                ]
                if in_scope_preset_ids is not None:
                    insert_cols.append("in_scope_preset_ids")
                    insert_vals.append(_encode_in_scope_list(in_scope_preset_ids))
                if in_scope_custom_connection_keys is not None:
                    insert_cols.append("in_scope_custom_connection_keys")
                    insert_vals.append(_encode_in_scope_list(in_scope_custom_connection_keys))
                if in_scope_mode is not None:
                    insert_cols.append("in_scope_mode")
                    insert_vals.append(in_scope_mode)
                if theme is not None:
                    insert_cols.append("theme")
                    insert_vals.append(theme)
                placeholders = ", ".join("?" for _ in insert_cols)
                cursor.execute(f"""
                    INSERT INTO sessions ({', '.join(insert_cols)})
                    VALUES ({placeholders})
                    ON CONFLICT(session_id) DO NOTHING;
                """, insert_vals)

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
                if llm_provider is not None:
                    updates.append("llm_provider = ?")
                    params.append(llm_provider)
                if llm_model is not None:
                    updates.append("llm_model = ?")
                    params.append(llm_model)
                if in_scope_preset_ids is not None:
                    updates.append("in_scope_preset_ids = ?")
                    params.append(_encode_in_scope_list(in_scope_preset_ids))
                if in_scope_custom_connection_keys is not None:
                    updates.append("in_scope_custom_connection_keys = ?")
                    params.append(_encode_in_scope_list(in_scope_custom_connection_keys))
                if in_scope_mode is not None:
                    updates.append("in_scope_mode = ?")
                    params.append(in_scope_mode)
                if theme is not None:
                    updates.append("theme = ?")
                    params.append(theme)
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
                        # Gated on name, not url: BigQuery/Snowflake/
                        # Databricks/Oracle/Redshift/MSSQL/Sheets rows have
                        # no real url of their own any more (always "" -
                        # see config_routes.py's module docstring) and are
                        # still real, complete rows that must be persisted.
                        # This function's only caller
                        # (_parse_incoming_custom_databases) already drops
                        # genuinely incomplete rows before they ever get
                        # here and always supplies a name, so gating on it
                        # here is just a last-resort guard against a
                        # malformed row, not the load-bearing completeness
                        # check url used to be.
                        if n:
                            cursor.execute("""
                                INSERT OR REPLACE INTO db_connections
                                    (user_id, connection_key, database_name, database_url, database_type, database_config)
                                VALUES (?, ?, ?, ?, ?, ?);
                            """, (
                            effective_user, key, n or "Custom", u, t,
                            _encrypt_config_to_text(cfg) if cfg else None,
                        ))
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
                    _encrypt_config_to_text(db_config) if db_config else None,
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
                ORDER BY created_at DESC LIMIT ?
            """, (effective_user, TRANSLATION_HISTORY_LIST_LIMIT))
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
            "llm_provider": "",
            "llm_model": "",
            "in_scope_preset_ids": [],
            "in_scope_custom_connection_keys": [],
            "in_scope_mode": "single",
            "theme": "",
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
                    in_scope_preset_ids, in_scope_custom_connection_keys = (
                        _lazy_derive_in_scope(connection_id, old_is_custom)
                    )
                    return {
                        "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                        "is_custom": old_is_custom,
                        "connection_id": connection_id,
                        "llm_provider": data.get("llm_provider") or "",
                        "llm_model": data.get("llm_model") or "",
                        "in_scope_preset_ids": in_scope_preset_ids,
                        "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                        "in_scope_mode": data.get("in_scope_mode") or "single",
                        "theme": data.get("theme") or "",
                    }
                if "in_scope_preset_ids" not in data or "in_scope_custom_connection_keys" not in data:
                    # Never explicitly saved (a session that already had
                    # connection_id but predates this feature) - lazily
                    # derive, same as the legacy-migration branch above,
                    # just without needing a field-scrubbing rewrite since
                    # there's nothing legacy to clean up here.
                    in_scope_preset_ids, in_scope_custom_connection_keys = _lazy_derive_in_scope(
                        data.get("connection_id") or "", bool(data.get("is_custom", False))
                    )
                else:
                    in_scope_preset_ids = list(data.get("in_scope_preset_ids") or [])
                    in_scope_custom_connection_keys = list(data.get("in_scope_custom_connection_keys") or [])
                return {
                    "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                    "is_custom": bool(data.get("is_custom", False)),
                    "connection_id": data.get("connection_id") or "",
                    "llm_provider": data.get("llm_provider") or "",
                    "llm_model": data.get("llm_model") or "",
                    "in_scope_preset_ids": in_scope_preset_ids,
                    "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                    "in_scope_mode": data.get("in_scope_mode") or "single",
                    "theme": data.get("theme") or "",
                }
        except Exception:
            logger.exception("Error fetching session from Firestore")
        return default_session

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None,
                     llm_provider=None, llm_model=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, theme=None):
        if not user_id or (connection_id is None and auto_sql_execute is None and is_custom is None
                            and llm_provider is None and llm_model is None
                            and in_scope_preset_ids is None and in_scope_custom_connection_keys is None
                            and in_scope_mode is None and theme is None):
            return
        update_data = {"updated_at": firestore.SERVER_TIMESTAMP}
        if connection_id is not None:
            update_data["connection_id"] = connection_id
        if auto_sql_execute is not None:
            update_data["auto_sql_execute"] = bool(auto_sql_execute)
        if is_custom is not None:
            update_data["is_custom"] = bool(is_custom)
        if llm_provider is not None:
            update_data["llm_provider"] = llm_provider
        if llm_model is not None:
            update_data["llm_model"] = llm_model
        if in_scope_preset_ids is not None:
            update_data["in_scope_preset_ids"] = list(in_scope_preset_ids)
        if in_scope_custom_connection_keys is not None:
            update_data["in_scope_custom_connection_keys"] = list(in_scope_custom_connection_keys)
        if in_scope_mode is not None:
            update_data["in_scope_mode"] = in_scope_mode
        if theme is not None:
            update_data["theme"] = theme
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
                # Gated on database_name, not database_url: BigQuery/
                # Snowflake/Databricks/Oracle/Redshift/MSSQL/Sheets rows
                # always have "" for database_url now (no real url of
                # their own - see config_routes.py's module docstring),
                # but are still real, complete rows that must be returned.
                # database_name is set on every row this class's
                # set_db_connections ever writes, so it's an equally
                # reliable "is this a real doc, not something malformed or
                # mid-write" guard, without excluding those 7 dialects.
                if data and data.get("database_name"):
                    config = _decrypt_firestore_config(data.get("database_config"))
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
                        # None (not "") when there's no real url - see
                        # config_routes.py's module docstring for which 7
                        # dialects that's always true for. data.get(...)
                        # already returns None on its own when the field is
                        # absent/null, so this default only matters for an
                        # old doc written before "" stopped being stored.
                        "url": data.get("database_url") or None,
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
                    # Gated on name, not url - see the matching SQLite
                    # comment above for why.
                    if n:
                        doc_id = f"{effective_user}_{key}"
                        self.client.collection("db_connections").document(doc_id).set({
                            "user_id": effective_user,
                            "connection_key": key,
                            "database_name": n or "Custom",
                            "database_type": t,
                            "database_url": u,
                            "database_config": _config_value_to_store(cfg),
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
                "database_config": _config_value_to_store(db_config),
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
            .limit(TRANSLATION_HISTORY_LIST_LIMIT)
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