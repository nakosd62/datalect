"""
translate_routes.py

Natural-language-to-SQL translation via Gemini: API key selection, chat
history -> Gemini `Content` conversion, the system prompt, and the
/api/translate route itself.
"""

import random
import os
import time

from flask import Blueprint, request, jsonify
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

# from app_config import DEFAULT_MODEL, logger, log_and_generalize_error
from app_config import DEFAULT_MODEL, logger

from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_database_schema, record_translation

translate_bp = Blueprint('translate', __name__)

# Transient Gemini failures (rate limiting, or the server-side hiccups it
# occasionally throws as a plain "500 INTERNAL") are worth a few automatic
# retries - the same request usually succeeds a couple seconds later.
# Anything else (bad request, invalid model, auth failure, etc.) will just
# fail the same way again, so it's raised immediately instead.
MAX_GEMINI_ATTEMPTS = 5
GEMINI_RETRY_DELAY_SECONDS = 2


def get_gemini_api_keys():
    """Collect Gemini API keys from env (preset list + optional single key)."""
    keys = []

    preset_keys_env = os.environ.get("GEMINI_PRESET_KEYS", "")
    if preset_keys_env:
        keys.extend(k.strip() for k in preset_keys_env.split(",") if k.strip())

    single_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if single_key and single_key.strip() not in keys:
        keys.append(single_key.strip())

    return keys


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
#   - delay (float): seconds to sleep before the next attempt
#   - rotate_key (bool): pick a different configured API key for the next
#     attempt rather than reusing the one that just failed. Used for
#     capacity/rate-limit errors, where the same key would likely just
#     fail the same way again; transient server-side errors aren't
#     key-related, so they retry with the same key.
# Returning None means "don't retry this - raise immediately" (e.g. bad
# request, invalid model, auth failure - these fail the same way every
# time, so retrying wastes the attempt budget).

def _classify_gemini_error(exc):
    """Decide whether/how to retry a failed Gemini call. Returns a retry
    action dict (see policy comment above) or None to raise immediately."""
    code = _gemini_error_code(exc)

    # 429 - per-key rate limit / capacity exhausted. Rotate to a
    # different configured key so the next attempt isn't just hitting
    # the same limit again.
    if code == 429:
        return {"rotate_key": True, "delay": GEMINI_RETRY_DELAY_SECONDS}

    # 5xx - transient, server-side hiccup (e.g. the plain "500 INTERNAL"
    # Gemini occasionally throws) unrelated to which key was used, so the
    # same key is fine to retry with.
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

    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity()
    conn_str = resolve_conn_str(data.get('database_url'), user_identity)

    history = data.get('history', [])[-20:]
    force_schema_refresh = bool(data.get('refresh_schema'))

    try:
        schema = get_database_schema(conn_str, user_identity, force_refresh=force_schema_refresh)
        client = genai.Client(api_key=api_key)

        system_instruction = (
            "You are an expert SQL generation assistant for PostgreSQL-compatible RDBMSs.\n"
            "Given the prpvided past chat interactions, the database schema and the user's natural language prompt, translate the request into SQL.\n"
            "You may return one or more independent SQL statements. You may use PL/pgSQL Functions or Procedures, if appropriate.\n"
            "Format the result data to be easily readable. For example, format timestamps as date:hour:min:sec.\n"
            "Return ONLY the raw SQL code block. Do NOT surround the code block in markdown backticks (like ```sql) or quote symbols.\n"
            "Do NOT include explanations or other text. Just the executable SQL statement itself.\n"
            "If you can respond to the prompt succinctly based on your general-purpose training, return your response prepended by the string '*** NO SQL ***'\n"
            "If the prompt is about this app itself (yDyL) respond as follows: '*** NO SQL *** OPEN HELP POPUP ***'\n"
            "If you cannot respond at all with reasonable confidence, return '*** NO SQL *** I am not able to respond to your prompt.'\n"
            "If you run into any error, return '*** NO SQL *** I ran into this error: <the error>'\n"
            "If you can split the prompt and handle part of it based on the database and part from general knowledge do that using separate queries for each part. Do not attempt to join the result sets.\n"
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
                    logger.warning(
                        "Gemini call failed (attempt %d/%d), rotating API key and retrying in %ds: %s",
                        attempt, MAX_GEMINI_ATTEMPTS, retry_action["delay"], e
                    )
                else:
                    logger.warning(
                        "Gemini call failed (attempt %d/%d), retrying in %ds: %s",
                        attempt, MAX_GEMINI_ATTEMPTS, retry_action["delay"], e
                    )

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

        resp = jsonify({
            'success': True,
            'sql': generated_sql,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': total_tokens,
            'thinking_tokens': thinking_tokens,
            'cached_content_tokens': cached_content_tokens,
            'duration': duration
        })
        return apply_session_cookie(resp, session_id)

    except Exception as e:
        logger.exception("Translation failed")
        resp = jsonify({
            'success': False,
            'error': str(e) or f"{type(e).__name__} occurred during translation."
        })
        return apply_session_cookie(resp, session_id), 500