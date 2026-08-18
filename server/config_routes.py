"""
config_routes.py

The /api/config endpoint: reads/writes the current session's active
database (now dialect-aware - Postgres or BigQuery) and its "Automatic
SQL Execution" preference, and reports back everything the frontend needs
to render its DB/session UI - including the server-configured list of
available Gemini models (PRESET_MODELS), which the frontend may pass
per-request to /api/translate, but which is not tied to or persisted on
the session.

Connections are represented as descriptors: {"type": "postgres", "url":
"..."} or {"type": "bigquery", "url": "bigquery://<project>/<dataset>",
"project_id": "...", "dataset": "...", "credentials_json": "...",
"billing_project_id": "..."} - see db.py's session_to_descriptor /
backends/base.py's module docstring / backends/bigquery.py's module
docstring for what billing_project_id is and why it's not just project_id.
credentials_json is the one field that must never round-trip back to the
frontend once saved (see state_store.get_db_connections'
include_credentials param); _resolve_bigquery_credentials below is what
lets a user re-select or rename a saved BigQuery connection, or just
switch back to it, without re-pasting its service-account key every time.
billing_project_id is NOT a credential (it's just a project id string) and
always round-trips to the frontend as-is - see get_db_connections'
_strip_credentials, which only strips credentials_json.

Billing policy, by design: admin-configured presets (CONFIGURED_DBS, from
DATABASE_PRESETS in app_config.py) authenticate via this app's own ambient
identity (ADC) and never carry a credentials_json; an admin who wants a
preset to bill anywhere other than its own project_id must say so
explicitly via that preset's own "billing_project_id" - there is no env
var or other implicit default, on purpose, so this app's own project never
silently pays for a preset an admin didn't deliberately configure that way
(see app_config.py). A user's own custom BigQuery connection is held to a
stricter rule still: it must ALWAYS supply both its own billing_project_id
and its own service-account key (credentials_json) - _parse_incoming_connection
and _parse_incoming_custom_databases below reject/skip a custom BigQuery
connection missing either, rather than falling back to a preset's or this
app's billing project. The reasoning: only a key with actual
bigquery.jobs.create rights on the given billing project can make that
project pay for the job at all, so accepting a billing_project_id without
requiring its own key would just fail at query time anyway - and never
inferring one from the other keeps a user's billing choice explicit rather
than a side effect of what happened to be embedded in their pasted key.
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




_CUSTOM_BIGQUERY_MISSING_FIELDS_ERROR = (
    "Custom BigQuery connections require both a billing project ID and a "
    "service-account key (JSON). This app's own project never pays for a "
    "custom connection - only a key with billing rights on the project you "
    "specify can actually run the query."
)


def _parse_incoming_connection(data, user_identity, is_custom):
    """Builds (db_type, db_url, db_config, error) from a POST body's
    top-level active-connection fields. db_url is None if the request
    didn't supply enough to identify a connection of the given type (e.g. a
    BigQuery selection missing project_id/dataset) - callers treat that the
    same as "no connection change requested". `error`, when not None, means
    this connection is invalid and MUST NOT be saved/activated - currently
    only used for a custom BigQuery connection missing its required
    billing_project_id and/or credentials_json (see module docstring)."""
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

    # Default / explicit postgres.
    return 'postgres', data.get('database_url'), {}, None


def _parse_incoming_custom_databases(custom_databases_in, user_identity):
    """Normalizes the frontend's `custom_databases` list (each item using
    the same flat per-type field shape as the top-level connection - see
    _parse_incoming_connection) into the {"connection_key", "name", "type",
    "url", "config"} shape state_store.set_db_connections expects, merging
    in previously saved BigQuery credentials where the request didn't
    supply a fresh key. Returns None if the request didn't include the
    field at all (meaning "leave the saved list alone"), same as before.

    A BigQuery entry missing its required billing_project_id and/or
    credentials_json (see module docstring) is silently skipped - not
    persisted - rather than erroring the whole batch save: this list can
    legitimately include an in-progress row the user hasn't finished
    filling in yet (e.g. a freshly-added blank "+ Add custom connection"
    row), same as an incomplete Postgres row is already silently dropped
    below. Trying to *activate* an incomplete BigQuery connection (as
    opposed to just having it sit half-filled in the saved list) is what
    actually gets rejected with a clear error - see
    _parse_incoming_connection's `error` return, used for the active
    selection, not this list.

    connection_key (see compute_connection_key's docstring in
    state_store.py) is computed here, once, from the exact (name, url,
    credentials_json) this function is about to persist - so it can also
    be reused as-is for the session's custom_connection_key pointer when
    this same request's active connection is one of these entries (see
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
        else:
            url = (db.get("url") or "").strip()
            if not url:
                continue
            name = db.get("name") or "Custom"
            merged.append({
                "connection_key": compute_connection_key(name, url, None),
                "name": name,
                "type": "postgres",
                "url": url,
                "config": {},
            })
    return merged


@config_bp.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
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

        if is_anonymous_user(user_identity):
            # Anonymous (Cloud Run, signed-out) users may switch between
            # admin-configured presets and toggle auto-execute, but can
            # never save or select a custom connection - that's inherently
            # user-scoped, and there is no real per-user identity here to
            # scope it to (every anonymous visitor shares one identity, see
            # ANONYMOUS_USER_ID in auth.py). Reject outright rather than
            # silently no-op'ing a custom-connection attempt.
            if is_custom or data.get('custom_databases'):
                resp = jsonify({
                    'success': False,
                    'error': 'Please log in to save a custom database connection.'
                })
                return apply_session_cookie(resp, session_id), 403

            # Anonymous users never receive real preset connection strings
            # (see the redacted configured_databases below) - the frontend
            # can only ask for "preset #N" by index, and the actual
            # descriptor is resolved here, server-side, from CONFIGURED_DBS.
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
                    compute_connection_key(db_name_to_save, new_db_url, new_db_config.get("credentials_json"))
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

    if IS_CLOUD_RUN and not is_authenticated:
        # Anonymous users can never save a custom connection (enforced
        # above at save-time) and must never be shown anyone else's - keep
        # the shared "anonymous" identity connection-less here regardless
        # of what (if anything) happens to be in the state store for it.
        custom_databases = []
        user_custom_name = None
        user_custom_url = None
        active_custom_connection_key = ""
        active_uses_custom_credentials = False
    else:
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

    if IS_CLOUD_RUN and not is_authenticated:
        # Anonymous users may still open the DB config dialog and switch
        # between admin-configured presets (never custom connections - see
        # the POST handling above), but must never see a preset's actual
        # connection string, since that embeds credentials. Presets are
        # exposed name/type only, and the currently active one is
        # identified by array index (active_preset_index) rather than by
        # URL, matched here server-side against the real (never-exposed)
        # active_conn_str - the frontend never learns the actual string.
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
        configured_dbs = [
            {"name": db.get("name"), "type": db.get("type", "postgres")}
            for db in CONFIGURED_DBS
        ]
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
        configured_dbs = CONFIGURED_DBS

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