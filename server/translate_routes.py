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

from app_config import DEFAULT_MODEL, logger, log_and_generalize_error
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie
from db import resolve_conn_str, get_database_schema, record_translation

translate_bp = Blueprint('translate', __name__)


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


def pick_gemini_api_key():
    keys = get_gemini_api_keys()
    if not keys:
        return None
    return random.choice(keys)


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
    api_key = pick_gemini_api_key()

    if not api_key:
        return jsonify({'error': 'Gemini API key is not configured.'}), 400

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
        response = client.models.generate_content(
            model=gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1
            )
        )
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
        safe_message = log_and_generalize_error("Translation failed", e)
        resp = jsonify({
            'success': False,
            'error': safe_message
        })
        return apply_session_cookie(resp, session_id), 500