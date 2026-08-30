"""
config_routes.py

The /api/config endpoint: reads/writes the current session's active
database (dialect-aware - Postgres, MySQL, BigQuery, or Snowflake), its
"Automatic SQL Execution" preference, and its LLM provider/model
selection, and reports back everything the frontend needs to render its
DB/session/model UI. Unlike this module's original design (every LLM
provider's preset model list used to be a server-configured, Gemini-only
value the frontend could pass per-request to /api/translate but never
persisted anywhere), the active llm_provider/llm_model IS now persisted on
the session - see state_store.py's get_session/set_session and
translate_routes.py's list_llm_providers_info() - exactly the same
persistence model the active database connection already uses, so a
user's model choice survives page reloads and follows them the same way
their DB connection does.

Connections are represented as descriptors: {"type": "postgres", "url":
"...", "ca_cert_pem": "..."} (MySQL is identical in shape, ca_cert_pem
included - {"type": "mysql", "url": "mysql://...", "ca_cert_pem": "..."} -
see backends/mysql.py's module docstring), {"type":
"bigquery", "url": None, "project_id": "...", "dataset": "...",
"credentials_json": "...", "billing_project_id": "..."}, {"type":
"snowflake", "url": None, "account": "...", "user": "...", "warehouse":
"...", "database": "...", "schema": "...", "role": "...", "password":
"..."} (or "private_key"/"private_key_passphrase" instead of
"password"), {"type": "databricks", "url": None, "server_hostname": "...",
"http_path": "...", "access_token": "...", "catalog": "...", "schema":
"..."}, {"type": "oracle", "url": None, "host": "...", "port": 1521,
"service_name": "..." (or "sid" instead), "user": "...", "password":
"...", "schema": "..."}, {"type": "redshift", "url": None, "host": "...",
"port": 5439, "database": "...", "user": "...", "password": "...",
"schema": "..."}, {"type": "mssql", "url": None, "host": "...", "port":
1433, "database": "...", "user": "...", "password": "...", "schema":
"...", "encrypt": true}, {"type": "sheets", "url": None,
"spreadsheet_id": "...", "tab_name": "...", "credentials_json": "..."
(optional)}, or {"type": "MongoDB", "url": "mongodb://...", "database":
"...", "user": "...", "password": "..."}. Postgres/MySQL/MongoDB Atlas
SQL are the only three dialects with a real, driver-parsed url of their
own; for the other 7, "url" is always None for a CUSTOM connection -
genuinely absent, not just blank, all the way down to the state_store row
(this module builds a purely internal, never-stored, never-returned
identity string instead for hashing/matching purposes - see the
_xxx_identity functions and compute_connection_key's call sites below) -
though it may still be a non-blank, synthetic, informational string for an
admin-configured preset (CONFIGURED_DBS, built independently in
app_config.py, out of scope for this distinction). MongoDB is a hybrid of
the two patterns: its "url" is real and stored/returned as-is (like
Postgres/MySQL - no separate identity function needed, see the mongodb
branches below), but it ALSO carries database/user/password as separate
config fields (like the other 7) rather than packing them into the url
string - see backends/mongodb_sql.py's module docstring for why (that
dialect's actual ODBC connection string needs a "Driver={...}" clause and
a "Uri=" key name neither of which are this app's concern to ask a user
for; the backend's connect() reassembles all of it). The GET /api/config
response still always sends
'active_database_url'/'custom_database_url' as a string (coalescing None
to "" right at that boundary) - only the internal representation and
storage are None now, not the wire format signed-in/anonymous clients
already depend on. See db.py's resolve_active_descriptor / backends/base.py's
module docstring / backends/bigquery.py's, backends/snowflake.py's,
backends/databricks.py's, backends/oracle.py's, backends/redshift.py's, and
backends/mssql.py's module docstrings for what billing_project_id is
and why it's not just project_id, for Snowflake's two mutually-exclusive
auth methods, and for why Databricks/Oracle/Redshift/SQL Server (like
Snowflake) only support one explicit credential shape (a Personal Access
Token for Databricks, plain username/password for the other three - no
wallet/mTLS/IAM-temp-credentials yet) rather than any ambient identity.
MongoDB Atlas SQL is the same plain-username/password shape too, with
just as little ambient-identity fallback available - see backends/
mongodb_sql.py's module docstring.
credentials_json (BigQuery, and optionally Sheets - see below), password/
private_key/private_key_passphrase (Snowflake), access_token (Databricks),
and password (Oracle/Redshift/SQL Server/MongoDB Atlas SQL - the same
field name Postgres's URL-embedded password plays, but standalone here
since none of these four pack their password into their url the way
Postgres/MySQL do) are the fields that must
never round-trip back to the frontend once saved (see
state_store.get_db_connections' include_credentials param and its
_CREDENTIAL_CONFIG_FIELDS); _resolve_bigquery_credentials/
_resolve_snowflake_credentials/_resolve_databricks_credentials/
_resolve_oracle_credentials/_resolve_redshift_credentials/
_resolve_mssql_credentials/_resolve_sheets_credentials/
_resolve_mongodb_sql_credentials below are what let a
user re-select or rename a saved connection, or just switch back to it,
without re-entering its credential every time. billing_project_id is NOT a credential (it's just a
project id string) and always round-trips to the frontend as-is - see
get_db_connections' _strip_credentials, which only strips the fields in
_CREDENTIAL_CONFIG_FIELDS. Postgres's and MySQL's optional ca_cert_pem is
the same way - it's a CA certificate (PEM text), used to populate libpq's
"sslrootcert" (Postgres) or an ssl.SSLContext's trusted CA (MySQL) for a
"sslmode=verify-ca"/"verify-full" connection (see backends/postgres.py's
and backends/mysql.py's module docstrings), and a CA certificate is
public information, not a secret, so it's likewise never stripped and
always round-trips as-is, unlike password/credentials_json/private_key/
access_token above.

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

Google Sheets ("sheets") is credential-OPTIONAL, unlike every other
dialect above (which are either always credentialed or never are): a
spreadsheet genuinely shared as "Anyone with the link can view" needs
nothing, since this app has no Google identity of its own to act on a
signed-in user's behalf (see auth.py - only ID tokens are ever verified,
never an OAuth access token). But a connection MAY also carry an optional
credentials_json (a pasted service-account key, same field/shape as
BigQuery's) for reaching a PRIVATE spreadsheet the sheet's owner has
explicitly shared with that service account's email - see
backends/sheets.py's module docstring for the full credential model and a
flagged caveat about this mechanism's verification status. Because of
that, _parse_incoming_connection's and _parse_incoming_custom_databases'
sheets branches still follow the unconditional-success shape Postgres/
MySQL's URL-based branches use (a missing credential is never an error -
most Sheets connections legitimately have none), but they now ALSO call
_resolve_sheets_credentials (mirroring _resolve_bigquery_credentials'
fresh-wins-else-fall-back-to-saved shape) to fold in an optional
credentials_json when one is provided or already saved.

Separately, a Sheets connection (preset OR custom) that has no
credentials_json at all - explicit or saved - can still reach a private
sheet via a single, app-wide ambient service-account key
(SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE - see backends/sheets.py's module
docstring). That fallback is resolved entirely inside backend.connect(),
never here and never persisted in any saved config, so nothing in this
file needs to know it exists to work correctly - it's mentioned here only
so a reader isn't left wondering how a credential-less-looking descriptor
still reaches a private sheet in practice.
"""

import json
import re
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify

from app_config import (
    CONFIGURED_DBS, DEFAULT_PRESET_ID, MAX_IN_SCOPE_CONNECTIONS,
    AUTH_ENABLED, IS_CLOUD_RUN, state_store,
)
import os
from auth import (
    get_or_create_session_id, get_current_user_identity, apply_session_cookie,
    is_anonymous_user,
)
from db import get_conn_identifier, resolve_active_descriptor
from state_store import compute_connection_key
from sheets_util import extract_spreadsheet_id
import schema_cache
# Read-only reuse of translate_routes.py's HISTORY_MAX_TURNS - the client
# needs the same number that already governs how many past turns
# /api/translate replays to the LLM (see that module's comment on it), so
# its own turn-navigation cap (chatStore in client.js) can match it exactly
# instead of carrying an independent, easy-to-drift hardcoded constant. No
# circular import risk: translate_routes.py doesn't import config_routes.py
# (or anything that transitively does).
from translate_routes import (
    HISTORY_MAX_TURNS, get_llm_provider, list_llm_providers_info,
)

config_bp = Blueprint('config', __name__)

# MAX_IN_SCOPE_CONNECTIONS itself now lives in app_config.py (imported
# above, alongside CONFIGURED_DBS/DEFAULT_PRESET_ID) - see its docstring
# there for why: it's the single cap on how many database connections are
# involved anywhere in the multi-database question-answering feature (both
# how many a user may mark "in scope" here, and how many of those
# in-scope connections one question's Phase A routing may select -
# connection_router.py's select_relevant_connections), no longer two
# separate constants to keep in sync.

# These 7 dialects have no real url of their own - what used to be stored
# as "url" for a CUSTOM connection of one of these types was always a
# synthetic, concatenated string built purely for display/identity
# purposes (see the _xxx_identity functions below), never something a
# backend's connect() actually parsed. Postgres/MySQL are deliberately
# absent from this set: their url is the real, user-typed DSN their
# backend's connect() genuinely parses, so it's still stored as-is.
_STRUCTURED_DIALECTS_WITHOUT_A_REAL_URL = frozenset({
    "bigquery", "snowflake", "databricks", "oracle", "redshift", "mssql", "sheets",
})


def _bigquery_identity(project_id, dataset):
    """Stable, non-secret string identifying a BigQuery CUSTOM connection -
    used only as compute_connection_key()'s hash input and to recognize an
    already-saved connection when a request re-edits it without
    re-pasting its credential (see _resolve_bigquery_credentials below).
    Deliberately NOT a "url" - it's never stored, never returned to the
    frontend, and never a real connection string the backend uses to
    connect (backends/bigquery.py's connect() never reads a "url" field
    at all - only Postgres/MySQL have a real, user-typed connection-string
    url; see this module's docstring). A prior version of this function
    built a synthetic "bigquery://project/dataset"-shaped string that WAS
    stored/returned as a "database_url" field - removed because it wasn't
    a real url, was duplicated near-identically in app_config.py's preset
    loader and client.js, and gave the impression a custom connection's
    identity was somehow url-based when it was always really just these
    two fields."""
    return f"{project_id}\x00{dataset}"


def _snowflake_identity(account, database, schema):
    """Same role _bigquery_identity plays, for Snowflake. Schema is
    optional on a Snowflake connection (omitted = the account's own
    default schema - see backends/snowflake.py's module docstring), so
    it's folded in either way (present or blank) rather than varying the
    number of \\x00-separated parts."""
    return f"{account}\x00{database}\x00{schema or ''}"


def _databricks_identity(server_hostname, http_path):
    """Same role _bigquery_identity/_snowflake_identity play, for
    Databricks."""
    return f"{server_hostname}\x00{http_path}"


def _oracle_identity(host, port, service_name_or_sid):
    """Same role _bigquery_identity/_snowflake_identity/_databricks_identity
    play, for Oracle. `service_name_or_sid` is whichever of
    service_name/sid the caller resolved (service_name preferred - see
    backends/oracle.py's connect())."""
    return f"{host}\x00{port}\x00{service_name_or_sid}"


def _redshift_identity(host, port, database):
    """Same role _bigquery_identity/_snowflake_identity/_databricks_identity/
    _oracle_identity play, for Redshift."""
    return f"{host}\x00{port}\x00{database}"


def _mssql_identity(host, port, database):
    """Same role _bigquery_identity/_snowflake_identity/_databricks_identity/
    _oracle_identity/_redshift_identity play, for SQL Server."""
    return f"{host}\x00{port}\x00{database}"


def _sheets_identity(spreadsheet_id, tab_name):
    """Same role _bigquery_identity/_oracle_identity/etc. play, for Google
    Sheets - see backends/sheets.py's module docstring for the (optional)
    credentials_json this dialect can separately carry."""
    return f"{spreadsheet_id}\x00{tab_name}"


def _resolve_sheets_credentials(user_identity, spreadsheet_id, tab_name, provided_credentials_json, name=None):
    """Returns the credentials_json to persist for a Sheets connection, or
    None. Mirrors _resolve_bigquery_credentials' fresh-wins-else-fall-back-
    to-saved shape, purely so re-selecting/renaming an already-saved
    private-sheet connection doesn't look like "credential removed" and
    silently drop it. UNLIKE _resolve_bigquery_credentials, a None return
    here is NOT an error anywhere it's called - most Sheets connections are
    legitimately credential-less (a public sheet), and that must stay the
    normal, zero-friction case."""
    if provided_credentials_json:
        return provided_credentials_json
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "sheets"
        and (db.get("config") or {}).get("spreadsheet_id") == spreadsheet_id
        and (db.get("config") or {}).get("tab_name") == tab_name
    ]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("credentials_json")
    return (matches[0].get("config") or {}).get("credentials_json")


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
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "bigquery"
        and (db.get("config") or {}).get("project_id") == project_id
        and (db.get("config") or {}).get("dataset") == dataset
    ]
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

    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "snowflake"
        and (db.get("config") or {}).get("account") == account
        and (db.get("config") or {}).get("database") == database
        and (db.get("config") or {}).get("schema") == (schema or None)
    ]
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
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "databricks"
        and (db.get("config") or {}).get("server_hostname") == server_hostname
        and (db.get("config") or {}).get("http_path") == http_path
    ]
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
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "oracle"
        and (db.get("config") or {}).get("host") == host
        and (db.get("config") or {}).get("port") == port
        and (
            (db.get("config") or {}).get("service_name") == service_name_or_sid
            or (db.get("config") or {}).get("sid") == service_name_or_sid
        )
    ]
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
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "redshift"
        and (db.get("config") or {}).get("host") == host
        and (db.get("config") or {}).get("port") == port
        and (db.get("config") or {}).get("database") == database
    ]
    if not matches:
        return None
    if name:
        named_match = next((db for db in matches if db.get("name") == name), None)
        if named_match:
            return (named_match.get("config") or {}).get("password")
    return (matches[0].get("config") or {}).get("password")


def _resolve_mongodb_sql_credentials(user_identity, url, database, provided_password, name=None):
    """Returns the password to persist for a MongoDB Atlas SQL connection -
    mirrors _resolve_redshift_credentials'/_resolve_mssql_credentials' role
    (letting a user re-select or rename an already-saved connection without
    re-entering its credential every time it's touched). Matches on "url"
    directly (not `(db.get("config") or {}).get("url")`) - unlike Oracle/
    Redshift/SQL Server, MongoDB's "url" is a real, top-level field (see
    this module's docstring), not something folded into an internal
    identity string, so matching reads it the same way Postgres/MySQL's
    own url-based matching effectively would if they needed this function
    at all (they don't - their url already carries the credential, so
    there's nothing to separately resolve). "user" deliberately isn't part
    of the match, same omission Oracle/Redshift/SQL Server's matching
    above already makes - name+url+password together (see
    compute_connection_key) are what actually disambiguate two saved
    connections in the rare case two share a url/database."""
    if provided_password:
        return provided_password
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "MongoDB"
        and db.get("url") == url
        and (db.get("config") or {}).get("database") == database
    ]
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
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    matches = [
        db for db in existing if db.get("type") == "mssql"
        and (db.get("config") or {}).get("host") == host
        and (db.get("config") or {}).get("port") == port
        and (db.get("config") or {}).get("database") == database
    ]
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

_CUSTOM_MONGODB_SQL_MISSING_FIELDS_ERROR = (
    "Custom MongoDB Atlas SQL connections require a URI, a database, a "
    "user, and a password. MongoDB Atlas SQL has no ambient/shared "
    "identity this app can fall back to - every connection needs its own "
    "explicit credential."
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
    callers treat that the same as "no connection change requested". For
    the 7 structured (non-"simple URL") dialects, a non-None db_url is a
    stable identity string (built by _bigquery_identity/etc. above) used
    ONLY as compute_connection_key()'s hash input in handle_config below -
    it is NEVER a real url and handle_config blanks it to None before this
    value is persisted or returned to the frontend (those dialects have no
    real connection-string url at all - see this module's docstring).
    Only Postgres/MySQL's db_url is a real, user-typed url that's actually
    stored/returned as-is. `error`, when not None, means this connection
    is invalid and MUST NOT be saved/activated - used for a custom
    BigQuery connection missing its required billing_project_id and/or
    credentials_json, or a custom Snowflake connection missing its
    required account/user/warehouse/database and/or a credential (see
    module docstring)."""
    db_type = (data.get('database_type') or 'postgres').strip().lower()

    if db_type == 'bigquery':
        project_id = (data.get('project_id') or '').strip()
        dataset = (data.get('dataset') or '').strip()
        if not (project_id and dataset):
            return db_type, None, {}, None
        db_url = _bigquery_identity(project_id, dataset)
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
        db_url = _snowflake_identity(account, database, schema)
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
        db_url = _databricks_identity(server_hostname, http_path)
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
        db_url = _oracle_identity(host, port, service_name_or_sid)
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
        db_url = _redshift_identity(host, port, database)
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
        db_url = _mssql_identity(host, port, database)
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

    if db_type == 'sheets':
        spreadsheet_url_or_id = (data.get('spreadsheet_url') or '').strip()
        tab_name = (data.get('tab_name') or '').strip()
        # Same "core identifying fields, nothing inferred" threshold every
        # other structured dialect above uses - a request missing either
        # field isn't enough to even identify a connection yet (e.g. a
        # fresh blank row), not a validation error - see the docstring
        # above.
        if not (spreadsheet_url_or_id and tab_name):
            return db_type, None, {}, None
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url_or_id)
        if not spreadsheet_id:
            return db_type, None, {}, None
        db_url = _sheets_identity(spreadsheet_id, tab_name)
        db_config = {"spreadsheet_id": spreadsheet_id, "tab_name": tab_name}
        # Optional, unlike every other credentialed dialect above: a
        # missing credential here is the normal public-sheet case, not an
        # error - see backends/sheets.py's module docstring. Unconditional
        # error=None regardless, same as the MySQL/default-Postgres branch
        # below, NOT the Oracle/Redshift/Snowflake/Databricks/mssql
        # "resolve or error" shape above.
        credentials_json = _resolve_sheets_credentials(
            user_identity, spreadsheet_id, tab_name, data.get('credentials_json'),
            name=data.get('database_name'),
        )
        if credentials_json:
            db_config["credentials_json"] = credentials_json
        return db_type, db_url, db_config, None

    if db_type == 'mongodb':
        # Unlike Postgres/MySQL, MongoDB's "url" only carries the bare
        # mongodb:// URI - database/user/password are separate structured
        # fields, same shape as Redshift's/SQL Server's above (see this
        # module's docstring). There's no ca_cert_pem-style optional field
        # either: TLS trust for MongoDB Atlas SQL is expressed inside the
        # URI itself (a "?ssl=true" query param), never as a separately
        # pasted CA certificate. See backends/mongodb_sql.py's module
        # docstring for the full shape and why this dialect is read-only.
        url = (data.get('database_url') or '').strip()
        database = (data.get('database') or '').strip()
        user = (data.get('user') or '').strip()
        # Same "core identifying fields, nothing inferred" threshold every
        # other structured dialect above uses - a request missing any of
        # these isn't enough to even identify a connection yet (e.g. a
        # fresh blank row), not a validation error - see the docstring
        # above.
        if not (url and database and user):
            return db_type, None, {}, None
        db_config = {"database": database, "user": user}

        # Same policy as Oracle's/Redshift's/SQL Server's custom
        # connections: every field explicit, nothing inferred, nothing
        # falls back to a shared/app identity or to an admin preset's
        # credential - MongoDB Atlas SQL has no ADC-equivalent ambient
        # auth mode to fall back to (see backends/mongodb_sql.py's module
        # docstring).
        password = _resolve_mongodb_sql_credentials(
            user_identity, url, database, data.get('password'),
            name=data.get('database_name'),
        )
        if not password:
            return 'MongoDB', url, db_config, _CUSTOM_MONGODB_SQL_MISSING_FIELDS_ERROR
        db_config["password"] = password
        # Returns the literal "MongoDB" (not the lowercased db_type just
        # compared above) so the stored/exposed type always matches
        # backends/__init__.py's _BACKENDS dict key exactly. Returns the
        # real url (not None, and not an internal-only identity string) -
        # MongoDB is NOT in _STRUCTURED_DIALECTS_WITHOUT_A_REAL_URL, so
        # this IS what gets persisted/returned as "url" (see this module's
        # docstring).
        return 'MongoDB', url, db_config, None

    if db_type == 'mysql':
        # Same shape as Postgres - a single connection-string URL carries
        # everything (host, credentials, database), so there's no other
        # dialect-specific config to build and no preset-credential
        # copy-over needed (unlike BigQuery/Snowflake) - a MySQL preset's
        # URL already contains its own credentials, same as a Postgres
        # preset's does. See backends/mysql.py's module docstring.
        #
        # ca_cert_pem is the one optional field both this dialect and
        # Postgres share (see backends/mysql.py's and backends/postgres.py's
        # module docstrings) - not a credential, so unlike password/
        # credentials_json above there's no "leave blank to keep the saved
        # one" resolver needed: it's either present in this request or it
        # isn't, same treatment as Oracle's "schema" or Redshift's "schema".
        db_config = {}
        ca_cert_pem = (data.get('ca_cert_pem') or '').strip()
        if ca_cert_pem:
            db_config["ca_cert_pem"] = ca_cert_pem
        return db_type, data.get('database_url'), db_config, None

    # Default / explicit postgres - also the fallback for any unrecognized
    # db_type value, same as before this module supported more than one
    # simple-URL dialect (predates multi-dialect support entirely - see
    # backends/__init__.py's get_backend() docstring for the equivalent
    # default at the dispatch layer).
    db_config = {}
    ca_cert_pem = (data.get('ca_cert_pem') or '').strip()
    if ca_cert_pem:
        db_config["ca_cert_pem"] = ca_cert_pem
    return 'postgres', data.get('database_url'), db_config, None


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
            identity = _bigquery_identity(project_id, dataset)
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
                "connection_key": compute_connection_key(name, identity, credentials_json),
                "name": name,
                "type": "bigquery",
                # None, not "" - BigQuery has no real url of its own (see
                # this module's docstring and _bigquery_identity above).
                # Still present as a key, for shape-parity with Postgres/
                # MySQL rows, which do carry a real one.
                "url": None,
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
            identity = _snowflake_identity(account, database, schema)
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
                "connection_key": compute_connection_key(name, identity, password or private_key),
                "name": name,
                "type": "snowflake",
                "url": None,  # None, not "" - see _snowflake_identity above (and this module's docstring).
                "config": config,
            })
        elif db_type == 'databricks':
            server_hostname = (db.get('server_hostname') or '').strip()
            http_path = (db.get('http_path') or '').strip()
            catalog = (db.get('catalog') or '').strip()
            schema = (db.get('schema') or '').strip()
            if not (server_hostname and http_path):
                continue
            identity = _databricks_identity(server_hostname, http_path)
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
                "connection_key": compute_connection_key(name, identity, access_token),
                "name": name,
                "type": "databricks",
                "url": None,  # None, not "" - see _databricks_identity above (and this module's docstring).
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
            identity = _oracle_identity(host, port, service_name_or_sid)
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
                "connection_key": compute_connection_key(name, identity, password),
                "name": name,
                "type": "oracle",
                "url": None,  # None, not "" - see _oracle_identity above (and this module's docstring).
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
            identity = _redshift_identity(host, port, database)
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
                "connection_key": compute_connection_key(name, identity, password),
                "name": name,
                "type": "redshift",
                "url": None,  # None, not "" - see _redshift_identity above (and this module's docstring).
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
            identity = _mssql_identity(host, port, database)
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
                "connection_key": compute_connection_key(name, identity, password),
                "name": name,
                "type": "mssql",
                "url": None,  # None, not "" - see _mssql_identity above (and this module's docstring).
                "config": config,
            })
        elif db_type == 'sheets':
            spreadsheet_url_or_id = (db.get('spreadsheet_url') or '').strip()
            tab_name = (db.get('tab_name') or '').strip()
            if not (spreadsheet_url_or_id and tab_name):
                # Incomplete - not ready to save yet (see docstring above).
                continue
            spreadsheet_id = extract_spreadsheet_id(spreadsheet_url_or_id)
            if not spreadsheet_id:
                continue
            identity = _sheets_identity(spreadsheet_id, tab_name)
            name = db.get("name") or tab_name or "Custom Sheet"
            credentials_json = _resolve_sheets_credentials(
                user_identity, spreadsheet_id, tab_name, db.get('credentials_json'),
                name=name,
            )
            config = {"spreadsheet_id": spreadsheet_id, "tab_name": tab_name}
            if credentials_json:
                config["credentials_json"] = credentials_json
            # credentials_json folded into the key (may be None) - unlike
            # the unconditional None this used to pass unconditionally,
            # two connections that differ only by credential (same
            # spreadsheet/tab, different service-account key) now get
            # distinct connection_keys instead of colliding and silently
            # overwriting each other - same reasoning as the password-
            # folding calls above.
            merged.append({
                "connection_key": compute_connection_key(name, identity, credentials_json),
                "name": name,
                "type": "sheets",
                "url": None,  # None, not "" - see _sheets_identity above (and this module's docstring).
                "config": config,
            })
        elif db_type == 'mongodb':
            # Unlike Postgres/MySQL just below, MongoDB's "url" only
            # carries the bare mongodb:// URI - database/user/password are
            # separate structured fields, same shape as Redshift's/SQL
            # Server's above (see this module's docstring and backends/
            # mongodb_sql.py's).
            url = (db.get('url') or '').strip()
            database = (db.get('database') or '').strip()
            user = (db.get('user') or '').strip()
            if not (url and database and user):
                continue
            password = _resolve_mongodb_sql_credentials(
                user_identity, url, database, db.get('password'),
                name=db.get('name'),
            )
            if not password:
                # Incomplete - not ready to save yet (see docstring above).
                continue
            config = {"database": database, "user": user, "password": password}
            name = db.get("name") or database or "Custom MongoDB"
            merged.append({
                # password folded directly into the key (not via url,
                # which carries no credential of its own for this
                # dialect - see _resolve_mongodb_sql_credentials above),
                # same as Redshift's/SQL Server's calls above.
                "connection_key": compute_connection_key(name, url, password),
                "name": name,
                "type": "MongoDB",
                "url": url,
                "config": config,
            })
        else:
            # Postgres and MySQL land here - each is a single
            # connection-string "url" that carries everything (including
            # its own credential), so there's nothing dialect-specific to
            # normalize beyond preserving whichever was actually selected
            # (db_type), rather than relabeling a non-Postgres row as
            # Postgres the way this used to unconditionally do (see
            # _parse_incoming_connection's matching fix, and
            # backends/mysql.py's module docstring).
            url = (db.get("url") or "").strip()
            if not url:
                continue
            name = db.get("name") or "Custom"
            resolved_type = "mysql" if db_type == "mysql" else "postgres"
            # ca_cert_pem is shared by both dialects (see
            # backends/postgres.py's and backends/mysql.py's module
            # docstrings) - not a credential, so it's simply carried
            # through as-is, never folded into connection_key (unlike a
            # real credential, it doesn't need to distinguish two
            # otherwise-identical connections from each other).
            config = {}
            ca_cert_pem = (db.get('ca_cert_pem') or '').strip()
            if ca_cert_pem:
                config["ca_cert_pem"] = ca_cert_pem
            merged.append({
                "connection_key": compute_connection_key(name, url, None),
                "name": name,
                "type": resolved_type,
                "url": url,
                "config": config,
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

        # LLM provider/model selection - an independent concern from the
        # database connection fields above/below (same "a request can touch
        # one, the other, both, or neither" reasoning as auto_sql_execute),
        # so it's parsed up front and threaded into every set_session() call
        # below rather than living in its own branch. Both silently ignored
        # (left as None -> "don't change this") unless they resolve to an
        # actually-registered provider/one of that provider's actual preset
        # models - a bad/stale request here should never persist garbage
        # that then breaks every subsequent /api/translate call for this
        # session.
        new_llm_provider = data.get('llm_provider')
        new_llm_model = data.get('llm_model')
        if isinstance(new_llm_provider, str) and new_llm_provider in (p["name"] for p in list_llm_providers_info()):
            provider_for_validation = get_llm_provider(new_llm_provider)
            if not (isinstance(new_llm_model, str) and new_llm_model in provider_for_validation.preset_models):
                new_llm_model = None
        else:
            new_llm_provider = None
            new_llm_model = None

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

        # In-scope connections (multi-database question-answering - see
        # translate_routes.py's module docstring): the set of connections a
        # question may ever be routed to, curated via the connection
        # picker's checkboxes. An independent concern from the single
        # active-connection fields above/below (same "a request can touch
        # one, the other, both, or neither" reasoning as llm_provider/
        # auto_sql_execute) - sent as an all-or-nothing pair, same
        # convention custom_databases uses, since a partial update would
        # leave the two lists describing an inconsistent set (see
        # StateStore.set_session's docstring). in_scope_custom_keys is
        # validated against `merged_custom_databases` (this SAME request's
        # about-to-be-saved list, already carrying each entry's final
        # connection_key - see _parse_incoming_custom_databases' docstring)
        # when the request also touches the saved-connection list, so a
        # connection added and marked in-scope in the same Save doesn't get
        # spuriously dropped for not existing yet; otherwise it's checked
        # against the currently-saved list.
        new_in_scope_preset_ids = data.get('in_scope_preset_ids')
        new_in_scope_custom_keys = data.get('in_scope_custom_connection_keys')
        if new_in_scope_preset_ids is not None or new_in_scope_custom_keys is not None:
            candidate_preset_ids = new_in_scope_preset_ids if isinstance(new_in_scope_preset_ids, list) else []
            candidate_custom_keys = new_in_scope_custom_keys if isinstance(new_in_scope_custom_keys, list) else []

            valid_preset_ids = {db.get("id") for db in CONFIGURED_DBS}
            reference_custom_databases = (
                merged_custom_databases if merged_custom_databases is not None
                else state_store.get_db_connections(user_identity)
            )
            valid_custom_keys = {
                db.get("connection_key") for db in reference_custom_databases if db.get("connection_key")
            }

            # Filtered (unknown/stale references silently dropped - same
            # leniency resolve_active_descriptor already applies to a
            # single stale connection_id) and deduped, preserving the
            # order the client sent, via dict.fromkeys.
            filtered_preset_ids = list(dict.fromkeys(
                pid for pid in candidate_preset_ids if isinstance(pid, str) and pid in valid_preset_ids
            ))
            filtered_custom_keys = list(dict.fromkeys(
                key for key in candidate_custom_keys if isinstance(key, str) and key in valid_custom_keys
            ))

            total_in_scope = len(filtered_preset_ids) + len(filtered_custom_keys)
            if total_in_scope == 0:
                resp = jsonify({
                    'success': False,
                    'error': 'At least one database connection must be in scope.',
                })
                return apply_session_cookie(resp, session_id), 400
            if total_in_scope > MAX_IN_SCOPE_CONNECTIONS:
                resp = jsonify({
                    'success': False,
                    'error': f'At most {MAX_IN_SCOPE_CONNECTIONS} database connections may be in scope at once.',
                })
                return apply_session_cookie(resp, session_id), 400

            new_in_scope_preset_ids_to_save = filtered_preset_ids
            new_in_scope_custom_keys_to_save = filtered_custom_keys
        else:
            new_in_scope_preset_ids_to_save = None
            new_in_scope_custom_keys_to_save = None

        # in_scope_mode ("single" | "all" - see StateStore.get_session's
        # docstring): the binary connection-scope choice behind
        # webClient/client.js's radio picker. An invalid/unrecognized value
        # is silently treated as "nothing to save" here (None), same
        # leniency new_llm_provider above already applies to an unknown
        # provider name, rather than rejecting the whole request over one
        # bad enum field. Note this is intentionally independent of the
        # in_scope_preset_ids/in_scope_custom_connection_keys validation
        # above: the client always sends both fields together (see
        # triggerConfigSave's payload construction), but they're saved
        # through three separate call sites below, so in_scope_mode needs
        # its own None-means-"don't touch" plumbing through all three,
        # exactly like every other independently-optional field here.
        new_in_scope_mode = data.get('in_scope_mode')
        new_in_scope_mode_to_save = (
            new_in_scope_mode if new_in_scope_mode in ("single", "all") else None
        )

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
                llm_provider=new_llm_provider, llm_model=new_llm_model,
                in_scope_preset_ids=new_in_scope_preset_ids_to_save,
                in_scope_custom_connection_keys=new_in_scope_custom_keys_to_save,
                in_scope_mode=new_in_scope_mode_to_save,
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

            # new_db_url, for the 7 structured dialects, is really just
            # _parse_incoming_connection's internal identity string (see
            # its docstring) - never a real url, and never what actually
            # gets persisted/returned as one. new_db_url_to_persist is that
            # real, storage/response-facing value: Postgres/MySQL's actual
            # url unchanged, None for everything else (those backends never
            # had a real url to begin with - see this module's docstring).
            # None, not "" - state_store.py's database_url column/field is
            # nullable specifically so this doesn't need to fake one.
            new_db_url_to_persist = (
                None if new_db_type in _STRUCTURED_DIALECTS_WITHOUT_A_REAL_URL else new_db_url
            )

            if new_db_url or new_auto_sql_execute is not None:
                if new_db_url:
                    prior_descriptor, _prior_missing = resolve_active_descriptor(
                        state_store.get_session(user_identity), user_identity
                    )
                    prior_config = {k: v for k, v in prior_descriptor.items() if k not in ("type", "url")}
                    if new_db_url != prior_descriptor.get("url") or new_db_config != prior_config:
                        # The DB connection is changing - drop any cached schema
                        # for the connection we're switching to. Without this,
                        # if that connection was cached earlier - e.g. by
                        # another session/user on the same DB, or from before
                        # the schema changed - /api/translate would keep
                        # serving that stale schema for up to
                        # SCHEMA_CACHE_TTL_SECONDS after the switch.
                        # Comparing new_db_config too (not just url), unlike
                        # before, is what makes this also fire for the 7
                        # structured dialects (whose real distinguishing
                        # fields live in config, not a url - see above) and
                        # incidentally now also catches a Postgres/MySQL
                        # ca_cert_pem-only change, which url comparison alone
                        # would have missed.
                        schema_cache.invalidate(get_conn_identifier(
                            {"type": new_db_type, "url": new_db_url_to_persist, **new_db_config}
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
                        elif new_db_type == 'sheets':
                            db_name_to_save = new_db_config.get("tab_name") or "Custom Sheet"
                        elif new_db_type == 'MongoDB':
                            # new_db_config.get("database") directly, same
                            # as Redshift's/SQL Server's branches above -
                            # database is now a real structured field, not
                            # something regex-scraped out of a packed
                            # url/identity string (see backends/
                            # mongodb_sql.py's and this module's
                            # docstrings). new_db_type is already the
                            # canonical "MongoDB" here (not lowercased) -
                            # see _parse_incoming_connection's mongodb
                            # branch, which is where this value came from.
                            db_name_to_save = new_db_config.get("database") or "Custom MongoDB"
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
                    llm_provider=new_llm_provider, llm_model=new_llm_model,
                    in_scope_preset_ids=new_in_scope_preset_ids_to_save,
                    in_scope_custom_connection_keys=new_in_scope_custom_keys_to_save,
                    in_scope_mode=new_in_scope_mode_to_save,
                )
                if db_name_to_save is not None:
                    # new_db_url_to_persist, not new_db_url: for the 7
                    # structured dialects new_db_url is only an internal
                    # identity string (already spent, above, on the cache
                    # check and active_connection_key) and was never a real
                    # url - persisting it would resurrect exactly the
                    # synthetic/concatenated field this refactor removes.
                    state_store.set_db_connections(
                        user_identity, db_name_to_save, new_db_type, new_db_url_to_persist,
                        db_config=new_db_config, custom_databases=merged_custom_databases,
                        connection_key=(active_connection_key or None),
                    )
                    custom_list_saved = True
        elif (new_auto_sql_execute is not None or new_llm_provider is not None or new_llm_model is not None
              or new_in_scope_preset_ids_to_save is not None or new_in_scope_custom_keys_to_save is not None
              or new_in_scope_mode_to_save is not None):
            # Neither a preset nor a custom connection was actively
            # selected in this request (e.g. only the auto-execute toggle
            # changed, only the in-scope checkboxes changed, or - the
            # model-selection modal's own save, which never sends
            # preset_id/is_custom at all - only llm_provider/llm_model
            # changed) - leave the active connection exactly as it is.
            # There's no hardcoded default to reset it to here anymore the
            # way there used to be: a blank/never-set connection_id already
            # resolves to the app default on its own - see db.py's
            # resolve_active_descriptor.
            state_store.set_session(
                user_identity, auto_sql_execute=new_auto_sql_execute,
                llm_provider=new_llm_provider, llm_model=new_llm_model,
                in_scope_preset_ids=new_in_scope_preset_ids_to_save,
                in_scope_custom_connection_keys=new_in_scope_custom_keys_to_save,
                in_scope_mode=new_in_scope_mode_to_save,
            )

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

    # A blank/never-set session field falls back to get_llm_provider()'s own
    # hardcoded default (Google), exactly like translate_query()'s own
    # resolution (see that function's comment) - the badge/modal always
    # shows the model that would actually be used, never a raw blank.
    active_llm_provider_obj = get_llm_provider(session_data.get("llm_provider"))
    active_llm_provider = active_llm_provider_obj.name
    active_llm_model = session_data.get("llm_model") or active_llm_provider_obj.default_model

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
        # A custom (user-supplied) connection's display name in the UI is
        # always the name the user gave it when they saved it - sent below
        # as 'custom_database_name' (see user_custom_name above) - which
        # client.js's updateConnectionDetails() prefers over 'database_name'
        # and never actually falls through to it in practice. This branch
        # used to also do a real backend.connect() + identity_label() call
        # here purely to populate that 'database_name'/'username' fallback
        # with the live DB name and connected user - a real network round
        # trip to the target database on every single GET/POST /api/config,
        # including on every config-modal open and Save, for a value
        # nothing in the UI ends up displaying. Removed entirely per the
        # user's request - db_name now just reuses the already-known custom
        # name (no connection needed) and username is left blank, since
        # nothing reads it. identity_label() itself is untouched on every
        # Backend subclass in case it's wanted again later - this was its
        # only caller anywhere in the server.
        db_name = user_custom_name or "Custom"
        username = ""
        # active_conn_str is None for a structured-dialect (BigQuery/etc.)
        # custom connection - coalesced to "" here, at the response
        # boundary, so 'active_database_url' below stays the string every
        # existing client (and test) already expects; only the internal
        # representation/storage is None now, not the wire format.
        active_conn_str_out = active_conn_str or ""
        active_db_type_out = active_db_type
        # Whether the active connection was explicitly selected as a saved
        # custom connection, as opposed to a preset - lets the frontend break
        # the tie when a custom connection's URL happens to collide with a
        # preset's (see the comment on active_custom_db above); URL equality
        # alone can't distinguish "the preset" from "my custom connection
        # that happens to point at the same database".
        active_is_custom_out = bool(session_data.get("is_custom"))

    # Multi-database question-answering's in-scope set, for DISPLAY
    # (what the connection picker's checkboxes render as checked) - see
    # the in_scope_preset_ids/in_scope_custom_connection_keys fields
    # below. state_store.py's _lazy_derive_in_scope deliberately leaves a
    # brand-new/never-explicitly-saved session's raw fields as two empty
    # lists when connection_id is blank (its own docstring explains why:
    # the same "nothing configured, fall back to the app default"
    # convention resolve_active_descriptor already uses) - but the
    # checkbox UI has no room for that nuance, and showing NOTHING
    # checked for a first-time visitor who nonetheless already has an
    # effective default connection is a real regression from the single-
    # radio picker this replaced (which always had exactly one radio
    # checked). Mirrors active_preset_id's own "or DEFAULT_PRESET_ID"
    # fallback just above: only kicks in when BOTH raw lists are empty
    # (an explicitly-saved empty set can't happen - config_routes.py's
    # POST handler rejects that outright), and reflects whichever
    # connection this response's other active_* fields already resolved
    # to, so the checkbox state is never inconsistent with the rest of
    # this same response.
    raw_in_scope_preset_ids = session_data.get('in_scope_preset_ids') or []
    raw_in_scope_custom_keys = session_data.get('in_scope_custom_connection_keys') or []
    if raw_in_scope_preset_ids or raw_in_scope_custom_keys:
        display_in_scope_preset_ids = raw_in_scope_preset_ids
        display_in_scope_custom_keys = raw_in_scope_custom_keys
    elif session_data.get("is_custom") and active_custom_connection_key:
        display_in_scope_preset_ids, display_in_scope_custom_keys = [], [active_custom_connection_key]
    else:
        display_in_scope_preset_ids, display_in_scope_custom_keys = [active_preset_id or DEFAULT_PRESET_ID], []

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
        # Sheets has no host/port/database/user/schema concept at all -
        # nothing here fits any of the generic fields above, so both of
        # its fields get their own dedicated names.
        'active_database_sheets_spreadsheet_id': active_db_config.get("spreadsheet_id", "") if active_db_type_out == "sheets" else "",
        'active_database_sheets_tab_name': active_db_config.get("tab_name", "") if active_db_type_out == "sheets" else "",
        # MongoDB's "url" already round-trips generically via
        # active_database_url above (it's a real field for this dialect,
        # unlike Redshift's/SQL Server's - see this module's docstring) -
        # only database/user need their own dedicated fields here, same
        # reasoning as Redshift's/SQL Server's own database/user fields.
        'active_database_mongodb_database': active_db_config.get("database", "") if active_db_type_out == "MongoDB" else "",
        'active_database_mongodb_user': active_db_config.get("user", "") if active_db_type_out == "MongoDB" else "",
        'custom_database_name': user_custom_name or "",
        'custom_database_url': user_custom_url or "",
        'custom_databases': custom_databases or [],
        # Multi-database question-answering (see translate_routes.py's
        # module docstring): the set of connections a question may ever be
        # routed to - what the connection picker's checkboxes render as
        # checked. Additive fields; every existing single-connection field
        # above is still computed purely off connection_id/is_custom
        # (now "the primary connection" - the first entry of this set, in
        # stable display order - see state_store.py's docstring), so an
        # older client that's never heard of these two fields keeps working
        # exactly as before. max_in_scope_connections is sent so the
        # frontend can show a friendly message before a Save would be
        # rejected, rather than only finding out from a 400 response.
        'in_scope_preset_ids': display_in_scope_preset_ids,
        'in_scope_custom_connection_keys': display_in_scope_custom_keys,
        'in_scope_mode': session_data.get('in_scope_mode') or 'single',
        'max_in_scope_connections': MAX_IN_SCOPE_CONNECTIONS,
        # Organized by provider (see list_llm_providers_info()'s docstring)
        # so the model-selection modal can render one radio-button section
        # per provider without the client needing its own hardcoded notion
        # of which models belong to which provider.
        'llm_providers': list_llm_providers_info(),
        'active_llm_provider': active_llm_provider,
        'active_llm_model': active_llm_model,
        'auto_sql_execute': auto_sql_execute,
        # So the client's own turn-navigation cap (chatStore in client.js)
        # can match the number of turns /api/translate actually replays to
        # the LLM, rather than carrying an independent hardcoded constant
        # that silently drifts if this env var is ever changed.
        'history_max_turns': HISTORY_MAX_TURNS,
        'database_name': db_name,
        'username': username
    })
    return apply_session_cookie(resp, session_id)