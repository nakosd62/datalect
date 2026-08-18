#!/bin/bash

gcloud iam service-accounts create ydyl-bq-test \
  --project=grand-cosmos-716 \
  --display-name="yDyL BigQuery test key"

  gcloud projects add-iam-policy-binding grand-cosmos-716 \
  --member="serviceAccount:ydyl-bq-test@grand-cosmos-716.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"

  gcloud iam service-accounts keys create ydyl-bq-test-key.json \
  --iam-account=ydyl-bq-test@grand-cosmos-716.iam.gserviceaccount.com