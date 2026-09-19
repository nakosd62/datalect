"""
server.py

Thin entrypoint. All the actual logic lives in focused modules:

    app_config.py        - env parsing, Flask app + CORS, state store singleton
    auth.py               - session/identity resolution, auth guard, /api/auth/me
    db.py                  - connection resolution, schema introspection
    translate_routes.py    - /api/translate (Gemini NL -> SQL)
    execute_routes.py      - /api/execute (run SQL, return results)
    config_routes.py       - /api/config (session DB/model selection)
    report_routes.py       - /api/report-issue (email an error/wrong-result report)

This file just wires them together: create the app, attach the auth
guard, register each blueprint, serve the SPA shell, and run.
"""

import os
import threading

from flask import send_from_directory

from app_config import app, state_store
from auth import auth_bp, enforce_authentication, refresh_auth_session_cookie
from config_routes import config_bp
from translate_routes import translate_bp
from execute_routes import execute_bp
from chat_history_routes import chat_history_bp
from report_routes import report_bp
from db import prefetch_all_preset_schemas

# Auth guard runs before every request (see EXEMPT_ENDPOINTS in auth.py
# for the routes that skip it).
app.before_request(enforce_authentication)
# Sliding renewal for the app's own long-lived session cookie - runs after
# EVERY request (not just non-exempt ones; /api/config and /api/auth/me
# both resolve identity themselves despite being exempt from the guard
# above) so activity on any authenticated route keeps a user's session
# alive. See auth.py's refresh_auth_session_cookie()/auth_session.py.
app.after_request(refresh_auth_session_cookie)

for bp in (auth_bp, config_bp, translate_bp, execute_bp, chat_history_bp, report_bp):
    app.register_blueprint(bp)


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# Runs at IMPORT time, not just under `if __name__ == '__main__'` below -
# deliberately. In production (see the Dockerfile's CMD) this module is
# imported by gunicorn as `server:app`, so this file's own `__main__` block
# never executes there; state_store.init() (schema creation/migrations -
# see its docstring, "safe to call on every startup") still has to run
# somewhere every process actually reaches, or a fresh SqliteStateStore
# would silently have no tables (FirestoreStateStore.init() is a no-op, so
# this mattered less there, but is a real correctness gap for local/dev use
# under gunicorn or any other non-`__main__` entry point). Confirmed by
# direct testing: before this was hoisted out of `__main__`, running this
# app under gunicorn instead of `python server/server.py` left every
# session/db_connection/chat_history read silently falling back to
# in-memory defaults (sqlite3.OperationalError: no such table, caught and
# logged rather than raised) instead of ever actually persisting.
state_store.init()

# Kicked off at import time, for the same reason as state_store.init()
# above (production imports this module under gunicorn, never reaching
# `__main__` below) - but on a background daemon thread, deliberately NOT
# a blocking call like state_store.init() just above. Warms every admin-
# configured preset's schema cache - cached indefinitely, like every
# schema fetch (see schema_cache.py) - so most requests after boot don't
# pay a live introspection query. Now that schema_cache.py durably
# persists every entry (state_store.py - SQLite locally, Firestore on
# Cloud Run), a RESTART of an already-running app usually finds this
# thread has nothing live to do at all: prefetch_all_preset_schemas()
# loads each preset's already-persisted schema/overview straight from
# that durable store instead of re-querying its real database, so this
# whole thread typically finishes in well under a second on a restart -
# the "up to about a minute" cost described below is really a first-ever-
# boot (nothing durable saved yet for any preset) or after-invalidation
# cost, not a per-restart one anymore. Running it on its own thread
# instead of inline means the server starts accepting requests
# immediately rather than making every request wait out however long the
# slowest preset takes to introspect (observed to be up to about a
# minute with several presets configured on a cold/never-persisted
# start, dominated by whichever single one is slowest, since a blocking
# call here waits for ALL of them - see prefetch_all_preset_schemas()'s
# own docstring for why it fetches concurrently across presets in the
# first place, and for exactly when a preset does vs. doesn't pay a real
# live fetch here). The tradeoff: a
# request that picks a preset before its prefetch thread gets to it falls
# back to fetching that one preset's schema live, inline, itself - exactly
# the same "fetch it on first use" path a custom connection already goes
# through today (see get_database_schema()'s cache-miss branch) - so this
# is a startup-latency/first-request-latency tradeoff, not a correctness
# one: nothing is different about whether a request eventually gets a
# working schema, only about which request pays for fetching it. See
# prefetch_all_preset_schemas()'s own docstring for why a single
# unreachable preset can't block (or delay) any other preset either.
# daemon=True so this thread can't keep the process alive on shutdown if
# it's still mid-fetch against a slow/unreachable preset.
threading.Thread(
    target=prefetch_all_preset_schemas,
    name="startup-schema-prefetch",
    daemon=True,
).start()

if __name__ == '__main__':
    # Local/dev entrypoint only now (`run_server.sh`'s
    # `python3 -u server/server.py`) - production runs this same `app`
    # object under gunicorn instead (see the Dockerfile's CMD), which is
    # why state_store.init() above was moved out of this block.
    hostname = os.environ.get("CRBOT_HOSTNAME", "0.0.0.0")
    port = int(os.environ.get("CRBOT_PORT", 3000))
    # threaded=True: without it, Werkzeug's dev server handles one request
    # at a time. A single slow/unreachable admin-configured database preset
    # - even with backends/base.py's DB_CONNECT_TIMEOUT_SECONDS now bounding
    # how long its connect() calls can hang - would otherwise stall every
    # other user's completely unrelated request for that whole window,
    # since nothing else can be serviced while the one worker is blocked.
    # Verified safe to flip on: every process-wide mutable global this app
    # has (schema_cache.py's _cache, cancel_registry.py's _registry) is
    # already guarded by its own threading.Lock(), and state_store.py's
    # SqliteStateStore opens a fresh sqlite3 connection per operation
    # rather than sharing one across threads, so nothing here relied on
    # single-threaded execution to begin with. See backends/base.py's
    # DB_CONNECT_TIMEOUT_SECONDS docstring for the other half of this fix
    # (bounding *how long* a bad connection can block) - this half bounds
    # *what else* is blocked meanwhile. The same reasoning is exactly why
    # production's gunicorn config (Dockerfile) uses one process with
    # multiple threads (--worker-class gthread) rather than multiple
    # worker processes: schema_cache.py's and cancel_registry.py's
    # in-memory state is process-local by design (see cancel_registry.py's
    # own module docstring) and assumes every request-handling thread
    # shares the same process - true here and under gunicorn's threaded
    # worker, but NOT true across multiple gunicorn worker processes.
    app.run(host=hostname, port=port, debug=False, use_reloader=False, threaded=True)