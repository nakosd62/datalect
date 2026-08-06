#!/bin/bash

echo "Terminating any running instances of CRBot..."

pkill -9 -f "server.py" 2>/dev/null
pkill -9 -f "ngrok http" 2>/dev/null
pkill -9 -f "cloud-sql-proxy" 2>/dev/null

echo "Done."