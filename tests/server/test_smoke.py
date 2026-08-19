def test_default_client_serves_index(client):
    resp = client.get('/')
    assert resp.status_code == 200


def test_config_endpoint_responds(client):
    resp = client.get('/api/config')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'configured_databases' in data
