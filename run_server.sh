#!/bin/bash

# Load environment variables from .env if present
if [ -f .env ]; then
    echo "Loading environment configuration from .env..."
    set -a
    source .env
    set +a
else
    echo "Error: .env file not found. Please create one with your required variables."
    exit 1
fi

# Setup Virtual Environment if missing
if [ ! -d "venv" ]; then
    echo "Virtual environment (venv) not found. Creating one..."
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create virtual environment. Make sure python3 is installed."
        exit 1
    fi
fi

# Install/Update requirements
if [ -f "requirements.txt" ]; then
    echo "Installing/checking dependencies from requirements.txt..."
    ./venv/bin/pip install --upgrade pip > /dev/null
    ./venv/bin/pip install -r requirements.txt > /dev/null
    if [ $? -ne 0 ]; then
        echo "Error: Failed to install dependencies."
        exit 1
    fi
fi

# Kill prior server instances
echo "Stopping any previous instances of the server..."
pkill -9 -f "server.py" 2>/dev/null
pkill -9 -f "cloud-sql-proxy" 2>/dev/null
# pkill -9 -f "ngrok http" 2>/dev/null
sleep 2

# Run the Cloud SQL Auth Proxy to access GCP CloudSQL databases
cloud-sql-proxy grand-cosmos-716:us-east1:trial > cloudsql-postgres-proxy.log 2>&1 &
cloud-sql-proxy mysql-506101:us-east1:free-trial-first-project > cloudsql-mysql-proxy.log 2>&1 &

# Start server
echo "Starting Flask server..."
nohup ./venv/bin/python3 -u server/server.py > server.log 2>&1 &
# sleep 2

# echo "Starting ngrok..."
# nohup ngrok http 127.0.0.1:$CRBOT_PORT > ngrok.log 2>&1 &

# Done
echo "Server started. Tail server.log for standard output / error."


