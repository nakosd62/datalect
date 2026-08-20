FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (optional, but good practice for networking tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy code
COPY ./server ./server
COPY ./webClient ./webClient

# Copy the admin database-presets file (see DATABASE_PRESETS_FILE in
# app_config.py/README.md) - it's gitignored like env.yaml, so it must
# exist locally (even as an empty "[]") before building this image, same
# precondition gcp_deploy.sh already has for env.yaml.
COPY database_presets_CR.json ./database_presets_CR.json

# Copy CRDB certificate
# COPY crdb.crt .

# Expose container port (Cloud Run defaults to 8080, but we can configure it)
EXPOSE 3000

# Start the Flask app
CMD ["python", "server/server.py"]
