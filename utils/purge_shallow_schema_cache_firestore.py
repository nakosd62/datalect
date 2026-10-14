#!/usr/bin/env python3
"""
One-off maintenance script: deletes every leftover "::shallow"-suffixed
schema_cache entry from the production Firestore database.

Background: db.py used to maintain two independent, separately-fetched
and cached schema representations per connection - a "deep" (full Phase 1
+ Phase 2) entry under cache_key, and a "shallow" (Phase 1-only,
catalog-only) entry under cache_key + "::shallow", used by db.py's
build_router_candidate_summaries() for all-dbs triage. That independent
shallow tier was removed entirely (build_router_candidate_summaries() now
derives its compact summary in-memory from the one cached deep entry via
derive_tables_only_schema_text() - see db.py/sql_generation.py's own
docstrings for the full history), so every "::shallow" row in schema_cache
is now pure leftover: nothing in the codebase reads or writes it anymore.

This script was written and tested against the LOCAL SQLite state store
(state/ydyl_state.db - already purged directly via sqlite3) but NOT yet
run against Firestore, because the sandboxed environment this was
authored in has no network path to *.googleapis.com (outbound Google API
calls are blocked by egress policy on both the cloud container and the
linked Mac's sandboxed VM - confirmed by hand, every googleapis.com host
returned a hard 403 at the proxy). Run this yourself, from a shell with
normal internet access and valid GCP credentials for the "grand-cosmos-716"
project.

Usage:
    cd <repo root, i.e. the datalect checkout>
    pip install google-cloud-firestore   # if not already installed
    python3 utils/purge_shallow_schema_cache_firestore.py            # dry run - lists what WOULD be deleted
    python3 utils/purge_shallow_schema_cache_firestore.py --delete   # actually deletes

Credentials: uses Application Default Credentials by default (the same
mechanism firestore.Client(project=...) uses in server/app_config.py) -
`gcloud auth application-default login` once, if you haven't. If you'd
rather use the grand-cosmos-716-3afa9cbc32b7.json service-account key
already sitting in this repo's root, note that it was created as
"ydyl-sheets-reader@grand-cosmos-716.iam.gserviceaccount.com" for reading
Google Sheets data sources, not necessarily with Firestore permissions -
check it has at least Cloud Firestore User (or equivalent read/write) on
this project before relying on it, or just use your own gcloud login
instead, which is simpler if you already have project access.
"""

import argparse
import sys

try:
    from google.cloud import firestore
except ImportError:
    print("Missing dependency: pip install google-cloud-firestore", file=sys.stderr)
    sys.exit(1)

PROJECT_ID = "grand-cosmos-716"
SUFFIX = "::shallow"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete", action="store_true",
        help="Actually delete the matching documents. Without this flag, "
             "only lists what would be deleted (safe to run anytime).",
    )
    parser.add_argument(
        "--project", default=PROJECT_ID,
        help=f"GCP project id (default: {PROJECT_ID!r}).",
    )
    args = parser.parse_args()

    client = firestore.Client(project=args.project)
    collection = client.collection("schema_cache")

    to_delete = []
    for doc in collection.stream():
        data = doc.to_dict() or {}
        cache_key = data.get("cache_key", "")
        if cache_key.endswith(SUFFIX):
            to_delete.append((doc.id, cache_key))

    if not to_delete:
        print("No \"::shallow\"-suffixed schema_cache entries found - nothing to do.")
        return

    print(f"Found {len(to_delete)} \"::shallow\"-suffixed entr{'y' if len(to_delete) == 1 else 'ies'}:")
    for doc_id, cache_key in to_delete:
        print(f"  {doc_id}  ({cache_key})")

    if not args.delete:
        print("\nDry run only - nothing deleted. Re-run with --delete to actually remove these.")
        return

    print("\nDeleting...")
    batch = client.batch()
    for doc_id, _ in to_delete:
        batch.delete(collection.document(doc_id))
    batch.commit()
    print(f"Deleted {len(to_delete)} entr{'y' if len(to_delete) == 1 else 'ies'}.")


if __name__ == "__main__":
    main()
