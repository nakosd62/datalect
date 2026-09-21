#!/bin/bash
set -e

# Always run from this script's own directory (the repo root), regardless of
# where it's invoked from - app_config.py's DATABASE_PRESETS_FILE is a plain
# relative path (./presets.json), resolved against the process's cwd at
# runtime, not against this script's location. Running from any other cwd
# (e.g. `cd tests && ../run_tests.sh`) would silently fail to find
# presets.json, falling back to a single hardcoded default preset and
# logging a DATABASE_DEFAULT-mismatch error on every app_factory() call -
# see the backend section below for why that also breaks unrelated tests.
cd "$(dirname "$0")"

# Install/Update requirements
if [ -f "requirements-dev.txt" ]; then
    echo "Installing/checking dev dependencies from requirements-dev.txt..."
    ./venv/bin/pip install --upgrade pip > /dev/null
    ./venv/bin/pip install -r ./requirements-dev.txt > /dev/null
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dev dependencies."
        exit 1
    fi
fi

echo "-----------------"
echo " BACKEND TESTING "
echo "-----------------"
# YDYL_SKIP_DOTENV=1 keeps this run from picking up the real local .env (see
# app_config.py's own comment on that var) - several tests assert against
# this app's hardcoded defaults (e.g. MAX_IN_SCOPE_CONNECTIONS,
# EXECUTE_RESULTS_MAX_ROWS), which only holds when no .env override is
# loaded. Without this, a real .env tuned for actual deployment (different
# limits, a real DATABASE_DEFAULT, etc.) fails those tests for reasons
# having nothing to do with an actual regression.
YDYL_SKIP_DOTENV=1 ./venv/bin/python -m pytest ./tests/server/

echo "----------------------------"
echo " UTILITY TESTING (utils/)   "
echo "----------------------------"
YDYL_SKIP_DOTENV=1 ./venv/bin/python -m pytest ./tests/utils/


echo "--------------------------"
echo " E2E TESTING (Playwright) "
echo "--------------------------"
cd ./tests/e2e
if [ ! -d "node_modules" ]; then
    echo "Installing e2e dependencies..."
    npm install
    if [ -z "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" ]; then
        npx playwright install chromium
    fi
fi
npx playwright test "$@"

