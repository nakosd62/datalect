"""
server.py

Thin entrypoint. All the actual logic lives in focused modules:

    app_config.py        - env parsing, Flask app + CORS, state store singleton
    auth.py               - session/identity resolution, auth guard, /api/auth/me
    db.py                  - connection resolution, schema introspection
    translate_routes.py    - /api/translate (Gemini NL -> SQL)
    execute_routes.py      - /api/execute (run SQL, return results)
    config_routes.py       - /api/config (session DB/model selection)
    history_routes.py      - /api/history, /api/history/purge

This file just wires them together: create the app, attach the auth
guard, register each blueprint, serve the SPA shell, and run.
"""

import os

from flask import send_from_directory

from app_config import app, state_store
from auth import auth_bp, enforce_authentication
from config_routes import config_bp
from translate_routes import translate_bp
from execute_routes import execute_bp
from history_routes import history_bp

# Auth guard runs before every request (see EXEMPT_ENDPOINTS in auth.py
# for the routes that skip it).
app.before_request(enforce_authentication)

for bp in (auth_bp, config_bp, translate_bp, execute_bp, history_bp):
    app.register_blueprint(bp)


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    hostname = os.environ.get("CRBOT_HOSTNAME", "0.0.0.0")
    port = int(os.environ.get("CRBOT_PORT", 3000))
    state_store.init()
    app.run(host=hostname, port=port, debug=False, use_reloader=False)