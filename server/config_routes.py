"""
config_routes.py

The /api/config endpoint: reads/writes the current session's active
database (dialect-aware - Postgres, MySQL, BigQuery, or Snowflake) and its
"Automatic SQL Execution" preference, and reports back everything the
frontend needs to render its DB/session UI - including the server-
configured list of available Gemini models (PRESET_MODELS), which the
frontend may pass per-request to /api/translate, but which is not tied to
or persisted on the session.

Connections are represented as descriptors: {"type": "postgres", "url":
"..."} (MySQL is identical in shape, just {"type": "mysql", "url":
"mysql://..."} - see backends/mysql.py's module docstring), {"type":
"bigquery", "url": "bigquery://<project>/<dataset>",
"project_id": "...", "dataset": "...", "credentials_json": "...",
"billing_project_id": "..."}, {"type": "snowflake", "url":
"snowflake://<account>/<database>[/<schema>]", "account": "...",
"user": "...", "warehouse": "...", "database": "...", "schema": "...",
"role": "...", "password": "..."} (or "private_key"/
"private_key_passphrase" instead of "password"), {"type": "databricks",
"url": "databricks://<server_hostname><http_path>", "server_hostname":
"...", "http_path": "...", "access_token": "...", "catalog": "...",
"schema": "..."}, or {"type": "oracle", "url": "oracle://<host>:<port>/
<service_name-or-sid>", "host": "...", "port": 1521, "service_name": "..."
(or "sid" instead), "user": "...", "password": "...", "schema": "..."} -
see db.py's session_to_descriptor / backends/base.py's module docstring /
backends/bigquery.py's, backends/snowflake.py's, backends/databricks.py's,
and backends/oracle.py's module docstrings for what billing_project_id is
and why it's not just project_id, for Snowflake's two mutually-exclusive
auth methods, and for why Databricks/Oracle (like Snowflake) only support
one explicit credential shape (a Personal Access Token for Databricks,
plain username/password for Oracle - no wallet/mTLS yet) rather than any
ambient identity. credentials_json (BigQuery), password/private_key/
private_key_passphrase (Snowflake), access_token (Databricks), and
password (Oracle - the same field name Postgres's URL-embedded password
plays, but standalone here since Oracle has no single connection-string
url of its own) are the fields that must never round-trip back to the
frontend once saved (see state_store.get_db_connections' include_credentials
param and its _CREDENTIAL_CONFIG_FIELDS); _resolve_bigquery_credentials/
_resolve_snowflake_credentials/_resolve_databricks_credentials/
_resolve_oracle_credentials below are what let a user re-select or rename
a saved connection, or just switch back to it, without re-entering its
credential every time. billing_project_id is NOT a credential (it's just a
project id string) and always round-trips to the frontend as-is - see
get_db_connections' _strip_credentials, which only strips the fields in
_CREDENTIAL_CONFIG_FIELDS.

Billing policy, by design: admin-configured BigQuery presets (CONFIGURED_DBS,
loaded from DATABASE_PRESETS_FILE in app_config.py) authenticate via this app's
own ambient identity (ADC) and never carry a credentials_json; an admin who
wants a preset to bill anywhere other than its own project_id must say so
explicitly via that preset's own "billing_project_id" - there is no env
var or other implicit default, on purpose, so this app's own project never
silently pays for a preset an admin didn't deliberately configure that way
(see app_config.py). Snowflake presets are different: Snowflake has no
ADC-equivalent ambient identity at all, so a Snowflake preset DOES carry
its own explicit password/private_key right in app_config.py's presets
file (see that module's DATABASE_PRESETS_FILE comment) - copied across
into the session's db_config wherever a preset is selected (both the
authenticated preset-match path in _parse_incoming_connection below and
the anonymous preset_index path in handle_config). A user's own custom
BigQuery connection is held to a stricter rule still: it must ALWAYS
supply both its own billing_project_id and its own service-account key
(credentials_json) - _parse_incoming_connection and
_parse_incoming_custom_databases below reject/skip a custom BigQuery
connection missing either, rather than falling back to a preset's or this
app's billing project. The reasoning: only a key with actual
bigquery.jobs.create rights on the given billing project can make that
project pay for the job at all, so accepting a billing_project_id without
requiring its own key would just fail at query time anyway - and never
inferring one from the other keeps a user's billing choice explicit rather
than a side effect of what happened to be embedded in their pasted key.

Snowflake has no ADC-equivalent ambient identity at all (see
backends/snowflake.py's module docstring), so there's no preset path to
speak of yet - every Snowflake connection today is a user's own custom
one, and _parse_incoming_connection/_parse_incoming_custom_databases hold
it to the same "every field explicit, nothing inferred" bar BigQuery's
custom connections are held to, just without a billing-project dimension
(Snowflake warehouses aren't billed the way BigQuery jobs are).

Databricks is architecturally the same shape as Snowflake (no ADC-
equivalent ambient identity, every connection needs its own explicit
credential - here, a Personal Access Token; see backends/databricks.py's
module docstring for why OAuth isn't supported yet either) - the one
difference is Databricks DOES have a preset path, unlike Snowflake: an
admin-configured Databricks preset carries its own access_token right in
app_config.py's presets file, copied across into the session's db_config
wherever that preset is selected, the same way a Snowflake preset's
password/private_key would be if Snowflake had preset support (it
doesn't, yet - see app_config.py's DATABASE_PRESETS_FILE comment).

Oracle is the same "no ADC-equivalent ambient identity, own preset
credential" shape as Databricks - an admin-configured Oracle preset
carries its own explicit password right in app_config.py's presets file,
copied across into the session's db_config wherever that preset is
selected. Unlike every other structured (non-URL) dialect here, Oracle's
"schema" descriptor field isn't a separate namespace within the connected
database the way BigQuery's dataset/Snowflake's-or-Databricks' schema is -
it's actually the *user/owner* objects belong to, and querying a different
one than the connecting user requires an explicit ALTER SESSION SET
CURRENT_SCHEMA once connected (see backends/oracle.py's module docstring
for the identifier-validation reasoning behind that).

Redshift is the same "no ADC-equivalent ambient identity, own preset
credential" shape as Oracle/Databricks - an admin-configured Redshift
preset carries its own explicit password right in app_config.py's presets
file, copied across into the session's db_config wherever that preset is
selected. Unlike Oracle, Redshift's "schema" descriptor field really is a
separate Postgres-style namespace (not a stand-in for a user) - see
backends/redshift.py's module docstring. Also unlike Oracle, there's no
"ssl" descriptor field/opt-in flag: Redshift connections always require
TLS (see backends/redshift.py's connect()), so it's simply always on
rather than a per-connection choice.
"""

import json
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify

from app_config import (
    CONFIGURED_DBS, DEFAULT_CONN, PRESET_MODELS,
    AUTH_ENABLED, IS_CLOUD_RUN, state_store, logger,
)
import os
from auth import (
    get_or_create_session_id, get_current_user_identity, apply_session_cookie,
    is_anonymous_user,
)
from db import get_conn_identifier, session_to_descriptor
from backends import get_backend
from state_store import compute_connection_key
import schema_cache

config_bp = Blueprint('config', __name__)


def _bigquery_url(project_id, dataset):
    """Synthetic, non-secret identifier for a BigQuery connection - plays
    the same role a Postgres DSN does elsewhere (schema-cache key, preset/
    custom-connection matching, display), but is never a credential."""
    return f"bigquery://{project_id}/{dataset}"


def _snowflake_url(account, database, schema):
    """Synthetic, non-secret identifier for a Snowflake connection - same
    role _bigquery_url plays. Schema is optional on a Snowflake connection
    (omitted = the account's own default schema - see
    backends/snowflake.py's module docstring), so it's only appended when
    given, rather than embedding an empty trailing segment."""
    base = f"snowflake://{account}/{database}"
    return f"{base}/{schema}" if schema else base


def _databricks_url(server_hostname, http_path):
    """Synthetic, non-secret identifier for a Databricks connection - same
    role _bigquery_url/_snowflake_url play (schema-cache key, preset/
    custom-connection matching, display), but is never a credential."""
    return f"databricks://{server_hostname}{http_path}"


def _oracle_url(host, port, service_name_or_sid):
    """Synthetic, non-secret identifier for an Oracle connection - same
    role _bigquery_url/_snowflake_url/_databricks_url play (schema-cache
    key, preset/custom-connection matching, display), but is never a
    credential. `service_name_or_sid` is whichever of service_name/sid the
    caller resolved (service_name preferred - see backends/oracle.py's
    connect())."""
    return f"oracle://{host}:{port}/{service_name_or_sid}"


def _redshift_url(host, port, database):
    """Synthetic, non-secret identifier for a Redshift connection - same
    role _bigquery_url/_snowflake_url/_databricks_url/_oracle_url play
    (schema-cache key, preset/custom-connection matching, display), but is
    never a credential."""
    return f"redshift://{host}:{port}/{database}"


def _credential_for_key(config):
    """Whichever single credential value `config` carries - BigQuery's
    credentials_json, Snowflake's password/private_key, Databricks'
    access_token, or Oracle's password - for folding into
    compute_connection_key()'s hash at the one call site (below, in
    handle_config) that has to work generically across every connection
    type rather than inside a type-specific branch that already knows
    which field it means. Mutually exclusive by connection type (a
    Postgres/BigQuery config never has "password"/"private_key"/
    "access_token" set, and so on - note Postgres/MySQL's own URL-embedded
    password never lands in `config` at all, only Oracle's standalone
    "password" field does, so there's no collision there either), so a
    plain OR-chain is correct without needing to dispatch on db_type - if a
    future backend adds its own credential field, add it here too (and to
    state_store.py's _CREDENTIAL_CONFIG_FIELDS, which is what actually
    keeps it from leaking to the frontend - this function only affects key
    derivation)."""
    config = config or {}
    return (
        config.get("credentials_json") or config.get("password")
        or config.get("private_key") or config.get("access_token")
    )


def _resolve_bigquery_credentials(user_identity, project_id, dataset, provided_credentials_json, name=None):
    """Returns the credentials_json to persist for a BigQuery connection:
    whatever was freshly provided in this request, else whatever was
    already stored for this exact project/dataset. Without this, simply
    re-selecting (or renaming) an already-saved custom BigQuery connection
    would look like "no credentials provided" and silently drop the
    stored key, since get_db_connections() never sends it back to the
    frontend in the first place.

    `name` disambiguates when *multiple* saved connections share this
    project/dataset (now possible - see compute_connection_key's
    docstring in state_store.py for why url/project/dataset alone isn't a
    unique identity anymore): matched first, falling back to the first
    project/dataset match if no name match is found (legacy behavior, and
    still correct whenever there's genuinely only one)."""
    if provided_credentials_json:
        return provided_credentials_json
    target_url = _bigquery_url(project_id, dataset)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "bigquery" and db.get("url") == target_url]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("credentials_json")
    return (matches[0].get("config") or {}).get("credentials_json")


def _resolve_snowflake_credentials(user_identity, account, database, schema, provided_password,
                                    provided_private_key, provided_private_key_passphrase, name=None):
    """Returns (password, private_key, private_key_passphrase) to persist
    for a Snowflake connection - mirrors _resolve_bigquery_credentials'
    role (letting a user re-select or rename an already-saved connection
    without re-entering its credential every time it's touched), extended
    for Snowflake's two mutually-exclusive auth methods instead of
    BigQuery's single credentials_json.

    A password or private_key freshly supplied in THIS request always wins
    outright over whatever was previously saved, and switching from one
    auth method to the other drops the old one entirely rather than
    merging - these are alternatives, not fields to accumulate. Only when
    the request supplies neither fresh does this fall back to whatever's
    already stored for this exact account/database/schema, the same
    "re-selecting/renaming shouldn't look like a blank credential" case
    _resolve_bigquery_credentials exists for."""
    if provided_password:
        return provided_password, None, None
    if provided_private_key:
        return None, provided_private_key, provided_private_key_passphrase or None

    target_url = _snowflake_url(account, database, schema)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "snowflake" and db.get("url") == target_url]
    if not matches:
        return None, None, None
    match = None
    if name:
        match = next((db for db in matches if db.get("name") == name), None)
    if not match:
        match = matches[0]
    cfg = match.get("config") or {}
    return cfg.get("password"), cfg.get("private_key"), cfg.get("private_key_passphrase")


def _resolve_databricks_credentials(user_identity, server_hostname, http_path, provided_access_token, name=None):
    """Returns the access_token to persist for a Databricks connection -
    mirrors _resolve_bigquery_credentials'/_resolve_snowflake_credentials'
    role (letting a user re-select or rename an already-saved connection
    without re-entering its credential every time it's touched). Simpler
    than Snowflake's version since Databricks (this first pass) has only
    one credential shape to resolve, not two mutually-exclusive ones - see
    backends/databricks.py's module docstring."""
    if provided_access_token:
        return provided_access_token
    target_url = _databricks_url(server_hostname, http_path)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "databricks" and db.get("url") == target_url]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("access_token")
    return (matches[0].get("config") or {}).get("access_token")


def _resolve_oracle_credentials(user_identity, host, port, service_name_or_sid, provided_password, name=None):
    """Returns the password to persist for an Oracle connection - mirrors
    _resolve_databricks_credentials' role (letting a user re-select or
    rename an already-saved connection without re-entering its credential
    every time it's touched). Same single-credential-shape simplicity as
    Databricks - this first pass is plain username/password only, no
    wallet/mTLS - see backends/oracle.py's module docstring."""
    if provided_password:
        return provided_password
    target_url = _oracle_url(host, port, service_name_or_sid)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "oracle" and db.get("url") == target_url]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("password")
    return (matches[0].get("config") or {}).get("password")


def _resolve_redshift_credentials(user_identity, host, port, database, provided_password, name=None):
    """Returns the password to persist for a Redshift connection - mirrors
    _resolve_oracle_credentials' role (letting a user re-select or rename
    an already-saved connection without re-entering its credential every
    time it's touched). Same single-credential-shape simplicity as
    Databricks/Oracle - this first pass is plain username/password only,
    no IAM temporary credentials - see backends/redshift.py's module
    docstring."""
    if provided_password:
        return provided_password
    target_url = _redshift_url(host, port, database)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "redshift" and db.get("url") == target_url]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("password")
    return (matches[0].get("config") or {}).get("password")


_CUSTOM_BIGQUERY_MISSING_FIELDS_ERROR = (
    "Custom BigQuery connections require both a billing project ID and a "
    "service-account key (JSON). This app's own project never pays for a "
    "custom connection - only a key with billing rights on the project you "
    "specify can actually run the query."
)

_CUSTOM_SNOWFLAKE_MISSING_FIELDS_ERROR = (
    "Custom Snowflake connections require an account, user, warehouse, and "
    "database, plus either a password or a private key. Snowflake has no "
    "ambient/shared identity this app can fall back to - every connection "
    "needs its own explicit credential."
)

_CUSTOM_DATABRICKS_MISSING_FIELDS_ERROR = (
    "Custom Databricks connections require a server hostname, an HTTP path, "
    "and an access token. Databricks has no ambient/shared identity this "
    "app can fall back to - every connection needs its own explicit "
    "Personal Access Token."
)

_CUSTOM_ORACLE_MISSING_FIELDS_ERROR = (
    "Custom Oracle connections require a host, a user, a password, and "
    "either a service name or a SID. Oracle has no ambient/shared identity "
    "this app can fall back to - every connection needs its own explicit "
    "credential."
)

_CUSTOM_REDSHIFT_MISSING_FIELDS_ERROR = (
    "Custom Redshift connections require a host, a database, a user, and a "
    "password. Redshift has no ambient/shared identity this app can fall "
    "back to - every connection needs its own explicit credential."
)


def _parse_incoming_connection(data, user_identity, is_custom):
    """Builds (db_type, db_url, db_config, error) from a POST body's
    top-level active-connection fields. db_url is None if the request
    didn't supply enough to identify a connection of the given type (e.g. a
    BigQuery selection missing project_id/dataset) - callers treat that the
    same as "no connection change requested". `error`, when not None, means
    this connection is invalid and MUST NOT be saved/activated - used for a
    custom BigQuery connection missing its required billing_project_id
    and/or credentials_json, or a custom Snowflake connection missing its
    required account/user/warehouse/database and/or a credential (see
    module docstring)."""
    db_type = (data.get('database_type') or 'postgres').strip().lower()

    if db_type == 'bigquery':
        project_id = (data.get('project_id') or '').strip()
        dataset = (data.get('dataset') or '').strip()
        if not (project_id and dataset):
            return db_type, None, {}, None
        db_url = _bigquery_url(project_id, dataset)
        db_config = {"project_id": project_id, "dataset": dataset}

        if is_custom:
            # A user's own connection: both fields are required, always
            # explicit, never inferred from the other or from a preset/app
            # default - see the module docstring for why. credentials_json
            # still supports "leave blank to keep the previously-saved key"
            # (it's the one field that never round-trips back to the
            # frontend to redisplay); billing_project_id doesn't need that
            # treatment since it's not a credential and is always shown/
            # resent as-is by the frontend.
            credentials_json = _resolve_bigquery_credentials(
                user_identity, project_id, dataset, data.get('credentials_json'),
                name=data.get('database_name'),
            )
            billing_project_id = (data.get('billing_project_id') or '').strip()
            if not (credentials_json and billing_project_id):
                return db_type, db_url, db_config, _CUSTOM_BIGQUERY_MISSING_FIELDS_ERROR
            db_config["credentials_json"] = credentials_json
            db_config["billing_project_id"] = billing_project_id
        else:
            # A genuine admin-preset selection (matched by its real fields,
            # not preset_index - see the anonymous branch in handle_config
            # for that path). Presets are trusted/admin-configured: use
            # their own explicit billing_project_id (app_config.py) if they
            # have one; if not, don't invent one here either - the backend
            # falls back to billing project_id itself, which will 403
            # loudly if that's data this app doesn't own. There is
            # deliberately no other fallback (see module docstring).
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "bigquery" and db.get("url") == db_url),
                None,
            )
            if preset_match and preset_match.get("billing_project_id"):
                db_config["billing_project_id"] = preset_match["billing_project_id"]
        return db_type, db_url, db_config, None

    if db_type == 'snowflake':
        account = (data.get('account') or '').strip()
        user = (data.get('user') or '').strip()
        warehouse = (data.get('warehouse') or '').strip()
        database = (data.get('database') or '').strip()
        schema = (data.get('schema') or '').strip()
        role = (data.get('role') or '').strip()
        if not (account and user and warehouse and database):
            return db_type, None, {}, None
        db_url = _snowflake_url(account, database, schema)
        db_config = {"account": account, "user": user, "warehouse": warehouse, "database": database}
        if schema:
            db_config["schema"] = schema
        if role:
            db_config["role"] = role

        if is_custom:
            # Same policy as BigQuery's custom connections: every field
            # explicit, nothing inferred, nothing falls back to a shared/
            # app identity or to an admin preset's credential - Snowflake
            # has no ADC-equivalent ambient auth mode to fall back to even
            # if it wanted to (see app_config.py's DATABASE_PRESETS_FILE
            # comment for the admin-preset side of this).
            password, private_key, private_key_passphrase = _resolve_snowflake_credentials(
                user_identity, account, database, schema,
                data.get('password'), data.get('private_key'), data.get('private_key_passphrase'),
                name=data.get('database_name'),
            )
            if not (password or private_key):
                return db_type, db_url, db_config, _CUSTOM_SNOWFLAKE_MISSING_FIELDS_ERROR
            if password:
                db_config["password"] = password
            else:
                db_config["private_key"] = private_key
                if private_key_passphrase:
                    db_config["private_key_passphrase"] = private_key_passphrase
        else:
            # A genuine admin-preset selection (matched by its real fields,
            # not preset_index - see the anonymous branch in handle_config
            # for that path). Unlike a BigQuery preset, a Snowflake preset
            # DOES carry its own credential (app_config.py - Snowflake has
            # no ADC-equivalent ambient identity to fall back to), so it
            # must be copied across here or connect() downstream would
            # raise "requires either 'password' or 'private_key'" against
            # an empty db_config.
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "snowflake" and db.get("url") == db_url),
                None,
            )
            if preset_match:
                if preset_match.get("password"):
                    db_config["password"] = preset_match["password"]
                elif preset_match.get("private_key"):
                    db_config["private_key"] = preset_match["private_key"]
                    if preset_match.get("private_key_passphrase"):
                        db_config["private_key_passphrase"] = preset_match["private_key_passphrase"]
        return db_type, db_url, db_config, None

    if db_type == 'databricks':
        server_hostname = (data.get('server_hostname') or '').strip()
        http_path = (data.get('http_path') or '').strip()
        catalog = (data.get('catalog') or '').strip()
        schema = (data.get('schema') or '').strip()
        if not (server_hostname and http_path):
            return db_type, None, {}, None
        db_url = _databricks_url(server_hostname, http_path)
        db_config = {"server_hostname": server_hostname, "http_path": http_path}
        if catalog:
            db_config["catalog"] = catalog
        if schema:
            db_config["schema"] = schema

        if is_custom:
            # Same policy as Snowflake's custom connections: every field
            # explicit, nothing inferred, nothing falls back to a shared/
            # app identity or to an admin preset's credential - Databricks
            # has no ADC-equivalent ambient auth mode to fall back to even
            # if it wanted to (see backends/databricks.py's module
            # docstring).
            access_token = _resolve_databricks_credentials(
                user_identity, server_hostname, http_path, data.get('access_token'),
                name=data.get('database_name'),
            )
            if not access_token:
                return db_type, db_url, db_config, _CUSTOM_DATABRICKS_MISSING_FIELDS_ERROR
            db_config["access_token"] = access_token
        else:
            # A genuine admin-preset selection (matched by its real fields,
            # not preset_index - see the anonymous branch in handle_config
            # for that path). Unlike a BigQuery preset, a Databricks preset
            # DOES carry its own credential (app_config.py - Databricks has
            # no ADC-equivalent ambient identity to fall back to), so it
            # must be copied across here or connect() downstream would
            # raise "requires an access_token" against an empty db_config.
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "databricks" and db.get("url") == db_url),
                None,
            )
            if preset_match and preset_match.get("access_token"):
                db_config["access_token"] = preset_match["access_token"]
        return db_type, db_url, db_config, None

    if db_type == 'oracle':
        host = (data.get('host') or '').strip()
        service_name = (data.get('service_name') or '').strip()
        sid = (data.get('sid') or '').strip()
        user = (data.get('user') or '').strip()
        schema = (data.get('schema') or '').strip()
        raw_port = data.get('port')
        use_ssl = bool(data.get('ssl'))
        # Same "core identifying fields, nothing inferred" threshold
        # Snowflake's account/user/warehouse/database is - a request
        # missing any of these isn't enough to even identify a connection
        # yet (e.g. a fresh blank row), not a validation error - see the
        # docstring above.
        if not (host and user and (service_name or sid)):
            return db_type, None, {}, None
        try:
            port = int(raw_port) if raw_port else 1521
        except (TypeError, ValueError):
            port = 1521
        service_name_or_sid = service_name or sid
        db_url = _oracle_url(host, port, service_name_or_sid)
        db_config = {"host": host, "port": port, "user": user}
        if service_name:
            db_config["service_name"] = service_name
        else:
            db_config["sid"] = sid
        if schema:
            db_config["schema"] = schema
        if use_ssl:
            # See backends/oracle.py's module docstring - TLS is opt-in per
            # connection, not inferred from host/port, so this has to be
            # threaded through explicitly like every other field here.
            db_config["ssl"] = True

        if is_custom:
            # Same policy as Snowflake's/Databricks' custom connections:
            # every field explicit, nothing inferred, nothing falls back to
            # a shared/app identity or to an admin preset's credential -
            # Oracle has no ADC-equivalent ambient auth mode to fall back
            # to (see backends/oracle.py's module docstring).
            password = _resolve_oracle_credentials(
                user_identity, host, port, service_name_or_sid, data.get('password'),
                name=data.get('database_name'),
            )
            if not password:
                return db_type, db_url, db_config, _CUSTOM_ORACLE_MISSING_FIELDS_ERROR
            db_config["password"] = password
        else:
            # A genuine admin-preset selection (matched by its real fields,
            # not preset_index - see the anonymous branch in handle_config
            # for that path). Like a Databricks preset, an Oracle preset
            # DOES carry its own credential (app_config.py - Oracle has no
            # ADC-equivalent ambient identity to fall back to), so it must
            # be copied across here or connect() downstream would raise
            # "requires a user and password" against an empty db_config.
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "oracle" and db.get("url") == db_url),
                None,
            )
            if preset_match and preset_match.get("password"):
                db_config["password"] = preset_match["password"]
            if preset_match and preset_match.get("ssl"):
                db_config["ssl"] = True
        return db_type, db_url, db_config, None

    if db_type == 'redshift':
        host = (data.get('host') or '').strip()
        database = (data.get('database') or '').strip()
        user = (data.get('user') or '').strip()
        schema = (data.get('schema') or '').strip()
        raw_port = data.get('port')
        # Same "core identifying fields, nothing inferred" threshold every
        # other structured dialect above uses - a request missing any of
        # these isn't enough to even identify a connection yet (e.g. a
        # fresh blank row), not a validation error - see the docstring
        # above.
        if not (host and database and user):
            return db_type, None, {}, None
        try:
            port = int(raw_port) if raw_port else 5439
        except (TypeError, ValueError):
            port = 5439
        db_url = _redshift_url(host, port, database)
        db_config = {"host": host, "port": port, "database": database, "user": user}
        if schema:
            db_config["schema"] = schema

        if is_custom:
            # Same policy as Oracle's/Databricks'/Snowflake's custom
            # connections: every field explicit, nothing inferred, nothing
            # falls back to a shared/app identity or to an admin preset's
            # credential - Redshift has no ADC-equivalent ambient auth mode
            # to fall back to (see backends/redshift.py's module
            # docstring).
            password = _resolve_redshift_credentials(
                user_identity, host, port, database, data.get('password'),
                name=data.get('database_name'),
            )
            if not password:
                return db_type, db_url, db_config, _CUSTOM_REDSHIFT_MISSING_FIELDS_ERROR
            db_config["password"] = password
        else:
            # A genuine admin-preset selection (matched by its real fields,
            # not preset_index - see the anonymous branch in handle_config
            # for that path). Like an Oracle preset, a Redshift preset DOES
            # carry its own credential (app_config.py - Redshift has no
            # ADC-equivalent ambient identity to fall back to), so it must
            # be copied across here or connect() downstream would raise
            # "requires a user and password" against an empty db_config.
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "redshift" and db.get("url") == db_url),
                None,
            )
            if preset_match and preset_match.get("password"):
                db_config["password"] = preset_match["password"]
        return db_type, db_url, db_config, None

    if db_type == 'mysql':
        # Same shape as Postgres - a single connection-string URL carries
        # everything (host, credentials, database), so there's no
        # dialect-specific config dict to build and no preset-credential
        # copy-over needed (unlike BigQuery/Snowflake) - a MySQL preset's
        # URL already contains its own credentials, same as a Postgres
        # preset's does. See backends/mysql.py's module docstring.
        return db_type, data.get('database_url'), {}, None

    # Default / explicit postgres - also the fallback for any unrecognized
    # db_type value, same as before this module supported more than one
    # simple-URL dialect (predates multi-dialect support entirely - see
    # backends/__init__.py's get_backend() docstring for the equivalent
    # default at the dispatch layer).
    return 'postgres', data.get('database_url'), {}, None


def _parse_incoming_custom_databases(custom_databases_in, user_identity):
    """Normalizes the frontend's `custom_databases` list (each item using
    the same flat per-type field shape as the top-level connection - see
    _parse_incoming_connection) into the {"connection_key", "name", "type",
    "url", "config"} shape state_store.set_db_connections expects, merging
    in previously saved BigQuery/Snowflake credentials where the request
    didn't supply a fresh one. Returns None if the request didn't include
    the field at all (meaning "leave the saved list alone"), same as
    before.

    A BigQuery entry missing its required billing_project_id and/or
    credentials_json, or a Snowflake entry missing account/user/warehouse/
    database and/or a credential (see module docstring), is silently
    skipped - not persisted - rather than erroring the whole batch save:
    this list can legitimately include an in-progress row the user hasn't
    finished filling in yet (e.g. a freshly-added blank "+ Add custom
    connection" row), same as an incomplete Postgres row is already
    silently dropped below. Trying to *activate* an incomplete connection
    (as opposed to just having it sit half-filled in the saved list) is
    what actually gets rejected with a clear error - see
    _parse_incoming_connection's `error` return, used for the active
    selection, not this list.

    connection_key (see compute_connection_key's docstring in
    state_store.py) is computed here, once, from the exact (name, url,
    credential) this function is about to persist - so it can also be
    reused as-is for the session's custom_connection_key pointer when this
    same request's active connection is one of these entries (see
    handle_config), instead of that being recomputed separately and
    risking drifting out of sync with what actually got saved."""
    if custom_databases_in is None:
        return None

    merged = []
    for db in custom_databases_in:
        db_type = (db.get('type') or 'postgres').strip().lower()
        if db_type == 'bigquery':
            project_id = (db.get('project_id') or '').strip()
            dataset = (db.get('dataset') or '').strip()
            if not (project_id and dataset):
                continue
            url = _bigquery_url(project_id, dataset)
            credentials_json = _resolve_bigquery_credentials(
                user_identity, project_id, dataset, db.get('credentials_json'),
                name=db.get('name'),
            )
            billing_project_id = (db.get('billing_project_id') or '').strip()
            if not (credentials_json and billing_project_id):
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {
                "project_id": project_id,
                "dataset": dataset,
                "credentials_json": credentials_json,
                "billing_project_id": billing_project_id,
            }
            name = db.get("name") or dataset or "Custom BigQuery"
            merged.append({
                "connection_key": compute_connection_key(name, url, credentials_json),
                "name": name,
                "type": "bigquery",
                "url": url,
                "config": config,
            })
        elif db_type == 'snowflake':
            account = (db.get('account') or '').strip()
            user = (db.get('user') or '').strip()
            warehouse = (db.get('warehouse') or '').strip()
            database = (db.get('database') or '').strip()
            schema = (db.get('schema') or '').strip()
            role = (db.get('role') or '').strip()
            if not (account and user and warehouse and database):
                continue
            url = _snowflake_url(account, database, schema)
            password, private_key, private_key_passphrase = _resolve_snowflake_credentials(
                user_identity, account, database, schema,
                db.get('password'), db.get('private_key'), db.get('private_key_passphrase'),
                name=db.get('name'),
            )
            if not (password or private_key):
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"account": account, "user": user, "warehouse": warehouse, "database": database}
            if schema:
                config["schema"] = schema
            if role:
                config["role"] = role
            if password:
                config["password"] = password
            else:
                config["private_key"] = private_key
                if private_key_passphrase:
                    config["private_key_passphrase"] = private_key_passphrase
            name = db.get("name") or database or "Custom Snowflake"
            merged.append({
                "connection_key": compute_connection_key(name, url, password or private_key),
                "name": name,
                "type": "snowflake",
                "url": url,
                "config": config,
            })
        elif db_type == 'databricks':
            server_hostname = (db.get('server_hostname') or '').strip()
            http_path = (db.get('http_path') or '').strip()
            catalog = (db.get('catalog') or '').strip()
            schema = (db.get('schema') or '').strip()
            if not (server_hostname and http_path):
                continue
            url = _databricks_url(server_hostname, http_path)
            access_token = _resolve_databricks_credentials(
                user_identity, server_hostname, http_path, db.get('access_token'),
                name=db.get('name'),
            )
            if not access_token:
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"server_hostname": server_hostname, "http_path": http_path, "access_token": access_token}
            if catalog:
                config["catalog"] = catalog
            if schema:
                config["schema"] = schema
            name = db.get("name") or http_path or "Custom Databricks"
            merged.append({
                "connection_key": compute_connection_key(name, url, access_token),
                "name": name,
                "type": "databricks",
                "url": url,
                "config": config,
            })
        elif db_type == 'oracle':
            host = (db.get('host') or '').strip()
            service_name = (db.get('service_name') or '').strip()
            sid = (db.get('sid') or '').strip()
            user = (db.get('user') or '').strip()
            schema = (db.get('schema') or '').strip()
            raw_port = db.get('port')
            use_ssl = bool(db.get('ssl'))
            if not (host and user and (service_name or sid)):
                continue
            try:
                port = int(raw_port) if raw_port else 1521
            except (TypeError, ValueError):
                port = 1521
            service_name_or_sid = service_name or sid
            url = _oracle_url(host, port, service_name_or_sid)
            password = _resolve_oracle_credentials(
                user_identity, host, port, service_name_or_sid, db.get('password'),
                name=db.get('name'),
            )
            if not password:
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"host": host, "port": port, "user": user}
            if service_name:
                config["service_name"] = service_name
            else:
                config["sid"] = sid
            if schema:
                config["schema"] = schema
            if use_ssl:
                config["ssl"] = True
            config["password"] = password
            name = db.get("name") or service_name_or_sid or "Custom Oracle"
            merged.append({
                "connection_key": compute_connection_key(name, url, password),
                "name": name,
                "type": "oracle",
                "url": url,
                "config": config,
            })
        elif db_type == 'redshift':
            host = (db.get('host') or '').strip()
            database = (db.get('database') or '').strip()
            user = (db.get('user') or '').strip()
            schema = (db.get('schema') or '').strip()
            raw_port = db.get('port')
            if not (host and database and user):
                continue
            try:
                port = int(raw_port) if raw_port else 5439
            except (TypeError, ValueError):
                port = 5439
            url = _redshift_url(host, port, database)
            password = _resolve_redshift_credentials(
                user_identity, host, port, database, db.get('password'),
                name=db.get('name'),
            )
            if not password:
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"host": host, "port": port, "database": database, "user": user}
            if schema:
                config["schema"] = schema
            config["password"] = password
            name = db.get("name") or database or "Custom Redshift"
            merged.append({
                "connection_key": compute_connection_key(name, url, password),
                "name": name,
                "type": "redshift",
                "url": url,
                "config": config,
            })
        else:
            # Postgres and MySQL both land here - a single connection-
            # string URL carries everything, so there's nothing dialect-
            # specific to normalize beyond preserving whichever of the two
            # was actually selected (db_type), rather than relabeling a
            # MySQL row as Postgres the way this used to unconditionally
            # do (see _parse_incoming_connection's matching fix, and
            # backends/mysql.py's module docstring).
            url = (db.get("url") or "").strip()
            if not url:
                continue
            name = db.get("name") or "Custom"
            merged.append({
                "connection_key": compute_connection_key(name, url, None),
                "name": name,
                "type": db_type if db_type == "mysql" else "postgres",
                "url": url,
                "config": {},
            })
    return merged


@config_bp.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    # session_id resolved first and passed into get_current_user_identity()
    # - required for an anonymous visitor's identity to be scoped to THIS
    # session rather than a fresh one each call; see that function's
    # docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    is_authenticated = bool(
        user_identity and user_identity != session_id and not is_anonymous_user(user_identity)
    )

    preset_urls = {db["url"] for db in CONFIGURED_DBS}

    if request.method == 'POST':
        data = request.get_json() or {}
        new_db_name = data.get('database_name')
        is_custom = bool(data.get('is_custom', False))
        new_auto_sql_execute = data.get('auto_sql_execute')
        if not isinstance(new_auto_sql_execute, bool):
            new_auto_sql_execute = None

        if is_anonymous_user(user_identity) and not is_custom and not data.get('custom_databases'):
            # Anonymous (Cloud Run, signed-out) users selecting one of the
            # admin-configured presets never receive that preset's real
            # connection string (see the redacted configured_databases
            # below) - the frontend can only ask for "preset #N" by index,
            # and the actual descriptor is resolved here, server-side, from
            # CONFIGURED_DBS. Saving/selecting a CUSTOM connection instead
            # falls through to the generic branch below - unlike admin
            # presets, a self-supplied custom connection's credentials
            # aren't a secret from the very user who typed them in, and
            # every anonymous visitor already gets their own fully
            # isolated "anonymous:<session_id>" identity at the
            # state_store layer (see ANONYMOUS_USER_ID_PREFIX in auth.py) -
            # the same reasoning history_routes.py already applies to
            # un-gating translation history for anonymous users.
            new_db_url, new_db_type, new_db_config = None, 'postgres', {}
            preset_index = data.get('preset_index')
            if isinstance(preset_index, int) and 0 <= preset_index < len(CONFIGURED_DBS):
                preset = CONFIGURED_DBS[preset_index]
                new_db_type = preset.get('type', 'postgres')
                new_db_url = preset.get('url')
                if new_db_type == 'bigquery':
                    new_db_config = {
                        "project_id": preset.get("project_id", ""),
                        "dataset": preset.get("dataset", ""),
                    }
                    # Presets authenticate via this app's own ambient
                    # identity (ADC), never a per-user key, so their
                    # billing project - set explicitly per-preset in
                    # app_config.py, with no other fallback (see this
                    # module's docstring) - has to be copied across
                    # explicitly here - without it, backends/bigquery.py's
                    # connect() falls back to project_id itself, which is
                    # exactly the "does not have bigquery.jobs.create
                    # permission" 403 this was meant to fix whenever a
                    # preset points at data outside the app's own project
                    # (e.g. a public dataset).
                    if preset.get("billing_project_id"):
                        new_db_config["billing_project_id"] = preset["billing_project_id"]
                elif new_db_type == 'snowflake':
                    new_db_config = {
                        "account": preset.get("account", ""),
                        "user": preset.get("user", ""),
                        "warehouse": preset.get("warehouse", ""),
                        "database": preset.get("database", ""),
                    }
                    if preset.get("schema"):
                        new_db_config["schema"] = preset["schema"]
                    if preset.get("role"):
                        new_db_config["role"] = preset["role"]
                    # Unlike BigQuery's ambient ADC identity, a Snowflake
                    # preset's credential lives right in app_config.py's
                    # CONFIGURED_DBS entry (Snowflake has none to fall back
                    # to - see that module's DATABASE_PRESETS_FILE comment)
                    # and must be copied across the same way, or
                    # connect() downstream raises "requires either
                    # 'password' or 'private_key'" against an empty config.
                    if preset.get("password"):
                        new_db_config["password"] = preset["password"]
                    elif preset.get("private_key"):
                        new_db_config["private_key"] = preset["private_key"]
                        if preset.get("private_key_passphrase"):
                            new_db_config["private_key_passphrase"] = preset["private_key_passphrase"]
                elif new_db_type == 'databricks':
                    new_db_config = {
                        "server_hostname": preset.get("server_hostname", ""),
                        "http_path": preset.get("http_path", ""),
                    }
                    if preset.get("catalog"):
                        new_db_config["catalog"] = preset["catalog"]
                    if preset.get("schema"):
                        new_db_config["schema"] = preset["schema"]
                    # Like Snowflake, Databricks has no ambient identity to
                    # fall back to - a preset's access_token lives right in
                    # app_config.py's CONFIGURED_DBS entry and must be
                    # copied across, or connect() downstream raises "requires
                    # an access_token" against an empty config.
                    if preset.get("access_token"):
                        new_db_config["access_token"] = preset["access_token"]
                elif new_db_type == 'oracle':
                    new_db_config = {
                        "host": preset.get("host", ""),
                        "port": preset.get("port", 1521),
                        "user": preset.get("user", ""),
                    }
                    if preset.get("service_name"):
                        new_db_config["service_name"] = preset["service_name"]
                    elif preset.get("sid"):
                        new_db_config["sid"] = preset["sid"]
                    if preset.get("schema"):
                        new_db_config["schema"] = preset["schema"]
                    # Like Databricks, Oracle has no ambient identity to
                    # fall back to - a preset's password lives right in
                    # app_config.py's CONFIGURED_DBS entry and must be
                    # copied across, or connect() downstream raises
                    # "requires a user and password" against an empty
                    # config.
                    if preset.get("password"):
                        new_db_config["password"] = preset["password"]
                    if preset.get("ssl"):
                        new_db_config["ssl"] = True
                elif new_db_type == 'redshift':
                    new_db_config = {
                        "host": preset.get("host", ""),
                        "port": preset.get("port", 5439),
                        "database": preset.get("database", ""),
                        "user": preset.get("user", ""),
                    }
                    if preset.get("schema"):
                        new_db_config["schema"] = preset["schema"]
                    # Like Oracle, Redshift has no ambient identity to fall
                    # back to - a preset's password lives right in
                    # app_config.py's CONFIGURED_DBS entry and must be
                    # copied across, or connect() downstream raises
                    # "requires a user and password" against an empty
                    # config.
                    if preset.get("password"):
                        new_db_config["password"] = preset["password"]

            if new_db_url or new_auto_sql_execute is not None:
                state_store.set_session(
                    user_identity, new_db_url, new_auto_sql_execute,
                    db_type=new_db_type, db_config=new_db_config, is_custom=False,
                    custom_connection_key="",
                )

        else:
            new_db_type, new_db_url, new_db_config, connection_error = _parse_incoming_connection(
                data, user_identity, is_custom
            )
            if connection_error:
                # Reject outright, before touching state_store - a
                # half-valid save here (e.g. persisting project_id/dataset
                # but silently dropping billing_project_id) would just
                # surface as a confusing BigQuery 403 later at query time
                # instead of a clear, immediate error now.
                resp = jsonify({'success': False, 'error': connection_error})
                return apply_session_cookie(resp, session_id), 400

            merged_custom_databases = _parse_incoming_custom_databases(
                data.get('custom_databases'), user_identity
            )

            if new_db_url or new_auto_sql_execute is not None:
                if new_db_url:
                    prior_conn_str = state_store.get_session(user_identity)["database_url"]
                    if new_db_url != prior_conn_str:
                        # The DB connection is changing - drop any cached schema
                        # for the connection we're switching to. Without this,
                        # if that connection was cached earlier - e.g. by
                        # another session/user on the same DB, or from before
                        # the schema changed - /api/translate would keep
                        # serving that stale schema for up to
                        # SCHEMA_CACHE_TTL_SECONDS after the switch.
                        schema_cache.invalidate(get_conn_identifier(
                            {"type": new_db_type, "url": new_db_url, **new_db_config}
                        ))

                # Resolved once, up front (rather than inside the
                # save-to-list block below), so the session's "which exact
                # saved connection is this" pointer (custom_connection_key)
                # and the actual saved-list row always agree on the same
                # name - a blank database_name from the frontend falls back
                # to a derived one, and computing the key before that
                # fallback ran would silently point at a connection that
                # was never actually saved under that name.
                db_name_to_save = None
                if new_db_url and (is_custom or (new_db_url not in preset_urls)):
                    db_name_to_save = new_db_name
                    if not db_name_to_save:
                        if new_db_type == 'bigquery':
                            db_name_to_save = new_db_config.get("dataset") or "Custom BigQuery"
                        elif new_db_type == 'snowflake':
                            db_name_to_save = new_db_config.get("database") or "Custom Snowflake"
                        elif new_db_type == 'databricks':
                            db_name_to_save = new_db_config.get("http_path") or "Custom Databricks"
                        elif new_db_type == 'oracle':
                            db_name_to_save = (
                                new_db_config.get("service_name") or new_db_config.get("sid")
                                or "Custom Oracle"
                            )
                        elif new_db_type == 'redshift':
                            db_name_to_save = new_db_config.get("database") or "Custom Redshift"
                        else:
                            try:
                                parsed = urlparse(new_db_url)
                                dbname = parsed.path.lstrip('/')
                                if '?' in dbname:
                                    dbname = dbname.split('?')[0]
                                db_name_to_save = dbname or "Custom"
                            except Exception:
                                db_name_to_save = "Custom"

                # "" (not None) whenever the active connection isn't a
                # custom one, so set_session actually clears any
                # previously-pinned key rather than leaving a stale one
                # behind from before the user switched to a preset.
                active_connection_key = (
                    compute_connection_key(db_name_to_save, new_db_url, _credential_for_key(new_db_config))
                    if is_custom and new_db_url else ""
                )

                state_store.set_session(
                    user_identity, new_db_url, new_auto_sql_execute,
                    db_type=new_db_type, db_config=new_db_config, is_custom=is_custom,
                    custom_connection_key=active_connection_key,
                )
                if db_name_to_save is not None:
                    state_store.set_db_connections(
                        user_identity, db_name_to_save, new_db_type, new_db_url,
                        db_config=new_db_config, custom_databases=merged_custom_databases,
                        connection_key=(active_connection_key or None),
                    )
            else:
                state_store.set_session(
                    user_identity, DEFAULT_CONN, db_type='postgres', db_config={}, is_custom=False,
                    custom_connection_key="",
                )

    session_data = state_store.get_session(user_identity)
    active_conn_str = session_data["database_url"]
    active_db_type = session_data.get("database_type") or "postgres"
    active_db_config = session_data.get("database_config") or {}
    auto_sql_execute = session_data["auto_sql_execute"]

    # Anonymous (Cloud Run, signed-out) users can now save their own custom
    # connections too (see the POST handling above), and every anonymous
    # visitor already has their own fully isolated "anonymous:<session_id>"
    # identity at the state_store layer (see ANONYMOUS_USER_ID_PREFIX in
    # auth.py) - there's deliberately no is_authenticated gate here
    # anymore, mirroring history_routes.py's reasoning for translation
    # history: it would only be rejecting someone from seeing their own
    # already-isolated custom connections.
    custom_databases = state_store.get_db_connections(user_identity)  # credentials stripped
    # Must be whichever saved custom connection is actually active, not
    # just custom_databases[0] (fine back when a user could only ever
    # save one) or a plain URL match (ambiguous once multiple saved
    # connections can share a URL - e.g. two BigQuery connections on
    # the same project/dataset with different service-account keys;
    # see compute_connection_key's docstring in state_store.py).
    # session_data's custom_connection_key is the precise pointer set
    # at save time; only fall back to URL matching for a session saved
    # before that field existed, so an already-active custom connection
    # doesn't just appear unselected the first time this loads after
    # upgrading.
    active_custom_key = session_data.get("custom_connection_key") or ""
    active_custom_db = None
    if active_custom_key:
        active_custom_db = next(
            (db for db in custom_databases if db.get("connection_key") == active_custom_key), None
        )
    else:
        active_custom_db = next(
            (db for db in custom_databases if db.get("url") == active_conn_str), None
        )
    user_custom_name = active_custom_db["name"] if active_custom_db else None
    user_custom_url = active_custom_db["url"] if active_custom_db else None
    active_custom_connection_key = active_custom_db.get("connection_key", "") if active_custom_db else ""
    # Whether the currently active connection is authenticating with its
    # own pasted service-account key, as opposed to this app's ambient
    # credentials (ADC) - surfaced so the frontend can tell the user
    # which one is actually in effect, rather than leaving that
    # invisible once the key itself is (correctly) never sent back. See
    # state_store.get_db_connections' has_custom_credentials docstring.
    active_uses_custom_credentials = bool(active_custom_db.get("has_custom_credentials")) if active_custom_db else False

    # Admin-configured presets are redacted to name/type only for anonymous
    # users UNCONDITIONALLY - CONFIGURED_DBS entries embed the admin's own
    # real connection strings and, for dialects with no ambient identity
    # (Snowflake/Databricks/Oracle), their plaintext credentials too (see
    # the preset-copying code above), regardless of what the anonymous
    # visitor's OWN active connection happens to be right now. This is
    # deliberately independent of active_is_custom_out below - a user's own
    # custom connection is never a secret from them, but another (admin's)
    # preset's credentials always are.
    if IS_CLOUD_RUN and not is_authenticated:
        configured_dbs = [
            {"name": db.get("name"), "type": db.get("type", "postgres")}
            for db in CONFIGURED_DBS
        ]
    else:
        configured_dbs = CONFIGURED_DBS

    if IS_CLOUD_RUN and not is_authenticated and not session_data.get("is_custom"):
        # Anonymous users may still open the DB config dialog and switch
        # between admin-configured presets, but must never see a PRESET's
        # actual connection string, since that embeds credentials the
        # admin configured, not the anonymous visitor themselves. The
        # currently active one is identified by array index
        # (active_preset_index) rather than by URL, matched here
        # server-side against the real (never-exposed) active_conn_str -
        # the frontend never learns the actual string.
        # This branch only applies when the active connection IS a preset
        # (session_data["is_custom"] is false) - when an anonymous user's
        # active connection is instead their own self-supplied custom one,
        # the `else` branch below runs for them too, exactly like an
        # authenticated user, since there's no credential of theirs being
        # hidden from them (only configured_dbs above stays redacted for
        # them either way, since that's about OTHER people's presets, not
        # their own active connection).
        active_preset_index = next(
            (i for i, db in enumerate(CONFIGURED_DBS) if db.get("url") == active_conn_str),
            None,
        )
        active_preset = (
            CONFIGURED_DBS[active_preset_index] if active_preset_index is not None
            else (CONFIGURED_DBS[0] if CONFIGURED_DBS else None)
        )
        db_name = active_preset["name"] if active_preset else "Database"
        username = ""
        active_conn_str_out = ""
        active_db_type_out = ""
        active_is_custom_out = False
    else:
        active_preset_index = None
        db_name, username = "Unknown", "Unknown"
        backend = None
        conn = None
        try:
            descriptor = session_to_descriptor(session_data)
            backend = get_backend(descriptor)
            conn = backend.connect(descriptor)
            db_name, username = backend.identity_label(conn)
        except Exception:
            logger.exception("Error fetching connection info")
        finally:
            if conn and backend:
                backend.close(conn)
        active_conn_str_out = active_conn_str
        active_db_type_out = active_db_type
        # Whether the active connection was explicitly selected as a saved
        # custom connection, as opposed to a preset - lets the frontend break
        # the tie when a custom connection's URL happens to collide with a
        # preset's (see the comment on active_custom_db above); URL equality
        # alone can't distinguish "the preset" from "my custom connection
        # that happens to point at the same database".
        active_is_custom_out = bool(session_data.get("is_custom"))

    resp = jsonify({
        'auth_enabled': AUTH_ENABLED,
        'google_client_id': os.getenv("GOOGLE_CLIENT_ID"),
        'session_id': session_id,
        'user_id': user_identity,
        'authenticated': is_authenticated,
        'is_cloud_run': IS_CLOUD_RUN,
        'configured_databases': configured_dbs,
        'active_preset_index': active_preset_index,
        'default_database_url': DEFAULT_CONN if (not IS_CLOUD_RUN or is_authenticated) else "",
        'active_database_url': active_conn_str_out,
        'active_database_type': active_db_type_out,
        'active_is_custom': active_is_custom_out,
        'active_custom_connection_key': active_custom_connection_key,
        'active_uses_custom_credentials': active_uses_custom_credentials,
        'active_database_project_id': active_db_config.get("project_id", "") if active_db_type_out == "bigquery" else "",
        'active_database_dataset': active_db_config.get("dataset", "") if active_db_type_out == "bigquery" else "",
        'active_database_account': active_db_config.get("account", "") if active_db_type_out == "snowflake" else "",
        'active_database_warehouse': active_db_config.get("warehouse", "") if active_db_type_out == "snowflake" else "",
        'active_database_snowflake_database': active_db_config.get("database", "") if active_db_type_out == "snowflake" else "",
        'active_database_schema': active_db_config.get("schema", "") if active_db_type_out in ("snowflake", "databricks", "oracle", "redshift") else "",
        'active_database_role': active_db_config.get("role", "") if active_db_type_out == "snowflake" else "",
        'active_database_server_hostname': active_db_config.get("server_hostname", "") if active_db_type_out == "databricks" else "",
        'active_database_http_path': active_db_config.get("http_path", "") if active_db_type_out == "databricks" else "",
        'active_database_catalog': active_db_config.get("catalog", "") if active_db_type_out == "databricks" else "",
        'active_database_host': active_db_config.get("host", "") if active_db_type_out in ("oracle", "redshift") else "",
        'active_database_port': active_db_config.get("port", "") if active_db_type_out in ("oracle", "redshift") else "",
        'active_database_service_name': active_db_config.get("service_name", "") if active_db_type_out == "oracle" else "",
        'active_database_sid': active_db_config.get("sid", "") if active_db_type_out == "oracle" else "",
        'active_database_oracle_user': active_db_config.get("user", "") if active_db_type_out == "oracle" else "",
        'active_database_ssl': bool(active_db_config.get("ssl")) if active_db_type_out == "oracle" else False,
        # Redshift's "host"/"port"/"schema" reuse Oracle's already-generic
        # field names above (same shape, no need for parallel ones), but
        # "database" and "user" get their own dialect-specific fields -
        # Oracle's own "service_name"/"sid"/"oracle_user" don't map cleanly
        # onto Redshift's plain "database"/"user" (Redshift's "user" isn't
        # scoped ambiguously the way Oracle's needed its own field name to
        # avoid colliding with a future generic "active_database_user").
        'active_database_redshift_database': active_db_config.get("database", "") if active_db_type_out == "redshift" else "",
        'active_database_redshift_user': active_db_config.get("user", "") if active_db_type_out == "redshift" else "",
        'custom_database_name': user_custom_name or "",
        'custom_database_url': user_custom_url or "",
        'custom_databases': custom_databases or [],
        'gemini_preset_keys': PRESET_MODELS,
        'models': PRESET_MODELS,
        'auto_sql_execute': auto_sql_execute,
        'database_name': db_name,
        'username': username
    })
    return apply_session_cookie(resp, session_id)