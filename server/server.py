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
    report_routes.py       - /api/report-issue (email an error/wrong-result report)

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
from report_routes import report_bp

# Auth guard runs before every request (see EXEMPT_ENDPOINTS in auth.py
# for the routes that skip it).
app.before_request(enforce_authentication)

for bp in (auth_bp, config_bp, translate_bp, execute_bp, history_bp, report_bp):
    app.register_blueprint(bp)


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    hostname = os.environ.get("CRBOT_HOSTNAME", "0.0.0.0")
    port = int(os.environ.get("CRBOT_PORT", 3000))
    state_store.init()
    # threaded=True: without it, Werkzeug's dev server (what this actually
    # is, in production too - see the Dockerfile's CMD) handles one request
    # at a time. A single slow/unreachable admin-configured database preset
    # - even with backends/base.py's DB_CONNECT_TIMEOUT_SECONDS now bounding
    # how long its connect() calls can hang - would otherwise stall every
    # other user's completely unrelated request for that whole window,
    # since nothing else can be serviced while the one worker is blocked.
    # Verified safe to flip on: every process-wide mutable global this app
    # has (schema_cache.py's _cache) is already guarded by its own
    # threading.Lock(), and state_store.py's SqliteStateStore opens a fresh
    # sqlite3 connection per operation rather than sharing one across
    # threads, so nothing here relied on single-threaded execution to begin
    # with. See backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring for
    # the other half of this fix (bounding *how long* a bad connection can
    # block) - this half bounds *what else* is blocked meanwhile.
    app.run(host=hostname, port=port, debug=False, use_reloader=False, threaded=True)