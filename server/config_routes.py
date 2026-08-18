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
_billing_project_from_credentials derives billing_project_id the same way,
from that same key, whenever a fresh/reused credentials_json is available.
Admin-preset BigQuery connections (CONFIGURED_DBS, from BIGQUERY_* env
vars in app_config.py) intentionally carry no credentials_json at all -
they authenticate via the app's own Cloud Run service account (ADC), not
a per-connection key; their billing_project_id is set in app_config.py
instead (defaulting to GCP_PROJECT_ID).
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
import schema_cache

config_bp = Blueprint('config', __name__)


def _bigquery_url(project_id, dataset):
    """Synthetic, non-secret identifier for a BigQuery connection - plays
    the same role a Postgres DSN does elsewhere (schema-cache key, preset/
    custom-connection matching, display), but is never a credential."""
    return f"bigquery://{project_id}/{dataset}"


def _resolve_bigquery_credentials(user_identity, project_id, dataset, provided_credentials_json):
    """Returns the credentials_json to persist for a BigQuery connection:
    whatever was freshly provided in this request, else whatever was
    already stored for this exact project/dataset. Without this, simply
    re-selecting (or renaming) an already-saved custom BigQuery connection
    would look like "no credentials provided" and silently drop the
    stored key, since get_db_connections() never sends it back to the
    frontend in the first place."""
    if provided_credentials_json:
        return provided_credentials_json
    target_url = _bigquery_url(project_id, dataset)
    existing = state_store.get_db_connections(user_identity, include_credentials=True)
    for db in existing:
        if db.get("type") == "bigquery" and db.get("url") == target_url:
            return (db.get("config") or {}).get("credentials_json")
    return None


def _billing_project_from_credentials(credentials_json):
    """The billing/job-execution project implied by a pasted service-account
    key - its own home project (where it was minted), not necessarily the
    project_id/dataset actually being queried (see backends/bigquery.py's
    module docstring: those can point at any project the key has read
    access to, including one the key's own project has no billing rights
    on at all, like a public dataset). Returns None if credentials_json is
    empty/unparseable/missing a project_id - the backend then falls back
    to project_id itself, same as before this existed."""
    if not credentials_json:
        return None
    try:
        return (json.loads(credentials_json) or {}).get("project_id") or None
    except Exception:
        return None


def _parse_incoming_connection(data, user_identity):
    """Builds (db_type, db_url, db_config) from a POST body's top-level
    active-connection fields. db_url is None if the request didn't supply
    enough to identify a connection of the given type (e.g. a BigQuery
    selection missing project_id/dataset) - callers treat that the same
    as "no connection change requested"."""
    db_type = (data.get('database_type') or 'postgres').strip().lower()

    if db_type == 'bigquery':
        project_id = (data.get('project_id') or '').strip()
        dataset = (data.get('dataset') or '').strip()
        if not (project_id and dataset):
            return db_type, None, {}
        db_url = _bigquery_url(project_id, dataset)
        db_config = {"project_id": project_id, "dataset": dataset}
        credentials_json = _resolve_bigquery_credentials(
            user_identity, project_id, dataset, data.get('credentials_json')
        )
        if credentials_json:
            db_config["credentials_json"] = credentials_json
            billing_project_id = _billing_project_from_credentials(credentials_json)
            if billing_project_id:
                db_config["billing_project_id"] = billing_project_id
        else:
            # No credentials at all for this project/dataset - an
            # authenticated user selecting a preset by its real URL/fields
            # (rather than a saved custom connection) lands here. Copy the
            # matching preset's own billing_project_id (set in
            # app_config.py) across, for the same reason the anonymous
            # preset_index branch above needs to: without it, connect()
            # falls back to project_id itself, breaking the moment the
            # preset points at data outside the app's own project.
            preset_match = next(
                (db for db in CONFIGURED_DBS if db.get("type") == "bigquery" and db.get("url") == db_url),
                None,
            )
            if preset_match and preset_match.get("billing_project_id"):
                db_config["billing_project_id"] = preset_match["billing_project_id"]
        return db_type, db_url, db_config

    # Default / explicit postgres.
    return 'postgres', data.get('database_url'), {}


def _parse_incoming_custom_databases(custom_databases_in, user_identity):
    """Normalizes the frontend's `custom_databases` list (each item using
    the same flat per-type field shape as the top-level connection - see
    _parse_incoming_connection) into the {"name", "type", "url", "config"}
    shape state_store.set_db_connections expects, merging in previously
    saved BigQuery credentials where the request didn't supply a fresh
    key. Returns None if the request didn't include the field at all
    (meaning "leave the saved list alone"), same as before."""
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
            config = {"project_id": project_id, "dataset": dataset}
            credentials_json = _resolve_bigquery_credentials(
                user_identity, project_id, dataset, db.get('credentials_json')
            )
            if credentials_json:
                config["credentials_json"] = credentials_json
                billing_project_id = _billing_project_from_credentials(credentials_json)
                if billing_project_id:
                    config["billing_project_id"] = billing_project_id
            merged.append({
                "name": db.get("name") or dataset or "Custom BigQuery",
                "type": "bigquery",
                "url": url,
                "config": config,
            })
        else:
            url = (db.get("url") or "").strip()
            if not url:
                continue
            merged.append({
                "name": db.get("name") or "Custom",
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
                    # billing project (set in app_config.py, defaulting to
                    # GCP_PROJECT_ID) has to be copied across explicitly
                    # here - without it, backends/bigquery.py's connect()
                    # falls back to project_id itself, which is exactly
                    # the "does not have bigquery.jobs.create permission"
                    # 403 this was meant to fix whenever a preset points at
                    # data outside the app's own project (e.g. a public
                    # dataset).
                    if preset.get("billing_project_id"):
                        new_db_config["billing_project_id"] = preset["billing_project_id"]

            if new_db_url or new_auto_sql_execute is not None:
                state_store.set_session(
                    user_identity, new_db_url, new_auto_sql_execute,
                    db_type=new_db_type, db_config=new_db_config, is_custom=False,
                )

        else:
            new_db_type, new_db_url, new_db_config = _parse_incoming_connection(data, user_identity)
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
                state_store.set_session(
                    user_identity, new_db_url, new_auto_sql_execute,
                    db_type=new_db_type, db_config=new_db_config, is_custom=is_custom,
                )
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
                    state_store.set_db_connections(
                        user_identity, db_name_to_save, new_db_type, new_db_url,
                        db_config=new_db_config, custom_databases=merged_custom_databases,
                    )
            else:
                state_store.set_session(
                    user_identity, DEFAULT_CONN, db_type='postgres', db_config={}, is_custom=False,
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
    else:
        custom_databases = state_store.get_db_connections(user_identity)  # credentials stripped
        # Must be whichever saved custom connection is actually active
        # (matched by URL), not just custom_databases[0] - that was fine
        # back when a user could only ever save one custom connection, but
        # now that multiple can be saved, "the first one saved" and "the
        # one currently selected" are frequently different, and the
        # frontend's connection badge trusts this field over the
        # live-introspected database_name (see updateConnectionDetails in
        # client.js) - so picking the wrong one here made the badge appear
        # to ignore the user's selection even though the session's
        # connection had actually switched correctly.
        active_custom_db = next(
            (db for db in custom_databases if db.get("url") == active_conn_str), None
        )
        user_custom_name = active_custom_db["name"] if active_custom_db else None
        user_custom_url = active_custom_db["url"] if active_custom_db else None

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