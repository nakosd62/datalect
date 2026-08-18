"""
backends/postgres.py

PostgresBackend: talks to any Postgres-compliant database via psycopg2.
This is a direct extraction of logic that used to live inline in db.py
(schema introspection) and execute_routes.py (the query-execution loop) -
the queries and behavior are unchanged, just moved behind the Backend
interface (see backends/base.py) so the route/dispatch layer no longer
needs to know it's talking to psycopg2 specifically.
"""

from urllib.parse import urlparse

import psycopg2
import sqlparse

from .base import Backend


class PostgresBackend(Backend):
    dialect_name = "PostgreSQL"

    def connect(self, descriptor):
        return psycopg2.connect(descriptor["url"])

    def close(self, connection):
        if connection:
            connection.close()

    def cache_key(self, descriptor):
        """username@dbname, parsed from the connection URL - never the
        URL itself, since that carries the password. Same derivation
        db.py's get_conn_identifier has always used."""
        url = (descriptor or {}).get("url")
        if not url:
            return "unknown@unknown"
        try:
            parsed = urlparse(url)
            username = parsed.username or "unknown"
            dbname = parsed.path.lstrip('/')
            if '?' in dbname:
                dbname = dbname.split('?')[0]
            return f"{username}@{dbname or 'unknown'}"
        except Exception:
            return "unknown@unknown"

    def identity_label(self, connection):
        db_name, username = "Unknown", "Unknown"
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), CURRENT_USER;")
            row = cursor.fetchone()
            if row:
                db_name, username = row[0], row[1]
        return db_name, username

    def get_schema(self, connection):
        schema_parts = []

        with connection.cursor() as cursor:
            # 1. Tables and Columns
            cursor.execute("""
                SELECT 
                    c.table_name, 
                    c.column_name, 
                    c.data_type, 
                    c.is_nullable, 
                    c.column_default
                FROM information_schema.columns c
                JOIN information_schema.tables t 
                  ON c.table_name = t.table_name AND c.table_schema = t.table_schema
                WHERE c.table_schema = 'public' 
                  AND t.table_type = 'BASE TABLE'
                ORDER BY c.table_name, c.ordinal_position;
            """)
            columns_data = cursor.fetchall()

            tables = {}
            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                if table_name not in tables:
                    tables[table_name] = []
                default_str = f" DEFAULT {col_default}" if col_default else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables[table_name].append(f"  {col_name} {data_type} {null_str}{default_str}")

            for table_name, col_defs in tables.items():
                schema_parts.append(f"Table: {table_name}\n" + "\n".join(col_defs))

            # 2. Constraints
            cursor.execute("""
                SELECT 
                    tc.table_name, 
                    tc.constraint_name, 
                    tc.constraint_type,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                LEFT JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.table_schema = 'public'
                ORDER BY tc.table_name, tc.constraint_name;
            """)
            constraints = cursor.fetchall()
            if constraints:
                constraint_lines = []
                for tbl, c_name, c_type, col, f_tbl, f_col in constraints:
                    if c_type == 'FOREIGN KEY':
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col} -> {f_tbl}({f_col})")
                    elif col:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type}): {col}")
                    else:
                        constraint_lines.append(f"  [{tbl}] {c_name} ({c_type})")
                schema_parts.append("Constraints:\n" + "\n".join(constraint_lines))

            # 3. Indexes
            cursor.execute("""
                SELECT 
                    tablename, 
                    indexname, 
                    indexdef 
                FROM pg_indexes 
                WHERE schemaname = 'public'
                ORDER BY tablename, indexname;
            """)
            indexes = cursor.fetchall()
            if indexes:
                idx_lines = [f"  [{row[0]}] {row[1]}: {row[2]}" for row in indexes]
                schema_parts.append("Indexes:\n" + "\n".join(idx_lines))

            # 4. Views
            cursor.execute("""
                SELECT 
                    table_name, 
                    view_definition 
                FROM information_schema.views 
                WHERE table_schema = 'public';
            """)
            views = cursor.fetchall()
            if views:
                view_lines = [f"  View {v[0]}: {v[1].strip()}" for v in views]
                schema_parts.append("Views:\n" + "\n".join(view_lines))

            # 5. Role Grants
            cursor.execute("""
                SELECT 
                    grantee, 
                    table_name, 
                    privilege_type 
                FROM information_schema.role_table_grants 
                WHERE table_schema = 'public'
                ORDER BY table_name, grantee;
            """)
            grants = cursor.fetchall()
            if grants:
                grant_lines = [f"  Grant {g[2]} on {g[1]} to {g[0]}" for g in grants]
                schema_parts.append("Grants:\n" + "\n".join(grant_lines))

            # 6. Triggers
            cursor.execute("""
                SELECT 
                    event_object_table, 
                    trigger_name, 
                    event_manipulation, 
                    action_statement 
                FROM information_schema.triggers 
                WHERE event_object_schema = 'public';
            """)
            triggers = cursor.fetchall()
            if triggers:
                trig_lines = [f"  [{t[0]}] {t[1]} ({t[2]}): {t[3]}" for t in triggers]
                schema_parts.append("Triggers:\n" + "\n".join(trig_lines))

        return "\n\n".join(schema_parts) if schema_parts else None

    def execute(self, connection, sql_text):
        connection.autocommit = True

        statements = [s.strip() for s in sqlparse.split(sql_text) if s.strip()]
        results = []

        with connection.cursor() as cursor:
            for stmt in statements:
                stmt_clean = stmt.rstrip(';').strip()
                if not stmt_clean:
                    continue

                cursor.execute(stmt_clean)
                row_count = cursor.rowcount

                columns = None
                rows = None

                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = []
                    for r in cursor.fetchall():
                        row_dict = {}
                        for idx, col in enumerate(columns):
                            val = r[idx]
                            if hasattr(val, 'isoformat'):
                                val = val.isoformat()
                            elif hasattr(val, 'to_eng_string'):
                                val = float(val)
                            elif isinstance(val, bytes):
                                val = val.decode('utf-8', errors='replace')
                            elif type(val).__name__ == 'Decimal':
                                val = float(val)
                            row_dict[col] = val
                        rows.append(row_dict)
                    count = len(rows)
                else:
                    count = row_count if row_count >= 0 else 0

                results.append({
                    'statement': stmt_clean,
                    'columns': columns,
                    'rows': rows,
                    'rowCount': count
                })

        return results