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

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)


class PostgresBackend(Backend):
    dialect_name = "PostgreSQL"

    def connect(self, descriptor):
        # connect_timeout bounds only TCP/handshake setup (libpq's own
        # definition of the parameter), never query execution afterwards -
        # see backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring for why
        # a wrong/unreachable host needs to fail fast here rather than
        # hanging on the OS's own (effectively unbounded) TCP connect
        # timeout. Passed as a kwarg alongside the DSN string rather than
        # appended to the URL itself - psycopg2 lets both coexist, and a
        # kwarg here always wins over anything already in descriptor["url"].
        return psycopg2.connect(descriptor["url"], connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)

    def close(self, connection):
        if connection:
            connection.close()

    def cache_key(self, descriptor):
        """username@host:port/dbname, parsed from the connection URL -
        never the URL itself, since that carries the password.

        host:port matters just as much as dbname does: two entirely
        different Postgres servers can easily share both a username and a
        database name (e.g. two "demo"/"mydb" presets pointing at two
        different customers' instances) - without the host/port, both
        would resolve to the same schema_cache.py entry, and whichever
        server's schema got fetched first would silently be served back
        for the *other* server's /api/translate calls too, for up to
        SCHEMA_CACHE_TTL_SECONDS. Username is still included too (not
        redundant with host:port/dbname): two different users against the
        exact same database can legitimately see different
        information_schema results if their grants differ, so a schema
        fetched as one user must not be served back for another.
        Port defaults to Postgres's standard 5432 when the URL omits it
        (e.g. "postgresql://user@host/db") - same default psycopg2/libpq
        themselves fall back to - so an explicit ":5432" and an omitted
        port are correctly treated as the same target, not two."""
        url = (descriptor or {}).get("url")
        if not url:
            return "unknown@unknown"
        try:
            parsed = urlparse(url)
            username = parsed.username or "unknown"
            host = parsed.hostname or "unknown"
            port = parsed.port or 5432
            dbname = parsed.path.lstrip('/')
            if '?' in dbname:
                dbname = dbname.split('?')[0]
            return f"{username}@{host}:{port}/{dbname or 'unknown'}"
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
            # Phase 1: cheap - just the distinct table names, bounded so a
            # schema with an extreme number of tables can't make even this
            # scan unbounded (SCHEMA_MAX_TABLE_NAMES_SCANNED). Grouped into
            # date-shard families (e.g. events_20240101 .. events_20241231
            # -> one "events" family) and capped to SCHEMA_MAX_TABLES
            # entries (see backends/base.py) *before* any column/constraint/
            # index/view/grant/trigger query runs - those all get scoped to
            # this bounded set below, which is what actually keeps schema
            # fetching tractable on a dataset with a huge number of tables,
            # rather than fetching everything and truncating the text after
            # the fact.
            cursor.execute("""
                SELECT DISTINCT c.table_name
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON c.table_name = t.table_name AND c.table_schema = t.table_schema
                WHERE c.table_schema = 'public'
                  AND t.table_type = 'BASE TABLE'
                ORDER BY c.table_name
                LIMIT %s;
            """, (SCHEMA_MAX_TABLE_NAMES_SCANNED,))
            all_table_names = [row[0] for row in cursor.fetchall()]

            if not all_table_names:
                return None

            kept_names, shard_groups = group_date_sharded_tables(all_table_names)
            kept_names, shard_groups, omitted_count = cap_kept_tables(kept_names, shard_groups)
            # Postgres has no wildcard-table query mechanism (unlike
            # BigQuery - see backends/bigquery.py), so a shard family's
            # representative is described under its own real, literal name;
            # the heading below just also explains the naming pattern and
            # member count, so Gemini can construct the literal name for
            # whichever date the user means instead of inventing one.
            shard_by_representative = {
                members[-1]: (prefix, members) for prefix, members in shard_groups.items()
            }

            # 1. Tables and Columns - scoped to the bounded kept_names set.
            cursor.execute("""
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.is_nullable,
                    c.column_default
                FROM information_schema.columns c
                WHERE c.table_schema = 'public'
                  AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position;
            """, (kept_names,))
            columns_data = cursor.fetchall()

            tables = {}
            for table_name, col_name, data_type, is_nullable, col_default in columns_data:
                if table_name not in tables:
                    tables[table_name] = []
                default_str = f" DEFAULT {col_default}" if col_default else ""
                null_str = "NULL" if is_nullable == "YES" else "NOT NULL"
                tables[table_name].append(f"  {col_name} {data_type} {null_str}{default_str}")

            for table_name in kept_names:
                col_defs = tables.get(table_name)
                if not col_defs:
                    continue
                if table_name in shard_by_representative:
                    prefix, members = shard_by_representative[table_name]
                    heading = (
                        f"Table family: {prefix}_<date> ({len(members)} date-sharded tables, "
                        f"e.g. {members[0]} .. {members[-1]}; identical columns in every "
                        f"member - substitute the exact table name for whichever date is "
                        f"meant, following this same naming pattern; never query "
                        f"'{prefix}_<date>' literally)"
                    )
                else:
                    heading = f"Table: {table_name}"
                schema_parts.append(heading + "\n" + "\n".join(col_defs))

            if omitted_count:
                schema_parts.append(
                    f"[... {omitted_count} more table(s)/table-family(ies) not shown - "
                    f"this schema has more than the {SCHEMA_MAX_TABLES}-table summary "
                    f"limit. Ask about a narrower set of tables to see the rest.]"
                )

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
                  AND tc.table_name = ANY(%s)
                ORDER BY tc.table_name, tc.constraint_name;
            """, (kept_names,))
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
                  AND tablename = ANY(%s)
                ORDER BY tablename, indexname;
            """, (kept_names,))
            indexes = cursor.fetchall()
            if indexes:
                idx_lines = [f"  [{row[0]}] {row[1]}: {row[2]}" for row in indexes]
                schema_parts.append("Indexes:\n" + "\n".join(idx_lines))

            # 4. Views - deliberately NOT scoped to kept_names: that set is
            # built exclusively from BASE TABLE names (the phase-1 scan
            # filters t.table_type = 'BASE TABLE'), so no view name could
            # ever appear in it - scoping this query to kept_names would
            # silently return zero views, always. Views are a categorically
            # separate set and aren't subject to the same table-count
            # blowup this whole cap/collapse scheme protects against (date-
            # sharded *view* families aren't a thing BigQuery/Postgres users
            # actually do), so leaving this unbounded is intentional, not
            # an oversight.
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
                  AND table_name = ANY(%s)
                ORDER BY table_name, grantee;
            """, (kept_names,))
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
                WHERE event_object_schema = 'public'
                  AND event_object_table = ANY(%s);
            """, (kept_names,))
            triggers = cursor.fetchall()
            if triggers:
                trig_lines = [f"  [{t[0]}] {t[1]} ({t[2]}): {t[3]}" for t in triggers]
                schema_parts.append("Triggers:\n" + "\n".join(trig_lines))

        if not schema_parts:
            return None
        return cap_schema_text("\n\n".join(schema_parts))

    def execute(self, connection, sql_text):
        connection.autocommit = True

        statements = [s.strip() for s in sqlparse.split(sql_text) if s.strip()]
        results = []

        with connection.cursor() as cursor:
            for stmt in statements:
                stmt_clean = stmt.rstrip(';').strip()
                if not stmt_clean:
                    continue

                try:
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
                except Exception as e:
                    # Don't let a mid-script failure silently drop every
                    # result already collected in `results` - see
                    # SqlExecutionError's docstring in backends/base.py.
                    raise SqlExecutionError(str(e), results, stmt_clean, len(results), len(statements)) from e

        return results