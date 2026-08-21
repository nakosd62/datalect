"""
translate_routes.py

Natural-language-to-SQL translation via Gemini: API key selection, chat
history -> Gemini `Content` conversion, the system prompt, and the
/api/translate route itself.

/api/translate streams its response as newline-delimited JSON (NDJSON)
rather than a single JSON body, so a client can show live "retrying..."
feedback while the retry loop below (the single place in this app that
retries a translation - see MAX_GEMINI_ATTEMPTS/_classify_gemini_error)
works through a transient Gemini failure, instead of the request just
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

# from app_config import DEFAULT_MODEL, logger, log_and_generalize_error
from app_config import DEFAULT_MODEL, logger

from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_database_schema, record_translation
from backends import get_backend

translate_bp = Blueprint('translate', __name__)

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
}
_DEFAULT_DIALECT_PROMPT_INTRO = _DIALECT_PROMPT_INTROS["PostgreSQL"]

# Two retryable Gemini failure kinds, each worth a few automatic retries -
# the same request usually gets through shortly after. Anything else (bad
# request, invalid model, auth failure, etc.) will just fail the same way
# again, so it's raised immediately instead.
#
# GEMINI_RETRY_DELAY_SECONDS applies ONLY to a 5xx/transient server-side
# hiccup - the same key is reused, and the failure is about Gemini's own
# backend momentarily struggling, not about anything the client did, so a
# brief pause before hitting the exact same thing again gives it a moment
# to clear. A 429 (per-key rate limit/capacity exhausted) is a different
# story: the next attempt uses a DIFFERENT key (see _classify_gemini_error's
# rotate_key), which isn't subject to whatever limit the failed key just
# hit, so there's nothing to wait out - that retry fires immediately, with
# no delay (see _classify_gemini_error below).
#
# Both knobs are configurable via env vars (e.g. to tune retry behavior for
# a noisier Gemini rollout without a code change) - same int()/float()-on-
# getenv pattern as SCHEMA_CACHE_TTL_SECONDS in schema_cache.py.
MAX_GEMINI_ATTEMPTS = int(os.environ.get("MAX_GEMINI_ATTEMPTS", 5))
GEMINI_RETRY_DELAY_SECONDS = float(os.environ.get("GEMINI_RETRY_DELAY_SECONDS", 1))


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
#   - delay (float): seconds to sleep before the next attempt. Only ever
#     non-zero for a failure that's NOT key-related (see rotate_key below) -
#     waiting only makes sense when the next attempt is otherwise identical
#     to the one that just failed (same key, same everything), giving
#     whatever went wrong a moment to clear. When the next attempt already
#     differs (a different key), there's nothing to wait out.
#   - rotate_key (bool): pick a different configured API key for the next
#     attempt rather than reusing the one that just failed. Used for
#     capacity/rate-limit errors: the failed key is (at least momentarily)
#     out of capacity, but a different configured key almost certainly
#     isn't, so that retry fires immediately (delay=0) rather than sitting
#     idle waiting out a limit a different key was never subject to.
#     Transient server-side errors aren't key-related at all, so they
#     retry with the same key instead - and since nothing changed about
#     the request, they DO wait out GEMINI_RETRY_DELAY_SECONDS first, on
#     the theory the same problem needs a moment to pass.
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
    # GEMINI_RETRY_DELAY_SECONDS (that delay is reserved for the 5xx case
    # below, where the same key retries against the same problem).
    if code == 429:
        return {"rotate_key": True, "delay": 0}

    # 5xx - transient, server-side hiccup (e.g. the plain "500 INTERNAL"
    # Gemini occasionally throws) unrelated to which key was used, so the
    # same key is fine to retry with. Unlike the 429 case above, the next
    # attempt is otherwise identical to the one that just failed, so this
    # one DOES wait out GEMINI_RETRY_DELAY_SECONDS first, giving the
    # transient condition a moment to actually pass before trying the
    # exact same thing again.
    is_server_error = (isinstance(code, int) and 500 <= code < 600) or isinstance(exc, genai_errors.ServerError)
    if is_server_error:
        return {"rotate_key": False, "delay": GEMINI_RETRY_DELAY_SECONDS}

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
                header = f"[Query Result {i + 1} - {row_count} row(s) total, showing {len(rws)}]"
                result_blocks.append(header + "\n" + format_results_table_text(cols, rws, max_rows=len(rws)))
            combined_text = combined_text + "\n\n" + "\n\n".join(result_blocks)

        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=combined_text)]
            )
        )
    return contents


@translate_bp.route('/api/translate', methods=['POST'])
def translate_query():
    data = request.get_json() or {}

    gemini_model = data.get('gemini_model') or data.get('model') or DEFAULT_MODEL
    tried_gemini_keys = set()
    api_key = pick_gemini_api_key()

    if not api_key:
        return jsonify({'error': 'Gemini API key is not configured.'}), 400
    tried_gemini_keys.add(api_key)

    prompt = data.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'Prompt cannot be empty'}), 400

    # session_id resolved first and passed into get_current_user_identity()
    # so an anonymous visitor's identity is scoped to THIS session, not a
    # freshly-derived one - see that function's docstring in auth.py.
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    history = data.get('history', [])[-20:]
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
                "If you can respond to the prompt succinctly based on your general-purpose training, return your response prepended by the string '*** NO SQL ***'\n"
                "If the prompt is about this app itself (yDyL) respond as follows: '*** NO SQL *** OPEN HELP POPUP ***'\n"
                "If you cannot respond at all with reasonable confidence, return '*** NO SQL *** I am not able to respond to your prompt.'\n"
                "If you run into any error, return '*** NO SQL *** I ran into this error: <the error>'\n"
            )

            user_message_content = f"Database Schema:\n{schema}\n\nUser Request: {prompt}\n\nSQL Query:"

            contents = build_gemini_history_contents(history)
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=user_message_content)]
                )
            )

            start_time = time.perf_counter()
            response = None
            for attempt in range(1, MAX_GEMINI_ATTEMPTS + 1):
                try:
                    response = client.models.generate_content(
                        model=gemini_model,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.1
                        )
                    )
                    break
                except Exception as e:
                    retry_action = _classify_gemini_error(e)
                    if attempt >= MAX_GEMINI_ATTEMPTS or retry_action is None:
                        raise

                    if retry_action["rotate_key"]:
                        next_key = pick_gemini_api_key(exclude=tried_gemini_keys)
                        if next_key != api_key:
                            api_key = next_key
                            client = genai.Client(api_key=api_key)
                        tried_gemini_keys.add(api_key)
                        # No "in %ds" here - a key-rotation retry always
                        # fires immediately (see _classify_gemini_error's
                        # comment for why waiting doesn't make sense when
                        # the next attempt already uses a different key).
                        logger.warning(
                            "Gemini call failed (attempt %d/%d), rotating API key and retrying immediately: %s",
                            attempt, MAX_GEMINI_ATTEMPTS, e
                        )
                    else:
                        logger.warning(
                            "Gemini call failed (attempt %d/%d), retrying in %ds: %s",
                            attempt, MAX_GEMINI_ATTEMPTS, retry_action["delay"], e
                        )

                    # Told to the client before sleeping, not after, so
                    # "retrying..." is visible for the full delay instead of
                    # appearing right as the next attempt actually fires.
                    yield json.dumps({
                        "status": "retrying",
                        "attempt": attempt + 1,
                        "maxAttempts": MAX_GEMINI_ATTEMPTS,
                        "delaySeconds": retry_action["delay"],
                        "rotatedKey": retry_action["rotate_key"],
                    }) + "\n"

                    # Skip the sleep entirely rather than call time.sleep(0) -
                    # a key-rotation retry's delay is always 0 (see
                    # _classify_gemini_error), and this makes "no delay"
                    # mean no sleep call at all, not a zero-length one.
                    if retry_action["delay"]:
                        time.sleep(retry_action["delay"])
                    continue
            end_time = time.perf_counter()

            generated_sql = response.text.strip() if response.text else ""
            if generated_sql.startswith("```"):
                lines = generated_sql.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                generated_sql = "\n".join(lines).strip()

            duration = round(1000 * (end_time - start_time))
            usage = response.usage_metadata
            input_tokens = usage.prompt_token_count if usage else 0
            output_tokens = usage.candidates_token_count if usage else 0
            total_tokens = usage.total_token_count if usage else 0
            thinking_tokens = getattr(usage, 'thoughts_token_count', 0) if usage else 0
            cached_content_tokens = getattr(usage, 'cached_content_token_count', 0) if usage else 0

            # Anonymous users share a single identity and can't view/purge
            # their own history via the app (see history_routes.py, still
            # gated) - but the translation itself is still worth recording
            # for aggregate usage/cost visibility (e.g. via export_state.py),
            # so it's logged the same as any other user's, just attributed to
            # the shared "anonymous" identity rather than a real one.
            record_translation(user_identity, conn_str, prompt, generated_sql, gemini_model, duration, input_tokens, output_tokens, total_tokens, thinking_tokens, cached_content_tokens)

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