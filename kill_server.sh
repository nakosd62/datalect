#!/bin/bash

echo "Terminating any running instances of yDyL Server and its proxies..."

PID=$(lsof -ti tcp:3000 -sTCP:LISTEN)
if [ -n "$PID" ]; then
  kill $PID
  sleep 1
  kill -0 $PID 2>/dev/null && kill -9 $PID   # force it if it's still alive
fi

# pkill -9 -f "server.py" 2>/dev/null
# pkill -9 -f "ngrok http" 2>/dev/null
# pkill -9 -f "cloud-sql-proxy" 2>/dev/null

echo "Done."