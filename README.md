# yDyL — your Data your Language

yDyL is a natural-language-to-SQL assistant for PostgreSQL-compatible databases,
MySQL-compatible databases, Google BigQuery, and Snowflake. Type a question in plain English (or any
language), review the SQL it generates, and run it — all from a single-page
web app backed by a small Flask API.

- **NL → SQL** via Gemini, grounded in a live introspection of your
  database schema (tables, columns, constraints, indexes, views, grants,
  triggers).
- **Multi-turn conversations** — the last 10 turns (prompt, SQL, and
  results) are kept in memory so follow-up questions have context.
- **Runs anywhere** — SQLite-backed for local development, Firestore-backed
  automatically when deployed on Cloud Run.
- **Optional Google Sign-In** — works fully anonymously by default,
  including translation history (scoped to your browser session, ~24h);
  signing in unlocks custom database connections and history that follows
  you across devices/sessions instead.

---

## Table of contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [Quick start (local)](#quick-start-local)
- [Configuration](#configuration)
- [Docker](#docker)
- [Deploying to Cloud Run](#deploying-to-cloud-run)
- [API reference](#api-reference)
- [Authentication model](#authentication-model)
- [Data persistence](#data-persistence)
- [Frontend notes](#frontend-notes)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## How it works

1. You type a question into the **NL prompt** box (or click one of the
   **Quick prompts** chips to try it immediately).
2. The server introspects your target database's `public` schema
   (cached for a few minutes — see [`SCHEMA_CACHE_TTL_SECONDS`](#configuration))
   and sends it, your prompt, and recent chat history to Gemini.
3. The generated SQL appears in the **SQL editor** box for you to review
   or edit — nothing runs automatically unless you've turned on
   **Automatic SQL Execution** in the connection settings.
4. Click **Execute** to run it against the database and see results,
   grouped by statement if the model returned more than one query.

If Gemini decides your prompt doesn't need SQL at all (a general
knowledge question, or a question about the app itself), it responds
directly instead — see the system prompt in
[`translate_routes.py`](./server/translate_routes.py) for the exact protocol
(`*** NO SQL ***`, etc.).

---

## Project structure

```
.
|── Various helper scripts     # run_server.sh, kill_server.sh, gcp_deploy.sh, run_tests.sh
├── Dockerfile
├── requirements.txt
├── requirements-dev.txt       # pytest + test-only deps (see Testing)
|── tests/
│   ├── server/                 # backend pytest suite (mocked, no external services)
│   └── e2e/                    # frontend Playwright suite - package.json,
│                                # playwright.config.js, and specs all live
│                                # here together (real Flask server, mocked AI/DB calls)
|── utils/                     # unrefined scripts to export the internal state of the app
|── mobile/                    # unfinished mobile app client
├── server/
│   ├── server.py              # Thin entrypoint: wires blueprints together, serves the SPA
│   ├── app_config.py          # Env parsing, Flask app + CORS, state-store construction
│   ├── auth.py                # Session/identity resolution, auth guard, /api/auth/me
│   ├── db.py                  # Connection resolution, schema introspection
│   ├── schema_cache.py        # Short-TTL in-memory cache for schema introspection text
│   ├── state_store.py         # StateStore abstraction: SqliteStateStore / FirestoreStateStore
│   ├── config_routes.py       # /api/config — session DB + model selection
│   ├── translate_routes.py    # /api/translate — Gemini NL -> SQL, API key selection/retry
│   ├── execute_routes.py      # /api/execute — run SQL, return results
│   └── history_routes.py      # /api/history, /api/history/purge
└── webClient/                 # Static frontend, served by Flask (../webClient relative to server/)
    ├── index.html
    ├── client.js
    ├── style.css
    └── help.html              # Fetched at runtime and rendered inside the in-app Help modal

```

`app_config.py` is the one module with import-time side effects
(creating the Flask `app`, connecting to Firestore on Cloud Run) — every
other server module imports shared state (`app`, `state_store`,
`CONFIGURED_DBS`, `logger`, …) from it rather than rebuilding it.

> Some other files/directories in the actual repo aren't covered above
> yet — this reflects what's been shared so far.

---

## Requirements

- Python 3.10+ (the provided `Dockerfile` builds on `python:3.12-slim`;
  see https://endoflife.ai/python before picking a different version)
- A PostgreSQL-compatible database to query — the default connection
  string and the `crdb.crt` cert copied in by the Dockerfile both point
  to [CockroachDB](https://www.cockroachlabs.com/), though any
  Postgres-compatible target works
- A [Gemini API key](https://ai.google.dev/) (or several — see
  [Configuration](#configuration))

Python packages, from [`requirements.txt`](./requirements.txt):

```
Flask==3.0.3
psycopg2-binary==2.9.9
flask-cors==4.0.1
google-genai>=1.0.0
sqlparse>=0.5.0
google-cloud-firestore
google-cloud-datastore
pandas
```

`google-cloud-firestore` is only actually *used* when a GCP project is
configured (see below) — the app runs fine locally without any GCP
credentials, falling back to SQLite. `google-auth` isn't listed
explicitly but is pulled in as a dependency of `google-genai`/
`google-cloud-firestore`, and covers the ID-token verification used in
`auth.py`.

`google-cloud-datastore` and `pandas` aren't imported anywhere in the
server modules covered by this README — they may be used by one of the
files not yet covered here.

---

## Quick start (local)

Run from the repository root (not from inside `server/`) — the Flask
app resolves its static folder relative to `server/app_config.py`'s own
location, but `state_store.py`'s local SQLite path (`state/ydyl_state.db`)
is relative to wherever the process is launched from, so running from
the root keeps that consistent with the Docker image's `WORKDIR /app`.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set at least a Gemini API key
export GEMINI_API_KEY="your-gemini-api-key"

# 3. Run it
python server/server.py
```

By default the server listens on `http://0.0.0.0:3000`. Open it in a
browser — no GCP project, Firestore, or Google Sign-In setup is required
for local use: the app falls back to a local SQLite file
(`state/ydyl_state.db`) for sessions, saved connections, and translation
history, and every request is treated as a single `"global"` local user.

With no database configured, the app still starts and falls back to a
single placeholder Postgres preset (not a real, reachable database) — add
your own database from the connection badge in the header once it's
running, or point `DATABASE_PRESETS_FILE` at a JSON file beforehand to
have real presets available from the start. See
[Database connections](#database-connections) below for both.

---

## Configuration

All configuration is via environment variables. Nothing is required
except a Gemini key and a database to connect to — everything else has a
sensible default.

### Gemini / model

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | A single Gemini API key. Either name works. |
| `GEMINI_PRESET_KEYS` | — | Comma-separated list of additional Gemini API keys. The app picks one at random per request and, on a rate-limit (429) error, automatically retries with a different key from the pool — immediately, with no delay, for up to one attempt per configured key (this budget is independent of `MAX_TRANSLATION_ATTEMPTS` below and is a Gemini-only mechanism; it does not apply when `LLM_PROVIDER=claude`). See [`translate_routes.py`](./server/translate_routes.py) for the full retry policy. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Default model used for translation. |
| `GEMINI_PRESET_MODELS` | `gemini-2.5-flash,gemini-2.5-pro` | Comma-separated list of models offered in the UI. `GEMINI_MODEL` is auto-added if missing. |
| `MAX_TRANSLATION_ATTEMPTS` | `5` | Max attempts (initial call + retries) for a single translation request before giving up, for transient (rate-limit/server-error/connection) failures. Shared by both Gemini and Claude — see [`translate_routes.py`](./server/translate_routes.py). (Formerly `MAX_GEMINI_ATTEMPTS`.) |
| `TRANSLATION_RETRY_DELAY_SECONDS` | `1` | Seconds to wait between transient-error retry attempts. Shared by both providers. (Formerly `GEMINI_RETRY_DELAY_SECONDS`.) |

At least one of `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or
`GEMINI_PRESET_KEYS` must be set, or `/api/translate` returns a 400
("Gemini API key is not configured").

### Database connections

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PRESETS_FILE` | — (no presets) | Path to a JSON file listing the admin-configured preset connections shown in the UI. Supports Postgres, MySQL, and BigQuery presets — see the shape below. If unset, the app falls back to a single synthetic "Default DB" Postgres preset pointing at `postgresql://postgres:password@host:23456/defaultdb?sslmode=verify-full`. |

`DATABASE_PRESETS_FILE` points at a file rather than holding the JSON
inline in an env var, so presets can be written multi-line and reviewed
like normal code instead of squeezed onto one line. The file is a JSON
array of preset objects, one per preset; every object needs `type` and
`name`, and the rest of the shape is dialect-specific:

```json
[
  {
    "type": "postgres",
    "name": "Demo (read-only)",
    "url": "postgresql://demo_user:pw@host:5432/demo"
  },
  {
    "type": "mysql",
    "name": "Sales (MySQL)",
    "url": "mysql://demo_user:pw@host:3306/sales"
  },
  {
    "type": "bigquery",
    "name": "Google Trends",
    "project_id": "bigquery-public-data",
    "dataset": "google_trends",
    "billing_project_id": "my-billing-project"
  }
]
```

The first Postgres preset in the file is the default connection for new
sessions. A MySQL preset is just a connection-string URL, the same as
Postgres — no dialect-specific fields, ambient identity, or always-explicit
credential to worry about. BigQuery presets authenticate as the app's own
ambient identity
(Application Default Credentials — the Cloud Run service account, or
whatever `gcloud auth application-default login` set up locally); an
admin who wants a BigQuery preset to read data outside the app's own
project must say explicitly who pays for it via `billing_project_id` —
there's no env var that supplies a default billing project, on purpose
(see the comment above `DATABASE_PRESETS_FILE` in
[`app_config.py`](./server/app_config.py) for the full rationale).

Relative paths resolve against the process's working directory (run from
the repo root, as the [Quick start](#quick-start-local) section above
already asks for). Since a presets file typically embeds real connection
credentials (a Postgres password, at minimum), keep it out of version
control the same way you would `.env`/`env.yaml` — see
[`database_presets.json`](./database_presets.json) for this repo's own
gitignored local-dev copy, and [Docker](#docker) below for how it reaches
the container image.

Signed-in users can also add their own custom connection (Postgres,
MySQL, BigQuery, or Snowflake) from the database badge in the header —
those are saved per-user (see [Data persistence](#data-persistence)) and
are **not** available to anonymous users. There's currently no admin-preset
path for Snowflake — see `config_routes.py`'s module docstring.

### Authentication

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_ID` | — | Enables Google Sign-In and ID-token verification. If unset, the app runs with auth disabled locally (or as an anonymous user, one identity per browser session, on Cloud Run). |

See [Authentication model](#authentication-model) for the full picture,
including how identity is resolved and what anonymous users can/can't do.

### GCP / Cloud Run

| Variable | Default | Purpose |
|---|---|---|
| `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT` | — | GCP project used to connect to Firestore (database name `ydyl`). First one set wins. |
| `K_SERVICE` | — | Set automatically by Cloud Run; used to detect that the app is running there. Don't set this manually. |

If `K_SERVICE` is present (i.e. running on Cloud Run) but no Firestore
client could be initialized, **startup fails on purpose** rather than
silently falling back to ephemeral SQLite in a container that could be
recycled at any time — see the `RuntimeError` in
[`app_config.py`](./server/app_config.py).

### Misc

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level for the app's own `"ydyl"` logger. Third-party library loggers stay at `WARNING` regardless. |
| `SCHEMA_CACHE_TTL_SECONDS` | `300` | How long introspected schema text is cached per connection before being re-fetched. |
| `CRBOT_HOSTNAME` | `0.0.0.0` | Host to bind when running via `python server/server.py` directly. |
| `CRBOT_PORT` | `3000` | Port to bind when running via `python server/server.py` directly. |

---

## Docker

The provided `Dockerfile` builds on `python:3.12-slim`, installs
`requirements.txt`, and copies in `server/`, `webClient/`, and
`database_presets.json` (see [Database connections](#database-connections)
above) — that file is gitignored, like `.env`/`env.yaml`, so it must exist
locally (even as an empty `[]`) before building. The container runs
`python server/server.py` and exposes port `3000`.

```bash
docker build -t ydyl .
docker run -p 3000:3000 \
  -e GEMINI_API_KEY="your-gemini-api-key" \
  ydyl
```

Note that the app reads its port from `CRBOT_PORT` (default `3000`),
not the `PORT` variable Cloud Run injects automatically — if you deploy
this image as-is, either set `CRBOT_PORT=8080` (or whatever Cloud Run's
configured to expect) or adjust `--port`/the `EXPOSE`/`CMD` to line up.

## Deploying to Cloud Run

On Cloud Run, the app automatically switches its state backend to
Firestore and enforces authentication for every route except the
handful listed in `EXEMPT_ENDPOINTS` (see [`auth.py`](./server/auth.py)) —
signed-out requests are treated as an anonymous identity (one per browser
session, not shared across visitors — see
[Authentication model](#authentication-model)) rather than being rejected
outright, with one anonymous-only restriction applied at the route level:
saving a custom database connection.

Minimum environment for a Cloud Run deployment (built from the
[Dockerfile](#docker) — remember the `CRBOT_PORT` note above, and that
`database_presets.json` needs to exist locally before this builds):

```bash
gcloud run deploy ydyl \
  --set-env-vars GEMINI_API_KEY=...,GOOGLE_CLIENT_ID=...,GCP_PROJECT_ID=your-project,CRBOT_PORT=8080
```

See [`gcp_deploy.sh`](./gcp_deploy.sh) and [`env.yaml`](./env.yaml) for
this repo's own version of the above.

`GCP_PROJECT_ID` (or `GOOGLE_CLOUD_PROJECT`/`GCP_PROJECT`, which Cloud
Run sets automatically) must resolve to a project with a Firestore
database named `ydyl` — see `firestore.Client(project=..., database="ydyl")`
in [`app_config.py`](./server/app_config.py).

`GOOGLE_CLIENT_ID` is optional even on Cloud Run, but without it every
request is treated as anonymous — nobody gets to sign in or save a custom
connection (each visitor's browser session still gets its own isolated
translation history, though).

---

## API reference

All endpoints are JSON in, JSON out, and set a `crbot_session_id`
cookie. Endpoints other than `/api/auth/*`, `/`, `/login`, and static
assets require authentication when running on Cloud Run or when
`GOOGLE_CLIENT_ID` is configured (see [`auth.py`](./server/auth.py)).

| Method & Path | Purpose |
|---|---|
| `GET /api/auth/me` | Who am I? Returns `authenticated`, `user_id`, `session_id`, `auth_required`. |
| `GET/POST /api/config` | `GET`: current session's active DB, auto-execute preference, configured/custom databases, available models. `POST`: update active DB and/or auto-execute preference (saving a *custom* connection is rejected for anonymous users on Cloud Run). |
| `POST /api/translate` | Body: `{ prompt, history, database_url?, gemini_model?, refresh_schema? }`. Returns `{ success, sql, *_tokens, duration }`. |
| `POST /api/execute` | Body: `{ sql, database_url? }` — runs one or more `;`-separated statements and returns per-statement results. Returns the **raw** Postgres error message on failure (intentionally — this is a SQL runner, the user needs the real error to fix their query). |
| `GET /api/history` | Returns recent translations plus per-day usage stats for the current identity — including anonymous Cloud Run visitors, whose history is isolated per browser session. |
| `DELETE/POST /api/history/purge` | Deletes all translation history for the current identity (same per-session isolation for anonymous visitors as above). |

---

## Authentication model

Identity is resolved in this order (see `get_current_user_identity` in
[`auth.py`](./server/auth.py)):

1. **Bearer token** — a Google ID token in the `Authorization` header,
   verified against `GOOGLE_CLIENT_ID` if configured.
2. **IAP / proxy headers** — `X-Goog-Authenticated-User-Email`,
   `X-User-Email`, or `X-User-ID`.
3. **Auth cookie** — `crbot_user_id` or `user_id`.
4. **Anonymous, scoped to the browser session** — if auth is enabled
   (Cloud Run or `GOOGLE_CLIENT_ID` set) but none of the above are
   present, the request is treated as a working-but-restricted anonymous
   identity (`anonymous:<session_id>`, keyed off the `crbot_session_id`
   cookie) rather than being rejected. Each browser session gets its own
   identity — and so its own active-DB/auto-execute state — rather than
   every anonymous visitor sharing one (see `ANONYMOUS_USER_ID_PREFIX` in
   [`auth.py`](./server/auth.py)).
5. **Local fallback** — a single `"global"` identity when auth isn't
   enabled at all (typical local dev) — unaffected by anonymous-user
   scoping, since it's a separate fallback (step 5, not step 4).

Anonymous users can translate and execute SQL against the default/preset
databases, and can view/purge their own translation history too — it's
isolated per browser session (see `ANONYMOUS_USER_ID_PREFIX` above), so
there's nothing to protect it from. The one thing still off-limits is
saving a custom database connection, which needs a more durable identity
than a transient anonymous session — `config_routes.py` explicitly checks
`is_anonymous_user(...)` there and returns a friendly 403.

---

## Data persistence

`state_store.py` abstracts session/connection/history storage behind one
interface (`StateStore`), with two implementations selected once at
startup:

- **`SqliteStateStore`** — local dev. Stores data in a SQLite file at
  `state/ydyl_state.db` (created automatically, including migrations for
  older schema versions).
- **`FirestoreStateStore`** — Cloud Run. Stores the same data in
  Firestore collections (`sessions`, `db_connections`, `translations`)
  under a database named `ydyl`.

Either way, translation history is recorded against a non-sensitive
`username@dbname` identifier (see `get_conn_identifier` in
[`db.py`](./server/db.py)) — raw connection strings, including credentials,
are never written to the history table.

---

## Frontend notes

The frontend is a single static page (`webClient/index.html` +
`client.js`, no build step or framework) served directly by Flask.
Notable pieces:

- **CodeMirror** (SQL mode, Dracula theme) powers the SQL editor;
  **sql-formatter** pretty-prints generated SQL before display.
- **Chart.js** renders the daily usage charts in the history modal.
- **Web Speech API** (browser-native, no library) powers the mic button
  for dictating prompts.
- **Quick prompts** — a row of example prompts above the SQL box to get
  new users going immediately; dismissible, and restorable again from
  inside the Help modal once dismissed.
- **First-run onboarding** — Help opens automatically once per browser
  (via `localStorage`), and the Help button pulses until it's actually
  been clicked.
- All of this — external CDN scripts, quick prompts, onboarding, help
  content — is documented in more detail via comments at the top of
  `client.js` and inside `help.html` itself.

---

## Testing

Two independent suites, both runnable locally with no real credentials of
any kind:

- **`tests/server/`** — a pytest suite covering every server module
  (auth, routes, all four backends, both state-store implementations, the
  full BigQuery billing-project policy and Snowflake credential policy).
  Everything is mocked at the library boundary (fake
  `psycopg2`/`pymysql`/`bigquery`/`snowflake.connector`/`firestore`/`genai`
  clients) - no real Postgres, MySQL, BigQuery, Snowflake, Firestore, or
  Gemini API key is ever needed. Any service-account keys used in tests are freshly
  generated, throwaway RSA keypairs, never real credentials.
- **`tests/e2e/`** — a JS/TS Playwright suite driving a real browser
  against a real Flask server + real (isolated, gitignored) SQLite state
  store. Only `/api/translate` and `/api/execute` are intercepted at the
  browser network layer (`page.route()`), so config/history/custom-
  connection behavior is exercised for real while still never touching a
  real Gemini key or a real target database.

Run both:

```bash
./run_tests.sh
```

Or individually:

```bash
# Backend
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/server/

# E2E - package.json/playwright.config.js live under tests/e2e/, not the
# repo root, so run npm/playwright commands from there (first time:
# npm install && npx playwright install chromium)
cd tests/e2e
npm install
npx playwright install chromium
npx playwright test              # add --headed or --ui while debugging
```

The e2e suite starts its own Flask server on a dedicated port
(`CRBOT_PORT=3100` by default, see `tests/e2e/playwright.config.js`) with a
scratch working directory (`tests/e2e/.e2e-runtime/`, gitignored) so it
never touches your real local `state/ydyl_state.db` or reads your real
`.env` - see `app_config.py`'s `YDYL_SKIP_DOTENV` handling. It's safe to
run alongside `./run_server.sh`'s normal dev server.

---

## Troubleshooting

- **"Gemini API key is not configured."** — set `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, or `GEMINI_PRESET_KEYS`.
- **Cloud Run deploy fails at startup with a `RuntimeError` about
  Firestore** — `GCP_PROJECT_ID` (or equivalent) isn't resolving to a
  project with a `ydyl` Firestore database, or the service account
  lacks Firestore access. This check is intentionally fatal rather than
  falling back to SQLite in a stateless container.
- **Custom database connections don't show up** — you're signed out (or
  being treated as an anonymous visitor on Cloud Run). Sign in with
  Google to unlock these. (Translation history, unlike custom
  connections, works while signed out too — it's just scoped to your
  current browser session rather than following you across devices.)
- **`/api/execute` errors look different from `/api/translate`
  errors** — that's intentional. `/api/execute` returns the real
  Postgres error text since it's a SQL runner; every other endpoint
  returns a generic, logged, correlation-id'd message (see
  `log_and_generalize_error` in [`app_config.py`](./server/app_config.py)) so
  internal details (schema names, hosts, etc.) never leak to the client.