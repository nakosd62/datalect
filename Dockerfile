# Use an official lightweight Python image
FROM python:3.9-slim

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

# Copy backend files
COPY server/* server/

#  Copy Web frontend files
COPY webClient/* webClient/

# Copy CRDB certificate 
COPY crdb.crt .

# Expose container port (Cloud Run defaults to 8080, but we can configure it)
EXPOSE 3000

# Start the Flask app
CMD ["python", "server/app.py"]
