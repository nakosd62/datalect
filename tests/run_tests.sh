#!/bin/bash
set -e

TOP_DIR="/Users/dimitris/Coding/datalect"

# Install/Update requirements
if [ -f "requirements-dev.txt" ]; then
    echo "Installing/checking dev dependencies from requirements-dev.txt..."
    $TOP_DIR/venv/bin/pip install --upgrade pip > /dev/null
    $TOP_DIR/venv/bin/pip install -r $TOP_DIR/requirements-dev.txt > /dev/null
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dev dependencies."
        exit 1
    fi
fi

echo "-----------------"
echo " BACKEND TESTING "
echo "-----------------"
$TOP_DIR/venv/bin/python -m pytest $TOP_DIR/tests/server/

echo "----------------------------"
echo " UTILITY TESTING (utils/)   "
echo "----------------------------"
$TOP_DIR/venv/bin/python -m pytest $TOP_DIR/tests/utils/


echo "--------------------------"
echo " E2E TESTING (Playwright) "
echo "--------------------------"
cd $TOP_DIR/tests/e2e
if [ ! -d "node_modules" ]; then
    echo "Installing e2e dependencies..."
    npm install
    if [ -z "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" ]; then
        npx playwright install chromium
    fi
fi
npx playwright test "$@"

