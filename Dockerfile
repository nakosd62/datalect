FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (optional, but good practice for networking tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    unixodbc \
    odbcinst \
    && rm -rf /var/lib/apt/lists/*

# backends/mongodb_sql.py (MongoDB Atlas SQL Interface) needs pyodbc PLUS
# MongoDB's own ODBC driver binary registered with unixODBC - unlike every
# other dialect in this app, there's no pure-Python driver for it (see that
# module's docstring). `unixodbc` is the driver MANAGER (glibc 2.36 in this
# base image already clears the driver's own glibc 2.34+ requirement) and
# `odbcinst` is the small CLI MongoDB's own docs use to locate/verify the
# config files (https://www.mongodb.com/docs/sql-interface/install-driver/,
# "Install the ODBC Driver" > Ubuntu prerequisites) - both are real, separate
# Debian bookworm packages (this base image's distro), confirmed via
# https://packages.debian.org/bookworm/odbcinst. `unixodbc-dev` (build
# headers) is NOT needed here - pyodbc ships prebuilt manylinux wheels that
# only dynamically link libodbc.so.2 at runtime, they don't compile against
# unixODBC at pip-install time.
#
# CAVEAT: MongoDB's compatibility table only lists "Ubuntu 22.04 (x86_64 and
# arm64)" for this driver - not Debian, which is what python:3.12-slim
# actually is. The driver is a plain dynamically-linked ELF .so with no
# Ubuntu-specific behavior baked in (same glibc-based reasoning as every
# other backend's Dockerfile comment in this file), so it should work here
# too, but this is genuinely outside MongoDB's own tested support matrix -
# worth knowing if something ever behaves oddly and you're troubleshooting
# with MongoDB support.
#
# Hardcoded rather than a --build-arg: this app is deployed via
# `gcloud run deploy --source .` (see gcp_deploy.sh), which builds the image
# on Cloud Build, not local `docker build` - and `gcloud run deploy`'s own
# flag set (--set-build-env-vars et al.) has no documented way to forward a
# value as a Dockerfile ARG/--build-arg the way plain `docker build` does.
# Since this URL isn't a secret (it's just a public tarball location, same
# shape as any other curl-a-release-tarball line in a Dockerfile), hardcoding
# it sidesteps that gap entirely and needs no local Docker install at all -
# `./gcp_deploy.sh` alone is enough. To bump the driver version later, get a
# fresh URL the same way this one was obtained (mongodb.com/try/download/
# odbc-driver, pick "Linux x64", click "Copy link" - not the Download
# button, which starts a browser download instead of handing you the URL)
# and replace it below.
RUN curl -L "https://downloads.mongodb.org/mongosql-odbc-driver/ubuntu2204/2.0.10/release/mongoodbc-2.0.10.tar.gz" --output /tmp/mongoodbc.tar.gz && \
    tar -zxf /tmp/mongoodbc.tar.gz --directory /usr/local/lib && \
    rm /tmp/mongoodbc.tar.gz && \
    printf '[ODBC Drivers]\nMongoDB Atlas SQL ODBC Driver = Installed\n\n[MongoDB Atlas SQL ODBC Driver]\nDriver=/usr/local/lib/mongoodbc/bin/libatsql.so\n' \
        >> /etc/odbcinst.ini
# The tarball extracts to /usr/local/lib/mongoodbc/ (LICENSE, README.MD, and
# a bin/ directory containing libatsql.so) and the odbcinst.ini entry above
# is registered under the exact name "MongoDB Atlas SQL ODBC Driver" - both
# copied verbatim from MongoDB's own docs (link above), not guessed. This is
# also exactly the string this app's connection strings use as
# "DRIVER={MongoDB Atlas SQL ODBC Driver}" (see backends/mongodb_sql.py's
# docstring) - the two have to agree, and they already do.

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy code
COPY ./server ./server
COPY ./webClient ./webClient

# Copy preset databases
COPY presets.json .

# Copy service account credentials
COPY grand-cosmos-716-3afa9cbc32b7.json .

# Expose container port (Cloud Run defaults to 8080, but we can configure it)
EXPOSE 3000

# Start the app under gunicorn rather than running server/server.py's own
# `if __name__ == '__main__'` block directly - that block runs Werkzeug's
# development server, which Flask's own docs say isn't designed to handle
# real production load/concurrency (see server.py's __main__ docstring for
# this app's own reasoning). gunicorn is a real, battle-tested WSGI server:
# proper request queuing/backlog, worker recycling, and correct SIGTERM
# handling on Cloud Run scale-down/redeploy (the `exec` below matters for
# that last part - without it, `sh` stays PID 1 and gunicorn never sees the
# signal directly).
#
# `--pythonpath server` (rather than `--chdir server`) is deliberate:
# server/*.py files import each other with bare names ("from app_config
# import app", not "from server.app_config import app" - see
# tests/server/helpers.py's own comment on why, server/ has no __init__.py)
# so gunicorn needs `/app/server` on sys.path for `server:app` and its
# internal imports to resolve, exactly like running
# `python server/server.py` does implicitly. But several of those modules
# also read *relative file paths* from env.yaml (DATABASE_PRESETS_FILE=
# "./presets.json", SHEETS_SERVICE_ACCOUNT_CREDENTIALS_FILE=
# "./grand-cosmos-716-3afa9cbc32b7.json", state_store.py's
# TRANSLATION_STATS_DB_PATH="state/ydyl_state.db") which are copied to
# /app, not /app/server (see the COPY lines above) - `--chdir server`
# would move the process's cwd there too and break every one of those
# lookups. `--pythonpath` adds to sys.path WITHOUT touching cwd, so this
# container's cwd stays at /app (this Dockerfile's WORKDIR, matching
# `python server/server.py` run from the repo root) while imports still
# resolve. Verified directly: `--chdir server` reproducibly breaks
# DATABASE_PRESETS_FILE lookup ("No such file or directory: './presets.json'"),
# `--pythonpath server` does not.
#
# --worker-class gthread --workers 1 --threads N: ONE process, N threads -
# not multiple worker processes - is deliberate, not a tuning oversight.
# schema_cache.py's cache and cancel_registry.py's cancellation registry
# are both process-local, in-memory, and guarded by a plain
# threading.Lock() (see cancel_registry.py's own module docstring, which
# explicitly calls out that "a future move to a multi-process server
# (gunicorn with >1 worker, say) would break this silently" - a
# /api/cancel call landing on a different worker process than the one
# actually running the query it's meant to cancel). Multiple *threads*
# within one process preserve the exact shared-memory assumption
# server.py's own threaded=True already relied on; multiple *worker
# processes* would silently fragment that state instead. If this ever
# needs to scale beyond one process's throughput, cancel_registry.py and
# schema_cache.py both need to move to a shared backend (Firestore, e.g.)
# BEFORE adding --workers > 1 - don't casually bump this thinking more
# workers is strictly better.
#
# GUNICORN_THREADS/GUNICORN_TIMEOUT are plain env vars (settable via
# env.yaml, no image rebuild needed) rather than hardcoded, so the thread
# count can be tuned alongside Cloud Run's own --concurrency flag without
# a redeploy-from-source - they should move together (gunicorn can't
# usefully serve more concurrent requests per instance than it has
# threads for, regardless of what Cloud Run is willing to route it).
CMD ["sh", "-c", "exec gunicorn --pythonpath server --bind 0.0.0.0:${CRBOT_PORT:-3000} --worker-class gthread --workers 1 --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-300} server:app"]
