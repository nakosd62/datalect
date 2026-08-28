FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (optional, but good practice for networking tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    unixodbc \
    odbcinst \
    && rm -rf /var/lib/apt/lists/*

# backends/mongodb_sql.py (MongoDB Atlas SQL Interface) needs pyodbc PLUS
# MongoDB's own ODBC driver binary registered with unixODBC - unlike every
# other dialect in this app, there's no pure-Python driver for it (see that
# module's docstring). `unixodbc` is the driver MANAGER (glibc 2.36 in this
# base image already clears the driver's own glibc 2.34+ requirement) and
# `odbcinst` is the small CLI MongoDB's own docs use to locate/verify the
# config files (https://www.mongodb.com/docs/sql-interface/install-driver/,
# "Install the ODBC Driver" > Ubuntu prerequisites) - both are real, separate
# Debian bookworm packages (this base image's distro), confirmed via
# https://packages.debian.org/bookworm/odbcinst. `unixodbc-dev` (build
# headers) is NOT needed here - pyodbc ships prebuilt manylinux wheels that
# only dynamically link libodbc.so.2 at runtime, they don't compile against
# unixODBC at pip-install time.
#
# CAVEAT: MongoDB's compatibility table only lists "Ubuntu 22.04 (x86_64 and
# arm64)" for this driver - not Debian, which is what python:3.12-slim
# actually is. The driver is a plain dynamically-linked ELF .so with no
# Ubuntu-specific behavior baked in (same glibc-based reasoning as every
# other backend's Dockerfile comment in this file), so it should work here
# too, but this is genuinely outside MongoDB's own tested support matrix -
# worth knowing if something ever behaves oddly and you're troubleshooting
# with MongoDB support.
#
# Hardcoded rather than a --build-arg: this app is deployed via
# `gcloud run deploy --source .` (see gcp_deploy.sh), which builds the image
# on Cloud Build, not local `docker build` - and `gcloud run deploy`'s own
# flag set (--set-build-env-vars et al.) has no documented way to forward a
# value as a Dockerfile ARG/--build-arg the way plain `docker build` does.
# Since this URL isn't a secret (it's just a public tarball location, same
# shape as any other curl-a-release-tarball line in a Dockerfile), hardcoding
# it sidesteps that gap entirely and needs no local Docker install at all -
# `./gcp_deploy.sh` alone is enough. To bump the driver version later, get a
# fresh URL the same way this one was obtained (mongodb.com/try/download/
# odbc-driver, pick "Linux x64", click "Copy link" - not the Download
# button, which starts a browser download instead of handing you the URL)
# and replace it below.
RUN curl -L "https://downloads.mongodb.org/mongosql-odbc-driver/ubuntu2204/2.0.10/release/mongoodbc-2.0.10.tar.gz" --output /tmp/mongoodbc.tar.gz && \
    tar -zxf /tmp/mongoodbc.tar.gz --directory /usr/local/lib && \
    rm /tmp/mongoodbc.tar.gz && \
    printf '[ODBC Drivers]\nMongoDB Atlas SQL ODBC Driver = Installed\n\n[MongoDB Atlas SQL ODBC Driver]\nDriver=/usr/local/lib/mongoodbc/bin/libatsql.so\n' \
        >> /etc/odbcinst.ini
# The tarball extracts to /usr/local/lib/mongoodbc/ (LICENSE, README.MD, and
# a bin/ directory containing libatsql.so) and the odbcinst.ini entry above
# is registered under the exact name "MongoDB Atlas SQL ODBC Driver" - both
# copied verbatim from MongoDB's own docs (link above), not guessed. This is
# also exactly the string this app's connection strings use as
# "DRIVER={MongoDB Atlas SQL ODBC Driver}" (see backends/mongodb_sql.py's
# docstring) - the two have to agree, and they already do.

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
COPY presets_CR.json .
COPY grand-cosmos-716-3afa9cbc32b7.json .

# Copy CRDB certificate
# COPY crdb.crt .

# Expose container port (Cloud Run defaults to 8080, but we can configure it)
EXPOSE 3000

# Start the Flask app
CMD ["python", "server/server.py"]
