"""
translate_routes.py

Natural-language-to-SQL translation via Gemini: API key selection, chat
history -> Gemini `Content` conversion, the system prompt, and the
/api/translate route itself.

/api/translate streams its response as newline-delimited JSON (NDJSON)
rather than a single JSON body, so a client can show live "retrying..."
feedback while the retry loop below (the single place in this app that
retries a translation - see MAX_TRANSLATION_ATTEMPTS/_classify_gemini_error)
works through a transient LLM failure, instead of the request just
appearing to hang. See translate_query()'s stream_translation() for the
exact line shapes and the HTTP-status-code trade-off streaming requires.
"""

import json
import random
import os
import time

from flask import Blueprint, request, jsonify, Response, stream_with_context
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
import anthropic

# from app_config import DEFAULT_MODEL, logger, log_and_generalize_error
from app_config import DEFAULT_MODEL, logger

from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_database_schema, record_translation
from backends import get_backend

translate_bp = Blueprint('translate', __name__)

# Which LLM backend /api/translate uses. Set LLM_PROVIDER=claude in the
# environment to switch this whole route over to Claude; anything else
# (including unset, the default) keeps the original Gemini path exactly as
# it was before. No other code path in this file branches on this except
# stream_translation() and the setup right before it.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()

# claude-sonnet-5 is a solid default for NL-to-SQL: strong structured-output
# reasoning at a much lower cost than the top-tier model. Override per
# request the same way gemini_model already can (data['claude_model'] /
# data['model']), or via this env var for a fleet-wide default change with
# no code edit.
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

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
    ),
    "Snowflake SQL": (
        "You are an expert SQL generation assistant for Snowflake.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Snowflake SQL.\n"
        "You may return one or more independent SQL statements, and Snowflake Scripting (DECLARE/BEGIN/IF/FOR) where appropriate.\n"
        "Use double quotes for identifiers that need quoting (Snowflake's default, case-sensitive form); unquoted identifiers are treated as upper-case.\n"
        "Snowflake has no enforced PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
    ),
    "MySQL": (
        "You are an expert SQL generation assistant for MySQL-compatible RDBMSs.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid MySQL SQL.\n"
        "You may return one or more independent SQL statements, and MySQL stored-program constructs (DECLARE/IF/LOOP/WHILE) where appropriate.\n"
        "Use backticks for identifiers that need quoting; MySQL treats double-quoted text as a string literal by default (like standard SQL), not an identifier.\n"
        "MySQL has no schemas separate from databases - a schema and a database are the same thing here.\n"
    ),
    "Databricks SQL": (
        "You are an expert SQL generation assistant for Databricks SQL (Spark SQL).\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Databricks SQL.\n"
        "You may return one or more independent SQL statements, and Databricks SQL scripting (DECLARE/IF/WHILE/FOR) where appropriate.\n"
        "Use backticks for identifiers that need quoting.\n"
        "The connection has a default catalog and schema already selected, so plain table names (not schema-qualified or catalog-qualified) resolve correctly - do not prefix table names with a catalog or schema unless the user explicitly asks to query a different one.\n"
        "Databricks (Unity Catalog) does not enforce PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
    ),
    "Oracle Database": (
        "You are an expert SQL generation assistant for Oracle Database.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Oracle SQL.\n"
        "You may return one or more independent SQL statements, and PL/SQL (DECLARE/BEGIN/END blocks, or CREATE PROCEDURE/FUNCTION) where appropriate.\n"
        "Use double quotes for identifiers that need quoting; unquoted identifiers are folded to upper-case, so schema entries shown in upper-case (the common case) resolve correctly unquoted - only quote an identifier if it needs to preserve lower/mixed case or contains special characters.\n"
        "Oracle has no LIMIT clause - use FETCH FIRST n ROWS ONLY (or ROWNUM/ROW_NUMBER() for older-style pagination) to cap result rows.\n"
        "Every SELECT must have a FROM clause - use FROM DUAL for a query that doesn't otherwise reference a table (e.g. SELECT SYSDATE FROM DUAL).\n"
        "String literals use single quotes only; double quotes are exclusively for identifiers, never string values.\n"
    ),
    "Amazon Redshift SQL": (
        "You are an expert SQL generation assistant for Amazon Redshift.\n"
        "Given the provided past chat interactions, the database schema and the user's natural language prompt, translate the request into valid Redshift SQL.\n"
        "Redshift SQL is derived from PostgreSQL - most standard SQL constructs from that dialect apply, but Redshift has limited support for PL/pgSQL-style procedural code (CREATE PROCEDURE using a small subset of PL/pgSQL is supported in recent versions; prefer plain SQL statements otherwise).\n"
        "Use double quotes for identifiers that need quoting, same as PostgreSQL.\n"
        "Redshift has no enforced PK/FK/UNIQUE constraints - schema entries listing them are informational only, not something the database rejects violations of.\n"
        "Redshift has no CREATE INDEX / index concept at all - schema entries instead list each table's DISTSTYLE/DISTKEY (how rows are distributed across compute nodes) and SORTKEY (how rows are ordered on disk); do not suggest creating an index, and do not invent WHERE-clause assumptions based on indexes that don't exist here.\n"
        "Redshift has no trigger support.\n"
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
        "Return EXACTLY ONE query - this dialect has no multi-statement/batch concept, so never return multiple semicolon-separated statements, and do not end the query with a trailing semicolon.\n"
    ),
}
_DEFAULT_DIALECT_PROMPT_INTRO = _DIALECT_PROMPT_INTROS["PostgreSQL"]

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
# needs updating.
MAX_TRANSLATION_ATTEMPTS = int(os.environ.get("MAX_TRANSLATION_ATTEMPTS", 5))
TRANSLATION_RETRY_DELAY_SECONDS = float(os.environ.get("TRANSLATION_RETRY_DELAY_SECONDS", 1))


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

    return None


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
    cached_content_tokens = getattr(usage, 'cached_content_token_count', 0) if usage else 0
    # Logged at INFO (this app's default LOG_LEVEL) so cache behavior can be
    # confirmed directly from the server's own logs rather than relying on
    # a provider console's dashboard - see the matching log in _call_claude
    # below for why this was added (both are the concrete way to verify a
    # cache hit actually happened on a given call, immediately, per-call).
    logger.info(
        "Gemini call cache stats: cached_content_tokens=%s prompt_tokens=%s",
        cached_content_tokens, usage.prompt_token_count if usage else 0,
    )
    return text, {
        "input_tokens": usage.prompt_token_count if usage else 0,
        "output_tokens": usage.candidates_token_count if usage else 0,
        "total_tokens": usage.total_token_count if usage else 0,
        "thinking_tokens": getattr(usage, 'thoughts_token_count', 0) if usage else 0,
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
    cache_read_tokens = getattr(usage, 'cache_read_input_tokens', 0) if usage else 0
    cache_creation_tokens = getattr(usage, 'cache_creation_input_tokens', 0) if usage else 0
    # Logged at INFO (this app's default LOG_LEVEL) rather than only
    # exposed via the Anthropic Console's own usage dashboard - that
    # dashboard aggregates across the whole workspace/account with its own
    # refresh delay, so it can't tell you whether THIS call actually hit
    # the cache. Both numbers are logged (not just the read count carried
    # in cached_content_tokens below): cache_creation_input_tokens > 0 on a
    # call means this call WROTE a new cache entry (a miss that primes the
    # next one), while cache_read_input_tokens > 0 means it actually reused
    # one written by an earlier call - seeing only the former for a while
    # after a change like this one is expected and fine, it's the latter
    # ever becoming nonzero that confirms reuse is really happening.
    logger.info(
        "Claude call cache stats: cache_creation_input_tokens=%s cache_read_input_tokens=%s input_tokens=%s",
        cache_creation_tokens, cache_read_tokens, usage.input_tokens if usage else 0,
    )
    return text, {
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "total_tokens": (usage.input_tokens + usage.output_tokens) if usage else 0,
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


@translate_bp.route('/api/translate', methods=['POST'])
def translate_query():
    data = request.get_json() or {}

    if LLM_PROVIDER == "claude":
        llm_model = data.get('claude_model') or data.get('model') or DEFAULT_CLAUDE_MODEL
        api_key = pick_claude_api_key()
        if not api_key:
            return jsonify({'error': 'Claude API key is not configured.'}), 400
    else:
        llm_model = data.get('gemini_model') or data.get('model') or DEFAULT_MODEL
        api_key = pick_gemini_api_key()
        if not api_key:
            return jsonify({'error': 'Gemini API key is not configured.'}), 400
    tried_llm_keys = {api_key}

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt cannot be empty'}), 400

    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

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
        try:
            schema = get_database_schema(conn_str, user_identity, force_refresh=force_schema_refresh)
            if LLM_PROVIDER == "claude":
                client = anthropic.Anthropic(api_key=api_key)
            else:
                client = genai.Client(api_key=api_key)

            try:
                dialect_name = get_backend(conn_str).dialect_name
            except Exception:
                dialect_name = "PostgreSQL"
            dialect_intro = _DIALECT_PROMPT_INTROS.get(dialect_name, _DEFAULT_DIALECT_PROMPT_INTRO)

            system_instruction = (
                dialect_intro +
                "Format the result data to be easily readable. For example, format timestamps as date:hour:min:sec.\n"
                "Return ONLY the raw SQL code block. Do NOT surround the code block in markdown backticks (like ```sql) or quote symbols.\n"
                "If asked to documnet the SQL command, add comments at the top of the query using standard SQL convenstion for how to mark comments.\n"
                "If you can respond to the prompt succinctly based on your general-purpose training, return your response prepended by the string '*** NO SQL ***'\n"
                "If the prompt is about the data available in the database that is currently configured, return your response based on your knowledge of the schema and include an ER diagram using ascii art. Prepend the string '*** NO SQL ***' to your response\n"
                "If the prompt is about this app itself, respond as follows: '*** NO SQL *** OPEN HELP POPUP ***'.\n"
                "If you cannot respond at all with reasonable confidence, return '*** NO SQL *** I am not able to respond to your prompt.'\n"
                "If you run into any error, return '*** NO SQL *** I ran into this error: <the error>'.\n"
                "If you want to respond partly with a SQL command and partly with free text, enclose the free text as follows 'SELECT <your free-text response in quotes> as RESPONSE;'.\n"
                "If a user asks you who you are or what model you are using, hide this behind a generic response.\n"
            )

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
            # schema -> history -> new prompt.
            schema_block = f"Database Schema:\n{schema}\n\n"
            new_prompt_content = f"User Request: {prompt}\n\nSQL Query:"

            if LLM_PROVIDER == "claude":
                llm_input = build_claude_history_messages(history)
                if llm_input:
                    llm_input[0]["content"] = schema_block + llm_input[0]["content"]
                    # Marks the end of the accumulated (stable) prefix -
                    # everything from the system prompt through this
                    # message is a caching candidate; only the new prompt
                    # appended below is left unmarked, since it's the one
                    # part guaranteed to differ every call. From the second
                    # call in a conversation onward, that first message
                    # (schema + the oldest turn's unchanged text) is
                    # byte-identical across calls - the ever-growing,
                    # cacheable prefix this relies on. See
                    # _mark_claude_cache_boundary's docstring.
                    _mark_claude_cache_boundary(llm_input[-1])
                    llm_input.append({"role": "user", "content": new_prompt_content})
                else:
                    # A conversation's very first call - there's no history
                    # turn to prepend the schema onto and mark, but the
                    # schema itself is still identical across EVERY first
                    # call against this DB (this user's next fresh
                    # conversation, or a different user's, as long as it
                    # lands within the cache's TTL) - by far the single
                    # biggest, most repeated, most worth-caching block this
                    # app ever sends, so it shouldn't go out unmarked just
                    # because there's no prior turn yet. Concatenating it
                    # onto new_prompt_content into one plain string (as a
                    # previous version of this code did) and marking THAT
                    # would be wrong - the marker would cover the
                    # ever-different question too, so the exact bytes would
                    # never repeat and the block would never actually hit.
                    # Splitting into two content blocks on the same message
                    # keeps schema_block as its own marked, reusable prefix
                    # and leaves new_prompt_content unmarked, exactly
                    # mirroring what _mark_claude_cache_boundary does for
                    # the has-history case above, just one call earlier.
                    llm_input.append({
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
                llm_input = build_gemini_history_contents(history)
                if llm_input:
                    first_part = llm_input[0].parts[0]
                    first_part.text = schema_block + first_part.text
                else:
                    new_prompt_content = schema_block + new_prompt_content
                llm_input.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=new_prompt_content)]
                    )
                )

            # Gemini's key-rotation retries (see _classify_gemini_error's
            # 429 case) get their own budget, sized to how many keys are
            # actually configured - NOT to MAX_TRANSLATION_ATTEMPTS. Only
            # Gemini ever sets rotate_key=True (Claude's classifier never
            # does - see _classify_claude_error's docstring), so this is
            # irrelevant when LLM_PROVIDER=="claude". tried_llm_keys already
            # starts as {api_key} (set above, before this generator runs),
            # so it's the natural running total of distinct keys tried.
            gemini_key_pool_size = len(get_gemini_api_keys())

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
                    if LLM_PROVIDER == "claude":
                        generated_sql, usage_info = _call_claude(client, llm_model, llm_input, system_instruction)
                    else:
                        generated_sql, usage_info = _call_gemini(client, llm_model, llm_input, system_instruction)
                    break
                except Exception as e:
                    retry_action = (
                        _classify_claude_error(e) if LLM_PROVIDER == "claude"
                        else _classify_gemini_error(e)
                    )
                    if retry_action is None:
                        raise

                    if retry_action["rotate_key"]:
                        # Gemini-only budget: one attempt per configured
                        # key. Checked BEFORE picking the next key (rather
                        # than relying on pick_gemini_api_key's own
                        # fallback-to-full-pool behavior) so exhaustion is
                        # decided here, not masked by that fallback.
                        if len(tried_llm_keys) >= gemini_key_pool_size:
                            raise
                        next_key = pick_gemini_api_key(exclude=tried_llm_keys)
                        if next_key != api_key:
                            api_key = next_key
                            client = genai.Client(api_key=api_key)
                        tried_llm_keys.add(api_key)
                        # No "in %ds" here - a key-rotation retry always
                        # fires immediately (see _classify_gemini_error's
                        # comment for why waiting doesn't make sense when
                        # the next attempt already uses a different key).
                        logger.warning(
                            "%s call failed (%d/%d configured keys tried), rotating API key and retrying immediately: %s",
                            LLM_PROVIDER, len(tried_llm_keys), gemini_key_pool_size, e
                        )
                        # Told to the client before continuing, so
                        # "retrying..." is visible even though there's no
                        # delay to speak of.
                        yield json.dumps({
                            "status": "retrying",
                            "attempt": len(tried_llm_keys),
                            "maxAttempts": gemini_key_pool_size,
                            "delaySeconds": 0,
                            "rotatedKey": True,
                        }) + "\n"
                        continue

                    # Shared transient-error budget (both providers).
                    if transient_attempt >= MAX_TRANSLATION_ATTEMPTS:
                        raise
                    logger.warning(
                        "%s call failed (attempt %d/%d), retrying in %ds: %s",
                        LLM_PROVIDER, transient_attempt, MAX_TRANSLATION_ATTEMPTS, retry_action["delay"], e
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

    resp = Response(stream_with_context(stream_translation()), mimetype='application/x-ndjson')
    return apply_session_cookie(resp, session_id)