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
"schema": "..."}, {"type": "oracle", "url": "oracle://<host>:<port>/
<service_name-or-sid>", "host": "...", "port": 1521, "service_name": "..."
(or "sid" instead), "user": "...", "password": "...", "schema": "..."},
{"type": "redshift", "url": "redshift://<host>:<port>/<database>",
"host": "...", "port": 5439, "database": "...", "user": "...",
"password": "...", "schema": "..."}, or {"type": "mssql", "url":
"mssql://<host>:<port>/<database>", "host": "...", "port": 1433,
"database": "...", "user": "...", "password": "...", "schema": "...",
"encrypt": true} - see db.py's resolve_active_descriptor / backends/base.py's
module docstring / backends/bigquery.py's, backends/snowflake.py's,
backends/databricks.py's, backends/oracle.py's, backends/redshift.py's, and
backends/mssql.py's module docstrings for what billing_project_id is
and why it's not just project_id, for Snowflake's two mutually-exclusive
auth methods, and for why Databricks/Oracle/Redshift/SQL Server (like
Snowflake) only support one explicit credential shape (a Personal Access
Token for Databricks, plain username/password for the other three - no
wallet/mTLS/IAM-temp-credentials yet) rather than any ambient identity.
credentials_json (BigQuery), password/private_key/private_key_passphrase
(Snowflake), access_token (Databricks), and password (Oracle/Redshift/SQL
Server - the same field name Postgres's URL-embedded password plays, but
standalone here since none of the three have a single connection-string
url of their own) are the fields that must never round-trip back to the
frontend once saved (see state_store.get_db_connections' include_credentials
param and its _CREDENTIAL_CONFIG_FIELDS); _resolve_bigquery_credentials/
_resolve_snowflake_credentials/_resolve_databricks_credentials/
_resolve_oracle_credentials/_resolve_redshift_credentials/
_resolve_mssql_credentials below are what let a user re-select or rename
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
file (see that module's DATABASE_PRESETS_FILE comment) - resolved fresh
from CONFIGURED_DBS by db.py's resolve_active_descriptor every time a
preset connection is actually used, rather than ever being copied into
or persisted on the session itself (see db.py's module docstring). A
user's own custom
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
app_config.py's presets file, resolved fresh from CONFIGURED_DBS whenever
that preset is actually used (never persisted on the session), the same
way a Snowflake preset's password/private_key would be if Snowflake had
preset support (it doesn't, yet - see app_config.py's
DATABASE_PRESETS_FILE comment).

Oracle is the same "no ADC-equivalent ambient identity, own preset
credential" shape as Databricks - an admin-configured Oracle preset
carries its own explicit password right in app_config.py's presets file,
resolved fresh from CONFIGURED_DBS whenever that preset is actually used
(never persisted on the session). Unlike every other structured (non-URL)
dialect here, Oracle's
"schema" descriptor field isn't a separate namespace within the connected
database the way BigQuery's dataset/Snowflake's-or-Databricks' schema is -
it's actually the *user/owner* objects belong to, and querying a different
one than the connecting user requires an explicit ALTER SESSION SET
CURRENT_SCHEMA once connected (see backends/oracle.py's module docstring
for the identifier-validation reasoning behind that).

Redshift is the same "no ADC-equivalent ambient identity, own preset
credential" shape as Oracle/Databricks - an admin-configured Redshift
preset carries its own explicit password right in app_config.py's presets
file, resolved fresh from CONFIGURED_DBS whenever that preset is actually
used (never persisted on the session). Unlike Oracle, Redshift's "schema" descriptor field really is a
separate Postgres-style namespace (not a stand-in for a user) - see
backends/redshift.py's module docstring. Also unlike Oracle, there's no
"ssl" descriptor field/opt-in flag: Redshift connections always require
TLS (see backends/redshift.py's connect()), so it's simply always on
rather than a per-connection choice.

SQL Server ("mssql") is the same "no ADC-equivalent ambient identity, own
preset credential" shape as Oracle/Databricks/Redshift - an admin-
configured SQL Server preset carries its own explicit password right in
app_config.py's presets file, resolved fresh from CONFIGURED_DBS whenever
that preset is actually used (never persisted on the session). Like
Redshift's, its "schema" descriptor field is a real separate namespace
(SQL Server's default is "dbo") rather than a stand-in for a user the way
Oracle's is - but unlike Oracle's ALTER SESSION or Redshift's SET
search_path, there's no session-level statement backends/mssql.py's
connect() can issue to apply it (see that module's docstring), so it's
merely carried through to the backend rather than acted on here. Like
Oracle's "ssl", SQL Server has its own opt-in "encrypt" descriptor field
(bool) - defaulting to on when absent, since Azure SQL Database requires
encryption and simply fails outright without it, a stricter default than
Oracle's own "off unless requested."
"""

import json
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify

from app_config import (
    CONFIGURED_DBS, DEFAULT_PRESET_ID, PRESET_MODELS,
    AUTH_ENABLED, IS_CLOUD_RUN, state_store, logger,
)
import os
from auth import (
    get_or_create_session_id, get_current_user_identity, apply_session_cookie,
    is_anonymous_user,
)
from db import get_conn_identifier, resolve_active_descriptor
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


def _mssql_url(host, port, database):
    """Synthetic, non-secret identifier for a SQL Server connection - same
    role _bigquery_url/_snowflake_url/_databricks_url/_oracle_url/
    _redshift_url play (schema-cache key, preset/custom-connection
    matching, display), but is never a credential."""
    return f"mssql://{host}:{port}/{database}"


def _credential_for_key(config):
    """Whichever single credential value `config` carries - BigQuery's
    credentials_json, Snowflake's password/private_key, Databricks'
    access_token, or Oracle's/Redshift's/SQL Server's password - for
    folding into compute_connection_key()'s hash at the one call site
    (below, in handle_config) that has to work generically across every
    connection type rather than inside a type-specific branch that already
    knows which field it means. Mutually exclusive by connection type (a
    Postgres/BigQuery config never has "password"/"private_key"/
    "access_token" set, and so on - note Postgres/MySQL's own URL-embedded
    password never lands in `config` at all, only Oracle's/Redshift's/SQL
    Server's standalone "password" field does, so there's no collision
    there either), so a
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


def _resolve_mssql_credentials(user_identity, host, port, database, provided_password, name=None):
    """Returns the password to persist for a SQL Server connection -
    mirrors _resolve_redshift_credentials' role (letting a user re-select
    or rename an already-saved connection without re-entering its
    credential every time it's touched). Same single-credential-shape
    simplicity as Databricks/Oracle/Redshift - this first pass is plain
    SQL Login (username/password) only, no Windows/Azure AD auth - see
    backends/mssql.py's module docstring."""
    if provided_password:
        return provided_password
    target_url = _mssql_url(host, port, database)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [db for db in existing if db.get("type") == "mssql" and db.get("url") == target_url]
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

_CUSTOM_MSSQL_MISSING_FIELDS_ERROR = (
    "Custom SQL Server connections require a host, a database, a user, and "
    "a password. SQL Server has no ambient/shared identity this app can "
    "fall back to - every connection needs its own explicit credential."
)


def _parse_incoming_connection(data, user_identity):
    """Builds (db_type, db_url, db_config, error) from a POST body's
    top-level active-connection fields, for a user's own CUSTOM connection
    only - a preset selection never reaches this function at all anymore
    (see handle_config's unified `preset_id` branch, which resolves a
    preset straight from CONFIGURED_DBS and never touches this code path -
    that's also why there's no `is_custom` parameter here any longer: every
    call site only ever calls this for a custom connection). db_url is
    None if the request didn't supply enough to identify a connection of
    the given type (e.g. a BigQuery selection missing project_id/dataset) -
    callers treat that the same as "no connection change requested".
    `error`, when not None, means this connection is invalid and MUST NOT
    be saved/activated - used for a custom BigQuery connection missing its
    required billing_project_id and/or credentials_json, or a custom
    Snowflake connection missing its required account/user/warehouse/
    database and/or a credential (see module docstring)."""
    db_type = (data.get('database_type') or 'postgres').strip().lower()

    if db_type == 'bigquery':
        project_id = (data.get('project_id') or '').strip()
        dataset = (data.get('dataset') or '').strip()
        if not (project_id and dataset):
            return db_type, None, {}, None
        db_url = _bigquery_url(project_id, dataset)
        db_config = {"project_id": project_id, "dataset": dataset}

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
        return db_type, db_url, db_config, None

    if db_type == 'mssql':
        host = (data.get('host') or '').strip()
        database = (data.get('database') or '').strip()
        user = (data.get('user') or '').strip()
        schema = (data.get('schema') or '').strip()
        raw_port = data.get('port')
        use_encrypt = data.get('encrypt')
        # Same "core identifying fields, nothing inferred" threshold every
        # other structured dialect above uses - a request missing any of
        # these isn't enough to even identify a connection yet (e.g. a
        # fresh blank row), not a validation error - see the docstring
        # above.
        if not (host and database and user):
            return db_type, None, {}, None
        try:
            port = int(raw_port) if raw_port else 1433
        except (TypeError, ValueError):
            port = 1433
        db_url = _mssql_url(host, port, database)
        db_config = {"host": host, "port": port, "database": database, "user": user}
        if schema:
            db_config["schema"] = schema
        if use_encrypt is not None:
            # Unlike every other optional field here, "encrypt" is a
            # meaningful boolean where an explicit False and an absent
            # value are different things (see backends/mssql.py's module
            # docstring - connect() itself defaults to True when this key
            # is missing from the descriptor entirely) - so this only adds
            # the key when the request actually specified one, rather than
            # defaulting it here too, letting the backend's own default
            # be the single place that default lives.
            db_config["encrypt"] = bool(use_encrypt)

        # Same policy as Oracle's/Databricks'/Snowflake's/Redshift's custom
        # connections: every field explicit, nothing inferred, nothing
        # falls back to a shared/app identity or to an admin preset's
        # credential - SQL Server has no ADC-equivalent ambient auth mode
        # to fall back to (see backends/mssql.py's module docstring).
        password = _resolve_mssql_credentials(
            user_identity, host, port, database, data.get('password'),
            name=data.get('database_name'),
        )
        if not password:
            return db_type, db_url, db_config, _CUSTOM_MSSQL_MISSING_FIELDS_ERROR
        db_config["password"] = password
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
    reused as-is for the session's connection_id pointer when this
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
        elif db_type == 'mssql':
            host = (db.get('host') or '').strip()
            database = (db.get('database') or '').strip()
            user = (db.get('user') or '').strip()
            schema = (db.get('schema') or '').strip()
            raw_port = db.get('port')
            use_encrypt = db.get('encrypt')
            if not (host and database and user):
                continue
            try:
                port = int(raw_port) if raw_port else 1433
            except (TypeError, ValueError):
                port = 1433
            url = _mssql_url(host, port, database)
            password = _resolve_mssql_credentials(
                user_identity, host, port, database, db.get('password'),
                name=db.get('name'),
            )
            if not password:
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"host": host, "port": port, "database": database, "user": user}
            if schema:
                config["schema"] = schema
            if use_encrypt is not None:
                config["encrypt"] = bool(use_encrypt)
            config["password"] = password
            name = db.get("name") or database or "Custom SQL Server"
            merged.append({
                "connection_key": compute_connection_key(name, url, password),
                "name": name,
                "type": "mssql",
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

    if request.method == 'POST':
        data = request.get_json() or {}
        new_db_name = data.get('database_name')
        is_custom = bool(data.get('is_custom', False))
        new_auto_sql_execute = data.get('auto_sql_execute')
        if not isinstance(new_auto_sql_execute, bool):
            new_auto_sql_execute = None

        # The saved custom-connections LIST and "which connection (if any)
        # is active this request" are independent concerns - a request can
        # touch one, the other, both, or neither (e.g. renaming a saved
        # custom connection without switching the active selection away
        # from a preset). Parsed up front, unconditionally, rather than
        # nested inside whichever active-connection branch below happens to
        # apply - `custom_list_saved` (below) tracks whether one of those
        # branches already folded this into its own state_store call, so
        # the trailing block at the end doesn't double up on it.
        merged_custom_databases = _parse_incoming_custom_databases(
            data.get('custom_databases'), user_identity
        )
        custom_list_saved = False

        preset_id = data.get('preset_id') if not is_custom else None
        preset = next((db for db in CONFIGURED_DBS if db.get("id") == preset_id), None) if preset_id else None

        if preset is not None:
            # A preset selection - anonymous and signed-in users alike now
            # identify a preset purely by its stable, non-secret "id" (see
            # app_config.py's DATABASE_PRESETS_FILE comment), never by
            # resending its own fields/credentials. Nothing about a
            # preset's actual connection details ever needs to flow through
            # this request at all - db.py's resolve_active_descriptor
            # resolves them fresh from CONFIGURED_DBS every time the
            # connection is actually used, so the session only ever
            # remembers the preset's id.
            state_store.set_session(
                user_identity, connection_id=preset["id"], is_custom=False,
                auto_sql_execute=new_auto_sql_execute,
            )
        elif is_custom:
            new_db_type, new_db_url, new_db_config, connection_error = _parse_incoming_connection(
                data, user_identity
            )
            if connection_error:
                # Reject outright, before touching state_store - a
                # half-valid save here (e.g. persisting project_id/dataset
                # but silently dropping billing_project_id) would just
                # surface as a confusing BigQuery 403 later at query time
                # instead of a clear, immediate error now.
                resp = jsonify({'success': False, 'error': connection_error})
                return apply_session_cookie(resp, session_id), 400

            if new_db_url or new_auto_sql_execute is not None:
                if new_db_url:
                    prior_descriptor, _prior_missing = resolve_active_descriptor(
                        state_store.get_session(user_identity), user_identity
                    )
                    if new_db_url != prior_descriptor.get("url"):
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
                # saved connection is this" pointer (connection_id) and the
                # actual saved-list row always agree on the same name - a
                # blank database_name from the frontend falls back to a
                # derived one, and computing the key before that fallback
                # ran would silently point at a connection that was never
                # actually saved under that name.
                db_name_to_save = None
                if new_db_url:
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
                        elif new_db_type == 'mssql':
                            db_name_to_save = new_db_config.get("database") or "Custom SQL Server"
                        else:
                            try:
                                parsed = urlparse(new_db_url)
                                dbname = parsed.path.lstrip('/')
                                if '?' in dbname:
                                    dbname = dbname.split('?')[0]
                                db_name_to_save = dbname or "Custom"
                            except Exception:
                                db_name_to_save = "Custom"

                # "" (not None) whenever no connection was actually
                # identified this request, so set_session leaves is_custom
                # true but with a blank connection_id rather than pinning a
                # stale one.
                active_connection_key = (
                    compute_connection_key(db_name_to_save, new_db_url, _credential_for_key(new_db_config))
                    if new_db_url else ""
                )

                state_store.set_session(
                    user_identity, connection_id=active_connection_key, is_custom=True,
                    auto_sql_execute=new_auto_sql_execute,
                )
                if db_name_to_save is not None:
                    state_store.set_db_connections(
                        user_identity, db_name_to_save, new_db_type, new_db_url,
                        db_config=new_db_config, custom_databases=merged_custom_databases,
                        connection_key=(active_connection_key or None),
                    )
                    custom_list_saved = True
        elif new_auto_sql_execute is not None:
            # Neither a preset nor a custom connection was actively
            # selected in this request (e.g. only the auto-execute toggle
            # changed) - leave the active connection exactly as it is.
            # There's no hardcoded default to reset it to here anymore the
            # way there used to be: a blank/never-set connection_id already
            # resolves to the app default on its own - see db.py's
            # resolve_active_descriptor.
            state_store.set_session(user_identity, auto_sql_execute=new_auto_sql_execute)

        if not custom_list_saved and merged_custom_databases is not None:
            state_store.set_db_connections(
                user_identity, None, None, None, custom_databases=merged_custom_databases
            )

    session_data = state_store.get_session(user_identity)
    # The FULL descriptor - including any credentials - is resolved fresh,
    # right now, from CONFIGURED_DBS/db_connections (see db.py's module
    # docstring); session_data itself holds only the identity reference
    # (is_custom, connection_id), never a copy of the connection's own
    # details. `connection_missing` is true when connection_id was set to
    # something but it no longer resolves to anything real - the preset
    # was removed/renamed, or the saved custom connection was deleted -
    # in which case active_descriptor is already the app default, and the
    # active_connection_missing* fields below tell the frontend so it can
    # warn the user instead of silently showing the default as if it were
    # what they'd actually picked.
    active_descriptor, connection_missing = resolve_active_descriptor(session_data, user_identity)
    active_conn_str = active_descriptor.get("url")
    active_db_type = active_descriptor.get("type") or "postgres"
    active_db_config = {k: v for k, v in active_descriptor.items() if k not in ("type", "url")}
    auto_sql_execute = session_data["auto_sql_execute"]

    active_connection_missing_message = ""
    if connection_missing:
        active_connection_missing_message = (
            "Your previously selected custom connection is no longer available "
            "(it may have been deleted) - showing the default connection instead."
            if session_data.get("is_custom") else
            "Your previously selected database preset is no longer available "
            "(it may have been removed or renamed) - showing the default "
            "connection instead."
        )

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
    # session_data's connection_id (when is_custom) IS that connection's
    # connection_key directly - no URL-matching fallback needed the way
    # there used to be for a session saved before that field existed,
    # since the state_store migration (see state_store.py) backfills
    # connection_id for every pre-existing session up front.
    active_custom_key = session_data.get("connection_id") if session_data.get("is_custom") else ""
    active_custom_db = next(
        (db for db in custom_databases if db.get("connection_key") == active_custom_key), None
    ) if active_custom_key else None
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

    # Admin-configured presets are always redacted to name/type only, for
    # EVERY visitor - authenticated or anonymous, on Cloud Run or running
    # locally, no exception. CONFIGURED_DBS entries embed the admin's own
    # real connection strings and, for dialects with no ambient identity
    # (Snowflake/Databricks/Oracle/Redshift), their plaintext credentials
    # too - those were never any individual visitor's own secret to see
    # just because they happened to sign in (so this doesn't key off
    # is_authenticated), and they're not tied to the Cloud Run deployment
    # either (so this doesn't key off IS_CLOUD_RUN anymore, and neither do
    # the other two redaction spots below - active-connection display and
    # default_database_url): a presets file checked into the same repo and
    # run locally carries exactly the same secret as it does on Cloud Run,
    # and a developer working locally has no more claim to another admin's
    # preset credential than a Cloud Run visitor does. This is deliberately
    # independent of active_is_custom_out below - a user's own custom
    # connection is never a secret from them, but another (admin's)
    # preset's credentials always are, regardless of who's asking or where
    # this is running.
    configured_dbs = [
        {"id": db.get("id"), "name": db.get("name"), "type": db.get("type", "postgres")}
        for db in CONFIGURED_DBS
    ]

    # Which preset (if any) is active - read straight off session_data's
    # own connection_id/is_custom, computed once, unconditionally,
    # regardless of anonymous/authenticated status, since "id" is never a
    # secret and is always safe to send to the frontend. This is what
    # actually lets the UI know which radio to check: matching by URL
    # client-side doesn't work for an anonymous visitor (who never receives
    # real preset URLs - see configured_dbs above) and matching by array
    # position doesn't survive the admin reordering/adding/removing presets
    # in DATABASE_PRESETS_FILE between deployments - "id" has neither
    # problem. A brand-new session (blank connection_id, not custom) falls
    # back to DEFAULT_PRESET_ID, matching the app's own default connection
    # (see app_config.py).
    if session_data.get("is_custom"):
        active_preset_id = None
    else:
        active_preset_id = session_data.get("connection_id") or DEFAULT_PRESET_ID

    if not session_data.get("is_custom"):
        # Any visitor - authenticated or anonymous, on Cloud Run or running
        # locally - may open the DB config dialog and switch between
        # admin-configured presets, but must never see a PRESET's actual
        # connection string: that embeds credentials the admin configured,
        # not something the requesting user supplied themselves, so being
        # logged in doesn't earn it either, and neither does running the
        # server on a laptop instead of Cloud Run - active_preset_id above
        # (safe, opaque) is what lets them know which one is checked
        # instead, and this branch skips the real connect()/identity_label()
        # call entirely rather than just hiding its result, so a preset's
        # host/user identity string never even gets fetched for display.
        # This branch only applies when the active connection IS a preset
        # (session_data["is_custom"] is false) - when the active connection
        # is instead the visitor's own self-supplied custom one, the `else`
        # branch below always runs, since there's no credential of theirs
        # being hidden from them (only configured_dbs above stays redacted
        # regardless, since that's about OTHER people's presets, not their
        # own active connection).
        preset_for_display = (
            next((db for db in CONFIGURED_DBS if db.get("id") == active_preset_id), None)
            or (CONFIGURED_DBS[0] if CONFIGURED_DBS else None)
        )
        db_name = preset_for_display["name"] if preset_for_display else "Database"
        username = ""
        active_conn_str_out = ""
        active_db_type_out = ""
        active_is_custom_out = False
    else:
        db_name, username = "Unknown", "Unknown"
        backend = None
        conn = None
        try:
            backend = get_backend(active_descriptor)
            conn = backend.connect(active_descriptor)
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
        'active_preset_id': active_preset_id,
        'active_connection_missing': connection_missing,
        'active_connection_missing_message': active_connection_missing_message,
        # The real default connection string is never sent to ANY visitor,
        # anywhere - it's an admin-configured preset's own credential like
        # any other (see configured_dbs above), not tied to whether this
        # particular visitor happens to be signed in or to which
        # environment the server happens to be running in.
        'default_database_url': "",
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
        'active_database_schema': active_db_config.get("schema", "") if active_db_type_out in ("snowflake", "databricks", "oracle", "redshift", "mssql") else "",
        'active_database_role': active_db_config.get("role", "") if active_db_type_out == "snowflake" else "",
        'active_database_server_hostname': active_db_config.get("server_hostname", "") if active_db_type_out == "databricks" else "",
        'active_database_http_path': active_db_config.get("http_path", "") if active_db_type_out == "databricks" else "",
        'active_database_catalog': active_db_config.get("catalog", "") if active_db_type_out == "databricks" else "",
        'active_database_host': active_db_config.get("host", "") if active_db_type_out in ("oracle", "redshift", "mssql") else "",
        'active_database_port': active_db_config.get("port", "") if active_db_type_out in ("oracle", "redshift", "mssql") else "",
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
        # SQL Server's "host"/"port"/"schema" also reuse the generic fields
        # above; "database"/"user" get their own fields for the same reason
        # Redshift's do, and "encrypt" gets its own boolean field mirroring
        # Oracle's "ssl" one (a different field name, not reused, since
        # they're independent booleans on independent dialects).
        'active_database_mssql_database': active_db_config.get("database", "") if active_db_type_out == "mssql" else "",
        'active_database_mssql_user': active_db_config.get("user", "") if active_db_type_out == "mssql" else "",
        'active_database_mssql_encrypt': bool(active_db_config.get("encrypt")) if active_db_type_out == "mssql" else False,
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