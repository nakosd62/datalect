"""
chat_history_routes.py: /api/chat-history, /api/chat-history/save, and
/api/chat-history/activate. Persists the actual chat/turn-navigation
conversation state (client.js's chatStoresByBucket) - distinct from the
"translations" table/collection, which is the separate NL->SQL audit log
(write-only now - see chat_history_routes.py's own module docstring for
why its old read/purge endpoints were removed).

Same "works for any identity, including a genuinely anonymous one" posture
as everywhere else in this app - auth.py's per-session
ANONYMOUS_USER_ID_PREFIX identity already isolates one anonymous visitor's
data from every other's, so there's no separate sign-in gate here either.
"""

from helpers import login_as


def test_get_chat_history_success_for_identified_user(client):
    login_as(client, "alice@example.com")
    resp = client.get('/api/chat-history')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['buckets'] == {}
    assert data['active_bucket_key'] == ""


def test_save_then_get_round_trips_a_bucket(client):
    login_as(client, "alice@example.com")
    turns = [{"role": "user", "text": "show users"}, {"role": "model", "text": "SELECT * FROM users;"}]
    resp = client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": turns})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    after = client.get('/api/chat-history').get_json()
    assert after['buckets'] == {"preset:1": turns}


def test_save_requires_bucket_key(client):
    login_as(client, "alice@example.com")
    resp = client.post('/api/chat-history/save', json={"turns": []})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_save_requires_turns_to_be_a_list(client):
    login_as(client, "alice@example.com")
    resp = client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": "not a list"})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_activate_then_get_reflects_the_active_bucket(client):
    login_as(client, "alice@example.com")
    resp = client.post('/api/chat-history/activate', json={"bucket_key": "preset:1"})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    after = client.get('/api/chat-history').get_json()
    assert after['active_bucket_key'] == "preset:1"


def test_activate_requires_bucket_key(client):
    login_as(client, "alice@example.com")
    resp = client.post('/api/chat-history/activate', json={})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False


def test_activate_never_touches_a_bucket_own_saved_turns(client):
    login_as(client, "alice@example.com")
    turns = [{"role": "user", "text": "a"}]
    client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": turns})
    client.post('/api/chat-history/activate', json={"bucket_key": "all"})

    after = client.get('/api/chat-history').get_json()
    assert after['buckets'] == {"preset:1": turns}
    assert after['active_bucket_key'] == "all"


def test_get_chat_history_rejected_for_local_global_identity_is_not_anonymous(client):
    resp = client.get('/api/chat-history')
    assert resp.status_code == 200


def test_anonymous_visitor_can_read_and_save_their_own_chat_history(app_factory):
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    resp = env.client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": [{"role": "user", "text": "a"}]})
    assert resp.status_code == 200

    after = env.client.get('/api/chat-history').get_json()
    assert after['buckets'] == {"preset:1": [{"role": "user", "text": "a"}]}


def test_two_anonymous_visitors_have_isolated_chat_history(app_factory):
    # Mirrors test_config_custom_connections.py's own "two anonymous
    # visitors" tests - chat history is keyed by the same per-session
    # anonymous:<session_id> identity as DB selection/custom connections, so
    # it's isolated the same way.
    env = app_factory(env={"GOOGLE_CLIENT_ID": "fake-client-id.apps.googleusercontent.com"})
    browser_one = env.app_config.app.test_client()
    browser_two = env.app_config.app.test_client()

    browser_one.get('/api/config')  # mints browser_one's own session cookie
    browser_two.get('/api/config')

    browser_one.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": [{"role": "user", "text": "a"}]})

    assert browser_one.get('/api/chat-history').get_json()['buckets'] == {"preset:1": [{"role": "user", "text": "a"}]}
    assert browser_two.get('/api/chat-history').get_json()['buckets'] == {}


def test_isolated_per_authenticated_user(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": [{"role": "user", "text": "a"}]})

    login_as(app_env.client, "bob@example.com")
    bob_history = app_env.client.get('/api/chat-history').get_json()
    assert bob_history['buckets'] == {}


def test_get_chat_history_handles_state_store_exception_gracefully(app_env, monkeypatch):
    def boom(user_id):
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "get_chat_history", boom)
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.get('/api/chat-history')
    assert resp.status_code == 500
    data = resp.get_json()
    assert data['success'] is False
    assert "db is on fire" not in data['error']


def test_save_chat_history_handles_state_store_exception_gracefully(app_env, monkeypatch):
    def boom(user_id, bucket_key, turns):
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "save_chat_bucket", boom)
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/chat-history/save', json={"bucket_key": "preset:1", "turns": []})
    assert resp.status_code == 500
    data = resp.get_json()
    assert data['success'] is False
    assert "db is on fire" not in data['error']


def test_activate_chat_history_handles_state_store_exception_gracefully(app_env, monkeypatch):
    def boom(user_id, bucket_key):
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "set_active_chat_bucket", boom)
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.post('/api/chat-history/activate', json={"bucket_key": "preset:1"})
    assert resp.status_code == 500
    data = resp.get_json()
    assert data['success'] is False
    assert "db is on fire" not in data['error']


# --- GET /api/chat-history/summary ----------------------------------------
# Powers the History modal's "which databases have saved turns" list - see
# this endpoint's own docstring in chat_history_routes.py. app_env's
# default CONFIGURED_DBS (no DATABASE_PRESETS_FILE set) is the single
# synthetic fallback preset {"id": "postgres+Default DB", "name":
# "Default DB", "type": "postgres"} - see app_config.py's own comment on
# it - used below as "the one preset that's actually resolvable".

def _push_turns(client, bucket_key, n):
    """Saves a bucket with n (user, model) turn pairs - summary's own
    turn_count is len(turns)//2, so this is the natural unit to seed with."""
    turns = []
    for i in range(n):
        turns.append({"role": "user", "text": f"question {i}"})
        turns.append({"role": "model", "text": f"SELECT {i};"})
    resp = client.post('/api/chat-history/save', json={"bucket_key": bucket_key, "turns": turns})
    assert resp.status_code == 200


def test_summary_empty_for_a_user_with_no_saved_turns(client):
    login_as(client, "alice@example.com")
    resp = client.get('/api/chat-history/summary')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['buckets'] == []


def test_summary_resolves_an_available_preset_by_name_and_type(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "preset:postgres+Default DB", 3)

    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == [{
        'bucket_key': 'preset:postgres+Default DB',
        'turn_count': 3,
        'kind': 'preset',
        'name': 'Default DB',
        'type': 'postgres',
        'available': True,
    }]


def test_summary_marks_a_preset_no_longer_in_config_as_unavailable(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "preset:some-removed-preset-id", 2)

    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == [{
        'bucket_key': 'preset:some-removed-preset-id',
        'turn_count': 2,
        'kind': 'preset',
        'name': None,
        'type': None,
        'available': False,
    }]


def test_summary_resolves_a_saved_custom_connection_by_name_and_type(app_env):
    login_as(app_env.client, "alice@example.com")
    app_env.app_config.state_store.set_db_connections(
        "alice@example.com", "My Warehouse", "bigquery", "bigquery://proj/dataset", db_config={},
    )
    saved = app_env.app_config.state_store.get_db_connections("alice@example.com")
    connection_key = saved[0]['connection_key']
    _push_turns(app_env.client, f"custom:{connection_key}", 1)

    data = app_env.client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == [{
        'bucket_key': f'custom:{connection_key}',
        'turn_count': 1,
        'kind': 'custom',
        'name': 'My Warehouse',
        'type': 'bigquery',
        'available': True,
    }]


def test_summary_marks_a_deleted_custom_connection_as_unavailable(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "custom:some-deleted-connection-key", 4)

    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == [{
        'bucket_key': 'custom:some-deleted-connection-key',
        'turn_count': 4,
        'kind': 'custom',
        'name': None,
        'type': None,
        'available': False,
    }]


def test_summary_never_echoes_the_raw_url_of_an_unsaved_custom_connection(client):
    # The bucket_key's own suffix here IS the user's raw, unsaved
    # connection string (see computeBucketConnectionSuffix()'s docstring in
    # client.js) - for many dialects that can embed a plaintext password,
    # so the summary must resolve it to a generic label, never echo any
    # part of the URL back.
    login_as(client, "alice@example.com")
    sensitive_key = "custom-adhoc:postgresql://admin:hunter2@internal-db.example.com:5432/prod"
    _push_turns(client, sensitive_key, 1)

    data = client.get('/api/chat-history/summary').get_json()
    assert len(data['buckets']) == 1
    entry = data['buckets'][0]
    # bucket_key itself necessarily still carries the raw URL - the delete
    # action (POST /api/chat-history/save with this same key) needs it -
    # what must never happen is any of the RESOLVED DISPLAY fields leaking
    # it back out under a different name.
    assert entry['bucket_key'] == sensitive_key
    assert entry['kind'] == 'custom-adhoc'
    assert entry['name'] is None
    assert entry['type'] is None
    assert entry['available'] is False
    display_fields = {k: v for k, v in entry.items() if k != 'bucket_key'}
    assert 'hunter2' not in str(display_fields)
    assert 'internal-db.example.com' not in str(display_fields)


def test_summary_includes_the_all_databases_bucket_with_a_special_label(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "all", 5)

    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == [{
        'bucket_key': 'all',
        'turn_count': 5,
        'kind': 'all',
        'name': 'All Pre-Configured Datasets (combined)',
        'type': None,
        'available': True,
    }]


def test_summary_omits_a_bucket_that_has_been_cleared_back_to_zero_turns(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "preset:postgres+Default DB", 2)
    _push_turns(client, "preset:postgres+Default DB", 0)  # the "delete" action itself

    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == []


def test_summary_is_isolated_per_authenticated_user(client):
    login_as(client, "alice@example.com")
    _push_turns(client, "preset:postgres+Default DB", 2)

    login_as(client, "bob@example.com")
    data = client.get('/api/chat-history/summary').get_json()
    assert data['buckets'] == []


def test_summary_handles_state_store_exception_gracefully(app_env, monkeypatch):
    def boom(user_id):
        raise Exception("db is on fire")
    monkeypatch.setattr(app_env.app_config.state_store, "get_chat_history", boom)
    login_as(app_env.client, "alice@example.com")
    resp = app_env.client.get('/api/chat-history/summary')
    assert resp.status_code == 500
    data = resp.get_json()
    assert data['success'] is False
    assert "db is on fire" not in data['error']
