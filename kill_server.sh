#!/bin/bash

echo "Terminating any running instances of CRBot..."

# Retrieve port from env, fallback to 3000
PORT=${CRBOT_PORT:-3000}

# 1. Kill the process binding to the server port (Flask)
if command -v lsof &> /dev/null; then
    PID=$(lsof -t -i:$PORT)
    if [ ! -z "$PID" ]; then
        echo "Killing process $PID listening on port $PORT..."
        kill -9 $PID 2>/dev/null
    fi
fi

# 2. Fallback name-based kill for app.py
pkill -9 -f "app.py" 2>/dev/null

# 3. Kill the ngrok tunnel process
pkill -9 -f "ngrok http" 2>/dev/null

# 4. Kill CloudSQL Proxy
pkill -9 -f "cloud-sql-proxy" 2>/dev/null

echo "Done."