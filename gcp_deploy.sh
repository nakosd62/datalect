#!/bin/bash

gcloud run deploy ydyl --source . --port 3000 --allow-unauthenticated --env-vars-file=env.yaml
