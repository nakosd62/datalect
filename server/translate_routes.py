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
    resolve_descriptor_by_reference,
)
from backends import get_backend
from connection_router import triage_all_mode_question, is_label_only_response, strip_markdown_fence
import cancel_registry
from concurrency_guard import TRANSLATE_GUARD, busy_response
from rate_limiter import translate_rate_limit, summarize_rate_limit

translate_bp = Blueprint('translate', __name__)

# Which LLM provider a request actually uses is resolved per-session (see
# translate_query()'s session_data.get('llm_provider') lookup below), never
# a provider-NAME env var - "google"/"anthropic"/"openai" are the only
# valid values, matching _LLM_PROVIDERS' keys below. A session that never
# explicitly picked one (via the model-selection UI) falls back to this
# app's one fleet-wide default: whichever provider get_llm_provider()
# returns for an unrecognized/blank name - see that function's (and
# _default_fleet_provider()'s) docstring. This used to be independently
# configurable via an LLM_PROVIDER env var; that's gone now, replaced by
# DEFAULT_MODEL below, which names a MODEL rather than a provider - the
# provider that model belongs to is derived from it, so there's still only
# one knob to set, not two that could drift out of sync with each other.

# Each provider's own *_MODELS env var (GOOGLE_MODELS/ANTHROPIC_MODELS/
# OPENAI_MODELS - see LlmProvider.models_env_var below) is a single
# comma-separated list: the full list is what the model-selection modal
# offers for that provider (see LlmProvider.preset_models). Left entirely
# unset, each provider falls back to its own hardcoded single-model default
# (LlmProvider.fallback_models) so this app works out of the box with zero
# model configuration - claude-sonnet-5 for Anthropic (strong structured-
# output reasoning at a much lower cost than the top-tier model),
# gpt-5.6-luna for OpenAI (its cost-efficient tier - "gpt-5.6" alone, no
# suffix, is an alias for the top "-sol" tier instead), gemini-3.6-flash for
# Google.
#
# DEFAULT_MODEL (a single, app-wide env var naming exactly one model, e.g.
# "claude-sonnet-5") is what actually picks each provider's default model
# now, instead of that provider's own *_MODELS list's first entry always
# winning by simply being first - see LlmProvider.default_model's
# docstring. It only takes effect for whichever provider's own
# preset_models actually contains that exact model name; every other
# provider's default_model is unaffected and still falls back to its own
# preset_models[0]. When a session hasn't picked a provider at all yet,
# DEFAULT_MODEL also decides which provider becomes the app's ONE
# fleet-wide default (see _default_fleet_provider() below) - so setting it
# to an Anthropic or OpenAI model moves the whole fleet's default off
# Google without any separate provider-name knob. Google/gemini-3.6-flash
# remains the final fallback-of-last-resort when DEFAULT_MODEL is unset,
# blank, or doesn't match any currently-configured model at all.

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
        "ROUND(...) with an explicit decimal-places argument (ROUND(x, n)) is ONLY defined for a numeric argument - there is NO round(double precision, integer) overload, only a separate 1-argument round(double precision) (rounds to the nearest integer, no precision control). Many common expressions actually evaluate to double precision, not numeric, even though they look like plain arithmetic - most often PERCENTILE_CONT/PERCENTILE_DISC, AVG()/SUM() over a float/double precision column, STDDEV/VARIANCE (and their _POP/_SAMP variants), and math functions like SQRT/LN/LOG/EXP/POWER/RANDOM. Calling ROUND(<any of these>, n) fails with \"function round(double precision, integer) does not exist\" unless the argument is cast first, e.g. ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)::numeric, 2). When it isn't certain an expression is already numeric, cast it to ::numeric before passing it to a 2-argument ROUND.\n"
        "The FILTER (WHERE <condition>) clause can ONLY be attached directly to a single aggregate function call immediately to its left - either a plain one (e.g. COUNT(*) FILTER (WHERE ...), SUM(x) FILTER (WHERE ...)) or an ordered-set aggregate's WITHIN GROUP form (e.g. PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) FILTER (WHERE ...)). It can NEVER be attached to a parenthesized expression that combines two or more aggregate calls, or to any other non-aggregate expression - e.g. (MAX(x) - MIN(x)) FILTER (WHERE ...) is a syntax error (\"syntax error at or near FILTER\"), even though each individual MAX(x)/MIN(x) call could validly carry its own FILTER. When a computed combination of aggregates needs to be filtered, apply FILTER separately to each aggregate call inside the expression instead (e.g. MAX(x) FILTER (WHERE ...) - MIN(x) FILTER (WHERE ...)), rather than wrapping the whole combined expression in one outer FILTER.\n"
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
    "If you cannot respond at all with reasonable confidence, return '*** NO SQL *** ' followed by a brief, specific explanation of WHY - e.g. the prompt is too ambiguous to act on, it references data/tables that aren't in the schema below, or it asks for something this database/dialect can't express. A bare, unexplained refusal (just 'I am not able to respond to your prompt.' with nothing else) is NOT acceptable - always give the user the actual reason, the same way you're required to explain your reasoning elsewhere in this app (e.g. when picking which database to check).\n"
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

# The CURRENT turn's own results, fed to the results-summarization LLM
# calls (_build_summary_prompt for "all databases" mode's Phase C,
# _build_single_summary_prompt for single-connection mode) - deliberately
# a SEPARATE, much more generous cap from HISTORY_RESULT_MAX_ROWS above:
# this is real, current data the summarization call exists to reason over
# in full, not old history being replayed turn after turn, so the default
# here is far higher. It still needs a real ceiling, though, now that both
# of those calls send every row rather than none - an adversarial (or just
# very wide) query (e.g. a bare `SELECT * FROM huge_table`) could otherwise
# blow the prompt out to an enormous token count on a single turn, with no
# history multiplier even needed to get there. Override via env var if
# 1000 is too aggressive/lenient for your data.
SUMMARY_RESULTS_MAX_ROWS = int(os.environ.get("SUMMARY_RESULTS_MAX_ROWS", 1000))

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
# backends/base.py's SCHEMA_SHARD_MIN_GROUP_SIZE. Formerly named
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
    anything else ambiguous in their scope between capturing the LLM
    exception and returning it (summarize_all_mode_results' own schema
    fetch, via _build_all_mode_schema_block, happens BEFORE this retry
    loop even starts, and get_database_schema() never raises regardless -
    see its own docstring), so their callers (translate_routes.py's
    router_only_all_mode branch, and the /api/summarize-results route)
    call format_llm_error_for_user() directly on the raw exception instead -
    one fewer layer of indirection where it isn't needed."""
    pass


def format_results_table_text(columns, rows, max_rows=500):
    """Render a query result set as plain text suitable for an LLM prompt."""
    cols = columns or []
    rws = rows or []
    text = f"Columns: {', '.join(cols)}\nTotal Rows: {len(rws)}\nSample/Full Data:\n"
    text += "\n".join([str(r) for r in rws[:max_rows]])
    return text


def _render_history_result_block(index, res):
    """Render one turn's per-statement history-results entry as text. A
    failed statement/connection is shaped {error, ...} (see client.js's
    summarizeResultForHistory) rather than {columns, rows, rowCount} -
    previously that shape fell through this code silently as a blank
    "Columns: \nTotal Rows: 0" block, losing the error text entirely. Now
    an error entry renders its actual error message instead."""
    if 'error' in res:
        header = f"[Query Result {index + 1} - failed]"
        return header + "\n" + f"Error: {res.get('error')}"
    cols = res.get('columns') or []
    rws = res.get('rows') or []
    row_count = res.get('rowCount', len(rws))
    shown_rows = min(len(rws), HISTORY_RESULT_MAX_ROWS)
    header = f"[Query Result {index + 1} - {row_count} row(s) total, showing {shown_rows}]"
    return header + "\n" + format_results_table_text(cols, rws, max_rows=HISTORY_RESULT_MAX_ROWS)


def _build_history_combined_text(msg):
    """Build the full text for one client-supplied history turn: the base
    {role, text} text, any per-statement `results` (or errors - see
    _render_history_result_block above) from that turn's execution, and
    finally that turn's own stored summary, so a later turn's LLM call sees
    not just the raw data/errors but what was actually told to the user
    about them.

    The summary comes from one of two places depending on which mode the
    turn was: single-connection turns carry it directly as `summary`
    (mirroring pending.entry.summary / modelEntry.summary in client.js);
    all-mode turns carry it nested under `allMode.routingMessage` - that
    field starts out (in captureAllModeHistory's caller) as the triage
    routing message, but is overwritten with the real Phase C summary text
    before the turn is persisted to history (see the
    `notes.routingMessage = summaryEntry.text` assignment in client.js just
    before captureAllModeHistory() is called), so by the time it reaches
    here it IS the summary, not the routing message.

    Without this, build_gemini_history_contents/build_claude_history_
    messages/build_openai_history_messages below only ever read the raw
    columns/rows/rowCount/error data for a past turn - the explanation the
    user actually saw (which may highlight things not obvious from the raw
    data/error alone) was silently unavailable to later turns."""
    text = msg.get("text") or ""
    combined_text = text

    hist_results = msg.get("results")
    if hist_results:
        result_blocks = [_render_history_result_block(i, res) for i, res in enumerate(hist_results)]
        combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

    summary_text = msg.get("summary")
    if not summary_text:
        all_mode = msg.get("allMode")
        if all_mode:
            summary_text = all_mode.get("routingMessage")
    if summary_text:
        combined_text = combined_text + "\n\n[Summary given to the user]\n" + summary_text

    return combined_text


def build_gemini_history_contents(history):
    """
    Turn the client-supplied chat history into Gemini `types.Content` objects.
    Each history message is {role, text} and may optionally carry a `results`
    list - one entry per SQL statement that was executed for that turn, each
    shaped like {columns, rows, rowCount} or {error} - and/or a stored
    summary (`summary`, or `allMode.routingMessage` for all-mode turns). See
    _build_history_combined_text above for exactly how these are combined
    into that turn's text.
    """
    contents = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

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
    unchanged. The results/error/summary-appending logic is identical to
    the Gemini version (see _build_history_combined_text) - only the
    returned container shape differs."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

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
    The results/error/summary-appending logic is identical to the other two
    providers' versions (see _build_history_combined_text) - only the
    returned container shape (a plain dict, not a types.Content) differs
    from Gemini's."""
    messages = []
    for msg in history:
        role = msg.get("role")
        text = msg.get("text")
        if not (role and text):
            continue

        combined_text = _build_history_combined_text(msg)

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
        (request_model_key/"model") nor a saved session choice picks one.

        Checks the app-wide DEFAULT_MODEL env var first (parsed live, same
        as preset_models above, so it's never stale relative to a test or
        deployment that sets it): if that value is one of THIS provider's
        own preset_models, it wins - letting an operator pin the default to
        any specific model (not necessarily the first entry in this
        provider's own *_MODELS list) via one env var, without reordering
        that list. Otherwise falls back to preset_models' first entry, i.e.
        this provider's *_MODELS env var's first entry, or
        fallback_models[0] when that env var is unset - exactly the
        behavior before DEFAULT_MODEL existed. See get_llm_provider()'s
        docstring for how DEFAULT_MODEL also picks which provider is used
        at all, for a session that hasn't chosen one either."""
        override = os.environ.get("DEFAULT_MODEL", "").strip()
        if override and override in self.preset_models:
            return override
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


def _default_fleet_provider():
    """The provider used for a session that hasn't picked one at all - see
    get_llm_provider()'s own docstring below, the only caller. Resolved
    from DEFAULT_MODEL (see LlmProvider.default_model's docstring) when
    that env var names a model belonging to one of the three registered
    providers' own preset_models - whichever provider matches becomes the
    app's fleet-wide default, so setting DEFAULT_MODEL alone can move it
    onto Claude or OpenAI, not just Google. Falls back to Google - this
    app's original, still-hardcoded fallback-of-last-resort - when
    DEFAULT_MODEL is unset, blank, or doesn't match any currently-
    configured model at all. Checked in _LLM_PROVIDERS' own definition
    order (google, anthropic, openai), so a DEFAULT_MODEL value that
    happened to collide across two providers' preset_models (unlikely in
    practice - model names aren't shared across vendors) would
    deterministically resolve to the earlier one rather than varying by
    dict iteration order."""
    override = os.environ.get("DEFAULT_MODEL", "").strip()
    if override:
        for provider in _LLM_PROVIDERS.values():
            if override in provider.preset_models:
                return provider
    return _LLM_PROVIDERS["google"]


def get_llm_provider(name):
    """Returns the LlmProvider for `name` (a session's saved llm_provider
    value - "google"/"anthropic"/"openai"). Unlike backends/__init__.py's
    get_backend() - which raises on an unrecognized "type" - an
    unrecognized/blank provider name here falls back to this app's ONE
    fleet-wide default (see _default_fleet_provider() above - Google unless
    DEFAULT_MODEL names a model that belongs to a different provider)
    rather than erroring, and the same graceful fallback also covers a
    session whose saved value predates this app's provider labels being
    renamed from "gemini"/"claude" to "google"/"anthropic" - such a session
    just silently reverts to the default instead of erroring."""
    return _LLM_PROVIDERS.get(name) or _default_fleet_provider()


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
# outright (see triage_all_mode_question's "failed" outcome). Reserved
# specifically for the "api_error": False case - the model actually
# responded (twice), but with something unparseable both times, so there's
# no real per-call detail to surface (unlike _COMMON_FORMAT_RULES' own "I
# cannot respond with reasonable confidence" case, which asks the MODEL
# itself to explain why in its own words - there's no such text to draw on
# here). Still names the one honest reason that IS known (two attempts,
# neither produced a usable response) rather than a bare, unexplained "I
# am not able to respond" with nothing behind it. The OTHER "failed" case
# (api_error=True: the LLM call itself raised, and its own retry budget -
# key rotation and/or transient-error retries - was fully used up without
# ever getting a response at all) used to show a second fixed apology
# text here (identical regardless of which model or what actually went
# wrong); it's now built per-call by format_llm_error_for_user() instead
# (see that function's own section comment, and its call site in
# stream_translation()'s router branch below) - honest about WHY it
# failed, and including the real error, rather than one more generic
# "try again in a moment."
_TRIAGE_FAILURE_TEXT = (
    "*** NO SQL *** I wasn't able to produce a usable response to your "
    "prompt, even after retrying. Try rephrasing your question."
)


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
    ("ok", generated_sql, usage_info, duration_ms) or
    ("failed", error_str, duration_ms), the exact tuple shapes
    _run_phase_b_fanout's ThreadPoolExecutor loop already produces - into
    the one shape both that function's per-completion streaming event AND
    its final original-order summary loop need, so the marker-prepend/
    note-strip logic is written exactly once instead of twice. Returns one
    of:
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
    outcome[0] == "failed" by the caller before this is invoked. The
    trailing duration_ms in both tuple shapes is irrelevant to
    classification itself (it's what _run_phase_b_fanout's own final loop
    uses to log each connection's own translations-table row) - unpacked
    here only so this still works against the real tuple shape.
    """
    if outcome[0] == "failed":
        return {"outcome": "failed", "error": outcome[1]}
    _, generated_sql, _usage_info, _duration = outcome
    stripped = (generated_sql or "").strip()
    if not stripped:
        return {"outcome": "note", "text": ""}
    if _NO_SQL_PREFIX_RE.match(stripped):
        return {"outcome": "note", "text": _strip_no_sql_prefix(stripped)}
    marked = f"-- database: {entry['kind']}:{entry['id']} ({entry['name']})\n{stripped}"
    return {"outcome": "sql", "sql": marked}


def _run_phase_b_fanout(selected_entries, prompts, histories, provider, model, user_identity, force_schema_refresh):
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

    `histories` is likewise a list the same length/order as
    `selected_entries` - each connection's own FULLY MERGED chat history
    (see stream_translation()'s `connection_histories` declaration comment
    for where this comes from and why it's already merged across single-
    connection mode and every prior all-mode turn by the time it reaches
    here). Same obliviousness as `prompts`: this function just sends
    histories[i] to selected_entries[i], already truncated/shaped by the
    caller.

    Each call is fully independent: its OWN freshly-picked api_key AND a
    client built from that exact key (never a shared tried-keys set - N
    threads racing on one shared mutable set would corrupt it - see
    generate_sql_for_connection's docstring), that connection's OWN
    history (histories[i] above), and that connection's own full schema/
    dialect intro - i.e. exactly as if the user had selected just that one
    connection and submitted its own `prompts[i]` directly. There is
    deliberately no shared `client` parameter here (unlike
    triage_all_mode_question/
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

    Returns (sql_blocks, database_notes, generation_failures, usage_totals,
    phase_b_log_entries):
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
      phase_b_log_entries: [{"entry", "prompt", "duration", "sql_command",
        "usage"}, ...] - ONE PER SELECTED CONNECTION, regardless of
        outcome, same original order - what stream_translation()'s "route"
        outcome branch needs to log each connection's OWN dedicated
        translations-table row (see record_translation) rather than one
        combined row for the whole batch attributed to only the first
        connection, which is what this function used to force on every
        caller. `duration` is this connection's own real measured elapsed
        time (never a derived share of a shared total - these calls run in
        parallel, so "correct" here means each call's own actual wall
        time), `usage` is `{}` for a failed call (nothing billable was
        ever returned) or that call's real usage_info dict otherwise, and
        `sql_command` is the exact text to log - real marker-prepended
        SQL, a "*** NO SQL ***"-prefixed note (or the bare prefix alone
        for the rare blank-response case), or a "TRANSLATION_ERROR
        (<error>)" sentinel - matching whichever of the three outcomes
        this specific connection had, the same conventions used elsewhere
        in this module for a non-SQL or failed translation.

    Resolves `user_identity`'s "Bring Your Own Key" value for `provider`
    (state_store.get_llm_byok_key) exactly ONCE here, up front - not
    per-worker - since it's the same user/provider for every entry in
    this fan-out. When set, every worker uses that key instead of picking
    its own from the env-configured pool, and generate_sql_for_connection
    is told using_byok=True so its own retry loop won't try to rotate to
    a different (env-configured) key on an auth failure.
    """
    byok_key = state_store.get_llm_byok_key(user_identity, provider.name)

    def _run_one(entry, entry_prompt, entry_history):
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
            entry["descriptor"], entry_prompt, entry_history, provider, worker_client, model, user_identity,
            force_schema_refresh=force_schema_refresh,
            api_key=worker_api_key, tried_keys={worker_api_key}, using_byok=bool(byok_key),
        )
        return _drain_generation(gen)  # (generated_sql, usage_info, duration_ms, _key, _client)

    def _run_one_timed(entry, entry_prompt, entry_history):
        # Wraps _run_one with its OWN start_time, captured here rather than
        # trusting generate_sql_for_connection's own returned duration_ms -
        # that value only exists on the success path (it's computed right
        # before that function's `return`, which a raised LlmCallFailed
        # never reaches). Measuring here instead means every connection
        # gets a real, honest duration whether it ultimately succeeds or
        # fails - each per-connection translations-table row logged below
        # (see stream_translation()'s "route" outcome branch) needs exactly
        # this, the same way the single-connection /api/translate failure
        # path and generate_sql_for_connection's own success path do.
        start_time = time.perf_counter()
        try:
            generated_sql, usage_info, _duration, _key, _client = _run_one(entry, entry_prompt, entry_history)
            duration = round(1000 * (time.perf_counter() - start_time))
            return ("ok", generated_sql, usage_info, duration)
        except Exception as e:
            duration = round(1000 * (time.perf_counter() - start_time))
            return ("failed", str(e), duration)

    outcomes = [None] * len(selected_entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected_entries)) as pool:
        future_to_index = {
            pool.submit(_run_one_timed, entry, prompts[i], histories[i]): i
            for i, entry in enumerate(selected_entries)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            entry = selected_entries[index]
            # _run_one_timed never raises (it catches its own exceptions
            # to measure duration on both the success and failure path -
            # see its own comment) - future.result() here can only ever
            # raise for something truly unexpected (e.g. the worker thread
            # itself being killed), which is deliberately NOT caught, same
            # as any other unexpected crash in this module.
            outcomes[index] = future.result()
            if outcomes[index][0] == "failed":
                logger.warning(
                    "Phase B generation failed for %s:%s: %s",
                    entry["kind"], entry["id"], outcomes[index][1],
                )
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
    # One entry per connection in `selected_entries` (ORIGINAL order, same
    # as everything else this function returns) - everything
    # stream_translation()'s "route" outcome branch needs to log this
    # connection's OWN dedicated translations-table row, rather than the
    # old single combined row attributed only to the first selected
    # connection (see this function's own module-level history/audit
    # notes above - this replaces that bundling entirely, one real row per
    # connection instead of one for the whole batch): the descriptor to
    # attribute it to, the actual per-connection instruction that was sent
    # (prompts[i] - triage's own rewrite when it supplied one, else the
    # user's original question, same resolution stream_translation()
    # already does before calling this function), the real per-connection
    # duration measured above (never a derived share of the total wall
    # time - these calls run in parallel, so each one's own elapsed time
    # is the honest number), that connection's own usage (zeroed for a
    # failure, same convention the single-connection /api/translate
    # failure path now uses), and the sql_command text to log - real SQL,
    # a "*** NO SQL ***"-prefixed note, or a TRANSLATION_ERROR(...)
    # sentinel, matching whichever of the three outcomes this connection
    # actually had.
    phase_b_log_entries = []
    for i, (entry, outcome) in enumerate(zip(selected_entries, outcomes)):
        entry_prompt = prompts[i]
        if outcome[0] == "failed":
            _, error_str, duration = outcome
            generation_failures.append({
                "kind": entry["kind"], "id": entry["id"], "name": entry["name"], "error": error_str,
            })
            phase_b_log_entries.append({
                "entry": entry, "prompt": entry_prompt, "duration": duration,
                "sql_command": f"TRANSLATION_ERROR ({error_str})",
                "usage": {},
            })
            continue
        _, generated_sql, usage_info, duration = outcome
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
            log_sql_command = classified["sql"]
        else:
            # "note" outcome - classified["text"] may legitimately be ""
            # (the model's response was blank after stripping); the
            # aggregate `database_notes` list still drops that empty case
            # exactly as it always has (nothing useful to show in the
            # Summary tab for it), but this connection still gets its own
            # logged row below, using the same "*** NO SQL ***" convention
            # as every other non-SQL reply in this table - an empty note
            # logs as the bare prefix rather than silently having no
            # sql_command text at all.
            if classified["text"]:
                database_notes.append({
                    "kind": entry["kind"], "id": entry["id"], "name": entry["name"],
                    "text": classified["text"],
                })
            log_sql_command = ("*** NO SQL *** " + classified["text"]) if classified["text"] else "*** NO SQL ***"
        phase_b_log_entries.append({
            "entry": entry, "prompt": entry_prompt, "duration": duration,
            "sql_command": log_sql_command, "usage": usage_info or {},
        })
    return sql_blocks, database_notes, generation_failures, usage_totals, phase_b_log_entries


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

# --- Language verification for the summarization steps below -------------
#
# Both _SUMMARY_SYSTEM_INSTRUCTION and _SINGLE_SUMMARY_SYSTEM_INSTRUCTION
# already tell the model (twice - once in the system instruction, once
# again in the trailing reminder _build_summary_prompt/_build_single_
# summary_prompt append to the actual user-turn text) to answer in the
# SAME LANGUAGE as the user's original question, never the language of the
# schema/table names or of the actual result data. That instruction alone
# turned out not to be reliable enough in practice: a database whose
# result rows/table content are themselves in some other language (e.g. a
# customer table full of German city/product names) can pull a model's
# output toward THAT language instead, regardless of how many times the
# prompt says not to - this has been observed concretely, and reported as
# happening consistently, with real production traffic. detect_language/
# describe_language below (imported from language_detect.py - see that
# module's own docstring for why this lives in a separate, shared module
# rather than inline here) add a second, independent layer that doesn't
# rely on the model choosing to comply: detect the question's actual
# language up front (so the reminder can name it concretely, e.g. "Respond
# in German." instead of the vaguer "same language as the question"), and,
# after the model responds, verify the response is actually in that
# language - if it isn't, _summarize_with_retry (below) discards the
# response and retries with an even more forceful, explicit correction
# rather than silently handing the user a summary in the wrong language.
#
# The exact same gap existed - unfixed, until now - for two OTHER free-text
# call sites: this file's own main NL-to-SQL translation call (the
# '*** NO SQL ***' free-text branch - see _no_sql_language_mismatch below
# and its call site in stream_translation()'s single-connection path), and
# connection_router.py's triage_all_mode_question ("answer"/"message" -
# see that function's own use of language_detect.detect_language/
# describe_language). Both were previously relying on nothing but
# _COMMON_FORMAT_RULES'/_TRIAGE_SYSTEM_INSTRUCTION's own "write this in the
# user's language" instruction line, with no verification behind it at all
# - the identical unverified-instruction gap the summarization fix above
# was built to close, just never extended to these two call sites until
# now.
from language_detect import detect_language as _detect_language, describe_language as _describe_language


def _no_sql_language_mismatch(generated_sql, expected_language_code):
    """Returns the language code the free-text portion of `generated_sql`
    is actually written in, when that confidently differs from
    `expected_language_code` - or None when there's nothing to flag.

    Added alongside the language-verification machinery above
    (_detect_language/_summarize_with_retry, originally built for the two
    post-execution summarization calls - see the section comment above)
    to close the same gap for the ORIGINAL NL-to-SQL translation call
    itself: a '*** NO SQL ***'-prefixed free-text reply (a general-
    knowledge answer, a help-popup request, an error explanation - see
    _COMMON_FORMAT_RULES) is exactly the kind of prose that can drift into
    the wrong language under the same "foreign-language data pulls the
    model along" failure mode the summarization fix was built for - but
    until this was added, this call site had only a bare system-prompt
    instruction telling the model to match the user's language, with
    nothing verifying it actually did. (connection_router.py's
    triage_all_mode_question has the identical need for its own "answer"/
    "message" free text, but calls language_detect.detect_language
    directly rather than through this function - see that module's own
    docstring for why it can't import from this file at all.)

    Deliberately narrow: only ever checks free text that's already been
    identified as such by the caller (the '*** NO SQL ***'-stripped
    portion of a translation response) - it does NOT try to detect whether
    `generated_sql` itself is a '*** NO SQL ***' reply; the caller
    (stream_translation()'s single-connection path, which also generates
    plain SQL with no free text to check at all) checks _NO_SQL_PREFIX_RE
    itself first. Ordinary generated SQL is never a valid `generated_sql`
    argument here for that reason - running _detect_language on a SELECT
    statement is meaningless. SQL comments the model was asked to add
    (_COMMON_FORMAT_RULES' other named "write this in the user's language"
    case) are also NOT covered - reliably isolating just the comment text
    out of a whole SQL script for language detection is separate work;
    this closes the far more common and more visibly reported gap (a whole
    free-text reply coming back in the wrong language), not every corner
    _COMMON_FORMAT_RULES' instruction touches.

    Returns None whenever there's nothing actionable to say:
    `expected_language_code` is None (detection unavailable/low-confidence
    on the user's own prompt - see _detect_language's own docstring),
    `generated_sql` is empty, detection on it is itself unavailable/
    low-confidence, or it already matches `expected_language_code`."""
    if expected_language_code is None:
        return None
    text = (generated_sql or "").strip()
    if not text:
        return None
    actual_language_code = _detect_language(text)
    if actual_language_code is not None and actual_language_code != expected_language_code:
        return actual_language_code
    return None


_SUMMARY_SYSTEM_INSTRUCTION = (
    "You previously helped route a user's natural-language question to one or more databases, and real "
    "queries have now been run against each of them. You will be given the user's ORIGINAL question and, "
    "for each database that was queried, exactly one of: its actual result rows, a note that it had "
    "nothing relevant to contribute, or an error explaining that querying it failed. Each database's "
    "results block below is labeled with its own [index], starting at [0] - use these SAME indices, as "
    "strings, to key your response.\n"
    "Respond with ONLY a single JSON object - no markdown code fences, no other text before or after it - "
    "shaped exactly like this:\n"
    "{\"label\": \"...\", \"per_database\": {\"0\": \"...\", \"1\": \"...\", ...}, \"cross_database\": "
    "\"...\" or null}\n"
    "CRITICAL, before anything else: every string value in this JSON - the label, AND every per-database "
    "paragraph, AND the cross_database paragraph if you write one - MUST be written in the SAME LANGUAGE "
    "as the user's original question, never the language of the database/table names or of the results "
    "data you're given, and never any other language. This applies to every single string you write, not "
    "just the label.\n"
    "\"label\" is a short (one to two word) section-heading label meaning \"Results Summary\" - in English "
    "this label is literally the phrase \"Results Summary\", but you must instead write it TRANSLATED into "
    "the SAME LANGUAGE as the user's original question. A response whose \"per_database\" is empty, or "
    "missing an entry for one of the indices given a results block above, is not a valid response - every "
    "such index must get its own entry.\n"
    "\"per_database\" must have ONE separate short paragraph PER DATABASE, keyed by that database's own "
    "[index] (as a string, e.g. \"0\"), answering the original question using just that database's own "
    "results. Keep every paragraph brief - one or two sentences - even if the underlying result set is "
    "large: this is a summary, not a report. If a database noted it had nothing relevant, say so in one "
    "short sentence rather than skipping it silently, so the user can see every database was actually "
    "considered. If a database's query instead failed with an error, don't just note that it failed - "
    "briefly explain, in plain language, what the error suggests actually went wrong (e.g. a permissions "
    "problem, a timeout, an ambiguous or unsupported request) and, if it's apparent from the error text, "
    "what could fix it, so the user understands the failure instead of only knowing that one occurred.\n"
    "\"cross_database\" is a SEPARATE field for a short paragraph that spans MULTIPLE databases at once "
    "(e.g. comparing two of them, or a single figure combined across all of them) - only set it to a "
    "non-null string when the question genuinely asks for something like that; otherwise set it to JSON "
    "null. Never use this field to restate or recap the per-database paragraphs a second time - it is "
    "only for content that couldn't be attributed to any single database's own paragraph.\n"
    "Respond with the JSON object only - no markdown tables, no bullet points, no headings, no commentary "
    "outside the JSON's own string values.\n"
    "One final reminder, since it's the single most important rule above: the language of every string "
    "you write must match the user's original question, not the language of the schema/data.\n"
)


def _build_summary_prompt(user_question, database_results, expected_language_code=None):
    """Renders `database_results` - client-submitted
    [{"name", "sql", "columns", "rows", "rowCount"} | {"name", "note"} |
    {"name", "error"} | {"name", "sql", "error"}, ...], one entry per
    statement result/note/failure Phase B + the client's own /api/execute
    call produced for a "route" outcome turn - into the prompt for Phase
    C's summarization call above: a "SQL executed for each database"
    section (Gap 4 of "Turn History Handling in Datalect" - previously
    Phase C had no SQL in its prompt at all, only the raw results/errors,
    despite the design's own LLM-3 input spec calling for "generated SQL
    for all in-scope databases"), followed by one labeled results block
    per entry, same as before. A note/generation-failure entry has no
    `sql` (nothing was ever generated to run for it) and is simply
    skipped in the SQL section, same as it's already skipped from having
    a results block below.

    Real result rows are capped at SUMMARY_RESULTS_MAX_ROWS (Gap 5's fix
    made this call reason over "the complete results/errors from all tabs
    and databases" per the design doc, matching _build_single_summary_
    prompt's own equivalent cap below - but "complete" with no ceiling at
    all is an abuse/cost vector once every row is actually being sent to
    an LLM call: a single wide `SELECT *` against a huge table would blow
    the prompt out to an enormous token count on this ONE turn alone, no
    history multiplier even needed. SUMMARY_RESULTS_MAX_ROWS is
    deliberately a SEPARATE, far more generous constant than
    HISTORY_RESULT_MAX_ROWS above (see its own definition/comment) - that
    one bounds how much of old, already-summarized history gets replayed
    turn after turn; this one bounds the CURRENT turn's own data, being
    reasoned over exactly once, for exactly the purpose of producing the
    summary that (once persisted - see captureAllModeHistory in client.js)
    is what future turns actually see instead. When a result set is
    actually truncated, the header names both the real total row count and
    how many are shown, so the model - and, since the header text ends up
    in the summary that's replayed as history, later turns too - isn't
    misled into thinking it saw everything.

    `expected_language_code` is _detect_language(user_question)'s result,
    computed once by the caller (summarize_all_mode_results) and threaded
    through here so the trailing reminder can name the target language
    concretely (e.g. "Respond in German.") instead of only the indirect
    "same language as the question" framing - see the language-
    verification section comment above _SUMMARY_SYSTEM_INSTRUCTION for
    why. None (detection unavailable or too low-confidence to trust)
    leaves the reminder exactly as it always was.

    Each per-database results/note/error block is prefixed with its own
    "[i]" (0-based index into `database_results`, matching this project's
    established convention for numbering candidates in a model prompt -
    see connection_router.py's _build_candidate_schema_block's own
    "[{i}] name=..." - so _SUMMARY_SYSTEM_INSTRUCTION's JSON contract can
    key its "per_database" object by that same index and the caller
    (summarize_all_mode_results/_clean_summary_response) can zip the
    parsed response straight back against `database_results` by position,
    with no name-matching or extra bookkeeping needed."""
    sql_blocks = []
    for entry in (database_results or []):
        sql = entry.get("sql")
        if sql:
            name = entry.get("name") or "Unknown database"
            sql_blocks.append(f"{name}:\n{sql}")
    sql_joiner = "\n\n"
    sql_section = f"SQL executed for each database:\n\n{sql_joiner.join(sql_blocks)}\n\n" if sql_blocks else ""

    blocks = []
    for i, entry in enumerate(database_results or []):
        name = entry.get("name") or "Unknown database"
        error = entry.get("error")
        note = entry.get("note")
        if error:
            blocks.append(f"[{i}] {name}: query failed - {error}")
        elif note:
            blocks.append(f"[{i}] {name}: {note}")
        else:
            cols = entry.get("columns") or []
            rows = entry.get("rows") or []
            row_count = entry.get("rowCount", len(rows))
            shown_rows = min(len(rows), SUMMARY_RESULTS_MAX_ROWS)
            header = (
                f"[{i}] {name} - {row_count} row(s):"
                if shown_rows >= row_count
                else f"[{i}] {name} - {row_count} row(s) total, showing the first {shown_rows}:"
            )
            blocks.append(header + "\n" + format_results_table_text(cols, rows, max_rows=SUMMARY_RESULTS_MAX_ROWS))
    results_text = "\n\n".join(blocks) if blocks else "(no databases returned anything)"
    # The trailing reminder repeats _SUMMARY_SYSTEM_INSTRUCTION's own
    # language-matching rule right here, at the very end of the actual
    # user-turn content rather than only up in the system instruction -
    # some models (observed concretely with gpt-5.3-codex on this exact
    # call) weight an instruction placed immediately before generation more
    # heavily than one stated earlier in a long system prompt, so this is
    # deliberate reinforcement/redundancy, not a duplicate to clean up.
    # When a language was confidently detected, name it directly instead
    # of (or rather, in addition to - the indirect framing stays as a
    # fallback for whatever the name-based sentence doesn't cover) asking
    # the model to infer it - a concrete target is harder for a model to
    # drift away from under pressure from foreign-language data than an
    # instruction that requires it to first correctly infer the question's
    # language and then remember to match it several paragraphs later.
    language_name = _describe_language(expected_language_code)
    named_language_sentence = (
        f" Concretely: the question above is in {language_name}, so your ENTIRE response - the "
        f"label line and every paragraph - must be written in {language_name}, in full, not "
        f"partially or with any other language mixed in."
        if language_name else ""
    )
    return (
        f"Original question: {user_question}\n\n"
        f"{sql_section}"
        f"Results gathered from each database queried to help answer it:\n\n{results_text}\n\n"
        "Reminder: write your response - the label line AND every paragraph - in the SAME "
        "LANGUAGE as the \"Original question\" above, no matter what language the database/table "
        "names or the results data shown above happen to be in." + named_language_sentence
    )


# is_label_only_response (imported from connection_router.py, shared with
# triage_all_mode_question there) detects a response that's just a leading
# label with no real paragraphs after it, so it can be retried exactly
# like a genuinely empty response, instead of silently showing the user a
# bare heading with nothing usable underneath it - see its own docstring
# for why this is POSITION-based rather than matching a specific word: the
# label is translated into the user's own question's language, so it can
# no longer be matched against a fixed English string like "Result
# Summary"/"Results Summary". "All databases" mode's own Phase C
# summarization (_SUMMARY_SYSTEM_INSTRUCTION/summarize_all_mode_results
# below) no longer uses this function at all - it moved from the free-text
# label+blank-line convention to a structured JSON response, and
# _clean_summary_response (below) validates that shape directly instead.
# Single-connection mode's own equivalent (_SINGLE_SUMMARY_SYSTEM_
# INSTRUCTION/summarize_single_connection_results, later in this file)
# still uses the original prose convention unchanged, and so still uses
# this function - via _summarize_with_retry's own default content_parser,
# see below.


def _default_content_parser(text):
    """Default `content_parser` for _summarize_with_retry (below) - single-
    connection mode's own original prose contract: a non-empty, non-
    label-only stripped string, or None. is_label_only_response runs on
    the RAW `text` (before stripping), same reasoning as everywhere else
    it's used - see its own docstring. Preserves the exact validity check
    this function always ran, before content_parser existed, for
    summarize_single_connection_results' unchanged call."""
    stripped = (text or "").strip()
    if stripped and not is_label_only_response(text or ""):
        return stripped
    return None


def _default_language_text_extractor(parsed):
    """Default `language_text_extractor` for _summarize_with_retry (below):
    `parsed` (from _default_content_parser above) already IS the text to
    run _detect_language over - single-connection mode's own prose
    contract has only ever had one string to check."""
    return parsed


def _summarize_with_retry(prompt_content, schema_block, system_instruction, provider, client, model,
                           api_key=None, tried_keys=None, using_byok=False, log_label="Summarization",
                           expected_language_code=None, content_parser=None, language_text_extractor=None,
                           invalid_content_error=None):
    """Shared bounded-retry machinery behind BOTH summarize_all_mode_results
    ("all databases" mode's Phase C, below) and summarize_single_
    connection_results (single-connection mode's own equivalent, added
    later in this file) - extracted so this retry/key-rotation policy is
    written and tested in exactly ONE place rather than duplicated
    verbatim across two callers that build different prompts/system
    instructions but need identical failure handling.

    `content_parser` and `language_text_extractor` are what let this one
    retry loop serve two callers whose notion of "valid content" is no
    longer the same shape: single-connection mode's own caller
    (summarize_single_connection_results) still needs the original
    free-text label+blank-line convention (a non-empty, non-label-only
    stripped string - see is_label_only_response), while "all databases"
    mode's own Phase C (summarize_all_mode_results, below) now needs a
    structured per-database JSON object instead (see
    _SUMMARY_SYSTEM_INSTRUCTION/_clean_summary_response). Rather than
    duplicate this whole ~140-line retry/rotation/language-check loop a
    third time for the JSON shape, both `content_parser` (raw model text ->
    parsed content, or None if invalid - replaces the old inline
    stripped-and-not-label-only check) and `language_text_extractor`
    (parsed content -> the text _detect_language should actually check,
    since the JSON shape has several separate strings, not one) default to
    closures reproducing EXACTLY the original prose behavior
    (_default_content_parser/_default_language_text_extractor, just below)
    when omitted, so summarize_single_connection_results' existing call
    needs no changes at all. `invalid_content_error` is the log_label-
    adjacent last_error text to use when `content_parser` rejects a
    response; left None, the original empty-vs-label-only-specific message
    is used (again, exactly reproducing prior behavior for the default
    caller) - a JSON-mode caller instead supplies one description covering
    every way its own parser can reject a response.

    Bounded 2-attempt retry at getting usable CONTENT back (a response
    `content_parser` rejects - see above - counts as a failed attempt,
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
    thread through a fresh call each time this fires. `log_label` only
    changes what appears in the logger.warning() calls below, so log
    lines stay distinguishable between callers.

    GENERATOR, same idiom as generate_sql_for_connection/triage_all_mode_
    question: every time the retry loop below actually rotates a key or
    waits out a transient error, this yields a fully wire-encoded NDJSON
    progress line (`json.dumps({"status": "retrying", ...}) + "\\n"`).
    This is newer than the retry/rotation POLICY itself (see the previous
    paragraph) - the policy was added first, purely server-side, with
    nothing surfaced to the client; a slow/rate-limited summarization call
    could silently sit in a multi-second TRANSLATION_RETRY_DELAY_SECONDS
    wait with the "Summarizing results…" banner frozen, indistinguishable
    from a hang. Both direct callers (summarize_all_mode_results,
    summarize_single_connection_results) forward these via `return (yield
    from _summarize_with_retry(...))`, and both routes that call THEM
    (/api/summarize-results, /api/summarize-result) forward them again the
    same way, all the way out to the client's existing generic 'retrying'
    handling - no new client-side event kind, just a new source for the
    same one. A caller with nowhere live to forward into (e.g. a unit test
    calling this directly) drains it with the same _drain_generation
    helper generate_sql_for_connection's own direct callers already use.

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
    apart). The caller uses format_llm_error_for_user() to build an honest
    message from `error` when it's a real exception, surfacing WHY
    summarization didn't produce anything rather than just leaving the
    Summary tab silently as it already was - it's the caller, not this
    function, that needs `using_byok` for that (see
    generate_sql_for_connection's docstring for the general reasoning);
    `using_byok` is accepted here only to force the key-rotation budget
    down to 1 attempt, same as there.

    Takes `prompt_content` (the plain new-user-turn text
    _build_summary_prompt/_build_single_summary_prompt built) and
    `schema_block` rather than an already-built `llm_input`, unlike
    before language verification was added: on a language-mismatch retry
    (see below) this function needs to rebuild `llm_input` itself from a
    CORRECTED prompt_content via provider.build_llm_input(), which it
    can't do starting from an opaque, already-provider-specific-shaped
    llm_input value.

    `expected_language_code` - _detect_language(user_question)'s result,
    threaded through from the caller - adds a second content-validity
    check alongside `content_parser`'s own: once content is accepted,
    `language_text_extractor(parsed)` is run through _detect_language, and
    if that doesn't match, the response is discarded exactly like invalid
    content is (consuming one of the 2 attempts), and - unlike the
    invalid-content case, which just retries with the exact same prompt -
    the next attempt's prompt gets an extra, blunt correction line naming
    the required language, since simply asking again with no change would
    likely just reproduce the same wrong-language answer. None (detection
    unavailable or too low-confidence) skips this check entirely - see
    _detect_language's own docstring.

    Returns (parsed, usage, None) on success - `parsed` is exactly what
    `content_parser` returned (a plain stripped string for the default
    prose caller, or _clean_summary_response's dict for the JSON caller),
    NOT YET wrapped in whatever presentation the caller adds on top (e.g.
    the app's "*** NO SQL ***" convention, or the "**Name:**" per-database
    tagging reconstructed by /api/summarize-results below) - that stays
    the caller's job, exactly as it always has, so it lives in exactly one
    place per caller."""
    content_parser = content_parser or _default_content_parser
    language_text_extractor = language_text_extractor or _default_language_text_extractor
    if api_key is None:
        api_key = provider.pick_api_key()
    if tried_keys is None:
        tried_keys = {api_key}
    key_pool_size = 1 if using_byok else provider.get_key_pool_size()

    last_error = None
    for attempt in range(2):
        text = None
        transient_attempt = 1
        llm_input = provider.build_llm_input([], schema_block, prompt_content)
        while True:
            try:
                text, usage = provider.call(client, model, llm_input, system_instruction)
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
                        "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                        log_label, len(tried_keys), key_pool_size, e,
                    )
                    # Told to the client before continuing - see this
                    # function's own module-level neighbor
                    # generate_sql_for_connection's identical line, and this
                    # function's docstring below on why this loop now yields
                    # at all (it didn't used to: this call had no live
                    # progress reporting even after retry/key-rotation was
                    # added to its policy).
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": len(tried_keys),
                        "maxAttempts": key_pool_size,
                        "delaySeconds": 0,
                        "rotatedKey": True,
                    }) + "\n"
                    continue

                if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                    text = None
                    break
                logger.warning(
                    "%s call failed (attempt %d/%d), retrying in %ds: %s",
                    log_label, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e,
                )
                # Told to the client before sleeping, not after - same
                # reasoning as generate_sql_for_connection's identical line.
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

        if text is None:
            # The LLM call's own retry/key-rotation budget is exhausted, or
            # it hit a non-retryable error outright - no point spending the
            # second content-validity attempt on a call that's already
            # just proven it can't succeed right now with any configured
            # key (same reasoning as triage_all_mode_question's identical
            # early break).
            break

        # content_parser runs on the RAW `text` - the default parser needs
        # it un-stripped for the same reason is_label_only_response always
        # has (see its own docstring: a "label line, then a blank line,
        # then nothing" response needs that blank line intact to be told
        # apart from a plain single-line response with no label convention
        # at all); a JSON-mode parser like _clean_summary_response just
        # ignores incidental leading/trailing whitespace itself via its own
        # json.loads.
        parsed = content_parser(text)
        if parsed is not None:
            if expected_language_code is not None:
                language_text = language_text_extractor(parsed)
                actual_language_code = _detect_language(language_text) if language_text else None
                if actual_language_code is not None and actual_language_code != expected_language_code:
                    expected_name = _describe_language(expected_language_code)
                    actual_name = _describe_language(actual_language_code)
                    logger.warning(
                        "%s came back in %s instead of the question's own %s (attempt %d/2) - discarding%s",
                        log_label, actual_name, expected_name, attempt + 1,
                        ", retrying with an explicit correction" if attempt + 1 < 2 else " (no attempts left)",
                    )
                    last_error = f"response was written in {actual_name} instead of {expected_name}"
                    # Simply retrying with the SAME prompt would likely just
                    # reproduce the same wrong-language answer, since
                    # whatever pulled the model toward actual_name (usually
                    # foreign-language data in the results) is still there -
                    # so the next attempt's prompt gets an explicit,
                    # unambiguous correction addendum on top of the
                    # reminder _build_summary_prompt/_build_single_summary_
                    # prompt already appended, naming both the mistake and
                    # the fix directly rather than repeating the same
                    # instruction that didn't work the first time.
                    prompt_content = (
                        f"{prompt_content}\n\nCORRECTION: your previous answer to this exact request was "
                        f"written in {actual_name}, which is WRONG - the question was in {expected_name}, so "
                        f"your response must be entirely in {expected_name}. Write your full response again, "
                        f"from scratch, entirely in {expected_name} this time."
                    )
                    continue
            return parsed, usage, None
        if invalid_content_error is not None:
            last_error = invalid_content_error
        else:
            stripped = (text or "").strip()
            last_error = (
                "response was only the label, or missing the label/blank-line shape, with no real content after it"
                if stripped else "empty summarization response"
            )

    logger.warning("%s failed after retry: %s", log_label, last_error)
    return None, None, last_error


def _build_all_mode_schema_block(database_results, user_identity):
    """Resolves and renders each unique in-scope database referenced in
    `database_results` (one entry per statement result/note/failure - see
    _build_summary_prompt's own docstring for the exact shape) into a
    single schema_block for Phase C's LLM call. Gap 4 of "Turn History
    Handling in Datalect": previously Phase C always ran with
    schema_block="" - literally no schema at all - even though the
    design's own LLM-3 input spec calls for "detailed database schema for
    all in-scope databases".

    Each unique (kind, id) pair is resolved via resolve_descriptor_by_
    reference - the same {kind, id}-only trust boundary translate_routes.py/
    execute_routes.py already use everywhere else (never raw descriptors/
    credentials sent by the client) - then its schema is fetched via the
    same cached get_database_schema() Phase B/single-connection mode
    already use, so this costs nothing beyond a cache lookup for a
    connection Phase B just fetched moments earlier in this same turn. A
    reference that no longer resolves (a preset removed, or a custom
    connection deleted, in the moments since triage/Phase B ran) is
    silently skipped, same leniency resolve_in_scope_descriptors already
    applies elsewhere - one missing schema shouldn't block summarizing the
    other databases that did resolve.

    `user_identity` falsy (a caller with no real session to resolve
    against - e.g. a unit test exercising summarize_all_mode_results()
    directly) returns "" - the exact schema-less prompt this call always
    sent before Gap 4, rather than raising."""
    if not user_identity:
        return ""
    names_by_key = {}
    order = []
    for entry in (database_results or []):
        kind = entry.get("kind")
        ref_id = entry.get("id")
        if not kind or ref_id is None:
            continue
        key = (kind, ref_id)
        if key not in names_by_key:
            names_by_key[key] = entry.get("name") or "Unknown database"
            order.append(key)

    blocks = []
    for key in order:
        kind, ref_id = key
        descriptor, _resolved_name = resolve_descriptor_by_reference(kind, ref_id, user_identity)
        if descriptor is None:
            continue
        schema = get_database_schema(descriptor, user_identity)
        blocks.append(f"{names_by_key[key]}:\n{schema}")
    if not blocks:
        return ""
    return "Database schema for each database queried:\n\n" + "\n\n".join(blocks) + "\n\n"


def _clean_summary_response(raw_text, num_databases):
    """Parses Phase C's structured JSON response (see
    _SUMMARY_SYSTEM_INSTRUCTION) into
      {"label": <non-empty str>, "per_database": {int_index: <non-empty str>, ...},
       "cross_database": <non-empty str> | None}
    or None (unparseable, or missing/incomplete required content) - the
    caller's bounded retry (_summarize_with_retry, via the content_parser
    it's given) treats None exactly like an empty/invalid response used to
    be treated before Phase C moved off free-text prose.

    Mirrors connection_router.py's _parse_triage_response/_clean_database_
    prompts shape closely (JSON via strip_markdown_fence + json.loads, a
    dict keyed by string-int index) but is intentionally STRICTER than
    that sibling: _clean_database_prompts tolerates a missing per-
    connection rewrite for triage (Phase B just falls back to the user's
    own original question for that one connection), but there is no
    equivalent fallback text for a missing per-database summary paragraph
    here - nothing sensible to show the user in its place - so unlike
    triage, a gap for ANY index in range(num_databases) invalidates the
    WHOLE response, giving the bounded retry loop another attempt instead
    of silently showing a summary with one database's paragraph missing.

    `num_databases` is the caller's own len(database_results) - see
    _build_summary_prompt's docstring for why its "[i]" indices already
    match this same 0-based numbering.

    "cross_database" is optional (see _SUMMARY_SYSTEM_INSTRUCTION - only
    meant to be written when the question genuinely asks for something
    spanning multiple databases): a missing, non-string, or blank value
    simply becomes None, never a reason to invalidate the rest of a
    response that otherwise checks out."""
    if not raw_text:
        return None
    cleaned = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    label = parsed.get("label")
    if not (isinstance(label, str) and label.strip()):
        return None
    label = label.strip()

    raw_per_database = parsed.get("per_database")
    if not isinstance(raw_per_database, dict):
        return None
    per_database = {}
    for key, value in raw_per_database.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str) and value.strip():
            per_database[index] = value.strip()
    for index in range(num_databases):
        if index not in per_database:
            return None

    cross_database = parsed.get("cross_database")
    cross_database = cross_database.strip() if isinstance(cross_database, str) and cross_database.strip() else None

    return {"label": label, "per_database": per_database, "cross_database": cross_database}


def _make_summary_content_parser(num_databases):
    """Binds `num_databases` into a content_parser closure for
    _summarize_with_retry - see _clean_summary_response above for the
    actual validation. A small wrapper rather than a lambda so it's
    consistent with, and greppable alongside, _default_content_parser."""
    def _parser(text):
        return _clean_summary_response(text, num_databases)
    return _parser


def _summary_language_text(parsed):
    """language_text_extractor for Phase C's JSON-mode content_parser (see
    _make_summary_content_parser/_clean_summary_response above): since
    `parsed` is now a dict of several separate strings rather than one, this
    concatenates every actual paragraph the model wrote - the label, each
    per-database paragraph, and the cross-database paragraph if present -
    into one blob for _detect_language to run over. Mirrors single-
    connection mode's own _default_language_text_extractor, which simply
    returns its one parsed prose string directly - adapted here to a
    parsed shape with more than one."""
    parts = [parsed.get("label") or ""]
    parts.extend((parsed.get("per_database") or {}).values())
    cross_database = parsed.get("cross_database")
    if cross_database:
        parts.append(cross_database)
    return "\n\n".join(part for part in parts if part)


def summarize_all_mode_results(user_question, database_results, provider, client, model, user_identity=None,
                                api_key=None, tried_keys=None, using_byok=False):
    """"All databases" mode's Phase C - see the section comment above for
    the fuller picture of when/why this runs. A brief, structured answer
    to `user_question` - one short paragraph per database, over the
    ACTUAL data gathered from every database Phase B was routed to,
    rather than the routing message triage produced before any of it was
    known, plus an optional separate cross-database paragraph (see
    _SUMMARY_SYSTEM_INSTRUCTION for the exact JSON shape asked for).

    All the retry/key-rotation policy (bounded 2-attempt content-validity
    retry, nested transient-error/key-rotation retry) now lives in the
    shared _summarize_with_retry() above - see its docstring for the full
    reasoning. This function's own job is just building the Phase-C-
    specific prompt/schema_block/system instruction and JSON parser/
    language-extractor, and delegating to it.

    `user_identity` (new - Gap 4) is what lets this build a real
    schema_block via _build_all_mode_schema_block above instead of the
    permanently-empty "" every call used to pass; defaults to None (and
    therefore an empty schema_block, unchanged from before Gap 4) for a
    caller with no real session to resolve connections against.

    GENERATOR (see _summarize_with_retry's own docstring): `yield from`s
    that function directly, so its live 'retrying' progress lines pass
    straight through unchanged - this function adds none of its own, it
    just builds the Phase-C-specific prompt/schema_block ahead of
    delegating.

    Returns (parsed, usage, error) on success/failure - `parsed`, when not
    None, is exactly _clean_summary_response's own returned shape
    ({"label", "per_database", "cross_database"} - see its docstring),
    never the model's raw JSON text. See _summarize_with_retry's docstring
    for the exact meaning of `error` on failure."""
    expected_language_code = _detect_language(user_question)
    prompt_content = _build_summary_prompt(user_question, database_results, expected_language_code)
    schema_block = _build_all_mode_schema_block(database_results, user_identity)
    num_databases = len(database_results or [])
    return (yield from _summarize_with_retry(
        prompt_content, schema_block, _SUMMARY_SYSTEM_INSTRUCTION, provider, client, model,
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
        log_label="Phase C summarization", expected_language_code=expected_language_code,
        content_parser=_make_summary_content_parser(num_databases),
        language_text_extractor=_summary_language_text,
        invalid_content_error="response was not valid, complete per-database summary JSON",
    ))


@translate_bp.route('/api/summarize-results', methods=['POST'])
# See concurrency_guard.py's own module docstring (TRANSLATE_GUARD) and
# rate_limiter.py's own docstring (RATE_LIMIT_TRANSLATE/
# _translate_family_rate_limit) for why this route draws from the SAME
# pooled guard/rate limit as /api/translate and summarize_result() below,
# rather than a separate one of its own: an LLM call to generate SQL and
# an LLM call to summarize results are the same kind of work as far as
# this app's admission control is concerned.
@summarize_rate_limit
def summarize_results():
    """See the "All databases" mode, Phase C section comment above for the
    full picture. Called by the client exactly once per "route" outcome
    turn, only after /api/execute has actually run every database Phase B
    selected (never for a single-connection session - client.js only ever
    calls this from executeSql()'s router_route handling).

    Streams newline-delimited JSON (NDJSON), same contract as /api/
    translate (see that route's module docstring) and for the same
    reason: summarize_all_mode_results()'s own retry loop
    (_summarize_with_retry, see its docstring) can now genuinely take
    several real seconds - a transient-error wait, possibly a key
    rotation first - and previously nothing reached the client during
    that wait at all; the "Summarizing results…" banner just sat there
    frozen, indistinguishable from a hang. Zero or more
    {"status": "retrying", "attempt": <next attempt #>, "maxAttempts": N,
     "delaySeconds": <float>, "rotatedKey": <bool>} lines are emitted live
    as that retry loop runs - client.js needs no changes to show these,
    since 'retrying' is already handled generically by its existing
    dispatcher, regardless of which server-side call produced the line -
    followed by exactly one terminal line:
      {"status": "done", "success": true, "summary": "...",
       "database_summaries": [{"kind", "id", "name", "text"}, ...],
       "cross_database_summary": "..." | null}
      or, on failure (retry/rotation budget exhausted, or 2 consecutive
      content-invalid responses):
      {"status": "done", "success": false, "error": "..."}
    "summary" stays the single joined, "**Name:** paragraph" string this
    route has always returned - untouched consumers (history persistence,
    the Summary tab's existing rendering) keep working unmodified - but it
    is now RECONSTRUCTED here from summarize_all_mode_results' own
    structured `parsed` return value rather than trusted verbatim from the
    model: Phase C's own response is JSON now (see _SUMMARY_SYSTEM_
    INSTRUCTION/_clean_summary_response), so the bold "**Name:**" lead-in
    for each paragraph is built from `database_results`' own real name,
    zipped back against `parsed["per_database"]` by the same 0-based index
    _build_summary_prompt's "[i]" labels used - a reliability improvement
    over the old free-text convention, which trusted the model to copy a
    database's name into its own prose verbatim. Grouped by (kind, id)
    before that heading is built, since `database_results` (and so
    `per_database`) has one entry per STATEMENT RESULT, not per database -
    a database whose SQL had multiple statements gets multiple indices,
    all sharing the same identity - so "database_summaries" really is one
    entry PER DATABASE (as its own shape below already promised), each
    "text" combining every one of that database's own resultset
    paragraphs (newline-joined, so they render as sub-paragraphs nested
    under one heading rather than that heading repeating once per
    resultset), not one entry per resultset. "database_summaries" and
    "cross_database_summary" are purely ADDITIVE new fields alongside that
    unchanged "summary" string (Chunk 1's own sql_blocks precedent) - the
    per-database split callers need to record separate per-database turns
    later, and the cross-database paragraph split out on its own, distinct
    from any one database's paragraph.
    The two early-validation returns below (missing API key, missing
    prompt/database_results) happen before any of this and keep their
    real plain-JSON 400 responses, exactly as /api/translate's own two
    early-validation cases do - nothing has streamed yet at that point,
    so there's no reason to pay the NDJSON/chunked-response cost for a
    request that never even reaches the retry loop."""
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

    def stream_summarize_results():
        start_time = time.perf_counter()
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            parsed, usage, error = yield from summarize_all_mode_results(
                prompt, database_results, provider, client, llm_model, user_identity=user_identity,
                api_key=api_key, using_byok=bool(byok_key),
            )
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()
        duration = round(1000 * (time.perf_counter() - start_time))

        if parsed is None:
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
            # Logged the same way a successful Phase C call is (see below) -
            # "All Pre-Configured Datasets"/"All Pre-Configured Datasets", 0 duration-attributed tokens
            # (a total failure never has a usable response to report token
            # counts from - see summarize_all_mode_results'/_summarize_with_
            # retry's own docstrings), and the sql_command column holding a
            # TRANSLATION_ERROR(...) sentinel rather than real SQL, since
            # there is none - same overloaded-column convention this app
            # already uses for "*** NO SQL ***" text.
            record_all_databases_triage(
                user_identity, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model, duration,
                0, 0, 0, 0, 0,
            )
            yield json.dumps({'status': 'done', 'success': False, 'error': error_message}) + "\n"
            return

        # Zip parsed["per_database"] (keyed by the same 0-based index
        # _build_summary_prompt's own "[i]" results-block labels used)
        # back against `database_results` by position, so each paragraph
        # is reunited with its database's real kind/id/name - see this
        # route's own docstring above for why this reconstruction (rather
        # than trusting the model's own name copy) is now a reliability
        # improvement, not just a format change.
        #
        # `database_results` has one entry per STATEMENT RESULT/note/
        # failure (see _build_summary_prompt's own docstring), not one per
        # database - a database whose own SQL had multiple statements
        # contributes multiple entries here, all sharing the same (kind,
        # id, name), and the model likewise wrote one paragraph per index
        # (still asked to reason about just "that index's" own results,
        # not to merge across indices itself - the wording of each
        # paragraph is unchanged by this). Grouped here, by (kind, id), so
        # the rendered summary shows ONE heading per actual database, with
        # each of its own resultsets' paragraphs nested underneath as
        # sub-paragraphs (joined by a single '\n' - a soft line break the
        # Summary tab's `white-space: pre-wrap` renders without a blank
        # line, distinct from the blank-line-separated '\n\n' between
        # different databases below) - instead of the same database's name
        # repeating as a separate, flat top-level paragraph once per
        # resultset.
        per_database = parsed["per_database"]
        grouped_by_database = {}
        database_order = []
        for i, entry in enumerate(database_results):
            key = (entry.get("kind"), entry.get("id"))
            if key not in grouped_by_database:
                grouped_by_database[key] = {
                    "kind": entry.get("kind"), "id": entry.get("id"),
                    "name": entry.get("name") or "Unknown database",
                    "paragraphs": [],
                }
                database_order.append(key)
            grouped_by_database[key]["paragraphs"].append(per_database.get(i, ""))

        database_summaries = []
        summary_paragraphs = []
        for key in database_order:
            group = grouped_by_database[key]
            # One combined block of text per database - a single resultset
            # (the common case) looks byte-identical to before this
            # change; 2+ resultsets get their own paragraphs stacked on
            # separate lines under the one heading instead of repeating it.
            combined_text = "\n".join(p for p in group["paragraphs"] if p)
            database_summaries.append({
                "kind": group["kind"], "id": group["id"], "name": group["name"], "text": combined_text,
            })
            summary_paragraphs.append(f"**{group['name']}:** {combined_text}")

        cross_database_summary = parsed.get("cross_database")
        if cross_database_summary:
            summary_paragraphs.append(cross_database_summary)

        summary_text = "*** NO SQL *** " + parsed["label"] + "\n\n" + "\n\n".join(summary_paragraphs)
        usage_dict = usage or {}
        # Logged the same way Phase A's own triage call is (see
        # record_all_databases_triage's docstring) - "All Pre-Configured Datasets"/
        # "All Pre-Configured Datasets" rather than any one real connection, since this call
        # is likewise never "about" just one specific database.
        record_all_databases_triage(
            user_identity, prompt, summary_text, llm_model, duration,
            usage_dict.get("input_tokens", 0), usage_dict.get("output_tokens", 0),
            usage_dict.get("total_tokens", 0), usage_dict.get("thinking_tokens", 0),
            usage_dict.get("cached_content_tokens", 0),
        )

        yield json.dumps({
            'status': 'done', 'success': True, 'summary': summary_text,
            'database_summaries': database_summaries,
            'cross_database_summary': cross_database_summary,
        }) + "\n"

    # See concurrency_guard.py's own module docstring - TRANSLATE_GUARD,
    # the SAME guard /api/translate itself uses (pooled, not a dedicated
    # one for this route) - acquired HERE, right before actually
    # streaming, same placement/reasoning as translate_query()'s own
    # TRANSLATE_GUARD.try_acquire() below: nothing above this point does
    # any real work, so nothing before it should compete for a scarce
    # slot. {"success": False, "error": ...} matches this route's own
    # early-validation failures above.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many results-summarization requests right now. Please try again in a few seconds.',
        })

    def _stream_summarize_results_with_guard_release():
        # Holds the guard slot open for stream_summarize_results()'s ENTIRE
        # lifetime - see translate_query()'s own
        # _translation_stream_with_guard_release for the identical
        # reasoning (the generator, not this view function, is what does
        # the real work, driven lazily by Flask/gunicorn as the response
        # streams out).
        try:
            yield from stream_summarize_results()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_summarize_results_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)


# --- Single-connection mode's own post-execution results summarization --
#
# The single-connection equivalent of "all databases" mode's Phase C above
# - see that section's own comment for the general shape/reasoning this
# mirrors. Once a single-connection turn's generated SQL has actually been
# executed (client.js's executeSql()), a SEPARATE LLM call is made with the
# original question, the schema, the SQL that ran, and up to
# SUMMARY_RESULTS_MAX_ROWS rows of what it returned (explicit product
# decision: this call exists to reason over the current turn's own result
# set, exactly like Phase C now does too - see _build_summary_prompt's own
# docstring for why that's a separate, far more generous cap than
# HISTORY_RESULT_MAX_ROWS, and SUMMARY_RESULTS_MAX_ROWS's own definition
# comment above for why a real ceiling is needed at all now that every row
# is actually sent to this call) - and asked to both answer the question and
# call out actionable insight, not just
# restate the data. Skipped entirely by the client when the LLM instead
# answered directly via the "*** NO SQL ***" convention (nothing was
# executed, so nothing to summarize). Same best-effort posture as Phase C:
# any failure here is reported back as {"success": false}, never a hard
# error, so the client just leaves the Summary tab out rather than
# treating a nice-to-have's failure as a turn failure.
#
# This call also decides, "ride-along" with the summary (one LLM call,
# not a second round trip), whether the results are worth showing as a
# chart instead of only a table - see _SINGLE_SUMMARY_SYSTEM_INSTRUCTION's
# own "visualization" paragraph below and _clean_single_summary_response.
# The model's own judgment about WHETHER charting is even possible is
# never trusted on its own: _pick_chartable_result decides that server-
# side, from the real executed statement_results, before the model is
# ever asked anything - a multi-statement result, a single-row result, or
# a result with no numeric column at all is never offered a chart no
# matter what the model might otherwise claim, and its own x_column/
# y_columns/series_column choices are re-validated against the real
# columns (and, for y_columns, the real row VALUES - see
# _column_looks_numeric) rather than trusted on faith, the same "never
# blindly trust LLM output" posture this app already applies to generated
# SQL (see translate_query()'s own docstring).

_CHART_MIN_ROWS = 2


def _column_looks_numeric(rows, column, sample_size=200):
    """True when a solid majority of `column`'s own non-null sampled values
    (across up to `sample_size` of `rows`) are real numbers. Excludes bool
    (a Python bool is technically an int subclass, but a true/false column
    is categorical, not something to plot on a value axis) and excludes
    numeric-LOOKING strings on purpose - this app never asks the client to
    stringify numbers before sending results here (see
    _build_single_summary_prompt's docstring on `statement_results`' own
    shape: real JSON values, not pre-stringified), so a string value here
    is real text, not a number rendered as text.

    Used twice: to build the prompt's own "Chartable columns" hints (see
    _describe_chartable_columns) and, independently, to re-validate the
    model's actual y_columns choice against the real data rather than
    trusting its guess from the column name alone (e.g. a column named
    "id" is numeric but rarely a sensible y-axis choice on its own - still
    allowed here, since "sensible" is a judgment call left to the model,
    but a column named "amount" that's actually stored as text is not a
    valid choice at all, and this catches that).

    Empty/all-null sampled data is treated as NOT numeric (there's nothing
    to plot), not as a vacuous pass. A solid-majority (not unanimous)
    threshold tolerates the occasional stray null/outlier without
    disqualifying an otherwise-numeric column."""
    seen = 0
    numeric = 0
    for row in (rows or [])[:sample_size]:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None:
            continue
        seen += 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric += 1
    if seen == 0:
        return False
    return (numeric / seen) >= 0.9


def _pick_chartable_result(statement_results):
    """Returns the single statement_results entry (see _build_single_
    summary_prompt's own docstring for the shape) eligible to be charted
    this turn, or None when charting isn't offered at all. Deliberately
    conservative - this decides eligibility server-side from the real
    executed results, rather than leaving it to the model's own judgment:
      - Exactly ONE statement_results entry must have real tabular
        columns/rows (not a note, not an error) - a multi-statement script
        with more than one real result set is ambiguous about which one
        to chart, so charting is skipped entirely rather than guessing.
      - That entry must have at least _CHART_MIN_ROWS rows - a single-row
        result has nothing to compare/trend, so a chart adds nothing over
        a table.
      - At least one of its columns must look numeric (_column_looks_
        numeric) - with no numeric column at all there is nothing to plot
        on a value axis.
    Returns the qualifying entry itself (not just True/False) so callers
    have its real columns/rows on hand both for building the prompt's own
    "Chartable columns" list and for later validating the model's column
    choices against them (see _clean_single_summary_response)."""
    tabular = [
        entry for entry in (statement_results or [])
        if isinstance(entry, dict) and not entry.get("error") and not entry.get("note") and entry.get("columns")
    ]
    if len(tabular) != 1:
        return None
    entry = tabular[0]
    rows = entry.get("rows") or []
    if len(rows) < _CHART_MIN_ROWS:
        return None
    columns = entry.get("columns") or []
    if not any(_column_looks_numeric(rows, col) for col in columns):
        return None
    return entry


def _describe_chartable_columns(chartable_entry):
    """Renders `chartable_entry`'s own columns (see _pick_chartable_result)
    into the "Chartable columns" prompt section _SINGLE_SUMMARY_SYSTEM_
    INSTRUCTION's "visualization" paragraph references - each column
    tagged (numeric) or (text) via _column_looks_numeric, so the model can
    tell which columns are even eligible for x_column/y_columns/
    series_column without having to infer types from a raw data dump
    itself. None (nothing chartable this turn - see _pick_chartable_
    result) renders the explicit "no chartable columns" sentence instead,
    so the prompt never leaves the model to guess why "visualization" must
    be null."""
    if chartable_entry is None:
        return "Chartable columns: none available for this turn - \"visualization\" MUST be null.\n"
    rows = chartable_entry.get("rows") or []
    columns = chartable_entry.get("columns") or []
    described = ", ".join(
        f"{col} ({'numeric' if _column_looks_numeric(rows, col) else 'text'})" for col in columns
    )
    return f"Chartable columns (use these EXACT names only): {described}\n"


_SINGLE_SUMMARY_SYSTEM_INSTRUCTION = (
    "You previously helped translate a user's natural-language question into a real SQL query - possibly "
    "more than one statement - and it has now actually been run against the database. You will be given "
    "the user's ORIGINAL question, the database schema, the SQL that was executed, and the outcome of "
    "each statement that ran: exactly one of its actual result rows, a note that it returned nothing "
    "useful, or an error explaining that it failed to execute.\n"
    "CRITICAL, before anything else: every string you write - the label line AND every paragraph of "
    "\"summary\" - MUST be written in the SAME LANGUAGE as the user's original question, never the "
    "language of the schema/table names or of the results data you're given, and never any other "
    "language. This applies to every single sentence you write, not just the label.\n"
    "Respond with ONLY a single JSON object - no markdown code fences, no other text before or after it - "
    "shaped exactly like this: {\"summary\": \"...\", \"visualization\": null or {...}}\n"
    "\"summary\" has two parts. FIRST, a single label line: a short (one to two word) section-heading "
    "label meaning \"Results Summary\" - in English this label is literally the phrase \"Results Summary\", "
    "but you must instead write it TRANSLATED into the SAME LANGUAGE as the user's original question, with "
    "nothing else on that line, followed by a blank line. SECOND, immediately after that blank line, your "
    "real, substantive answer, ALSO written in that same language. Example of the full shape, if the "
    "question was in English: \"Results Summary\\n\\nRevenue is up 12% quarter over quarter, driven mostly "
    "by the Enterprise segment - worth digging into why SMB slipped.\". Never stop after the label - the "
    "label by itself, with no paragraphs following it, is not a valid \"summary\"; the label is a UI "
    "section heading prepended to your answer, not a substitute for writing one. The label itself is "
    "plain text with no markdown emphasis of your own around it, and \"summary\" as a whole must be plain "
    "text only - no SQL, no markdown tables, no code fences, no bullet points, no other headings.\n"
    "Directly answer the user's original question using the actual result rows, and go further: call out "
    "whatever is genuinely notable in the data (trends, outliers, concentrations, anything surprising) and "
    "derive concrete, actionable insight or next steps the user could reasonably take away from these "
    "SPECIFIC results - not generic advice unrelated to what the data actually shows. Keep it concise - a "
    "few short paragraphs - even if the result set is large: this is a summary with insight, not a report. "
    "If the results are empty or don't actually answer the question, say so plainly rather than inventing "
    "an answer. If a statement instead failed with an error, don't just report that it failed - briefly "
    "explain, in plain language, what the error suggests actually went wrong (e.g. a permissions problem, "
    "a timeout, an ambiguous or unsupported request) and, if it's apparent from the error text, what could "
    "fix it, so the user understands the failure instead of only knowing that one occurred. When some "
    "statements succeeded and others failed, address both: summarize what the successful ones show, and "
    "explain the failure(s) alongside that, rather than covering only one or the other.\n"
    "\"visualization\" decides whether the results are ALSO shown as a chart instead of only a table. Set "
    "it to JSON null whenever a chart wouldn't add anything - a single scalar/lookup answer, mostly "
    "textual data, or whenever the \"Chartable columns\" list given to you below says none are available "
    "(charting isn't possible for this result set no matter what the question asks, in that case). When a "
    "real \"Chartable columns\" list IS given, and the user's question is naturally about comparing, "
    "trending, or ranking numeric values (e.g. \"sales by month\", \"top products by revenue\", \"how has "
    "X changed over time\"), set \"visualization\" to an object shaped exactly like this: {\"chart_type\": "
    "\"bar\" or \"line\" or \"scatter\", \"x_column\": \"<one column name>\", \"y_columns\": [\"<one or "
    "more column names>\"], \"series_column\": \"<one column name>\" or null}. Use \"bar\" to compare "
    "values across categories, \"line\" when x_column is a time/sequence-like column and the question is "
    "about a trend over it, \"scatter\" to relate two numeric columns to each other. \"x_column\" is the "
    "column to place along the other axis from the values being measured; \"y_columns\" are the numeric "
    "column(s) actually being measured/compared - only ever pick columns explicitly marked \"(numeric)\" "
    "in \"Chartable columns\" below, never a column marked \"(text)\". \"series_column\" optionally splits "
    "the chart into multiple series/groups (e.g. one line per region) - set it to null unless the data "
    "genuinely has a separate grouping column, distinct from x_column, that the question calls for "
    "breaking out. Every column name you write anywhere inside \"visualization\" must be copied EXACTLY "
    "(case-sensitive) from the \"Chartable columns\" list below - never invent, translate, or abbreviate a "
    "column name. If you are ever unsure whether a chart genuinely helps here, prefer null - a plain table "
    "is always an acceptable, safe default, and there is no penalty for choosing it.\n"
    "One final reminder, since it's the single most important rule above: the language of \"summary\" "
    "must match the user's original question, not the language of the schema/SQL/data.\n"
)


def _build_single_summary_prompt(user_question, sql, statement_results, chartable_entry, expected_language_code=None):
    """Renders `statement_results` - client-submitted [{"columns", "rows",
    "rowCount"} | {"note"} | {"error"}, ...], one entry per SQL statement
    /api/execute actually ran for this turn - into one labeled text block
    per statement for the single-connection summarization call above.

    Real result rows are capped at SUMMARY_RESULTS_MAX_ROWS, the same
    abuse/cost-protection cap _build_summary_prompt's own docstring
    explains above (a SEPARATE, far more generous constant than
    HISTORY_RESULT_MAX_ROWS - see SUMMARY_RESULTS_MAX_ROWS's own
    definition comment). When a statement's results are actually
    truncated, the header names both the real total row count and how
    many are shown, so the model isn't misled into thinking it saw
    everything.

    `chartable_entry` - _pick_chartable_result(statement_results)'s own
    return value, computed once by the caller and threaded through here
    (rather than recomputed) so the "Chartable columns" section below
    always describes the exact same entry _clean_single_summary_response
    will later validate the model's "visualization" choice against - see
    _describe_chartable_columns for the rendering itself.

    `expected_language_code` - see _build_summary_prompt's own docstring
    for what this is and why it's threaded through from the caller rather
    than detected here."""
    blocks = []
    for i, entry in enumerate(statement_results or []):
        error = entry.get("error")
        note = entry.get("note")
        if error:
            blocks.append(f"Query Result {i + 1}: query failed - {error}")
        elif note:
            blocks.append(f"Query Result {i + 1}: {note}")
        else:
            cols = entry.get("columns") or []
            rows = entry.get("rows") or []
            row_count = entry.get("rowCount", len(rows))
            shown_rows = min(len(rows), SUMMARY_RESULTS_MAX_ROWS)
            header = (
                f"Query Result {i + 1} - {row_count} row(s):"
                if shown_rows >= row_count
                else f"Query Result {i + 1} - {row_count} row(s) total, showing the first {shown_rows}:"
            )
            blocks.append(header + "\n" + format_results_table_text(cols, rows, max_rows=SUMMARY_RESULTS_MAX_ROWS))
    results_text = "\n\n".join(blocks) if blocks else "(no rows returned)"
    # Same reinforcement-at-the-end rationale as _build_summary_prompt's own
    # trailing reminder above - see that function's comment, and see its
    # named_language_sentence for why a confidently-detected language gets
    # named concretely here too.
    language_name = _describe_language(expected_language_code)
    named_language_sentence = (
        f" Concretely: the question above is in {language_name}, so your ENTIRE response - the "
        f"label line and every paragraph - must be written in {language_name}, in full, not "
        f"partially or with any other language mixed in."
        if language_name else ""
    )
    return (
        f"Original question: {user_question}\n\n"
        f"SQL executed:\n{sql}\n\n"
        f"Results:\n\n{results_text}\n\n"
        f"{_describe_chartable_columns(chartable_entry)}\n"
        "Reminder: write your response - the label line AND every paragraph of \"summary\" - in "
        "the SAME LANGUAGE as the \"Original question\" above, no matter what language the schema, "
        "SQL, or results data shown above happen to be in." + named_language_sentence
    )


def _clean_visualization(raw, chartable_entry):
    """Validates the model's own "visualization" value (see
    _SINGLE_SUMMARY_SYSTEM_INSTRUCTION's own paragraph on it) against the
    REAL executed result this turn - `chartable_entry`, the exact same
    _pick_chartable_result(...) value _describe_chartable_columns rendered
    into the prompt the model actually saw. Returns a cleaned
      {"chart_type": "bar"|"line"|"scatter", "x_column": <str>,
       "y_columns": [<str>, ...], "series_column": <str>|None}
    or None (meaning: show a table, not a chart) - never raises, and a
    None return here is never treated as a parse failure by
    _clean_single_summary_response (unlike a genuinely malformed
    "summary") since a table is always an acceptable, valid outcome.

    `chartable_entry` being None (charting wasn't even offered this turn -
    see _pick_chartable_result) forces None regardless of what `raw` says,
    the same "never trust the model's own judgment about eligibility"
    posture _pick_chartable_result's own docstring describes - the model
    was told there were no chartable columns, so anything else it might
    have written for "visualization" anyway is simply ignored, not treated
    as a reason to fail the whole response.

    Otherwise: `raw` must be a dict; "chart_type" must be one of the three
    values the prompt actually offers (deliberately NOT "pie" - see this
    feature's own design notes on why that was left out); "x_column" must
    name one of `chartable_entry`'s real columns; "y_columns" must be a
    non-empty list of real column names, each independently re-verified
    NUMERIC via _column_looks_numeric against the real row data (not just
    "a real column name" - the model was already told which columns are
    numeric, but its choice is re-checked here rather than trusted, same
    as every other LLM output this app validates before acting on it) and
    deduplicated, excluding x_column itself; a "series_column" is kept
    only when it's also a real column, distinct from x_column. Any
    structural problem with "x_column" or an empty "y_columns" after
    filtering invalidates the whole visualization (returns None, falls
    back to table) rather than partially rendering something the model
    didn't actually intend."""
    if chartable_entry is None or not isinstance(raw, dict):
        return None
    chart_type = raw.get("chart_type")
    if chart_type not in ("bar", "line", "scatter"):
        return None
    columns = chartable_entry.get("columns") or []
    rows = chartable_entry.get("rows") or []
    column_set = set(columns)

    x_column = raw.get("x_column")
    if not (isinstance(x_column, str) and x_column in column_set):
        return None

    raw_y_columns = raw.get("y_columns")
    if not isinstance(raw_y_columns, list):
        return None
    y_columns = []
    for y in raw_y_columns:
        if (
            isinstance(y, str) and y in column_set and y != x_column
            and y not in y_columns and _column_looks_numeric(rows, y)
        ):
            y_columns.append(y)
    if not y_columns:
        return None

    series_column = raw.get("series_column")
    if not (isinstance(series_column, str) and series_column in column_set and series_column != x_column):
        series_column = None

    return {
        "chart_type": chart_type, "x_column": x_column,
        "y_columns": y_columns, "series_column": series_column,
    }


def _clean_single_summary_response(raw_text, chartable_entry):
    """Parses the single-connection summarization call's structured JSON
    response (see _SINGLE_SUMMARY_SYSTEM_INSTRUCTION) into
      {"summary": <non-empty str>, "visualization": <_clean_visualization's
       shape> | None}
    or None (unparseable, or "summary" itself is missing/invalid - the
    caller's bounded retry, via _summarize_with_retry's content_parser,
    treats None exactly like an empty/invalid response always was before
    this call moved off free-text prose). Mirrors _clean_summary_response's
    JSON-via-strip_markdown_fence-then-json.loads shape closely - see that
    function's own docstring - adapted to this call's own "summary" +
    "visualization" envelope instead of Phase C's per-database one.

    "summary" is validated exactly like the old free-text contract
    (_default_content_parser) always was: a non-empty, non-label-only
    stripped string. A malformed/missing "summary" invalidates the WHOLE
    response (returns None, giving the bounded retry another attempt) -
    same as a missing per-database paragraph does for Phase C - since
    there's nothing sensible to show in its place. "visualization", by
    contrast, is validated leniently: _clean_visualization returning None
    (a table) is always a fine, valid outcome, never a reason to retry -
    only "summary" itself failing validation is."""
    if not raw_text:
        return None
    cleaned = strip_markdown_fence(raw_text)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    summary = parsed.get("summary")
    if not (isinstance(summary, str) and summary.strip() and not is_label_only_response(summary)):
        return None
    summary = summary.strip()

    visualization = _clean_visualization(parsed.get("visualization"), chartable_entry)
    return {"summary": summary, "visualization": visualization}


def _make_single_summary_content_parser(chartable_entry):
    """Binds `chartable_entry` into a content_parser closure for
    _summarize_with_retry - see _clean_single_summary_response above for
    the actual validation. A small wrapper rather than a lambda so it's
    consistent with, and greppable alongside, _make_summary_content_parser
    (Phase C's own equivalent binder)."""
    def _parser(text):
        return _clean_single_summary_response(text, chartable_entry)
    return _parser


def _single_summary_language_text(parsed):
    """language_text_extractor for this call's JSON-mode content_parser -
    mirrors Phase C's own _summary_language_text, adapted to this call's
    "summary" + "visualization" shape: only "summary" is ever prose worth
    running _detect_language over ("visualization" is column names/enum
    values, not natural language)."""
    return parsed.get("summary") or ""


def summarize_single_connection_results(user_question, schema, sql, statement_results, provider, client, model,
                                         api_key=None, tried_keys=None, using_byok=False):
    """Single-connection mode's equivalent of summarize_all_mode_results
    above - see this file's "Single-connection mode's own post-execution
    results summarization" section comment for the fuller picture, and
    _summarize_with_retry's docstring for the shared retry/key-rotation
    policy both this and summarize_all_mode_results now go through.

    Unlike Phase C (which has no single SQL statement to point to - it
    summarizes across possibly several databases, each with its own
    schema and its own generated SQL - see _build_all_mode_schema_block/
    _build_summary_prompt's SQL section), this call has a real schema and
    a real, already-executed SQL statement for exactly one connection,
    both of which are given to the model: the schema via build_llm_input's
    own schema_block parameter (same cache-friendly placement
    translate_query()'s single-connection path already uses - schema
    ahead of the (empty, here) history and the new prompt), and the SQL/
    results via _build_single_summary_prompt.

    Also decides, ride-along with the summary (see this file's "Single-
    connection mode's own post-execution results summarization" section
    comment), whether the results are chartable - _pick_chartable_result
    computed once here and threaded into both the prompt
    (_describe_chartable_columns, via _build_single_summary_prompt) and
    the response validation (_clean_visualization, via
    _make_single_summary_content_parser), so the two can never disagree
    about which columns were actually on offer.

    GENERATOR (see _summarize_with_retry's own docstring): `yield from`s
    that function directly, so its live 'retrying' progress lines pass
    straight through unchanged - this function adds none of its own.

    Returns (parsed, usage, error) - `parsed` is exactly
    _clean_single_summary_response's own {"summary", "visualization"}
    dict, or None on failure (see _summarize_with_retry's docstring for
    the exact meaning of `usage`/`error` in that case) - NOT yet unwrapped
    into a plain summary string; that stays the caller's job (see
    stream_summarize_result below), exactly the same "parsed, not
    presentation-wrapped" contract summarize_all_mode_results' own
    {"label", "per_database", "cross_database"} return already has."""
    expected_language_code = _detect_language(user_question)
    chartable_entry = _pick_chartable_result(statement_results)
    prompt_content = _build_single_summary_prompt(
        user_question, sql, statement_results, chartable_entry, expected_language_code,
    )
    return (yield from _summarize_with_retry(
        prompt_content, f"Database Schema:\n{schema}\n\n", _SINGLE_SUMMARY_SYSTEM_INSTRUCTION, provider, client, model,
        content_parser=_make_single_summary_content_parser(chartable_entry),
        language_text_extractor=_single_summary_language_text,
        invalid_content_error="response was not the expected {\"summary\": ..., \"visualization\": ...} JSON shape, "
                               "or \"summary\" itself was empty/label-only",
        api_key=api_key, tried_keys=tried_keys, using_byok=using_byok,
        log_label="Single-connection results summarization", expected_language_code=expected_language_code,
    ))


@translate_bp.route('/api/summarize-result', methods=['POST'])
# See rate_limiter.py's own docstring (RATE_LIMIT_TRANSLATE/
# _translate_family_rate_limit) - pooled with /api/translate and
# summarize_results() above under the SAME budget, not a separate knob of
# its own: an LLM call to generate SQL and an LLM call to summarize
# results are the same kind of work as far as this app's admission
# control is concerned.
@summarize_rate_limit
def summarize_result():
    """See this module's "Single-connection mode's own post-execution
    results summarization" section comment above for the full picture.
    Called by the client once per single-connection turn, after
    /api/execute has actually run the generated SQL - never for a "route"
    outcome turn (that already gets its own Phase C summary via
    /api/summarize-results above) and never when the LLM answered
    directly via the "*** NO SQL ***" convention (nothing was executed,
    so nothing to summarize).

    Streams NDJSON, same contract/reasoning as /api/summarize-results
    above (see that route's docstring) - summarize_single_connection_
    results shares the exact same underlying retry machinery
    (_summarize_with_retry), so it needed the exact same fix: zero or
    more live {"status": "retrying", ...} lines while that retry loop
    runs, then exactly one terminal {"status": "done", "success": ...,
    "summary"/"error": ...} line. The two early-validation returns below
    (missing API key, missing prompt/sql/results) keep their real plain-
    JSON 400 responses, same as above."""
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
    sql = (data.get('sql') or '').strip()
    statement_results = data.get('results')
    if not prompt or not sql or not isinstance(statement_results, list) or not statement_results:
        resp = jsonify({'success': False, 'error': 'prompt, sql and results are required'})
        return apply_session_cookie(resp, session_id), 400

    # See concurrency_guard.py's own module docstring - TRANSLATE_GUARD,
    # the SAME pooled guard /api/translate and /api/summarize-results also
    # use, not a dedicated one for this route - acquired HERE, before the
    # schema lookup just below, not just before streaming starts: unlike
    # /api/summarize-results above (which has no equivalent inline DB work
    # before its own guard acquire), this route's schema fetch is itself
    # real (schema-cache-backed, but occasionally cold) DB work, and needs
    # to happen while this guard is actually held, not in a gap between
    # acquiring it and the point where a wrapping generator's own finally
    # would take over. {"success": False, "error": ...} matches this
    # route's own early-validation failures above.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'success': False,
            'error': 'The server is handling too many results-summarization requests right now. Please try again in a few seconds.',
        })

    def stream_summarize_result():
        # Same connection this turn's own /api/translate call itself
        # resolved (no client-side override needed/sent - the session's
        # current single connection, exactly like /api/translate's
        # single-connection path). Resolved HERE, inside the generator
        # (rather than synchronously above, before TRANSLATE_GUARD was even
        # acquired) so this DB work happens entirely within the window the
        # guard is held - see the comment above this generator's
        # definition.
        conn_str = resolve_conn_str(data.get('database_url'), user_identity)
        schema = get_database_schema(conn_str, user_identity)

        start_time = time.perf_counter()
        client = provider.make_client(api_key)
        cancel_token = cancel_handle = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            cancel_token, cancel_handle = cancel_registry.register(session_id, close_fn)
        try:
            parsed, usage, error = yield from summarize_single_connection_results(
                prompt, schema, sql, statement_results, provider, client, llm_model, api_key=api_key,
                using_byok=bool(byok_key),
            )
        finally:
            if cancel_token is not None:
                cancel_registry.unregister(session_id, cancel_token)
            if cancel_handle is not None:
                cancel_handle.close()
        duration = round(1000 * (time.perf_counter() - start_time))

        if parsed is None:
            error_message = (
                format_llm_error_for_user(provider, llm_model, error, using_byok=bool(byok_key))
                if isinstance(error, BaseException) else
                'Unable to summarize results right now.'
            )
            # Logged against the real connection this was run for (unlike
            # Phase C's "All Pre-Configured Datasets"/"All Pre-Configured Datasets" logging above), 0
            # tokens (no usable response on a total failure - see
            # _summarize_with_retry's own docstring), sql_command holding a
            # TRANSLATION_ERROR(...) sentinel in place of real SQL, same
            # overloaded-column convention "*** NO SQL ***" already uses.
            record_translation(
                user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model, duration,
                0, 0, 0, 0, 0,
            )
            yield json.dumps({'status': 'done', 'success': False, 'error': error_message}) + "\n"
            return

        # `parsed` is summarize_single_connection_results' own
        # {"summary", "visualization"} dict (see its own docstring) - only
        # "summary" gets the "*** NO SQL ***" prefix/translations-table
        # logging treatment; "visualization" (already fully validated
        # against the real executed columns/rows - see _clean_
        # visualization) rides along in the response as-is, for client.js
        # to render as a chart instead of/alongside the results table when
        # it's not None.
        summary_text = "*** NO SQL *** " + parsed["summary"]
        visualization = parsed["visualization"]
        usage_dict = usage or {}
        # Logged as a real translations-table row against the actual connection
        # this was run for (unlike Phase C's "All Pre-Configured Datasets"/"All Pre-Configured Datasets"
        # special-case logging - there IS one real connection here), same call
        # translate_query()'s own single-connection path already uses.
        record_translation(
            user_identity, conn_str, prompt, summary_text, llm_model, duration,
            usage_dict.get("input_tokens", 0), usage_dict.get("output_tokens", 0),
            usage_dict.get("total_tokens", 0), usage_dict.get("thinking_tokens", 0),
            usage_dict.get("cached_content_tokens", 0),
        )

        yield json.dumps({
            'status': 'done', 'success': True, 'summary': summary_text, 'visualization': visualization,
        }) + "\n"

    def _stream_summarize_result_with_guard_release():
        # Same reasoning as _stream_summarize_results_with_guard_release
        # above (and translate_query()'s own equivalent wrapper) - holds
        # the guard slot open for stream_summarize_result()'s ENTIRE
        # lifetime, schema lookup included, not just until this view
        # function returns.
        try:
            yield from stream_summarize_result()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_stream_summarize_result_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)


@translate_bp.route('/api/translate', methods=['POST'])
# rate_limiter.py's RATE_LIMIT_TRANSLATE (per-user request RATE over time,
# via Flask-Limiter) - runs before translate_query() is called at all, so
# a rate-limited request never reaches this function's own early-
# validation checks or TRANSLATE_GUARD.try_acquire() further down. See
# rate_limiter.py's own module docstring for why this is a different,
# complementary axis from that concurrency guard (rate over time vs.
# simultaneous in-flight requests).
@translate_rate_limit
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
    # The triage call itself gets this turn's ordinary conversation history
    # (see triage_all_mode_question's docstring) - it's a single, non-per-
    # database step, so there's exactly one shared thread for it to consult
    # (e.g. resolving "how large is THIS database" against a prior turn's
    # answer). Phase B's per-connection calls below are different: each one
    # gets THAT SPECIFIC connection's own history instead (see
    # connection_histories just below) - completely merged across however
    # the user has ever reached it, single-connection mode and "all
    # databases" mode alike (see client.js's connectionBucketKey()/
    # buildInScopeConnectionHistories() docstrings for the client-side half
    # of this).

    history = data.get('history', [])[-(HISTORY_MAX_TURNS * 2):]
    # Chunk 5 of "splitting SQL/summary per in-scope database" (see
    # client.js's captureAllModeHistory()/fanOutAllModeHistoryPerDatabase()/
    # buildInScopeConnectionHistories() docstrings for the full, multi-
    # window design history of this feature): one entry per in-scope
    # connection the client currently has a bucket for, keyed exactly like
    # client.js's connectionBucketKey() builds its bucket keys -
    # "preset:<id>" / "custom:<key>" - each value that connection's own
    # FULLY MERGED history array (every single-connection-mode turn AND
    # every all-mode turn ever fanned out to it, indistinguishably - see
    # fanOutAllModeHistoryPerDatabase()'s own docstring for why that merge
    # is already real by the time this ever reaches the server). Consulted
    # below, per selected connection, ONLY for Phase B's real SQL-generation
    # calls - triage above keeps using the ordinary shared `history`, since
    # routing is not itself an NL-to-SQL translation. Defaults to `{}` for
    # an older client that never sends this field at all, or a connection
    # this dict simply has no entry for (never visited, directly or via
    # fan-out) - both cases fall back to the same empty-history behavior
    # generate_sql_for_connection has always had, not an error.
    connection_histories = data.get('connection_histories') or {}
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
                # yield from (not a plain call) - triage_all_mode_question is
                # now a generator that yields live "retrying" NDJSON lines
                # whenever its own internal retry loop actually fires (key
                # rotation or a transient-error wait - see its docstring for
                # why this used to be invisible to the client). Forwarding
                # them here means a slow/rate-limited triage call gets the
                # exact same live feedback the single-connection generate-SQL
                # retry loop already gives - client.js needs no changes for
                # this, since 'retrying' is already handled generically
                # regardless of which server-side call produced it.
                triage_result = yield from triage_all_mode_question(
                    candidate_summaries, prompt, provider, client, llm_model, history=history,
                    api_key=api_key, using_byok=bool(byok_key),
                )
                # Phase A's own elapsed time and LLM usage, isolated from
                # whatever Phase B work (if any) happens next below -
                # logged as its own dedicated "All Pre-Configured Datasets"/"All Pre-Configured Datasets" translations-
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
                    # Each connection's OWN merged history (Chunk 5 - see
                    # connection_histories' own declaration comment above)
                    # looked up by the exact same "kind:id" string
                    # client.js's connectionBucketKey() builds - `.get(...)
                    # or []` covers both an old client that never sent this
                    # field at all and a connection this dict simply has no
                    # entry for yet, falling back to empty history either
                    # way rather than erroring. Re-truncated here with the
                    # same HISTORY_MAX_TURNS bound `history` above already
                    # got - a per-connection bucket is capped client-side
                    # too (createChatHistoryStore's own maxTurns), but this
                    # is the same defensive belt-and-suspenders re-slice
                    # every other history value in this module gets.
                    entry_histories = [
                        (connection_histories.get(f"{e['kind']}:{e['id']}") or [])[-(HISTORY_MAX_TURNS * 2):]
                        for e in selected_entries
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
                    # `prompt` (new - Chunk 4 of "splitting SQL/summary per
                    # in-scope database") is this database's own entry from
                    # `entry_prompts` above - triage's per-connection
                    # rewrite of the user's original question when it
                    # supplied one, else that original question unchanged
                    # (see entry_prompts' own comment just above). Every
                    # OTHER consumer of connection_selection (PINNED_
                    # CONNECTIONS' kind/id-only mapping in client.js, every
                    # existing test) only ever reads .kind/.id/.name, so
                    # this is purely additive - added here (rather than a
                    # separate field) so a later per-database history
                    # fan-out has, in one place, everything it needs to
                    # record that database's own {prompt, SQL, results,
                    # summary} tuple: the same list already threaded
                    # through to client.js's allModeStreamState.
                    # connectionOrder (see startAllModeStreaming()) and
                    # pendingAllModeNotes.
                    #
                    # `type` (new - GA4 fan-out tracking): each entry's own
                    # dialect, pulled from its resolved descriptor
                    # (resolve_in_scope_descriptors always sets "type" -
                    # see db.py's _to_descriptor default) - client.js's
                    # trackAllModeFanoutTranslate()/-Execute() need this to
                    # report a real per-database `database_type` on the GA
                    # events fired for each connection in this fan-out,
                    # rather than the single active connection's type (or
                    # none at all), which is all client.js could see before
                    # this field existed. Purely additive, same reasoning
                    # as `prompt` above.
                    connection_selection = [
                        {
                            "kind": e["kind"], "id": e["id"], "name": e["name"],
                            "type": (e.get("descriptor") or {}).get("type", ""),
                            "prompt": p,
                        }
                        for e, p in zip(selected_entries, entry_prompts)
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
                        selected_entries, entry_prompts, entry_histories,
                        provider, llm_model, user_identity, force_schema_refresh,
                    )
                    try:
                        while True:
                            done_entry, classified = next(phase_b_gen)
                            yield json.dumps({
                                "status": "phase_b_connection_done",
                                "kind": done_entry["kind"], "id": done_entry["id"], "name": done_entry["name"],
                                # Same "type" field/reasoning as
                                # connection_selection above - lets
                                # executeOneAllModeConnection() in client.js
                                # report a real per-database database_type
                                # on the sql_fanout_executed GA event it
                                # fires for THIS connection's own streamed
                                # auto-execute, without a second lookup.
                                "type": (done_entry.get("descriptor") or {}).get("type", ""),
                                **classified,
                            }) + "\n"
                    except StopIteration as stop:
                        sql_blocks, database_notes, generation_failures, phase_b_usage, phase_b_log_entries = stop.value

                    generated_sql = "\n\n".join(marked for _, marked in sql_blocks)
                    # Per-database structured equivalent of the joined
                    # `generated_sql` string above - the whole reason
                    # sql_blocks (from _run_phase_b_fanout) is a list of
                    # (entry, marked_sql) pairs in the first place, rather
                    # than already-joined text, is so a per-database view
                    # of "what SQL did THIS database get" doesn't need to
                    # be reconstructed later by re-parsing the combined
                    # string's own '-- database: ...' markers. `sql`
                    # above stays exactly as it's always been (joined,
                    # marker-tagged) for every existing consumer (the
                    # response's own 'sql' field, the SQL editor box,
                    # existing tests) - history logging below now uses
                    # `phase_b_log_entries` instead, one dedicated row per
                    # connection, rather than this joined string. This is
                    # purely additive, feeding a future per-database-history
                    # feature (and any other future per-database
                    # consumer) without touching anything that already
                    # depends on the flattened shape. Only ever present
                    # for databases that actually returned real SQL - a
                    # database that noted or failed instead has no entry
                    # here, same as it has no entry in `sql_blocks`
                    # itself; database_notes/generation_failures below
                    # remain the source of truth for those two cases.
                    sql_by_database = [
                        {"kind": entry["kind"], "id": entry["id"], "name": entry["name"], "sql": marked}
                        for entry, marked in sql_blocks
                    ]
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
                        "sql_blocks": sql_by_database,
                    }
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
                # "All Pre-Configured Datasets"/"All Pre-Configured Datasets" translations-table row - see
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
                    # One dedicated translations-table row PER SELECTED
                    # CONNECTION - not one combined row for the whole batch
                    # attributed only to the first connection, which is
                    # what this used to do (see phase_b_log_entries'
                    # docstring in _run_phase_b_fanout for the full
                    # reasoning). Each entry already carries its own real,
                    # independently-measured duration and its own usage
                    # (zeroed for a failure) - no derived "share of the
                    # total" math needed here at all, since Phase A's
                    # duration/usage was already logged separately above
                    # and every Phase B call's own elapsed time was
                    # measured directly, not inferred from a shared total.
                    for log_entry in phase_b_log_entries:
                        usage = log_entry["usage"]
                        record_translation(
                            user_identity, log_entry["entry"]["descriptor"], log_entry["prompt"],
                            log_entry["sql_command"], llm_model, log_entry["duration"],
                            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                            usage.get("total_tokens", 0), usage.get("thinking_tokens", 0),
                            usage.get("cached_content_tokens", 0),
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

            # Computed once, up front, off the user's own prompt - see
            # _no_sql_language_mismatch's docstring for the full picture.
            # None (detection unavailable or too low-confidence) disables
            # the check entirely below, exactly like every other call site
            # that threads this through.
            expected_language_code = _detect_language(prompt)

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
            # Bounded 2-attempt outer loop, same "1 real attempt + 1
            # corrective retry" budget _summarize_with_retry uses for the
            # exact same reason (see its own docstring, and
            # _no_sql_language_mismatch's) - closes the language-
            # verification gap for THIS call's own free-text replies
            # ('*** NO SQL ***' answers/help-popup/error text), which
            # previously had only _COMMON_FORMAT_RULES' bare instruction
            # and nothing checking whether the model actually followed it.
            # Ordinary generated SQL (no '*** NO SQL ***' prefix) never
            # enters the language-mismatch branch below at all - see
            # _no_sql_language_mismatch's docstring for why that check is
            # deliberately scoped to free text only.
            for language_attempt in range(2):
                llm_input = provider.build_llm_input(history, schema_block, new_prompt_content)
                # transient_attempt tracks the SHARED, both-providers budget
                # for same-key/after-a-delay retries (MAX_TRANSLATION_ATTEMPTS)
                # - it's advanced only by the "else" (non-rotate) branch
                # below, and reset fresh on each language_attempt: a
                # language-mismatch retry is a brand new call, not a
                # continuation of whatever transient-error budget the
                # previous attempt happened to consume. The Gemini-only
                # key-rotation budget above is tracked separately via
                # tried_llm_keys/gemini_key_pool_size, so a run of 429s
                # doesn't eat into this counter at all, and vice versa.
                transient_attempt = 1
                try:
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
                except LlmCallFailed as e:
                    # Every attempt (and, for Gemini, every configured key) is
                    # exhausted - this is the ONE exception type raised only
                    # from inside this retry loop, so reaching here means the
                    # LLM genuinely was called and genuinely never returned
                    # usable SQL, as opposed to e.g. a schema-fetch failure
                    # before this loop even started (those fall through to the
                    # generic `except Exception` below, unlogged, since no LLM
                    # call was ever attempted for them). A real API failure
                    # like this is never retried for language reasons - it
                    # fails outright here exactly as it always has, on either
                    # language_attempt.
                    #
                    # duration is measured from the SAME start_time the success
                    # path uses (captured once, before this whole outer loop) -
                    # so, same as a successful later-attempt call, it already
                    # includes every attempt's own call time plus every
                    # inter-attempt wait/rotation (transient AND, now,
                    # language-driven), and nothing from before the loop
                    # (schema fetch, prompt building) or after it - i.e. only
                    # time actually spent waiting on the LLM.
                    duration = round(1000 * (time.perf_counter() - start_time))
                    error_message = str(e)
                    # No usage_info was ever populated (it's only ever assigned
                    # on a successful provider.call() return above), so every
                    # token count here is a real, honest 0 - not a placeholder
                    # standing in for tokens that were actually spent.
                    record_translation(
                        user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model, duration,
                        0, 0, 0, 0, 0,
                    )
                    yield json.dumps({
                        'status': 'done',
                        'success': False,
                        'error': error_message,
                    }) + "\n"
                    return

                if generated_sql.startswith("```"):
                    lines = generated_sql.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    generated_sql = "\n".join(lines).strip()

                # Language verification - see _no_sql_language_mismatch's
                # docstring for exactly what this does and doesn't cover.
                # Only a '*** NO SQL ***' free-text reply is ever checked;
                # plain generated SQL always falls straight through to
                # `break` below, unchanged from before this loop existed.
                stripped_sql = generated_sql.strip()
                if _NO_SQL_PREFIX_RE.match(stripped_sql):
                    free_text = _strip_no_sql_prefix(stripped_sql)
                    actual_language_code = _no_sql_language_mismatch(free_text, expected_language_code)
                    if actual_language_code is not None:
                        expected_name = _describe_language(expected_language_code)
                        actual_name = _describe_language(actual_language_code)
                        if language_attempt + 1 < 2:
                            logger.warning(
                                "Translation free-text reply came back in %s instead of the prompt's own %s "
                                "(attempt %d/2) - discarding, retrying with an explicit correction",
                                actual_name, expected_name, language_attempt + 1,
                            )
                            # Same "name the mistake and the fix directly"
                            # shape as _summarize_with_retry's own correction
                            # addendum - simply re-asking with the identical
                            # prompt would likely just reproduce the same
                            # wrong-language answer, since whatever pulled
                            # the model toward actual_name (usually
                            # foreign-language schema/data in view) is still
                            # there.
                            new_prompt_content = (
                                f"{new_prompt_content}\n\nCORRECTION: your previous free-text reply to this "
                                f"exact request was written in {actual_name}, which is WRONG - the request was "
                                f"in {expected_name}, so any free-text reply (not real generated SQL itself, "
                                f"which is unaffected) must be written entirely in {expected_name}. Write your "
                                f"full response again, from scratch, entirely in {expected_name} this time."
                            )
                            continue
                        # The one corrective retry is exhausted and the
                        # reply STILL came back in the wrong language -
                        # mirrors _summarize_with_retry's own "never
                        # knowingly serve a response in the wrong language"
                        # guarantee: this turn fails outright (an honest,
                        # specific error) rather than silently showing text
                        # already confirmed to be in the wrong language.
                        logger.warning(
                            "Translation free-text reply still came back in %s instead of %s after "
                            "retrying - failing this turn rather than serving a known-wrong-language response",
                            actual_name, expected_name,
                        )
                        duration = round(1000 * (time.perf_counter() - start_time))
                        error_message = (
                            f"The response kept coming back in {actual_name} instead of {expected_name}, "
                            f"even after retrying. Try rephrasing your question."
                        )
                        # Unlike the LlmCallFailed path above, real usage WAS
                        # spent (the call itself succeeded, twice) - logged
                        # honestly rather than as 0s.
                        record_translation(
                            user_identity, conn_str, prompt, f"TRANSLATION_ERROR ({error_message})", llm_model,
                            duration, usage_info.get("input_tokens", 0), usage_info.get("output_tokens", 0),
                            usage_info.get("total_tokens", 0), usage_info.get("thinking_tokens", 0),
                            usage_info.get("cached_content_tokens", 0),
                        )
                        yield json.dumps({
                            'status': 'done',
                            'success': False,
                            'error': error_message,
                        }) + "\n"
                        return
                break
            end_time = time.perf_counter()

            duration = round(1000 * (end_time - start_time))
            input_tokens = usage_info.get("input_tokens", 0)
            output_tokens = usage_info.get("output_tokens", 0)
            total_tokens = usage_info.get("total_tokens", 0)
            thinking_tokens = usage_info.get("thinking_tokens", 0)
            cached_content_tokens = usage_info.get("cached_content_tokens", 0)

            # Anonymous visitors share a single per-session identity
            # (anonymous:<session_id>) rather than a real signed-in one, but
            # the translation is recorded the same way regardless - a
            # write-only audit trail (aggregate usage/cost visibility, e.g.
            # via export_state.py) with no in-app read/purge surface anymore
            # (the /api/history endpoint that used to expose it was removed
            # as dead code once the History modal stopped showing it - see
            # chat_history_routes.py's module docstring for where that
            # modal's data actually comes from today).
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

    # See concurrency_guard.py's own module docstring for why this exists
    # alongside Cloud Run's --concurrency/--max-instances (gcp_deploy.sh).
    # Acquired HERE, right before actually streaming - not any earlier -
    # so the two early-validation returns above (missing API key, empty
    # prompt) never touch this guard at all: they do essentially no work,
    # so they shouldn't consume one of a scarce number of "expensive work"
    # slots. A rejection here keeps the same real, non-streamed HTTP
    # status (503, not 200-with-success-false) those two early returns
    # already use, for the identical reason spelled out in the big comment
    # above stream_translation() - nothing has streamed yet at this point,
    # so an ordinary HTTP status is still possible. {"error": ...} (no
    # "success" key) matches that same early-validation shape exactly,
    # which client.js's readNdjsonStream already reads correctly as a
    # single, non-streamed line straight into `finalData` (see that
    # function's own docstring) - zero client-side changes needed.
    if not TRANSLATE_GUARD.try_acquire():
        return busy_response({
            'error': 'The server is handling too many translation requests right now. Please try again in a few seconds.',
        })

    def _translation_stream_with_guard_release():
        # Holds the guard slot open for stream_translation()'s ENTIRE
        # lifetime, not just until this route function returns - the
        # generator itself is what does the real (expensive) work, driven
        # lazily by Flask/gunicorn as it sends the response, well after
        # translate_query() has already returned the Response object below.
        # A plain try/finally around the outer call (the way
        # execute_routes.py's @guarded_route decorator works for its own,
        # non-streaming route) would release this slot immediately,
        # before any real work even started. Relies on stream_translation()'s
        # own `finally` (just above) reliably running even if the client
        # disconnects/cancels mid-stream - already something this codebase
        # depends on for cancel_registry's own cleanup there, not a new
        # assumption introduced here.
        try:
            yield from stream_translation()
        finally:
            TRANSLATE_GUARD.release()

    resp = Response(stream_with_context(_translation_stream_with_guard_release()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)