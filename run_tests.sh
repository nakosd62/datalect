#!/bin/bash
set -e

# Install/Update requirements
if [ -f "requirements-dev.txt" ]; then
    echo "Installing/checking dev dependencies from requirements-dev.txt..."
    ./venv/bin/pip install --upgrade pip > /dev/null
    ./venv/bin/pip install -r requirements-dev.txt > /dev/null
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dev dependencies."
        exit 1
    fi
fi

echo "-----------------"
echo " BACKEND TESTING "
echo "-----------------"
./venv/bin/python -m pytest tests/server/


echo "--------------------------"
echo " E2E TESTING (Playwright) "
echo "--------------------------"
cd tests/e2e
if [ ! -d "node_modules" ]; then
    echo "Installing e2e dependencies..."
    npm install
    if [ -z "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" ]; then
        npx playwright install chromium
    fi
fi
npx playwright test "$@"

