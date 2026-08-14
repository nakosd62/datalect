import argparse
import os
import sqlite3
import sys
from datetime import datetime
import pandas as pd


VALID_TABLES = ["translations", "sessions", "db_connections"]


# ==========================================
# Datastore Export Handler
# ==========================================
def export_datastore(target_table, output_dir, namespace=None):
    try:
        from google.cloud import datastore
    except ImportError:
        print("Error: 'google-cloud-datastore' package is required.")
        print("Install it via: python -m pip install google-cloud-datastore")
        sys.exit(1)

    try:
        client = datastore.Client(project="grand-cosmos-716", database="ydyl")

        # Fetch actual Kind names in database
        kind_query = client.query(kind="__kind__")
        kind_query.keys_only()
        existing_kinds = [
            entity.key.id_or_name
            for entity in kind_query.fetch()
            if entity.key.id_or_name and not entity.key.id_or_name.startswith("__")
        ]

        # Determine target kinds (case-insensitive matching)
        if target_table == "all":
            kinds_to_export = existing_kinds if existing_kinds else ["Translations", "Sessions", "db_connections"]
        else:
            matched = [k for k in existing_kinds if k.lower() == target_table.lower()]
            kinds_to_export = matched if matched else [target_table]

        kinds_to_export = list(dict.fromkeys(kinds_to_export))

        exported_any = False
        for kind in kinds_to_export:
            query = client.query(kind=kind)
            entities = list(query.fetch())

            if not entities:
                continue

            records = []
            for entity in entities:
                data = dict(entity)
                data["doc_id"] = entity.key.id_or_name or entity.key.id
                records.append(data)

            df = pd.DataFrame(records)
            if "doc_id" in df.columns:
                cols = ["doc_id"] + [c for c in df.columns if c != "doc_id"]
                df = df[cols]

            filename = os.path.join(output_dir, f"{kind.lower()}.csv")
            df.to_csv(filename, index=False)
            print(f"[Datastore] Exported {len(df)} records from Kind '{kind}' -> {filename}")
            exported_any = True

        if not exported_any:
            print("[Datastore] No matching records found.")
            if existing_kinds:
                print(f"Available Kinds detected in database: {existing_kinds}")

    except Exception as e:
        print(f"[Datastore Error] Failed to export data: {e}")


# ==========================================
# SQLite Export Handler
# ==========================================
def export_sqlite(db_path, target_table, output_dir):
    if not os.path.exists(db_path):
        print(f"[SQLite Error] Database file not found at path: {db_path}")
        return

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
            return

        for table_name in tables_to_export:
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
                filename = os.path.join(output_dir, f"{table_name.lower()}.csv")
                df.to_csv(filename, index=False)
                print(f"[SQLite] Exported {len(df)} rows from '{table_name}' -> {filename}")
            except Exception as e:
                print(f"[SQLite Error] Failed to export table '{table_name}': {e}")

        conn.close()

    except sqlite3.Error as e:
        print(f"[SQLite Error] Connection error: {e}")


# ==========================================
# Main Entry Point & CLI Parser
# ==========================================
def main():
    parser = argparse.ArgumentParser(
        description="Export application state from Datastore or SQLite into a timestamped directory."
    )

    parser.add_argument(
        "--store",
        choices=["firestore", "sqlite"],
        required=True,
        help="Specify the storage engine to dump from (firestore/datastore or sqlite)."
    )

    parser.add_argument(
        "--table",
        choices=VALID_TABLES + ["all"],
        default="all",
        help="Target table/collection to export (translations, sessions, db_connections, or all). Default: 'all'."
    )

    parser.add_argument(
        "--db-path",
        default="../state/ydyl_state.db",
        help="Path to SQLite database file (only used when --store sqlite). Default: '../state/ydyl_state.db'."
    )

    parser.add_argument(
        "--namespace",
        default=None,
        help="Optional Datastore namespace."
    )

    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"data_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)
    print(f"Created export directory: {output_dir}\n" + "-" * 50)

    if args.store in ["firestore", "datastore"]:
        export_datastore(args.table, output_dir, namespace=args.namespace)
    elif args.store == "sqlite":
        export_sqlite(args.db_path, args.table, output_dir)


if __name__ == "__main__":
    main()