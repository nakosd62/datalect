"""
backends/postgres.py

PostgresBackend: talks to any Postgres-compliant database via psycopg2.
This is a direct extraction of logic that used to live inline in db.py
(schema introspection) and execute_routes.py (the query-execution loop) -
the queries and behavior are unchanged, just moved behind the Backend
interface (see backends/base.py) so the route/dispatch layer no longer
needs to know it's talking to psycopg2 specifically.

TLS/server-certificate verification ("sslmode", "verify-full", etc.) is
deliberately NOT forced or defaulted here - unlike backends/redshift.py's
unconditional sslmode="require", a user typing their own "?sslmode=..."
query parameter into the connection URL already reaches libpq untouched
(psycopg2.connect() forwards the whole DSN string as-is), so nothing needs
to change for sslmode alone. The one piece that DOES need help from this
module is "verify-ca"/"verify-full": those modes need a CA certificate to
validate the server's certificate against, and libpq's "sslrootcert"
parameter is a filesystem path - not something a user pasting a connection
string into a web form can supply directly unless they also happen to
have filesystem access to wherever this app is actually running. See
"ca_cert_pem" below for how that gap is closed.

A descriptor's optional "schema" field, when present, scopes the connection
to a non-public schema - the exact same mechanism backends/redshift.py's own
"schema" field uses (Redshift IS Postgres, wire-protocol-wise): connect()
runs `SET search_path TO <schema>, public` right after connecting, and
get_schema() below is scoped via current_schema() rather than a hardcoded
'public', so introspection follows wherever that SET actually pointed.
Omitted (the overwhelming common case, and every preset that predates this
field) behaves exactly as before - current_schema() then evaluates to
'public', matching the old hardcoded literal.
"""

import os
from urllib.parse import urlparse, parse_qs

import psycopg2
from psycopg2 import sql
import sqlparse

from .base import (
    Backend, SqlExecutionError, SCHEMA_MAX_TABLE_NAMES_SCANNED, SCHEMA_MAX_TABLES,
    DB_CONNECT_TIMEOUT_SECONDS, materialize_ca_cert_tempfile,
    group_date_sharded_tables, cap_kept_tables, cap_schema_text,
)


def _url_already_specifies_sslrootcert(url):
    """True if `url` already carries its own "?sslrootcert=..." (or the
    connection is otherwise unparseable, in which case this errs toward
    "yes" - i.e. leave descriptor["ca_cert_pem"] unused rather than risk
    fighting a URL this function couldn't even parse). A self-hoster who
    already has a CA cert file sitting on the same machine this app runs
    on can point sslrootcert at that path directly in the URL exactly as
    before - that path always wins over descriptor["ca_cert_pem"], never
    the other way around, so this module never silently overrides a
    connection string the user already fully specified themselves."""
    if not url:
        return False
    try:
        return bool(parse_qs(urlparse(url).query).get("sslrootcert"))
    except Exception:
        return True


class PostgresBackend(Backend):
    dialect_name = "PostgreSQL"

    def connect(self, descriptor):
        descriptor = descriptor or {}
        url = descriptor.get("url")
        ca_cert_pem = descriptor.get("ca_cert_pem")
        schema = descriptor.get("schema") or None

        # connect_timeout bounds only TCP/handshake setup (libpq's own
        # definition of the parameter), never query execution afterwards -
        # see backends/base.py's DB_CONNECT_TIMEOUT_SECONDS docstring for why
        # a wrong/unreachable host needs to fail fast here rather than
        # hanging on the OS's own (effectively unbounded) TCP connect
        # timeout. Passed as a kwarg alongside the DSN string rather than
        # appended to the URL itself - psycopg2 lets both coexist, and a
        # kwarg here always wins over anything already in descriptor["url"].
        kwargs = {"connect_timeout": DB_CONNECT_TIMEOUT_SECONDS}

        # ca_cert_pem is only ever used when the URL doesn't already name
        # its own sslrootcert - see _url_already_specifies_sslrootcert's
        # docstring for why a self-hoster's own explicit choice always
        # wins. sslmode itself is never touched here regardless (see
        # module docstring) - the user's own "?sslmode=verify-full" (or
        # any other value) in the URL is what actually turns verification
        # on; this only supplies the CA cert that mode then needs.
        temp_ca_path = None
        if ca_cert_pem and not _url_already_specifies_sslrootcert(url):
            temp_ca_path = materialize_ca_cert_tempfile(ca_cert_pem)
            kwargs["sslrootcert"] = temp_ca_path

        try:
            connection = psycopg2.connect(url, **kwargs)
        finally:
            # Only needed for the handshake inside psycopg2.connect() above
            # - libpq doesn't keep the file open/re-read it for the life of
            # the connection - so it's safe (and best practice, since this
            # is derived from user-pasted PEM text) to remove it right
            # away rather than leaving it on disk for any longer than the
            # single connect() call needs it.
            if temp_ca_path:
                try:
                    os.remove(temp_ca_path)
                except OSError:
                    pass

        if schema:
            # Same optional "schema" descriptor field backends/redshift.py
            # already supports (Redshift IS Postgres, so the identical
            # trick applies): SET the session's search_path right after
            # connecting, so every later query - both this connection's own
            # execute() calls and get_schema()'s introspection below - sees
            # `schema` as if it were the default, without the caller having
            # to schema-qualify anything. "public" stays appended after it
            # (not replaced) so objects that aren't in `schema` - e.g.
            # built-in extensions many self-hosters install into public -
            # still resolve. sql.Identifier does correct, driver-native
            # quoting/escaping for an interpolated identifier - SET has no
            # parameterized form, but this is still a safe, correct way to
            # build one (mirrors backends/redshift.py's connect() exactly).
            # Explicitly committed (this connection isn't necessarily in
            # autocommit mode the way Redshift's is from connect() onward -
            # see execute() below, which only turns autocommit on for its
            # own DML/SELECT loop) so the SET survives regardless of
            # whatever the caller does with the connection next.
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
            connection.commit()

        return connection

    def close(self, connection):
        # hasattr-guarded like backends/mysql.py's, backends/bigquery.py's,
        # and backends/snowflake.py's close() (not just `if connection:`,
        # which this used to be) - config_routes.py's /api/config handler
        # calls this unconditionally in a finally block, including in
        # tests that patch connect() with a lightweight stand-in object
        # that has no close() of its own (see
        # helpers.install_fake_postgres_connect and mysql.py's own close()
        # docstring for the original reasoning this mirrors).
        if connection is not None and hasattr(connection, "close"):
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
        port are correctly treated as the same target, not two.

        The descriptor's optional "schema" field (see connect() above) is
        appended too, but only when actually present - two presets that
        share a host/port/dbname/user but point connect() at different
        schemas must not collide on this cache the same way two different
        servers mustn't, but every existing preset (from before "schema"
        existed at all) has no such field, and this key must stay byte-
        identical for those."""
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
            key = f"{username}@{host}:{port}/{dbname or 'unknown'}"
            schema = (descriptor or {}).get("schema") or ""
            if schema:
                key += f".{schema}"
            return key
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
                WHERE c.table_schema = current_schema()
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
                WHERE c.table_schema = current_schema()
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
                WHERE tc.table_schema = current_schema()
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
                WHERE schemaname = current_schema()
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
                WHERE table_schema = current_schema();
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
                WHERE table_schema = current_schema()
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
                WHERE event_object_schema = current_schema()
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