
import os
import pytest
from unittest.mock import patch

# Ensure test environment variables are set before importing the app
os.environ["DATABASE_URL"] = "postgresql://postgres:testpassword@localhost:23456/testdb?sslmode=disable"
os.environ["GEMINI_AVAILABLE_MODELS"] = "gemini-3.6-flash,gemini-3.5-flash-lite"
os.environ["GEMINI_PRESET_KEYS"] = "test_api_key"

from server.app import app

@pytest.fixture
def client():
    """Provides a Flask test client with testing mode enabled."""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client