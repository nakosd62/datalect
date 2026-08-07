import sqlite3
import sys

def dump_all_tables(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Get all non-internal table names from the database
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [row[0] for row in cursor.fetchall()]

        if not tables:
            print("No user tables found in the database.")
            return

        for table in tables:
            print(f"\n" + "=" * 50)
            print(f"TABLE: {table}")
            print("=" * 50)

            # Quote table name to handle reserved words or special characters
            cursor.execute(f'SELECT * FROM "{table}"')

            # Fetch column headers
            headers = [desc[0] for desc in cursor.description]
            print(" | ".join(headers))
            print("-" * 50)

            # Fetch and print row data
            rows = cursor.fetchall()
            if not rows:
                print("(0 rows)")
            else:
                for row in rows:
                    print(row)

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    # Pass the database path via command-line argument, or default to 'your_database.db'
    db_file = sys.argv[1] if len(sys.argv) > 1 else "../state/crbot_state.db"
    dump_all_tables(db_file)