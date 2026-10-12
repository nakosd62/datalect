import argparse
import os
import sqlite3
import sys
from datetime import datetime
import pandas as pd


# The seven collections/tables this app actually uses. The first six are
# also utils/migrate_firestore_to_default_db.py's own --collections
# default (see that docstring) - schema_cache was missing here for a
# while (this utility predates that collection), so "export everything"
# silently never included it even once the rest of this file was pointed
# at the right database. "shares" is the newer public-share-link table
# (see server/state_store.py's "Public shares" comment / server/share_routes.py)
# and was missing from here the same way until this list was updated for it.
VALID_TABLES = ["translations", "sessions", "db_connections", "chat_history", "llm_usage", "schema_cache", "shares"]


# ==========================================
# Shared Helpers
# ==========================================
def normalize_datetime_columns(df):
    """
    Reformats any timestamp-looking column to 'YYYY-MM-DD HH:MM:SS' so
    exported CSVs are consistent regardless of source:
      - Firestore's created_at/updated_at come back as tz-aware Python
        datetime objects, which pandas would otherwise render with
        microseconds and a UTC offset (e.g. "2026-08-15 23:12:03.456789+00:00").
      - SQLite's created_at/updated_at are already "YYYY-MM-DD HH:MM:SS"
        text via CURRENT_TIMESTAMP, but are normalized too for safety/
        consistency rather than assumed.
    Only columns whose name looks like a timestamp (contains "date",
    "time", or "_at") are considered, so regular text fields (e.g. a
    prompt that happens to contain numbers) are never touched.
    """
    if df.empty:
        return df

    for col in df.columns:
        series = df[col]

        if pd.api.types.is_datetime64_any_dtype(series):
            parsed = series
        elif series.dtype == object:
            if not any(kw in col.lower() for kw in ("date", "time", "_at")):
                continue
            try:
                parsed = pd.to_datetime(series, errors="raise")
            except (ValueError, TypeError):
                continue
        else:
            continue

        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_localize(None)

        df[col] = parsed.dt.strftime("%Y-%m-%d %H:%M:%S")

    return df


def sort_columns_alphabetically(df):
    """
    Returns df with its columns reordered alphabetically by name, so every
    exported CSV's column layout is fully deterministic across separate
    runs - regardless of source, and with no exceptions for "well-known"
    columns like doc_id/_source.

    This matters most for Firestore: pd.DataFrame(list_of_dicts) (see
    fetch_firestore) takes its column order from whichever row it
    processes first, then appends any new keys later rows introduce - and
    neither a document's own field order (doc.to_dict() just reflects
    however the client library happened to deserialize that document's
    fields, not a documented stable ordering) nor its query row order
    (collection.stream() here has no explicit .order()) is guaranteed
    stable between two separate export runs, even against unchanged
    underlying data. SQLite's `SELECT *` column order is already stable
    (schema-defined), so this is a no-op there in practice - but it's
    applied uniformly anyway (fetch_sqlite, fetch_firestore, and the
    unioned output in export_union all call this) so there's a single,
    simple, always-true rule rather than "stable except when it isn't."
    """
    return df[sorted(df.columns)]


# ==========================================
# Firestore Fetch/Export Handlers
# ==========================================
def fetch_firestore(target_table, namespace=None):
    """
    Queries this app's actual Firestore database - the exact same
    google.cloud.firestore client/collection/document API
    server/state_store.py's FirestoreStateStore uses, against the
    project's "(default)" database (no `database=` argument - see
    app_config.py's own Firestore-client comment: this app has exactly
    one Firestore database to talk to) - and returns
    {lowercased_collection_name: DataFrame} for every matching collection
    that has at least one document. Returns {} (and prints diagnostics)
    on any connection/import failure rather than raising, so callers
    (e.g. the union path) can treat "Firestore unavailable" the same as
    "no rows".

    Was previously named fetch_datastore() and implemented with
    google.cloud.datastore.Client (project="grand-cosmos-716",
    database="ydyl") - broken on two independent counts after this app's
    own Firestore migration (see app_config.py's Firestore-client comment
    and utils/migrate_firestore_to_default_db.py's docstring):
    (1) "ydyl" is the OLD, no-longer-written-to database - every bit of
    this app's real state has lived in the project's "(default)" database
    since that migration (done specifically to stop paying for "ydyl" as
    a second billed database), so pointing here read an increasingly
    stale, frozen snapshot rather than anything current. (2) More
    fundamentally: that migration script's own docstring is explicit that
    both "ydyl" and "(default)" are Firestore NATIVE mode databases - the
    same mode this real implementation needs - and GCP's Datastore-mode
    client API does not operate against a Native-mode database at all,
    regardless of which database name it's given. Every call the old
    implementation made would fail outright, caught by its own broad
    except-and-return-{} (same shape still used below), so the failure
    was completely silent: `export_state.sh` with no arguments (--store
    all, the default) exited looking successful while quietly exporting
    zero Firestore rows.

    `namespace` is accepted for backward CLI-flag compatibility but is a
    Datastore-only concept with no Firestore Native equivalent, so it's
    ignored here - every one of this app's own collections lives at the
    top level, ungated by any namespace, so this was never actually
    exercised against real data anyway.
    """
    try:
        from google.cloud import firestore
    except ImportError:
        print("Error: 'google-cloud-firestore' package is required.")
        print("Install it via: python -m pip install google-cloud-firestore")
        return {}

    try:
        client = firestore.Client(project="grand-cosmos-716")

        # Fetch actual collection names in the database.
        existing_collections = [collection.id for collection in client.collections()]

        # Determine target collections (case-insensitive matching).
        if target_table == "all":
            collections_to_export = existing_collections if existing_collections else list(VALID_TABLES)
        else:
            matched = [c for c in existing_collections if c.lower() == target_table.lower()]
            collections_to_export = matched if matched else [target_table]

        collections_to_export = list(dict.fromkeys(collections_to_export))

        results = {}
        for collection_name in collections_to_export:
            docs = list(client.collection(collection_name).stream())

            if not docs:
                continue

            records = []
            for doc in docs:
                data = doc.to_dict() or {}
                data["doc_id"] = doc.id
                records.append(data)

            df = pd.DataFrame(records)
            df = sort_columns_alphabetically(df)

            results[collection_name.lower()] = normalize_datetime_columns(df)

        if not results and existing_collections:
            print(f"[Firestore] No matching records found. Available collections detected in database: {existing_collections}")

        return results

    except Exception as e:
        print(f"[Firestore Error] Failed to export data: {e}")
        return {}


def export_firestore(target_table, output_dir, namespace=None):
    data = fetch_firestore(target_table, namespace=namespace)
    if not data:
        print("[Firestore] No matching records found.")
        return

    for name, df in data.items():
        filename = os.path.join(output_dir, f"{name}.csv")
        df.to_csv(filename, index=False)
        print(f"[Firestore] Exported {len(df)} records from collection '{name}' -> {filename}")


# ==========================================
# SQLite Fetch/Export Handlers
# ==========================================
def fetch_sqlite(db_path, target_table):
    """
    Reads the given SQLite file and returns
    {lowercased_table_name: DataFrame} for every matching table that has
    at least one row. Returns {} (and prints diagnostics) on any
    missing-file/connection failure rather than raising, so callers (e.g.
    the union path) can treat "SQLite unavailable" the same as "no rows".
    """
    if not os.path.exists(db_path):
        print(f"[SQLite Error] Database file not found at path: {db_path}")
        return {}

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        existing_tables = [row[0] for row in cursor.fetchall()]

        if target_table == "all":
            tables_to_export = existing_tables
        else:
            matched = [t for t in existing_tables if t.lower() == target_table.lower()]
            tables_to_export = matched if matched else [target_table]

        if not tables_to_export:
            print("[SQLite] No user tables found to export.")
            conn.close()
            return {}

        results = {}
        for table_name in tables_to_export:
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
                if df.empty:
                    continue
                df = sort_columns_alphabetically(df)
                results[table_name.lower()] = normalize_datetime_columns(df)
            except Exception as e:
                print(f"[SQLite Error] Failed to export table '{table_name}': {e}")

        conn.close()
        return results

    except sqlite3.Error as e:
        print(f"[SQLite Error] Connection error: {e}")
        return {}


def export_sqlite(db_path, target_table, output_dir):
    data = fetch_sqlite(db_path, target_table)
    if not data:
        return

    for name, df in data.items():
        filename = os.path.join(output_dir, f"{name}.csv")
        df.to_csv(filename, index=False)
        print(f"[SQLite] Exported {len(df)} rows from '{name}' -> {filename}")


# ==========================================
# Combined (Firestore + SQLite) Export Handler
# ==========================================
def export_union(target_table, output_dir, db_path, namespace=None):
    """
    Fetches from both stores and, for every table/collection name present
    in either, unions the rows into a single CSV (outer join on columns,
    so a field only present in one store is just left blank for rows from
    the other). A '_source' column records which store each row came
    from, since the same table name in different stores/instances is not
    guaranteed to share row identity.
    """
    firestore_data = fetch_firestore(target_table, namespace=namespace)
    sqlite_data = fetch_sqlite(db_path, target_table)

    table_names = sorted(set(firestore_data) | set(sqlite_data))
    if not table_names:
        print("[Union] No matching records found in either store.")
        return

    for name in table_names:
        frames = []

        if name in firestore_data:
            df = firestore_data[name].copy()
            df.insert(0, "_source", "firestore")
            frames.append(df)

        if name in sqlite_data:
            df = sqlite_data[name].copy()
            df.insert(0, "_source", "sqlite")
            frames.append(df)

        # Individual frames are already alphabetically sorted (both
        # fetch_firestore and fetch_sqlite apply sort_columns_alphabetically
        # themselves), but pd.concat(sort=False) takes the FIRST frame's
        # column order as its base and only appends the other frame's
        # columns that aren't already present - so a column unique to
        # sqlite_data would land after every firestore_data column instead
        # of in true alphabetical position. Re-sorting the combined result
        # is what actually guarantees one global alphabetical order for the
        # union output, regardless of which store contributed which column.
        combined = pd.concat(frames, ignore_index=True, sort=False)
        combined = sort_columns_alphabetically(combined)

        filename = os.path.join(output_dir, f"{name}.csv")
        combined.to_csv(filename, index=False)

        breakdown = ", ".join(f"{len(df)} from {df['_source'].iloc[0]}" for df in frames)
        print(f"[Union] Exported {len(combined)} rows ({breakdown}) for '{name}' -> {filename}")


# ==========================================
# Main Entry Point & CLI Parser
# ==========================================
def main():
    parser = argparse.ArgumentParser(
        description="Export application state from Firestore or SQLite into a timestamped directory."
    )

    parser.add_argument(
        "--store",
        choices=["firestore", "sqlite", "all"],
        default="all",
        help="Specify the storage engine to dump from (firestore, sqlite, or all - unioned "
             "into one CSV per table). Default: 'all'."
    )

    parser.add_argument(
        "--table",
        choices=VALID_TABLES + ["all"],
        default="all",
        help="Target table/collection to export (translations, sessions, db_connections, chat_history, llm_usage, schema_cache, shares, or all). Default: 'all'."
    )

    parser.add_argument(
        "--db-path",
        default="../state/ydyl_state.db",
        help="Path to SQLite database file (only used when --store sqlite or all). Default: '../state/ydyl_state.db'."
    )

    parser.add_argument(
        "--namespace",
        default=None,
        help="Deprecated/unused - Firestore Native has no namespace concept. Kept only so an "
             "old invocation that still passes --namespace doesn't hard-fail on an unknown flag."
    )

    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join("data", timestamp)
    os.makedirs(output_dir, exist_ok=True)
    print(f"Created export directory: {output_dir}\n" + "-" * 50)

    if args.store == "all":
        export_union(args.table, output_dir, args.db_path, namespace=args.namespace)
    elif args.store == "firestore":
        export_firestore(args.table, output_dir, namespace=args.namespace)
    elif args.store == "sqlite":
        export_sqlite(args.db_path, args.table, output_dir)


if __name__ == "__main__":
    main()