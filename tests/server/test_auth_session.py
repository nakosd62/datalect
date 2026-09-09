"""
tests/server/test_auth_session.py

Pure unit tests for auth_session.py's signed session-cookie mechanism -
issue_session_token()/read_session_email()/is_session_signing_configured().
No Flask app or request context needed here (this module doesn't touch
flask.request/g at all) - just os.environ + itsdangerous, so these tests
import auth_session directly rather than going through app_factory/
fresh_import(). `helpers` is imported solely for its module-level sys.path
setup (see that module's own comment on SERVER_DIR) so `import auth_session`
resolves the same way server.py's own bare-name imports do.
"""

import sys

import pytest

import helpers  # noqa: F401 - side effect: adds server/ to sys.path

# Fresh, uncached per test since auth_session._load_serializer() re-reads
# os.environ on every call - no module reload is needed the way
# fresh_import() needs one for app_config.py's import-time side effects.
if "auth_session" in sys.modules:
    del sys.modules["auth_session"]
import auth_session  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_signing_key(monkeypatch):
    """Every test here starts with no key configured, then opts in
    explicitly via monkeypatch.setenv - same hermeticity goal as
    helpers.fresh_import()'s _ENV_VARS_TO_CLEAR, just scoped to this one
    var since this file never touches fresh_import()/app_config.py."""
    monkeypatch.delenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, raising=False)


def test_is_session_signing_configured_false_when_key_unset():
    assert auth_session.is_session_signing_configured() is False


def test_is_session_signing_configured_true_when_key_set(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    assert auth_session.is_session_signing_configured() is True


def test_issue_session_token_returns_none_when_key_unset():
    assert auth_session.issue_session_token("alice@example.com") is None


def test_issue_session_token_returns_none_for_falsy_email(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    assert auth_session.issue_session_token("") is None
    assert auth_session.issue_session_token(None) is None


def test_issue_and_read_roundtrip(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    token = auth_session.issue_session_token("alice@example.com")
    assert token is not None
    assert auth_session.read_session_email(token) == "alice@example.com"


def test_read_session_email_returns_none_for_missing_value(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    assert auth_session.read_session_email(None) is None
    assert auth_session.read_session_email("") is None


def test_read_session_email_returns_none_when_key_unset_even_with_a_token():
    # A token issued earlier (e.g. before the key was rotated out/unset)
    # should just silently stop being honored, not raise.
    assert auth_session.read_session_email("some-opaque-value") is None


def test_read_session_email_rejects_tampered_value(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    token = auth_session.issue_session_token("alice@example.com")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth_session.read_session_email(tampered) is None


def test_read_session_email_rejects_token_signed_under_a_different_key(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "key-one")
    token = auth_session.issue_session_token("alice@example.com")
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "key-two")
    assert auth_session.read_session_email(token) is None


def test_read_session_email_rejects_expired_token(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    serializer = auth_session._load_serializer()
    # Sign a payload with a timestamp far enough in the past to be beyond
    # SESSION_MAX_AGE_SECONDS - itsdangerous embeds the signing time inside
    # the token itself, so loads(..., max_age=...) alone is what enforces
    # this; no separate exp field of this module's own exists to fake.
    old_token = serializer.dumps({"email": "alice@example.com"})
    real_loads = serializer.loads

    def _loads_as_if_ancient(value, max_age=None):
        # Simulate SignatureExpired by asking itsdangerous to check against
        # a negative max_age - itsdangerous's own check is `age > max_age`,
        # and age is always >= 0, so -1 guarantees "expired" regardless of
        # how many whole seconds have actually elapsed since signing (which
        # for a token just signed a moment ago would otherwise be 0, not
        # comfortably past any real max_age). A real "30 days ago" token
        # would behave identically; this just avoids sleeping/mocking
        # wall-clock time.
        return real_loads(value, max_age=-1)

    monkeypatch.setattr(serializer, "loads", _loads_as_if_ancient)
    monkeypatch.setattr(auth_session, "_load_serializer", lambda: serializer)
    assert auth_session.read_session_email(old_token) is None


def test_read_session_email_returns_none_for_non_dict_payload(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    serializer = auth_session._load_serializer()
    token = serializer.dumps(["not", "a", "dict"])
    assert auth_session.read_session_email(token) is None


def test_read_session_email_returns_none_when_dict_has_no_email(monkeypatch):
    monkeypatch.setenv(auth_session.SESSION_SIGNING_KEY_ENV_VAR, "some-key")
    serializer = auth_session._load_serializer()
    token = serializer.dumps({"not_email": "x"})
    assert auth_session.read_session_email(token) is None
