#!/bin/bash

gcloud run deploy ydyl --source . --port 3000 --allow-unauthenticated --env-vars-file=env.yaml \
                       --service-account=cloudrun-sa@grand-cosmos-716.iam.gserviceaccount.com
