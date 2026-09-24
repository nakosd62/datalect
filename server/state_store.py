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

# The three "Bring Your Own Key" provider names (Preferences dialog) - kept
# as a plain literal here rather than imported from translate_routes.py's
# _LLM_PROVIDERS, which would be a circular import (translate_routes.py
# already imports the `state_store` singleton from app_config.py; this
# module can never import anything back from translate_routes.py). Same
# "deferred/duplicated rather than circularly imported" reasoning already
# used for CONFIGURED_DBS above - just a plain module-level constant this
# time since, unlike CONFIGURED_DBS, this list never changes at runtime.
LLM_BYOK_PROVIDER_NAMES = ("google", "anthropic", "openai")


def _decode_byok_keys(raw_text):
    """Decodes a stored llm_byok_keys column/field value into the raw
    {"google": "<key>", ...} dict (only ever containing providers that
    currently have a non-empty key saved) - reuses _loads_config's Fernet-
    decrypt-then-JSON-parse logic as-is (see that function's docstring),
    since an API key deserves exactly the same encryption-at-rest treatment
    as database_config's credentials and the stored shape (an opaque
    encrypted blob of a plain JSON dict) is identical either way. Never
    raises - degrades to {} same as _loads_config does for anything
    corrupt/foreign. This is the ONLY function in this module that returns
    the raw key values - see get_llm_byok_key/get_session's docstrings for
    why every other read path only ever exposes booleans."""
    return _loads_config(raw_text)


def _encode_byok_keys(keys_dict):
    """Inverse of _decode_byok_keys for SQLite's TEXT column - reuses
    _encrypt_config_to_text as-is. `keys_dict` should already have empty-
    string entries removed (see _merge_byok_keys) - storing an empty string
    would be indistinguishable from "no key saved" on next read anyway,
    since get_llm_byok_key treats a missing/falsy entry as None either
    way, so there's no reason to keep it in the persisted blob."""
    return _encrypt_config_to_text(keys_dict)


def _merge_byok_keys(existing_dict, updates_dict):
    """Applies a set_session()-style llm_byok_keys update ({provider: new
    value, ...} - only the provider(s) actually being changed) onto an
    existing decoded keys dict, returning the new dict to persist. Per
    LLM_BYOK_PROVIDER_NAMES entry present in `updates_dict`: a non-empty
    string sets/replaces it, an empty string ("" - an explicit clear, not
    the provider being absent from updates_dict at all) removes it
    entirely from the result. A provider not mentioned in updates_dict is
    carried over from existing_dict completely unchanged - see
    StateStore.set_session's docstring for the full contract."""
    merged = dict(existing_dict or {})
    for provider_name, new_value in (updates_dict or {}).items():
        if provider_name not in LLM_BYOK_PROVIDER_NAMES:
            continue
        if new_value:
            merged[provider_name] = new_value
        else:
            merged.pop(provider_name, None)
    return merged


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


# --- Persisted chat/turn-navigation history (client.js's chatStoresByBucket) -
#
# Distinct from the "translations" table/collection (see record_translation
# above): that's an append-only, write-only AUDIT LOG (one row per NL->SQL
# call) - it used to be read back by a /api/history endpoint for the
# History modal's stats/list and clearable via "Purge Translations", but
# both were removed as dead code once the modal was redesigned around THIS
# module's data instead (see chat_history_routes.py's module docstring).
# This is the actual CONVERSATION state - the back/forward-navigable turns, results,
# and summaries client.js keeps per (identity, connection) "bucket" - which
# used to live only in an in-memory Map and vanish on every page reload or
# server restart. One row/doc per (user_id, bucket_key); "bucket_key" is
# exactly client.js's own bucketKeySuffix (e.g. "all", "preset:<id>",
# "custom:<key>", "custom-adhoc:<url>") - NOT prefixed with identity, since
# user_id is already its own separate partition here, same as every other
# per-user table in this module.
#
# Tags every stored row/doc with the shape version of the payload it was
# written under (currently always CHAT_HISTORY_SCHEMA_VERSION), so a future
# change to what a "turn" looks like can tell an old row apart from a new
# one rather than guessing from its contents. get_chat_history's read path
# (_decode_chat_turns) treats a row from a NEWER version than this build
# understands - or a payload that fails to parse/isn't a list at all - as
# "silently unavailable", never a hard error: one corrupt/foreign bucket
# should never take down every other bucket a user has, and a version bump
# rolled out to only some replicas/processes shouldn't crash the others.
CHAT_HISTORY_SCHEMA_VERSION = 1


def _decode_chat_turns(value, schema_version):
    """Best-effort decode of one persisted chat-history bucket's turn list.
    `value` is SQLite's TEXT column content (a JSON string, since a SQLite
    column can't hold a native list) or Firestore's field value (already a
    native list - Firestore has no reason to double-encode it the way
    SQLite must); this transparently handles either shape, same "one
    decode path for both backends" pattern _loads_config already
    established for database_config. Returns None (never raises) if
    `schema_version` is newer than CHAT_HISTORY_SCHEMA_VERSION, or the
    value is missing/corrupt/not ultimately a list - the caller
    (get_chat_history) treats None as "omit this one bucket", not as a
    reason to fail the whole call."""
    if schema_version and schema_version > CHAT_HISTORY_SCHEMA_VERSION:
        logger.warning(
            "Ignoring a chat history bucket with schema_version=%r - this "
            "build only understands up to %d.",
            schema_version, CHAT_HISTORY_SCHEMA_VERSION,
        )
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            logger.warning("Failed to parse stored chat history payload JSON; ignoring it.")
            return None
    return value if isinstance(value, list) else None


class StateStore(ABC):
    """Backend-agnostic persistence for sessions, saved DB connections, and
    translation history/stats. Deliberately holds no notion of "the default
    connection" itself (there used to be a default_conn constructor param
    for exactly that) - a session only ever stores an identity reference
    (connection_id/is_custom, see get_session's docstring), never a
    connection's actual details, so there's nothing here that would need a
    fallback URL to seed a blank row with. db.py's resolve_active_descriptor
    is what applies DEFAULT_DESCRIPTOR (imported directly from
    app_config.py - normally derived from DEFAULT_CONN, or overridden via
    the DATABASE_DEFAULT env var, see that module's own comment) when a
    session's connection_id is blank."""

    @abstractmethod
    def init(self):
        """One-time setup (schema creation, migrations). Safe to call on every startup."""

    @abstractmethod
    def get_session(self, user_id):
        """Returns {"auto_sql_execute", "is_custom", "connection_id",
        "llm_provider", "llm_model", "llm_byok_key_set", "in_scope_preset_ids",
        "in_scope_custom_connection_keys", "in_scope_mode", "in_scope_group_id",
        "theme"} for a user/session id - identity only,
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

        "llm_byok_key_set" ({"google": bool, "anthropic": bool, "openai":
        bool} - the "Bring Your Own Key" feature in the Preferences dialog)
        reports, per provider, whether THIS user has saved their own API
        key to use instead of this app's env-configured one - never the key
        itself, same "report whether a credential is saved, never the
        credential" posture get_db_connections' has_custom_credentials
        already uses for BigQuery's service-account key. Getting the actual
        key value (needed only at LLM-call time, server-side, never for an
        API response) is deliberately a SEPARATE method, get_llm_byok_key -
        see its docstring for why this is split out rather than folded into
        this dict behind an include_credentials-style flag the way
        get_db_connections does it: unlike that method (called from exactly
        one place, config_routes.py, which always knows whether it wants
        credentials), get_session is called from many places, several of
        which build API responses directly from its return value - keeping
        the raw key out of this dict's shape entirely means no call site
        can accidentally leak it by omitting a flag, rather than relying on
        every caller remembering to pass include_credentials=False.

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

        "in_scope_mode" ("single" or "group", defaulting to "single" for a
        session that's never explicitly saved it) is the connection
        picker's binary choice (see webClient/client.js's
        renderDbRadioButtons()): "single" means in_scope_preset_ids/
        in_scope_custom_connection_keys above are the actual in-scope set,
        exactly as described above; "group" means db.py's
        resolve_in_scope_descriptors ignores those two lists entirely and
        instead resolves "in_scope_group_id"'s own fixed "dataset_list"
        (app_config.py's CONFIGURED_DB_GROUPS - see its own "DATASET
        GROUPS" comment), fresh, on every request - so a presets-file
        change to that group's membership is immediately reflected, not a
        list frozen at Save time. That's the whole reason this is a
        separate field rather than just a third possible shape for
        in_scope_preset_ids/in_scope_custom_connection_keys.

        "in_scope_group_id" (defaults to "" - "no group selected", same
        blank-means-unset convention connection_id uses) is which
        DATABASE_PRESETS_FILE dataset_group entry's "id" is active when
        in_scope_mode == "group"; meaningless (ignored) in "single" mode,
        the same way in_scope_preset_ids/in_scope_custom_connection_keys
        are ignored in "group" mode. A group_id that no longer resolves to
        anything (the group was removed/renamed from the presets file
        since this was saved) is handled the same lenient way a stale
        connection_id is - see db.py's _resolve_group_configured_descriptors.

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
                     llm_provider=None, llm_model=None, llm_byok_keys=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, in_scope_group_id=None, theme=None):
        """Persists the active connection reference (connection_id,
        is_custom), auto_sql_execute flag, llm_provider/llm_model
        selection, Bring-Your-Own-Key values, in-scope connection set,
        in-scope mode, in-scope group id, and/or theme for a user/session
        id. Only the fields passed (not None) are changed - the others are
        left as-is. Pass connection_id="" (not None) to explicitly clear
        it, e.g. when switching to a fresh/default connection - same
        not-None-means-"change this" convention is_custom/llm_provider/
        llm_model/in_scope_mode/in_scope_group_id/theme already use.
        in_scope_preset_ids/in_scope_custom_connection_keys follow the same
        convention: pass [] (not None) to explicitly clear one to empty,
        None to leave it untouched - callers that mean to update the
        in-scope set always pass both together (see config_routes.py),
        since a partial update would leave the two lists describing an
        inconsistent set. Callers that mean to switch a session into
        "group" mode likewise always pass in_scope_mode="group" and
        in_scope_group_id="<id>" together, same reasoning.

        llm_byok_keys is a dict of ONLY the provider(s) this call means to
        change - {"google": "<new key>"} updates just Google's, leaving
        Anthropic's/OpenAI's saved keys (if any) completely untouched, same
        as omitting llm_provider from a call leaves it alone. Within that
        dict, a non-empty string sets/replaces that provider's key; ""
        (explicit empty, not the key being absent from the dict at all)
        clears it - matching connection_id's own not-passed-at-all-vs-
        explicit-"" distinction, just one level deeper since this dict
        holds up to three independently-settable values instead of one.
        Pass llm_byok_keys=None (not {}) to leave every provider's key
        untouched - {} would be a no-op in practice (nothing to iterate)
        but None is the unambiguous "don't even look at this" signal
        matching every other optional param here."""

    @abstractmethod
    def get_llm_byok_key(self, user_id, provider_name):
        """Returns the raw, saved Bring-Your-Own-Key value for `user_id`/
        `provider_name` ("google"/"anthropic"/"openai"), or None if that
        user has no key saved for that provider. Deliberately separate from
        get_session() - see that method's docstring on llm_byok_key_set for
        why - and meant to be called from exactly one place per request:
        wherever translate_routes.py/connection_router.py already resolves
        the active provider/model for an LLM call, right before making it.
        Never call this to build an API response."""

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
    def record_llm_usage(self, user_id, call_type, model, usage, dataset_type=None, dataset_name=None):
        """Logs the token cost of exactly ONE real provider.call() -
        every single LLM call this app ever makes, not just the ones that
        end up producing a SQL translation. This is a SEPARATE ledger from
        record_translation()/the "translations" table above, which serves
        a different purpose (a per-NL->SQL-attempt audit trail, keyed by
        the resolved database and carrying the SQL text itself) and, by
        deliberate design, never logs Phase A/triage calls at all - see
        translate_routes.py's stream_translation, the "Phase A (triage) is
        deliberately NEVER recorded" comment. This table is the opposite:
        purely about LLM cost/usage visibility, so it logs every call -
        triage, sqlgen, AND summary - uniformly, with no notion of
        success/failure or the SQL/text that came out of it.

        `call_type` is one of "triage", "sqlgen", or "summary" - the three
        kinds of LLM call this app makes:
          "triage": connection_router.py's run_triage_call - Call 1 for
            both single-dataset mode and "all databases"/group mode.
          "sqlgen": the actual NL->SQL generation call - either
            stream_translation()'s own inline single-connection Call 2, or
            sql_generation.py's generate_sql_for_connection (Phase B's
            per-connection fan-out in dataset-group mode).
          "summary": summarize_routes.py's shared _summarize_with_retry -
            Phase C's per-turn summarization call, used identically by
            both single-connection mode and "all databases" mode.

        `user_id` is the same already-resolved identity every other method
        on this class takes (auth.py's get_current_user_identity - a real
        signed-in user id, "anonymous:<session_id>" for an anonymous
        visitor, or "global" for a local deployment with no auth) - passed
        through _effective_user() here purely as a defensive fallback,
        exactly like record_translation above, not because callers are
        expected to ever pass something that still needs resolving.

        `usage` is the shared usage_dict every provider's call() returns
        (see llm_providers.py's LlmProvider.call docstring) - always
        carrying "input_tokens"/"output_tokens"/"total_tokens"/
        "thinking_tokens"/"cached_content_tokens", read defensively here
        (missing/None treated as 0) so a provider that can't report one of
        these (e.g. Claude's thinking_tokens) never breaks this logging.
        Called ONCE per successful provider.call() return, regardless of
        what happens to that call's result afterward (a parse failure, a
        language-mismatch retry, etc.) - real tokens were spent either
        way, so this is a strictly more complete cost record than
        "translations" rows ever were, which sometimes log 0 usage for a
        call that failed outright and sometimes skip a row entirely (any
        triage call, by design).

        `dataset_type`/`dataset_name` identify WHAT this call was actually
        about, same (db_type, db_name) shape record_translation's own
        columns already use - db.py's resolve_dataset_identity() resolves
        this pair for a call tied to exactly one connection (sqlgen calls
        always are; triage/summary are too in single-dataset mode), and
        its sibling resolve_group_identity() resolves it for a call that
        spans a whole configured dataset group at once (group-mode
        triage's own multi-candidate call, and "all databases" mode's
        Phase C summary call - dataset_type is then the fixed marker
        "Dataset Group", never a dialect name, since a group can span
        several dialects). Both default to None for a caller with no
        real dataset/group to attribute the call to (e.g. a bare unit
        test) - stored as-is, NULL, rather than coerced to a placeholder
        string, so a genuinely-unknown row stays visibly distinct from one
        that legitimately resolved to some real, human-readable name."""

    @abstractmethod
    def get_chat_history(self, user_id):
        """Returns {"buckets": {bucket_key: [turns...]}, "active_bucket_key":
        str} - every persisted conversation bucket this identity has ever
        saved (see this module's "Persisted chat/turn-navigation history"
        section above for what a bucket_key/turn actually is). A bucket
        whose stored payload is corrupt or from an unrecognized future
        schema version is silently omitted rather than failing the whole
        call (see _decode_chat_turns). "active_bucket_key" is "" if this
        user has never had one explicitly set (see
        set_active_chat_bucket)."""

    @abstractmethod
    def save_chat_bucket(self, user_id, bucket_key, turns):
        """Upserts one bucket's full turn list, tagged with the current
        CHAT_HISTORY_SCHEMA_VERSION. `turns` is already trimmed to
        history_max_turns by the client (createChatHistoryStore's own
        maxEntries cap) - this stores it as-is, opaquely, the same "don't
        interpret the caller's blob" posture database_config/llm_byok_keys
        already use. Deliberately does NOT also mark `bucket_key` active -
        pushing a turn into a bucket that ISN'T the currently active one
        (all-mode's per-database history fan-out) must never disturb which
        bucket the user is actually looking at; see set_active_chat_bucket
        for the one thing that does that."""

    @abstractmethod
    def set_active_chat_bucket(self, user_id, bucket_key):
        """Records which bucket_key is "active" for a user, without
        touching any bucket's own saved turns - called whenever the client
        switches to a (possibly still-empty) bucket, independent of
        whether a turn was ever pushed into it this request. Purely a
        restart-time hint for get_chat_history's "active_bucket_key" -
        which bucket a restart actually reopens on is primarily decided by
        the user's separately-persisted connection/in-scope-mode selection
        (get_session/set_session above), which already recomputes the same
        bucket_key in the common case."""

    # --- Durable schema cache (the ONLY layer schema_cache.py has - see
    # that module's own docstring for why it no longer keeps any
    # process-local in-memory copy alongside this) --------------------
    #
    # Not user-scoped at all, unlike everything else in this class - a
    # schema (re)fetch is keyed purely by db.py's get_conn_identifier(),
    # the same non-sensitive per-connection identifier schema_cache.py
    # already uses as its own keys (see that module's docstring: "a
    # non-sensitive identifier ... never the raw connection string"). A
    # preset's cached schema is the same for every user who queries it,
    # so there's no per-user partition to add here - this is the app's
    # global "what does this connection currently look like"
    # cache, durable purely so it survives a process restart, not a
    # per-user preference the way sessions/db_connections/chat_history
    # are.
    #
    # Deliberately stored in plaintext, same posture the "translations"
    # table/collection already has for nl_prompt/sql_command (see that
    # method's own docstring) - not the "encrypt the whole blob"
    # treatment database_config/llm_byok_keys get. Those two protect
    # actual CREDENTIALS (a saved connection's password, a BigQuery
    # service-account key, an LLM API key); a schema cache entry is
    # DDL/column metadata plus a handful of real sampled column values
    # (see backends/base.py's frequent-value sampling) - business data
    # comparable to what a translated SQL query's own result already is,
    # not a secret that unlocks anything, so it gets the same plaintext
    # treatment already accepted for that.

    @abstractmethod
    def get_cached_schema(self, cache_key):
        """Returns {"schema_text", "cached_at", "overview"} for a durably-
        saved schema cache entry, or None if nothing has ever been saved
        for `cache_key`. "schema_text"/"cached_at" mirror schema_cache.py's
        own get()/get_cached_at() exactly (the same string, and the same
        ISO 8601 UTC timestamp string, passed straight through by
        set_cached_schema below - not recomputed here). "overview" is the
        {"prose", "questions", "generated_at"} dict schema_cache.py's own
        get_overview()/set_overview() already document, or None if no
        overview has ever been durably saved for this key (same "absence
        means nothing to report" convention every other overview lookup in
        this app already uses) - independent of whether schema_text itself
        is present, since the two are written by two separate calls (see
        set_cached_schema/set_cached_schema_overview below).

        This IS schema_cache.py's storage, called on every get() - not a
        read-through layer underneath a faster in-memory cache the way it
        used to be (see that module's own docstring for why a
        process-local in-memory layer was actively wrong on multi-instance
        Cloud Run, not just a missed optimization). The round trip this
        costs is small next to both the live database introspection query
        this cache exists to avoid and the LLM call that dominates a real
        request's latency regardless."""

    @abstractmethod
    def set_cached_schema(self, cache_key, schema_text, cached_at):
        """Durably saves `cache_key`'s schema_text - the write-through
        counterpart to schema_cache.py's own set(), called from that exact
        same place so the in-memory and durable copies never drift apart.
        `cached_at` is the same ISO 8601 UTC timestamp string set() already
        computed for the in-memory copy, passed through rather than
        independently recomputed, so a later get_cached_schema() call
        reports the exact same value regardless of which layer answered
        it. Overwrites whatever schema_text/cached_at was previously saved
        for this key, if anything - there's no history/versioning here,
        matching the in-memory cache's own single-current-entry design.
        Leaves any previously-saved overview for this key completely
        untouched (see set_cached_schema_overview below) - a schema
        refetch and an overview regeneration are two separate calls in
        db.py's prime_schema_cache_with_reason(), exactly as they already
        are for the in-memory cache's own set()/set_overview()."""

    @abstractmethod
    def set_cached_schema_overview(self, cache_key, overview):
        """Durably saves `cache_key`'s freshly generated overview dict -
        the write-through counterpart to schema_cache.py's own
        set_overview(), called from the exact same place
        (db.py's _generate_and_cache_schema_overview(), moments after a
        successful set_cached_schema() call for the same key). Leaves any
        previously-saved schema_text/cached_at for this key untouched."""

    @abstractmethod
    def delete_cached_schema(self, cache_key):
        """Durably deletes `cache_key`'s entire entry (schema_text AND
        overview together, as one unit) - the write-through counterpart to
        schema_cache.py's own invalidate(), called from the exact same
        place (db.py's invalidate_schema_cache()) so a durably-persisted
        copy never outlives the invalidation it's meant to enact - without
        this, a connection whose config just changed would keep serving
        the OLD, now-invalid schema this call was supposed to have erased.
        Safe to call even if nothing was ever saved for this key. Also
        clears any fetch-pending/fetch-error status recorded for this key
        (see mark_schema_fetch_pending/mark_schema_fetch_done below) - an
        invalidated entry has nothing in flight and nothing to report a
        failure about any more."""

    @abstractmethod
    def mark_schema_fetch_pending(self, cache_key):
        """Records that a schema (re)fetch for `cache_key` has just
        started - the write-through counterpart to schema_cache.py's own
        mark_fetch_pending(), called from the exact same place (db.py's
        prime_schema_cache_with_reason(), right before it does any real
        work). Durable (not process-local) for the same reason schema_text
        itself is: the background thread that runs a (re)fetch after a
        config-modal Save runs on whichever instance handled that POST,
        but the client's polling GET (/api/config/schema-fetch-status) can
        land on a different Cloud Run instance - which must see the same
        "yes, still fetching" answer that instance would give itself.
        Never touches fetch_error - a fetch that's merely STARTING says
        nothing about the outcome of the previous attempt; mark_schema_
        fetch_done below is what records that."""

    @abstractmethod
    def mark_schema_fetch_done(self, cache_key, error=None):
        """Records that the fetch mark_schema_fetch_pending() announced for
        `cache_key` has finished - the write-through counterpart to
        schema_cache.py's own mark_fetch_done(). Always clears the pending
        flag; `error` (one of db.py's SCHEMA_FETCH_FAILURE_REASON_*
        strings) records the failure reason if given, or clears any
        previously-recorded one if not (a successful fetch supersedes
        whatever failed before it)."""

    @abstractmethod
    def get_schema_fetch_status(self, cache_key):
        """Returns {"pending": bool, "error": str|None} for `cache_key` -
        "pending" mirrors schema_cache.py's own is_fetch_pending(), "error"
        mirrors get_last_fetch_error() (both False/None for a key that's
        never been fetched at all, same as one whose last fetch already
        finished cleanly - see mark_schema_fetch_done above). A single
        combined read (not two separate methods/round trips) since both
        fields live in the same row/document and webClient's polling loop
        against /api/config/schema-fetch-status wants both on every tick."""

    @abstractmethod
    def list_cached_schema_texts(self):
        """Returns every currently durably-cached {cache_key: schema_text}
        pair (omitting any key with no schema_text saved yet - e.g. a
        fetch is pending but hasn't succeeded). Local-dev debugging only -
        the one caller is config_routes.py's /api/debug/schema-cache route,
        itself gated off entirely on Cloud Run (see that route's own
        docstring) - so this has no latency/cost budget to respect the way
        every other method here does."""


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

                # Separate from "translations" above on purpose - see
                # StateStore.record_llm_usage's docstring for why this is
                # its own table (every LLM call, including triage/summary
                # calls "translations" deliberately never logs, rather than
                # only NL->SQL attempts).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS llm_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT,
                        call_type TEXT,
                        dataset_type TEXT,
                        dataset_name TEXT,
                        model TEXT,
                        input_tokens INTEGER,
                        cached_content_tokens INTEGER,
                        thinking_tokens INTEGER,
                        output_tokens INTEGER,
                        total_tokens INTEGER,
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
                        in_scope_group_id TEXT NOT NULL DEFAULT '',
                        theme TEXT NOT NULL DEFAULT '',
                        llm_byok_keys TEXT,
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
                # Migration: existing DBs created before dataset groups
                # existed (see get_session's docstring on in_scope_group_id).
                # Defaults to '' - "no group selected", meaningless/ignored
                # unless in_scope_mode == 'group' - same blank-means-unset
                # convention connection_id already uses, not in_scope_mode's
                # own NULL-means-"never saved" one, since there's no
                # separate lazy-derivation step for this field the way
                # in_scope_preset_ids/in_scope_custom_connection_keys have.
                if "in_scope_group_id" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN in_scope_group_id TEXT NOT NULL DEFAULT '';"
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
                            in_scope_group_id TEXT NOT NULL DEFAULT '',
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
                    old_has_in_scope_group_id = "in_scope_group_id" in old_columns
                    select_cols = "session_id, auto_sql_execute, is_custom"
                    select_cols += ", custom_connection_key" if old_has_custom_key else ", NULL"
                    select_cols += ", database_url" if old_has_url else ", NULL"
                    select_cols += ", llm_provider" if old_has_llm_provider else ", ''"
                    select_cols += ", llm_model" if old_has_llm_model else ", ''"
                    select_cols += ", in_scope_preset_ids" if old_has_in_scope_presets else ", NULL"
                    select_cols += ", in_scope_custom_connection_keys" if old_has_in_scope_custom else ", NULL"
                    select_cols += ", in_scope_mode" if old_has_in_scope_mode else ", NULL"
                    select_cols += ", in_scope_group_id" if old_has_in_scope_group_id else ", ''"
                    select_cols += ", updated_at" if "updated_at" in old_columns else ", CURRENT_TIMESTAMP"
                    cursor.execute(f"SELECT {select_cols} FROM sessions_old;")
                    for (old_session_id, old_auto_exec, old_is_custom,
                         old_custom_key, old_url, old_llm_provider, old_llm_model,
                         old_in_scope_presets, old_in_scope_custom, old_in_scope_mode,
                         old_in_scope_group_id, old_updated_at) in cursor.fetchall():
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
                                 in_scope_custom_connection_keys, in_scope_mode,
                                 in_scope_group_id, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            old_session_id, old_auto_exec, old_is_custom,
                            new_connection_id, old_llm_provider or '', old_llm_model or '',
                            old_in_scope_presets, old_in_scope_custom, old_in_scope_mode,
                            old_in_scope_group_id or '', old_updated_at,
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
                # Migration: existing DBs created before the "Bring Your Own
                # Key" feature existed. NULL is the default and means "no
                # keys saved for any provider" - same "never explicitly
                # saved" convention in_scope_preset_ids/in_scope_mode
                # already use above, not theme/llm_provider's "saved as
                # blank" one, since the stored value here is a whole
                # encrypted JSON blob (see _encrypt_config_to_text/
                # _loads_config, reused as-is - this is exactly the same
                # "encrypt the whole dict as one opaque blob" shape
                # database_config already uses, for the same reason: an API
                # key is just as much a credential as a saved connection's
                # password) rather than a single scalar column.
                if "llm_byok_keys" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN llm_byok_keys TEXT;"
                    )
                # Migration: existing DBs created before persisted chat
                # history existed (see this module's "Persisted chat/turn-
                # navigation history" section). '' means "never explicitly
                # set" - same convention theme/llm_provider already use -
                # set_active_chat_bucket() is the only thing that ever
                # writes a non-empty value here.
                if "active_chat_bucket_key" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE sessions ADD COLUMN active_chat_bucket_key TEXT NOT NULL DEFAULT '';"
                    )

                # One row per (user_id, bucket_key) - see this module's
                # "Persisted chat/turn-navigation history" section above.
                # "payload" is the turn list, JSON-encoded (a SQLite TEXT
                # column can't hold a native list the way a Firestore field
                # can - see _decode_chat_turns).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history (
                        user_id TEXT NOT NULL,
                        bucket_key TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        schema_version INTEGER NOT NULL DEFAULT 1,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, bucket_key)
                    );
                """)

                # One row per connection - not per (user_id, ...) the way
                # every other table here is (see StateStore's own "Durable
                # schema cache" comment for why: this is a single global
                # cache shared by every user who queries a given
                # connection, not a per-user preference). "overview" is a
                # JSON-encoded {"prose", "questions", "generated_at"} dict
                # (a SQLite TEXT column can't hold a native nested object
                # the way a Firestore field can - same reasoning
                # chat_history's own "payload" column comment gives), NULL
                # until the first successful overview generation for that
                # key - independent of schema_text/cached_at, which are set
                # by a separate call (see set_cached_schema vs.
                # set_cached_schema_overview).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS schema_cache (
                        cache_key TEXT PRIMARY KEY,
                        schema_text TEXT,
                        cached_at TEXT,
                        overview TEXT,
                        fetch_pending INTEGER NOT NULL DEFAULT 0,
                        fetch_error TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Migration: fetch_pending/fetch_error added when
                # schema_cache.py's in-flight/last-error tracking (once a
                # process-local _pending set/_last_error dict, invisible
                # across Cloud Run instances - see schema_cache.py's own
                # module docstring) moved into this same durably-shared
                # table, for the same cross-instance-visibility reason
                # schema_text/cached_at/overview already live here.
                cursor.execute("PRAGMA table_info(schema_cache);")
                schema_cache_columns = [column[1] for column in cursor.fetchall()]
                if "fetch_pending" not in schema_cache_columns:
                    cursor.execute(
                        "ALTER TABLE schema_cache ADD COLUMN fetch_pending INTEGER NOT NULL DEFAULT 0;"
                    )
                if "fetch_error" not in schema_cache_columns:
                    cursor.execute("ALTER TABLE schema_cache ADD COLUMN fetch_error TEXT;")

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
                    "in_scope_preset_ids, in_scope_custom_connection_keys, in_scope_mode, theme, "
                    "llm_byok_keys, in_scope_group_id "
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
                    byok_keys = _decode_byok_keys(row[9])
                    return {
                        "auto_sql_execute": bool(row[0]),
                        "is_custom": is_custom,
                        "connection_id": connection_id,
                        "llm_provider": row[3] or "",
                        "llm_model": row[4] or "",
                        "llm_byok_key_set": {name: bool(byok_keys.get(name)) for name in LLM_BYOK_PROVIDER_NAMES},
                        "in_scope_preset_ids": in_scope_preset_ids,
                        "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                        "in_scope_mode": row[7] or "single",
                        "in_scope_group_id": row[10] or "",
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
            "llm_byok_key_set": {name: False for name in LLM_BYOK_PROVIDER_NAMES},
            "in_scope_preset_ids": [],
            "in_scope_custom_connection_keys": [],
            "in_scope_mode": "single",
            "in_scope_group_id": "",
            "theme": "",
        }

    def get_llm_byok_key(self, user_id, provider_name):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT llm_byok_keys FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                if row:
                    return _decode_byok_keys(row[0]).get(provider_name) or None
        except Exception:
            logger.exception("Error fetching BYOK key from SQLite")
        return None

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None,
                     llm_provider=None, llm_model=None, llm_byok_keys=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, in_scope_group_id=None, theme=None):
        if (connection_id is None and auto_sql_execute is None and is_custom is None
                and llm_provider is None and llm_model is None and llm_byok_keys is None
                and in_scope_preset_ids is None and in_scope_custom_connection_keys is None
                and in_scope_mode is None and in_scope_group_id is None and theme is None):
            return
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # llm_byok_keys is stored as one whole encrypted blob (see
                # _decode_byok_keys/_merge_byok_keys) but updated per-
                # provider - unlike every other field here, applying it
                # needs a read-modify-write: fetch whatever's already
                # saved (a brand-new row that doesn't exist yet reads back
                # as {}, which _merge_byok_keys handles the same as any
                # other existing dict), merge this call's changes into it,
                # then persist the merged result below through the exact
                # same insert_cols/updates plumbing every other field uses.
                new_byok_text = None
                if llm_byok_keys is not None:
                    cursor.execute(
                        "SELECT llm_byok_keys FROM sessions WHERE session_id = ?",
                        (effective_user,),
                    )
                    existing_row = cursor.fetchone()
                    existing_byok_keys = _decode_byok_keys(existing_row[0] if existing_row else None)
                    new_byok_text = _encode_byok_keys(_merge_byok_keys(existing_byok_keys, llm_byok_keys))
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
                if in_scope_group_id is not None:
                    insert_cols.append("in_scope_group_id")
                    insert_vals.append(in_scope_group_id)
                if theme is not None:
                    insert_cols.append("theme")
                    insert_vals.append(theme)
                if new_byok_text is not None:
                    insert_cols.append("llm_byok_keys")
                    insert_vals.append(new_byok_text)
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
                if in_scope_group_id is not None:
                    updates.append("in_scope_group_id = ?")
                    params.append(in_scope_group_id)
                if theme is not None:
                    updates.append("theme = ?")
                    params.append(theme)
                if new_byok_text is not None:
                    updates.append("llm_byok_keys = ?")
                    params.append(new_byok_text)
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

    def record_llm_usage(self, user_id, call_type, model, usage, dataset_type=None, dataset_name=None):
        effective_user = _effective_user(user_id)
        usage = usage or {}
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO llm_usage (
                        user_id, call_type, dataset_type, dataset_name, model, input_tokens,
                        cached_content_tokens, thinking_tokens, output_tokens, total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    effective_user, call_type, dataset_type, dataset_name, model,
                    usage.get("input_tokens") or 0,
                    usage.get("cached_content_tokens") or 0,
                    usage.get("thinking_tokens") or 0,
                    usage.get("output_tokens") or 0,
                    usage.get("total_tokens") or 0,
                ))
                conn.commit()
        except Exception:
            logger.exception("Error recording LLM usage")

    def get_chat_history(self, user_id):
        effective_user = _effective_user(user_id)
        buckets = {}
        active_bucket_key = ""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT bucket_key, payload, schema_version FROM chat_history WHERE user_id = ?",
                    (effective_user,),
                )
                for bucket_key, raw_payload, schema_version in cursor.fetchall():
                    turns = _decode_chat_turns(raw_payload, schema_version)
                    if turns is not None:
                        buckets[bucket_key] = turns
                cursor.execute(
                    "SELECT active_chat_bucket_key FROM sessions WHERE session_id = ?",
                    (effective_user,),
                )
                row = cursor.fetchone()
                active_bucket_key = (row[0] or "") if row else ""
        except Exception:
            logger.exception("Error fetching chat history from SQLite")
            return {"buckets": {}, "active_bucket_key": ""}
        return {"buckets": buckets, "active_bucket_key": active_bucket_key}

    def save_chat_bucket(self, user_id, bucket_key, turns):
        if not bucket_key:
            return
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO chat_history (user_id, bucket_key, payload, schema_version, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, bucket_key) DO UPDATE SET
                        payload = excluded.payload,
                        schema_version = excluded.schema_version,
                        updated_at = CURRENT_TIMESTAMP;
                """, (effective_user, bucket_key, json.dumps(turns or []), CHAT_HISTORY_SCHEMA_VERSION))
                conn.commit()
        except Exception:
            logger.exception("Error saving chat history bucket to SQLite")

    def set_active_chat_bucket(self, user_id, bucket_key):
        effective_user = _effective_user(user_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # Same "ensure a row exists, defaults for everything else"
                # insert-then-conflict-update shape set_session() uses -
                # this can be the very first thing ever written for a
                # brand-new session/user (e.g. a fresh anonymous visitor
                # who switches connections before their first translate),
                # and every other sessions column already has a schema
                # DEFAULT to fall back on.
                cursor.execute("""
                    INSERT INTO sessions (session_id, active_chat_bucket_key)
                    VALUES (?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        active_chat_bucket_key = excluded.active_chat_bucket_key,
                        updated_at = CURRENT_TIMESTAMP;
                """, (effective_user, bucket_key or ""))
                conn.commit()
        except Exception:
            logger.exception("Error saving active chat bucket to SQLite")

    def get_cached_schema(self, cache_key):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT schema_text, cached_at, overview FROM schema_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
        except Exception:
            logger.exception("Error fetching cached schema from SQLite")
            return None
        if row is None:
            return None
        schema_text, cached_at, raw_overview = row
        overview = None
        if raw_overview:
            try:
                overview = json.loads(raw_overview)
            except Exception:
                # A corrupt/unparseable saved overview should never take
                # down the whole read - same "silently unavailable, never a
                # hard error" posture _decode_chat_turns already uses for a
                # bad chat_history payload.
                overview = None
        return {"schema_text": schema_text, "cached_at": cached_at, "overview": overview}

    def set_cached_schema(self, cache_key, schema_text, cached_at):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # ON CONFLICT only touches schema_text/cached_at, never
                # `overview` - a schema refetch must never clobber a
                # previously-saved overview for this same key (see
                # set_cached_schema_overview below - a separate call from a
                # separate, best-effort LLM step).
                cursor.execute("""
                    INSERT INTO schema_cache (cache_key, schema_text, cached_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        schema_text = excluded.schema_text,
                        cached_at = excluded.cached_at,
                        updated_at = CURRENT_TIMESTAMP;
                """, (cache_key, schema_text, cached_at))
                conn.commit()
        except Exception:
            logger.exception("Error saving cached schema to SQLite")

    def set_cached_schema_overview(self, cache_key, overview):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # ON CONFLICT only touches `overview`, never schema_text/
                # cached_at - see set_cached_schema's own comment above for
                # why these two are kept independent.
                cursor.execute("""
                    INSERT INTO schema_cache (cache_key, overview, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        overview = excluded.overview,
                        updated_at = CURRENT_TIMESTAMP;
                """, (cache_key, json.dumps(overview)))
                conn.commit()
        except Exception:
            logger.exception("Error saving cached schema overview to SQLite")

    def delete_cached_schema(self, cache_key):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM schema_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
        except Exception:
            logger.exception("Error deleting cached schema from SQLite")

    def mark_schema_fetch_pending(self, cache_key):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                # Never touches fetch_error - see this method's own
                # docstring in StateStore above.
                cursor.execute("""
                    INSERT INTO schema_cache (cache_key, fetch_pending, updated_at)
                    VALUES (?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        fetch_pending = 1,
                        updated_at = CURRENT_TIMESTAMP;
                """, (cache_key,))
                conn.commit()
        except Exception:
            logger.exception("Error marking schema fetch pending in SQLite")

    def mark_schema_fetch_done(self, cache_key, error=None):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO schema_cache (cache_key, fetch_pending, fetch_error, updated_at)
                    VALUES (?, 0, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        fetch_pending = 0,
                        fetch_error = excluded.fetch_error,
                        updated_at = CURRENT_TIMESTAMP;
                """, (cache_key, error))
                conn.commit()
        except Exception:
            logger.exception("Error marking schema fetch done in SQLite")

    def get_schema_fetch_status(self, cache_key):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT fetch_pending, fetch_error FROM schema_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
        except Exception:
            logger.exception("Error fetching schema fetch status from SQLite")
            return {"pending": False, "error": None}
        if row is None:
            return {"pending": False, "error": None}
        pending, error = row
        return {"pending": bool(pending), "error": error}

    def list_cached_schema_texts(self):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT cache_key, schema_text FROM schema_cache WHERE schema_text IS NOT NULL;")
                rows = cursor.fetchall()
        except Exception:
            logger.exception("Error listing cached schemas from SQLite")
            return {}
        return {cache_key: schema_text for cache_key, schema_text in rows}


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
            "llm_byok_key_set": {name: False for name in LLM_BYOK_PROVIDER_NAMES},
            "in_scope_preset_ids": [],
            "in_scope_custom_connection_keys": [],
            "in_scope_mode": "single",
            "in_scope_group_id": "",
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
                    legacy_byok_keys = _decrypt_firestore_config(data.get("llm_byok_keys"))
                    return {
                        "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                        "is_custom": old_is_custom,
                        "connection_id": connection_id,
                        "llm_provider": data.get("llm_provider") or "",
                        "llm_model": data.get("llm_model") or "",
                        "llm_byok_key_set": {
                            name: bool(legacy_byok_keys.get(name)) for name in LLM_BYOK_PROVIDER_NAMES
                        },
                        "in_scope_preset_ids": in_scope_preset_ids,
                        "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                        "in_scope_mode": data.get("in_scope_mode") or "single",
                        "in_scope_group_id": data.get("in_scope_group_id") or "",
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
                byok_keys = _decrypt_firestore_config(data.get("llm_byok_keys"))
                return {
                    "auto_sql_execute": bool(data.get("auto_sql_execute", DEFAULT_AUTO_SQL_EXECUTE)),
                    "is_custom": bool(data.get("is_custom", False)),
                    "connection_id": data.get("connection_id") or "",
                    "llm_provider": data.get("llm_provider") or "",
                    "llm_model": data.get("llm_model") or "",
                    "llm_byok_key_set": {name: bool(byok_keys.get(name)) for name in LLM_BYOK_PROVIDER_NAMES},
                    "in_scope_preset_ids": in_scope_preset_ids,
                    "in_scope_custom_connection_keys": in_scope_custom_connection_keys,
                    "in_scope_mode": data.get("in_scope_mode") or "single",
                    "in_scope_group_id": data.get("in_scope_group_id") or "",
                    "theme": data.get("theme") or "",
                }
        except Exception:
            logger.exception("Error fetching session from Firestore")
        return default_session

    def get_llm_byok_key(self, user_id, provider_name):
        if not user_id:
            return None
        try:
            doc = self.client.collection("sessions").document(user_id).get()
            if doc.exists:
                data = doc.to_dict() or {}
                byok_keys = _decrypt_firestore_config(data.get("llm_byok_keys"))
                return byok_keys.get(provider_name) or None
        except Exception:
            logger.exception("Error fetching BYOK key from Firestore")
        return None

    def set_session(self, user_id, connection_id=None, auto_sql_execute=None, is_custom=None,
                     llm_provider=None, llm_model=None, llm_byok_keys=None,
                     in_scope_preset_ids=None, in_scope_custom_connection_keys=None,
                     in_scope_mode=None, in_scope_group_id=None, theme=None):
        if not user_id or (connection_id is None and auto_sql_execute is None and is_custom is None
                            and llm_provider is None and llm_model is None and llm_byok_keys is None
                            and in_scope_preset_ids is None and in_scope_custom_connection_keys is None
                            and in_scope_mode is None and in_scope_group_id is None and theme is None):
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
        if llm_byok_keys is not None:
            # Same read-modify-write reasoning as SqliteStateStore.set_session
            # - this is one whole-document field covering all three
            # providers, but the caller only ever means to change the
            # provider(s) present in llm_byok_keys (see _merge_byok_keys).
            existing_byok_keys = {}
            try:
                existing_doc = self.client.collection("sessions").document(user_id).get()
                if existing_doc.exists:
                    existing_byok_keys = _decrypt_firestore_config(
                        (existing_doc.to_dict() or {}).get("llm_byok_keys")
                    )
            except Exception:
                logger.exception("Error reading existing BYOK keys from Firestore before merge")
            merged_byok_keys = _merge_byok_keys(existing_byok_keys, llm_byok_keys)
            update_data["llm_byok_keys"] = _config_value_to_store(merged_byok_keys)
        if in_scope_preset_ids is not None:
            update_data["in_scope_preset_ids"] = list(in_scope_preset_ids)
        if in_scope_custom_connection_keys is not None:
            update_data["in_scope_custom_connection_keys"] = list(in_scope_custom_connection_keys)
        if in_scope_mode is not None:
            update_data["in_scope_mode"] = in_scope_mode
        if in_scope_group_id is not None:
            update_data["in_scope_group_id"] = in_scope_group_id
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

    def record_llm_usage(self, user_id, call_type, model, usage, dataset_type=None, dataset_name=None):
        effective_user = _effective_user(user_id)
        usage = usage or {}
        try:
            self.client.collection("llm_usage").add({
                "user_id": effective_user,
                "call_type": call_type,
                "dataset_type": dataset_type,
                "dataset_name": dataset_name,
                "model": model,
                "input_tokens": usage.get("input_tokens") or 0,
                "cached_content_tokens": usage.get("cached_content_tokens") or 0,
                "thinking_tokens": usage.get("thinking_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            logger.exception("Error recording LLM usage in Firestore")

    def get_chat_history(self, user_id):
        effective_user = _effective_user(user_id)
        buckets = {}
        try:
            docs = self.client.collection("chat_history").where("user_id", "==", effective_user).stream()
            for doc in docs:
                d = doc.to_dict() or {}
                bucket_key = d.get("bucket_key")
                turns = _decode_chat_turns(d.get("payload"), d.get("schema_version", 1))
                if bucket_key and turns is not None:
                    buckets[bucket_key] = turns
        except Exception:
            logger.exception("Error fetching chat history from Firestore")
            return {"buckets": {}, "active_bucket_key": ""}
        active_bucket_key = ""
        try:
            doc = self.client.collection("sessions").document(effective_user).get()
            if doc.exists:
                active_bucket_key = (doc.to_dict() or {}).get("active_chat_bucket_key") or ""
        except Exception:
            logger.exception("Error fetching active chat bucket from Firestore")
        return {"buckets": buckets, "active_bucket_key": active_bucket_key}

    def save_chat_bucket(self, user_id, bucket_key, turns):
        if not bucket_key:
            return
        effective_user = _effective_user(user_id)
        try:
            # Composite doc id, same "flatten (user_id, key) into one doc
            # id" pattern set_db_connections() already uses for
            # db_connections - a plain field-based document, not a
            # subcollection, so a single get_chat_history() query (below)
            # can list every bucket for this user with one where() filter.
            doc_id = f"{effective_user}_{bucket_key}"
            self.client.collection("chat_history").document(doc_id).set({
                "user_id": effective_user,
                "bucket_key": bucket_key,
                # Stored as a native list, unlike SQLite's TEXT column -
                # Firestore documents hold nested lists/maps directly, so
                # there's no reason to double-encode this as a JSON string
                # the way _encrypt_config_to_text does for a column-bound
                # backend (and this data isn't a credential, so it gets no
                # encryption-at-rest treatment either - same plaintext
                # posture the "translations" collection already has for
                # nl_prompt/sql_command).
                "payload": turns or [],
                "schema_version": CHAT_HISTORY_SCHEMA_VERSION,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            logger.exception("Error saving chat history bucket to Firestore")

    def set_active_chat_bucket(self, user_id, bucket_key):
        effective_user = _effective_user(user_id)
        try:
            self.client.collection("sessions").document(effective_user).set(
                {"active_chat_bucket_key": bucket_key or ""}, merge=True
            )
        except Exception:
            logger.exception("Error saving active chat bucket to Firestore")

    def _schema_cache_doc_id(self, cache_key):
        """Firestore document IDs are split on "/" into alternating
        collection/document path segments when passed as a single string
        to .document() - fine for every OTHER doc id in this module
        (a user_id, or a hash-based key like compute_connection_key's own
        output), but cache_key here is db.py's get_conn_identifier(),
        which for several dialects is a literal "user@host:port/dbname"
        (see that function's own docstring) - passing that straight
        through would silently misinterpret it as a nested-subcollection
        path instead of one flat document, or raise outright. Hashing it
        into a plain hex string sidesteps that entirely; the original
        cache_key is still stored as its own field below so a document can
        be identified/debugged without reversing the hash."""
        return hashlib.sha256(cache_key.encode('utf-8')).hexdigest()

    def get_cached_schema(self, cache_key):
        try:
            doc = self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).get()
        except Exception:
            logger.exception("Error fetching cached schema from Firestore")
            return None
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        return {
            "schema_text": data.get("schema_text"),
            "cached_at": data.get("cached_at"),
            # Stored as a native map (see save_chat_bucket's own comment on
            # why a Firestore field never needs schema_cache.py's own
            # JSON-string encoding SQLite's TEXT column requires) - None
            # for a key whose overview was never saved, same "absence
            # means nothing to report" convention as everywhere else.
            "overview": data.get("overview"),
        }

    def set_cached_schema(self, cache_key, schema_text, cached_at):
        try:
            # merge=True - never touches a previously-saved "overview"
            # field for this same doc, same independence
            # SqliteStateStore.set_cached_schema's own ON CONFLICT clause
            # keeps between the two columns.
            self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).set(
                {
                    "cache_key": cache_key,
                    "schema_text": schema_text,
                    "cached_at": cached_at,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        except Exception:
            logger.exception("Error saving cached schema to Firestore")

    def set_cached_schema_overview(self, cache_key, overview):
        try:
            # merge=True - never touches a previously-saved schema_text/
            # cached_at for this same doc, mirroring set_cached_schema
            # above.
            self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).set(
                {
                    "cache_key": cache_key,
                    "overview": overview,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        except Exception:
            logger.exception("Error saving cached schema overview to Firestore")

    def delete_cached_schema(self, cache_key):
        try:
            self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).delete()
        except Exception:
            logger.exception("Error deleting cached schema from Firestore")

    def mark_schema_fetch_pending(self, cache_key):
        try:
            # merge=True - never touches a previously-saved fetch_error for
            # this same doc, same independence set_cached_schema/
            # set_cached_schema_overview already keep between their own
            # fields (see this method's own docstring in StateStore above).
            self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).set(
                {
                    "cache_key": cache_key,
                    "fetch_pending": True,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        except Exception:
            logger.exception("Error marking schema fetch pending in Firestore")

    def mark_schema_fetch_done(self, cache_key, error=None):
        try:
            self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).set(
                {
                    "cache_key": cache_key,
                    "fetch_pending": False,
                    "fetch_error": error,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        except Exception:
            logger.exception("Error marking schema fetch done in Firestore")

    def get_schema_fetch_status(self, cache_key):
        try:
            doc = self.client.collection("schema_cache").document(self._schema_cache_doc_id(cache_key)).get()
        except Exception:
            logger.exception("Error fetching schema fetch status from Firestore")
            return {"pending": False, "error": None}
        if not doc.exists:
            return {"pending": False, "error": None}
        data = doc.to_dict() or {}
        return {"pending": bool(data.get("fetch_pending")), "error": data.get("fetch_error")}

    def list_cached_schema_texts(self):
        try:
            docs = self.client.collection("schema_cache").stream()
        except Exception:
            logger.exception("Error listing cached schemas from Firestore")
            return {}
        result = {}
        for doc in docs:
            data = doc.to_dict() or {}
            schema_text = data.get("schema_text")
            cache_key = data.get("cache_key")
            if schema_text is not None and cache_key is not None:
                result[cache_key] = schema_text
        return result