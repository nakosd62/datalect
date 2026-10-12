"""
test_share_routes.py

POST /api/share and GET /api/share/<share_id> (share_routes.py) - the
header "Share" button's own endpoints. See share_routes.py's own module
docstring for the full feature: a share is a durable, anonymously-readable
snapshot of one turn (prompt/SQL/results/labels), keyed by a short random
share_id; what a VIEWER's browser does with `dataset_ref` once it reads one
back (switch its own session, push the turn into its own history, or leave
a private connection alone) is entirely client-side (webClient/client.js's
maybeRenderSharedView()) and out of scope here - this file only covers the
server's own job: sanitize, persist, and return the snapshot opaquely,
never a credential, never a live query.

NOTE: share_routes.share_bp was missing from this suite's own
fresh_import()/app_factory harness (tests/server/helpers.py) for a while -
every request made against the `client`/`app_env` fixtures 404'd until
that was fixed (see helpers.py's own comment on it) - the same class of
gap VALID_TABLES had for "shares" in utils/export_state.py. Nothing below
would have been exercising the real route at all before that fix.
"""

import json

import pytest

from helpers import login_as


def _sample_payload(**overrides):
    payload = {
        "user_entry": {"prompt": "Top 10 countries by GDP per capita"},
        "model_entry": {"sql": "SELECT * FROM countries;", "summary": "Here you go."},
        "database_name": "World Stats",
        "database_type": "postgres",
        "provider": "google",
        "model": "gemini-3.6-flash",
        "in_scope_mode": "single",
        "dataset_ref": {"kind": "preset", "id": "postgres-world-stats"},
    }
    payload.update(overrides)
    return payload


# ==========================================
# POST /api/share - validation
# ==========================================

def test_create_share_requires_model_entry(app_env):
    resp = app_env.client.post("/api/share", json={"database_name": "World Stats"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_create_share_rejects_non_dict_model_entry(app_env):
    resp = app_env.client.post("/api/share", json={"model_entry": "not an object"})
    assert resp.status_code == 400


def test_create_share_accepts_missing_user_entry(app_env):
    # A single-database turn always has a user_entry (see buildSharePayload()
    # client-side), but the route itself doesn't require it - only
    # model_entry is ever treated as mandatory.
    resp = app_env.client.post("/api/share", json={"model_entry": {"sql": "SELECT 1;"}})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_create_share_rejects_oversized_body(app_env):
    huge_entry = {"sql": "SELECT 1;", "summary": "x" * (5 * 1024 * 1024)}
    resp = app_env.client.post("/api/share", json=_sample_payload(model_entry=huge_entry))
    assert resp.status_code == 413
    assert resp.get_json()["success"] is False


# ==========================================
# POST /api/share - success shape (share_id/url)
# ==========================================

def test_create_share_returns_success_with_share_id_and_url(app_env):
    resp = app_env.client.post("/api/share", json=_sample_payload())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert isinstance(data["share_id"], str)
    # SHARE_ID_ALPHABET/SHARE_ID_LENGTH (state_store.py) - lowercase
    # letters/digits only, with the visually-ambiguous 0/o/1/l/i removed,
    # 12 chars - see share_routes.SHARE_ID_PATH_RE, which every share_id
    # this route ever generates must satisfy.
    assert len(data["share_id"]) == 12
    assert all(c in "abcdefghjkmnpqrstuvwxyz23456789" for c in data["share_id"])
    assert data["url"].endswith(data["share_id"])


def test_create_share_url_uses_request_host_without_public_base_url(app_env):
    resp = app_env.client.post("/api/share", json=_sample_payload())
    data = resp.get_json()
    assert data["url"] == f"http://localhost/{data['share_id']}"


def test_create_share_url_uses_public_base_url_when_configured(app_factory):
    env = app_factory(env={"PUBLIC_BASE_URL": "https://agent.datalect.xyz"})
    resp = env.client.post("/api/share", json=_sample_payload())
    data = resp.get_json()
    assert data["url"] == f"https://agent.datalect.xyz/{data['share_id']}"


def test_create_share_two_calls_return_different_ids(app_env):
    first = app_env.client.post("/api/share", json=_sample_payload()).get_json()
    second = app_env.client.post("/api/share", json=_sample_payload()).get_json()
    assert first["share_id"] != second["share_id"]


# ==========================================
# GET /api/share/<share_id> - round trip
# ==========================================

def test_create_then_get_share_round_trips_every_field(app_env):
    created = app_env.client.post("/api/share", json=_sample_payload()).get_json()
    resp = app_env.client.get(f"/api/share/{created['share_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["user_entry"] == {"prompt": "Top 10 countries by GDP per capita"}
    assert data["model_entry"] == {"sql": "SELECT * FROM countries;", "summary": "Here you go."}
    assert data["database_name"] == "World Stats"
    assert data["database_type"] == "postgres"
    assert data["provider"] == "google"
    assert data["model"] == "gemini-3.6-flash"
    assert data["in_scope_mode"] == "single"
    assert data["dataset_ref"] == {"kind": "preset", "id": "postgres-world-stats"}


def test_get_share_works_with_no_session_cookie_at_all(app_env):
    # A share link is meant to work for anyone holding it, including a
    # visitor with no identity of their own (see share_routes.py's own
    # module docstring and auth.py's EXEMPT_ENDPOINTS entry for
    # 'share.get_share') - this is the normal case for every test in this
    # file (none call login_as), but this test makes that guarantee
    # explicit rather than merely incidental.
    created = app_env.client.post("/api/share", json=_sample_payload()).get_json()
    fresh_client = app_env.app_config.app.test_client()  # its own, cookie-less jar
    resp = fresh_client.get(f"/api/share/{created['share_id']}")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_get_share_unknown_id_is_404(app_env):
    resp = app_env.client.get("/api/share/abcdefghjkmn")  # well-formed, never created
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


@pytest.mark.parametrize("bad_id", [
    "short",                      # under SHARE_ID_PATH_RE's 6-char minimum
    "x" * 25,                     # over its 24-char maximum
    "HASUPPERCASE",               # SHARE_ID_ALPHABET is lowercase-only
    "has spaces12",
    "abc123!@#$%^",
])
def test_get_share_malformed_id_is_404_without_touching_the_store(app_env, monkeypatch, bad_id):
    # SHARE_ID_PATH_RE's own job (see its docstring): reject anything that
    # could never have been generated BEFORE ever asking the state store -
    # verified here by making state_store.get_share raise if it's reached
    # at all, not just by asserting the response code. Every case here is
    # still a single path segment (no "/") so it actually reaches this
    # route's own view function rather than failing Flask's routing a
    # layer earlier (an empty id, or one containing "/", 404s before ever
    # matching the `/api/share/<share_id>` rule at all, which would make
    # this test exercise Flask/Werkzeug's own 404 handling instead of
    # share_routes.py's).
    def _boom(share_id):
        raise AssertionError("state_store.get_share should never be called for a malformed id")

    monkeypatch.setattr(app_env.app_config.state_store, "get_share", _boom)

    resp = app_env.client.get(f"/api/share/{bad_id}")
    assert resp.status_code == 404
    assert resp.get_json()["success"] is False


def test_get_share_response_never_includes_created_by(app_env):
    # created_by is audit-only (see create_share's own docstring) - never
    # meant to be shown to a viewer, whoever they are.
    login_as(app_env.client, "alice@example.com")
    created = app_env.client.post("/api/share", json=_sample_payload()).get_json()
    resp = app_env.client.get(f"/api/share/{created['share_id']}")
    assert "created_by" not in resp.get_json()


# ==========================================
# Label truncation / defaulting
# ==========================================

def test_create_share_truncates_overly_long_labels(app_env):
    huge_name = "x" * 500
    created = app_env.client.post("/api/share", json=_sample_payload(database_name=huge_name)).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    # _MAX_LABEL_CHARS (share_routes.py) is 200.
    assert len(fetched["database_name"]) == 200


def test_create_share_defaults_blank_labels_to_empty_string(app_env):
    created = app_env.client.post("/api/share", json={"model_entry": {"sql": "SELECT 1;"}}).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    assert fetched["database_name"] == ""
    assert fetched["database_type"] == ""
    assert fetched["provider"] == ""
    assert fetched["model"] == ""


def test_create_share_defaults_unrecognized_in_scope_mode_to_single(app_env):
    created = app_env.client.post(
        "/api/share", json=_sample_payload(in_scope_mode="something-bogus")
    ).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    assert fetched["in_scope_mode"] == "single"


def test_create_share_preserves_group_in_scope_mode(app_env):
    created = app_env.client.post(
        "/api/share", json=_sample_payload(in_scope_mode="group", dataset_ref={"kind": "group", "id": "grp-ab"})
    ).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    assert fetched["in_scope_mode"] == "group"
    assert fetched["dataset_ref"] == {"kind": "group", "id": "grp-ab"}


# ==========================================
# dataset_ref sanitization - see share_routes._sanitized_dataset_ref's own
# docstring: a pure UI hint, never an authorization decision, and "assume
# nothing can be switched to" (kind="custom", id=None) is the safe fallback
# for anything this doesn't recognize.
# ==========================================

@pytest.mark.parametrize("sent_ref,expected", [
    ({"kind": "preset", "id": "postgres-world-stats"}, {"kind": "preset", "id": "postgres-world-stats"}),
    ({"kind": "group", "id": "grp-ab"}, {"kind": "group", "id": "grp-ab"}),
    ({"kind": "custom", "id": None}, {"kind": "custom", "id": None}),
    ({"kind": "custom", "id": "some-custom-key"}, {"kind": "custom", "id": None}),  # id ignored for 'custom'
    ({"kind": "bogus", "id": "whatever"}, {"kind": "custom", "id": None}),
    ({"kind": "preset", "id": ""}, {"kind": "custom", "id": None}),   # blank id, not a real reference
    ({"kind": "preset", "id": "   "}, {"kind": "custom", "id": None}),  # whitespace-only id
    ({"kind": "preset"}, {"kind": "custom", "id": None}),             # id missing entirely
    ({"kind": "preset", "id": 12345}, {"kind": "custom", "id": None}),  # id not a string
    (None, {"kind": "custom", "id": None}),
    ("not-a-dict", {"kind": "custom", "id": None}),
    ([], {"kind": "custom", "id": None}),
])
def test_create_share_sanitizes_dataset_ref(app_env, sent_ref, expected):
    body = _sample_payload()
    if sent_ref is None:
        body.pop("dataset_ref", None)
    else:
        body["dataset_ref"] = sent_ref
    created = app_env.client.post("/api/share", json=body).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    assert fetched["dataset_ref"] == expected


def test_create_share_truncates_an_overly_long_dataset_ref_id(app_env):
    huge_id = "x" * 500
    created = app_env.client.post(
        "/api/share", json=_sample_payload(dataset_ref={"kind": "group", "id": huge_id})
    ).get_json()
    fetched = app_env.client.get(f"/api/share/{created['share_id']}").get_json()
    assert fetched["dataset_ref"]["kind"] == "group"
    assert len(fetched["dataset_ref"]["id"]) == 200  # _MAX_LABEL_CHARS, same cap as the plain labels


# ==========================================
# Backward compatibility - a share saved before dataset_ref existed
# ==========================================

def test_get_share_defaults_dataset_ref_for_a_pre_dataset_ref_row(app_env):
    # Simulates a real share_id created by an older build, whose stored
    # payload (schema_version 1, same as every row today - see
    # share_routes.py's own comment on get_share) simply has no
    # "dataset_ref" key at all, rather than a client ever sending a
    # malformed one - see get_share's own comment on why this needs no
    # schema_version bump.
    old_payload = _sample_payload()
    del old_payload["dataset_ref"]
    share_id = app_env.app_config.state_store.create_share(old_payload, "someone")

    resp = app_env.client.get(f"/api/share/{share_id}")
    assert resp.status_code == 200
    assert resp.get_json()["dataset_ref"] == {"kind": "custom", "id": None}


# ==========================================
# Failure paths
# ==========================================

def test_create_share_failure_returns_generalized_error(app_env, monkeypatch):
    def _boom(payload, created_by):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app_env.app_config.state_store, "create_share", _boom)

    resp = app_env.client.post("/api/share", json=_sample_payload())
    assert resp.status_code == 500
    data = resp.get_json()
    assert data["success"] is False
    # The real exception text must never leak to the client - same
    # generalized-error posture test_report_routes.py already covers for
    # report_routes.py's own SMTP failure.
    assert "disk full" not in data["error"]


def test_create_share_returns_500_when_store_returns_no_id(app_env, monkeypatch):
    monkeypatch.setattr(app_env.app_config.state_store, "create_share", lambda payload, created_by: None)
    resp = app_env.client.post("/api/share", json=_sample_payload())
    assert resp.status_code == 500
    assert resp.get_json()["success"] is False


def test_get_share_failure_returns_generalized_error(app_env, monkeypatch):
    def _boom(share_id):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app_env.app_config.state_store, "get_share", _boom)

    resp = app_env.client.get("/api/share/abcdefghjkmn")
    assert resp.status_code == 500
    data = resp.get_json()
    assert data["success"] is False
    assert "disk full" not in data["error"]
