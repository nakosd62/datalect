"""
report_routes.py

POST /api/report-issue - lets a user flag either:
  - an "error" report: a raw/uncategorized error execute_routes.py returned
    verbatim (see that module's docstring) - typically a database driver's
    own cryptic message for SQL the model generated incorrectly. Deliberately
    NOT for LLM system errors (translate_routes.py's
    format_llm_error_for_user() output) - those already carry an
    app-authored, human-readable explanation, so reporting one would teach a
    reviewer nothing str(exc) itself wouldn't already have.
  - a "wrong_result" report: a SUCCESSFUL response (a table of rows, a
    plain-text reply, an all-databases summarization) the user believes is
    wrong or misleading.

Both categories are reviewed by the user client-side BEFORE this endpoint is
ever called - see webClient/index.html's #reportIssueModal, which shows
exactly what will be emailed (and lets the user add free-text details)
before Send is clicked. This route trusts that review already happened; it
does no content moderation of its own beyond the length cap below.

Delivery is real SMTP, sent by this app itself (not a mailto: link) - see
app_config.py's ISSUE_REPORT_* env vars for the connection this uses, and
ISSUE_REPORTING_ENABLED for the one flag that gates whether this route (and
the client's Report buttons - see config_routes.py's
'issue_reporting_enabled' field) is active at all. No SMTP SDK dependency -
stdlib smtplib is enough for a single plain-text send to one fixed,
admin-configured recipient.
"""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from flask import Blueprint, request, jsonify

from app_config import (
    log_and_generalize_error,
    ISSUE_REPORTING_ENABLED, ISSUE_REPORT_TO_EMAIL,
    ISSUE_REPORT_SMTP_HOST, ISSUE_REPORT_SMTP_PORT,
    ISSUE_REPORT_SMTP_USERNAME, ISSUE_REPORT_SMTP_PASSWORD,
    ISSUE_REPORT_SMTP_FROM, ISSUE_REPORT_SMTP_USE_TLS,
)
from auth import get_or_create_session_id, get_current_user_identity, apply_session_cookie

report_bp = Blueprint('report', __name__)

# What the client is allowed to send as `category` - anything else is
# rejected outright rather than silently mislabeled in the email subject.
# The dict value is the human-readable label used in both the subject line
# and the body's section headers.
_VALID_CATEGORIES = {
    'error': 'Execution Error',
    'wrong_result': 'Wrong Result',
}

# Hard cap on every free-text field this route embeds into an email body -
# this endpoint is reachable by any authenticated/anonymous session (same
# auth posture as /api/translate, /api/execute), so an absurdly long paste
# (or a deliberately hostile one) can't turn into a multi-megabyte email.
# Generous enough for a real SQL script, a real driver error message, or a
# real table preview - not for someone pasting an entire novel into the
# "additional details" box. Applied per-field, not to the whole body.
_MAX_FIELD_CHARS = 8000


def _truncate(value):
    text = (value or '').strip()
    if len(text) > _MAX_FIELD_CHARS:
        omitted = len(text) - _MAX_FIELD_CHARS
        return text[:_MAX_FIELD_CHARS] + f"\n... [truncated, {omitted} more characters]"
    return text


def _build_email(category_label, payload, reporter_identity):
    """Builds (subject, plain-text body) for one report. `payload` is the
    request's own JSON body (already validated to have a known `category`
    by the caller) - every other field is optional and simply omitted from
    the body when blank, rather than printed as an empty section."""
    subject = f"[Datalect] {category_label} report"
    database_name = (payload.get('database_name') or '').strip()
    if database_name:
        subject += f" - {database_name}"

    lines = [
        f"Category: {category_label}",
        f"Reported by: {reporter_identity or 'unknown'}",
    ]
    provider = (payload.get('provider') or '').strip()
    model = (payload.get('model') or '').strip()
    if provider or model:
        lines.append(f"LLM: {provider} / {model}")
    if database_name:
        lines.append(f"Database/connection: {database_name}")
    lines.append("")

    prompt = _truncate(payload.get('prompt'))
    if prompt:
        lines.append("--- User's question ---")
        lines.append(prompt)
        lines.append("")

    sql = _truncate(payload.get('sql'))
    if sql:
        lines.append("--- Generated SQL ---")
        lines.append(sql)
        lines.append("")

    content = _truncate(payload.get('content'))
    if content:
        lines.append(f"--- {category_label} content (as shown to the user) ---")
        lines.append(content)
        lines.append("")

    details = _truncate(payload.get('details'))
    if details:
        lines.append("--- User's additional details ---")
        lines.append(details)
        lines.append("")

    return subject, "\n".join(lines)


@report_bp.route('/api/report-issue', methods=['POST'])
def report_issue():
    # session_id resolved first and passed into get_current_user_identity()
    # - same pattern as every other route module (see auth.py's docstring
    # on why the order matters).
    session_id = get_or_create_session_id()
    user_identity = get_current_user_identity(session_id)

    if not ISSUE_REPORTING_ENABLED:
        # Belt-and-suspenders: the client is expected to hide the Report
        # buttons entirely once config_routes.py's 'issue_reporting_enabled'
        # comes back False, but a stale page (or a direct API call) could
        # still reach here.
        resp = jsonify({
            'success': False,
            'error': 'Issue reporting is not configured on this server.',
        })
        return apply_session_cookie(resp, session_id), 503

    data = request.get_json(silent=True) or {}
    category = data.get('category')
    if category not in _VALID_CATEGORIES:
        resp = jsonify({
            'success': False,
            'error': "Invalid or missing 'category' - expected 'error' or 'wrong_result'.",
        })
        return apply_session_cookie(resp, session_id), 400

    category_label = _VALID_CATEGORIES[category]
    subject, body = _build_email(category_label, data, user_identity)

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr(("Datalect Issue Reports", ISSUE_REPORT_SMTP_FROM))
    msg['To'] = ISSUE_REPORT_TO_EMAIL
    if user_identity and '@' in user_identity:
        # Lets a reviewer hit "Reply" and land straight in the reporting
        # user's own inbox - only set when the identity actually looks like
        # an email address (an anonymous "anonymous:<session_id>" or local-
        # dev "global" identity never does), so Reply-To always resolves to
        # somewhere real rather than a synthetic, undeliverable address.
        msg['Reply-To'] = user_identity
    msg.set_content(body)

    try:
        with smtplib.SMTP(ISSUE_REPORT_SMTP_HOST, ISSUE_REPORT_SMTP_PORT, timeout=15) as smtp:
            if ISSUE_REPORT_SMTP_USE_TLS:
                smtp.starttls()
            if ISSUE_REPORT_SMTP_USERNAME:
                smtp.login(ISSUE_REPORT_SMTP_USERNAME, ISSUE_REPORT_SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        # Unlike execute_routes.py's deliberate raw-error passthrough, an
        # SMTP send failure here is an app/ops problem, not something the
        # user can act on themselves - generalized the same way every other
        # route in this codebase treats an unexpected server-side failure.
        safe_message = log_and_generalize_error("Failed to send issue report email", e)
        resp = jsonify({'success': False, 'error': safe_message})
        return apply_session_cookie(resp, session_id), 500

    resp = jsonify({'success': True})
    return apply_session_cookie(resp, session_id)
