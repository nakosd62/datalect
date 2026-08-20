#!/bin/bash

gcloud run deploy ydyl \
--source . \
--port 3000 --allow-unauthenticated \
--env-vars-file=env.yaml \
--service-account=cloudrun-bigquery-sa@grand-cosmos-716.iam.gserviceaccount.com \
--add-cloudsql-instances=mysql-506101:us-east1:free-trial-first-project