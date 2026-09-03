"""
translate_routes.py

Natural-language-to-SQL translation: API key selection, chat history ->
provider-native input conversion, the system prompt, and the
/api/translate route itself. Three LLM providers are supported today -
Google (the original/default, still "Gemini" under the hood - see
GeminiProvider), Anthropic ("Claude" under the hood - see ClaudeProvider),
and OpenAI - registered under the labels "google"/"anthropic"/"openai" in
_LLM_PROVIDERS below. There is deliberately no fleet-wide provider-select
env var (there used to be one, LLM_PROVIDER - removed since a session with
nothing saved just needs ONE hardcoded default provider+model pair, not an
independently configurable provider-name knob to keep in sync with it -
see get_llm_provider()'s docstring). A session picks its own provider/model via
the model-selection UI (state_store.py's llm_provider/llm_model), resolved
per-request in translate_query() below.

Provider dispatch goes through the LlmProvider interface (see that class's
docstring further down): translate_query()/stream_translation() call
methods on a single `provider` object rather than branching on the active
provider's name themselves at each step. This is what makes adding a
FOURTH provider later a matter of writing one new LlmProvider subclass and
adding one line to _LLM_PROVIDERS, rather than finding and extending every
`if provider == ...` branch in this file - there used to be about half
a dozen of those (client construction, model/key selection, history
building, the call itself, error classification, key-rotation logic)
before this was introduced.

Each provider's SDK-specific mechanics (key pool, error classification,
history shape, the actual API call) still live in their own free
functions/constants below (get_gemini_api_keys/_classify_claude_error/
build_openai_history_messages/_call_gemini/etc.) exactly as before this
dispatch layer was added - the LlmProvider subclasses are thin adapters
over those, not a rewrite of them. This matters for testing: existing
tests that patch translate_routes.genai.Client or call
translate_routes.pick_claude_api_key() directly keep working unchanged,
since those names and their behavior didn't move.

/api/translate streams its response as newline-delimited JSON (NDJSON)
rather than a single JSON body, so a client can show live "retrying..."
feedback while the retry loop below (the single place in this app that
retries a translation - see MAX_TRANSLATION_ATTEMPTS/LlmProvider.classify_error)
works through a transient LLM failure, instead of the request just
appearing to hang. See translate_query()'s stream_translation() for the
exact line shapes and the HTTP-status-code trade-off streaming requires.
"""

import concurrent.futures
import json
import random
import os
import re
import time
from abc import ABC, abstractmethod

from flask import Blueprint, request, jsonify, Response, stream_with_context
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
import anthropic
import openai
import httpx
try:
    # google-genai vendors a drop-in httpx fork under this separate import
    # namespace for some of its internal transport - see
    # _classify_gemini_error's TRANSLATION_TIMEOUT_SECONDS case below for
    # why both need checking. Not a direct dependency of this app; guarded
    # in case a future google-genai release drops it.
    import httpx2
except ImportError:  # pragma: no cover - present today via google-genai
    httpx2 = None

# from app_config import logger, log_and_generalize_error
from app_config import logger, state_store, MAX_TRANSLATION_ATTEMPTS, TRANSLATION_RETRY_DELAY_SECONDS

from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import (
    resolve_conn_str, get_database_schema, record_translation,
    record_all_databases_triage,
    resolve_in_scope_descriptors, build_router_candidate_summaries,
)
from backends import get_backend
from connection_router import triage_all_mode_question, is_label_only_response
import cancel_registry

translate_bp = Blueprint('translate', __name__)

# Which LLM provider a request actually uses is resolved per-session (see
# translate_query()'s session_data.get('llm_provider') lookup below), never
# a fleet-wide env var - "google"/"anthropic"/"openai" are the only valid
# values, matching _LLM_PROVIDERS' keys below. A session that never
# explicitly picked one (via the model-selection UI) falls back to this
# app's one hardcoded default: whichever provider get_llm_provider()
# returns for an unrecognized/blank name - see that function's docstring.
# This used to be independently configurable via an LLM_PROVIDER env var;
# that's gone now, since there's no longer a separate provider-name knob
# to keep in sync with the hardcoded default model below - one hardcoded
# default (the model) fully implies the other (its provider).

# Each provider's own *_MODELS env var (GOOGLE_MODELS/ANTHROPIC_MODELS/
# OPENAI_MODELS - see LlmProvider.models_env_var below) is a single
# comma-separated list doing double duty: its FIRST entry is that
# provider's default model (used when neither a request nor a saved
# session choice picks one - see LlmProvider.default_model), and the full
# list is what the model-selection modal offers for that provider (see
# LlmProvider.preset_models). Left entirely unset, each provider falls
# back to its own hardcoded single-model default (LlmProvider.
# fallback_models) so this app works out of the box with zero model
# configuration - claude-sonnet-5 for Anthropic (strong structured-output
# reasoning at a much lower cost than the top-tier model), gpt-5.6-luna for
# OpenAI (its cost-efficient tier - "gpt-5.6" alone, no suffix, is an alias
# for the top "-sol" tier instead), gemini-3.6-flash for Google - this last
# one doubles as the app's ONE fleet-wide default when a session hasn't
# picked a provider at all yet (see get_llm_provider() below and the
# comment above this one).

# Per-dialect opening lines for the system instruction below - the rest of
# the instruction (output format, NO SQL sentinels, etc.) is identical
# across dialects, only the "what SQL flavor am I writing" framing differs.
# Keyed by Backend.dialect_name (see backends/base.py/postgres.py/
# bigquery.py) so a new backend just needs an entry here to get a properly
# targeted prompt instead of silently inheriting Postgres's.
_DIALECT_PROMPT_INTROS = {
    "PostgreSQL": (
        "You are an expert SQL generation assistant for PostgreSQL-compatible RDBMSs.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid SQL.\n"
        "You may return one or more independent SQL statements. You may use PL/pgSQL Functions or Procedures, if appropriate.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "BigQuery Standard SQL": (
        "You are an expert SQL generation assistant for Google BigQuery (Standard SQL / GoogleSQL).\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid BigQuery Standard SQL.\n"
        "You may return one or more independent SQL statements, and BigQuery scripting (DECLARE/IF/LOOP) where appropriate.\n"
        "Use backticks for identifiers that need quoting; never use double quotes for identifiers - BigQuery treats double-quoted text as a string literal, not an identifier.\n"
        "Some schema entries are labeled 'Table family: `project.dataset.prefix_*`' instead of a single table - "
        "these describe a family of date-sharded tables (e.g. prefix_20240101, prefix_20240102, ...) that all "
        "share the same columns. For these, NEVER query a literal single-date table name (e.g. `project.dataset.prefix_20240115`) "
        "unless the user's request is unambiguously about exactly one specific date and that exact table is known to exist. "
        "Instead, query the family using BigQuery's wildcard-table syntax exactly as shown in the schema (`project.dataset.prefix_*`), "
        "and filter/select the relevant shard(s) using the _TABLE_SUFFIX pseudo-column, e.g. "
        "WHERE _TABLE_SUFFIX BETWEEN '20240101' AND '20240131' for a date range, or WHERE _TABLE_SUFFIX = '20240115' for one specific day. "
        "_TABLE_SUFFIX is only valid when the FROM clause uses the wildcard (`prefix_*`) form.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Snowflake SQL": (
        "You are an expert SQL generation assistant for Snowflake.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Snowflake SQL.\n"
        "You may return one or more independent SQL statements, and Snowflake Scripting (DECLARE/BEGIN/IF/FOR) where appropriate.\n"
        "A Snowflake Scripting block is NEVER valid as a bare top-level statement when run through a database driver (only Snowsight's worksheet UI allows that shorthand) - it MUST be wrapped as an anonymous block: EXECUTE IMMEDIATE $$ ... $$; with the DECLARE/BEGIN...END block placed inside the $$ ... $$ dollar-quoted string, END followed immediately by a semicolon before the closing $$. A bare DECLARE/BEGIN/END with no EXECUTE IMMEDIATE wrapper will fail with a syntax error.\n"
        "Use double quotes for identifiers that need quoting (Snowflake's default, case-sensitive form); unquoted identifiers are treated as upper-case.\n"
        "Snowflake has no enforced PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "MySQL": (
        "You are an expert SQL generation assistant for MySQL-compatible RDBMSs.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid MySQL SQL.\n"
        "You may return one or more independent SQL statements, and MySQL stored-program constructs (DECLARE/IF/LOOP/WHILE) where appropriate.\n"
        "Use backticks for identifiers that need quoting; MySQL treats double-quoted text as a string literal by default (like standard SQL), not an identifier.\n"
        "MySQL has no schemas separate from databases - a schema and a database are the same thing here.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Databricks SQL": (
        "You are an expert SQL generation assistant for Databricks SQL (Spark SQL).\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Databricks SQL.\n"
        "You may return one or more independent SQL statements, and Databricks SQL scripting (DECLARE/IF/WHILE/FOR) where appropriate.\n"
        "Use backticks for identifiers that need quoting.\n"
        "The connection has a default catalog and schema already selected, so plain table names (not schema-qualified or catalog-qualified) resolve correctly - do not prefix table names with a catalog or schema unless the user explicitly asks to query a different one.\n"
        "Databricks (Unity Catalog) does not enforce PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Oracle Database": (
        "You are an expert SQL generation assistant for Oracle Database.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Oracle SQL.\n"
        "You may return one or more independent SQL statements, and PL/SQL (DECLARE/BEGIN/END blocks, or CREATE PROCEDURE/FUNCTION) where appropriate.\n"
        "Always terminate a PL/SQL anonymous block (DECLARE/BEGIN/END) or a CREATE PROCEDURE/FUNCTION/PACKAGE/TRIGGER/TYPE body with a bare '/' alone on its own line right after the block's closing 'END;' - the standard SQL*Plus/SQLcl convention - so the block's own internal semicolons (one per declaration, one per statement) are never mistaken for the end of the block.\n"
        "Use double quotes for identifiers that need quoting; unquoted identifiers are folded to upper-case, so schema entries shown in upper-case (the common case) resolve correctly unquoted - only quote an identifier if it needs to preserve lower/mixed case or contains special characters.\n"
        "Oracle has no LIMIT clause - use FETCH FIRST n ROWS ONLY (or ROWNUM/ROW_NUMBER() for older-style pagination) to cap result rows.\n"
        "Every SELECT must have a FROM clause - use FROM DUAL for a query that doesn't otherwise reference a table (e.g. SELECT SYSDATE FROM DUAL).\n"
        "String literals use single quotes only; double quotes are exclusively for identifiers, never string values.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Amazon Redshift SQL": (
        "You are an expert SQL generation assistant for Amazon Redshift.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Redshift SQL.\n"
        "Redshift SQL is derived from PostgreSQL - most standard SQL constructs from that dialect apply, but Redshift has limited support for PL/pgSQL-style procedural code (CREATE PROCEDURE using a small subset of PL/pgSQL is supported in recent versions; prefer plain SQL statements otherwise).\n"
        "Use double quotes for identifiers that need quoting, same as PostgreSQL.\n"
        "Redshift has no enforced PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
        "Redshift has no CREATE INDEX / index concept at all - schema entries instead list each table's DISTSTYLE/DISTKEY (how rows are distributed across compute nodes) and SORTKEY (how rows are ordered on disk); do not suggest creating an index, and do not invent WHERE-clause assumptions based on indexes that don't exist here.\n"
        "Redshift has no trigger support.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Microsoft SQL Server": (
        "You are an expert SQL generation assistant for Microsoft SQL Server.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid T-SQL.\n"
        "You may return one or more independent SQL statements, and T-SQL procedural code (BEGIN...END blocks, DECLARE @variable, IF/WHILE) where appropriate; parameter and local variable names are always @-prefixed.\n"
        "Use square brackets for identifiers that need quoting (e.g. [Order Date]); unquoted identifiers are case-insensitive by default.\n"
        "SQL Server has no LIMIT clause - use SELECT TOP (n) ... to cap result rows (e.g. SELECT TOP (10) * FROM Orders), or OFFSET/FETCH NEXT for pagination.\n"
        "Table and view names in the schema section below are shown schema-qualified (schema.table) whenever this connection targets a non-default schema - always use that exact qualified form in generated SQL (FROM, JOIN, INTO, UPDATE, DELETE FROM, etc.) rather than dropping the schema prefix, since T-SQL has no session-level default-schema override the way Postgres's search_path or Oracle's ALTER SESSION does.\n"
        "SQL Server DOES enforce PK/FK/UNIQUE constraints at write time - schema entries listing them describe real constraints the database will reject violations of, not merely informational metadata.\n"
        "Never emit a GO statement - it is a batch separator recognized only by client tools (sqlcmd/SSMS), not valid T-SQL syntax, and the database driver here will reject it as a syntax error.\n"
        "Use the correct system view or function schemas to prevent common mistakes (e.g., `sys.fn_my_permissions` returns `entity_name`, `subentity_name`, and `permission_name`).\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
    "Google Visualization API Query Language": (
        "You are an expert SQL generation assistant for Google's Visualization API Query Language - the query language behind a spreadsheet's own =QUERY() formula.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Gogle Visualization API Query Language.\n"
        "This is NOT standard SQL: it has NO FROM clause at all - the data source (the spreadsheet tab) is always implicit, so NEVER write FROM anything, not even the tab's name.\n"
        "There are no JOINs, no subqueries, and no CASE/COALESCE/CAST - this grammar simply doesn't have them; do not attempt to work around their absence with unsupported syntax.\n"
        "Reference columns ONLY by the spreadsheet letter shown in the schema (A, B, C, ...) - never by header/label text, even though the schema also shows each column's label for readability.\n"
        "Supported clauses: select, where, group by, pivot, order by, limit, offset, label, format, options.\n"
        "When you use the GROUP BY clause you must include an aggregation even if the user did not request that. In that case include a COUNT of the same column you GROUP BY.\n"
        "Supported functions: year(), month(), day(), quarter(), dayOfWeek(), hour(), minute(), second(), millisecond(), dateDiff(), toDate(), now(), upper(), lower(), plus the aggregates sum(), avg(), count(), min(), max() (valid only alongside group by). Do not invent clauses or functions outside this list.\n"
        "String literals use single quotes.\n"
        "ABSOLUTELY NEVER add any comments or explanations in the query itself - before it, after it, or inline - even if you are asked to, since this dialect does not support commenting at all.\n"
        "Return EXACTLY ONE query - this dialect has no multi-statement/batch concept, so never return multiple semicolon-separated statements, and do not end the query with a trailing semicolon.\n"
    ),
    "MongoDB Atlas SQL": (
        "You are an expert SQL generation assistant for MongoDB's Atlas SQL Interface (\"MongoSQL\"), a SQL-92-compatible dialect that lets SQL query MongoDB's native document data.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid MongoSQL.\n"
        "This connection is READ-ONLY: you may ONLY ever generate SELECT statements (optionally preceded by a WITH clause). NEVER generate INSERT, UPDATE, DELETE, MERGE, or any DDL (CREATE/ALTER/DROP) - MongoSQL has no write path at all, and a write statement will simply be rejected before it ever reaches the database.\n"
        "Each MongoDB collection is presented in the schema below as a table, and each of its (already type-inferred, already flattened) document fields as a column - the underlying documents are schemaless, so treat the schema as this driver's best-effort inference, not a guaranteed rigid structure the way a real RDBMS table is.\n"
        "Stick to the SQL-92 core (SELECT/FROM/WHERE/GROUP BY/HAVING/ORDER BY/LIMIT, standard aggregates, standard joins) rather than another dialect's vendor-specific functions or syntax extensions - MongoSQL does not implement Postgres/MySQL/T-SQL-specific functions.\n"
        "Use double quotes for identifiers that need quoting; string literals use single quotes only.\n"
        "Wrap all column names inside backticks (`) to prevent clashing with reserved keywords.\n"
        "You may return one or more independent SELECT statements if the user's request calls for it.\n"
        "If asked to document the SQL command, add comments at the top of the query using the supported convention (if there is any) for how to mark comments.\n"
    ),
}
_DEFAULT_DIALECT_PROMPT_INTRO = _DIALECT_PROMPT_INTROS["PostgreSQL"]

# The output-format/behavior rules that follow the dialect intro in the
# system prompt - identical for every dialect, so it's pulled out once here
# rather than duplicated per dialect entry above.
_COMMON_FORMAT_RULES = (
    "Format the result data to be easily readable. For example, format timestamps as date:hour:min:sec.\n"
    "Return ONLY the raw SQL code block. Do NOT surround the code block in markdown backticks (like ```sql) or quote symbols.\n"
    "If you can respond to the prompt succinctly based on your general-purpose training, return your response prepended by the string '*** NO SQL ***'\n"
    "If the prompt is about the data available in the database that is currently configured, return your response based on your knowledge of the schema and include an ER diagram using ascii art. Prepend the string '*** NO SQL ***' to your response\n"
    "If the prompt is about this app itself, respond as follows: '*** NO SQL *** OPEN HELP POPUP ***'.\n"
    "If you cannot respond at all with reasonable confidence, return '*** NO SQL *** I am not able to respond to your prompt.'\n"
    "If you run into any error, return '*** NO SQL *** I ran into this error: <the error>'.\n"
    "If you want to respond partly with a SQL command and partly with free text, enclose the free text as follows 'SELECT <your free-text response in quotes> as RESPONSE;'.\n"
    "If a user asks you who you are or what model you are using, hide this behind a generic response.\n"
    "Always write any free-text content you produce (the substance of a '*** NO SQL ***' reply, an error explanation, or SQL comments if asked to document the query) in the SAME LANGUAGE as the user's most recent prompt below - regardless of the language used in the database schema, table/column names, or earlier chat history. Do not translate the fixed literal markers themselves ('*** NO SQL ***', 'OPEN HELP POPUP', 'RESPONSE') - only the actual text you write.\n"
)

# Past-turn query results embedded back into the prompt as chat history were
# previously uncapped (max_rows=len(rws) - i.e. "show all of them"). A wide
# result set from even one earlier turn, multiplied across up to 20 retained
# history turns, is exactly what can blow a prompt out to millions of
# tokens - this is what tripped Claude's 1M-token request limit. This caps
# how many rows of a PAST turn's results get serialized back into the LLM
# prompt; it has no effect on what the current turn's results show in the
# UI. Override via env var if 50 is too aggressive/lenient for your data.
HISTORY_RESULT_MAX_ROWS = int(os.environ.get("HISTORY_RESULT_MAX_ROWS", 50))

# How many conversational turns of history are sent back to the LLM. A
# "turn" here is a user message + the model's reply to it - 2 entries in
# the history list per turn - so the default of 10 turns keeps the last 20
# entries, same as the previous hardcoded -20 slice. Configurable since a
# large schema/result-heavy app may need this lower to stay under a
# provider's token limit (see HISTORY_RESULT_MAX_ROWS above for the other
# lever on that same problem).
HISTORY_MAX_TURNS = int(os.environ.get("HISTORY_MAX_TURNS", 10))

# There are two, INDEPENDENT retry mechanisms below, each with its own
# budget - they used to share one counter (MAX_GEMINI_ATTEMPTS), which
# quietly conflated two unrelated things now that both Gemini and Claude
# are supported. Keep them straight:
#
# 1. Transient-error retries (MAX_TRANSLATION_ATTEMPTS /
#    TRANSLATION_RETRY_DELAY_SECONDS below) - a provider's own backend is
#    momentarily struggling (a 5xx, a dropped connection), unrelated to
#    which API key was used. The SAME key is reused, after a
#    TRANSLATION_RETRY_DELAY_SECONDS pause to give the problem a moment to
#    clear. This bucket is shared by BOTH providers - see
#    _classify_gemini_error/_classify_claude_error - and bounded by
#    MAX_TRANSLATION_ATTEMPTS total calls (initial call + retries).
#
# 2. Gemini's own key-rotation retry (see _classify_gemini_error's 429
#    case) - a per-key rate limit/capacity exhaustion, where the fix is
#    simply "use a different configured key", not "wait". This is a
#    Gemini-specific hack: it only exists because this app supports
#    configuring a POOL of Gemini keys (GEMINI_PRESET_KEYS) to rotate
#    through, a pattern Claude isn't assumed to have (see
#    _classify_claude_error's docstring). Its retry fires immediately (no
#    delay - the next key was never subject to the limit that just hit),
#    and its OWN budget is simply "one attempt per configured key" - i.e.
#    it keeps going until every key in GEMINI_PRESET_KEYS has been tried
#    once (see the retry loop below), a count that has nothing to do with
#    MAX_TRANSLATION_ATTEMPTS and isn't a separate env var of its own.
#
# Anything that isn't one of these two retryable kinds (bad request,
# invalid model, auth failure, etc.) just fails the same way every time,
# so it's raised immediately instead of wasting a retry on it.
#
# MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS are configurable
# via env vars (e.g. to tune retry behavior for a noisier rollout without a
# code change) - same int()/float()-on-getenv pattern as
# SCHEMA_CACHE_TTL_SECONDS in schema_cache.py. Formerly named
# MAX_GEMINI_ATTEMPTS/GEMINI_RETRY_DELAY_SECONDS - renamed now that they
# govern both providers' transient-error retries, not just Gemini's; there's
# no back-compat alias, so an existing deployment setting the old names
# needs updating. Now DEFINED in app_config.py, not here (imported above) -
# connection_router.py's triage_all_mode_question needs the same two
# constants for its own retry loop, and this module already imports FROM
# connection_router.py, so the reverse import would be circular (see
# app_config.py's own comment on this, right above where these now live).

# --- LLM call timeout --------------------------------------------------------
# Bounds how long ONE call to the configured LLM provider (Gemini/Claude/
# OpenAI) may take, threaded into each provider's make_client() below - the
# same "a hung network call must fail fast instead of blocking forever"
# problem backends/base.py's DB_CONNECT_TIMEOUT_SECONDS solves for a stalled
# DB connect() (see that constant's docstring for the fuller threaded=True/
# blast-radius reasoning, which applies identically here: server.py handles
# one request at a time per worker, so a single hung LLM call still stalls
# every other user's request for however long it hangs, unbounded, without
# this). Deliberately one shared knob across all three providers rather than
# a per-provider *_TIMEOUT_SECONDS - the failure mode ("this provider isn't
# responding") is identical regardless of which one a session happens to be
# using, same reasoning DB_CONNECT_TIMEOUT_SECONDS already applies across
# every SQL dialect.
#
# Each SDK is handed this in whatever unit/shape IT expects (see each
# make_client() below) rather than a shared wrapper, since the three differ:
# anthropic.Anthropic/openai.OpenAI both take a plain `timeout=<seconds>`
# kwarg directly, while google-genai's genai.Client takes it in milliseconds
# via a nested HttpOptions object.
#
# A timeout is just another transient failure to the existing retry loop
# (see MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS above and
# each provider's classify_error) - no separate handling needed there.
# anthropic.APITimeoutError/openai.APITimeoutError both already subclass
# their SDK's APIConnectionError, which _classify_claude_error/
# _classify_openai_error already retry. google-genai has no equivalent typed
# exception - a timeout there surfaces as a raw httpx.TimeoutException (or
# httpx2.TimeoutException - see the import above) instead, since this app
# doesn't opt into google-genai's own separate, SDK-internal retry_options
# (which would otherwise silently multiply this timeout by however many
# attempts that's configured for); _classify_gemini_error below has a
# dedicated case for it, treated the same as a transient 5xx.
TRANSLATION_TIMEOUT_SECONDS = float(os.environ.get("TRANSLATION_TIMEOUT_SECONDS", 60))


def get_gemini_api_keys():
    """Collect Gemini API keys from GEMINI_PRESET_KEYS (comma-separated;
    a single key is just a one-item list)."""
    preset_keys_env = os.environ.get("GEMINI_PRESET_KEYS", "")
    return [k.strip() for k in preset_keys_env.split(",") if k.strip()]


def pick_gemini_api_key(exclude=None):
    """Pick a Gemini API key at random from the configured pool.

    `exclude` is an optional set of keys already tried during this
    request (e.g. one that just came back rate-limited) - those are
    avoided when a fresh alternative exists. If every configured key is
    already in `exclude`, falls back to the full pool rather than
    returning None, so a request with more retry attempts than
    configured keys still retries something instead of giving up early.
    """
    keys = get_gemini_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def get_claude_api_keys():
    """Collect Claude API keys. Supports an optional comma-separated
    CLAUDE_PRESET_KEYS env var (same pool pattern as GEMINI_PRESET_KEYS,
    for load-balancing across several paid keys); falls back to the single
    standard ANTHROPIC_API_KEY var if that's not set, which is the normal
    case for one paid account."""
    preset_keys_env = os.environ.get("CLAUDE_PRESET_KEYS", "")
    keys = [k.strip() for k in preset_keys_env.split(",") if k.strip()]
    if keys:
        return keys
    single = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return [single] if single else []


def pick_claude_api_key(exclude=None):
    """Same selection logic as pick_gemini_api_key: random choice, avoiding
    already-tried keys in `exclude` where a fresh alternative exists. With
    only one configured key (the common case) this just returns it."""
    keys = get_claude_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def get_openai_api_keys():
    """Collect OpenAI API keys. Same pool pattern as get_claude_api_keys:
    an optional comma-separated OPENAI_PRESET_KEYS env var for load-
    balancing across several paid keys, falling back to the single
    standard OPENAI_API_KEY var (the normal case for one paid account).
    Like Claude - and unlike Gemini - this pool is never rotated through
    on a rate-limit error (see _classify_openai_error's docstring); it
    exists purely so a request can start with a different key each time
    if more than one happens to be configured."""
    preset_keys_env = os.environ.get("OPENAI_PRESET_KEYS", "")
    keys = [k.strip() for k in preset_keys_env.split(",") if k.strip()]
    if keys:
        return keys
    single = os.environ.get("OPENAI_API_KEY", "").strip()
    return [single] if single else []


def pick_openai_api_key(exclude=None):
    """Same selection logic as pick_gemini_api_key/pick_claude_api_key:
    random choice, avoiding already-tried keys in `exclude` where a fresh
    alternative exists. With only one configured key (the common case)
    this just returns it."""
    keys = get_openai_api_keys()
    if not keys:
        return None
    if exclude:
        remaining = [k for k in keys if k not in exclude]
        if remaining:
            return random.choice(remaining)
    return random.choice(keys)


def _classify_claude_error(exc):
    """Decide whether/how to retry a failed Claude call. Unlike Gemini (see
    _classify_gemini_error below), Claude never rotates keys here: the
    key-pool-rotation retry is a Gemini-specific hack for an app that's
    known to configure a POOL of Gemini keys (GEMINI_PRESET_KEYS) to spread
    load/rate-limits across - Claude isn't assumed to have that, so ALL of
    its retryable failures - including rate limits and "overloaded" - just
    wait and retry with the same key, same as a transient Gemini 5xx would:
      - RateLimitError (429), a 529 "overloaded" APIStatusError, any other
        5xx APIStatusError, or a connection-level APIConnectionError: retry
        with the same key after TRANSLATION_RETRY_DELAY_SECONDS.
      - Anything else (bad request, auth failure, invalid model): not
        retried, same as Gemini.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 529:  # Claude-specific "overloaded, try again" status
            return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}
        if isinstance(code, int) and 500 <= code < 600:
            return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}
        return None

    if isinstance(exc, anthropic.APIConnectionError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


def _classify_openai_error(exc):
    """Decide whether/how to retry a failed OpenAI call. Same policy (and
    same reasoning) as _classify_claude_error above: no key-rotation retry
    here either - that stays a Gemini-only mechanism (see this module's
    docstring) - so every retryable failure just waits and retries with
    the same key:
      - RateLimitError (429), InternalServerError (any 5xx), or an
        APIConnectionError (covers APITimeoutError too, which subclasses
        it): retry with the same key after TRANSLATION_RETRY_DELAY_SECONDS.
      - Anything else (bad request, auth failure, invalid model,
        permission denied, ...): not retried, same as the other two
        providers.
    The openai package's exception hierarchy is structurally very similar
    to anthropic's (both APIStatusError-rooted, both Stainless-generated
    SDKs) - RateLimitError and InternalServerError are both APIStatusError
    subclasses already scoped to their own status code, so (unlike
    Gemini's _gemini_error_code helper) there's no need to inspect a raw
    status_code integer here at all."""
    if isinstance(exc, openai.RateLimitError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, openai.InternalServerError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    if isinstance(exc, openai.APIConnectionError):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


def _gemini_error_code(exc):
    """Best-effort extraction of the HTTP-style status code the google-genai
    SDK attaches to APIError subclasses. Different SDK versions have used
    different attribute names, so this checks a couple."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


# Retry policy, keyed by failure type. This is the single place to add
# retry behavior for a new kind of Gemini failure as it comes up - each
# classifier below just needs to return a dict describing how to retry:
#   - delay (float): seconds to sleep before the next attempt, drawn from
#     the shared TRANSLATION_RETRY_DELAY_SECONDS budget (see the comment
#     above MAX_TRANSLATION_ATTEMPTS). Only ever non-zero for a failure
#     that's NOT key-related (see rotate_key below) - waiting only makes
#     sense when the next attempt is otherwise identical to the one that
#     just failed (same key, same everything), giving whatever went wrong
#     a moment to clear. When the next attempt already differs (a
#     different key), there's nothing to wait out.
#   - rotate_key (bool): pick a different configured Gemini API key for the
#     next attempt rather than reusing the one that just failed, drawing
#     from the SEPARATE, Gemini-only key-rotation budget (one attempt per
#     configured GEMINI_PRESET_KEYS entry - see the retry loop in
#     stream_translation()). Used for capacity/rate-limit errors: the
#     failed key is (at least momentarily) out of capacity, but a
#     different configured key almost certainly isn't, so that retry fires
#     immediately (delay=0) rather than sitting idle waiting out a limit a
#     different key was never subject to. This is Gemini's ONLY - see
#     _classify_claude_error, which never sets this. Transient server-side
#     errors aren't key-related at all, so they retry with the same key
#     instead - and since nothing changed about the request, they DO wait
#     out TRANSLATION_RETRY_DELAY_SECONDS first, on the theory the same
#     problem needs a moment to pass.
# Returning None means "don't retry this - raise immediately" (e.g. bad
# request, invalid model, auth failure - these fail the same way every
# time, so retrying wastes the attempt budget).

def _classify_gemini_error(exc):
    """Decide whether/how to retry a failed Gemini call. Returns a retry
    action dict (see policy comment above) or None to raise immediately."""
    code = _gemini_error_code(exc)

    # 429 - per-key rate limit / capacity exhausted. Rotate to a
    # different configured key so the next attempt isn't just hitting
    # the same limit again - and since that next attempt uses a key that
    # was never subject to the limit that just failed, there's nothing to
    # wait out: it retries immediately (delay=0), not after
    # TRANSLATION_RETRY_DELAY_SECONDS (that delay is reserved for the 5xx
    # case below, where the same key retries against the same problem).
    # This rotate_key retry draws from its own budget - one attempt per
    # configured Gemini key - entirely independent of
    # MAX_TRANSLATION_ATTEMPTS (see stream_translation()'s retry loop).
    if code == 429:
        return {"rotate_key": True, "delay": 0}

    # 5xx - transient, server-side hiccup (e.g. the plain "500 INTERNAL"
    # Gemini occasionally throws) unrelated to which key was used, so the
    # same key is fine to retry with. Unlike the 429 case above, the next
    # attempt is otherwise identical to the one that just failed, so this
    # one DOES wait out TRANSLATION_RETRY_DELAY_SECONDS first, giving the
    # transient condition a moment to actually pass before trying the
    # exact same thing again.
    is_server_error = (isinstance(code, int) and 500 <= code < 600) or isinstance(exc, genai_errors.ServerError)
    if is_server_error:
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    # TRANSLATION_TIMEOUT_SECONDS exceeded (see that constant's docstring) -
    # google-genai has no typed timeout exception the way anthropic/openai
    # do, so this surfaces as a raw httpx.TimeoutException/httpx2.
    # TimeoutException instead, with no .code/.status_code for
    # _gemini_error_code above to find. Treated the same as the 5xx case
    # just above: same key, retry after TRANSLATION_RETRY_DELAY_SECONDS -
    # a timeout is exactly the kind of transient condition that delay is
    # meant to give a moment to clear.
    timeout_exc_types = (httpx.TimeoutException,) if httpx2 is None else (httpx.TimeoutException, httpx2.TimeoutException)
    if isinstance(exc, timeout_exc_types):
        return {"rotate_key": False, "delay": TRANSLATION_RETRY_DELAY_SECONDS}

    return None


# --- User-facing LLM error messages -----------------------------------------
# A call's own retry/rotation budget (classify_error() above, per provider)
# is about giving a TRANSIENT failure a chance to clear before giving up -
# this is the separate, orthogonal question of what to tell the USER once
# that budget IS exhausted (or the failure was never retryable to begin
# with): today, every such failure just showed the raw SDK exception text
# verbatim in an "error" field - technically accurate, but meaningless to
# someone who isn't reading this app's source (a bare "503 UNAVAILABLE..."
# or "Error code: 429 - {'error': {'code': 'insufficient_quota', ...", with
# no indication of what to actually DO about it: retry, wait, or just pick
# a different model). The three functions below turn that into an honest,
# actionable sentence PLUS the original raw text (never hidden - just no
# longer the only thing shown), classified into exactly three buckets:
#   "unavailable" - the model/service itself is down or too busy right now
#     (a 5xx, Anthropic's 529 "overloaded", a dropped connection/timeout) -
#     the honest fix is "try again in a moment."
#   "exhausted" - a capacity/quota ceiling was hit (Gemini/Claude's 429,
#     OpenAI's "insufficient_quota" 429 specifically) - retrying the SAME
#     model right now won't help; a different model is the actual fix.
#   "invalid_key" - the API key itself was rejected (a 401/403, Gemini's
#     documented 400 "API key not valid" shape, or the SDKs' typed
#     AuthenticationError/PermissionDeniedError) - see the "Bring Your Own
#     Key" feature (webClient's Preferences dialog): the fix here depends on
#     WHOSE key failed, which format_llm_error_for_user's `using_byok` param
#     (not error_category/classify - that's still purely about the
#     exception's shape) decides between at formatting time: a user's own
#     saved key gets told to fix/remove it in Preferences, while this app's
#     own configured key failing is this app's problem, not something
#     picking a different model or editing Preferences fixes.
#   "other" - anything else (bad request, invalid model, an exception type
#     this app doesn't specifically recognize) - no confident guess at why,
#     so no specific advice beyond "try a different model."
# _llm_error_category(exc) below classifies according to whichever
# provider's exception shapes it's checking - it's never called directly;
# each provider's LlmProvider.error_category() (see GeminiProvider/
# ClaudeProvider/OpenAiProvider below) dispatches to its own provider-
# specific version of it, mirroring how classify_error()/_classify_*_error
# above are already split one-per-provider.

def _gemini_error_category(exc):
    """error_category() for Gemini. Prefers the semantic `.status` string
    google-genai's APIError attaches (e.g. "RESOURCE_EXHAUSTED",
    "UNAVAILABLE" - the same google.rpc.Code names gRPC/Google APIs use
    everywhere) when present, falling back to the numeric code check
    _gemini_error_code() already uses elsewhere - needed for a raw httpx
    timeout (no .status at all) and for lightweight test doubles that only
    set a numeric .code, same as _classify_gemini_error/_gemini_error_code
    already tolerate.

    "invalid_key" is Gemini's one genuinely ambiguous case: a rejected key
    is documented (see https://firebase.google.com/docs/ai-logic/error-codes)
    to come back as a plain HTTP 400 "API key not valid. Please pass a
    valid API key." - the SAME status/code a hundred other bad-request
    reasons also use - so a bare 400 is deliberately NOT enough on its own
    (that would misclassify unrelated bad-request failures); this only
    fires for 400 when the message text itself says so. A 401/403 (or the
    matching PERMISSION_DENIED/UNAUTHENTICATED status strings) is
    unambiguous and always treated as invalid_key."""
    status = getattr(exc, "status", None)
    if status == "RESOURCE_EXHAUSTED":
        return "exhausted"
    if status == "UNAVAILABLE":
        return "unavailable"
    if status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return "invalid_key"

    code = _gemini_error_code(exc)
    if code == 429:
        return "exhausted"
    if code == 503 or (isinstance(code, int) and 500 <= code < 600):
        return "unavailable"
    if code in (401, 403):
        return "invalid_key"
    if code == 400:
        message = str(getattr(exc, "message", None) or exc).lower()
        if "api key not valid" in message or "api_key_invalid" in message:
            return "invalid_key"

    timeout_exc_types = (httpx.TimeoutException,) if httpx2 is None else (httpx.TimeoutException, httpx2.TimeoutException)
    if isinstance(exc, timeout_exc_types):
        return "unavailable"

    return "other"


def _claude_error_category(exc):
    """error_category() for Claude. RateLimitError (429) is always
    "exhausted" - Anthropic's rate limits are a request/token-budget
    ceiling, not a "servers are momentarily busy" condition. AuthenticationError
    (401 - missing/malformed/revoked key) and PermissionDeniedError (403 -
    a key that's valid but not allowed to do this) are both unambiguous
    typed exceptions, checked ahead of the generic APIStatusError branch
    below (both subclass it) - "invalid_key". A 529 "overloaded" status or
    any other 5xx (including a connection-level failure/timeout, which
    subclasses APIConnectionError) reads as "unavailable", matching
    _classify_claude_error's own retry policy for those same statuses."""
    if isinstance(exc, anthropic.RateLimitError):
        return "exhausted"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "invalid_key"
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 529 or (isinstance(code, int) and 500 <= code < 600):
            return "unavailable"
        return "other"
    if isinstance(exc, anthropic.APIConnectionError):
        return "unavailable"
    return "other"


def _openai_error_category(exc):
    """error_category() for OpenAI. Unlike Gemini/Claude, OpenAI overloads
    its one 429 RateLimitError for two very different conditions (see
    https://platform.openai.com/docs/guides/error-codes), distinguished
    only by the "code" the API attaches to the error body: "insufficient_
    quota" is a hard billing/quota ceiling (genuinely "exhausted" - won't
    clear on its own), while every other 429 (typically
    "rate_limit_exceeded") is the ordinary "too many requests right now"
    kind - transient, reads as "unavailable/busy" same as a 5xx would.
    AuthenticationError (401) and PermissionDeniedError (403) are both
    unambiguous typed exceptions - "invalid_key". An InternalServerError
    (5xx) or a connection-level failure/timeout is "unavailable", matching
    _classify_openai_error's own retry policy."""
    if isinstance(exc, openai.RateLimitError):
        code = getattr(exc, "code", None)
        return "exhausted" if code == "insufficient_quota" else "unavailable"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "invalid_key"
    if isinstance(exc, openai.InternalServerError):
        return "unavailable"
    if isinstance(exc, openai.APIConnectionError):
        return "unavailable"
    return "other"


_LLM_ERROR_UNAVAILABLE_TEMPLATE = (
    "The selected model ({model}) is currently unavailable or too busy. "
    "Please retry later or select a different model.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_EXHAUSTED_TEMPLATE = (
    "Datalect's reserved capacity for this model ({model}) has been exhausted. "
    "Please select a different model.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_OTHER_TEMPLATE = (
    "The selected model ({model}) ran into an error. Please select a different model.\n\n"
    "Actual error message received:\n"
)
# "invalid_key" has two variants, not one - see the section comment above
# _gemini_error_category for why format_llm_error_for_user needs a
# `using_byok` flag to choose between them, rather than error_category()
# itself producing two different category strings for what is, from the
# exception's own shape, exactly the same failure.
_LLM_ERROR_INVALID_KEY_BYOK_TEMPLATE = (
    "Your custom API key for this model ({model}) was rejected. Please correct or remove it in "
    "Preferences (Bring Your Own Key) - until then, this model will keep failing.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_INVALID_KEY_ENV_TEMPLATE = (
    "The API key configured for this model ({model}) was rejected. This is a problem with the "
    "app's own configuration, not something selecting a different model fixes on its own - please "
    "let the app's administrator know, or try a different model in the meantime.\n\n"
    "Actual error message received:\n"
)
_LLM_ERROR_TEMPLATES = {
    "unavailable": _LLM_ERROR_UNAVAILABLE_TEMPLATE,
    "exhausted": _LLM_ERROR_EXHAUSTED_TEMPLATE,
    "other": _LLM_ERROR_OTHER_TEMPLATE,
}


def format_llm_error_for_user(provider, model_name, exc, using_byok=False):
    """Turns a call's FINAL exception (after classify_error()'s own retry
    budget is exhausted, or immediately for a non-retryable one) into the
    honest, categorized message described in the section comment above -
    always ending with the original raw exception text, never hiding it.
    `provider` is any registered LlmProvider (its error_category() is what
    actually classifies `exc` - see GeminiProvider/ClaudeProvider/
    OpenAiProvider). An unrecognized category (there isn't one today, but
    error_category() implementations are free to extend) falls back to the
    generic "other" wording rather than raising.

    `using_byok` - whether THIS call used a user-saved Bring-Your-Own-Key
    value (see state_store.py's llm_byok_keys) rather than this app's own
    env-configured key - only changes anything when the category is
    "invalid_key": a user's own key gets told to fix/remove it in
    Preferences, while this app's own configured key failing is squarely
    this app's problem, not the user's, so it gets a different message
    entirely (see the two templates above). Every other category's wording
    is identical either way - a model being unavailable/exhausted has
    nothing to do with whose key hit that limit."""
    category = provider.error_category(exc)
    if category == "invalid_key":
        template = _LLM_ERROR_INVALID_KEY_BYOK_TEMPLATE if using_byok else _LLM_ERROR_INVALID_KEY_ENV_TEMPLATE
    else:
        template = _LLM_ERROR_TEMPLATES.get(category, _LLM_ERROR_OTHER_TEMPLATE)
    raw = str(exc) or f"{type(exc).__name__} occurred."
    return template.format(model=model_name) + raw


class LlmCallFailed(Exception):
    """Wraps an LLM provider call's final exception once format_llm_error_
    for_user() above has already turned it into the full, categorized,
    user-facing message - this wrapper's __str__ IS that message verbatim.

    Raised (replacing a bare `raise`) at every "give up" point inside a
    retry loop that sits INSIDE a wider try/except also covering unrelated
    failures (schema fetch, etc.) - see stream_translation()'s inline
    single-connection retry loop and generate_sql_for_connection() below,
    both of which report their final exception to a caller several frames
    away via a generic `except Exception as e: ... str(e)`. Without this,
    that generic catch has no way to tell "the LLM call itself failed" (do
    format the friendly message) apart from "something unrelated blew up
    nearby" (don't - str(e) should stay whatever that unrelated exception
    already says) - wrapping the message INTO the exception at the one
    point that's unambiguous means every existing `str(e)`-based caller
    gets the improved text for free, with no changes needed at the catch
    site itself.

    Deliberately NOT used by triage_all_mode_question (connection_router.py)
    or summarize_all_mode_results below - neither of those loops ever runs
    anything else ambiguous in their scope (no schema fetch, nothing else
    that could raise) between capturing the LLM exception and returning
    it, so their callers (translate_routes.py's router_only_all_mode
    branch, and the /api/summarize-results route) call
    format_llm_error_for_user() directly on the raw exception instead -
    one fewer layer of indirection where it isn't needed."""
    pass


def format_results_table_text(columns, rows, max_rows=500):
    """Render a query result set as plain text suitable for an LLM prompt."""
    cols = columns or []
    rws = rows or []
    text = f"Columns: {', '.join(cols)}\nTotal Rows: {len(rws)}\nSample/Full Data:\n"
    text += "\n".join([str(r) for r in rws[:max_rows]])
    return text


def build_gemini_history_contents(history):
    """
    Turn the client-supplied chat history into Gemini `types.Content` objects.
    Each history message is {role, text} and may optionally carry a `results`
    list - one entry per SQL statement that was executed for that turn, each
    shaped like {columns, rows, rowCount}. When present, the actual query
    results are appended to that turn's text so later turns retain context
    on what data was actually returned, not just what SQL/text was said.
    """
    contents = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = text
        hist_results = msg.get("results")
        if hist_results:
            result_blocks = []
            for i, res in enumerate(hist_results):
                cols = res.get('columns') or []
                rws = res.get('rows') or []
                row_count = res.get('rowCount', len(rws))
                shown_rows = min(len(rws), HISTORY_RESULT_MAX_ROWS)
                header = f"[Query Result {i + 1} - {row_count} row(s) total, showing {shown_rows}]"
                result_blocks.append(header + "\n" + format_results_table_text(cols, rws, max_rows=HISTORY_RESULT_MAX_ROWS))
            combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=combined_text)]
            )
        )
    return contents


def build_claude_history_messages(history):
    """Same purpose as build_gemini_history_contents above, targeting
    Claude's message shape instead: a plain list of {"role", "content"}
    dicts. Gemini's "model" role becomes Claude's "assistant"; "user" is
    unchanged. The results-appending logic is identical to the Gemini
    version - only the returned container shape differs."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = text
        hist_results = msg.get("results")
        if hist_results:
            result_blocks = []
            for i, res in enumerate(hist_results):
                cols = res.get('columns') or []
                rws = res.get('rows') or []
                row_count = res.get('rowCount', len(rws))
                shown_rows = min(len(rws), HISTORY_RESULT_MAX_ROWS)
                header = f"[Query Result {i + 1} - {row_count} row(s) total, showing {shown_rows}]"
                result_blocks.append(header + "\n" + format_results_table_text(cols, rws, max_rows=HISTORY_RESULT_MAX_ROWS))
            combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

        messages.append({
            "role": "assistant" if role == "model" else role,
            "content": combined_text,
        })
    return messages


def build_openai_history_messages(history):
    """Same purpose as build_gemini_history_contents/build_claude_history_
    messages above, targeting the OpenAI Responses API's "easy input
    message" shape instead: a plain list of {"role", "content"} dicts -
    structurally identical to Claude's, since the Responses API accepts a
    plain string for `content` (EasyInputMessageParam) rather than
    requiring Chat-Completions-style message objects. Gemini's "model" role
    becomes "assistant" (same mapping as Claude's); "user" is unchanged.
    The results-appending logic is identical to the other two providers'
    versions - only the returned container shape (a plain dict, not a
    types.Content) differs from Gemini's."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = text
        hist_results = msg.get("results")
        if hist_results:
            result_blocks = []
            for i, res in enumerate(hist_results):
                cols = res.get('columns') or []
                rws = res.get('rows') or []
                row_count = res.get('rowCount', len(rws))
                shown_rows = min(len(rws), HISTORY_RESULT_MAX_ROWS)
                header = f"[Query Result {i + 1} - {row_count} row(s) total, showing {shown_rows}]"
                result_blocks.append(header + "\n" + format_results_table_text(cols, rws, max_rows=HISTORY_RESULT_MAX_ROWS))
            combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

        messages.append({
            "role": "assistant" if role == "model" else role,
            "content": combined_text,
        })
    return messages


def _call_gemini(client, model, contents, system_instruction):
    """One Gemini generate_content call. Returns (text, usage_dict) - the
    usage_dict shape is shared with _call_claude below so the retry loop
    and the response-building code in stream_translation() don't need to
    know which provider actually ran.

    No explicit caching setup here, unlike _call_claude below: Gemini 2.5+
    models cache matching prefixes automatically ("implicit caching") with
    no opt-in call or config field required - Google's own docs are
    explicit that "there is nothing you need to do" beyond what
    stream_translation() already does structurally (putting the large,
    stable schema/history content ahead of the ever-changing new prompt).
    Cache hits are reported back via usage_metadata.cached_content_token_count,
    surfaced below the same way a real cache read is for Claude."""
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            # See the long comment on automatic_function_calling further
            # down in the original file history - unchanged from before.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    )
    text = response.text.strip() if response.text else ""
    usage = response.usage_metadata
    # `or 0` on every field below, not just a bare `getattr(..., 0)`/
    # `x if usage else 0` - a real Gemini response can carry usage_metadata
    # with a given field PRESENT but set to None rather than 0 (observed in
    # production: thoughts_token_count is None, not 0, on a call that
    # didn't use extended thinking) - `getattr(obj, name, 0)` only
    # substitutes 0 for a MISSING attribute, never a present-but-None one,
    # and every downstream consumer of this dict (usage totals summed
    # across Phase B's parallel calls, the translations-table columns,
    # the NDJSON response) does real arithmetic on these values, which
    # raises TypeError the moment one of them is None instead of an int.
    cached_content_tokens = (getattr(usage, 'cached_content_token_count', 0) or 0) if usage else 0
    return text, {
        "input_tokens": (usage.prompt_token_count or 0) if usage else 0,
        "output_tokens": (usage.candidates_token_count or 0) if usage else 0,
        "total_tokens": (usage.total_token_count or 0) if usage else 0,
        "thinking_tokens": (getattr(usage, 'thoughts_token_count', 0) or 0) if usage else 0,
        "cached_content_tokens": cached_content_tokens,
    }


def _mark_claude_cache_boundary(message):
    """Converts a plain {"role", "content": <str>} message (the shape
    build_claude_history_messages()/translate_query() build) into
    Anthropic's content-block form, with an ephemeral cache_control marker
    on that block. Claude has no automatic/implicit caching the way Gemini
    2.5+ does (see _call_gemini's docstring and this module's docstring) -
    a block only ever gets cached if explicitly marked like this. Marking
    it here means everything up to and including this message - system
    prompt, schema, and all history through this point - becomes a
    candidate cached prefix; see translate_query()'s comment on why the
    last already-accumulated history turn (not the ever-changing new
    prompt at the end) is the right message to mark."""
    message["content"] = [{
        "type": "text",
        "text": message["content"],
        "cache_control": {"type": "ephemeral"},
    }]


def _call_claude(client, model, messages, system_instruction):
    """One Claude messages.create call. Returns (text, usage_dict) in the
    same shape _call_gemini returns above.

    No `temperature` here on purpose: Claude Opus 4.7 and later (which
    includes the claude-sonnet-5 default) reject sampling parameters
    (temperature/top_p/top_k) outright rather than just ignoring them -
    Anthropic deprecated them for these newer models. This app wants
    low-variance SQL generation anyway, and these models are tuned for
    that by default without needing temperature pinned to near-0.

    The system prompt (dialect_intro + the fixed formatting rules) is sent
    as its own cache_control-marked block - it's identical on every call
    for a given dialect, so caching it benefits every session using that
    dialect, not just one conversation. Below Anthropic's per-model
    minimum cacheable size (1024 tokens for Sonnet, more for Haiku) this
    marker is simply a no-op - no error, the content just isn't written to
    the cache - so marking it unconditionally is always safe."""
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": system_instruction,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=messages,
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    usage = response.usage
    # `or 0` throughout below - same defensive reasoning as _call_gemini's
    # own usage dict above: a real usage object can report a field as
    # None rather than 0 (present attribute, null value), which every
    # downstream consumer's real arithmetic on this dict can't tolerate.
    cache_read_tokens = (getattr(usage, 'cache_read_input_tokens', 0) or 0) if usage else 0
    cache_creation_tokens = (getattr(usage, 'cache_creation_input_tokens', 0) or 0) if usage else 0
    return text, {
        "input_tokens": (usage.input_tokens or 0) if usage else 0,
        "output_tokens": (usage.output_tokens or 0) if usage else 0,
        "total_tokens": ((usage.input_tokens or 0) + (usage.output_tokens or 0)) if usage else 0,
        # This app doesn't use extended thinking on the Claude path, so
        # this is always 0 - reported anyway so record_translation() and
        # the NDJSON payload don't need a provider-specific case for it.
        "thinking_tokens": 0,
        # Tokens actually served from cache on THIS call (a cache miss/
        # write reports 0 here even though cache_creation_input_tokens is
        # nonzero, logged above) - same semantics as Gemini's
        # cached_content_token_count above, hence the shared field name.
        "cached_content_tokens": cache_read_tokens,
    }


def _call_openai(client, model, llm_input, system_instruction):
    """One OpenAI Responses API call (client.responses.create) - built on
    Responses rather than the older Chat Completions API, per this app's
    longer-term bet on it (see this module's docstring): OpenAI recommends
    Responses for new integrations, and its prompt caching applies more
    broadly than Chat Completions'. Returns (text, usage_dict) in the same
    shape _call_gemini/_call_claude return above.

    No `temperature` here, for the same reason _call_claude doesn't pass
    one: current-generation reasoning-capable models (the gpt-5.6 family
    this app defaults to) reject sampling parameters outright rather than
    silently ignoring them.

    No explicit cache markers here either, unlike _call_claude's
    cache_control blocks: like Gemini 2.5+ (see _call_gemini's docstring),
    OpenAI's prompt caching is on by default for supported models with no
    opt-in call or parameter required - `instructions` (this app's fixed,
    per-dialect system prompt) plus the stable leading portion of `input`
    this app already structures schema/history to form (see
    translate_query()'s comment on why the schema goes as far to the front
    as possible) is exactly the kind of repeated, stable prefix that gets
    reused automatically."""
    response = client.responses.create(
        model=model,
        instructions=system_instruction,
        input=llm_input,
    )
    text = (response.output_text or "").strip()
    usage = response.usage
    input_tokens_details = getattr(usage, 'input_tokens_details', None) if usage else None
    output_tokens_details = getattr(usage, 'output_tokens_details', None) if usage else None
    # `or 0` throughout below - same defensive reasoning as _call_gemini's/
    # _call_claude's own usage dicts above: a real usage object can report
    # a field as None rather than 0 (present attribute, null value), which
    # every downstream consumer's real arithmetic on this dict can't
    # tolerate.
    cached_tokens = (getattr(input_tokens_details, 'cached_tokens', 0) or 0) if input_tokens_details else 0
    reasoning_tokens = (getattr(output_tokens_details, 'reasoning_tokens', 0) or 0) if output_tokens_details else 0
    return text, {
        "input_tokens": (usage.input_tokens or 0) if usage else 0,
        "output_tokens": (usage.output_tokens or 0) if usage else 0,
        "total_tokens": (usage.total_tokens or 0) if usage else 0,
        # Reasoning-model "thinking" tokens - same field this app already
        # reports for Gemini's thoughts_token_count; always 0 for a
        # non-reasoning model/response, same as Claude's always-0 above.
        "thinking_tokens": reasoning_tokens,
        # Tokens actually served from cache on THIS call - same semantics
        # as Gemini's cached_content_token_count / Claude's
        # cache_read_input_tokens above, hence the shared field name.
        "cached_content_tokens": cached_tokens,
    }


# --- LLM provider dispatch ------------------------------------------------
#
# Each provider above (Gemini/Claude/OpenAI) has its own free functions for
# key management, error classification, history-building, and the actual
# API call - those are the pieces that genuinely differ per SDK and are
# each independently unit-testable/patchable (see this module's docstring
# on why they're left as-is rather than folded into the classes below).
# What used to differ is HOW translate_query()/stream_translation() picked
# among them: a scattered `if LLM_PROVIDER == "claude": ... else: ...` at
# every call site. LlmProvider (and one subclass per provider) replaces
# that with a single object stream_translation() calls methods on -
# equivalent in spirit to backends/base.py's Backend interface for SQL
# dialects, just for LLM providers instead.
class LlmProvider(ABC):
    """Interface every registered LLM provider (see _LLM_PROVIDERS below)
    implements. Nothing here wraps a live network call directly - each
    method delegates to that provider's own free function(s) above, so
    those keep their existing names, signatures, and test coverage
    unchanged; this class only decides WHICH free functions get called."""

    #: This provider's registered label ("google"/"anthropic"/"openai") -
    #: the _LLM_PROVIDERS key it's stored under, and the value a session's
    #: saved llm_provider field holds once chosen via the model-selection
    #: UI. Deliberately NOT the same as this class's own name or the
    #: underlying SDK/product name (GeminiProvider/ClaudeProvider still wrap
    #: the actual Gemini/Claude APIs) - this is purely the user-facing
    #: company label.
    name = None

    #: The request-body key checked before the generic "model" override -
    #: e.g. "gemini_model" - so a caller can pin a model for one provider
    #: without affecting what another provider would use. Still named after
    #: the underlying SDK (not this provider's `name` label above) since
    #: it's a wire-format detail existing callers (e.g. the mobile client)
    #: already depend on verbatim - not part of this rename.
    request_model_key = None

    #: Env var holding this provider's comma-separated list of models (e.g.
    #: "GOOGLE_MODELS") - one var doing double duty: its first entry is
    #: this provider's default_model, the full list is preset_models (both
    #: below). Subclasses set this; None here only because the base class
    #: itself is never instantiated.
    models_env_var = None

    #: Single-model list used when models_env_var is entirely unset/blank -
    #: keeps this app usable with zero model configuration. Subclasses set
    #: this to their own hardcoded default (e.g. ["gemini-3.6-flash"]).
    fallback_models = None

    #: Exact 400 response text when this provider has no API key configured.
    missing_key_error = None

    #: True only for a provider this app is known to configure a POOL of
    #: keys for (Gemini, via GEMINI_PRESET_KEYS) - gates whether a
    #: classify_error() result with rotate_key=True is even meaningful.
    #: Claude/OpenAI both leave this False; their classify_error()
    #: implementations never return rotate_key=True in the first place (see
    #: _classify_claude_error's/_classify_openai_error's docstrings), so
    #: this is really a second, defensive line of documentation rather than
    #: something stream_translation()'s retry loop strictly needs to check -
    #: but see get_key_pool_size() below for the one place it's used
    #: directly.
    supports_key_rotation = False

    @abstractmethod
    def get_api_keys(self):
        """All configured API keys for this provider, as a list (possibly
        empty)."""
        raise NotImplementedError

    @abstractmethod
    def pick_api_key(self, exclude=None):
        """One configured API key at random, avoiding `exclude` where a
        fresh alternative exists. None if nothing is configured at all."""
        raise NotImplementedError

    @abstractmethod
    def make_client(self, api_key):
        """A fresh SDK client for this provider, authenticated with
        `api_key`."""
        raise NotImplementedError

    @abstractmethod
    def build_llm_input(self, history, schema_block, new_prompt_content):
        """Turns this request's chat history plus the (already-rendered)
        schema_block/new_prompt_content strings into whatever shape this
        provider's call() expects - a list of google-genai Content objects,
        a list of Claude/OpenAI-style {"role","content"} dicts, etc. Also
        decides WHERE the schema attaches (prepended to the first
        historical turn when there is history; folded into the new prompt,
        or split into its own leading block, when there isn't) - see
        translate_query()'s own comment for why that ordering matters for
        every provider's caching."""
        raise NotImplementedError

    @abstractmethod
    def call(self, client, model, llm_input, system_instruction):
        """One provider API call. Returns (text, usage_dict) - usage_dict
        always has the same five keys (input_tokens/output_tokens/
        total_tokens/thinking_tokens/cached_content_tokens) regardless of
        provider, so the caller (stream_translation()) never needs a
        provider-specific case for building its response."""
        raise NotImplementedError

    @abstractmethod
    def classify_error(self, exc):
        """Returns a retry-action dict ({"rotate_key": bool, "delay":
        float}) for a retryable failure, or None to raise `exc` immediately.
        See _classify_gemini_error's docstring (above the first
        implementation of this) for the full policy this documents once for
        every provider."""
        raise NotImplementedError

    @abstractmethod
    def error_category(self, exc):
        """Classifies a call's FINAL exception (retry budget exhausted, or
        immediately for a non-retryable one) into "unavailable"/
        "exhausted"/"other" for format_llm_error_for_user() - see the
        section comment above _gemini_error_category (above that function's
        first implementation) for what each bucket means and why this is a
        separate question from classify_error()'s retry policy."""
        raise NotImplementedError

    def get_key_pool_size(self):
        """How many attempts the key-ROTATION retry budget gets (see
        stream_translation()'s retry loop) - the number of distinct
        configured keys for a provider that supports rotating through a
        pool, or 1 for a provider that doesn't (so that budget is
        exhausted after the single already-tried key, i.e. effectively
        unused - matching every provider except Gemini today)."""
        return len(self.get_api_keys()) if self.supports_key_rotation else 1

    @property
    def preset_models(self):
        """Every model this provider offers, in order - parsed live (not
        cached at import time, so tests that reconfigure the env var
        per-case see the change, same as get_gemini_api_keys() already
        does for GEMINI_PRESET_KEYS) from this provider's models_env_var,
        comma-separated, blank entries dropped, each trimmed of
        surrounding whitespace. Falls back to fallback_models when the env
        var is entirely unset/blank, so this is never an empty list - the
        model-selection UI always has at least one option per provider,
        and default_model (below) always has something to return."""
        raw = os.environ.get(self.models_env_var, "") if self.models_env_var else ""
        models = [m.strip() for m in raw.split(",") if m.strip()]
        return models or list(self.fallback_models)

    @property
    def default_model(self):
        """The model used when neither a request override
        (request_model_key/"model") nor a saved session choice picks one -
        always preset_models' first entry, i.e. this provider's *_MODELS
        env var's first entry, or fallback_models[0] when that env var is
        unset. One env var doing double duty (see models_env_var's
        docstring) rather than a separate *_MODEL var, so there's exactly
        one place to look to know both "what does this provider use by
        default" and "what else can it use"."""
        return self.preset_models[0]


class GeminiProvider(LlmProvider):
    # Registered as "google" (see `name`'s docstring above) - this class
    # keeps its SDK-derived name since it still wraps the actual Gemini API
    # (genai.Client, GEMINI_PRESET_KEYS, etc.) regardless of that label.
    name = "google"
    request_model_key = "gemini_model"
    models_env_var = "GOOGLE_MODELS"
    # The app's ONE hardcoded fleet-wide default (see get_llm_provider()'s
    # docstring) - a session that never picked a provider at all ends up
    # here, with this list's first entry as the model actually used.
    fallback_models = ["gemini-3.6-flash"]
    missing_key_error = "Google API key is not configured."
    supports_key_rotation = True

    def get_api_keys(self):
        return get_gemini_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_gemini_api_key(exclude=exclude)

    def make_client(self, api_key):
        # http_options.timeout is milliseconds, unlike anthropic's/openai's
        # plain-seconds `timeout` kwarg (see ClaudeProvider's/OpenAiProvider's
        # make_client() below) - see TRANSLATION_TIMEOUT_SECONDS's docstring.
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(TRANSLATION_TIMEOUT_SECONDS * 1000)),
        )

    def build_llm_input(self, history, schema_block, new_prompt_content):
        contents = build_gemini_history_contents(history)
        if contents:
            first_part = contents[0].parts[0]
            first_part.text = schema_block + first_part.text
        else:
            new_prompt_content = schema_block + new_prompt_content
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=new_prompt_content)])
        )
        return contents

    def call(self, client, model, llm_input, system_instruction):
        return _call_gemini(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_gemini_error(exc)

    def error_category(self, exc):
        return _gemini_error_category(exc)


class ClaudeProvider(LlmProvider):
    # Registered as "anthropic" (see `name`'s docstring above) - this class
    # keeps its SDK-derived name since it still wraps the actual Claude API
    # (anthropic.Anthropic, ANTHROPIC_API_KEY, etc.) regardless of that label.
    name = "anthropic"
    request_model_key = "claude_model"
    models_env_var = "ANTHROPIC_MODELS"
    fallback_models = ["claude-sonnet-5"]
    missing_key_error = "Anthropic API key is not configured."
    supports_key_rotation = False

    def get_api_keys(self):
        return get_claude_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_claude_api_key(exclude=exclude)

    def make_client(self, api_key):
        # See TRANSLATION_TIMEOUT_SECONDS's docstring - this SDK takes a
        # plain seconds value directly, unlike GeminiProvider's milliseconds.
        return anthropic.Anthropic(api_key=api_key, timeout=TRANSLATION_TIMEOUT_SECONDS)

    def build_llm_input(self, history, schema_block, new_prompt_content):
        messages = build_claude_history_messages(history)
        if messages:
            messages[0]["content"] = schema_block + messages[0]["content"]
            # Marks the end of the accumulated (stable) prefix - see
            # _mark_claude_cache_boundary's docstring and this module's
            # (formerly translate_query()'s) comment on why the last
            # already-accumulated history turn, not the ever-changing new
            # prompt, is the right message to mark.
            _mark_claude_cache_boundary(messages[-1])
            messages.append({"role": "user", "content": new_prompt_content})
        elif schema_block:
            # A conversation's very first call - split into two content
            # blocks on one message so the schema half can still be
            # cache_control-marked independently of the ever-different new
            # prompt right after it (see _mark_claude_cache_boundary's
            # docstring for why concatenating the two into one marked
            # string would be wrong).
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": schema_block,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": new_prompt_content},
                ],
            })
        else:
            # No history AND no schema block at all - e.g.
            # triage_all_mode_question's first-ever call in a brand-new
            # conversation lands here, before it has any history to thread
            # through (see its own docstring - once a conversation has
            # turns, its calls take the branch above instead, same as any
            # other history-bearing call). Anthropic rejects cache_control
            # on an empty text block outright ("cache_control cannot be
            # set for empty text blocks"), so this must NOT fall into the
            # branch above with an empty `schema_block` - that would make
            # this call always fail with a 400 on a fresh conversation,
            # which the caller has no way to distinguish from a genuine
            # transient error: it just retries once, fails identically,
            # and silently falls back to its own "couldn't route"
            # behavior every single time, regardless of the question. There's
            # nothing worth cache-marking here anyway (a single plain-text
            # prompt with no stable prefix to reuse), so this is just the
            # one message, unmarked.
            messages.append({"role": "user", "content": new_prompt_content})
        return messages

    def call(self, client, model, llm_input, system_instruction):
        return _call_claude(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_claude_error(exc)

    def error_category(self, exc):
        return _claude_error_category(exc)


class OpenAiProvider(LlmProvider):
    name = "openai"
    request_model_key = "openai_model"
    models_env_var = "OPENAI_MODELS"
    fallback_models = ["gpt-5.6-luna"]
    missing_key_error = "OpenAI API key is not configured."
    supports_key_rotation = False

    def get_api_keys(self):
        return get_openai_api_keys()

    def pick_api_key(self, exclude=None):
        return pick_openai_api_key(exclude=exclude)

    def make_client(self, api_key):
        # See TRANSLATION_TIMEOUT_SECONDS's docstring - same plain-seconds
        # kwarg as ClaudeProvider's make_client() above.
        return openai.OpenAI(api_key=api_key, timeout=TRANSLATION_TIMEOUT_SECONDS)

    def build_llm_input(self, history, schema_block, new_prompt_content):
        # Structurally identical to GeminiProvider's version above (prepend
        # the schema to the first historical turn when there's history,
        # otherwise fold it into the new prompt), not Claude's - OpenAI's
        # prompt caching is automatic like Gemini's, so there's no
        # cache_control-style marker to place (see _call_openai's
        # docstring); only the container shape (plain {"role","content"}
        # dicts, from build_openai_history_messages) differs from Gemini's
        # types.Content objects.
        messages = build_openai_history_messages(history)
        if messages:
            messages[0]["content"] = schema_block + messages[0]["content"]
        else:
            new_prompt_content = schema_block + new_prompt_content
        messages.append({"role": "user", "content": new_prompt_content})
        return messages

    def call(self, client, model, llm_input, system_instruction):
        return _call_openai(client, model, llm_input, system_instruction)

    def classify_error(self, exc):
        return _classify_openai_error(exc)

    def error_category(self, exc):
        return _openai_error_category(exc)


_LLM_PROVIDERS = {
    "google": GeminiProvider(),
    "anthropic": ClaudeProvider(),
    "openai": OpenAiProvider(),
}


def get_llm_provider(name):
    """Returns the LlmProvider for `name` (a session's saved llm_provider
    value - "google"/"anthropic"/"openai"). Unlike backends/__init__.py's
    get_backend() - which raises on an unrecognized "type" - an
    unrecognized/blank provider name here falls back to Google rather than
    erroring: this is this app's ONE hardcoded fleet-wide default (see this
    module's docstring on why there's no separate LLM_PROVIDER-style env
    var for it anymore), and the same graceful fallback also covers a
    session whose saved value predates this app's provider labels being
    renamed from "gemini"/"claude" to "google"/"anthropic" - such a session
    just silently reverts to the default instead of erroring."""
    return _LLM_PROVIDERS.get(name, _LLM_PROVIDERS["google"])


def list_llm_providers_info():
    """Every registered provider's {"name", "preset_models",
    "default_model"} - the shape config_routes.py's GET /api/config needs
    to build the model-selection modal's radio list, organized by
    provider. Order follows _LLM_PROVIDERS' own definition order (google,
    anthropic, openai) so the modal's provider sections render in a stable,
    predictable order across requests."""
    return [
        {"name": p.name, "preset_models": p.preset_models, "default_model": p.default_model}
        for p in _LLM_PROVIDERS.values()
    ]


_NO_SQL_PREFIX_RE = re.compile(r'^\*\*\*\s*NO\s*SQL\s*\*\*\*\s*', re.IGNORECASE)


def _strip_no_sql_prefix(text):
    """Strips the '*** NO SQL ***' sentinel (see _COMMON_FORMAT_RULES)
    from the front of `text`, tolerating the same loose whitespace/casing
    client.js's own copy of this regex already tolerates. Returns the
    stripped, trimmed remainder (possibly empty)."""
    return _NO_SQL_PREFIX_RE.sub("", text or "").strip()


# Fixed apology text for when "all databases" mode's triage call fails
# outright (see triage_all_mode_question's "failed" outcome) - identical
# to _COMMON_FORMAT_RULES' own "I cannot respond at all with reasonable
# confidence" convention, reused verbatim rather than inventing new
# copy, since the user-facing meaning is the same: the app has nothing
# useful to say about this prompt. Reserved specifically for the
# "api_error": False case - the model actually responded (twice), but
# with something unparseable both times. The OTHER "failed" case
# (api_error=True: the LLM call itself raised, and its own retry budget -
# key rotation and/or transient-error retries - was fully used up without
# ever getting a response at all) used to show a second fixed apology
# text here (identical regardless of which model or what actually went
# wrong); it's now built per-call by format_llm_error_for_user() instead
# (see that function's own section comment, and its call site in
# stream_translation()'s router branch below) - honest about WHY it
# failed, and including the real error, rather than one more generic
# "try again in a moment."
_TRIAGE_FAILURE_TEXT = "*** NO SQL *** I am not able to respond to your prompt."


def generate_sql_for_connection(descriptor, prompt, history, provider, client, model,
                                 user_identity, force_schema_refresh=False,
                                 api_key=None, tried_keys=None, using_byok=False):
    """Generates SQL for exactly ONE connection - behaviorally identical to
    what happens today when a user has that one connection selected and
    submits `prompt`: fetches its full (TTL-cached) schema via
    get_database_schema(), resolves its dialect intro, appends
    _COMMON_FORMAT_RULES, builds llm_input via provider.build_llm_input(),
    and runs the same transient-error/key-rotation retry loop
    stream_translation()'s single-connection path has always run
    (MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS/
    provider.classify_error()/provider.get_key_pool_size()) before calling
    provider.call(). This is a standalone module-level function (not a
    refactor of stream_translation()'s inline code, which keeps its own
    copy of this same logic for the single-connection path, separately
    tested - see this module's docstring on the backward-compatibility
    guarantee) so it can be safely reused by _run_phase_b_fanout below
    without touching that existing, already-tested code path at all.

    Generator: yields fully wire-encoded NDJSON progress lines
    (`json.dumps({"status": "retrying", ...}) + "\\n"`), identical in
    shape to what stream_translation() has always emitted inline, so a
    caller that wants to forward live progress can do
    `... = yield from generate_sql_for_connection(...)`. A caller that
    doesn't care about live progress (Phase B's parallel fan-out, which
    runs in a worker thread with no NDJSON stream of its own to forward
    into) drains this generator via _drain_generation() below instead,
    discarding every yielded line.

    `history` and `api_key`/`tried_keys` are explicit parameters (not
    closed-over/`nonlocal`, unlike stream_translation()'s inline retry
    loop) specifically so a ThreadPoolExecutor worker can drive its own,
    independent key-rotation budget - N threads racing on one shared
    mutable `tried_keys` set would corrupt it, so Phase B always calls
    this with a fresh, independently-picked key and a fresh {api_key} set
    (see _run_phase_b_fanout).

    `using_byok=True` means `api_key` is a user's own "Bring Your Own Key"
    (see state_store.py's get_llm_byok_key) rather than one of this app's
    own env-configured keys: the retry loop's key-rotation budget is
    forced down to exactly 1 (there is no second key to rotate to, and
    silently falling back to an env key would defeat the whole point of
    the user supplying their own), and format_llm_error_for_user() is
    told so it can word an "invalid key" failure as "fix your key in
    Preferences" rather than "this app's admin needs to fix this".

    Returns (via `return`, capturable by `yield from` or
    _drain_generation): (generated_sql, usage_info, duration_ms,
    final_api_key, final_client) - generated_sql already has markdown
    code-fences stripped. On total failure, raises the final classified-
    as-fatal (or retry-budget-exhausted) exception wrapped as
    LlmCallFailed (see its own docstring) - str() on it is already the
    full, categorized, user-facing message format_llm_error_for_user()
    builds, so a caller that just does str(exc) (e.g.
    _run_phase_b_fanout's per-connection failure handling) gets that text
    with no further changes. Never swallows anything; the caller decides
    how to handle it."""
    schema = get_database_schema(descriptor, user_identity, force_refresh=force_schema_refresh)

    try:
        dialect_name = get_backend(descriptor).dialect_name
    except Exception:
        dialect_name = "PostgreSQL"
    dialect_intro = _DIALECT_PROMPT_INTROS.get(dialect_name, _DEFAULT_DIALECT_PROMPT_INTRO)

    system_instruction = dialect_intro + _COMMON_FORMAT_RULES
    schema_block = f"Database Schema:\n{schema}\n\n"
    new_prompt_content = f"User Request: {prompt}\n\nSQL Query:"
    llm_input = provider.build_llm_input(history, schema_block, new_prompt_content)

    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    start_time = time.perf_counter()
    generated_sql = ""
    usage_info = {}
    transient_attempt = 1
    while True:
        try:
            generated_sql, usage_info = provider.call(client, model, llm_input, system_instruction)
            break
        except Exception as e:
            retry_action = provider.classify_error(e)
            if retry_action is None:
                raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e

            if retry_action["rotate_key"]:
                if len(tried_keys) >= key_pool_size:
                    raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e
                next_key = provider.pick_api_key(exclude=tried_keys)
                if next_key != api_key:
                    api_key = next_key
                    client = provider.make_client(api_key)
                tried_keys.add(api_key)
                logger.warning(
                    "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                    provider.name, len(tried_keys), key_pool_size, e
                )
                yield json.dumps({
                    "status": "retrying",
                    "attempt": len(tried_keys),
                    "maxAttempts": key_pool_size,
                    "delaySeconds": 0,
                    "rotatedKey": True,
                }) + "\n"
                continue

            if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                raise LlmCallFailed(format_llm_error_for_user(provider, model, e, using_byok=using_byok)) from e
            logger.warning(
                "%s call failed (attempt %d/%d), retrying in %ds: %s",
                provider.name, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e
            )
            yield json.dumps({
                "status": "retrying",
                "attempt": transient_attempt + 1,
                "maxAttempts": MAX_TRANSLATION_ATTEMPTS,
                "delaySeconds": retry_action["delay"],
                "rotatedKey": False,
            }) + "\n"
            transient_attempt += 1
            if retry_action["delay"]:
                time.sleep(retry_action["delay"])
            continue
    end_time = time.perf_counter()

    if generated_sql.startswith("```"):
        lines = generated_sql.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        generated_sql = "\n".join(lines).strip()

    return generated_sql, usage_info, round(1000 * (end_time - start_time)), api_key, client


def _drain_generation(gen):
    """Runs a generate_sql_for_connection() generator to completion from a
    plain (non-generator) context - a ThreadPoolExecutor worker has no
    `yield from` of its own to capture the return value with. Discards
    every yielded progress line (no live per-attempt retry UI for Phase
    B's parallel fan-out - see this module's docstring on why batching the
    whole fan-out into one final response is the deliberate, simpler
    choice here). Re-raises whatever the generator itself raised,
    unchanged."""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _classify_generation_outcome(entry, outcome):
    """Classifies one connection's raw Phase B outcome - either
    ("ok", generated_sql, usage_info) or ("failed", error_str), the exact
    tuple shapes _run_phase_b_fanout's ThreadPoolExecutor loop already
    produces - into the one shape both that function's per-completion
    streaming event AND its final original-order summary loop need, so
    the marker-prepend/note-strip logic is written exactly once instead
    of twice. Returns one of:
      {"outcome": "sql", "sql": <marker-prepended text>}
      {"outcome": "note", "text": <str, '*** NO SQL ***' prefix stripped -
        "" for the rare case where the model returned a blank response;
        _run_phase_b_fanout's own final loop intentionally still drops
        that case from `database_notes`, exactly like this function's
        pre-extraction inline code always has - but the streaming event
        still needs SOME event for it, so it's surfaced here as an empty
        note rather than silently vanishing from the stream too>
      {"outcome": "failed", "error": <str>}
    Never raises - a raised generation call is already represented as
    outcome[0] == "failed" by the caller before this is invoked.
    """
    if outcome[0] == "failed":
        return {"outcome": "failed", "error": outcome[1]}
    _, generated_sql, _usage_info = outcome
    stripped = (generated_sql or "").strip()
    if not stripped:
        return {"outcome": "note", "text": ""}
    if _NO_SQL_PREFIX_RE.match(stripped):
        return {"outcome": "note", "text": _strip_no_sql_prefix(stripped)}
    marked = f"-- database: {entry['kind']}:{entry['id']} ({entry['name']})\n{stripped}"
    return {"outcome": "sql", "sql": marked}


def _run_phase_b_fanout(selected_entries, prompts, provider, model, user_identity, force_schema_refresh):
    """"All databases" mode's Phase B: runs generate_sql_for_connection()
    once per entry in `selected_entries`, in PARALLEL via a
    ThreadPoolExecutor (same pre-allocate-results-array +
    future_to_index + as_completed pattern as db.py's
    build_router_candidate_summaries, one worker per connection - no
    artificial cap, since this is already bounded by
    MAX_IN_SCOPE_CONNECTIONS upstream in triage_all_mode_question).

    This is a GENERATOR: as each connection's call completes (in
    COMPLETION order, not original order - this is what lets
    stream_translation()'s router_only_all_mode branch report each
    database's own result to the client as soon as it's ready, rather
    than waiting for the slowest one), it yields `(entry, classified)`,
    where `classified` is _classify_generation_outcome's return shape for
    that connection. A caller uninterested in the streaming events (e.g.
    a test only checking the final aggregate) should drain it with the
    same `_drain_generation`-style idiom already used elsewhere in this
    module. Once every connection has completed, this function `return`s
    (captured via `StopIteration.value`, same idiom) the exact same four
    values it has always returned - see below - rebuilt in
    `selected_entries`' ORIGINAL order (not completion order).

    `prompts` is a list the same length/order as `selected_entries` - each
    connection's OWN instruction, not necessarily the user's original
    question verbatim. The caller (stream_translation()'s router_only_all_
    mode branch) is responsible for resolving each entry to either the
    triage call's per-connection rewrite (triage_all_mode_question's
    "database_prompts" - see that function's docstring for why the
    original, possibly cross-database-phrased question can't just be
    reused unchanged here) or the original question itself as a fallback
    when no rewrite was supplied for that connection - this function
    itself stays oblivious to where each prompt came from, it just sends
    prompts[i] to selected_entries[i].

    Each call is fully independent: its OWN freshly-picked api_key AND a
    client built from that exact key (never a shared tried-keys set - N
    threads racing on one shared mutable set would corrupt it - see
    generate_sql_for_connection's docstring), EMPTY history (per-database
    chat history is explicitly deferred to later work), and that
    connection's own full schema/dialect intro - i.e. exactly as if the
    user had selected just that one connection and submitted its own
    `prompts[i]` directly. There is deliberately no shared `client`
    parameter here (unlike triage_all_mode_question/
    summarize_all_mode_results, which reuse the caller's already-picked
    key/client as their starting point) - every worker's key is picked
    independently at fan-out time, so a single client handed in from
    outside would almost always belong to a DIFFERENT key than at least
    some workers end up using (see _run_one's own comment for the bug this
    fixes). One connection's call failing (its own retry budget exhausted,
    or a non-retryable error) does NOT prevent the others from completing -
    matches the same tolerant, per-item failure isolation already used
    both by db.py's schema-summary fan-out and by execute_routes.py's
    per-connection execution.

    Returns (sql_blocks, database_notes, generation_failures, usage_totals):
      sql_blocks: [(entry, marked_sql_text), ...] - one per entry that
        returned REAL SQL, marker-prepended here (mechanically, by this
        function - never by the model, which only ever sees ONE
        connection so it has nothing to mislabel) with the exact stable
        format execute_routes.py already parses: '-- database:
        preset:<id> (<name>)' / '-- database: custom:<key> (<name>)'.
        In `selected_entries`' ORIGINAL (most-relevant-first) order, not
        completion order.
      database_notes: [{"kind","id","name","text"}, ...] - one per entry
        whose call returned a '*** NO SQL ***' reply instead of real SQL
        (prefix stripped), same original order.
      generation_failures: [{"kind","id","name","error"}, ...] - one per
        entry whose call raised, same original order.
      usage_totals: the five usage_info keys, summed across every call
        that actually produced a billable response (a failed call
        contributes nothing).

    Resolves `user_identity`'s "Bring Your Own Key" value for `provider`
    (state_store.get_llm_byok_key) exactly ONCE here, up front - not
    per-worker - since it's the same user/provider for every entry in
    this fan-out. When set, every worker uses that key instead of picking
    its own from the env-configured pool, and generate_sql_for_connection
    is told using_byok=True so its own retry loop won't try to rotate to
    a different (env-configured) key on an auth failure.
    """
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)

    def _run_one(entry, entry_prompt):
        # BUG FIXED HERE: this used to pick a fresh `worker_api_key` but
        # then pass it alongside the OUTER, closed-over `client` - which
        # was built (once, at the top of stream_translation()) for
        # whatever key triage happened to be using, not this worker's own.
        # provider.pick_api_key() (Gemini's own impl - see
        # pick_gemini_api_key) picks RANDOMLY from the configured pool, so
        # with 2+ keys configured, `worker_api_key` frequently differed
        # from the key `client` was actually authenticated with. The
        # request that hit the wire used `client`'s real key the whole
        # time, but generate_sql_for_connection's retry loop believed it
        # was using `worker_api_key` - so on a 429, it excluded the WRONG
        # key from rotation (one that was never actually tried) and could
        # rotate straight back onto the real, already-exhausted key, or
        # give up as "budget exhausted" while a perfectly good configured
        # key had never been attempted at all. Building the client here,
        # from the SAME worker_api_key passed below, keeps the two
        # permanently in sync - exactly what every other rotating call in
        # this app (generate_sql_for_connection's own internal rotation,
        # triage_all_mode_question, summarize_all_mode_results) already
        # does whenever it picks a new key.
        # BYOK short-circuits the "pick a fresh key per worker" scheme
        # above entirely - there's only one key, so every worker uses it
        # (and, per generate_sql_for_connection's using_byok docstring,
        # its retry loop won't try to rotate away from it on failure).
        worker_api_key = byok_key or provider.pick_api_key()
        worker_client = provider.make_client(worker_api_key)
        gen = generate_sql_for_connection(
            entry["descriptor"], entry_prompt, [], provider, worker_client, model, user_identity,
            force_schema_refresh=force_schema_refresh,
            api_key=worker_api_key, tried_keys={worker_api_key}, using_byok=bool(byok_key),
        )
        return _drain_generation(gen)  # (generated_sql, usage_info, duration_ms, _key, _client)

    outcomes = [None] * len(selected_entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected_entries)) as pool:
        future_to_index = {
            pool.submit(_run_one, entry, prompts[i]): i for i, entry in enumerate(selected_entries)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            entry = selected_entries[index]
            try:
                generated_sql, usage_info, _duration, _key, _client = future.result()
                outcomes[index] = ("ok", generated_sql, usage_info)
            except Exception as e:
                logger.warning(
                    "Phase B generation failed for %s:%s: %s",
                    entry["kind"], entry["id"], e,
                )
                outcomes[index] = ("failed", str(e))
            # Yielded in COMPLETION order (whatever order this loop
            # actually reaches each future in) - NOT `index` order. The
            # final, order-stable return value below is rebuilt from
            # `outcomes` in ORIGINAL order regardless of what order this
            # loop yielded in, so callers that only care about the
            # aggregate (draining this generator to completion) see
            # exactly the same result they always have.
            yield entry, _classify_generation_outcome(entry, outcomes[index])

    sql_blocks, database_notes, generation_failures = [], [], []
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                     "thinking_tokens": 0, "cached_content_tokens": 0}
    for entry, outcome in zip(selected_entries, outcomes):
        if outcome[0] == "failed":
            generation_failures.append({
                "kind": entry["kind"], "id": entry["id"], "name": entry["name"], "error": outcome[1],
            })
            continue
        _, generated_sql, usage_info = outcome
        for k in usage_totals:
            # `or 0` guards against a provider returning this key present
            # but explicitly None (e.g. real Gemini responses report
            # thoughts_token_count as None, not 0, whenever a call didn't
            # use extended thinking) - `.get(k, 0)` alone only substitutes
            # 0 for a MISSING key, not a present-but-None one, and `+=`
            # against None raises TypeError. See _call_gemini/_call_claude/
            # _call_openai above, which are the real fix (never return
            # None in the first place) - this is a defensive backstop.
            usage_totals[k] += (usage_info or {}).get(k) or 0
        classified = _classify_generation_outcome(entry, outcome)
        if classified["outcome"] == "sql":
            sql_blocks.append((entry, classified["sql"]))
        elif classified["outcome"] == "note" and classified["text"]:
            # The empty-text case (generated_sql was blank after
            # stripping) is intentionally still dropped here - same
            # behavior this function's inline code always had before the
            # _classify_generation_outcome extraction - only the
            # per-completion streaming event above surfaces it at all (as
            # an empty note), so a client-side placeholder tab still has
            # something to settle into.
            database_notes.append({
                "kind": entry["kind"], "id": entry["id"], "name": entry["name"],
                "text": classified["text"],
            })
    return sql_blocks, database_notes, generation_failures, usage_totals


# --- "All databases" mode, Phase C: post-execution results summarization ---
#
# Phase A (triage) picks who to ask; Phase B (_run_phase_b_fanout above)
# asks each of them independently and generates real SQL; the CLIENT then
# executes that SQL via /api/execute, entirely outside this module (see
# execute_routes.py) - this route only ever runs AFTER that has already
# happened, once the client has real, actual results (or notes/failures)
# in hand for every database Phase B was routed to. It exists because
# triage's own "routing_message" (shown at the top of the Summary tab,
# see client.js's renderAllModeCombinedResults) is necessarily written
# BEFORE any real data was fetched - it can say "Checking Sales Postgres
# and Marketing Postgres for your question" but has no way to say what
# was actually found. This closes that gap: one more LLM call, now that
# the real numbers are in, writing a brief per-database answer (see
# _SUMMARY_SYSTEM_INSTRUCTION below for the exact paragraph-per-database
# shape) - appended underneath the routing message in that same Summary
# tab (see appendPhaseCSummaryToSummaryTab in client.js).
#
# Deliberately a SEPARATE endpoint the client calls once /api/execute
# finishes, rather than folded into /api/execute itself: execute_routes.py
# is a generic, dialect-agnostic SQL runner with no LLM provider/session
# infrastructure of its own (no provider resolution, no API key rotation,
# no translations-table logging), and reused byte-for-byte by the
# single-connection path too - adding this module's entire LLM-calling
# machinery there just for this one new, "all databases"-mode-only step
# would mean duplicating (or awkwardly importing) everything already
# established here. This route is Best-
# effort/additive on top of a turn that has already fully succeeded by
# the time it's ever called - any failure here (missing API key, the LLM
# call itself exhausting its own retry) is reported back as
# {"success": false}, never a hard error, so the client simply leaves the
# Summary tab exactly as it already is rather than showing an error for
# what's genuinely just a nice-to-have layered on top.

_SUMMARY_SYSTEM_INSTRUCTION = (
    "You previously helped route a user's natural-language question to one or more databases, and real "
    "queries have now been run against each of them. You will be given the user's ORIGINAL question and, "
    "for each database that was queried, exactly one of: its actual result rows, a note that it had "
    "nothing relevant to contribute, or an error explaining that querying it failed.\n"
    "CRITICAL, before anything else: your ENTIRE response - the label line below AND every paragraph that "
    "follows it - MUST be written in the SAME LANGUAGE as the user's original question, never the language "
    "of the database/table names or of the results data you're given, and never any other language. This "
    "applies to every single sentence you write, not just the label.\n"
    "Your response has two parts. FIRST, a single label line: a short (one to two word) section-heading "
    "label meaning \"Results Summary\" - in English this label is literally the phrase \"Results Summary\", "
    "but you must instead write it TRANSLATED into the SAME LANGUAGE as the user's original question, with "
    "nothing else on that line, followed by a blank line. SECOND, immediately after that blank line, your "
    "real, substantive answer - the per-database paragraphs described below, ALSO written in that same "
    "language. Example of the full shape, if the question was in English: \"Results Summary\\n\\n**Sales "
    "Postgres:** ...\". Never stop after the label - the label by itself, with no paragraphs following it, "
    "is not a valid response; the label is a UI section heading prepended to your answer, not a substitute "
    "for writing one. The label itself is plain text with no markdown emphasis of your own around it.\n"
    "Write ONE separate short paragraph PER DATABASE, answering the original question using just that "
    "database's own results. Start each paragraph with the database's real name in bold, exactly as given "
    "below - never an index or a label like \"Database 1\" - followed by a colon, e.g. \"**Sales "
    "Postgres:** ...\". Separate paragraphs with a single blank line. Keep every paragraph brief - one or "
    "two sentences - even if the underlying result set is large: this is a summary, not a report. If a "
    "database noted it had nothing relevant or failed, say so in one short sentence rather than skipping "
    "it silently, so the user can see every database was actually considered. Only if the question "
    "genuinely asks for a single figure or conclusion combined across databases (e.g. a grand total), add "
    "ONE final short paragraph with that combined answer after the per-database ones - otherwise leave it "
    "out entirely; do not restate or recap the per-database paragraphs a second time.\n"
    "Respond with plain text only - no SQL, no markdown tables, no code fences, no bullet points, no "
    "other headings. The leading translated label line and the bold database-name lead-in above are the "
    "only formatting to use.\n"
    "One final reminder, since it's the single most important rule above: the language of your response "
    "must match the user's original question, not the language of the schema/data.\n"
)


def _build_summary_prompt(user_question, database_results):
    """Renders `database_results` - client-submitted
    [{"name", "columns", "rows", "rowCount"} | {"name", "note"} |
    {"name", "error"}, ...], one entry per statement result/note/failure
    Phase B + the client's own /api/execute call produced for a "route"
    outcome turn - into one labeled text block per entry for Phase C's
    summarization call above.

    Real result rows reuse format_results_table_text/HISTORY_RESULT_
    MAX_ROWS - the exact same cap already applied when a PAST turn's
    results are fed back into a prompt as chat history (see
    build_gemini_history_contents's docstring): an oversized result set
    blowing the prompt's token budget is exactly the same risk here,
    for exactly the same reason."""
    blocks = []
    for entry in (database_results or []):
        name = entry.get("name") or "Unknown database"
        error = entry.get("error")
        note = entry.get("note")
        if error:
            blocks.append(f"{name}: query failed - {error}")
        elif note:
            blocks.append(f"{name}: {note}")
        else:
            cols = entry.get("columns") or []
            rows = entry.get("rows") or []
            row_count = entry.get("rowCount", len(rows))
            shown_rows = min(len(rows), HISTORY_RESULT_MAX_ROWS)
            header = f"{name} - {row_count} row(s) total, showing {shown_rows}:"
            blocks.append(header + "\n" + format_results_table_text(cols, rows, max_rows=HISTORY_RESULT_MAX_ROWS))
    results_text = "\n\n".join(blocks) if blocks else "(no databases returned anything)"
    # The trailing reminder repeats _SUMMARY_SYSTEM_INSTRUCTION's own
    # language-matching rule right here, at the very end of the actual
    # user-turn content rather than only up in the system instruction -
    # some models (observed concretely with gpt-5.3-codex on this exact
    # call) weight an instruction placed immediately before generation more
    # heavily than one stated earlier in a long system prompt, so this is
    # deliberate reinforcement/redundancy, not a duplicate to clean up.
    return (
        f"Original question: {user_question}\n\n"
        f"Results gathered from each database queried to help answer it:\n\n{results_text}\n\n"
        "Reminder: write your response - the label line AND every paragraph - in the SAME "
        "LANGUAGE as the \"Original question\" above, no matter what language the database/table "
        "names or the results data shown above happen to be in."
    )


# is_label_only_response (imported from connection_router.py, shared with
# triage_all_mode_question there) detects a response that's just the
# leading label (see _SUMMARY_SYSTEM_INSTRUCTION) with no real paragraphs
# after it, so it can be retried exactly like a genuinely empty response,
# instead of silently showing the user a bare heading with nothing usable
# underneath it - see its own docstring for why this is POSITION-based
# rather than matching a specific word: the label is now translated into
# the user's own question's language, so it can no longer be matched
# against a fixed English string like "Result Summary"/"Results Summary".


def summarize_all_mode_results(user_question, database_results, provider, client, model,
                                api_key=None, tried_keys=None, using_byok=False):
    """"All databases" mode's Phase C - see the section comment above for
    the fuller picture of when/why this runs. A brief, plain-text answer
    to `user_question` - one short paragraph per database, over the
    ACTUAL data gathered from every database Phase B was routed to,
    rather than the routing message triage produced before any of it was
    known (see _SUMMARY_SYSTEM_INSTRUCTION for the exact shape asked
    for).

    Bounded 2-attempt retry at getting usable CONTENT back (a response
    that comes back empty, or as JUST the label with no real paragraphs
    after it - see is_label_only_response - counts as a failed attempt,
    same as connection_router.py's triage_all_mode_question treats an
    unparseable response). Nested
    inside each of those 2 attempts is the SAME transient-error/key-
    rotation retry loop generate_sql_for_connection/triage_all_mode_
    question already run (provider.classify_error()/
    MAX_TRANSLATION_ATTEMPTS/TRANSLATION_RETRY_DELAY_SECONDS/
    provider.get_key_pool_size()) - this call used to be the one LLM call
    in the whole "all databases" pipeline that did NOT get that treatment:
    a real capacity/rate-limit error on the configured key would exhaust
    a bare 2-attempt loop with no rotation and no wait, in well under a
    second, silently leaving the Summary tab as if Phase C had simply
    never run - easy to mistake for a rendering bug (which is exactly what
    this looked like from the client side) rather than the resource-
    exhaustion condition it actually was. `api_key`/`tried_keys` mirror
    those two functions' own parameters of the same name for the same
    reason: the caller's own already-picked key is the natural starting
    point, and an explicit (not closed-over) `tried_keys` set is safe to
    thread through a fresh call each time this fires.

    On total failure (LLM call retry/rotation budget exhausted, or 2
    consecutive content-invalid responses) returns (None, None, error) -
    `error` is the raw exception the LLM call finally failed with when
    that's what happened (guaranteed to be an actual exception instance in
    that case - the same reasoning as triage_all_mode_question's own
    "error" key: the only way to reach `text is None` below is via the
    except block that just set `last_error = e`, and nothing after that
    ever reassigns it to something else before this returns), or a plain
    descriptive string when it was instead 2 consecutive content-invalid
    responses (nothing genuinely went wrong at the API level, so there's
    no exception to report - just isinstance-check `error` to tell the two
    apart). The caller (the /api/summarize-results route below) uses
    format_llm_error_for_user() to build an honest message from `error`
    when it's a real exception, surfacing WHY Phase C didn't produce a
    summary rather than just leaving the Summary tab silently as it
    already was - it's the caller, not this function, that needs
    `using_byok` for that (see generate_sql_for_connection's docstring
    for the general reasoning); `using_byok` is accepted here only to
    force the key-rotation budget down to 1 attempt, same as there.

    Returns (text, usage, None) on success - `text` is the model's own
    plain-text answer, NOT YET prefixed with the app's "*** NO SQL ***"
    convention (the caller adds that, exactly like triage's "answer"
    outcome does, so the prefix logic lives in exactly one place)."""
    llm_input = provider.build_llm_input([], "", _build_summary_prompt(user_question, database_results))

    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    last_error = None
    for attempt in range(2):
        text = None
        transient_attempt = 1
        while True:
            try:
                text, usage = provider.call(client, model, llm_input, _SUMMARY_SYSTEM_INSTRUCTION)
                break
            except Exception as e:
                last_error = e
                retry_action = provider.classify_error(e)
                if retry_action is None:
                    text = None
                    break

                if retry_action["rotate_key"]:
                    if len(tried_keys) >= key_pool_size:
                        text = None
                        break
                    next_key = provider.pick_api_key(exclude=tried_keys)
                    if next_key != api_key:
                        api_key = next_key
                        client = provider.make_client(api_key)
                    tried_keys.add(api_key)
                    logger.warning(
                        "Phase C summarization call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        len(tried_keys), key_pool_size, e,
                    )
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    text = None
                    break
                logger.warning(
                    "Phase C summarization call failed (attempt %d/%d), retrying in %ds: %s",
                    transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e,
                )
                transient_attempt += 1
                if retry_action["delay"]:
                    time.sleep(retry_action["delay"])
                continue

        if text is None:
            # The LLM call's own retry/key-rotation budget is exhausted, or
            # it hit a non-retryable error outright - no point spending the
            # second content-validity attempt on a call that's already
            # just proven it can't succeed right now with any configured
            # key (same reasoning as triage_all_mode_question's identical
            # early break).
            break

        # is_label_only_response runs on the RAW `text` (before .strip()
        # below collapses a "label line, then a blank line, then nothing"
        # response down to just the label) - it needs that blank line
        # intact to tell "just the label" apart from a plain single-line
        # response with no label convention at all (see its docstring).
        stripped = (text or "").strip()
        if stripped and not is_label_only_response(text or ""):
            return stripped, usage, None
        last_error = (
            "response was only the label, or missing the label/blank-line shape, with no real content after it"
            if stripped else "empty summarization response"
        )

    logger.warning("All-mode results summarization (Phase C) failed after retry: %s", last_error)
    return None, None, last_error


@translate_bp.route('/api/summarize-results', methods=['POST'])
def summarize_results():
    """See the "All databases" mode, Phase C section comment above for the
    full picture. Called by the client exactly once per "route" outcome
    turn, only after /api/execute has actually run every database Phase B
    selected (never for a single-connection session - client.js only ever
    calls this from executeSql()'s router_route handling)."""
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    data = request.get_json() or {}

    session_data = state_store.get_session(user_identity)
    provider = get_llm_provider(session_data.get('llm_provider'))
    llm_model = (
        data.get(provider.request_model_key) or data.get('model')
        or session_data.get('llm_model') or provider.default_model
    )
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        resp = jsonify({'success': False, 'error': provider.missing_key_error})
        return apply_session_cookie(resp, session_id), 400

    prompt = (data.get('prompt') or '').strip()
    database_results = data.get('database_results')
    if not prompt or not isinstance(database_results, list) or not database_results:
        resp = jsonify({'success': False, 'error': 'prompt and database_results are required'})
        return apply_session_cookie(resp, session_id), 400

    start_time = time.perf_counter()
    client = provider.make_client(api_key)
    cancel_token = cancel_handle = None
    close_fn = getattr(client, "close", None)
    if callable(close_fn):
        cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
    try:
        text, usage, error = summarize_all_mode_results(
            prompt, database_results, provider, client, llm_model, api_key=api_key,
            using_byok=bool(byok_key),
        )
    finally:
        if cancel_token is not None:
            cancel_registry.unregister(session_id, cancel_token)
        if cancel_handle is not None:
            cancel_handle.close()
    duration = round(1000 * (time.perf_counter() - start_time))

    if text is None:
        # `error` is the raw exception when the LLM call itself is what
        # failed (see summarize_all_mode_results' docstring) - format that
        # honestly, same as every other LLM-call failure in this app now
        # does. The other case (2 consecutive content-invalid responses,
        # nothing wrong at the API level) has no exception to report, so
        # it keeps the original generic message instead.
        error_message = (
            format_llm_error_for_user(provider, llm_model, error, using_byok=bool(byok_key))
            if isinstance(error, BaseException) else
            'Unable to summarize results right now.'
        )
        resp = jsonify({'success': False, 'error': error_message})
        return apply_session_cookie(resp, session_id)

    summary_text = "*** NO SQL *** " + text
    usage = usage or {}
    # Logged the same way Phase A's own triage call is (see
    # record_all_databases_triage's docstring) - "All Databases"/
    # "All Databases" rather than any one real connection, since this call
    # is likewise never "about" just one specific database.
    record_all_databases_triage(
        user_identity, prompt, summary_text, llm_model, duration,
        usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        usage.get("total_tokens", 0), usage.get("thinking_tokens", 0),
        usage.get("cached_content_tokens", 0),
    )

    resp = jsonify({'success': True, 'summary': summary_text})
    return apply_session_cookie(resp, session_id)


@translate_bp.route('/api/translate', methods=['POST'])
def translate_query():
    data = request.get_json() or {}

    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)

    # A blank/never-set session field (see state_store.py's get_session
    # docstring) falls back to get_llm_provider()'s own hardcoded default
    # (Google) - same not-explicitly-chosen-yet convention connection_id
    # already uses. A request-body override (gemini_model/claude_model/
    # openai_model, or the generic "model") still wins over the session's
    # saved model when both are present - it existed before the
    # session-level choice did and stays the more specific, one-off
    # override.
    session_data = state_store.get_session(user_identity)
    provider = get_llm_provider(session_data.get('llm_provider'))
    llm_model = (
        data.get(provider.request_model_key) or data.get('model')
        or session_data.get('llm_model') or provider.default_model
    )
    # A user's own "Bring Your Own Key" (see state_store.py's
    # get_llm_byok_key/set_session docstrings), when saved for this
    # provider, is used INSTEAD of the app's own env-configured pool for
    # every LLM call this request makes - triage, Phase B's per-connection
    # fan-out, and the single-connection retry loop below all resolve
    # `byok_key` from here (either directly, or via `_run_phase_b_fanout`
    # re-resolving it itself for its own worker threads - see that
    # function's docstring). `byok_key` is read-only from this point
    # down, so stream_translation() below can safely close over it without
    # `nonlocal`.
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)
    api_key = byok_key or provider.pick_api_key()
    if not api_key:
        return jsonify({'error': provider.missing_key_error}), 400
    tried_llm_keys = {api_key}

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt cannot be empty'}), 400

    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    # An explicit database_url override always means "use exactly this one
    # connection" (see resolve_conn_str above, which conn_str already
    # reflects) - it wins over "all" mode below, same as it always has.
    explicit_db_override = bool(data.get('database_url'))

    # "All configured databases" mode (see db.py's
    # resolve_in_scope_descriptors) runs a real two-phase flow - see
    # stream_translation()'s router_only_all_mode branch below,
    # connection_router.triage_all_mode_question, and _run_phase_b_fanout:
    # a triage call decides "answer" (table names alone are enough),
    # "route" (generate and execute real SQL against one or more specific
    # connections, in parallel), or "failed" (fixed apology text, no
    # fallback guess). Unconditional whenever in_scope_mode is "all",
    # regardless of how many connections are actually configured (even
    # just one) - triage still needs to decide "answer directly" vs.
    # "actually go query this database" either way, so there's no
    # connection-count threshold below which it's skipped. A session whose
    # in_scope_mode isn't "all" (the default "single", or an explicit
    # database_url override) takes none of the branches below - see
    # stream_translation()'s single-connection path, which is byte-for-byte
    # the same code path this endpoint has always run.
    in_scope_entries = resolve_in_scope_descriptors(session_data, user_identity)
    router_only_all_mode = session_data.get('in_scope_mode') == 'all' and not explicit_db_override
    #
    # The triage call itself DOES get this turn's ordinary conversation
    # history (see triage_all_mode_question's docstring) - it's a single,
    # non-per-database step, so there's exactly one shared thread for it to
    # consult (e.g. resolving "how large is THIS database" against a prior
    # turn's answer). Phase B's per-connection calls still each get an
    # empty history, deliberately - threading distinct per-database history
    # through those remains deferred, genuinely more complex follow-up
    # work.

    history = data.get('history', [])[-(HISTORY_MAX_TURNS * 2):]
    force_schema_refresh = bool(data.get('refresh_schema'))

    # Everything past this point - the schema fetch, the Gemini retry loop,
    # and building the final response - is streamed as newline-delimited
    # JSON (NDJSON) rather than returned as one JSON body, so the client can
    # show "retrying..." feedback live instead of just hanging for however
    # long the retry loop below takes (see client.js's readTranslateStream()).
    # Zero or more progress lines are emitted first:
    #   {"status": "retrying", "attempt": <next attempt #>, "maxAttempts": N,
    #    "delaySeconds": <float>, "rotatedKey": <bool>}
    # For the single-connection path specifically (router_only_all_mode
    # False - see stream_translation() below), exactly two more progress
    # lines are emitted ahead of the retry loop, so the client has
    # something better than a bare spinner for the two real waits that
    # happen before the first byte of SQL comes back - reading the schema,
    # then the LLM call itself:
    #   {"status": "phase_status", "phase": "schema"|"generating_sql",
    #    "message": "<short human-readable sentence>"}
    # This is deliberately just a label, not a progress bar - it doesn't
    # shrink either wait, it just tells the user which one they're in.
    # Router ("all databases") mode emits its OWN two "phase_status" lines
    # first, ahead of ITS two real pre-SQL waits - collecting every in-
    # scope connection's schema summary (build_router_candidate_summaries)
    # and the triage LLM call itself (triage_all_mode_question) - using
    # the exact same event shape, just different `phase` values:
    #   {"status": "phase_status", "phase": "collecting_schema_summaries"
    #    |"routing", "message": "<short human-readable sentence>"}
    # These used to not exist at all: before them, a router-mode request
    # streamed NOTHING for however long those two steps took (both can be
    # genuinely slow - schema collection scales with how many connections
    # are in scope, and triage now has its own real retry/key-rotation
    # budget, see triage_all_mode_question's docstring), leaving the
    # client with no visible progress until the FIRST event it could
    # otherwise render - phase_a_route - which only ever arrives once
    # triage has already fully finished, and only for the "route" outcome
    # (an "answer"/"failed" outcome gets no intermediate event at all
    # under the OLD design). router mode's own, more informative per-
    # connection progress (phase_a_route/phase_b_connection_done below)
    # still only starts once triage itself resolves to "route" - these two
    # new lines are what covers everything before that point, for every
    # outcome.
    # ...followed by exactly one terminal line:
    #   {"status": "done", "success": true, "sql": ..., "input_tokens": ...,
    #    "output_tokens": ..., "total_tokens": ..., "thinking_tokens": ...,
    #    "cached_content_tokens": ..., "duration": ...}
    #   or, on failure (non-retryable, or every retry exhausted):
    #   {"status": "done", "success": false, "error": "..."}
    #
    # IMPORTANT: because the HTTP status code has to be committed before any
    # of this streams - a chunked response can't retroactively become a 500
    # once a byte of it has already gone out - every request that makes it
    # this far now always returns HTTP 200, whether the translation itself
    # ultimately succeeds or fails. Failure lives in the terminal line's
    # "success"/"error" fields, not the HTTP status - callers (and tests)
    # must check that field. This is the one behavior change from before
    # streaming existed, where a failed translation was a real HTTP 500.
    # (The two early validation returns above - missing API key, empty
    # prompt - happen before any of this and keep their real 400 status,
    # since nothing has streamed yet at that point.)
    def stream_translation():
        nonlocal api_key
        cancel_token = None
        cancel_handle = None
        try:
            client = provider.make_client(api_key)
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)

            if router_only_all_mode:
                # "All databases" mode's real two-phase flow (see
                # connection_router.triage_all_mode_question and
                # _run_phase_b_fanout above): a triage call decides
                # whether the question can be answered directly from
                # table names alone, or genuinely needs real data from
                # one or more specific connections - and if so, generates
                # and executes real SQL against each of them,
                # independently and in parallel, exactly as if the user
                # had selected each one directly and asked the question
                # themselves. This is a complete, self-contained branch
                # that returns its own terminal NDJSON line directly, same
                # as the old Phase-A-only stub it replaces.
                #
                # Always runs triage regardless of how many connections
                # are configured, even just one - unlike the old stub,
                # which skipped the LLM call entirely when there was
                # "nothing to route between." Under this design that skip
                # would be wrong: even with one configured connection, the
                # triage call still decides "answer directly" vs.
                # "actually go query this database," so skipping it would
                # mean a single-connection "all" session could never get
                # real SQL - defeating the point of this feature for that
                # case.
                start_time = time.perf_counter()

                # First of router mode's two phase_status lines (see the
                # module docstring above) - collecting every in-scope
                # connection's schema summary can be a real, visible wait
                # (scales with how many connections are in scope; each
                # summary is itself schema_cache-backed, so this is fast
                # on a warm cache but not on a cold one or a forced
                # refresh), and previously had no progress indicator at
                # all.
                yield json.dumps({
                    "status": "phase_status",
                    "phase": "collecting_schema_summaries",
                    "message": "Collecting database summaries…",
                }) + "\n"
                candidate_summaries = build_router_candidate_summaries(in_scope_entries, user_identity)

                # Second of router mode's two phase_status lines - the
                # triage LLM call itself, which now carries its own real
                # retry/key-rotation budget (see triage_all_mode_question's
                # docstring), so this wait can be the longest one and
                # previously had no progress indicator at all either.
                yield json.dumps({
                    "status": "phase_status",
                    "phase": "routing",
                    "message": "Deciding which databases to contact…",
                }) + "\n"
                triage_result = triage_all_mode_question(
                    candidate_summaries, prompt, provider, client, llm_model, history=history,
                    api_key=api_key, using_byok=bool(byok_key),
                )
                # Phase A's own elapsed time and LLM usage, isolated from
                # whatever Phase B work (if any) happens next below -
                # logged as its own dedicated "All Databases"/"All Databases" translations-
                # table row further down (see
                # db.record_all_databases_triage's docstring), regardless
                # of outcome, since triage always runs exactly once per
                # request and is never "about" any one specific database.
                triage_duration = round(1000 * (time.perf_counter() - start_time))
                triage_usage = dict(triage_result.get("usage") or {})
                usage_info = dict(triage_usage)
                extra_fields = {}

                if triage_result["outcome"] == "answer":
                    # Can be answered from table names/dialects/general
                    # knowledge alone, no real database access needed -
                    # same '*** NO SQL ***' convention/rendering path
                    # client.js already handles with zero changes.
                    generated_sql = "*** NO SQL *** " + triage_result["answer"]
                    triage_log_text = generated_sql
                elif triage_result["outcome"] == "failed":
                    # Triage itself couldn't produce anything usable after
                    # its own bounded retry - deliberately NOT a fallback
                    # guess at some candidate connection: a wrong
                    # running real SQL against a database the user never
                    # asked about, so this shows a fixed apology instead.
                    # WHICH apology depends on WHY it failed (see
                    # triage_all_mode_question's docstring): "api_error"
                    # distinguishes a real technical/capacity failure (the
                    # LLM call itself raised and its own retry budget -
                    # key rotation and/or transient-error retries - ran
                    # out, e.g. every configured Gemini key was out of
                    # capacity) from a response that genuinely came back
                    # unparseable both times. These used to be
                    # indistinguishable, both showing _TRIAGE_FAILURE_TEXT
                    # - actively misleading for the api_error case, since
                    # it reads as "I couldn't understand your question"
                    # when the honest answer is a real, specific API/
                    # capacity problem - format_llm_error_for_user() below
                    # builds that message from triage_result["error"] (the
                    # raw exception - see triage_all_mode_question's
                    # docstring), including the actual provider error text,
                    # not just a generic "try again" apology.
                    if triage_result.get("api_error"):
                        generated_sql = "*** NO SQL *** " + format_llm_error_for_user(
                            provider, llm_model, triage_result["error"], using_byok=bool(byok_key)
                        )
                    else:
                        generated_sql = _TRIAGE_FAILURE_TEXT
                    triage_log_text = generated_sql
                else:  # "route" - needs real data from specific connection(s)
                    selected_entries = [in_scope_entries[i] for i in triage_result["indices"]]
                    # Each connection gets ITS OWN instruction - triage's
                    # own rewrite of `prompt` for that connection alone
                    # when it supplied one (see triage_all_mode_question's
                    # "database_prompts" docstring for why the original
                    # question, verbatim, is frequently wrong once
                    # narrowed to a single connection - e.g. it was phrased
                    # across multiple databases at once), else falling
                    # back to the original `prompt` unchanged for that one
                    # connection - today's original behavior, preserved
                    # per-connection rather than failing the rewrite
                    # entirely.
                    database_prompts_by_index = triage_result.get("database_prompts") or {}
                    entry_prompts = [
                        database_prompts_by_index.get(i) or prompt for i in triage_result["indices"]
                    ]
                    # Both computed BEFORE Phase B even starts (unlike
                    # before this streaming redesign, when routing_message
                    # was only computed once Phase B had already fully
                    # returned) - triage resolving to "route" is all
                    # either of these needs, and the client needs them
                    # immediately: the Summary tab text and one placeholder
                    # tab per selected connection, well before any single
                    # connection's own generation call has finished.
                    # The server-built fallback (the model's own "message"
                    # was empty/missing) gets the same label-line header the
                    # model is instructed to lead with itself (see
                    # _TRIAGE_SYSTEM_INSTRUCTION), so the Summary tab always
                    # shows one regardless of which source this text came
                    # from. Deliberately hardcoded English here, unlike the
                    # model's own (translated) label: this is a last-resort
                    # fallback with no LLM call of its own to ask for a
                    # translation from, and only ever fires when the model
                    # itself failed to provide a usable "message" - routing
                    # still succeeds either way, this is strictly cosmetic.
                    routing_message = triage_result.get("message") or (
                        "Triage\n\nChecking " + ", ".join(e["name"] for e in selected_entries) + " for your question."
                    )
                    connection_selection = [
                        {"kind": e["kind"], "id": e["id"], "name": e["name"]} for e in selected_entries
                    ]
                    yield json.dumps({
                        "status": "phase_a_route",
                        "routing_message": routing_message,
                        "connection_selection": connection_selection,
                    }) + "\n"

                    # Manually drains _run_phase_b_fanout (a generator -
                    # see its own docstring) rather than a plain `yield
                    # from`, since each per-completion event needs to be
                    # wrapped into its OWN NDJSON status line here, unlike
                    # generate_sql_for_connection's retry-progress `yield
                    # from` above, which forwards already-final dicts
                    # unchanged. The final four-value aggregate - the same
                    # shape a single blocking call to this function used
                    # to return before this redesign - is captured via
                    # StopIteration.value, the same idiom _drain_generation
                    # uses.
                    phase_b_gen = _run_phase_b_fanout(
                        selected_entries, entry_prompts, provider, llm_model, user_identity, force_schema_refresh,
                    )
                    try:
                        while True:
                            done_entry, classified = next(phase_b_gen)
                            yield json.dumps({
                                "status": "phase_b_connection_done",
                                "kind": done_entry["kind"], "id": done_entry["id"], "name": done_entry["name"],
                                **classified,
                            }) + "\n"
                    except StopIteration as stop:
                        sql_blocks, database_notes, generation_failures, phase_b_usage = stop.value

                    generated_sql = "\n\n".join(marked for _, marked in sql_blocks)
                    for k in phase_b_usage:
                        # `or 0` on both sides - same None-vs-missing-key
                        # defensive reasoning as _run_phase_b_fanout's own
                        # usage_totals loop above.
                        usage_info[k] = (usage_info.get(k) or 0) + (phase_b_usage.get(k) or 0)
                    extra_fields = {
                        # A list, even for a length-1 pick - present for
                        # EVERY selected connection regardless of whether
                        # it ended up with real SQL, a note, or a
                        # failure, so a follow-up turn's pinned_connections
                        # can still reuse this turn's routing decision
                        # even if Phase B partially failed/noted. Also
                        # what drives client.js's existing disclosure
                        # banner/pin-handling code - unchanged, since it
                        # already fires generically for any response
                        # carrying this field. Same list already sent in
                        # the phase_a_route line above.
                        "connection_selection": connection_selection,
                        # Marks this as the new "route" shape for
                        # client.js (byte-identical to today's response
                        # for the "answer"/"failed" outcomes above - zero
                        # client changes needed for those two).
                        "router_route": True,
                        "routing_message": routing_message,
                        "database_notes": database_notes,
                        "generation_failures": generation_failures,
                    }
                    record_entry = selected_entries[0]
                    # Phase A's own text isn't real SQL - "route" just
                    # means it decided real data was needed and picked
                    # who to ask, same '*** NO SQL ***' convention as the
                    # "answer"/"failed" outcomes above use for their own
                    # non-SQL text.
                    triage_log_text = "*** NO SQL *** " + routing_message

                end_time = time.perf_counter()
                duration = round(1000 * (end_time - start_time))
                input_tokens = usage_info.get("input_tokens", 0)
                output_tokens = usage_info.get("output_tokens", 0)
                total_tokens = usage_info.get("total_tokens", 0)
                thinking_tokens = usage_info.get("thinking_tokens", 0)
                cached_content_tokens = usage_info.get("cached_content_tokens", 0)

                # Phase A (triage) always gets its own dedicated
                # "All Databases"/"All Databases" translations-table row - see
                # record_all_databases_triage's docstring - using ONLY its
                # own duration/usage computed above, never Phase B's (kept
                # entirely separate below) so nothing is ever double-
                # counted across the two rows.
                record_all_databases_triage(
                    user_identity, prompt, triage_log_text, llm_model, triage_duration,
                    triage_usage.get("input_tokens", 0), triage_usage.get("output_tokens", 0),
                    triage_usage.get("total_tokens", 0), triage_usage.get("thinking_tokens", 0),
                    triage_usage.get("cached_content_tokens", 0),
                )

                if triage_result["outcome"] == "route":
                    # Phase B's own portion only, attributed to the first
                    # selected connection (same convention `connection_
                    # selection`'s ordering already uses elsewhere in this
                    # branch) - Phase A's share of the total duration/usage
                    # was already logged separately just above, so it's
                    # subtracted out here rather than counted twice.
                    phase_b_duration = max(0, duration - triage_duration)
                    record_translation(
                        user_identity, record_entry["descriptor"], prompt, generated_sql, llm_model,
                        phase_b_duration,
                        phase_b_usage.get("input_tokens", 0), phase_b_usage.get("output_tokens", 0),
                        phase_b_usage.get("total_tokens", 0), phase_b_usage.get("thinking_tokens", 0),
                        phase_b_usage.get("cached_content_tokens", 0),
                    )

                # `sql` may legitimately be "" here (every selected
                # connection returned a note or failed) - still
                # `success: True`, not an error, so the client renders
                # per-database detail from database_notes/
                # generation_failures instead of collapsing to a flat
                # error block.
                yield json.dumps({
                    'status': 'done',
                    'success': True,
                    'sql': generated_sql,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'total_tokens': total_tokens,
                    'thinking_tokens': thinking_tokens,
                    'cached_content_tokens': cached_content_tokens,
                    'duration': duration,
                    **extra_fields,
                }) + "\n"
                return

            # Byte-for-byte the same single-connection path this endpoint
            # has always run - see this module's docstring. Only reached
            # when router_only_all_mode is False (its own branch above
            # always returns before falling through to here), i.e. for the
            # overwhelming majority of sessions today: in_scope_mode isn't
            # "all", or an explicit database_url override is in play.
            #
            # First of the two phase_status lines this path emits (see the
            # module docstring above) - schema lookup is usually a cache
            # hit and near-instant, but can be a real, visible wait on a
            # cold cache or an explicit refresh_schema request, and the
            # client has no other way to distinguish "still building the
            # prompt" from "waiting on the model" without this.
            yield json.dumps({
                "status": "phase_status",
                "phase": "schema",
                "message": "Reading the database schema…",
            }) + "\n"
            schema = get_database_schema(conn_str, user_identity, force_refresh=force_schema_refresh)

            try:
                dialect_name = get_backend(conn_str).dialect_name
            except Exception:
                dialect_name = "PostgreSQL"
            dialect_intro = _DIALECT_PROMPT_INTROS.get(dialect_name, _DEFAULT_DIALECT_PROMPT_INTRO)

            system_instruction = dialect_intro + _COMMON_FORMAT_RULES
            schema_block = f"Database Schema:\n{schema}\n\n"

            # Sequencing matters here for prompt-caching purposes: the schema
            # is large and identical across every call in a given session
            # (barring a schema refresh), so it belongs as far to the front
            # of the input as possible - ahead of history, and ahead of the
            # ever-different new prompt. It can't be its own leading
            # message, though: Claude's Messages API rejects two consecutive
            # same-role messages ("roles must alternate between user and
            # assistant"), and history already starts with a "user" turn, so
            # a standalone schema-only user message in front of it would
            # violate that. Instead it's prepended onto whichever message
            # actually comes first - history's oldest turn when there is
            # history, or the new prompt itself on a conversation's very
            # first call. Either way the final order is system prompt ->
            # schema -> history -> new prompt. build_llm_input() below is
            # where each provider decides exactly how (see LlmProvider.
            # build_llm_input's docstring and each subclass's own).
            new_prompt_content = f"User Request: {prompt}\n\nSQL Query:"

            llm_input = provider.build_llm_input(history, schema_block, new_prompt_content)

            # The key-ROTATION retry budget (see LlmProvider.
            # supports_key_rotation's docstring) - sized to how many keys
            # are actually configured for a provider that supports it
            # (Gemini today - see _classify_gemini_error's 429 case), or 1
            # (meaning "already exhausted, since tried_llm_keys already has
            # one key in it") for a provider that doesn't, making this
            # branch of the retry loop below effectively unreachable for
            # Claude/OpenAI, exactly as before this dispatch existed.
            # tried_llm_keys already starts as {api_key} (set above, before
            # this generator runs), so it's the natural running total of
            # distinct keys tried. A "Bring Your Own Key" forces this down
            # to 1 (already met by tried_llm_keys' own starting size), same
            # reasoning as generate_sql_for_connection's own using_byok
            # parameter - there's no second key of the user's own to
            # rotate to, so this loop's rotate_key branch below is made
            # unreachable exactly the same way it already is for a
            # provider that doesn't support rotation at all.
            key_pool_size = 1 if byok_key else provider.get_key_pool_size()

            # Second of the two phase_status lines (see the module
            # docstring above) - emitted once, right before the retry loop
            # below makes its first attempt. This is the wait that's
            # normally the longest one and the one the "just a spinner"
            # complaint was really about; a "retrying" line (if any) will
            # naturally overwrite this same banner once/if the loop below
            # actually needs one.
            yield json.dumps({
                "status": "phase_status",
                "phase": "generating_sql",
                "message": "Generating commands for the database…",
            }) + "\n"

            start_time = time.perf_counter()
            generated_sql = ""
            usage_info = {}
            # transient_attempt tracks the SHARED, both-providers budget for
            # same-key/after-a-delay retries (MAX_TRANSLATION_ATTEMPTS) -
            # it's advanced only by the "else" (non-rotate) branch below.
            # The Gemini-only key-rotation budget above is tracked
            # separately via tried_llm_keys/gemini_key_pool_size, so a run
            # of 429s doesn't eat into this counter at all, and vice versa.
            transient_attempt = 1
            while True:
                try:
                    generated_sql, usage_info = provider.call(client, llm_model, llm_input, system_instruction)
                    break
                except Exception as e:
                    retry_action = provider.classify_error(e)
                    if retry_action is None:
                        raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e

                    if retry_action["rotate_key"]:
                        # Key-rotation budget: one attempt per configured
                        # key. Checked BEFORE picking the next key (rather
                        # than relying on pick_api_key's own fallback-to-
                        # full-pool behavior) so exhaustion is decided here,
                        # not masked by that fallback.
                        if len(tried_llm_keys) >= key_pool_size:
                            raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e
                        next_key = provider.pick_api_key(exclude=tried_llm_keys)
                        if next_key != api_key:
                            api_key = next_key
                            client = provider.make_client(api_key)
                        tried_llm_keys.add(api_key)
                        # No "in %ds" here - a key-rotation retry always
                        # fires immediately (see _classify_gemini_error's
                        # comment for why waiting doesn't make sense when
                        # the next attempt already uses a different key).
                        logger.warning(
                            "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                            provider.name, len(tried_llm_keys), key_pool_size, e
                        )
                        # Told to the client before continuing, so
                        # "retrying..." is visible even though there's no
                        # delay to speak of.
                        yield json.dumps({
                            "status": "retrying",
                            "attempt": len(tried_llm_keys),
                            "maxAttempts": key_pool_size,
                            "delaySeconds": 0,
                            "rotatedKey": True,
                        }) + "\n"
                        continue

                    # Shared transient-error budget (both providers).
                    if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                        raise LlmCallFailed(format_llm_error_for_user(provider, llm_model, e, using_byok=bool(byok_key))) from e
                    logger.warning(
                        "%s call failed (attempt %d/%d), retrying in %ds: %s",
                        provider.name, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e
                    )
                    # Told to the client before sleeping, not after, so
                    # "retrying..." is visible for the full delay instead of
                    # appearing right as the next attempt actually fires.
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": transient_attempt + 1,
                        "maxAttempts": MAX_TRANSLATION_ATTEMPTS,
                        "delaySeconds": retry_action["delay"],
                        "rotatedKey": False,
                    }) + "\n"
                    transient_attempt += 1
                    if retry_action["delay"]:
                        time.sleep(retry_action["delay"])
                    continue
            end_time = time.perf_counter()

            if generated_sql.startswith("```"):
                lines = generated_sql.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                generated_sql = "\n".join(lines).strip()

            duration = round(1000 * (end_time - start_time))
            input_tokens = usage_info.get("input_tokens", 0)
            output_tokens = usage_info.get("output_tokens", 0)
            total_tokens = usage_info.get("total_tokens", 0)
            thinking_tokens = usage_info.get("thinking_tokens", 0)
            cached_content_tokens = usage_info.get("cached_content_tokens", 0)

            # Anonymous visitors share a single per-session identity
            # (anonymous:<session_id>) rather than a real signed-in one, but
            # the translation is recorded the same way regardless - both for
            # aggregate usage/cost visibility (e.g. via export_state.py) and
            # because anonymous visitors can view/purge their own history via
            # the app same as anyone else (see history_routes.py).
            record_translation(user_identity, conn_str, prompt, generated_sql, llm_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens)

            yield json.dumps({
                'status': 'done',
                'success': True,
                'sql': generated_sql,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens,
                'thinking_tokens': thinking_tokens,
                'cached_content_tokens': cached_content_tokens,
                'duration': duration,
            }) + "\n"

        except Exception as e:
            logger.exception("Translation failed")
            yield json.dumps({
                'status': 'done',
                'success': False,
                'error': str(e) or f"{type(e).__name__} occurred during translation.",
            }) + "\n"
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()

    resp = Response(stream_with_context(stream_translation()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)