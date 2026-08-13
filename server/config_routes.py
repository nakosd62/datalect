"""
config_routes.py

The /api/config endpoint: reads/writes the current session's active
database + model selection, and reports back everything the frontend
needs to render its DB/session UI.
"""

from urllib.parse import urlparse

from flask import Blueprint, request, jsonify

from app_config import (
    CONFIGURED_DBS, DEFAULT_CONN, DEFAULT_MODEL, PRESET_MODELS,
    AUTH_ENABLED, IS_CLOUD_RUN, state_store, logger,
)
import os
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import get_db_connection

config_bp = Blueprint('config', __name__)


@config_bp.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    is_authenticated = bool(user_identity and user_identity != session_id)

    preset_urls = {db["url"] for db in CONFIGURED_DBS}

    if request.method == 'POST':
        data = request.get_json() or {}
        new_db_url = data.get('database_url')
        new_db_name = data.get('database_name')
        new_model = data.get('model') or data.get('gemini_model')
        is_custom = data.get('is_custom', False)
        custom_databases = data.get('custom_databases')

        if new_db_url or new_model:
            state_store.set_session(user_identity, new_db_url or DEFAULT_CONN, new_model)
            if new_db_url and (is_custom or (new_db_url not in preset_urls)):
                db_name_to_save = new_db_name
                if not db_name_to_save:
                    try:
                        parsed = urlparse(new_db_url)
                        dbname = parsed.path.lstrip('/')
                        if '?' in dbname:
                            dbname = dbname.split('?')[0]
                        db_name_to_save = dbname or "Custom"
                    except Exception:
                        db_name_to_save = "Custom"
                state_store.set_db_connections(user_identity, db_name_to_save, new_db_url, custom_databases)
        else:
            state_store.set_session(user_identity, DEFAULT_CONN, DEFAULT_MODEL)

    active_conn_str, _ = state_store.get_session(user_identity)
    custom_databases = state_store.get_db_connections(user_identity)
    user_custom_name = custom_databases[0]["name"] if custom_databases else None
    user_custom_url = custom_databases[0]["url"] if custom_databases else None

    if IS_CLOUD_RUN and not is_authenticated:
        db_name, username = "", ""
        active_conn_str_out = ""
        configured_dbs = []
    else:
        db_name, username = "Unknown", "Unknown"
        conn = None
        try:
            conn = get_db_connection(active_conn_str, user_identity)
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database(), CURRENT_USER;")
                row = cursor.fetchone()
                if row:
                    db_name, username = row[0], row[1]
        except Exception:
            logger.exception("Error fetching connection info")
        finally:
            if conn:
                conn.close()
        active_conn_str_out = active_conn_str
        configured_dbs = CONFIGURED_DBS

    resp = jsonify({
        'auth_enabled': AUTH_ENABLED,
        'google_client_id': os.getenv("GOOGLE_CLIENT_ID"),
        'session_id': session_id,
        'user_id': user_identity,
        'authenticated': is_authenticated,
        'is_cloud_run': IS_CLOUD_RUN,
        'configured_databases': configured_dbs,
        'default_database_url': DEFAULT_CONN if (not IS_CLOUD_RUN or is_authenticated) else "",
        'active_database_url': active_conn_str_out,
        'custom_database_name': user_custom_name or "",
        'custom_database_url': user_custom_url or "",
        'custom_databases': custom_databases or [],
        'gemini_preset_keys': PRESET_MODELS,
        'models': PRESET_MODELS,
        'database_name': db_name,
        'username': username
    })
    return apply_session_cookie(resp, session_id)