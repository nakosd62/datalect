import pytest
from unittest.mock import patch, MagicMock

from server.app import redact_connection_url

def test_redact_connection_url_masks_password():
    conn_str = "postgresql://bankclerk:secret@host:26257/kitchenbank?sslmode=verify-full"
    assert redact_connection_url(conn_str) == "postgresql://bankclerk:****@host:26257/kitchenbank?sslmode=verify-full"

def test_redact_connection_url_without_password():
    conn_str = "postgresql://bankclerk@host:26257/kitchenbank"
    assert redact_connection_url(conn_str) == conn_str

@patch("server.app.sqlite3.connect")
def test_record_translation_redacts_connection_url(mock_sqlite_connect):
    """Should store connection URL with password redacted."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_sqlite_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    from server.app import record_translation

    record_translation(
        conn_str="postgresql://user:secret@host:26257/mydb",
        nl_prompt="Find users",
        sql_command="SELECT 1;",
        gemini_model="gemini-3.6-flash",
        duration=120,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        thinking_tokens=0,
        cached_content_tokens=0,
    )

    stored_conn_str = mock_cursor.execute.call_args[0][1][0]
    assert stored_conn_str == "postgresql://user:****@host:26257/mydb"
    assert "secret" not in stored_conn_str

def test_index_route(client):
    """Should serve index.html from root folder."""
    response = client.get('/')
    assert response.status_code == 200

@patch("server.app.get_db_connection")
def test_get_config_route(mock_get_db, client):
    """Should return active database configuration and default settings."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = ("testdb", "testuser")

    response = client.get('/api/config')
    assert response.status_code == 200
    data = response.get_json()
    assert data["database_name"] == "testdb"
    assert data["username"] == "testuser"
    assert "default_database_url" in data

def test_translate_query_missing_api_key(client):
    """Should return 400 if API key is missing."""
    with patch.dict("os.environ", {}, clear=True):
        response = client.post('/api/translate', json={'prompt': 'show users'})
        assert response.status_code == 400
        assert "Gemini API key is not configured." in response.get_json()['error']

def test_translate_query_empty_prompt(client):
    """Should return 400 if prompt is empty."""
    response = client.post('/api/translate', json={'prompt': '   '})
    assert response.status_code == 400
    assert "Prompt cannot be empty" in response.get_json()['error']

@patch("server.app.get_database_schema")
@patch("server.app.genai.Client")
@patch("server.app.record_translation")
def test_translate_query_success_strips_markdown(mock_record, mock_genai_client, mock_schema, client):
    """Should send prompt to Gemini, strip markdown backticks, and return formatted response."""
    mock_schema.return_value = "Table: users"
    
    # Mock Gemini response
    mock_response = MagicMock()
    mock_response.text = "```sql\nSELECT * FROM users;\n```"
    mock_response.usage_metadata.prompt_token_count = 10
    mock_response.usage_metadata.candidates_token_count = 5
    mock_response.usage_metadata.total_token_count = 15
    mock_response.usage_metadata.thoughts_token_count = 0
    mock_response.usage_metadata.cached_content_token_count = 0

    mock_client_instance = MagicMock()
    mock_client_instance.models.generate_content.return_value = mock_response
    mock_genai_client.return_value = mock_client_instance

    payload = {
        'prompt': 'Show all users',
        'history': [{'role': 'user', 'text': 'hi'}]
    }
    
    response = client.post('/api/translate', json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data['success'] is True
    assert data['sql'] == "SELECT * FROM users;"
    assert data['total_tokens'] == 15
    mock_record.assert_called_once()

def test_execute_query_empty_sql(client):
    """Should return 400 if SQL string is missing or empty."""
    response = client.post('/api/execute', json={'sql': ''})
    assert response.status_code == 400
    assert "Query cannot be empty" in response.get_json()['error']

@patch("server.app.get_db_connection")
def test_execute_query_success(mock_get_db, client):
    """Should split and execute SQL statements, converting database types to JSON-friendly formats."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # Setup mock cursor description and fetchall output
    col1 = MagicMock()
    col1.__getitem__.return_value = "id"
    col2 = MagicMock()
    col2.__getitem__.return_value = "name"
    mock_cursor.description = [col1, col2]
    mock_cursor.fetchall.return_value = [(1, "Alice"), (2, "Bob")]

    response = client.post('/api/execute', json={'sql': 'SELECT * FROM users;'})
    assert response.status_code == 200
    data = response.get_json()
    assert data['success'] is True
    assert data['rowCount'] == 2
    assert len(data['results']) == 1
    assert data['results'][0]['rows'] == [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"}
    ]

@patch("server.app.get_db_connection")
def test_execute_query_database_error(mock_get_db, client):
    """Should return HTTP 400 on database execution failure."""
    mock_get_db.side_effect = Exception("Syntax error in SQL")

    response = client.post('/api/execute', json={'sql': 'INVALID SQL;'})
    assert response.status_code == 400
    data = response.get_json()
    assert data['success'] is False
    assert "Syntax error in SQL" in data['error']