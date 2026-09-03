"""
test_report_routes.py

POST /api/report-issue (report_routes.py) and the one field it adds to GET
/api/config (config_routes.py's 'issue_reporting_enabled' - see that
module's comment on it). Patches report_routes.smtplib.SMTP with a fake
that records every call instead of opening a real SMTP connection - no test
here ever sends a real email.
"""

import pytest

from helpers import login_as


ISSUE_REPORT_ENV = {
    "ISSUE_REPORT_TO_EMAIL": "reviewer@example.com",
    "ISSUE_REPORT_SMTP_HOST": "smtp.example.com",
    "ISSUE_REPORT_SMTP_PORT": "587",
    "ISSUE_REPORT_SMTP_USERNAME": "bot@example.com",
    "ISSUE_REPORT_SMTP_PASSWORD": "hunter2",
}


class _FakeSmtp:
    """Records every call made through the `with smtplib.SMTP(...) as smtp:`
    context manager in report_routes.report_issue() - constructor args,
    starttls()/login()/send_message() calls - instead of opening a real
    connection. `raise_on_send`, when set, is raised from send_message()
    to simulate a delivery failure (auth rejected, connection refused,
    etc.) after a successful connect."""

    instances = []

    def __init__(self, host, port, timeout=None, raise_on_send=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_calls = []
        self.sent_messages = []
        self._raise_on_send = raise_on_send
        _FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, msg):
        if self._raise_on_send:
            raise self._raise_on_send
        self.sent_messages.append(msg)


@pytest.fixture
def smtp_harness(monkeypatch):
    """Patches report_routes.smtplib.SMTP with a fake constructor - must be
    called AFTER app_factory/app_env (same ordering caveat every other
    fake-connect harness in this suite has), since it patches the
    already-imported report_routes module's own `smtplib` reference.
    Returns a callable(raise_on_send=None) that installs the fake and
    returns the (empty, not-yet-constructed) _FakeSmtp.instances list -
    inspect instances[-1] after the request to see what that call did."""
    _FakeSmtp.instances = []

    def install(raise_on_send=None):
        import report_routes

        def fake_smtp_ctor(host, port, timeout=None):
            return _FakeSmtp(host, port, timeout=timeout, raise_on_send=raise_on_send)

        monkeypatch.setattr(report_routes.smtplib, "SMTP", fake_smtp_ctor)
        return _FakeSmtp.instances

    return install


def test_config_reports_issue_reporting_disabled_by_default(app_env):
    resp = app_env.client.get("/api/config")
    assert resp.get_json()["issue_reporting_enabled"] is False


def test_config_reports_issue_reporting_enabled_once_configured(app_factory):
    env = app_factory(env=ISSUE_REPORT_ENV)
    resp = env.client.get("/api/config")
    assert resp.get_json()["issue_reporting_enabled"] is True


def test_config_issue_reporting_requires_recipient_and_smtp_host(app_factory):
    # Missing ISSUE_REPORT_TO_EMAIL - SMTP fully configured otherwise.
    env = app_factory(env={k: v for k, v in ISSUE_REPORT_ENV.items() if k != "ISSUE_REPORT_TO_EMAIL"})
    assert env.client.get("/api/config").get_json()["issue_reporting_enabled"] is False


def test_report_issue_returns_503_when_not_configured(app_env, smtp_harness):
    instances = smtp_harness()
    resp = app_env.client.post("/api/report-issue", json={"category": "error"})
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["success"] is False
    assert instances == []  # never even tried to connect


def test_report_issue_rejects_missing_category(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    smtp_harness()
    resp = env.client.post("/api/report-issue", json={})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_report_issue_rejects_unknown_category(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    smtp_harness()
    resp = env.client.post("/api/report-issue", json={"category": "something_else"})
    assert resp.status_code == 400


def test_report_issue_error_category_sends_expected_email(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    instances = smtp_harness()

    resp = env.client.post("/api/report-issue", json={
        "category": "error",
        "prompt": "How many orders were placed last week?",
        "sql": "SELECT * FROM ordrs;",
        "database_name": "E-Commerce Store",
        "provider": "google",
        "model": "gemini-3.6-flash",
        "content": 'relation "ordrs" does not exist',
        "details": "I think the model misspelled the table name.",
    })

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    assert len(instances) == 1
    smtp = instances[0]
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.starttls_called is True  # ISSUE_REPORT_SMTP_USE_TLS defaults to on
    assert smtp.login_calls == [("bot@example.com", "hunter2")]
    assert len(smtp.sent_messages) == 1

    msg = smtp.sent_messages[0]
    assert msg["To"] == "reviewer@example.com"
    assert "bot@example.com" in msg["From"]
    assert "Execution Error" in msg["Subject"]
    assert "E-Commerce Store" in msg["Subject"]

    body = msg.get_content()
    assert "Category: Execution Error" in body
    assert "How many orders were placed last week?" in body
    assert "SELECT * FROM ordrs;" in body
    assert 'relation "ordrs" does not exist' in body
    assert "I think the model misspelled the table name." in body
    assert "google / gemini-3.6-flash" in body


def test_report_issue_wrong_result_category_labels_subject_and_body(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    instances = smtp_harness()

    resp = env.client.post("/api/report-issue", json={
        "category": "wrong_result",
        "prompt": "Summarize sales trends",
        "content": "Sales grew 400% (this looks wrong).",
    })

    assert resp.status_code == 200
    msg = instances[0].sent_messages[0]
    assert "Wrong Result" in msg["Subject"]
    body = msg.get_content()
    assert "Category: Wrong Result" in body
    assert "Sales grew 400%" in body


def test_report_issue_sets_reply_to_for_authenticated_email_identity(app_factory, smtp_harness):
    env = app_factory(env=dict(ISSUE_REPORT_ENV, GOOGLE_CLIENT_ID="fake-client-id"))
    instances = smtp_harness()
    login_as(env.client, "someone@example.com")

    env.client.post("/api/report-issue", json={"category": "error", "content": "boom"})

    msg = instances[0].sent_messages[0]
    assert msg["Reply-To"] == "someone@example.com"
    assert "someone@example.com" in msg.get_content()


def test_report_issue_omits_reply_to_for_non_email_identity(app_factory, smtp_harness):
    # Local-dev default identity (no GOOGLE_CLIENT_ID/IS_CLOUD_RUN, no
    # cookie set) is the literal string "global" - not an email address -
    # so Reply-To must never be set to it.
    env = app_factory(env=ISSUE_REPORT_ENV)
    instances = smtp_harness()

    env.client.post("/api/report-issue", json={"category": "error", "content": "boom"})

    msg = instances[0].sent_messages[0]
    assert msg["Reply-To"] is None


def test_report_issue_smtp_failure_returns_generalized_error(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    smtp_harness(raise_on_send=Exception("Connection refused"))

    resp = env.client.post("/api/report-issue", json={"category": "error", "content": "boom"})

    assert resp.status_code == 500
    data = resp.get_json()
    assert data["success"] is False
    # The real exception text must never leak to the client - unlike
    # execute_routes.py's deliberate raw-error passthrough, this is an
    # app/ops failure, not something the user can act on.
    assert "Connection refused" not in data["error"]


def test_report_issue_skips_starttls_and_login_when_not_configured(app_factory, smtp_harness):
    env_vars = {
        "ISSUE_REPORT_TO_EMAIL": "reviewer@example.com",
        "ISSUE_REPORT_SMTP_HOST": "smtp.example.com",
        "ISSUE_REPORT_SMTP_USE_TLS": "0",
        # No username/password - some internal relays allow anonymous
        # submission from trusted IPs (see app_config.py's comment). A
        # from-address still has to come from somewhere in that case, since
        # it can no longer fall back to the (unset) username.
        "ISSUE_REPORT_SMTP_FROM": "reports@example.com",
    }
    env = app_factory(env=env_vars)
    instances = smtp_harness()

    resp = env.client.post("/api/report-issue", json={"category": "error", "content": "boom"})

    assert resp.status_code == 200
    smtp = instances[0]
    assert smtp.starttls_called is False
    assert smtp.login_calls == []


def test_report_issue_truncates_overly_long_fields(app_factory, smtp_harness):
    env = app_factory(env=ISSUE_REPORT_ENV)
    instances = smtp_harness()

    huge_sql = "SELECT 1; -- " + ("x" * 9000)
    resp = env.client.post("/api/report-issue", json={"category": "error", "sql": huge_sql, "content": "boom"})

    assert resp.status_code == 200
    body = instances[0].sent_messages[0].get_content()
    assert "truncated" in body
    assert len(body) < len(huge_sql) + 2000
