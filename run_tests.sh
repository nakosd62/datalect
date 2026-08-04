#!/bin/bash

echo "---------------"
echo "BACKEND TESTING"
echo "---------------"
./venv/bin/python -m pytest tests/server/

echo "----------------"
echo "FRONTEND TESTING"
echo "----------------"
./run_server.sh > /dev/null 2>&1 &
sleep 5
./venv/bin/python -m pytest tests/e2e/ $1  ##(use --headed if needed)
./kill_server.sh > /dev/null 2>&1 &