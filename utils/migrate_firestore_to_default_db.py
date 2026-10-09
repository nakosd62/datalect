#!/usr/bin/env python3
"""
utils/migrate_firestore_to_default_db.py

One-off migration tool: copies every document (and any subcollections,
recursively) out of this app's old, separately-billed named Firestore
database ("ydyl") into the project's default database ("(default)"), so
the app can stop paying for a second database it doesn't actually need.

Run this from a machine/shell that's already authenticated against GCP
with a principal that has Firestore read access to the source database
and write access to the target (e.g. your own `gcloud auth
application-default login` session, if your user account has Editor/Owner
or the `roles/datastore.user` role on the project - the same thing
`firestore.Client(...)` in server/app_config.py relies on).

Usage:
    # 1. Dry run first - lists what would be copied, writes nothing:
    python3 utils/migrate_firestore_to_default_db.py --project grand-cosmos-716

    # 2. Once the dry-run counts look right, actually copy the data:
    python3 utils/migrate_firestore_to_default_db.py --project grand-cosmos-716 --execute

    # 3. Re-run any time to re-sync (every write is a .set(), so this is
    #    idempotent - safe to run again, e.g. right before cutover, to
    #    pick up anything written to "ydyl" since the first pass):
    python3 utils/migrate_firestore_to_default_db.py --project grand-cosmos-716 --execute

Flags:
    --project       GCP project ID (default: grand-cosmos-716, this repo's
                     own project - see env.yaml).
    --source-db     Source database ID (default: ydyl).
    --target-db     Target database ID (default: (default)).
    --collections   Comma-separated collection names to copy (default: the
                     six this app actually uses - sessions, db_connections,
                     translations, llm_usage, chat_history, schema_cache).
    --execute       Actually write to the target database. Without this,
                     the script only reads from the source and reports
                     what it WOULD do.
    --force         Required in addition to --execute if the target
                     database already has any documents in a collection
                     being migrated - a safety check against silently
                     merging into/overwriting data that's already there
                     for an unrelated reason.

What this does NOT do:
    - It does not create the target database. If "(default)" doesn't
      exist yet in this project, create it first (Firestore Native mode,
      same location as "ydyl" - see `gcloud firestore databases describe
      --database=ydyl` for ydyl's location, then `gcloud firestore
      databases create --database='(default)' --location=<that location>
      --type=firestore-native`).
    - It does not touch the source database ("ydyl") at all - purely
      reads from it. Deleting it afterwards (to actually stop the extra
      billing) is a separate, deliberate step - see the migration plan.
    - It does not change any application code or redeploy anything.
"""

import argparse
import sys

try:
    from google.cloud import firestore
except ImportError:
    print("Error: 'google-cloud-firestore' package is required.")
    print("Install it via: python3 -m pip install google-cloud-firestore")
    sys.exit(1)


DEFAULT_COLLECTIONS = [
    "sessions",
    "db_connections",
    "translations",
    "llm_usage",
    "chat_history",
    "schema_cache",
]

# Firestore batched writes cap out at 500 operations per commit.
BATCH_SIZE = 400


def rewrite_document_references(value, target_client):
    """
    Recursively walks a document's field data and rewrites any
    firestore.DocumentReference value to point at `target_client` instead
    of whatever client originally produced it (the source database's
    client, if this value came from one of its documents).

    Why this matters: a DocumentReference read off a document carries a
    reference back to the CLIENT (and therefore the database) that read
    it. Writing that reference object as-is into a different database
    would silently leave it pointing at a path in the OLD database rather
    than the new one. None of this app's six collections are expected to
    actually store DocumentReference fields (they're flat state records),
    but this is cheap, correct, and future-proof rather than assuming
    that never changes.
    """
    if isinstance(value, firestore.DocumentReference):
        return target_client.document(value.path)
    if isinstance(value, dict):
        return {k: rewrite_document_references(v, target_client) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_document_references(v, target_client) for v in value]
    return value


def copy_document(src_doc_ref, dst_doc_ref, target_client, batch, counters, execute):
    """
    Copies one document's data, queues it onto `batch`, and recurses into
    any subcollections it has (none of this app's six top-level
    collections currently use subcollections, but this doesn't assume
    that stays true).
    """
    snapshot = src_doc_ref.get()
    if not snapshot.exists:
        return

    data = rewrite_document_references(snapshot.to_dict(), target_client)
    counters["docs_read"] += 1

    if execute:
        batch["batch"].set(dst_doc_ref, data)
        batch["count"] += 1
        counters["docs_written"] += 1
        if batch["count"] >= BATCH_SIZE:
            batch["batch"].commit()
            batch["batch"] = target_client.batch()
            batch["count"] = 0

    for src_subcol in src_doc_ref.collections():
        counters["subcollections_found"] += 1
        for src_subdoc in src_subcol.stream():
            dst_subdoc_ref = dst_doc_ref.collection(src_subcol.id).document(src_subdoc.id)
            copy_document(src_subdoc.reference, dst_subdoc_ref, target_client, batch, counters, execute)


def migrate_collection(source_client, target_client, collection_name, execute, force):
    print(f"\n--- {collection_name} ---")

    if execute:
        existing = list(target_client.collection(collection_name).limit(1).stream())
        if existing and not force:
            print(
                f"REFUSING: target collection '{collection_name}' already has at least one "
                f"document. Pass --force if you're sure migrating into it anyway is correct "
                f"(e.g. you're re-running this after a partial previous run)."
            )
            return {"docs_read": 0, "docs_written": 0, "subcollections_found": 0, "refused": True}

    counters = {"docs_read": 0, "docs_written": 0, "subcollections_found": 0, "refused": False}
    batch = {"batch": target_client.batch() if execute else None, "count": 0}

    src_collection = source_client.collection(collection_name)
    for src_doc in src_collection.stream():
        dst_doc_ref = target_client.collection(collection_name).document(src_doc.id)
        copy_document(src_doc.reference, dst_doc_ref, target_client, batch, counters, execute)

    if execute and batch["count"] > 0:
        batch["batch"].commit()

    verb = "copied" if execute else "would copy"
    print(f"{verb} {counters['docs_read']} document(s)" + (
        f" ({counters['subcollections_found']} subcollection(s) found and included)"
        if counters["subcollections_found"] else ""
    ))
    return counters


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="grand-cosmos-716")
    parser.add_argument("--source-db", default="ydyl")
    parser.add_argument("--target-db", default="(default)")
    parser.add_argument("--collections", default=",".join(DEFAULT_COLLECTIONS))
    parser.add_argument("--execute", action="store_true", help="Actually write. Without this, dry-run only.")
    parser.add_argument("--force", action="store_true", help="Allow writing into a non-empty target collection.")
    args = parser.parse_args()

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]

    print(f"Project:      {args.project}")
    print(f"Source DB:    {args.source_db}")
    print(f"Target DB:    {args.target_db}")
    print(f"Collections:  {', '.join(collections)}")
    print(f"Mode:         {'EXECUTE (writing)' if args.execute else 'DRY RUN (read-only, no writes)'}")

    source_client = firestore.Client(project=args.project, database=args.source_db)
    target_client = firestore.Client(project=args.project, database=args.target_db)

    totals = {"docs_read": 0, "docs_written": 0, "subcollections_found": 0}
    refused = []
    for name in collections:
        result = migrate_collection(source_client, target_client, name, args.execute, args.force)
        if result["refused"]:
            refused.append(name)
        for k in ("docs_read", "docs_written", "subcollections_found"):
            totals[k] += result[k]

    print("\n=== Summary ===")
    print(f"Documents read from '{args.source_db}':    {totals['docs_read']}")
    if args.execute:
        print(f"Documents written to '{args.target_db}':  {totals['docs_written']}")
    if totals["subcollections_found"]:
        print(f"Subcollections found and included:        {totals['subcollections_found']}")
    if refused:
        print(f"\nCollections REFUSED (target not empty, --force not given): {', '.join(refused)}")
        sys.exit(1)
    if not args.execute:
        print("\nThis was a dry run - nothing was written. Re-run with --execute to actually copy.")


if __name__ == "__main__":
    main()
