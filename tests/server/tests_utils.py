import pytest
from unittest.mock import patch, MagicMock
from server.app import resolve_conn_str, get_database_schema, record_translation

def test_resolve_conn_str_no_mask():
    """Should return connection string as-is if no mask is present."""
    conn_str = "postgresql://user:pass@host:5432/db"
    assert resolve_conn_str(conn_str) == conn_str

def test_resolve_conn_str_with_masking():
    """Should replace **** with password extracted from DEFAULT_CONN."""
    masked = "postgresql://user:****@host:5432/db"
    resolved = resolve_conn_str(masked)
    assert "****" not in resolved
    assert "testpassword" in resolved

@patch("app.get_db_connection")
def test_get_database_schema_success(mock_get_db):
    """Should format database tables and columns correctly into a schema string."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # Mock tables query response
    mock_cursor.fetchall.side_effect = [
        [("users",)],  # Tables query
        [("id", "integer", "NO"), ("name", "text", "YES")]  # Columns query for 'users'
    ]

    schema = get_database_schema()
    assert "Table: users" in schema
    assert "id integer (NOT NULL)" in schema
    assert "name text (NULL)" in schema

@patch("app.get_db_connection")
def test_get_database_schema_handles_error(mock_get_db):
    """Should gracefully handle database errors when fetching schema."""
    mock_get_db.side_effect = Exception("Database connection failed")
    schema = get_database_schema()
    assert schema == "No schema description available."

@patch("app.get_db_connection")
def test_record_translation_success(mock_get_db):
    """Should insert translation record into stats database."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    record_translation(
        conn_str="postgresql://db",
        nl_prompt="Find users",
        sql_command="SELECT * FROM users;",
        gemini_model="gemini-2.5-flash",
        duration=120,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        thinking_tokens=0,
        cached_content_tokens=0
    )

    mock_cursor.execute.assert_called_once()
    assert "INSERT INTO translations" in mock_cursor.execute.call_args[0][0]