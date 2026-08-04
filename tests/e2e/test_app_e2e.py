# tests/e2e/test_app_e2e.py
import pytest
import json
from playwright.sync_api import Page, expect

def test_full_translate_and_execute_flow(page: Page):
    """Test translating prompt and executing SQL using Playwright in Python."""
    
    # 1. Mock the /api/config route
    def handle_config(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "database_name": "testdb",
                "username": "testuser",
                "default_database_url": "postgresql://user:pass@localhost:23456/testdb"
            })
        )

    # 2. Mock the /api/translate route
    def handle_translate(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "success": True,
                "sql": "SELECT id, name FROM users;",
                "model": "gemini-2.5-flash",
                "total_tokens": 35
            })
        )

    # 3. Mock the /api/execute route
    def handle_execute(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "success": True,
                "rowCount": 2,
                "results": [{
                    "columns": ["id", "name"],
                    "rows": [
                        {"id": 1, "name": "Alice"},
                        {"id": 2, "name": "Bob"}
                    ]
                }]
            })
        )

    # Apply network route handlers
    page.route("**/api/config", handle_config)
    page.route("**/api/translate", handle_translate)
    page.route("**/api/execute", handle_execute)

    # Load local app
    page.goto("http://localhost:3000/")

    # Fill natural language prompt
    page.fill("#aiPrompt", "Show all users")
    
    # Click translate and wait for API network response
    with page.expect_response("**/api/translate"):
        page.click("#translateBtn")

    # Click execute button and wait for execution API network response
    with page.expect_response("**/api/execute"):
        page.click("#runBtn")

    # ADD IT HERE: Wait for at least the first row to be rendered in the DOM
    page.locator("table tbody tr").first.wait_for()

    # Verify table results appear
    expect(page.locator("table tbody tr")).to_have_count(3)
    expect(page.locator("text=Alice")).to_be_visible()
    expect(page.locator("text=Bob")).to_be_visible()