#!/usr/bin/env bash

set -e

PROJECT_ID="grand-cosmos-716"
SA_NAME="cloudrun-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. Set default project context
gcloud config set project "$PROJECT_ID"

# 2. Create the service account
gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Cloud Run Application Service Account"

# 3. Define required roles for Firestore, Cloud SQL, and BigQuery
ROLES=(
    "roles/datastore.user"     # Firestore access
    "roles/cloudsql.client"     # Cloud SQL access
    "roles/bigquery.jobUser"    # BigQuery query execution
    "roles/bigquery.dataViewer" # BigQuery data read access (use roles/bigquery.dataEditor if write access is needed)
)

# 4. Bind roles to the new service account
for ROLE in "${ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="$ROLE"
done

echo "Service account $SA_EMAIL successfully created and granted privileges."