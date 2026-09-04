# yDyL — your Data your Language

yDyL is a natural-language-to-SQL assistant for PostgreSQL-compatible databases,
MySQL-compatible databases, Google BigQuery, and Snowflake. Type a question in plain English (or any
language), review the SQL it generates, and run it — all from a single-page
web app backed by a small Flask API.

- **NL → SQL** via your choice of Google Gemini, Anthropic Claude, or
  OpenAI (switchable per session from the model badge), grounded in a
  live introspection of your database schema (tables, columns,
  constraints, indexes, views, grants, triggers).
- **Multi-turn conversations** — the last 10 turns (prompt, SQL, and
  results) are kept in memory so follow-up questions have context.
- **Multi-database question answering** — mark 2+ connections in scope
  (or switch to "all configured databases") and ask a question without
  saying which one it's about; a cheap triage step decides what's
  actually needed, and results stream in per-database as they finish.
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
   and sends it, your prompt, and recent chat history to whichever LLM
   provider/model your session currently has selected — Gemini by
   default, or Claude/OpenAI if you've switched (see
   [Model selection UI](#model-selection-ui)).
3. The generated SQL appears in the **SQL editor** box for you to review
   or edit — nothing runs automatically unless you've turned on
   **Automatic SQL Execution** in the connection settings.
4. Click **Execute** to run it against the database and see results,
   grouped by statement if the model returned more than one query.

While a translate or execute call is in flight, click **Cancel** to
abandon it — see `POST /api/cancel` in the [API reference](#api-reference).

If the model decides your prompt doesn't need SQL at all (a general
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
│   ├── cancel_registry.py     # Process-local registry backing the Stop button / POST /api/cancel
│   ├── state_store.py         # StateStore abstraction: SqliteStateStore / FirestoreStateStore
│   ├── config_routes.py       # /api/config — session DB + model selection
│   ├── connection_router.py   # "All databases" mode's triage step (see Multi-database question answering)
│   ├── translate_routes.py    # /api/translate — NL -> SQL across 3 LLM providers, API key selection/retry
│   ├── execute_routes.py      # /api/execute, /api/cancel — run SQL, return results
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

### LLM provider / model

`/api/translate` supports three interchangeable LLM providers, registered
under the labels `google` (Gemini under the hood - the default), `anthropic`
(Claude under the hood), and `openai`. There's no fleet-wide provider-select
env var - each signed-in/anonymous session picks its own provider and model
via the model-selection badge in the app's header (see "Model selection UI"
below); that per-session choice is what `/api/translate` actually uses once
one has been saved, falling back to this app's one fleet-wide default
(`google` / `gemini-3.6-flash`, unless `DEFAULT_MODEL` below points
elsewhere) otherwise. See
[`translate_routes.py`](./server/translate_routes.py)'s module docstring
for how a new provider is added (one `LlmProvider` subclass); this section
just covers the env vars for the three that exist today.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_TRANSLATION_ATTEMPTS` | `5` | Max attempts (initial call + retries) for a single translation request before giving up, for transient (rate-limit/server-error/connection) failures. Shared by all three providers — see [`translate_routes.py`](./server/translate_routes.py). (Formerly `MAX_GEMINI_ATTEMPTS`.) |
| `TRANSLATION_RETRY_DELAY_SECONDS` | `1` | Seconds to wait between transient-error retry attempts. Shared by all three providers. (Formerly `GEMINI_RETRY_DELAY_SECONDS`.) |
| `TRANSLATION_TIMEOUT_SECONDS` | `60` | How long one call to the configured LLM provider may take before it's treated as a timeout (a retryable transient failure, same as a 5xx — see `MAX_TRANSLATION_ATTEMPTS` above). Shared by all three providers. |
| `DEFAULT_MODEL` | — | Names exactly one model (e.g. `claude-opus-5`) to use as the default, instead of whichever provider's `*_MODELS` list happens to list a model first. Only takes effect for whichever provider's own `*_MODELS` list actually contains that model name — every other provider is unaffected and keeps using its own first entry. When no session has picked a provider at all yet, this also decides which provider becomes the app's one fleet-wide default (e.g. setting it to a Claude model moves the whole fleet's default off Google, with no separate provider-name variable needed). Falls back to Google / `gemini-3.6-flash` when unset, blank, or naming a model nothing configured actually offers. |

#### Google (Gemini)

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | A single Gemini API key. Either name works. |
| `GEMINI_PRESET_KEYS` | — | Comma-separated list of additional Gemini API keys. The app picks one at random per request and, on a rate-limit (429) error, automatically retries with a different key from the pool — immediately, with no delay, for up to one attempt per configured key (this budget is independent of `MAX_TRANSLATION_ATTEMPTS` above and is a Gemini-only mechanism; no other provider rotates keys this way). See [`translate_routes.py`](./server/translate_routes.py) for the full retry policy. |
| `GOOGLE_MODELS` | `gemini-3.6-flash` | Comma-separated list of models this provider can use. The **first** entry is the default used for translation (per-request override: `gemini_model` or the generic `model`; per-session override via the model-selection UI), unless `DEFAULT_MODEL` above names a different model from this same list. The full list is what the model-selection UI offers for Google. |

At least one of `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or
`GEMINI_PRESET_KEYS` must be set, or `/api/translate` returns a 400
("Google API key is not configured").

#### Anthropic (Claude)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | A single Claude API key. |
| `CLAUDE_PRESET_KEYS` | — | Comma-separated list of additional Claude API keys - same pool-of-keys idea as `GEMINI_PRESET_KEYS`, but a rate limit here never rotates keys, it just waits and retries the same key (see `_classify_claude_error` in [`translate_routes.py`](./server/translate_routes.py)). |
| `ANTHROPIC_MODELS` | `claude-sonnet-5` | Comma-separated list of models this provider can use - same "first entry is the default, unless overridden by `DEFAULT_MODEL`" convention as `GOOGLE_MODELS`. |

At least one of `ANTHROPIC_API_KEY` or `CLAUDE_PRESET_KEYS` must be set to
select Anthropic via the model-selection UI, or `/api/translate` returns a
400 ("Anthropic API key is not configured").

#### OpenAI

Built on the [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
(`client.responses.create`), not the older Chat Completions API - see
`_call_openai` in [`translate_routes.py`](./server/translate_routes.py).

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | A single OpenAI API key. |
| `OPENAI_PRESET_KEYS` | — | Comma-separated list of additional OpenAI API keys - same pool-of-keys idea as `CLAUDE_PRESET_KEYS`; a rate limit here never rotates keys either, it just waits and retries the same key. |
| `OPENAI_MODELS` | `gpt-5.6-luna` | Comma-separated list of models this provider can use - same "first entry is the default, unless overridden by `DEFAULT_MODEL`" convention as `GOOGLE_MODELS`. |

At least one of `OPENAI_API_KEY` or `OPENAI_PRESET_KEYS` must be set to
select OpenAI via the model-selection UI, or `/api/translate` returns a 400
("OpenAI API key is not configured").

#### Model selection UI

The header shows a model badge (next to the database connection badge)
naming the model currently in use; clicking it opens a modal listing every
provider's models (see the `*_MODELS` env vars above) as radio buttons,
organized by provider (labeled Google/Anthropic/OpenAI). Saving a
selection there persists it on the current session (`state_store.py`'s
`llm_provider`/`llm_model` fields, same persistence mechanism the active
database connection already uses) via `POST /api/config`, and every
subsequent `/api/translate` call from that session uses it, though a
request-body override (`gemini_model`/`claude_model`/`openai_model`/
`model`) still wins over the session's saved choice when both are present.
A session that has never saved a selection falls back to this app's one
hardcoded default (`google` / `gemini-3.6-flash`), exactly as before this
UI existed.

### Database connections

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PRESETS_FILE` | — (no presets) | Path to a JSON file listing the admin-configured preset connections shown in the UI. Supports Postgres, MySQL, and BigQuery presets — see the shape below. If unset, the app falls back to a single synthetic "Default DB" Postgres preset pointing at `postgresql://postgres:password@host:23456/defaultdb?sslmode=verify-full`. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Bounds how long establishing a new database connection may take, so a wrong/unreachable host fails fast instead of hanging indefinitely. Covers every dialect except Databricks (no connect-only timeout knob in its driver) and BigQuery (doesn't dial out synchronously). See [`backends/base.py`](./server/backends/base.py). |
| `SQL_EXECUTE_TIMEOUT_SECONDS` | `30` | Bounds how long running a query may take once the connection is already open — the execute-time counterpart to `DB_CONNECT_TIMEOUT_SECONDS` above. Applies to `/api/execute` and the `/api/ping` liveness check. Set to `0` to disable. See [`execute_routes.py`](./server/execute_routes.py). |

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

### Multi-database question answering

The connection badge is a radio choice between a **single active
connection** (the default) and **all configured databases**
(`in_scope_mode`, "single" or "all"). Switching to "all" also opens a
checkbox list, in the same badge's modal, for marking which
presets/custom connections are actually "in scope" for questions
(`in_scope_preset_ids`/`in_scope_custom_connection_keys`) — a session no
longer has to say *which* connection a question is about before asking
it.

In "all" mode, every question first goes through a cheap **triage** call
([`connection_router.py`](./server/connection_router.py)'s
`triage_all_mode_question`) that decides, from just the in-scope
connections' names/dialects/table names — no full schema fetch yet —
whether the question can be answered directly (e.g. "which of these are
Postgres?") or genuinely needs real data, and if so from which specific
connection(s). Only when real data is needed does the app fetch full
column-level schema and generate SQL, one call per selected connection
in parallel, each tagged with a leading
`-- database: preset:<id>|custom:<key> (<name>)` comment; `/api/execute`
dispatches each tagged statement to its own connection. Results stream
back into their own tab as each connection finishes — you don't wait for
the slowest one to see the first result — and once every connection has
settled, a final call summarizes the combined results into a leading
**Results Summary** tab (see the module docstrings on
[`translate_routes.py`](./server/translate_routes.py),
[`connection_router.py`](./server/connection_router.py), and
[`execute_routes.py`](./server/execute_routes.py) for the full
triage/generation/summary design). The marker comment itself is stripped
back out before a statement ever reaches its own connection's `execute()`
call, so it plays no part in the actual query — this matters beyond
tidiness for a Google Sheets connection specifically, since GViz (its
query language) has no comment syntax at all and would otherwise reject a
marker-tagged statement outright (see `execute_routes.py`'s
`_strip_database_marker_lines`).

A session in "single" mode — the default, and the overwhelming majority
of sessions — takes none of this: no triage call, no `-- database:`
comment, no added latency, and `/api/translate`'s/`/api/execute`'s
response shapes are byte-identical to before this feature existed.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_IN_SCOPE_CONNECTIONS` | `20` | The one cap on how many database connections are involved anywhere in multi-database question-answering: how many presets/custom connections a user may mark "in scope" at once (see [`config_routes.py`](./server/config_routes.py)), and how many of those in-scope connections a single question's Phase A routing may select for one response (clamped server-side regardless of what the model returns — see [`connection_router.py`](./server/connection_router.py)). See [`app_config.py`](./server/app_config.py). |
| `ROUTER_MAX_TABLE_NAMES_PER_CONNECTION` | `200` | Caps how many table/tab names go into Phase A's routing prompt per candidate connection — independent of `SCHEMA_MAX_TABLES`, since the router only ever needs names, never full column-level schema. See [`backends/base.py`](./server/backends/base.py). |

### Authentication

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_ID` | — | Enables Google Sign-In and ID-token verification. If unset, the app runs with auth disabled locally (or as an anonymous user, one identity per browser session, on Cloud Run). |

See [Authentication model](#authentication-model) for the full picture,
including how identity is resolved and what anonymous users can/can't do.

### Encryption at rest

| Variable | Default | Purpose |
|---|---|---|
| `DB_CONFIG_ENCRYPTION_KEY` | — | Encrypts every saved connection's `database_config` (passwords, service-account keys, private keys, CA certificates, ...) before it's written to SQLite/Firestore, and decrypts it transparently on read. A [Fernet](https://cryptography.io/en/latest/fernet/) key — generate one with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Required on Cloud Run** — like `K_SERVICE`/Firestore below, the app refuses to start without a valid one there; optional locally, where an unset/invalid key just means `database_config` is stored unencrypted, same as today. |

The whole `database_config` value is encrypted as one blob rather than
picking individual "sensitive" fields — a field added to any backend's
config later is automatically covered, with nothing to remember to add to
an allowlist. A row saved before this was configured (or under a
previously-configured key) keeps reading correctly with no migration
step: decryption is tried first and silently falls back to the
row's original plain representation on any failure. Losing the key means
losing access to every already-saved connection's credentials (there's
no recovery path by design — that's what "encrypted" means); rotating it
means previously-saved connections need to be re-saved to pick up the new
key. See [`state_store.py`](./server/state_store.py)'s "Encryption at
rest for database_config" comment for the full design.

### Issue reporting ("Report Error" / "Report Wrong Result")

Lets a user flag either a raw database error caused by SQL the model
generated incorrectly (execute_routes.py's `/api/execute` intentionally
shows those verbatim — see that module's docstring) or a "wrong result"
(a successful response — a table, a summarization, any other reply given
directly by the model — the user believes is wrong or misleading). The
user always reviews the exact email content client-side before sending.
Reports are emailed by the app itself over SMTP, not via a `mailto:` link.

| Variable | Default | Purpose |
|---|---|---|
| `ISSUE_REPORT_TO_EMAIL` | — | Recipient address for reports. **Required** to activate the feature at all — leave unset and the Report buttons never appear client-side (see `config_routes.py`'s `issue_reporting_enabled` field). |
| `ISSUE_REPORT_SMTP_HOST` | — | SMTP host the app connects to send the report. Also required for the feature to activate. |
| `ISSUE_REPORT_SMTP_PORT` | `587` | SMTP port (587 = STARTTLS submission, the common case for both self-hosted mail servers and providers like Gmail/SendGrid/SES). |
| `ISSUE_REPORT_SMTP_USERNAME` | — | SMTP auth username. Optional — some internal relays allow anonymous submission from trusted IPs. |
| `ISSUE_REPORT_SMTP_PASSWORD` | — | SMTP auth password. |
| `ISSUE_REPORT_SMTP_FROM` | `ISSUE_REPORT_SMTP_USERNAME` | Envelope/header From address. Falls back to the SMTP username; set explicitly when the username isn't itself a real mailbox address (e.g. an API-key-shaped username). Required (directly or via the username fallback) for the feature to activate. |
| `ISSUE_REPORT_SMTP_USE_TLS` | `1` | STARTTLS on connect. Set to `0` only for a relay that expects a plaintext/already-implicit-TLS connection instead. |

See [`report_routes.py`](./server/report_routes.py) for the full design,
including what gets included in a report's email body and the length cap
applied to every free-text field.

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
  --set-env-vars GEMINI_API_KEY=...,GOOGLE_CLIENT_ID=...,GCP_PROJECT_ID=your-project,CRBOT_PORT=8080,DB_CONFIG_ENCRYPTION_KEY=...
```

`DB_CONFIG_ENCRYPTION_KEY` isn't optional here the way `GOOGLE_CLIENT_ID`
is — see [Encryption at rest](#encryption-at-rest) above; the app halts
at startup without a valid one. Prefer passing it via `--set-secrets`
from [Secret Manager](https://cloud.google.com/secret-manager) rather
than `--set-env-vars` for a real deployment, the same way you'd handle
any other production secret.

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
| `GET/POST /api/config` | `GET`: current session's active DB, auto-execute preference, configured/custom databases, available models, and the multi-database in-scope set (`in_scope_preset_ids`/`in_scope_custom_connection_keys`/`max_in_scope_connections` — see [Multi-database question answering](#multi-database-question-answering)). `POST`: update active DB, auto-execute preference, and/or the in-scope set (saving a *custom* connection is rejected for anonymous users on Cloud Run; an in-scope set must never end up empty). |
| `POST /api/translate` | Body: `{ prompt, history, database_url?, gemini_model?/claude_model?/openai_model?/model?, refresh_schema?, pinned_connections? }` — a model override always wins over the session's saved model-selection choice (see [Model selection UI](#model-selection-ui)). Returns `{ success, sql, *_tokens, duration }`, plus `connection_selection: [{kind, id, name}, ...]` in "all" mode (see [Multi-database question answering](#multi-database-question-answering)). |
| `POST /api/execute` | Body: `{ sql, database_url?, pinned_connections? }` — runs one or more `;`-separated statements and returns per-statement results. A script with no `-- database: ...` marker runs against one connection exactly as before; a marker-tagged script (see [Multi-database question answering](#multi-database-question-answering)) dispatches each connection's statements independently and the response gains a `failures` list plus a `database` field per result when at least one connection's statements fail. Returns the **raw** Postgres error message on failure (intentionally — this is a SQL runner, the user needs the real error to fix their query). |
| `POST /api/cancel` | Best-effort abandonment of whatever's currently in flight for this browser session (an `/api/translate` LLM call, or an `/api/execute` database call) — backs the header's **Cancel** button. Always returns `{ success: true, cancelled: <count> }`, even when nothing was registered (the work may have already finished on its own). See [`cancel_registry.py`](./server/cancel_registry.py) for the mechanism and its limits — it's a best-effort nudge (closing whatever connection/client the work is blocked on), not a guaranteed hard stop. |
| `GET /api/history` | Returns recent translations plus per-day usage stats for the current identity — including anonymous Cloud Run visitors, whose history is isolated per browser session. |
| `DELETE/POST /api/history/purge` | Deletes all translation history for the current identity (same per-session isolation for anonymous visitors as above). |
| `POST /api/report-issue` | Body: `{ category: "error"\|"wrong_result", prompt?, sql?, database_name?, provider?, model?, content?, details? }` — emails a report to `ISSUE_REPORT_TO_EMAIL` (see [Issue reporting](#issue-reporting-report-error--report-wrong-result)). Returns `503` if the feature isn't configured, `400` for an invalid/missing `category`. |

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

`GET /api/history` (the history popup) returns two things from
`get_translation_history()`: the translations list itself, capped to
`TRANSLATION_HISTORY_LIST_LIMIT` most-recent rows (sorted newest-first),
and the aggregated per-day stats shown on the popup's Statistics tab,
which are always computed over the user's **complete** history —
uncapped, regardless of how many rows the list above is limited to.

| Variable | Default | Purpose |
|---|---|---|
| `TRANSLATION_HISTORY_LIST_LIMIT` | `50` | How many rows the history popup's translations list shows, most-recent first. Does not affect the popup's Statistics tab (always the full history) or how much history is actually stored (`DELETE/POST /api/history/purge` is still the only way to remove rows). See [`state_store.py`](./server/state_store.py). |

Every saved connection's `database_config` is encrypted at rest before
either backend ever writes it — see [Encryption at
rest](#encryption-at-rest) above.

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
  `psycopg2`/`pymysql`/`bigquery`/`snowflake.connector`/`firestore`/`genai`/
  `anthropic`/`openai` clients) - no real Postgres, MySQL, BigQuery,
  Snowflake, Firestore, Gemini, Claude, or OpenAI API key is ever needed.
  Any service-account keys used in tests are freshly
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

- **"Google API key is not configured."** — set `GEMINI_API_KEY`,
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